import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xhs_research.adapter import MediaCrawlerAdapter
from xhs_research.errors import RiskBlocked


class BrowserDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.adapter = MediaCrawlerAdapter(SimpleNamespace(state_dir=self.directory), None,
                                           SimpleNamespace(now=lambda: 1795150531))
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def response(self, url, status=403):
        return SimpleNamespace(url=url, status=status, request=SimpleNamespace(resource_type="xhr"))

    def test_allowlisted_path_without_credentials(self):
        self.adapter.browser_phase = "login_page"
        self.adapter._page_response(self.response(
            "https://user:password@edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo?xsec_token=SECRET#PRIVATE"))
        content = (self.directory / "browser-diagnostics.jsonl").read_text()
        row = json.loads(content)
        self.assertEqual(row["path"], "/api/sns/web/v1/user/selfinfo")
        self.assertEqual(row["phase"], "login_page")
        self.assertEqual(row["status"], 403)
        for secret in ("password", "SECRET", "PRIVATE", "xsec_token"):
            self.assertNotIn(secret, content + self.output.getvalue())
        with self.assertRaisesRegex(RiskBlocked, "browser_page_restricted"):
            self.adapter._check_browser()

    def test_dynamic_and_unknown_paths_redacted(self):
        for path in ("/explore/SECRET", "/user/profile/SECRET", "/unknown/SECRET"):
            self.adapter._page_response(self.response("https://SECRET.xiaohongshu.com" + path))
        content = (self.directory / "browser-diagnostics.jsonl").read_text()
        self.assertNotIn("SECRET", content + self.output.getvalue())
        self.assertEqual(len(content.splitlines()), 3)

    def test_only_existing_status_and_host_rules_trigger(self):
        for url, status in (("https://www.xiaohongshu.com/explore", 200),
                            ("https://xiaohongshu.com.example.org/", 403)):
            self.adapter._page_response(self.response(url, status))
        self.assertFalse(self.adapter.external_risk)
        self.assertFalse((self.directory / "browser-diagnostics.jsonl").exists())
        for status in (403, 429, 461, 471):
            self.adapter._page_response(self.response("https://www.xiaohongshu.com/", status))
        rows = [json.loads(line) for line in (self.directory / "browser-diagnostics.jsonl").read_text().splitlines()]
        self.assertEqual([row["status"] for row in rows], [403, 429, 461, 471])

    def test_disk_failure_does_not_disable_pause_or_leak(self):
        with patch("xhs_research.browser_diagnostics.os.open", side_effect=OSError("SECRET")):
            self.adapter._page_response(self.response("https://www.xiaohongshu.com/"))
        self.assertNotIn("SECRET", self.output.getvalue())
        with self.assertRaises(RiskBlocked):
            self.adapter._check_browser()
