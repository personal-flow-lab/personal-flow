import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


def sample_item(item_id: int = 1) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT ? AS id, ? AS title, ? AS url, ? AS note, ? AS article, ? AS created_at",
        (item_id, "保存した記事", "https://example.com", "自分のメモ", "記事本文", "2026-08-16 10:00"),
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
    def test_confirmation_page_identifies_target_and_does_not_delete(self) -> None:
        item = sample_item(7)
        result = app.delete_item_confirmation_page(item).decode()
        self.assertIn("まだ削除されていません", result)
        self.assertIn("保存した記事", result)
        self.assertIn("https://example.com", result)
        self.assertIn("name='item_id' value='7'", result)

    def test_delete_is_limited_to_the_current_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "DB_PATH", Path(directory) / "test.db"):
                active = app.active_flow()
                with app.database() as connection:
                    saved_flow = connection.execute(
                        "INSERT INTO flows(name, status, created_at) VALUES ('保存済み', 'saved', '2026-08-16 10:00')"
                    ).lastrowid
                    active_item = connection.execute(
                        "INSERT INTO items(url, title, article, summary, note, created_at, flow_id) VALUES (?, ?, '', '', '', ?, ?)",
                        ("https://example.com/active", "今の情報", "2026-08-16 10:00", active["id"]),
                    ).lastrowid
                    saved_item = connection.execute(
                        "INSERT INTO items(url, title, article, summary, note, created_at, flow_id) VALUES (?, ?, '', '', '', ?, ?)",
                        ("https://example.com/saved", "保存済み情報", "2026-08-16 10:00", saved_flow),
                    ).lastrowid
                self.assertFalse(app.delete_flow_item(active["id"], str(saved_item)))
                self.assertTrue(app.delete_flow_item(active["id"], str(active_item)))
                with app.database() as connection:
                    remaining = connection.execute("SELECT id FROM items ORDER BY id").fetchall()
                self.assertEqual([row["id"] for row in remaining], [saved_item])


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
        self.assertEqual(len(self.run_suggestion(valid_proposal())), 3)

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


class LocalLogTests(unittest.TestCase):
    def test_technical_detail_is_written_to_local_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "personal-flow-error.log"
            with patch.object(app, "DIAGNOSTIC_LOG_PATH", log_path):
                app.record_local_error("theme_suggestion", "invalid_json", "line 1")
            self.assertIn("code=invalid_json", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
