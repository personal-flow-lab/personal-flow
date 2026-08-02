#!/usr/bin/env python3
"""Personal Flow: a private, local-first reading inbox for one person."""

from __future__ import annotations

import html
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "personal-flow.db"


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
    existing = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
    if "article" not in existing:
        connection.execute("ALTER TABLE items ADD COLUMN article TEXT NOT NULL DEFAULT ''")
    return connection


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
.actions{{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}} .outline{{background:transparent;color:var(--ink);border:1px solid var(--line)}} .theme{{border-left:4px solid var(--green)}} .theme p{{margin:4px 0 0;white-space:pre-line;color:#4f5551;font-size:14px}} .message{{padding:12px 15px;border-radius:10px;background:#fff1d7;color:#684611;margin-bottom:16px}}
@media(max-width:760px){{main{{padding:28px 14px}}header{{display:block}}header .sub{{margin-top:12px}}h1{{font-size:34px}}.grid{{grid-template-columns:1fr}}}}
</style></head><body><main>{body}</main></body></html>""".encode()


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
        if route.path not in {"/", "/themes"}:
            self.send_html(page("見つかりません", "<h1>見つかりません</h1>"), 404)
            return
        items = database().execute("SELECT * FROM items ORDER BY id DESC").fetchall()
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
<section class='panel'><div class='section-head'><h2>保存した情報</h2><span class='count'>{len(items)} 件</span></div><div class='actions'><a class='button outline' href='/themes'>最近の情報からnoteの入口を見る</a><a class='button outline' href='/'>一覧へ戻る</a></div>{theme_cards}{cards}</section></div>"""
        self.send_html(page("情報受け箱", body))

    def do_POST(self) -> None:
        if self.path != "/save":
            self.send_html(page("見つかりません", "<h1>見つかりません</h1>"), 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        values = parse_qs(self.rfile.read(length).decode())
        url = values.get("url", [""])[0].strip()
        note = values.get("note", [""])[0].strip()
        try:
            title, article = extract_article(url)
            with database() as connection:
                connection.execute(
                    "INSERT INTO items(url, title, article, summary, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (url, title, article, make_summary(article), note, datetime.now().strftime("%Y-%m-%d %H:%M")),
                )
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        except (ValueError, OSError, urllib.error.URLError) as error:
            body = f"<h1>保存できませんでした</h1><p class='message'>{html.escape(str(error))}</p><p><a href='/'>戻る</a></p>"
            self.send_html(page("保存できませんでした", body), 400)


if __name__ == "__main__":
    database().close()
    server = ThreadingHTTPServer(("127.0.0.1", 8765), PersonalFlowHandler)
    print("Personal Flow is running at http://127.0.0.1:8765")
    server.serve_forever()
