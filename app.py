#!/usr/bin/env python3
"""Personal Flow: a private, local-first reading inbox for one person."""

from __future__ import annotations

import cgi
import html
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "personal-flow.db"
NOTE_FLOW_ROOT = ROOT / "note-flow-rules"
THEME_SCHEMA = ROOT / "theme_suggestion_schema.json"
X_POST_SCHEMA = ROOT / "x_post_schema.json"
DIAGNOSTIC_LOG_PATH = ROOT / "personal-flow-diagnostics.log"
CODEX_PATH = os.environ.get("PERSONAL_FLOW_CODEX", str(Path.home() / ".local/bin/codex"))
THEME_MODEL = os.environ.get("PERSONAL_FLOW_THEME_MODEL", "gpt-5.4-mini")
THEME_RETRY_MODEL = os.environ.get("PERSONAL_FLOW_THEME_RETRY_MODEL", "gpt-5.6-sol")
THEME_REASONING_EFFORT = os.environ.get("PERSONAL_FLOW_THEME_REASONING", "low")
THEME_PRIMARY_TIMEOUT = 75
THEME_RETRY_TIMEOUT = 90
DIRECTION_PRIMARY_TIMEOUT = 75
DIRECTION_RETRY_TIMEOUT = 90
DEFAULT_NOTE_PROFILE = "https://note.com/light_bee885"

X_POST_STYLE = """- 元パン屋で、異業種からパソコンとAIを触り始めた途中にいる人の口調にする。
- できる人の解説ではなく、分からない側から実際に試した記録として書く。
- 専門家ぶった断定、煽り、過剰な成果アピールをしない。
- 材料にない経験、数字、成果、感情を足さない。
- 読む人に命令せず、自分が考えたこと・次に試したいことへ自然に戻す。"""


class CodexRunError(RuntimeError):
    """A classified failure from the local Codex CLI process."""

    def __init__(self, code: str, user_message: str):
        super().__init__(user_message)
        self.code = code


class ThemeSuggestionError(RuntimeError):
    """A classified failure while turning Codex output into note themes."""

    def __init__(self, code: str, user_message: str):
        super().__init__(user_message)
        self.code = code


class DirectionSuggestionError(RuntimeError):
    """A classified failure while refining a chosen note direction."""

    def __init__(self, code: str, user_message: str):
        super().__init__(user_message)
        self.code = code


def record_local_error(area: str, code: str, detail: str) -> None:
    """Keep technical details on this Mac without exposing them in the page."""
    try:
        logger = logging.getLogger(f"personal-flow:{DIAGNOSTIC_LOG_PATH}")
        if not logger.handlers:
            handler = RotatingFileHandler(
                DIAGNOSTIC_LOG_PATH,
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.ERROR)
            logger.propagate = False
        compact_detail = re.sub(r"\s+", " ", detail).strip()[:4000]
        logger.error("area=%s code=%s detail=%s", area, code, compact_detail or "none")
    except OSError:
        # A logging problem must never replace the original, user-facing error.
        return


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
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS direction_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            theme_run_id INTEGER NOT NULL,
            theme_index INTEGER NOT NULL,
            direction_notes TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
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


def user_context() -> str:
    row = database().execute("SELECT value FROM app_settings WHERE key = 'user_context'").fetchone()
    return row["value"].strip() if row else ""


def save_user_context(value: str) -> None:
    with database() as connection:
        connection.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES ('user_context', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (value.strip(), datetime.now().strftime("%Y-%m-%d %H:%M")),
        )


def flow_items(flow_id: int) -> list[sqlite3.Row]:
    return database().execute("SELECT * FROM items WHERE flow_id = ? ORDER BY id DESC", (flow_id,)).fetchall()


def selected_flow_items(flow_id: int, item_ids: list[str]) -> list[sqlite3.Row]:
    items = flow_items(flow_id)
    selected = {int(item_id) for item_id in item_ids if item_id.isdigit()}
    return [item for item in items if item["id"] in selected]


def flow_item(flow_id: int, item_id: str) -> sqlite3.Row | None:
    if not item_id.isdigit():
        return None
    return database().execute(
        "SELECT * FROM items WHERE id = ? AND flow_id = ?",
        (int(item_id), flow_id),
    ).fetchone()


def delete_flow_item(flow_id: int, item_id: str) -> bool:
    if not item_id.isdigit():
        return False
    with database() as connection:
        cursor = connection.execute(
            "DELETE FROM items WHERE id = ? AND flow_id = ?",
            (int(item_id), flow_id),
        )
    return cursor.rowcount == 1


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


def note_profile_articles(profile_url: str) -> list[str]:
    """Collect public note article links from one creator page, without using an account."""
    parsed = urlparse(profile_url)
    if parsed.netloc != "note.com" or not parsed.path.strip("/"):
        raise ValueError("noteのプロフィールURLを入れてください。")
    creator = parsed.path.strip("/").split("/")[0]
    request = urllib.request.Request(profile_url, headers={"User-Agent": "PersonalFlow/0.3 (private local tool)"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(3_000_000).decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    found = re.findall(rf'(?:https://note\\.com)?/{re.escape(creator)}/n/(n[a-z0-9]+)', raw, re.I)
    links: list[str] = []
    for note_id in found:
        link = f"https://note.com/{creator}/n/{note_id}"
        if link not in links:
            links.append(link)
    if not links:
        raise ValueError("note記事を取得できませんでした。公開されているプロフィールページか確認してください。")
    return links[:40]


def build_user_context(profile_url: str) -> str:
    links = note_profile_articles(profile_url)
    material: list[str] = []
    for number, link in enumerate(links, start=1):
        try:
            title, article = extract_article(link)
            material.append(f"【記事 {number}】{title}\n{article[:1800]}")
        except (ValueError, OSError, urllib.error.URLError):
            continue
    if not material:
        raise RuntimeError("記事本文を取得できませんでした。")
    prompt = """あなたは、個人の公開note記事を読み、その人専用の『Personal Flowの土台』を作る編集者です。
以下は本人が書いた記事です。記事そのものを要約・コピーするのではなく、今後の記事の素材選びに使える、短い判断基準を作ってください。

必ず守ること:
- 書かれていない経歴、感情、主張を作らない。
- 本人の意見と、記事タイトル・記述からの推測を混ぜない。
- 過去の自分を固定しない。新しいテーマも選べる余白を残す。
- 900〜1,400字程度の日本語に圧縮する。

出力の形:
1. 本人について確実に言えること
2. 繰り返し出る関心・問い
3. 反応しやすい情報の特徴
4. noteで生きる材料（経験・視点）
5. 避けたいこと・注意点
6. Personal Flowへの短い指示

公開note記事:\n""" + "\n\n".join(material)
    return ask_codex(prompt, timeout=300)


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


def ask_codex(
    prompt: str,
    timeout: int = 180,
    schema: Path | None = None,
    images: list[Path] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    ignore_user_config: bool = False,
) -> str:
    """Use the existing Codex login. No API key is involved."""
    with tempfile.NamedTemporaryFile(prefix="personal-flow-", suffix=".txt", delete=False) as output:
        output_path = Path(output.name)
    command = [CODEX_PATH, "exec", "--ephemeral", "--skip-git-repo-check"]
    if ignore_user_config:
        command.append("--ignore-user-config")
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    for image in images or []:
        command.extend(["--image", str(image)])
    if schema:
        command.extend(["--output-schema", str(schema)])
    command.extend(["--output-last-message", str(output_path), prompt])
    try:
        try:
            completed = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as error:
            record_local_error(
                "codex",
                "timeout",
                f"timeout={timeout}s prompt_chars={len(prompt)} model={model or 'default'} reasoning={reasoning_effort or 'default'}",
            )
            raise CodexRunError(
                "timeout",
                "Codexからの返答に時間がかかりすぎたため、いったん中断しました。少ししてから、もう一度試してください。",
            ) from error
        except OSError as error:
            record_local_error(
                "codex",
                "start_failed",
                f"executable={CODEX_PATH} error={type(error).__name__}: {error}",
            )
            raise CodexRunError(
                "start_failed",
                "Codexを使う機能を起動できませんでした。詳しい原因はこのMacの記録に残しました。Personal Flowを開き直してから、もう一度試してください。",
            ) from error
        if completed.returncode != 0:
            record_local_error(
                "codex",
                "process_failed",
                f"returncode={completed.returncode} model={model or 'default'} reasoning={reasoning_effort or 'default'} stderr={(completed.stderr or '')[-3000:]}",
            )
            raise CodexRunError(
                "process_failed",
                "処理の途中でCodexが止まりました。少ししてから、もう一度試してください。",
            )
        try:
            result = output_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            record_local_error("codex", "output_read_failed", f"{type(error).__name__}: {error}")
            raise CodexRunError(
                "output_read_failed",
                "Codexから返ってきた内容を読み込めませんでした。詳しい原因はこのMacの記録に残しました。もう一度試してください。",
            ) from error
        if not result:
            record_local_error("codex", "empty_output", "Codex finished without a last message")
            raise CodexRunError(
                "empty_output",
                "Codexから内容が返ってきませんでした。もう一度試してください。",
            )
        return result
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except OSError as error:
            record_local_error("codex", "temp_cleanup_failed", f"{type(error).__name__}: {error}")


def build_theme_prompt(
    selected: list[sqlite3.Row],
    user_angle: str,
    context: str,
    compact: bool = False,
) -> str:
    """Build a bounded prompt so a few articles do not become a large agent task."""
    sources = []
    for number, item in enumerate(selected, start=1):
        summary_limit = 650 if compact else 900
        excerpt_limit = 450 if compact else 1600
        note_limit = 500 if compact else 900
        summary = str(item["summary"] or "").strip()[:summary_limit]
        excerpt = str(item["article"] or "").strip()[:excerpt_limit]
        note = str(item["note"] or "").strip()[:note_limit]
        sources.append(
            f"【保存 {number}】\n"
            f"タイトル: {str(item['title'])[:300]}\n"
            f"自分のメモ: {note or 'なし'}\n"
            f"要点: {summary or 'なし'}\n"
            f"本文の短い抜粋: {excerpt or 'なし'}"
        )
    retry_note = "今回は短くした材料だけで判断し、ツールや外部情報を使わないでください。\n" if compact else ""
    angle_limit = 800 if compact else 1200
    context_limit = 500 if compact else 900
    angle_text = user_angle[:angle_limit] or "まだ指定なし。本人の経験や気持ちは作らない。"
    context_text = context[:context_limit] or "まだ土台はありません。保存情報だけを根拠にする。"
    source_text = "\n\n".join(sources)
    return f"""あなたは、保存情報からnote記事の種を見つける編集パートナーです。
{retry_note}以下の材料だけを読み、note記事のテーマを3案提案してください。ツール、検索、ファイル操作は不要です。

守ること:
- 単に各記事を言い換えず、複数の記事をつなぐ共通点・対比・本人のメモから切り口を作る。
- 事実を作らない。根拠にした保存番号を各案に添える。
- 各案はタイトル、何を書くか（3〜5文）、組み合わせた理由、中心にしない情報、使う保存番号を返す。
- ありきたりな案より、本人が自分の経験や考えを足して書ける案を優先する。
- 返答は指定されたJSONの形だけにする。説明文、前置き、Markdownの囲みは付けない。
- themesには必ず3案を入れ、各案のtitle、approach、picked、left_outを空にしない。
- sourcesには、この依頼に実在する保存番号だけを1つ以上入れる。

【今回の記事で入れたい本人の思い】
{angle_text}

【本人のPersonal Flowの土台】
{context_text}

【保存情報】
{source_text}"""


def parse_theme_proposal(raw_proposal: str, source_count: int) -> list[dict[str, object]]:
    """Validate the structured result before it reaches the page."""
    try:
        proposal = json.loads(raw_proposal)
    except json.JSONDecodeError as error:
        record_local_error(
            "theme_suggestion",
            "invalid_json",
            f"line={error.lineno} column={error.colno} output={raw_proposal[:2000]}",
        )
        raise ThemeSuggestionError(
            "invalid_json",
            "Codexから返ったテーマの形を読み取れませんでした。もう一度試してください。",
        ) from error
    if not isinstance(proposal, dict) or not isinstance(proposal.get("themes"), list):
        record_local_error("theme_suggestion", "invalid_format", f"output={raw_proposal[:2000]}")
        raise ThemeSuggestionError(
            "invalid_format",
            "Codexから返ったテーマの並び方が想定と違いました。もう一度試してください。",
        )
    result = proposal["themes"]
    if not result:
        record_local_error("theme_suggestion", "empty_themes", f"output={raw_proposal[:2000]}")
        raise ThemeSuggestionError(
            "empty_themes",
            "テーマ候補が0件でした。選ぶ情報を変えるか、同じ内容でもう一度試してください。",
        )
    if len(result) < 3 or len(result) > 4:
        record_local_error(
            "theme_suggestion",
            "invalid_theme_count",
            f"theme_count={len(result)} output={raw_proposal[:2000]}",
        )
        raise ThemeSuggestionError(
            "invalid_theme_count",
            "返ってきたテーマの件数が想定と違いました。もう一度試してください。",
        )
    required_text = {"title", "approach", "picked", "left_out"}
    valid_source_numbers = set(range(1, source_count + 1))
    for index, theme in enumerate(result, start=1):
        sources_value = theme.get("sources") if isinstance(theme, dict) else None
        if (
            not isinstance(theme, dict)
            or any(not isinstance(theme.get(key), str) or not theme[key].strip() for key in required_text)
            or not isinstance(sources_value, list)
            or not sources_value
            or any(not isinstance(number, int) or number not in valid_source_numbers for number in sources_value)
        ):
            record_local_error(
                "theme_suggestion",
                "invalid_theme",
                f"theme_index={index} output={raw_proposal[:2000]}",
            )
            raise ThemeSuggestionError(
                "invalid_theme",
                "テーマ案の一部が不足していたため、安全に表示できませんでした。もう一度試してください。",
            )
    return result


def theme_error_from_codex(error: CodexRunError) -> ThemeSuggestionError:
    messages = {
        "timeout": "短くした材料でもテーマ作成が時間内に終わりませんでした。少ししてから、もう一度試してください。",
        "start_failed": "テーマを考える機能を起動できませんでした。詳しい原因はこのMacの記録に残しました。Personal Flowを開き直してから、もう一度試してください。",
        "process_failed": "テーマを考える途中でCodexが止まりました。少ししてから、もう一度試してください。",
        "output_read_failed": "返ってきたテーマを読み込めませんでした。詳しい原因はこのMacの記録に残しました。もう一度試してください。",
        "empty_output": "Codexからテーマ候補が返ってきませんでした。もう一度試してください。",
    }
    return ThemeSuggestionError(error.code, messages.get(error.code, str(error)))


def suggest_note_themes(items: list[sqlite3.Row], user_angle: str = "") -> list[dict[str, object]]:
    """Generate themes in an isolated, bounded Codex process with one compact retry."""
    selected = items[:8]
    if not selected:
        raise ValueError("テーマに使う情報を、少なくとも1件選んでください。")

    context = user_context()
    attempts = lightweight_note_attempts(
        build_theme_prompt(selected, user_angle, context),
        build_theme_prompt(selected, user_angle, context, compact=True),
        THEME_PRIMARY_TIMEOUT,
        THEME_RETRY_TIMEOUT,
    )
    retryable = {
        "timeout",
        "process_failed",
        "empty_output",
        "invalid_json",
        "invalid_format",
        "empty_themes",
        "invalid_theme_count",
        "invalid_theme",
    }
    for attempt_index, (attempt_name, prompt, timeout, model) in enumerate(attempts):
        try:
            raw_proposal = ask_codex(
                prompt,
                timeout=timeout,
                schema=THEME_SCHEMA,
                model=model,
                reasoning_effort=THEME_REASONING_EFFORT,
                ignore_user_config=True,
            )
            return parse_theme_proposal(raw_proposal, len(selected))
        except CodexRunError as error:
            theme_error = theme_error_from_codex(error)
        except ThemeSuggestionError as error:
            theme_error = error
        if attempt_index == 0 and theme_error.code in retryable:
            record_local_error(
                "theme_suggestion",
                "automatic_retry",
                f"first_error={theme_error.code} first_prompt_chars={len(prompt)} retry_prompt_chars={len(attempts[1][1])}",
            )
            continue
        raise theme_error
    raise ThemeSuggestionError("unknown", "テーマを作れませんでした。もう一度試してください。")


def lightweight_note_attempts(
    primary_prompt: str,
    compact_prompt: str,
    primary_timeout: int,
    retry_timeout: int,
) -> list[tuple[str, str, int, str]]:
    """Use the same isolated, low-effort profile for short note-planning tasks."""
    return [
        ("primary", primary_prompt, primary_timeout, THEME_MODEL),
        ("compact_retry", compact_prompt, retry_timeout, THEME_RETRY_MODEL),
    ]


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

【本人のPersonal Flowの土台】
{user_context() or 'まだ土台はありません。保存記事と本人メモだけを根拠にする。'}

出力順:
1. タイトル案3つ
2. 記事の軸（1文）
3. H2見出し案
4. 本文初稿
5. 公開前チェック
6. ハッシュタグ案

保存記事は参考材料であり、記事の表現や構成をコピーしない。本人の事実・本人の意見・保存記事の事例を混ぜない。"""
    return ask_codex(prompt, timeout=240)


def build_direction_prompt(
    theme: dict[str, object],
    items: list[sqlite3.Row],
    user_notes: str,
    previous: str,
    context: str,
    compact: bool = False,
) -> str:
    """Build a small, grounded prompt for direction planning."""
    source_numbers = {int(number) for number in theme.get("sources", []) if str(number).isdigit()}
    numbered_items = [
        (number, item)
        for number, item in enumerate(items, start=1)
        if not source_numbers or number in source_numbers
    ][:3]
    summary_limit = 450 if compact else 700
    excerpt_limit = 350 if compact else 1000
    note_limit = 400 if compact else 650
    sources = "\n\n".join(
        f"【保存記事 {number}】\n"
        f"タイトル: {str(item['title'])[:300]}\n"
        f"自分のメモ: {str(item['note'] or '').strip()[:note_limit] or 'なし'}\n"
        f"要点: {str(item['summary'] or '').strip()[:summary_limit] or 'なし'}\n"
        f"本文の短い抜粋: {str(item['article'] or '').strip()[:excerpt_limit] or 'なし'}"
        for number, item in numbered_items
    ) or "該当する保存記事はありません。テーマと本人メモだけを根拠にする。"
    notes_limit = 800 if compact else 1200
    previous_limit = 1000 if compact else 1800
    context_limit = 400 if compact else 700
    retry_note = "今回は短くした材料だけで判断してください。\n" if compact else ""
    return f"""あなたはnote記事の方向性を整理する編集パートナーです。下書きはまだ書きません。
{retry_note}以下の材料だけを使ってください。ツール、検索、ファイル操作は不要です。

【選んだテーマ】
タイトル: {str(theme.get('title', ''))[:300]}
何を書くか: {str(theme.get('approach', ''))[:1200 if not compact else 750]}

【本人が今書いていること】
{user_notes[:notes_limit] or 'まだなし。本人の経験や気持ちは作らない。'}

【前回までに固めた内容】
{previous[:previous_limit] or '初回です。'}

【保存記事】
{sources}

【本人の土台】
{context[:context_limit] or 'まだ土台はありません。保存記事と本人メモだけを根拠にする。'}

守ること:
- 本人が話していない出来事、感情、理由、数字を作らない。
- 本人の体験・本人の意見・保存記事の事例を混ぜない。
- 一般論や成功物語へ広げず、本人が書ける範囲を中心にする。
- 足りない本人の体験は補わず、質問として残す。
- 参考記事の表現や構成をコピーしない。

次の順番で、簡潔に返す。
1. 今回の記事の中心（2〜3文）
2. 入れると本人らしくなる要素
3. 今回は外した方がよい要素
4. 次に本人へ聞きたい質問（最大3つ）
5. 今の時点での見出しのたたき台"""


def direction_error_from_codex(error: CodexRunError) -> DirectionSuggestionError:
    messages = {
        "timeout": "短くした材料でも方向性の整理が時間内に終わりませんでした。少ししてから、同じ内容でもう一度試してください。",
        "start_failed": "方向性を整理する機能を起動できませんでした。詳しい原因はこのMacの記録に残しました。Personal Flowを開き直してから、もう一度試してください。",
        "process_failed": "方向性を整理する途中でCodexが止まりました。少ししてから、同じ内容でもう一度試してください。",
        "output_read_failed": "返ってきた方向性を読み込めませんでした。詳しい原因はこのMacの記録に残しました。もう一度試してください。",
        "empty_output": "Codexから方向性が返ってきませんでした。同じ内容でもう一度試してください。",
    }
    return DirectionSuggestionError(error.code, messages.get(error.code, str(error)))


def refine_note_direction(theme: dict[str, object], items: list[sqlite3.Row], user_notes: str, previous: str = "") -> str:
    """Develop a chosen theme in an isolated, bounded process with one compact retry."""
    context = user_context()
    attempts = lightweight_note_attempts(
        build_direction_prompt(theme, items, user_notes, previous, context),
        build_direction_prompt(theme, items, user_notes, previous, context, compact=True),
        DIRECTION_PRIMARY_TIMEOUT,
        DIRECTION_RETRY_TIMEOUT,
    )
    retryable = {"timeout", "process_failed", "output_read_failed", "empty_output"}
    for attempt_index, (attempt_name, prompt, timeout, model) in enumerate(attempts):
        try:
            return ask_codex(
                prompt,
                timeout=timeout,
                model=model,
                reasoning_effort=THEME_REASONING_EFFORT,
                ignore_user_config=True,
            )
        except CodexRunError as error:
            direction_error = direction_error_from_codex(error)
        if attempt_index == 0 and direction_error.code in retryable:
            record_local_error(
                "note_direction",
                "automatic_retry",
                f"first_error={direction_error.code} first_prompt_chars={len(prompt)} retry_prompt_chars={len(attempts[1][1])}",
            )
            continue
        raise direction_error
    raise DirectionSuggestionError("unknown", "方向性を整理できませんでした。同じ内容でもう一度試してください。")


def make_x_posts(material: str, images: list[Path] | None = None) -> dict[str, object]:
    """Create copy-ready X post text locally, using the signed-in Codex account only."""
    prompt = f"""あなたは、本人が自分でXへ貼り付ける投稿文を整える編集パートナーです。
Xへの投稿、ログイン、予約、外部API接続は一切しません。

【本人らしさの基準】
{X_POST_STYLE}

【Personal Flowの土台】
{user_context() or 'まだ土台はありません。今回の材料だけを根拠にする。'}

【今回の材料】
{material}

画像が添付されている場合は、画像内の文章・見出しを今回の材料として読んでよい。ただし、見えない内容を補わない。

次のルールで、X投稿案を返す。
- 内容が一つなら single、背景を省くと本人らしさが消える時だけ thread を選ぶ。
- single は1本、thread は2〜3本。各投稿は280字以内。
- 本文に投稿番号、見出し、絵文字、ハッシュタグ、URLは入れない。
- 最初の投稿だけでも、何の話かと本人の結論が分かるようにする。
- 材料が薄い時は、無理に立派な投稿にせず、分かる範囲だけで短く書く。
- JSONだけを返す。"""
    try:
        result = json.loads(ask_codex(prompt, timeout=180, schema=X_POST_SCHEMA, images=images))
        posts = result.get("posts", [])
        if not isinstance(posts, list) or not posts or any(len(str(post)) > 280 for post in posts):
            raise ValueError
        return result
    except (json.JSONDecodeError, ValueError, AttributeError) as error:
        raise RuntimeError("X投稿文の形を読み取れませんでした。もう一度押してください。") from error


def x_material_from_items(items: list[sqlite3.Row]) -> str:
    if not items:
        raise ValueError("X投稿に使う情報を、少なくとも1件選んでください。")
    return "\n\n".join(
        f"【保存記事 {number}】\nタイトル: {item['title']}\n自分のメモ: {item['note'] or 'なし'}\n本文の抜粋: {item['article'][:4500]}"
        for number, item in enumerate(items[:5], start=1)
    )


def temporary_image_uploads(parts: object) -> list[Path]:
    """Keep accepted uploads only for the current X-post generation request."""
    uploads = parts if isinstance(parts, list) else [parts]
    paths: list[Path] = []
    try:
        for upload in uploads:
            if not getattr(upload, "filename", None) or not getattr(upload, "file", None):
                continue
            data = upload.file.read(10_000_001)
            if len(data) > 10_000_000:
                raise ValueError("画像は1枚10MBまでにしてください。")
            suffix = Path(upload.filename).suffix.lower()
            valid = (
                (suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff"))
                or (suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
                or (suffix == ".webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP")
            )
            if not valid:
                raise ValueError("PNG・JPEG・WebP形式の画像を入れてください。")
            with tempfile.NamedTemporaryFile(prefix="personal-flow-x-", suffix=suffix, delete=False) as output:
                output.write(data)
                paths.append(Path(output.name))
        return paths
    except Exception:
        for path in paths:
            path.unlink(missing_ok=True)
        raise


def x_post_page(result: dict[str, object]) -> bytes:
    posts = [str(post) for post in result.get("posts", [])]
    cards = "".join(
        f"<article class='card'><h2>{'X投稿' if len(posts) == 1 else f'{index}つ目にコピー'}</h2>"
        f"<div class='summary'>{html.escape(post)}</div><button class='copy' type='button' data-copy='{html.escape(post, quote=True)}'>この投稿文をコピー</button></article>"
        for index, post in enumerate(posts, start=1)
    )
    body = f"""
    <header><div><p class='eyebrow'>COPY READY</p><h1>X投稿文ができました</h1></div><p class='sub'>Xへの自動投稿はしていません。コピーして、ご自身でXへ貼り付けてください。</p></header>
    <section class='panel'><div class='note'><strong>{'1投稿' if result.get('format') == 'single' else 'スレッド投稿'}</strong><br>{html.escape(str(result.get('reason', '')))}</div>{cards}<div class='actions'><a class='button outline' href='/x-post'>別の材料で作る</a><a class='button outline' href='/'>Personal Flowへ戻る</a></div></section>"""
    return page("X投稿文", body)


def theme_suggestion_error_page(
    error: ThemeSuggestionError,
    item_ids: list[str],
    user_angle: str,
) -> bytes:
    hidden_ids = "".join(
        f"<input type='hidden' name='item_ids' value='{html.escape(item_id, quote=True)}'>"
        for item_id in item_ids
    )
    retry = (
        "<form method='post' action='/suggest'>"
        f"{hidden_ids}<input type='hidden' name='user_angle' value='{html.escape(user_angle, quote=True)}'>"
        "<button type='submit'>同じ内容でもう一度試す</button></form>"
    )
    body = (
        "<h1>テーマを作れませんでした</h1>"
        f"<p class='message'>{html.escape(str(error))}</p>"
        f"<div class='actions'>{retry}<a class='button outline' href='/choose-sources'>情報を選び直す</a>"
        "<a class='button outline' href='/'>保存した情報へ戻る</a></div>"
    )
    return page("テーマを作れませんでした", body)


def direction_suggestion_error_page(
    error: DirectionSuggestionError,
    run_id: int,
    theme_index: str,
    addition: str,
    direction_run_id: int | None = None,
) -> bytes:
    run_field = (
        f"<input type='hidden' name='direction_run_id' value='{direction_run_id}'>"
        if direction_run_id is not None
        else f"<input type='hidden' name='run_id' value='{run_id}'>"
    )
    retry = (
        "<form method='post' action='/direction'>"
        f"{run_field}<input type='hidden' name='theme' value='{html.escape(theme_index, quote=True)}'>"
        f"<input type='hidden' name='direction' value='{html.escape(addition, quote=True)}'>"
        "<button type='submit'>同じ内容でもう一度試す</button></form>"
    )
    body = (
        "<h1>方向性を固められませんでした</h1>"
        f"<p class='message'>{html.escape(str(error))}</p>"
        f"<div class='actions'>{retry}"
        f"<a class='button outline' href='/theme-run?id={run_id}'>テーマを選ぶ画面へ戻る</a>"
        "<a class='button outline' href='/'>保存した情報へ戻る</a></div>"
    )
    return page("方向性を固められませんでした", body)


def delete_item_confirmation_page(item: sqlite3.Row) -> bytes:
    body = f"""
    <header><div><p class='eyebrow'>CHECK BEFORE DELETE</p><h1>この情報を削除しますか？</h1></div><p class='sub'>まだ削除されていません。対象を確認してから決められます。</p></header>
    <section class='panel'><p class='message'><strong>この操作は元に戻せません。</strong><br>この1件だけが、今回の記事の箱から削除されます。</p>
    <article class='card'><div class='meta'>{html.escape(item['created_at'])}</div><h2>{html.escape(item['title'])}</h2>
    <a href='{html.escape(item['url'], quote=True)}' target='_blank' rel='noreferrer'>原文を開く ↗</a>
    <div class='note'><strong>自分のメモ</strong><br>{html.escape(item['note']) or 'まだメモはありません。'}</div></article>
    <div class='actions'><form method='post' action='/delete-item'><input type='hidden' name='item_id' value='{item['id']}'><button class='danger' type='submit'>確認したので、この1件を削除する</button></form>
    <a class='button outline' href='/'>削除せずに戻る</a></div></section>"""
    return page("削除する情報の確認", body)


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
button,.button{{display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:9px;padding:12px 15px;background:var(--ink);color:#fff;font-weight:750;font:inherit;text-decoration:none;cursor:pointer}} button.primary{{background:var(--green);width:100%;margin-top:18px}} button.danger{{background:#a3342e}} .danger-outline{{background:transparent;color:#8c2f2a;border:1px solid #cda5a1}} .hint{{font-size:12px;color:var(--muted);margin:9px 0 0}}
.section-head{{display:flex;justify-content:space-between;align-items:center;gap:12px}} .count{{font-size:12px;color:var(--muted);background:#edebe5;border-radius:99px;padding:3px 8px}} .meta{{font-size:12px;color:var(--muted)}} .summary{{white-space:pre-wrap;font-size:14px;margin:12px 0}} .note{{background:#f2f5ef;border-radius:8px;padding:10px 12px;font-size:13px}} a{{color:var(--green)}} .empty{{color:var(--muted);padding:24px 0;text-align:center}}
.actions{{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}} .outline{{background:transparent;color:var(--ink);border:1px solid var(--line)}} .theme{{border-left:4px solid var(--green)}} .theme p{{margin:4px 0 0;white-space:pre-line;color:#4f5551;font-size:14px}} .message{{padding:12px 15px;border-radius:10px;background:#fff1d7;color:#684611;margin-bottom:16px}} .pick{{display:flex;align-items:center;gap:9px;font-size:13px;font-weight:750;color:var(--green);cursor:pointer}} .pick input{{width:18px;height:18px;margin:0;accent-color:var(--green)}} .entry-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:0 0 24px}} .entry{{display:block;padding:16px;border:1px solid var(--line);border-radius:14px;background:var(--card);text-decoration:none;color:var(--ink)}} .entry strong{{display:block;margin-bottom:5px}} .entry span{{display:block;color:var(--muted);font-size:13px}} .copy{{background:var(--green)}}
@media(max-width:760px){{main{{padding:28px 14px}}header{{display:block}}header .sub{{margin-top:12px}}h1{{font-size:34px}}.grid,.entry-grid{{grid-template-columns:1fr}}}}
</style></head><body><main>{body}</main><script>
document.querySelectorAll("form[action='/suggest'], form[action='/draft'], form[action='/direction'], form[action='/x-generate']").forEach((form) => {{
  form.addEventListener("submit", () => {{
    const button = form.querySelector("button");
    button.disabled = true;
    button.textContent = form.action.endsWith("/draft") ? "Youがnoteの下書きを作っています…" : form.action.endsWith("/direction") ? "note記事化フローと方向性を整理しています…" : form.action.endsWith("/x-generate") ? "YouがX投稿文を作っています…" : "Youがテーマを考えています…";
  }});
}});
document.querySelectorAll("[data-copy]").forEach((button) => {{
  button.addEventListener("click", async () => {{
    await navigator.clipboard.writeText(button.dataset.copy || "");
    button.textContent = "コピーしました";
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
        if route.path not in {"/", "/themes", "/theme-run", "/article-form", "/finish", "/choose-sources", "/context", "/x-post", "/x-choose-sources"}:
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
        if route.path == "/context":
            context = user_context()
            body = f"""
            <header><div><p class='eyebrow'>YOUR PRIVATE BASE</p><h1>自分の土台を見直す</h1></div><p class='sub'>普段は画面に出ません。記事を選ぶ時にだけ、あなたらしい切り口を考える参考として使います。</p></header>
            <section class='panel'><h2>note記事から土台を作る</h2><p class='hint'>公開noteの記事を読み、あなたの考え方・関心・書き方を圧縮します。原文はこのMacの外へ保存しません。</p>
            <form method='post' action='/build-context'><label>noteプロフィールURL</label><input name='profile_url' value='{DEFAULT_NOTE_PROFILE}' required><button class='primary' type='submit'>note記事から土台を作り直す</button></form></section>
            <section class='panel'><h2>今の土台</h2><form method='post' action='/context-save'><textarea name='context' placeholder='まだ作られていません。上のボタンから作れます。'>{html.escape(context)}</textarea><div class='actions'><button type='submit'>この内容で保存する</button><a class='button outline' href='/'>戻る</a></div></form></section>"""
            self.send_html(page("自分の土台", body))
            return
        if route.path == "/x-post":
            body = """
            <header><div><p class='eyebrow'>MAKE AN X POST</p><h1>X投稿をつくる</h1></div><p class='sub'>文章や画像を入れると、Youが投稿文を整えます。Xへ自動で投稿されることはありません。</p></header>
            <section class='panel'><form method='post' action='/x-generate' enctype='multipart/form-data'>
            <label>投稿の材料（文章）</label><textarea name='text' placeholder='気づいたこと／伝えたいこと／投稿にしたい文章を、そのまま貼ってください。'></textarea>
            <label>画像（任意・複数可）</label><input name='images' type='file' accept='image/png,image/jpeg,image/webp' multiple><p class='hint'>画像に書かれた文章も材料にできます。画像は投稿文作成のためだけに使い、保存しません。</p>
            <button class='primary' type='submit'>この材料からX投稿文をつくる</button></form></section>"""
            self.send_html(page("X投稿をつくる", body))
            return
        if route.path == "/x-choose-sources":
            source_cards = "".join(
                f"<article class='card'><label class='pick'><input type='checkbox' name='item_ids' value='{item['id']}'>この情報をX投稿に使う</label>"
                f"<div class='meta'>{html.escape(item['created_at'])}</div><h3>{html.escape(item['title'])}</h3>"
                f"<div class='summary'>{html.escape(item['summary'])}</div></article>"
                for item in items
            ) or "<div class='empty'>まず『情報をためる』からURLを保存してください。</div>"
            body = f"""
            <header><div><p class='eyebrow'>FROM SAVED INFORMATION</p><h1>ためた情報からX投稿をつくる</h1></div><p class='sub'>今回の投稿に使う記事だけを選びます。選んだ内容以外は使いません。</p></header>
            <form method='post' action='/x-generate'><section class='panel'>{source_cards}
            <label>投稿に足したい自分の言葉（任意）</label><textarea name='text' placeholder='たとえば、自分が引っかかった理由／今回いちばん伝えたいこと。'></textarea>
            <div class='actions'><button type='submit'>選んだ情報からX投稿文をつくる</button><a class='button outline' href='/'>戻る</a></div></section></form>"""
            self.send_html(page("ためた情報からX投稿", body))
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
            <form method='post' action='/suggest'><section class='panel'>{source_cards}<label>今回の記事で入れたい自分の思い（任意）</label><textarea name='user_angle' placeholder='たとえば、自分が実際に困ったこと／この記事で言いたいこと／絶対に残したい感情。箇条書きで大丈夫です。'></textarea><div class='actions'><button type='submit'>選んだ情報からnoteテーマを提案</button><a class='button outline' href='/'>戻る</a></div></section></form>"""
            self.send_html(page("テーマに使う情報を選ぶ", body))
            return
        if route.path == "/theme-run":
            run = self.load_theme_run(query)
            if not run:
                self.send_html(page("見つかりません", "<h1>テーマ案が見つかりません</h1><p><a href='/'>保存した情報へ戻る</a></p>"), 404)
                return
            proposal = json.loads(run["proposal_json"])
            selected_ids = [str(item_id) for item_id in proposal.get("item_ids", [])]
            source_count = len(selected_flow_items(run["flow_id"], selected_ids))
            user_angle = str(proposal.get("user_angle", "")).strip()
            theme_cards = "".join(
                f"<article class='card theme'><h3>{html.escape(str(theme['title']))}</h3>"
                f"<p>{html.escape(str(theme['approach']))}</p>"
                f"<p><strong>この組み合わせを選んだ理由</strong><br>{html.escape(str(theme.get('picked', '')))}</p>"
                f"<p><strong>今回は中心にしないこと</strong><br>{html.escape(str(theme.get('left_out', '')))}</p>"
                f"<p class='meta'>根拠にした保存記事: {html.escape('・'.join(str(n) for n in theme['sources']))}</p>"
                f"<form method='post' action='/direction'><input type='hidden' name='run_id' value='{run['id']}'>"
                f"<input type='hidden' name='theme' value='{index}'><label>このテーマで入れたいこと（任意）</label>"
                f"<textarea name='direction' placeholder='自分の経験／絶対に残したい言葉／今回の記事で言いたいこと。箇条書きで大丈夫です。'></textarea>"
                f"<button type='submit'>note記事化フローと方向性を固める</button></form></article>"
                for index, theme in enumerate(proposal["themes"])
            )
            body = f"""
            <header><div><p class='eyebrow'>NOTE THEME PROPOSAL</p><h1>書けそうなテーマ</h1></div><p class='sub'>選んだ{source_count}件をまとめて読んだ結果です。テーマを1つ選ぶと、note記事化フローへ渡します。</p></header>
            <section class='panel'>{f"<div class='note'><strong>今回入れたい自分の思い</strong><br>{html.escape(user_angle)}</div>" if user_angle else ""}{theme_cards}<div class='actions'><a class='button outline' href='/'>保存した情報へ戻る</a></div></section>"""
            self.send_html(page("noteテーマ", body))
            return
        if route.path == "/article-form":
            run = self.load_theme_run(query)
            theme = self.load_theme(run, query)
            if not run or not theme:
                self.send_html(page("見つかりません", "<h1>選んだテーマが見つかりません</h1><p><a href='/'>保存した情報へ戻る</a></p>"), 404)
                return
            direction = query.get("direction", [""])[0].strip()
            direction_run = self.load_direction_run(query)
            if direction_run:
                direction = "\n\n".join(part for part in [direction, direction_run["result"]] if part)
            direction_note = f"<div class='note'><strong>テーマを選ぶ時に書いたこと</strong><br>{html.escape(direction)}</div>" if direction else ""
            body = f"""
            <header><div><p class='eyebrow'>NOTE ARTICLE FLOW</p><h1>記事にする前のメモ</h1></div><p class='sub'>テーマと保存記事はすでに渡ります。ここには、あなた自身の出来事や本音だけを足してください。</p></header>
            <section class='panel'><h2>{html.escape(str(theme['title']))}</h2><p class='hint'>{html.escape(str(theme['approach']))}</p>{direction_note}
            <form method='post' action='/draft'><input type='hidden' name='run_id' value='{run['id']}'><input type='hidden' name='theme' value='{query.get('theme', ['0'])[0]}'><input type='hidden' name='direction' value='{html.escape(direction, quote=True)}'>
            <label>希望文字数</label><input name='length' value='約2,000字'>
            <label>自分メモ（任意）</label><textarea name='memo' placeholder='実際にあったこと／自分が思ったこと／残したい言葉。箇条書きで大丈夫です。'></textarea>
            <button class='primary' type='submit'>note記事の下書きを作る</button></form></section>"""
            self.send_html(page("記事にする", body))
            return
        cards = "".join(
            f"<article class='card'><div class='meta'>{html.escape(item['created_at'])}</div><h3>{html.escape(item['title'])}</h3>"
            f"<a href='{html.escape(item['url'], quote=True)}' target='_blank' rel='noreferrer'>原文を開く ↗</a>"
            f"<div class='summary'>{html.escape(item['summary'])}</div>"
            f"<div class='note'><strong>自分のメモ</strong><br>{html.escape(item['note']) or 'まだメモはありません。'}</div>"
            f"<div class='actions'><form method='post' action='/x-generate' class='inline-form'><input type='hidden' name='item_ids' value='{item['id']}'><button class='button outline' type='submit'>この情報からX投稿をつくる</button></form>"
            f"<form method='post' action='/delete-item-confirm' class='inline-form'><input type='hidden' name='item_id' value='{item['id']}'><button class='danger-outline' type='submit'>この情報を削除</button></form></div></article>"
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
<section class='entry-grid'><a class='entry' href='#save'><strong>情報をためる</strong><span>URLとメモを残して、記事の材料を集める。</span></a><a class='entry' href='/x-post'><strong>X投稿をつくる</strong><span>文章や画像を直接入れて、投稿文を作る。</span></a><a class='entry' href='/x-choose-sources'><strong>ためた情報からX投稿をつくる</strong><span>保存済みの記事を選んで、投稿文を作る。</span></a></section>
<div class='grid'><section class='panel' id='save'><h2>情報をためる</h2><p class='hint'>URLを貼るだけ。データはこのMacの中に保存される。</p>
<form method='post' action='/save'><label>記事・動画・ページのURL</label><input name='url' type='url' placeholder='https://...' required>
<label>ひとことメモ（任意）</label><textarea name='note' placeholder='なぜ気になったか／何に使えそうか'></textarea><button class='primary' type='submit'>保存して要点を見る</button></form></section>
<section class='panel'><div class='section-head'><h2>今回の記事の情報</h2><span class='count'>{len(items)} 件</span></div><div class='actions'><a class='button' href='/choose-sources'>テーマに使う情報を選ぶ</a><a class='button outline' href='/finish'>今回の記事を終える</a><a class='button outline' href='/context'>自分の土台を見直す</a><a class='button outline' href='/themes'>仮の入口を見る</a></div><p class='hint'>テーマに使う記事を選んでから、Youにテーマ案を頼めます。</p>{theme_cards}{cards}</section></div>"""
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

    def load_direction_run(self, query: dict[str, list[str]]) -> sqlite3.Row | None:
        direction_id = query.get("direction_run_id", [""])[0]
        if not direction_id.isdigit():
            return None
        return database().execute("SELECT * FROM direction_runs WHERE id = ?", (direction_id,)).fetchone()

    def do_POST(self) -> None:
        if self.path in {"/delete-item-confirm", "/delete-item"}:
            length = int(self.headers.get("Content-Length", "0"))
            values = parse_qs(self.rfile.read(length).decode())
            item_id = values.get("item_id", [""])[0]
            flow = active_flow()
            item = flow_item(flow["id"], item_id)
            if not item:
                body = "<h1>削除する情報が見つかりません</h1><p class='message'>すでに削除されたか、記事の箱が切り替わった可能性があります。</p><p><a class='button' href='/'>今の情報へ戻る</a></p>"
                self.send_html(page("情報が見つかりません", body), 404)
                return
            if self.path == "/delete-item-confirm":
                self.send_html(delete_item_confirmation_page(item))
                return
            if not delete_flow_item(flow["id"], item_id):
                body = "<h1>削除できませんでした</h1><p class='message'>対象が見つからなかったため、何も削除していません。</p><p><a class='button' href='/'>今の情報へ戻る</a></p>"
                self.send_html(page("削除できませんでした", body), 409)
                return
            body = f"<h1>1件だけ削除しました</h1><p class='message'>「{html.escape(item['title'])}」を今回の記事の箱から削除しました。</p><p><a class='button' href='/'>今の情報へ戻る</a></p>"
            self.send_html(page("情報を削除しました", body))
            return
        if self.path == "/x-generate":
            content_type = self.headers.get("Content-Type", "")
            images: list[Path] = []
            try:
                if content_type.startswith("multipart/form-data"):
                    form = cgi.FieldStorage(
                        fp=self.rfile,
                        headers=self.headers,
                        environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type},
                    )
                    text = form.getfirst("text", "").strip()
                    file_parts = form["images"] if "images" in form else []
                    images = temporary_image_uploads(file_parts)
                    material = text
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    values = parse_qs(self.rfile.read(length).decode())
                    text = values.get("text", [""])[0].strip()
                    selected_ids = values.get("item_ids", [])
                    selected = selected_flow_items(active_flow()["id"], selected_ids)
                    source_material = x_material_from_items(selected)
                    material = "\n\n".join(part for part in [source_material, f"【本人が足した言葉】\n{text}" if text else ""] if part)
                if not material and not images:
                    raise ValueError("文章か画像を、少なくとも1つ入れてください。")
                result = make_x_posts(material or "画像だけが今回の材料です。", images)
                self.send_html(x_post_page(result))
            except (ValueError, RuntimeError, OSError) as error:
                body = f"<h1>X投稿文を作れませんでした</h1><p class='message'>{html.escape(str(error))}</p><p><a href='/x-post'>X投稿をつくる画面へ戻る</a></p>"
                self.send_html(page("X投稿文を作れませんでした", body), 400)
            finally:
                for image in images:
                    image.unlink(missing_ok=True)
            return
        if self.path == "/context-save":
            length = int(self.headers.get("Content-Length", "0"))
            values = parse_qs(self.rfile.read(length).decode())
            save_user_context(values.get("context", [""])[0])
            self.send_response(303)
            self.send_header("Location", "/context")
            self.end_headers()
            return
        if self.path == "/build-context":
            length = int(self.headers.get("Content-Length", "0"))
            values = parse_qs(self.rfile.read(length).decode())
            profile_url = values.get("profile_url", [""])[0].strip()
            try:
                context = build_user_context(profile_url)
                save_user_context(context)
                body = f"<h1>自分の土台を作りました</h1><p class='message'>普段は裏で使われます。必要な時だけ、ここで直せます。</p><section class='panel'><div class='summary'>{html.escape(context)}</div><p><a class='button' href='/'>Personal Flowへ戻る</a></p></section>"
                self.send_html(page("自分の土台", body))
            except (ValueError, RuntimeError, OSError, urllib.error.URLError) as error:
                body = f"<h1>土台を作れませんでした</h1><p class='message'>{html.escape(str(error))}</p><p><a href='/context'>土台の画面へ戻る</a></p>"
                self.send_html(page("土台を作れませんでした", body), 400)
            return
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
            user_angle = values.get("user_angle", [""])[0].strip()
            if not selected_ids:
                body = "<h1>情報が選ばれていません</h1><p class='message'>テーマに使う情報を、少なくとも1件チェックしてください。</p><p><a href='/choose-sources'>情報を選ぶ画面へ戻る</a></p>"
                self.send_html(page("情報を選んでください", body), 400)
                return
            items = selected_flow_items(flow["id"], selected_ids)
            if not items:
                record_local_error(
                    "theme_suggestion",
                    "no_valid_items",
                    f"flow_id={flow['id']} submitted_ids={selected_ids}",
                )
                body = "<h1>選んだ情報が見つかりませんでした</h1><p class='message'>画面を開いたあとに記事の箱が切り替わった可能性があります。今の情報から、もう一度選び直してください。</p><p><a class='button' href='/choose-sources'>今の情報を選び直す</a></p>"
                self.send_html(page("情報を選び直してください", body), 400)
                return
            try:
                proposal = suggest_note_themes(items, user_angle)
                with database() as connection:
                    cursor = connection.execute(
                        "INSERT INTO theme_runs(proposal_json, flow_id, created_at) VALUES (?, ?, ?)",
                        (
                            json.dumps({"themes": proposal, "item_ids": [item["id"] for item in items], "user_angle": user_angle}, ensure_ascii=False),
                            flow["id"],
                            datetime.now().strftime("%Y-%m-%d %H:%M"),
                        ),
                    )
                self.send_response(303)
                self.send_header("Location", f"/theme-run?id={cursor.lastrowid}")
                self.end_headers()
            except ThemeSuggestionError as error:
                status = 504 if error.code == "timeout" else 502
                self.send_html(theme_suggestion_error_page(error, selected_ids, user_angle), status)
            return
        if self.path == "/direction":
            length = int(self.headers.get("Content-Length", "0"))
            values = parse_qs(self.rfile.read(length).decode())
            direction_run = self.load_direction_run(values)
            run = self.load_theme_run(values) if not direction_run else database().execute(
                "SELECT * FROM theme_runs WHERE id = ?", (direction_run["theme_run_id"],)
            ).fetchone()
            theme_index = values.get("theme", [str(direction_run["theme_index"]) if direction_run else ""])[0]
            theme = self.load_theme(run, {"theme": [theme_index]})
            if not run or not theme or not theme_index.isdigit():
                self.send_html(page("見つかりません", "<h1>選んだテーマが見つかりません</h1>"), 404)
                return
            proposal = json.loads(run["proposal_json"])
            selected_ids = [str(item_id) for item_id in proposal.get("item_ids", [])]
            items = selected_flow_items(run["flow_id"], selected_ids)
            addition = values.get("direction", [""])[0].strip()
            prior_notes = direction_run["direction_notes"] if direction_run else ""
            combined_notes = "\n\n".join(part for part in [prior_notes, addition] if part)
            previous = direction_run["result"] if direction_run else ""
            try:
                result = refine_note_direction(theme, items, combined_notes, previous)
                with database() as connection:
                    cursor = connection.execute(
                        "INSERT INTO direction_runs(theme_run_id, theme_index, direction_notes, result, created_at) VALUES (?, ?, ?, ?, ?)",
                        (run["id"], int(theme_index), combined_notes, result, datetime.now().strftime("%Y-%m-%d %H:%M")),
                    )
                body = f"""
                <header><div><p class='eyebrow'>DIRECTION WITH NOTE FLOW</p><h1>記事の方向性を固める</h1></div><p class='sub'>note記事化フローのルールと、選んだ保存記事を使って整理した結果です。</p></header>
                <section class='panel'><h2>{html.escape(str(theme['title']))}</h2><div class='summary'>{html.escape(result)}</div>
                <form method='post' action='/direction'><input type='hidden' name='direction_run_id' value='{cursor.lastrowid}'><input type='hidden' name='theme' value='{theme_index}'>
                <label>追加で伝えたいこと（任意）</label><textarea name='direction' placeholder='質問への答え／自分の経験／残したい言葉。もう一度整理したい時だけ書いてください。'></textarea>
                <div class='actions'><button type='submit'>追加の思いを入れて、もう一度方向性を固める</button></div></form>
                <form method='get' action='/article-form'><input type='hidden' name='run_id' value='{run['id']}'><input type='hidden' name='theme' value='{theme_index}'><input type='hidden' name='direction_run_id' value='{cursor.lastrowid}'><div class='actions'><button class='outline' type='submit'>この方向でnote記事の下書きを作る</button></div></form></section>"""
                self.send_html(page("記事の方向性", body))
            except DirectionSuggestionError as error:
                status = 504 if error.code == "timeout" else 502
                self.send_html(
                    direction_suggestion_error_page(
                        error,
                        run["id"],
                        theme_index,
                        addition,
                        direction_run["id"] if direction_run else None,
                    ),
                    status,
                )
            except RuntimeError as error:
                record_local_error("note_direction", "unexpected_error", f"{type(error).__name__}: {error}")
                body = f"<h1>方向性を固められませんでした</h1><p class='message'>{html.escape(str(error))}</p><p><a href='/'>保存した情報へ戻る</a></p>"
                self.send_html(page("方向性を固められませんでした", body), 400)
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
            direction = values.get("direction", [""])[0].strip()
            desired_length = values.get("length", [""])[0].strip()
            proposal = json.loads(run["proposal_json"])
            selected_ids = [str(item_id) for item_id in proposal.get("item_ids", [])]
            items = selected_flow_items(run["flow_id"], selected_ids)
            try:
                combined_memo = "\n\n".join(part for part in [direction, memo] if part)
                draft = make_note_draft(theme, items, combined_memo, desired_length)
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
