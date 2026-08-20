from __future__ import annotations

import json
import io
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

import app


def sample_item(
    item_id: int = 1,
    summary: str = "記事の要点",
    article: str = "記事本文",
    note: str = "自分のメモ",
    title: str = "保存した記事",
    url: str = "https://example.com",
) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT ? AS id, ? AS title, ? AS url, ? AS note, ? AS summary, ? AS article, ? AS created_at",
        (
            item_id,
            title,
            url,
            note,
            summary,
            article,
            "2026-08-16 10:00",
        ),
    ).fetchone()
    connection.close()
    return row


def valid_proposal() -> str:
    theme = {
        "title": "テーマ",
        "approach": "何を書くか",
        "picked": "選んだ理由",
        "left_out": "中心にしないこと",
        "sources": [1],
    }
    return json.dumps({"themes": [theme, theme, theme]}, ensure_ascii=False)


def valid_draft() -> str:
    body = "これは本人の材料だけを使った本文です。" * 45
    return f"""1. タイトル案3つ
タイトルA
タイトルB
タイトルC
2. 記事の軸
本人の経験を中心にする。
3. H2見出し案
## 見出し
4. 本文初稿
{body}
5. 公開前チェック
作っていない事実がないか確認する。
6. おすすめハッシュタグ
- #学び｜記事の種類が学びの記録だから
- #AI活用｜中心テーマがAIの使い方だから
- #初心者の疑問｜知らない分野を覚える悩みを扱うから
- #図解｜理解に使った具体的な方法だから
- #Cloudflare｜記事で学んだ関連語だから"""


class FakeUpload:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self.file = io.BytesIO(data)


class HandlerHarness(app.PersonalFlowHandler):
    """Exercise GET/POST routes without opening a network port."""

    def __init__(self, method: str, path: str, values: list[tuple[str, str]] | None = None):
        body = urlencode(values or []).encode() if values is not None else b""
        self.path = path
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/x-www-form-urlencoded"}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = 200
        self.response_headers: dict[str, str] = {}
        if method == "POST":
            self.do_POST()
        else:
            self.do_GET()

    def send_response(self, code: int, *_: object) -> None:
        self.status = code

    def send_header(self, keyword: str, value: str) -> None:
        self.response_headers[keyword] = value

    def end_headers(self) -> None:
        return

    @property
    def body(self) -> str:
        return self.wfile.getvalue().decode()

    @property
    def raw_body(self) -> bytes:
        return self.wfile.getvalue()


class PastedArticleSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(app, "DB_PATH", Path(self.temp.name) / "pasted.db")
        self.db_patch.start()
        app.database().close()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def completed_article() -> str:
        return "完成した記事のタイトル\n\n" + "自分で書き上げた記事の本文です。外部ページを読まず、この文章だけを材料にします。" * 12

    def test_home_clearly_separates_url_and_completed_article_entries(self) -> None:
        page = HandlerHarness("GET", "/").body
        self.assertIn("URLから情報をためる", page)
        self.assertIn("完成した記事から始める", page)
        self.assertIn("action='/save'", page)
        self.assertIn("action='/save-pasted-article'", page)
        self.assertIn("URLや外部ページは読み込みません", page)
        self.assertIn("今日の記事情報", page)
        self.assertLess(page.index("URLから情報をためる"), page.index("完成した記事から始める"))

    def test_pasted_article_saves_without_fetching_and_joins_current_flow(self) -> None:
        article = self.completed_article()
        with patch.object(app, "extract_article") as fetch:
            response = HandlerHarness("POST", "/save-pasted-article", [("article", article)])
        self.assertEqual(response.status, 303)
        self.assertEqual(response.response_headers["Location"], "/?saved=pasted")
        fetch.assert_not_called()

        items = app.flow_items(app.active_flow()["id"])
        self.assertEqual(len(items), 1)
        saved = items[0]
        self.assertEqual(saved["url"], "")
        self.assertEqual(saved["title"], "完成した記事のタイトル")
        self.assertEqual(saved["article"], article)
        self.assertTrue(saved["summary"])
        self.assertEqual(saved["note"], "")

        result_page = HandlerHarness("GET", response.response_headers["Location"]).body
        self.assertIn("完成した記事を保存しました", result_page)
        self.assertIn("完成した記事のタイトル", result_page)
        self.assertIn("URLなし・貼り付けた完成記事", result_page)
        self.assertNotIn("href=''", result_page)
        next_page = HandlerHarness("GET", "/choose-sources").body
        self.assertIn("完成した記事のタイトル", next_page)
        self.assertIn("URLなし・貼り付けた完成記事", next_page)

    def test_empty_and_short_articles_show_friendly_errors_and_save_nothing(self) -> None:
        empty = HandlerHarness("POST", "/save-pasted-article", [("article", "")])
        self.assertEqual(empty.status, 400)
        self.assertIn("完成した記事の本文を貼ってください", empty.body)
        short = HandlerHarness("POST", "/save-pasted-article", [("article", "短い本文です。")])
        self.assertEqual(short.status, 400)
        self.assertIn("記事本文が短すぎます", short.body)
        self.assertEqual(len(app.flow_items(app.active_flow()["id"])), 0)

    def test_existing_url_save_still_fetches_and_saves_normally(self) -> None:
        with patch.object(app, "extract_article", return_value=("URL記事", "URLから取得した本文です。" * 10)) as fetch:
            response = HandlerHarness(
                "POST",
                "/save",
                [("url", "https://example.com/article"), ("note", "一言メモ")],
            )
        self.assertEqual(response.status, 303)
        fetch.assert_called_once_with("https://example.com/article")
        saved = app.flow_items(app.active_flow()["id"])[0]
        self.assertEqual(saved["url"], "https://example.com/article")
        self.assertEqual(saved["note"], "一言メモ")


class SelectedItemsTests(unittest.TestCase):
    def test_invalid_or_missing_ids_never_fall_back_to_all_items(self) -> None:
        items = [sample_item(1), sample_item(2)]
        with patch.object(app, "flow_items", return_value=items):
            self.assertEqual(app.selected_flow_items(1, []), [])
            self.assertEqual(app.selected_flow_items(1, ["not-a-number"]), [])
            self.assertEqual(app.selected_flow_items(1, ["999"]), [])

    def test_only_matching_items_are_returned(self) -> None:
        items = [sample_item(1), sample_item(2)]
        with patch.object(app, "flow_items", return_value=items):
            selected = app.selected_flow_items(1, ["2"])
        self.assertEqual([item["id"] for item in selected], [2])


class DeleteItemTests(unittest.TestCase):
    def seed_items(self, count: int = 4) -> tuple[sqlite3.Row, list[int]]:
        active = app.active_flow()
        ids = []
        with app.database() as connection:
            for number in range(1, count + 1):
                ids.append(
                    connection.execute(
                        "INSERT INTO items(url, title, article, summary, note, created_at, flow_id) VALUES (?, ?, '', ?, '', ?, ?)",
                        (
                            f"https://example.com/{number}",
                            f"記事{number}",
                            f"要点{number}",
                            f"2026-08-16 10:0{number}",
                            active["id"],
                        ),
                    ).lastrowid
                )
        return active, ids

    def test_organize_page_has_one_entry_multiple_checks_and_zero_is_disabled(self) -> None:
        result = app.organize_items_page([sample_item(1), sample_item(2)]).decode()
        self.assertIn("不要な情報を整理する", result)
        self.assertEqual(result.count("<input type='checkbox' name='item_ids'"), 2)
        self.assertIn("選んだ0件を削除", result)
        self.assertIn("disabled", result)
        self.assertNotIn("/delete-item-confirm", result)

    def test_confirmation_page_lists_one_and_multiple_targets_without_deleting(self) -> None:
        one = app.delete_items_confirmation_page(
            3,
            [sample_item(7, title="記事A", url="https://example.com/a")],
        ).decode()
        self.assertIn("選んだ1件を削除しますか", one)
        self.assertIn("記事A", one)
        self.assertIn("https://example.com/a", one)
        self.assertIn("2026-08-16 10:00", one)
        items = [sample_item(index, title=f"記事{index}", url=f"https://example.com/{index}") for index in (7, 8, 9)]
        multiple = app.delete_items_confirmation_page(3, items).decode()
        self.assertIn("選んだ3件を削除しますか", multiple)
        self.assertIn("今回の記事の箱", multiple)
        self.assertIn("元に戻せません", multiple)
        self.assertEqual(multiple.count("<input type='hidden' name='item_ids'"), 3)

    def test_confirmation_does_not_change_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "DB_PATH", Path(directory) / "test.db"):
                active, ids = self.seed_items(3)
                items = app.exact_flow_items(active["id"], [str(item_id) for item_id in ids])
                before = len(app.flow_items(active["id"]))
                app.delete_items_confirmation_page(active["id"], items)
                self.assertEqual(len(app.flow_items(active["id"])), before)

    def test_zero_invalid_and_duplicate_selection_delete_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "DB_PATH", Path(directory) / "test.db"):
                active, ids = self.seed_items(3)
                with app.database() as connection:
                    saved_flow = connection.execute(
                        "INSERT INTO flows(name, status, created_at) VALUES ('保存済み', 'saved', '2026-08-16 10:00')"
                    ).lastrowid
                    foreign_id = connection.execute(
                        "INSERT INTO items(url, title, article, summary, note, created_at, flow_id) VALUES ('https://example.com/foreign', '別の箱', '', '', '', '2026-08-16 10:00', ?)",
                        (saved_flow,),
                    ).lastrowid
                self.assertEqual(app.delete_flow_items(active["id"], []), [])
                self.assertEqual(app.delete_flow_items(active["id"], [str(ids[0]), "bad"]), [])
                self.assertEqual(app.delete_flow_items(active["id"], [str(ids[0]), str(ids[0])]), [])
                self.assertEqual(app.delete_flow_items(active["id"], [str(ids[0]), str(foreign_id)]), [])
                self.assertEqual(len(app.flow_items(active["id"])), 3)

    def test_only_the_three_confirmed_items_are_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "DB_PATH", Path(directory) / "test.db"):
                active, ids = self.seed_items(4)
                with app.database() as connection:
                    saved_flow = connection.execute(
                        "INSERT INTO flows(name, status, created_at) VALUES ('保存済み', 'saved', '2026-08-16 10:00')"
                    ).lastrowid
                    saved_item = connection.execute(
                        "INSERT INTO items(url, title, article, summary, note, created_at, flow_id) VALUES (?, ?, '', '', '', ?, ?)",
                        ("https://example.com/saved", "保存済み情報", "2026-08-16 10:00", saved_flow),
                    ).lastrowid
                deleted = app.delete_flow_items(active["id"], [str(item_id) for item_id in ids[:3]])
                self.assertEqual(len(deleted), 3)
                with app.database() as connection:
                    remaining = connection.execute("SELECT id FROM items ORDER BY id").fetchall()
                self.assertEqual([row["id"] for row in remaining], [ids[3], saved_item])

    def test_changed_target_aborts_without_partial_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "DB_PATH", Path(directory) / "test.db"):
                active, ids = self.seed_items(3)
                confirmed = [str(ids[0]), str(ids[1])]
                self.assertEqual(len(app.exact_flow_items(active["id"], confirmed)), 2)
                with app.database() as connection:
                    connection.execute("DELETE FROM items WHERE id = ?", (ids[0],))
                self.assertEqual(app.delete_flow_items(active["id"], confirmed), [])
                remaining = [row["id"] for row in app.flow_items(active["id"])]
                self.assertIn(ids[1], remaining)

    def test_confirmation_count_matches_deleted_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "DB_PATH", Path(directory) / "test.db"):
                active, ids = self.seed_items(3)
                items = app.exact_flow_items(active["id"], [str(item_id) for item_id in ids])
                page = app.delete_items_confirmation_page(active["id"], items).decode()
                deleted = app.delete_flow_items(active["id"], [str(item_id) for item_id in ids])
                self.assertIn(f"選んだ{len(deleted)}件", page)


class CodexFailureTests(unittest.TestCase):
    def test_timeout_is_classified(self) -> None:
        with (
            patch.object(app.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 1)),
            patch.object(app, "record_local_error"),
        ):
            with self.assertRaisesRegex(app.CodexRunError, "時間") as caught:
                app.ask_codex("test", timeout=1)
        self.assertEqual(caught.exception.code, "timeout")

    def test_start_failure_is_classified(self) -> None:
        with (
            patch.object(app.subprocess, "run", side_effect=FileNotFoundError("missing")),
            patch.object(app, "record_local_error"),
        ):
            with self.assertRaises(app.CodexRunError) as caught:
                app.ask_codex("test")
        self.assertEqual(caught.exception.code, "start_failed")

    def test_empty_output_is_classified(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch.object(app.subprocess, "run", return_value=completed),
            patch.object(app, "record_local_error"),
        ):
            with self.assertRaises(app.CodexRunError) as caught:
                app.ask_codex("test")
        self.assertEqual(caught.exception.code, "empty_output")

    def test_nonzero_exit_is_classified(self) -> None:
        completed = subprocess.CompletedProcess([], 1, "", "failure detail")
        with (
            patch.object(app.subprocess, "run", return_value=completed),
            patch.object(app, "record_local_error"),
        ):
            with self.assertRaises(app.CodexRunError) as caught:
                app.ask_codex("test")
        self.assertEqual(caught.exception.code, "process_failed")


class XPostGenerationTests(unittest.TestCase):
    def test_x_posts_retry_with_bounded_prompt_after_timeout(self) -> None:
        valid = json.dumps({"format": "thread", "reason": "背景が必要", "posts": ["最初の投稿", "続きの投稿"]})
        with (
            patch.object(app, "ask_codex", side_effect=[app.CodexRunError("timeout", "time"), valid]) as mocked,
            patch.object(app, "user_context", return_value="土台" * 2000),
            patch.object(app, "record_local_error"),
        ):
            result = app.make_x_posts("材料" * 7000)
        self.assertEqual(result["format"], "thread")
        self.assertEqual(mocked.call_count, 2)
        self.assertLess(len(mocked.call_args_list[1].args[0]), len(mocked.call_args_list[0].args[0]))
        self.assertEqual(mocked.call_args_list[0].kwargs["timeout"], app.X_POST_PRIMARY_TIMEOUT)
        self.assertEqual(mocked.call_args_list[1].kwargs["timeout"], app.X_POST_RETRY_TIMEOUT)
        self.assertEqual(mocked.call_args_list[0].kwargs["reasoning_effort"], "low")

    def test_x_posts_reject_more_than_three_posts(self) -> None:
        invalid = json.dumps({"format": "thread", "reason": "多すぎる", "posts": ["1", "2", "3", "4"]})
        with (
            patch.object(app, "ask_codex", side_effect=[invalid, invalid]),
            patch.object(app, "user_context", return_value=""),
            patch.object(app, "record_local_error"),
        ):
            with self.assertRaisesRegex(RuntimeError, "形を読み取れません"):
                app.make_x_posts("材料")

    def test_long_material_retries_single_as_a_thread(self) -> None:
        single = json.dumps({"format": "single", "reason": "短くまとめた", "posts": ["1本に圧縮"]})
        thread = json.dumps({"format": "thread", "reason": "試行と変化を残す", "posts": ["試したこと", "考えが変わったこと"]})
        with (
            patch.object(app, "ask_codex", side_effect=[single, thread]) as mocked,
            patch.object(app, "user_context", return_value=""),
            patch.object(app, "record_local_error"),
        ):
            result = app.make_x_posts("長い記事の材料。" * 100)
        self.assertEqual(result["format"], "thread")
        self.assertEqual(len(result["posts"]), 2)
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("必ずthread", mocked.call_args_list[0].args[0])
        self.assertIn("必ずthread", mocked.call_args_list[1].args[0])
        self.assertIn("記事やnoteは背景資料", mocked.call_args_list[0].args[0])
        self.assertIn("具体的な場面、つまずき、試したこと", mocked.call_args_list[0].args[0])

    def test_short_self_contained_material_can_stay_single(self) -> None:
        single = json.dumps({"format": "single", "reason": "自己完結", "posts": ["短い結論"]})
        with (
            patch.object(app, "ask_codex", return_value=single),
            patch.object(app, "user_context", return_value=""),
        ):
            result = app.make_x_posts("短い自己完結した材料です。")
        self.assertEqual(result["format"], "single")

    def test_x_prompt_contains_approved_writing_foundation(self) -> None:
        valid = json.dumps({"format": "single", "reason": "短い事実", "posts": ["具体的な出来事から書く"]})
        with patch.object(app, "ask_codex", return_value=valid) as mocked, patch.object(app, "user_context", return_value=""):
            app.make_x_posts("今日、実際に試した操作で一つだけ結果が変わった。")
        prompt = mocked.call_args.args[0]
        for phrase in ("具体的な出来事、数字、会話、試した操作", "実際の事実・行動 → 迷い・不確かさ", "記事やnoteは背景資料", "元記事の言い回し"):
            self.assertIn(phrase, prompt)

    def test_source_centered_and_generic_closing_phrases_trigger_retry(self) -> None:
        banned = json.dumps({"format": "thread", "reason": "まとめ", "posts": ["保存した記事に、という話があった。", "これが参考になれば幸いです。"]})
        valid = json.dumps({"format": "thread", "reason": "次に試す", "posts": ["実際に試したこと", "次にもう一度試すこと"]})
        with (
            patch.object(app, "ask_codex", side_effect=[banned, valid]) as mocked,
            patch.object(app, "user_context", return_value=""),
            patch.object(app, "record_local_error"),
        ):
            result = app.make_x_posts("記事材料をそのまま要約せず、手元の作業で試したことを考える。" * 20)
        self.assertEqual(result["posts"], ["実際に試したこと", "次にもう一度試すこと"])
        self.assertEqual(mocked.call_count, 2)


class TodayNoteTests(unittest.TestCase):
    def test_today_note_accepts_target_length_and_reaction(self) -> None:
        text = "今日、初めて触る画面で設定を一つ変えた。説明を読んでもすぐには意味が分からず、何度か戻ってやり直した。小さな操作でも、前に進んだ感覚が残った。うまくできたというより、分からないまま止まらずに試せたことが大きい。明日は同じ手順をもう一度やって、どこで迷ったのかを短くメモしておきたい。まだ手順を人に説明できるほどではないが、昨日より一つだけ分かる場所が増えた。明日も同じ画面を開くつもりだ。"
        text += "その感覚を忘れないようにしたい。"
        text += "補足。" * 38
        with patch.object(app, "ask_codex", return_value=json.dumps({"text": text}, ensure_ascii=False)):
            result = app.make_today_note("今日見つけた画面で、設定を一つ変えて何度かやり直した。前より少しだけ仕組みが見えた", "何度かやり直したことが自分に残った。明日もう一度試したい")
        self.assertTrue(300 <= len(result) <= 400)
        self.assertIn("残った", result)

    def test_today_note_rejects_source_summary_without_reaction(self) -> None:
        text = "保存した記事に、仕事は終わらず通過点だという話があった。記事では目標と現在地の差を説明し、通常の努力と特別な努力を分けて考えていた。さらに、目標が高い場合は働き方を変える必要があると説明していた。これは記事の内容をまとめただけで、自分が何を思ったかは書いていない。これが参考になれば幸いです。明日は別のことを考える。内容を何度も説明しているが、本人の反応はまだ書かれていない。"
        text += "補足。" * 38
        with patch.object(app, "ask_codex", return_value=json.dumps({"text": text}, ensure_ascii=False)):
            with self.assertRaisesRegex(RuntimeError, "出典中心"):
                app.make_today_note("記事のURLと、記事に書かれていた目標と現在地の説明を見た。仕事の進め方についての長い文章だった。気になった。")

    def test_today_note_retries_short_model_output(self) -> None:
        short = json.dumps({"text": "短い反応だけです。"}, ensure_ascii=False)
        valid = ("今日見つけた具体的な一文が残った。" * 18 + "自分は少し考え直し、明日もう一度本文を読んでみたいと思った。")[:300]
        with patch.object(app, "ask_codex", side_effect=[short, json.dumps({"text": valid}, ensure_ascii=False)]) as mocked:
            result = app.make_today_note("今日見つけた文章と、自分が気になった具体的な箇所。画面に残った言葉をもう一度読み返した", "自分の反応を少し具体的に書いた。明日も考えたいし、なぜ残ったのかをもう少し考えたい")
        self.assertTrue(300 <= len(result) <= 400)
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("短すぎました", mocked.call_args_list[1].args[0])

    def test_today_note_prompt_requires_encounter_and_reaction_without_history(self) -> None:
        text = ("今日、実際に見つけた一文が妙に残った。" * 20 + "自分は少し引っかかり、明日もう一度考えてみたい。")[:300]
        with patch.object(app, "ask_codex", return_value=json.dumps({"text": text}, ensure_ascii=False)) as mocked:
            app.make_today_note("今日見つけたURLと、そのページに書かれていた具体的な一文を読んだ。短い文章だが気になった", "自分の反応を少し具体的に書いた。あとで考え直したい")
        prompt = mocked.call_args.args[0]
        for expected in ("Substack Notes", "300〜400文字", "出会ったもの", "本人が考えたこと・感じたこと", "過去の保存記事"):
            self.assertIn(expected, prompt)

    def test_today_note_rejects_url_with_thin_reaction(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "20文字以上"):
            app.make_today_note("https://x.com/minowanowa/status/2089590383873560837", "気になる本とタイトル")

    def test_today_note_prompt_rejects_invented_personal_profile(self) -> None:
        text = ("今日見つけた一文が残った。自分は少し考え直した。" * 25)[:300]
        with patch.object(app, "ask_codex", return_value=json.dumps({"text": text}, ensure_ascii=False)) as mocked:
            app.make_today_note("今日のタイトルと本文を読んで、具体的な一文を見つけた。内容の一部が気になった", "自分の反応を具体的に書いた。少し考え直した")
        prompt = mocked.call_args.args[0]
        for expected in ("タイトルや人の説明は一文まで", "人物評、経歴、成功物語", "根拠のない決意や未来予測"):
            self.assertIn(expected, prompt)

    def test_today_note_pages_are_copy_ready(self) -> None:
        self.assertIn("今日のこれ", app.today_note_page().decode())
        self.assertIn("自動公開はしていません", app.today_note_result_page("自分の反応" * 60).decode())
        self.assertIn("today-note-generate", app.today_note_page().decode())
        self.assertIn("今日のこれを作っています", app.today_note_page().decode())


class ThemeSuggestionTests(unittest.TestCase):
    def run_suggestion(self, output: str) -> list[dict[str, object]]:
        with (
            patch.object(app, "ask_codex", return_value=output),
            patch.object(app, "user_context", return_value=""),
            patch.object(app, "record_local_error"),
        ):
            return app.suggest_note_themes([sample_item()])

    def test_valid_theme_proposal_is_accepted(self) -> None:
        result = self.run_suggestion(valid_proposal())
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["source_item_ids"], [1])

    def test_theme_generation_uses_an_isolated_lightweight_profile(self) -> None:
        with (
            patch.object(app, "ask_codex", return_value=valid_proposal()) as mocked,
            patch.object(app, "user_context", return_value=""),
        ):
            app.suggest_note_themes([sample_item(), sample_item(2)])
        _, kwargs = mocked.call_args
        self.assertTrue(kwargs["ignore_user_config"])
        self.assertEqual(kwargs["model"], app.THEME_MODEL)
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertEqual(kwargs["timeout"], app.THEME_PRIMARY_TIMEOUT)

    def test_timeout_retries_once_with_a_smaller_prompt(self) -> None:
        with (
            patch.object(
                app,
                "ask_codex",
                side_effect=[app.CodexRunError("timeout", "time"), valid_proposal()],
            ) as mocked,
            patch.object(app, "user_context", return_value="土台" * 1000),
            patch.object(app, "record_local_error"),
        ):
            result = app.suggest_note_themes([sample_item(), sample_item(2)])
        self.assertEqual(len(result), 3)
        self.assertEqual(mocked.call_count, 2)
        primary_prompt = mocked.call_args_list[0].args[0]
        retry_prompt = mocked.call_args_list[1].args[0]
        self.assertLess(len(retry_prompt), len(primary_prompt))
        self.assertEqual(mocked.call_args_list[1].kwargs["timeout"], app.THEME_RETRY_TIMEOUT)
        self.assertEqual(mocked.call_args_list[1].kwargs["model"], app.THEME_RETRY_MODEL)

    def test_start_failure_is_not_retried(self) -> None:
        with (
            patch.object(app, "ask_codex", side_effect=app.CodexRunError("start_failed", "missing")) as mocked,
            patch.object(app, "user_context", return_value=""),
        ):
            with self.assertRaises(app.ThemeSuggestionError) as caught:
                app.suggest_note_themes([sample_item()])
        self.assertEqual(caught.exception.code, "start_failed")
        self.assertEqual(mocked.call_count, 1)

    def test_two_article_prompt_is_bounded(self) -> None:
        long_item = sample_item(summary="要点" * 2000, article="本文" * 5000, note="メモ" * 3000)
        prompt = app.build_theme_prompt([long_item, long_item], "思い" * 2000, "土台" * 2000)
        compact = app.build_theme_prompt([long_item, long_item], "思い" * 2000, "土台" * 2000, compact=True)
        self.assertLess(len(prompt), 10_000)
        self.assertLess(len(compact), len(prompt))

    def test_invalid_json_has_its_own_error(self) -> None:
        with self.assertRaises(app.ThemeSuggestionError) as caught:
            self.run_suggestion("not-json")
        self.assertEqual(caught.exception.code, "invalid_json")

    def test_empty_theme_list_has_its_own_error(self) -> None:
        with self.assertRaises(app.ThemeSuggestionError) as caught:
            self.run_suggestion('{"themes": []}')
        self.assertEqual(caught.exception.code, "empty_themes")

    def test_wrong_theme_count_has_its_own_error(self) -> None:
        proposal = json.loads(valid_proposal())
        proposal["themes"] = proposal["themes"][:1]
        with self.assertRaises(app.ThemeSuggestionError) as caught:
            self.run_suggestion(json.dumps(proposal, ensure_ascii=False))
        self.assertEqual(caught.exception.code, "invalid_theme_count")

    def test_out_of_range_source_number_is_rejected(self) -> None:
        proposal = json.loads(valid_proposal())
        proposal["themes"][0]["sources"] = [2]
        with self.assertRaises(app.ThemeSuggestionError) as caught:
            self.run_suggestion(json.dumps(proposal, ensure_ascii=False))
        self.assertEqual(caught.exception.code, "invalid_theme")

    def test_retry_page_keeps_the_same_selection_safely(self) -> None:
        error = app.ThemeSuggestionError("timeout", "時間切れです")
        result = app.theme_suggestion_error_page(error, ["1"], "本人の思い'&").decode()
        self.assertIn("同じ内容でもう一度試す", result)
        self.assertIn("name='item_ids' value='1'", result)
        self.assertIn("本人の思い&#x27;&amp;", result)


class DirectionSuggestionTests(unittest.TestCase):
    def test_direction_uses_the_same_isolated_lightweight_profile(self) -> None:
        with (
            patch.object(app, "ask_codex", return_value="方向性") as mocked,
            patch.object(app, "user_context", return_value=""),
        ):
            result = app.refine_note_direction(
                {"title": "テーマ", "approach": "切り口", "sources": [1]},
                [sample_item(), sample_item(2)],
                "本人メモ",
            )
        self.assertEqual(result, "方向性")
        _, kwargs = mocked.call_args
        self.assertTrue(kwargs["ignore_user_config"])
        self.assertEqual(kwargs["model"], app.THEME_MODEL)
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertEqual(kwargs["timeout"], app.DIRECTION_PRIMARY_TIMEOUT)

    def test_direction_timeout_retries_once_with_a_smaller_prompt(self) -> None:
        with (
            patch.object(
                app,
                "ask_codex",
                side_effect=[app.CodexRunError("timeout", "time"), "方向性"],
            ) as mocked,
            patch.object(app, "user_context", return_value="土台" * 1000),
            patch.object(app, "record_local_error"),
        ):
            result = app.refine_note_direction(
                {"title": "テーマ", "approach": "切り口" * 1000, "sources": [1, 2]},
                [
                    sample_item(article="本文" * 5000, note="メモ" * 2000),
                    sample_item(2, article="本文" * 5000, note="メモ" * 2000),
                ],
                "本人メモ" * 1000,
                "前回" * 1000,
            )
        self.assertEqual(result, "方向性")
        self.assertEqual(mocked.call_count, 2)
        primary_prompt = mocked.call_args_list[0].args[0]
        retry_prompt = mocked.call_args_list[1].args[0]
        self.assertLess(len(retry_prompt), len(primary_prompt))
        self.assertEqual(mocked.call_args_list[1].kwargs["model"], app.THEME_RETRY_MODEL)
        self.assertEqual(mocked.call_args_list[1].kwargs["timeout"], app.DIRECTION_RETRY_TIMEOUT)

    def test_direction_start_failure_is_not_retried(self) -> None:
        with (
            patch.object(app, "ask_codex", side_effect=app.CodexRunError("start_failed", "missing")) as mocked,
            patch.object(app, "user_context", return_value=""),
        ):
            with self.assertRaises(app.DirectionSuggestionError) as caught:
                app.refine_note_direction(
                    {"title": "テーマ", "approach": "切り口", "sources": [1]},
                    [sample_item()],
                    "",
                )
        self.assertEqual(caught.exception.code, "start_failed")
        self.assertEqual(mocked.call_count, 1)

    def test_direction_prompt_is_bounded_and_uses_only_theme_sources(self) -> None:
        items = [
            sample_item(1, summary="要点" * 2000, article="本文" * 5000, note="メモ" * 3000),
            sample_item(2, summary="使わない要点", article="使わない本文", note="使わないメモ"),
        ]
        theme = {"title": "題", "approach": "方向" * 2000, "sources": [1]}
        prompt = app.build_direction_prompt(theme, items, "思い" * 2000, "前回" * 2000, "土台" * 2000)
        compact = app.build_direction_prompt(theme, items, "思い" * 2000, "前回" * 2000, "土台" * 2000, compact=True)
        self.assertLess(len(prompt), 8_000)
        self.assertLess(len(compact), len(prompt))
        self.assertNotIn("使わない本文", prompt)

    def test_direction_error_page_keeps_the_same_input(self) -> None:
        error = app.DirectionSuggestionError("timeout", "時間切れです")
        result = app.direction_suggestion_error_page(error, 19, "0", "本人の言葉'&").decode()
        self.assertIn("同じ内容でもう一度試す", result)
        self.assertIn("name='run_id' value='19'", result)
        self.assertIn("本人の言葉&#x27;&amp;", result)


class DraftGenerationTests(unittest.TestCase):
    def draft_theme(self) -> dict[str, object]:
        return {
            "title": "選んだテーマ",
            "approach": "本人の経験から書く",
            "picked": "二つの記事を組み合わせた理由",
            "left_out": "成功談は外す",
            "sources": [1],
            "source_item_ids": [1],
        }

    def test_balanced_material_and_all_user_inputs_remain_in_both_attempts(self) -> None:
        article = "冒頭の事実" + ("あ" * 3000) + "中盤の具体例" + ("い" * 3000) + "末尾の結論"
        summary = "要点冒頭" + ("う" * 1200) + "要点中盤" + ("え" * 1200) + "要点末尾"
        items = [
            sample_item(1, summary=summary, article=article, note="本人の保存メモ"),
            sample_item(2, article="選んでいない記事の本文", title="選んでいない記事"),
        ]
        for compact in (False, True):
            prompt = app.build_draft_prompt(
                self.draft_theme(),
                items,
                "方向性の中心と外す要素",
                "今回の本人メモ",
                "約2,000字",
                "本人の土台",
                compact=compact,
            )
            for expected in (
                "冒頭の事実",
                "中盤の具体例",
                "末尾の結論",
                "本人の保存メモ",
                "選んだテーマ",
                "方向性の中心と外す要素",
                "今回の本人メモ",
                "約2,000字",
                "本人の土台",
                "成功談は外す",
                "本人が話していない出来事",
            ):
                self.assertIn(expected, prompt)
            self.assertNotIn("選んでいない記事の本文", prompt)

    def test_draft_uses_isolated_quality_model_and_longer_timeout(self) -> None:
        with (
            patch.object(app, "ask_codex", return_value=valid_draft()) as mocked,
            patch.object(app, "user_context", return_value="本人の土台"),
        ):
            result = app.make_note_draft(self.draft_theme(), [sample_item()], "方向性", "本人メモ", "約2,000字")
        self.assertIn("本文初稿", result)
        _, kwargs = mocked.call_args
        self.assertTrue(kwargs["ignore_user_config"])
        self.assertEqual(kwargs["model"], app.DRAFT_MODEL)
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertGreater(app.DRAFT_PRIMARY_TIMEOUT, 150)

    def test_draft_prompt_contains_approved_prose_foundation_and_bans_source_opening(self) -> None:
        prompt = app.build_draft_prompt(
            self.draft_theme(),
            [sample_item(article="今回、実際に試した操作とつまずき")],
            "方向性",
            "本人メモ",
            "約2,000字",
            "本人の土台",
        )
        for expected in (
            "具体的な出来事、試したこと、つまずき、数字、観察、または問い",
            "実際の事実・行動 → 迷い・不確かさ",
            "本人の出来事、考えの動き、見方が変わった点",
            "「記事を読んだ」「保存した記事に」「記事に書いてあった」などで始めない",
            "一般的な締めを避け",
            "外部の書き手の言い回し・見出し",
        ):
            self.assertIn(expected, prompt)

    def test_draft_prompt_does_not_use_generic_identity_as_opening_instruction(self) -> None:
        prompt = app.build_draft_prompt(
            self.draft_theme(), [sample_item()], "方向性", "本人メモ", "約2,000字", "本人の土台"
        )
        self.assertNotIn("冒頭はパソコンとAIを触り始めた", prompt)

    def test_timeout_retries_once_with_preserved_material(self) -> None:
        with (
            patch.object(
                app,
                "ask_codex",
                side_effect=[app.CodexRunError("timeout", "time"), valid_draft()],
            ) as mocked,
            patch.object(app, "user_context", return_value="本人の土台"),
            patch.object(app, "record_local_error"),
        ):
            app.make_note_draft(self.draft_theme(), [sample_item(article="本文" * 3000)], "方向性", "本人メモ", "約2,000字")
        self.assertEqual(mocked.call_count, 2)
        primary = mocked.call_args_list[0].args[0]
        retry = mocked.call_args_list[1].args[0]
        self.assertLess(len(retry), len(primary))
        for value in ("選んだテーマ", "方向性", "本人メモ", "約2,000字", "本人の土台", "成功談は外す"):
            self.assertIn(value, retry)
        self.assertEqual(mocked.call_args_list[1].kwargs["model"], app.DRAFT_RETRY_MODEL)
        self.assertEqual(mocked.call_args_list[1].kwargs["timeout"], app.DRAFT_RETRY_TIMEOUT)

    def test_broken_or_too_short_output_retries_once(self) -> None:
        with (
            patch.object(app, "ask_codex", side_effect=["短い返答", valid_draft()]) as mocked,
            patch.object(app, "user_context", return_value=""),
            patch.object(app, "record_local_error"),
        ):
            result = app.make_note_draft(self.draft_theme(), [sample_item()], "", "", "約2,000字")
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("公開前チェック", result)

    def test_draft_error_page_keeps_theme_direction_memo_and_length(self) -> None:
        error = app.DraftGenerationError("timeout", "時間切れです")
        result = app.draft_generation_error_page(
            error, 20, "0", "方向性'&", "本人メモ", "約2,000字"
        ).decode()
        self.assertIn("同じ内容でもう一度試す", result)
        self.assertIn("name='run_id' value='20'", result)
        self.assertIn("方向性&#x27;&amp;", result)
        self.assertIn("本人メモ", result)
        self.assertIn("約2,000字", result)

    def test_missing_material_is_told_not_to_be_invented(self) -> None:
        prompt = app.build_draft_prompt(self.draft_theme(), [sample_item(article="")], "", "", "約2,000字", "")
        self.assertIn("本人が話していない出来事", prompt)
        self.assertIn("足りない本人の体験は作らない", prompt)

    def test_hashtags_are_grounded_limited_and_parseable(self) -> None:
        prompt = app.build_draft_prompt(
            self.draft_theme(), [sample_item()], "方向性", "本人メモ", "約2,000字", "本人の土台"
        )
        for rule in ("記事の種類", "中心テーマ", "具体的な悩みや問い", "関連語", "一般的な人気タグで水増ししない"):
            self.assertIn(rule, prompt)
        suggestions = app.parse_hashtag_suggestions(valid_draft())
        self.assertEqual(len(suggestions), 5)
        self.assertEqual(suggestions[0], ("#学び", "記事の種類が学びの記録だから"))

    def test_duplicate_or_excessive_hashtags_trigger_one_retry(self) -> None:
        broken = valid_draft().replace("#AI活用", "#学び")
        with (
            patch.object(app, "ask_codex", side_effect=[broken, valid_draft()]) as mocked,
            patch.object(app, "user_context", return_value=""),
            patch.object(app, "record_local_error"),
        ):
            result = app.make_note_draft(self.draft_theme(), [sample_item()], "", "", "約2,000字")
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(len(app.parse_hashtag_suggestions(result)), 5)

    def test_three_hashtags_are_allowed_when_theme_is_unclear(self) -> None:
        shorter = "\n".join(valid_draft().splitlines()[:-2])
        self.assertEqual(len(app.parse_hashtag_suggestions(app.validate_note_draft(shorter, "約2,000字"))), 3)

    def test_draft_page_shows_reasons_and_copies_tags_only(self) -> None:
        markdown_result = valid_draft().replace("6. おすすめハッシュタグ", "## 6. おすすめハッシュタグ")
        draft_only, parsed = app.split_draft_hashtags(markdown_result)
        self.assertNotIn("おすすめハッシュタグ", draft_only)
        self.assertEqual(len(parsed), 5)
        result = app.draft_result_page(markdown_result).decode()
        self.assertIn("おすすめハッシュタグ", result)
        self.assertIn("記事の種類が学びの記録だから", result)
        self.assertIn("タグだけをまとめてコピー", result)
        self.assertIn("data-copy='#学び #AI活用 #初心者の疑問 #図解 #Cloudflare'", result)
        self.assertNotIn("data-copy='#学び｜", result)
        self.assertIn("タイトル、見出し画像、本文、外部共有", result)
        self.assertIn("完成記事を貼ってサムネ準備へ", result)


class ThumbnailPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(app, "DB_PATH", Path(self.temp.name) / "thumbnail.db")
        self.asset_patch = patch.object(app, "THUMBNAIL_ASSET_DIR", Path(self.temp.name) / "references")
        self.output_patch = patch.object(app, "THUMBNAIL_OUTPUT_DIR", Path(self.temp.name) / "generated")
        self.db_patch.start()
        self.asset_patch.start()
        self.output_patch.start()
        app.database().close()

    def tearDown(self) -> None:
        self.output_patch.stop()
        self.asset_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def test_unregistered_page_explains_one_time_setup_and_disables_generation(self) -> None:
        result = app.thumbnail_prepare_page().decode()
        self.assertIn("決まったタイトル", result)
        self.assertIn("Claude Codeで添削した完成記事", result)
        self.assertIn("最初の1回だけ", result)
        self.assertIn("disabled", result)
        with self.assertRaisesRegex(ValueError, "基本プロンプト"):
            app.make_thumbnail_instruction("決定タイトル", "完成記事", "", {}, None)

    def test_new_thumbnail_flow_is_blank_even_when_previous_article_was_saved(self) -> None:
        app.save_setting("thumbnail_latest_title", "前の記事のタイトル")
        app.save_setting("thumbnail_latest_article", "前の記事の完成文")
        fresh_page = HandlerHarness("GET", "/thumbnail").body
        self.assertIn("name='final_title'", fresh_page)
        self.assertIn("name='final_article'", fresh_page)
        self.assertNotIn("前の記事のタイトル", fresh_page)
        self.assertNotIn("前の記事の完成文", fresh_page)
        self.assertLess(fresh_page.index("決まったタイトル"), fresh_page.index("Claude Codeで添削した完成記事"))

        retry_page = HandlerHarness("GET", "/thumbnail?retry=1").body
        self.assertIn("前の記事のタイトル", retry_page)
        self.assertIn("前の記事の完成文", retry_page)

    def test_error_page_can_keep_only_the_current_title_and_article(self) -> None:
        page = app.thumbnail_prepare_page(
            "もう一度試してください。",
            "今回の決定タイトル",
            "今回の完成記事",
        ).decode()
        self.assertIn("今回の決定タイトル", page)
        self.assertIn("今回の完成記事", page)
        self.assertNotIn("前の記事", page)

    def test_two_reference_images_and_prompt_are_persistent_and_automatic(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"image-data"
        saved = app.save_thumbnail_settings(
            "Obsidianから貼った基本プロンプト",
            {
                "style": FakeUpload("いつもの絵柄.png", png),
                "layout": FakeUpload("いつもの配置.png", png),
            },
            set(),
        )
        self.assertEqual(set(saved), {"style", "layout"})
        self.assertEqual(app.setting_value("thumbnail_base_prompt"), "Obsidianから貼った基本プロンプト")
        page = app.thumbnail_prepare_page().decode()
        self.assertIn("登録済みの基本プロンプトと参考画像2枚を、毎回自動で使います", page)
        self.assertNotIn("type='file' accept='image/png,image/jpeg,image/webp' multiple", page)
        self.assertIn("3枚目の参考画像（今回だけ・任意）", page)
        self.assertIn("サムネ画像を作る", page)
        self.assertIn("APIキーや追加料金の設定は使いません", page)

    def test_instruction_uses_both_registered_images_without_generating_an_image(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"image-data"
        references = app.save_thumbnail_settings(
            "基本プロンプト",
            {"style": FakeUpload("style.png", png), "layout": FakeUpload("layout.png", png)},
            set(),
        )
        with patch.object(app, "ask_codex", return_value="完成した画像指示文") as mocked:
            result = app.make_thumbnail_instruction("決定したタイトル", "Claude Code完成記事", "基本プロンプト", references)
        self.assertEqual(result, "完成した画像指示文")
        _, kwargs = mocked.call_args
        self.assertEqual(len(kwargs["images"]), 2)
        self.assertTrue(kwargs["ignore_user_config"])
        prompt = mocked.call_args.args[0]
        self.assertIn("画像そのものは生成せず", prompt)
        self.assertIn("【決まったタイトル】\n決定したタイトル", prompt)
        self.assertIn("【Claude Codeで添削した完成記事】\nClaude Code完成記事", prompt)
        self.assertIn("別のタイトルを作らない", prompt)

    def test_title_and_article_are_both_required(self) -> None:
        references = {"style": {"stored_name": "style.png"}, "layout": {"stored_name": "layout.png"}}
        with self.assertRaisesRegex(ValueError, "決まったタイトル"):
            app.make_thumbnail_instruction("", "完成記事", "基本プロンプト", references)
        with self.assertRaisesRegex(ValueError, "Claude Code"):
            app.make_thumbnail_instruction("決定タイトル", "", "基本プロンプト", references)

    def test_generation_always_passes_two_registered_images_and_optional_third(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + (b"image-data" * 20)
        references = app.save_thumbnail_settings(
            "基本プロンプト",
            {"style": FakeUpload("style.png", png), "layout": FakeUpload("layout.png", png)},
            set(),
        )

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            marker = "保存する: "
            target = Path(command[-1].split(marker, 1)[1].splitlines()[0])
            target.write_bytes(png)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(app.subprocess, "run", side_effect=fake_run) as mocked:
            generated = app.generate_thumbnail_image("完成した画像指示文", references)
        command = mocked.call_args.args[0]
        passed_images = [command[index + 1] for index, value in enumerate(command) if value == "--image"]
        self.assertEqual(
            passed_images,
            [
                str(app.THUMBNAIL_ASSET_DIR / references["style"]["stored_name"]),
                str(app.THUMBNAIL_ASSET_DIR / references["layout"]["stored_name"]),
            ],
        )
        self.assertIn("$imagegen", command[-1])
        self.assertNotIn("OPENAI_API_KEY", " ".join(command))
        self.assertNotIn("--ask-for-approval", command)
        self.assertTrue(generated.is_file())

        extra = Path(self.temp.name) / "extra.png"
        extra.write_bytes(png)
        with patch.object(app.subprocess, "run", side_effect=fake_run) as mocked_extra:
            app.generate_thumbnail_image("完成した画像指示文", references, extra)
        extra_command = mocked_extra.call_args.args[0]
        extra_images = [extra_command[index + 1] for index, value in enumerate(extra_command) if value == "--image"]
        self.assertEqual(extra_images[:2], passed_images)
        self.assertEqual(extra_images[2], str(extra))

    def test_missing_generated_image_is_a_clear_failure(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + (b"image-data" * 20)
        references = app.save_thumbnail_settings(
            "基本プロンプト",
            {"style": FakeUpload("style.png", png), "layout": FakeUpload("layout.png", png)},
            set(),
        )
        completed = subprocess.CompletedProcess(["codex"], 0, "", "")
        with (
            patch.object(app.subprocess, "run", return_value=completed),
            patch.object(app, "record_local_error"),
            self.assertRaisesRegex(app.ThumbnailGenerationError, "完成画像"),
        ):
            app.generate_thumbnail_image("画像指示文", references)

    def test_result_page_displays_and_downloads_the_saved_image(self) -> None:
        filename = "note-thumbnail-20260816-120000-abcdef12.png"
        page = app.thumbnail_result_page(filename, False).decode()
        self.assertIn("noteのサムネ画像ができました", page)
        self.assertIn(f"/thumbnail-image?name={filename}", page)
        self.assertIn(f"/thumbnail-image?name={filename}&amp;download=1", page)
        self.assertIn("画像をダウンロード", page)
        self.assertIn("このMacのPersonal Flowにも保存済み", page)
        self.assertNotIn("画像指示文", page)
        self.assertIn("/thumbnail?retry=1", page)

    def test_generated_image_path_rejects_unrelated_paths(self) -> None:
        self.assertIsNone(app.generated_thumbnail_path("../../personal-flow.db"))
        self.assertIsNone(app.generated_thumbnail_path("other.png"))

    def test_saved_image_route_supports_display_and_download(self) -> None:
        app.THUMBNAIL_OUTPUT_DIR.mkdir(parents=True)
        filename = "note-thumbnail-20260816-120000-abcdef12.png"
        image = b"\x89PNG\r\n\x1a\n" + (b"image-data" * 20)
        (app.THUMBNAIL_OUTPUT_DIR / filename).write_bytes(image)
        display = HandlerHarness("GET", f"/thumbnail-image?name={filename}")
        self.assertEqual(display.status, 200)
        self.assertEqual(display.response_headers["Content-Type"], "image/png")
        self.assertTrue(display.response_headers["Content-Disposition"].startswith("inline"))
        self.assertEqual(display.raw_body, image)
        download = HandlerHarness("GET", f"/thumbnail-image?name={filename}&download=1")
        self.assertEqual(download.status, 200)
        self.assertTrue(download.response_headers["Content-Disposition"].startswith("attachment"))

    def test_settings_page_is_the_only_place_for_replace_and_remove(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"image-data"
        app.save_thumbnail_settings(
            "基本プロンプト",
            {"style": FakeUpload("style.png", png), "layout": FakeUpload("layout.png", png)},
            set(),
        )
        settings = app.thumbnail_settings_page().decode()
        prepare = app.thumbnail_prepare_page().decode()
        self.assertIn("画像1を登録・差し替え", settings)
        self.assertIn("画像2を登録・差し替え", settings)
        self.assertIn("name='remove_style'", settings)
        self.assertNotIn("name='remove_style'", prepare)


class ArticleFlowHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(app, "DB_PATH", Path(self.temp.name) / "flow.db")
        self.db_patch.start()
        active = app.active_flow()
        with app.database() as connection:
            self.item_ids = [
                connection.execute(
                    "INSERT INTO items(url, title, article, summary, note, created_at, flow_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"https://example.com/{number}",
                        f"記事{number}",
                        f"記事{number}の冒頭。具体例。記事{number}の結論。" * 30,
                        f"記事{number}の要点",
                        f"本人メモ{number}",
                        "2026-08-16 10:00",
                        active["id"],
                    ),
                ).lastrowid
                for number in (1, 2)
            ]
    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp.cleanup()

    def request(self, method: str, path: str, values: list[tuple[str, str]] | None = None) -> tuple[int, dict[str, str], str]:
        handler = HandlerHarness(method, path, values)
        return handler.status, handler.response_headers, handler.body

    def create_run(self) -> int:
        proposal = {
            "themes": [{
                "title": "テーマ",
                "approach": "何を書くか",
                "picked": "選んだ理由",
                "left_out": "成功談は外す",
                "sources": [1, 2],
                "source_item_ids": self.item_ids[:],
            }],
            "item_ids": self.item_ids[:],
            "user_angle": "本人の思い",
        }
        with app.database() as connection:
            flow_id = app.active_flow()["id"]
            return connection.execute(
                "INSERT INTO theme_runs(proposal_json, flow_id, created_at) VALUES (?, ?, ?)",
                (json.dumps(proposal, ensure_ascii=False), flow_id, "2026-08-16 10:00"),
            ).lastrowid

    def test_full_http_flow_uses_isolated_ai_and_keeps_saved_items(self) -> None:
        with patch.object(app, "ask_codex", side_effect=[valid_proposal(), "方向性の結果", valid_draft()]) as mocked:
            status, headers, _ = self.request(
                "POST",
                "/suggest",
                [("item_ids", str(item_id)) for item_id in self.item_ids] + [("user_angle", "本人の思い")],
            )
            self.assertEqual(status, 303)
            location = headers["Location"]
            status, _, theme_page = self.request("GET", location)
            self.assertEqual(status, 200)
            self.assertIn("書けそうなテーマ", theme_page)
            run_id = int(location.split("=")[-1])
            with app.database() as connection:
                saved = json.loads(connection.execute("SELECT proposal_json FROM theme_runs WHERE id = ?", (run_id,)).fetchone()[0])
            self.assertEqual(set(saved["item_ids"]), set(self.item_ids))
            self.assertEqual(saved["themes"][0]["source_item_ids"], [saved["item_ids"][0]])

            status, _, direction_page = self.request("POST", "/direction", [("run_id", str(run_id)), ("theme", "0")])
            self.assertEqual(status, 200)
            self.assertIn("記事の方向性を固める", direction_page)
            with app.database() as connection:
                direction_id = connection.execute("SELECT id FROM direction_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
            status, _, form_page = self.request("GET", f"/article-form?run_id={run_id}&theme=0&direction_run_id={direction_id}")
            self.assertEqual(status, 200)
            self.assertIn("note記事の下書きを作る", form_page)

            status, _, draft_page = self.request(
                "POST",
                "/draft",
                [("run_id", str(run_id)), ("theme", "0"), ("direction", "方向性の結果"), ("memo", "本人の追記メモ"), ("length", "約2,000字")],
            )
            self.assertEqual(status, 200)
            for label in ("タイトル案", "記事の軸", "見出し", "本文初稿", "公開前チェック"):
                self.assertIn(label, draft_page)
            self.assertIn("おすすめハッシュタグ", draft_page)
            self.assertIn("タグだけをまとめてコピー", draft_page)
            self.assertIn("完成記事を貼ってサムネ準備へ", draft_page)
        self.assertEqual(mocked.call_count, 3)
        for call in mocked.call_args_list:
            self.assertTrue(call.kwargs["ignore_user_config"])
            self.assertTrue(call.kwargs["model"])
            self.assertTrue(call.kwargs["reasoning_effort"])
        self.assertEqual(len(app.flow_items(app.active_flow()["id"])), 2)

    def test_deleted_source_stops_downstream_without_changing_remaining_data(self) -> None:
        run_id = self.create_run()
        with app.database() as connection:
            connection.execute("DELETE FROM items WHERE id = ?", (self.item_ids[0],))
        status, _, body = self.request("POST", "/direction", [("run_id", str(run_id)), ("theme", "0")])
        self.assertEqual(status, 409)
        self.assertIn("別の記事を根拠にしないよう", body)
        with app.database() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM direction_runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)

    def test_draft_timeout_http_page_keeps_all_retry_values(self) -> None:
        run_id = self.create_run()
        with patch.object(app, "ask_codex", side_effect=app.CodexRunError("timeout", "time")) as mocked:
            status, _, body = self.request(
                "POST",
                "/draft",
                [("run_id", str(run_id)), ("theme", "0"), ("direction", "方向性"), ("memo", "本人メモ"), ("length", "約1,800字")],
            )
        self.assertEqual(status, 504)
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("同じ内容でもう一度試す", body)
        for value in ("方向性", "本人メモ", "約1,800字"):
            self.assertIn(value, body)

    def test_organize_and_confirmation_are_visible_without_deleting(self) -> None:
        status, _, home = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("不要な情報を整理する", home)
        status, _, organize = self.request("GET", "/organize")
        self.assertEqual(status, 200)
        self.assertIn("選んだ0件を削除", organize)
        status, _, zero = self.request("POST", "/delete-items-confirm", [])
        self.assertEqual(status, 400)
        self.assertIn("1件以上選んでください", zero)
        status, _, confirmation = self.request(
            "POST",
            "/delete-items-confirm",
            [("item_ids", str(item_id)) for item_id in self.item_ids],
        )
        self.assertEqual(status, 200)
        self.assertIn("選んだ2件を削除しますか", confirmation)
        status, _, choose_sources = self.request("GET", "/choose-sources")
        self.assertEqual(status, 200)
        self.assertIn("テーマに使う情報を選ぶ", choose_sources)
        self.assertEqual(len(app.flow_items(app.active_flow()["id"])), 2)

    def test_final_delete_stops_if_article_box_changed(self) -> None:
        old_flow = app.active_flow()
        app.start_next_flow(old_flow, keep=True)
        status, _, body = self.request(
            "POST",
            "/delete-items",
            [("flow_id", str(old_flow["id"]))] + [("item_ids", str(item_id)) for item_id in self.item_ids],
        )
        self.assertEqual(status, 409)
        self.assertIn("記事の箱が切り替わった", body)
        with app.database() as connection:
            remaining = connection.execute("SELECT COUNT(*) FROM items WHERE flow_id = ?", (old_flow["id"],)).fetchone()[0]
        self.assertEqual(remaining, 2)


class LocalLogTests(unittest.TestCase):
    def test_technical_detail_is_written_to_local_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "personal-flow-error.log"
            with patch.object(app, "DIAGNOSTIC_LOG_PATH", log_path):
                app.record_local_error("theme_suggestion", "invalid_json", "line 1")
            self.assertIn("code=invalid_json", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
