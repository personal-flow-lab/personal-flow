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
6. ハッシュタグ案
#学び"""


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
