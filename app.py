#!/usr/bin/env python3
"""Personal Flow: a private, local-first reading inbox for one person."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "personal-flow.db"
NOTE_FLOW_ROOT = Path("/Users/kumagainorihiko/Documents/Codex/2026-06-28/30-note-10-1")
THEME_SCHEMA = ROOT / "theme_suggestion_schema.json"


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            article TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS theme_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_json TEXT NOT NULL,
            flow_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            closed_at TEXT
        )
        """
    )
    existing = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
    if "article" not in existing:
        connection.execute("ALTER TABLE items ADD COLUMN article TEXT NOT NULL DEFAULT ''")
    if "flow_id" not in existing:
        connection.execute("ALTER TABLE items ADD COLUMN flow_id INTEGER")
    theme_columns = {row[1] for row in connection.execute("PRAGMA table_info(theme_runs)")}
    if "flow_id" not in theme_columns:
        connection.execute("ALTER TABLE theme_runs ADD COLUMN flow_id INTEGER")
    active = connection.execute("SELECT id FROM flows WHERE status = 'active' ORDER BY id DESC LIMIT 1").fetchone()
    if not active:
        cursor = connection.execute(
            "INSERT INTO flows(name, status, created_at) VALUES (?, 'active', ?)",
            ("今回の記事", datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        active_id = cursor.lastrowid
        connection.execute("UPDATE items SET flow_id = ? WHERE flow_id IS NULL", (active_id,))
        connection.execute("UPDATE theme_runs SET flow_id = ? WHERE flow_id IS NULL", (active_id,))
    connection.commit()
    return connection


def active_flow() -> sqlite3.Row:
    flow = database().execute("SELECT * FROM flows WHERE status = 'active' ORDER BY id DESC LIMIT 1").fetchone()
    if not flow:
        raise RuntimeError("今の記事の箱を用意できませんでした。")
    return flow


def flow_items(flow_id: int) -> list[sqlite3.Row]:
    return database().execute("SELECT * FROM items WHERE flow_id = ? ORDER BY id DESC", (flow_id,)).fetchall()


def selected_flow_items(flow_id: int, item_ids: list[str]) -> list[sqlite3.Row]:
    items = flow_items(flow_id)
    selected = {int(item_id) for item_id in item_ids if item_id.isdigit()}
    return [item for item in items if item["id"] in selected] if selected else items


def start_next_flow(previous: sqlite3.Row, keep: bool) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with database() as connection:
        if keep:
            connection.execute(
                "UPDATE flows SET name = ?, status = 'saved', closed_at = ? WHERE id = ?",
                (f"保存した情報 {now}", now, previous["id"]),
            )
        else:
            connection.execute("DELETE FROM theme_runs WHERE flow_id = ?", (previous["id"],))
            connection.execute("DELETE FROM items WHERE flow_id = ?", (previous["id"],))
            connection.execute(
                "UPDATE flows SET status = 'discarded', closed_at = ? WHERE id = ?",
                (now, previous["id"]),
            )
        connection.execute(
            "INSERT INTO flows(name, status, created_at) VALUES (?, 'active', ?)",
            ("今回の記事", now),
        )


def extract_article(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("http または https で始まるURLを入れてください。")
    request = urllib.request.Request(
        url, headers={"User-Agent": "PersonalFlow/0.2 (private local tool)"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(2_000_000).decode(
            response.headers.get_content_charset() or "utf-8", errors="replace"
        )
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    title = (
        re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()
        if title_match
        else parsed.netloc
    )
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", raw, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    if len(text) < 80:
        raise ValueError("記事本文を十分に取得できませんでした。ログインが必要なページかもしれません。")
    return title, text


def make_summary(text: str) -> str:
    """Local temporary digest. This is deliberately not presented as AI output."""
    sentences = re.split(r"(?<=[。！？.!?])\s+", text)
    useful = [sentence.strip() for sentence in sentences if len(sentence.strip()) >= 25]
    picked = useful[:4] or [text[:500]]
    return "\n".join(f"・{sentence[:180]}" for sentence in picked)


def themes(items: list[sqlite3.Row]) -> list[tuple[str, str]]:
    """Transparent local starters, based only on saved titles and personal notes."""
    selected = items[:5]
    if not selected:
        return []
    titles = " / ".join(item["title"][:52] for item in selected[:3])
    notes = [item["note"] for item in selected if item["note"]]
    focus = notes[0][:90] if notes else "最近保存した情報を、自分の言葉でどう使うか"
    return [
        ("最近のインプットから見えた、いま考えるべきこと", f"素材: {titles}\n切り口: {focus}"),
        ("情報を集めるだけで終わらせないための、自分なりの整理", f"素材: {titles}\nまず原文で確かめたい点を選ぶ。"),
        ("いま気になっているテーマを、仕事とnoteへつなぐ", f"素材: {titles}\n自分の経験と重なる部分を一つ足す。"),
    ]


def ask_codex(prompt: str, timeout: int = 180, schema: Path | None = None) -> str:
    """Use the existing Codex login. No API key is involved."""
    with tempfile.NamedTemporaryFile(prefix="personal-flow-", suffix=".txt", delete=False) as output:
        output_path = Path(output.name)
    command = ["codex", "exec", "--ephemeral", "--skip-git-repo-check"]
    if schema:
        command.extend(["--output-schema", str(schema)])
    command.extend(["--output-last-message", str(output_path), prompt])
    try:
        completed = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=timeout
        )
        if completed.returncode != 0 or not output_path.exists():
            raise RuntimeError("Youから結果を受け取れませんでした。もう一度押してください。")
        result = output_path.read_text(encoding="utf-8").strip()
        if not result:
            raise RuntimeError("Youから結果を受け取れませんでした。もう一度押してください。")
        return result
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("考えるのに時間がかかっています。少ししてからもう一度押してください。") from error
    finally:
        output_path.unlink(missing_ok=True)


def suggest_note_themes(items: list[sqlite3.Row]) -> list[dict[str, object]]:
    """Ask the signed-in Codex CLI for a small set of grounded note ideas.

    This deliberately uses the user's existing Codex login, rather than an API key.
    """
    selected = items[:8]
    if not selected:
        raise ValueError("テーマに使う情報を、少なくとも1件選んでください。")

    sources = []
    for number, item in enumerate(selected, start=1):
        excerpt = item["article"][:5000]
        sources.append(
            f"【保存 {number}】\n"
            f"タイトル: {item['title']}\n"
            f"URL: {item['url']}\n"
            f"自分のメモ: {item['note'] or 'なし'}\n"
            f"本文の抜粋: {excerpt}"
        )
    prompt = """あなたは、個人が最近読んだ情報からnote記事の種を見つける編集パートナーです。
以下の保存情報を横断して読み、note記事のテーマを3〜4案だけ提案してください。

守ること:
- 単に各記事を言い換えず、複数の記事をつなぐ共通点・対比・本人のメモから切り口を作る。
- 事実を作らない。根拠にした保存番号を各案に添える。
- 各案はタイトル、何を書くか（2文以内）、使う保存番号を返す。
- ありきたりな案より、本人が自分の経験や考えを足して書ける案を優先する。
- 断定できない点は、原文を読み直すべき点として短く示す。

保存情報:
""" + "\n\n".join(sources)
    try:
        proposal = json.loads(ask_codex(prompt, schema=THEME_SCHEMA))
        themes = proposal.get("themes", [])
        if not isinstance(themes, list) or not themes:
            raise ValueError
        return themes
    except (json.JSONDecodeError, ValueError, AttributeError) as error:
        raise RuntimeError("テーマ案の形を読み取れませんでした。もう一度押してください。") from error


def note_flow_rules() -> str:
    files = [
        "outputs/Brain_Codex_note1万円/文章ルール.md",
        "note記事生成ルール.md",
        "outputs/Brain_Codex_note1万円/10_現在地.md",
    ]
    try:
        return "\n\n".join(
            f"【{file}】\n{(NOTE_FLOW_ROOT / file).read_text(encoding='utf-8')}"
            for file in files
        )
    except OSError as error:
        raise RuntimeError("note記事化フローのルールを読めませんでした。") from error


def make_note_draft(theme: dict[str, object], items: list[sqlite3.Row], memo: str, length: str) -> str:
    source_numbers = {int(number) for number in theme.get("sources", []) if str(number).isdigit()}
    source_items = [item for number, item in enumerate(items, start=1) if number in source_numbers]
    if not source_items:
        source_items = items[:3]
    sources = "\n\n".join(
        f"【保存記事 {number}】\nタイトル: {item['title']}\nURL: {item['url']}\n"
        f"自分のメモ: {item['note'] or 'なし'}\n本文の抜粋: {item['article'][:5000]}"
        for number, item in enumerate(source_items, start=1)
    )
    prompt = f"""あなたはnote記事化フローの実行役です。以下のルールを守り、Personal Flowから渡されたテーマを記事の下書きにしてください。

【選ばれたテーマ】
タイトル: {theme.get('title', '')}
何を書くか: {theme.get('approach', '')}

【本人の追加メモ】
{memo or 'まだ追加メモはありません。保存記事から分かる事実だけを使い、足りない本人の体験は作らない。'}

【希望文字数】
{length or '約2,000字'}

【保存記事】
{sources}

【note記事化フローのルール】
{note_flow_rules()}

出力順:
1. タイトル案3つ
2. 記事の軸（1文）
3. H2見出し案
4. 本文初稿
5. 公開前チェック
6. ハッシュタグ案

保存記事は参考材料であり、記事の表現や構成をコピーしない。本人の事実・本人の意見・保存記事の事例を混ぜない。"""
    return ask_codex(prompt, timeout=240)


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} | Personal Flow</title>
<style>
:root{{--ink:#1d2421;--green:#12675b;--paper:#f6f4ef;--line:#dedbd1;--muted:#6b706b;--card:#fffefa}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif;line-height:1.55}}
main{{max-width:1180px;margin:0 auto;padding:46px 24px 72px}} header{{display:flex;justify-content:space-between;gap:28px;align-items:end;border-bottom:1px solid var(--line);padding-bottom:24px;margin-bottom:24px}}
h1{{font-size:42px;letter-spacing:-.05em;margin:0}} h2{{font-size:18px;margin:0 0 14px}} h3{{font-size:16px;margin:0 0 7px}} .eyebrow{{color:var(--green);font-weight:800;font-size:12px;letter-spacing:.08em;margin:0 0 6px}} .sub{{color:var(--muted);max-width:460px;margin:0;font-size:14px}}
.grid{{display:grid;grid-template-columns:minmax(310px,.9fr) minmax(390px,1.25fr);gap:20px;align-items:start}} .panel,.card{{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:0 8px 22px #25251a09}} .panel{{padding:22px}} .card{{padding:18px;margin-top:12px}}
label{{display:block;font-weight:750;font-size:13px;margin-top:15px}} input,textarea{{width:100%;font:inherit;border:1px solid #cac7bd;border-radius:9px;padding:12px;background:#fff;margin-top:6px}} textarea{{min-height:100px;resize:vertical}}
button,.button{{display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:9px;padding:12px 15px;background:var(--ink);color:#fff;font-weight:750;font:inherit;text-decoration:none;cursor:pointer}} button.primary{{background:var(--green);width:100%;margin-top:18px}} .hint{{font-size:12px;color:var(--muted);margin:9px 0 0}}
.section-head{{display:flex;justify-content:space-between;align-items:center;gap:12px}} .count{{font-size:12px;color:var(--muted);background:#edebe5;border-radius:99px;padding:3px 8px}} .meta{{font-size:12px;color:var(--muted)}} .summary{{white-space:pre-wrap;font-size:14px;margin:12px 0}} .note{{background:#f2f5ef;border-radius:8px;padding:10px 12px;font-size:13px}} a{{color:var(--green)}} .empty{{color:var(--muted);padding:24px 0;text-align:center}}
.actions{{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}} .outline{{background:transparent;color:var(--ink);border:1px solid var(--line)}} .theme{{border-left:4px solid var(--green)}} .theme p{{margin:4px 0 0;white-space:pre-line;color:#4f5551;font-size:14px}} .message{{padding:12px 15px;border-radius:10px;background:#fff1d7;color:#684611;margin-bottom:16px}} .pick{{display:flex;align-items:center;gap:9px;font-size:13px;font-weight:750;color:var(--green);cursor:pointer}} .pick input{{width:18px;height:18px;margin:0;accent-color:var(--green)}}
@media(max-width:760px){{main{{padding:28px 14px}}header{{display:block}}header .sub{{margin-top:12px}}h1{{font-size:34px}}.grid{{grid-template-columns:1fr}}}}
</style></head><body><main>{body}</main><script>
document.querySelectorAll("form[action='/suggest'], form[action='/draft']").forEach((form) => {{
  form.addEventListener("submit", () => {{
    const button = form.querySelector("button");
    button.disabled = true;
    button.textContent = form.action.endsWith("/draft") ? "Youがnoteの下書きを作っています…" : "Youがテーマを考えています…";
  }});
}});
</script></body></html>""".encode()


class PersonalFlowHandler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        return

    def send_html(self, content: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        route = urlparse(self.path)
        if route.path not in {"/", "/themes", "/theme-run", "/article-form", "/finish", "/choose-sources"}:
            self.send_html(page("見つかりません", "<h1>見つかりません</h1>"), 404)
            return
        flow = active_flow()
        items = flow_items(flow["id"])
        query = parse_qs(route.query)
        if route.path == "/finish":
            body = f"""
            <header><div><p class='eyebrow'>FINISH THIS ARTICLE</p><h1>今回の記事を終える</h1></div><p class='sub'>今の{len(items)}件を、次の記事の材料に混ぜないための区切りです。</p></header>
            <section class='panel'><h2>この{len(items)}件をどうしますか？</h2>
            <p class='hint'>「保存」はあとで見返せる履歴にします。次の記事のテーマ提案には入りません。「消す」は今回の情報だけを削除します。</p>
            <div class='actions'><form method='post' action='/finish-action'><input type='hidden' name='keep' value='yes'><button type='submit'>保存して、新しい記事を始める</button></form>
            <form method='post' action='/finish-action'><input type='hidden' name='keep' value='no'><button class='outline' type='submit'>保存せずに、新しい記事を始める</button></form>
            <a class='button outline' href='/'>まだ続ける</a></div></section>"""
            self.send_html(page("今回の記事を終える", body))
            return
        if route.path == "/choose-sources":
            source_cards = "".join(
                f"<article class='card'><label class='pick'><input type='checkbox' name='item_ids' value='{item['id']}'>この情報をテーマに使う</label>"
                f"<div class='meta'>{html.escape(item['created_at'])}</div><h3>{html.escape(item['title'])}</h3>"
                f"<a href='{html.escape(item['url'], quote=True)}' target='_blank' rel='noreferrer'>原文を開く ↗</a>"
                f"<div class='summary'>{html.escape(item['summary'])}</div></article>"
                for item in items
            ) or "<div class='empty'>まず情報を保存してください。</div>"
            body = f"""
            <header><div><p class='eyebrow'>CHOOSE SOURCES</p><h1>テーマに使う情報を選ぶ</h1></div><p class='sub'>今回の記事に使いたい情報だけを選びます。1件でも、複数の記事を組み合わせても大丈夫です。</p></header>
            <form method='post' action='/suggest'><section class='panel'>{source_cards}<div class='actions'><button type='submit'>選んだ情報からnoteテーマを提案</button><a class='button outline' href='/'>戻る</a></div></section></form>"""
            self.send_html(page("テーマに使う情報を選ぶ", body))
            return
        if route.path == "/theme-run":
            run = self.load_theme_run(query)
            if not run:
                self.send_html(page("見つかりません", "<h1>テーマ案が見つかりません</h1><p><a href='/'>保存した情報へ戻る</a></p>"), 404)
                return
            proposal = json.loads(run["proposal_json"])
            theme_cards = "".join(
                f"<article class='card theme'><h3>{html.escape(str(theme['title']))}</h3>"
                f"<p>{html.escape(str(theme['approach']))}</p>"
                f"<p class='meta'>根拠にした保存記事: {html.escape('・'.join(str(n) for n in theme['sources']))}</p>"
                f"<form method='get' action='/article-form'><input type='hidden' name='run_id' value='{run['id']}'>"
                f"<input type='hidden' name='theme' value='{index}'><button type='submit'>このテーマでnote記事にする</button></form></article>"
                for index, theme in enumerate(proposal["themes"])
            )
            body = f"""
            <header><div><p class='eyebrow'>NOTE THEME PROPOSAL</p><h1>書けそうなテーマ</h1></div><p class='sub'>今の記事の箱にある{len(items)}件をまとめて読んだ結果です。テーマを1つ選ぶと、note記事化フローへ渡します。</p></header>
            <section class='panel'>{theme_cards}<div class='actions'><a class='button outline' href='/'>保存した情報へ戻る</a></div></section>"""
            self.send_html(page("noteテーマ", body))
            return
        if route.path == "/article-form":
            run = self.load_theme_run(query)
            theme = self.load_theme(run, query)
            if not run or not theme:
                self.send_html(page("見つかりません", "<h1>選んだテーマが見つかりません</h1><p><a href='/'>保存した情報へ戻る</a></p>"), 404)
                return
            body = f"""
            <header><div><p class='eyebrow'>NOTE ARTICLE FLOW</p><h1>記事にする前のメモ</h1></div><p class='sub'>テーマと保存記事はすでに渡ります。ここには、あなた自身の出来事や本音だけを足してください。</p></header>
            <section class='panel'><h2>{html.escape(str(theme['title']))}</h2><p class='hint'>{html.escape(str(theme['approach']))}</p>
            <form method='post' action='/draft'><input type='hidden' name='run_id' value='{run['id']}'><input type='hidden' name='theme' value='{query.get('theme', ['0'])[0]}'>
            <label>希望文字数</label><input name='length' value='約2,000字'>
            <label>自分メモ（任意）</label><textarea name='memo' placeholder='実際にあったこと／自分が思ったこと／残したい言葉。箇条書きで大丈夫です。'></textarea>
            <button class='primary' type='submit'>note記事の下書きを作る</button></form></section>"""
            self.send_html(page("記事にする", body))
            return
        cards = "".join(
            f"<article class='card'><div class='meta'>{html.escape(item['created_at'])}</div><h3>{html.escape(item['title'])}</h3>"
            f"<a href='{html.escape(item['url'], quote=True)}' target='_blank' rel='noreferrer'>原文を開く ↗</a>"
            f"<div class='summary'>{html.escape(item['summary'])}</div>"
            f"<div class='note'><strong>自分のメモ</strong><br>{html.escape(item['note']) or 'まだメモはありません。'}</div></article>"
            for item in items
        ) or "<div class='empty'>まだ何もありません。最初のURLを入れてみよう。</div>"
        theme_cards = ""
        if route.path == "/themes":
            candidates = themes(items)
            theme_cards = "".join(
                f"<article class='card theme'><h3>{html.escape(name)}</h3><p>{html.escape(detail)}</p></article>"
                for name, detail in candidates
            ) or "<div class='empty'>まず記事を保存すると、ここに考える入口が出る。</div>"
        body = f"""
<header><div><p class='eyebrow'>PRIVATE KNOWLEDGE INBOX</p><h1>Personal Flow</h1></div><p class='sub'>流れてきた情報を受け取り、あとで自分の言葉とnoteへつなげるための、あなた専用の場所。</p></header>
<div class='grid'><section class='panel'><h2>情報を入れる</h2><p class='hint'>URLを貼るだけ。データはこのMacの中に保存される。</p>
<form method='post' action='/save'><label>記事・動画・ページのURL</label><input name='url' type='url' placeholder='https://...' required>
<label>ひとことメモ（任意）</label><textarea name='note' placeholder='なぜ気になったか／何に使えそうか'></textarea><button class='primary' type='submit'>保存して要点を見る</button></form></section>
<section class='panel'><div class='section-head'><h2>今回の記事の情報</h2><span class='count'>{len(items)} 件</span></div><div class='actions'><a class='button' href='/choose-sources'>テーマに使う情報を選ぶ</a><a class='button outline' href='/finish'>今回の記事を終える</a><a class='button outline' href='/themes'>仮の入口を見る</a></div><p class='hint'>テーマに使う記事を選んでから、Youにテーマ案を頼めます。</p>{theme_cards}{cards}</section></div>"""
        self.send_html(page("情報受け箱", body))

    def load_theme_run(self, query: dict[str, list[str]]) -> sqlite3.Row | None:
        run_id = query.get("run_id", query.get("id", [""]))[0]
        if not run_id.isdigit():
            return None
        return database().execute("SELECT * FROM theme_runs WHERE id = ?", (run_id,)).fetchone()

    def load_theme(self, run: sqlite3.Row | None, query: dict[str, list[str]]) -> dict[str, object] | None:
        if not run:
            return None
        theme_index = query.get("theme", [""])[0]
        if not theme_index.isdigit():
            return None
        themes = json.loads(run["proposal_json"]).get("themes", [])
        index = int(theme_index)
        return themes[index] if 0 <= index < len(themes) else None

    def do_POST(self) -> None:
        if self.path == "/finish-action":
            length = int(self.headers.get("Content-Length", "0"))
            values = parse_qs(self.rfile.read(length).decode())
            keep = values.get("keep", [""])[0] == "yes"
            start_next_flow(active_flow(), keep)
            message = "保存して、新しい記事の箱を始めました。" if keep else "今回の情報を消して、新しい記事の箱を始めました。"
            body = f"<h1>新しい記事を始められます</h1><p class='message'>{message}</p><p><a class='button' href='/'>空の箱を開く</a></p>"
            self.send_html(page("新しい記事を始める", body))
            return
        if self.path == "/suggest":
            length = int(self.headers.get("Content-Length", "0"))
            values = parse_qs(self.rfile.read(length).decode())
            flow = active_flow()
            selected_ids = values.get("item_ids", [])
            if not selected_ids:
                body = "<h1>情報が選ばれていません</h1><p class='message'>テーマに使う情報を、少なくとも1件チェックしてください。</p><p><a href='/choose-sources'>情報を選ぶ画面へ戻る</a></p>"
                self.send_html(page("情報を選んでください", body), 400)
                return
            items = selected_flow_items(flow["id"], selected_ids)
            try:
                proposal = suggest_note_themes(items)
                with database() as connection:
                    cursor = connection.execute(
                        "INSERT INTO theme_runs(proposal_json, flow_id, created_at) VALUES (?, ?, ?)",
                        (
                            json.dumps({"themes": proposal, "item_ids": [item["id"] for item in items]}, ensure_ascii=False),
                            flow["id"],
                            datetime.now().strftime("%Y-%m-%d %H:%M"),
                        ),
                    )
                self.send_response(303)
                self.send_header("Location", f"/theme-run?id={cursor.lastrowid}")
                self.end_headers()
            except (ValueError, RuntimeError) as error:
                body = f"<h1>テーマを出せませんでした</h1><p class='message'>{html.escape(str(error))}</p><p><a href='/'>保存した情報へ戻る</a></p>"
                self.send_html(page("テーマを出せませんでした", body), 400)
            return
        if self.path == "/draft":
            length = int(self.headers.get("Content-Length", "0"))
            values = parse_qs(self.rfile.read(length).decode())
            run = self.load_theme_run(values)
            theme = self.load_theme(run, values)
            if not run or not theme:
                self.send_html(page("見つかりません", "<h1>選んだテーマが見つかりません</h1>"), 404)
                return
            memo = values.get("memo", [""])[0].strip()
            desired_length = values.get("length", [""])[0].strip()
            proposal = json.loads(run["proposal_json"])
            selected_ids = [str(item_id) for item_id in proposal.get("item_ids", [])]
            items = selected_flow_items(run["flow_id"], selected_ids)
            try:
                draft = make_note_draft(theme, items, memo, desired_length)
                body = f"""
                <header><div><p class='eyebrow'>NOTE ARTICLE DRAFT</p><h1>note記事の下書き</h1></div><p class='sub'>選んだテーマと保存記事を、note記事化フローへ渡して作った下書きです。</p></header>
                <section class='panel'><div class='summary'>{html.escape(draft)}</div><div class='actions'><a class='button' href='/'>保存した情報へ戻る</a></div></section>"""
                self.send_html(page("note記事の下書き", body))
            except RuntimeError as error:
                body = f"<h1>下書きを作れませんでした</h1><p class='message'>{html.escape(str(error))}</p><p><a href='/'>保存した情報へ戻る</a></p>"
                self.send_html(page("下書きを作れませんでした", body), 400)
            return
        if self.path != "/save":
            self.send_html(page("見つかりません", "<h1>見つかりません</h1>"), 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        values = parse_qs(self.rfile.read(length).decode())
        url = values.get("url", [""])[0].strip()
        note = values.get("note", [""])[0].strip()
        try:
            title, article = extract_article(url)
            flow = active_flow()
            with database() as connection:
                connection.execute(
                    "INSERT INTO items(url, title, article, summary, note, created_at, flow_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (url, title, article, make_summary(article), note, datetime.now().strftime("%Y-%m-%d %H:%M"), flow["id"]),
                )
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        except (ValueError, OSError, urllib.error.URLError) as error:
            body = f"<h1>保存できませんでした</h1><p class='message'>{html.escape(str(error))}</p><p><a href='/'>戻る</a></p>"
            self.send_html(page("保存できませんでした", body), 400)


if __name__ == "__main__":
    database().close()
    port = int(os.environ.get("PERSONAL_FLOW_PORT", "8765"))
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", port), PersonalFlowHandler)
    print(f"Personal Flow is running at http://127.0.0.1:{port}")
    server.serve_forever()
