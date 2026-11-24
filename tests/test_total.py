import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from xhs_research.cli import main
from xhs_research.config import Config
from xhs_research.errors import SetupError
from xhs_research.export import NOTE_COLUMNS, COMMENT_COLUMNS, SOURCE_COLUMNS, write_xlsx
from xhs_research.store import Store
from xhs_research.total import read_research, total_note_ids


def table_row(header, **values):
    return [values.get(column, "") for column in header]


class TotalResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def workbook(self, name, notes=(), comments=(), sources=()):
        path = self.root / name
        write_xlsx(path, {"notes": [NOTE_COLUMNS, *notes], "comments": [COMMENT_COLUMNS, *comments],
                          "sources": [SOURCE_COLUMNS, *sources]})
        return path

    def test_read_external_total_and_skip_existing_or_source_only_note(self):
        path = self.workbook("totalresearch.xlsx",
                             [table_row(NOTE_COLUMNS, note_id="n1", state="comments", fetched_at="1")],
                             [table_row(COMMENT_COLUMNS, comment_id="c1", note_id="n1", fetched_at="1")],
                             [table_row(SOURCE_COLUMNS, note_id="n1", keyword="亲喂"),
                              table_row(SOURCE_COLUMNS, note_id="n2", keyword="断奶")])
        original = path.read_bytes()
        tables = read_research(path)
        self.assertEqual(tables["notes"][1][1], "comments")
        self.assertEqual(total_note_ids(path), {"n1", "n2"})
        self.assertEqual(path.read_bytes(), original)

        config = Config(root=self.root)
        store = Store(config.state_dir)
        try:
            store.load_external_note_ids(total_note_ids(path))
            store.register_keywords(config)
            job = store.next_keyword(config)
            store.add_search_page(job, [{"id": n, "token": "token", "source": "search"} for n in ("n1", "n2", "n3")],
                                  True, 1, config.sampling_version)
            self.assertEqual([r[0] for r in store.db.execute("SELECT note_id FROM notes")], ["n3"])
            store.load_external_note_ids({"n3"})
            self.assertIsNone(store.next_note("2026-11-24"))
            self.assertEqual(store.pending_count(), 0)
        finally:
            store.close()

    def test_invalid_or_missing_workbook_fails(self):
        with self.assertRaises(SetupError):
            total_note_ids(self.root / "missing.xlsx")
        malformed = self.root / "bad.xlsx"
        malformed.write_text("not an Excel file", encoding="utf-8")
        with self.assertRaises(SetupError):
            total_note_ids(malformed)

    def test_cli_does_not_offer_or_accept_merge_command(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertNotIn("merge-total", output.getvalue())
        self.assertIn("export", output.getvalue())
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            main(["merge-total", "inbox/*.xlsx", "--output", "totalresearch.xlsx"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("invalid choice", errors.getvalue())
