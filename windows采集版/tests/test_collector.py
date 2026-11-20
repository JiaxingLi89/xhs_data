import asyncio
import dataclasses
import json
import plistlib
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from xhs_research.adapter import classify_response, retry_after
from xhs_research.config import Config
from xhs_research.control import RequestGate, SessionManager
from xhs_research.demo import DemoAdapter, FakeClock
from xhs_research.engine import Engine
from xhs_research.errors import (AuthExpired, BrowserDisconnected, NetworkFailure, ProtocolError,
                                 RiskBlocked, SetupError, StaleLink, StopRun, Unavailable)
from xhs_research.export import export_data
from xhs_research.runtime import local_day, process_lock
from xhs_research.schedule import create_schedule, scheduled_slot
from xhs_research.store import Store
from xhs_research.validation import comments_page, note_detail, search_page


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Explicit legacy limits exercise compatibility with existing configurations.
        self.config = Config(root=Path(self.temp.name), request_interval_seconds=[120, 120],
                             daily_notes_pilot=5, daily_notes_formal=10, notes_per_run=5,
                             daily_requests_pilot=30, daily_requests_formal=60,
                             comments_per_note=50, max_comment_pages=20,
                             run_timeout_seconds=14400, candidate_low_watermark=1)
        self.store = Store(self.config.state_dir)
        self.clock = FakeClock()
        self.adapter = DemoAdapter(self.clock)
        self.reset_session()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reset_session(self):
        self.gate = RequestGate(self.store, self.config, self.clock, lambda *_: None)
        self.session = SessionManager(self.adapter, self.gate, self.store, self.config, self.clock, lambda *_: None)
        self.engine = Engine(self.config, self.store, self.session, self.clock, lambda *_: None)

    async def run_until_stop(self):
        try:
            await self.engine.run()
        except StopRun:
            pass

    def seed(self, count=20):
        self.store.register_keywords(self.config)
        job = self.store.next_keyword(self.config)
        self.store.add_search_page(job, [{"id": "n%d" % n, "token": "SECRET_TOKEN", "source": "pc_search"} for n in range(count)], True,
                                   self.clock.now(), self.config.sampling_version)

    async def test_missing_credentials_skip_checks_but_preserve_wait_and_confirmation(self):
        self.adapter.credentials_present = False
        self.adapter.authenticated = False
        start = self.clock.now()
        self.store.set("next_request_at", start + 208)
        messages = []
        self.gate.progress = messages.append
        await self.session.ensure_ready(manual=True)
        self.assertEqual([c[1] for c in self.adapter.calls], ["login_page", "auth_check"])
        self.assertEqual(self.adapter.calls[0][0], start + 208)
        self.assertGreaterEqual(self.adapter.calls[1][0] - self.adapter.calls[0][0], 120)
        self.assertEqual(self.store.count_requests(self.gate.day), 2)
        self.assertFalse(self.store.get("auth_waiting"))
        self.assertIn("等待打开登录页面", messages[0])
        self.assertIn("208秒", messages[0])
        self.assertIn("178秒", messages[1])
        self.assertIn("等待检查登录状态", messages[-1])

    async def test_missing_credentials_do_not_bypass_pause_or_budget(self):
        self.adapter.credentials_present = False
        self.store.pause(self.clock.now(), "test", self.clock.now() + 86400)
        with self.assertRaises(StopRun):
            await self.session.ensure_ready(manual=True)
        self.assertEqual(self.adapter.calls, [])
        self.clock.timestamp += 86401
        self.store.resume(self.clock.now())
        self.reset_session()
        self.config.daily_requests_pilot = 1
        await self.gate.call("test", self.adapter.sync_cookies)
        with self.assertRaises(StopRun):
            await self.session.ensure_ready(manual=True)
        self.assertEqual(self.adapter.login_windows, 0)

    async def test_search_returns_twenty_but_only_five_details_and_fifty_comments_each(self):
        await self.run_until_stop()
        self.assertEqual(self.store.complete_count(), 5)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notes").fetchone()[0], 20)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM comments").fetchone()[0], 250)
        self.assertEqual(len([c for c in self.adapter.calls if c[1] == "detail"]), 5)
        for first, second in zip(self.adapter.calls, self.adapter.calls[1:]):
            self.assertGreaterEqual(second[0] - first[0], 120)

    async def test_restarting_does_not_reset_quota_or_repeat_completed_notes(self):
        await self.run_until_stop()
        day = self.gate.day
        count = self.store.count_requests(day)
        self.store.close()
        self.store = Store(self.config.state_dir)
        self.reset_session()
        await self.run_until_stop()
        self.assertEqual(self.store.count_requests(day), count)
        self.assertEqual(self.store.complete_count(), 5)

    async def test_request_budget_interrupts_comments_then_next_day_resumes_cursor(self):
        self.config.daily_requests_pilot = 4  # auth + search + detail + first comment page
        await self.run_until_stop()
        row = self.store.db.execute("SELECT * FROM notes WHERE state='comments'").fetchone()
        self.assertEqual(row["cursor"], "20")
        self.clock.timestamp += 86400
        self.config.daily_requests_pilot = 30
        self.reset_session()
        before = len(self.adapter.calls)
        await self.run_until_stop()
        later = self.adapter.calls[before:]
        self.assertFalse(any(c[1] == "detail" and c[2] == row["note_id"] for c in later))
        self.assertEqual(next(c[3] for c in later if c[1] == "comments" and c[2] == row["note_id"]), "20")

    async def test_network_failure_does_not_trigger_login_or_remove_session(self):
        self.adapter.failures["auth_check"] = [NetworkFailure("offline")]
        with self.assertRaises(NetworkFailure):
            await self.session.ensure_ready()
        self.assertEqual(self.adapter.login_windows, 0)
        self.assertTrue(self.adapter.authenticated)
        self.assertFalse(self.store.get("auth_waiting", False))
        with self.assertRaises(StopRun):
            await self.session.ensure_ready()
        self.assertEqual(len(self.adapter.calls), 1)

    async def test_network_retry_is_bounded_across_restarts(self):
        self.adapter.failures["auth_check"] = [NetworkFailure("offline"), NetworkFailure("offline")]
        with self.assertRaises(NetworkFailure):
            await self.session.ensure_ready()
        self.clock.timestamp += 900
        self.reset_session()
        with self.assertRaises(NetworkFailure):
            await self.session.ensure_ready()
        self.assertIsNotNone(self.store.get("pause"))
        self.clock.timestamp += 86400
        self.reset_session()
        with self.assertRaises(StopRun):
            await self.session.ensure_ready()
        self.assertEqual(self.adapter.login_windows, 0)

    async def test_expired_session_opens_one_login_then_automatically_collects(self):
        self.adapter.authenticated = False
        await self.run_until_stop()
        self.assertEqual(self.adapter.login_windows, 1)
        self.assertFalse(self.store.get("auth_waiting"))
        self.assertEqual(self.store.complete_count(), 5)
        self.assertTrue(self.store.get("auto_login_day"))

    async def test_waiting_for_login_survives_next_day_without_more_popup(self):
        self.adapter.authenticated = False
        self.adapter.login_succeeds = False
        with self.assertRaises(StopRun):
            await self.session.ensure_ready()
        self.clock.timestamp += 86400
        self.reset_session()
        with self.assertRaises(StopRun):
            await self.session.ensure_ready()
        self.assertEqual(self.adapter.login_windows, 1)
        self.adapter.login_succeeds = True
        await self.session.ensure_ready(manual=True)
        self.assertEqual(self.adapter.login_windows, 2)

    async def test_cookie_resync_can_restore_session_without_login_window(self):
        self.adapter.failures["auth_check"] = [AuthExpired("stale"), None]
        await self.session.ensure_ready()
        self.assertEqual(self.adapter.login_windows, 0)

    async def test_mid_comment_login_recovery_reuses_same_cursor(self):
        self.adapter.failures["comments"] = [None, AuthExpired("expired"), None]
        await self.run_until_stop()
        calls = [c for c in self.adapter.calls if c[1] == "comments"]
        self.assertEqual(calls[1][3], calls[2][3])
        self.assertEqual(self.store.complete_count(), 5)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM comments").fetchone()[0], 250)

    async def test_rate_limit_blocks_entire_run_and_manual_login_does_not_clear_it(self):
        self.adapter.failures["comments"] = [RiskBlocked("rate_limited", 86400)]
        with self.assertRaises(RiskBlocked):
            await self.engine.run()
        count = len(self.adapter.calls)
        with self.assertRaises(StopRun):
            await self.session.ensure_ready(manual=True)
        with self.assertRaises(StopRun):
            self.store.resume(self.clock.now())
        self.clock.timestamp += 86401
        self.reset_session()
        with self.assertRaises(StopRun):
            await self.session.ensure_ready()
        self.assertEqual(len(self.adapter.calls), count)
        self.store.resume(self.clock.now())
        self.assertIsNone(self.store.get("pause"))

    async def test_repeated_cursor_marks_partial_sample_and_stops_paging(self):
        self.seed(1)
        note_id = "n0"
        self.store.save_comments_page(note_id, "", "same", [{"id": "c1", "content": "a"}], True, self.clock.now(), self.config)
        self.store.save_comments_page(note_id, "same", "same", [{"id": "c1", "content": "a"}], True, self.clock.now(), self.config)
        row = self.store.db.execute("SELECT * FROM notes").fetchone()
        self.assertEqual(row["state"], "needs_review")
        self.assertEqual(row["reason"], "repeated_cursor")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM comments").fetchone()[0], 1)

    async def test_cross_keyword_duplicate_is_one_note_with_multiple_sources(self):
        self.seed(2)
        self.config.keywords = ["另一个关键词"]
        self.seed(2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notes").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sources").fetchone()[0], 4)

    async def test_reservation_survives_interruption_and_delays_next_signature(self):
        self.store.reserve_request(self.gate.day, self.clock.now(), "detail", "n1", self.config, self.clock.now() + 240)
        before = self.clock.now()
        signatures = []
        async def sign_and_send():
            signatures.append(self.clock.now())
            return True
        self.reset_session()
        await self.gate.call("detail", sign_and_send, "n1")
        self.assertEqual(signatures, [before + 240])
        self.assertEqual(self.store.count_requests(self.gate.day), 2)

    async def test_browser_disconnect_rebuilds_at_most_once_and_preserves_backoff(self):
        self.adapter.failures["detail"] = [BrowserDisconnected("closed")]
        with self.assertRaises(BrowserDisconnected):
            await self.session.call("detail", lambda: self.adapter.detail("n0", "x", "pc_search"), "n0")
        self.assertEqual(self.adapter.reopens, 1)
        self.assertGreater(self.store.get("network_until"), self.clock.now())
        self.assertEqual(self.adapter.login_windows, 0)

    async def test_exports_are_joinable_and_have_no_tokens_or_profile_data(self):
        await self.run_until_stop()
        path = export_data(self.store, self.config, self.clock.now())
        text = "".join(p.read_text(encoding="utf-8-sig") for p in path.glob("*.csv"))
        self.assertNotIn("DEMO_TOKEN", text)
        self.assertIn("DEMO_AUTHOR", text)
        self.assertIn("DEMO_COMMENTER", text)
        self.assertNotIn("不应导出", text)
        self.assertNotIn("not-collected", text)
        with zipfile.ZipFile(path / "research.xlsx") as archive:
            for name in archive.namelist():
                ElementTree.fromstring(archive.read(name))
        manifest = json.loads((path / "manifest.json").read_text())
        self.assertEqual(manifest["counts"]["notes"], 5)
        self.assertEqual(manifest["counts"]["comments"], 250)

    async def test_legacy_daily_note_limit_stops_before_more_searches_after_restart(self):
        self.config.keywords = ["甲", "乙"]
        await self.run_until_stop()
        self.reset_session()
        await self.run_until_stop()
        self.assertEqual(len([c for c in self.adapter.calls if c[1] == "search"]), 1)

    async def test_unavailable_note_is_not_retried_and_still_uses_note_budget(self):
        self.adapter.failures["detail"] = [Unavailable("deleted")]
        await self.run_until_stop()
        self.assertEqual(self.store.complete_count(), 4)
        self.assertEqual(self.store.count_daily_notes(self.gate.day), 5)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notes WHERE state='unavailable'").fetchone()[0], 1)

    async def test_stale_link_refreshes_once_and_retries_detail(self):
        self.adapter.failures["detail"] = [StaleLink("expired")]
        await self.run_until_stop()
        self.assertIsNone(self.store.link_refresh_job())
        first_id = next(c[2] for c in self.adapter.calls if c[1] == "detail")
        self.assertEqual(len([c for c in self.adapter.calls if c[1] == "detail" and c[2] == first_id]), 2)
        self.assertEqual(self.store.db.execute("SELECT refresh_attempts FROM notes WHERE note_id=?", (first_id,)).fetchone()[0], 1)

    async def test_stale_link_refresh_does_not_loop_after_second_failure(self):
        self.adapter.failures["detail"] = [StaleLink("expired"), StaleLink("expired")]
        await self.run_until_stop()
        self.assertIsNone(self.store.link_refresh_job())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notes WHERE reason='link_refresh_exhausted'").fetchone()[0], 1)

    async def test_final_search_page_items_are_saved_even_when_has_more_false(self):
        rows, more = search_page({"items": [{"id": "last", "xsec_token": "x"}], "has_more": False})
        self.store.register_keywords(self.config)
        self.store.add_search_page(self.store.next_keyword(self.config), rows, more, self.clock.now(), "v1")
        self.assertEqual(self.store.pending_count(), 1)
        self.assertIsNone(self.store.next_keyword(self.config))

    async def test_transactions_rollback_comments_and_cursor_together(self):
        self.seed(2)
        self.store.save_comments_page("n0", "", "", [{"id": "same-id", "content": "original"}], False, self.clock.now(), self.config)
        with self.assertRaises(ProtocolError):
            self.store.save_comments_page("n1", "", "20", [{"id": "new", "content": "new"}, {"id": "same-id", "content": "bad"}], True, self.clock.now(), self.config)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM comments WHERE note_id='n1'").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT cursor FROM notes WHERE note_id='n1'").fetchone()[0], "")

    async def test_scheduled_wakeup_skips_missed_slot_and_duplicate_slot(self):
        at = datetime(2026, 11, 13, 10, 2, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        scheduled_slot(at, self.config, self.store)
        with self.assertRaises(StopRun):
            scheduled_slot(at + 60, self.config, self.store)
        with self.assertRaises(StopRun):
            scheduled_slot(at + 3600, self.config, self.store)

    async def test_process_lock_prevents_two_workers_sharing_profile(self):
        with process_lock(self.config.state_dir):
            with self.assertRaises(StopRun):
                with process_lock(self.config.state_dir):
                    pass

    async def test_pilot_limit_stops_without_another_login_or_search(self):
        self.config.pilot_notes = 5
        await self.run_until_stop()
        calls = len(self.adapter.calls)
        self.clock.timestamp += 86400
        self.reset_session()
        await self.run_until_stop()
        self.assertEqual(len(self.adapter.calls), calls)


class PureTests(unittest.TestCase):
    def test_response_classification_including_http_200_risk(self):
        cases = [(401, {}, None, AuthExpired), (403, {}, None, RiskBlocked), (461, {}, None, RiskBlocked),
                 (471, {}, None, RiskBlocked), (429, {"Retry-After": "120"}, None, RiskBlocked),
                 (503, {}, None, NetworkFailure), (200, {}, {"code": 300011}, RiskBlocked),
                 (200, {}, {"code": 300012}, RiskBlocked), (200, {}, {"code": -510000}, Unavailable),
                 (200, {}, {"success": True, "data": None}, ProtocolError),
                 (200, {}, {"success": False, "msg": "登录已过期"}, AuthExpired),
                 (200, {}, "<html>verify</html>", ProtocolError)]
        for status, headers, body, expected in cases:
            with self.subTest(status=status, body=body):
                with self.assertRaises(expected):
                    classify_response(status, headers, body, 0)

    def test_retry_after_seconds_date_and_fallback(self):
        self.assertEqual(retry_after("120", 0), 120)
        self.assertEqual(retry_after("Thu, 01 Jan 1970 00:03:00 GMT", 60), 120)
        self.assertEqual(retry_after(None, 0), 86400)

    def test_empty_and_missing_comments_are_distinct(self):
        self.assertEqual(comments_page({"comments": [], "has_more": False}, "n"), ([], "", False))
        with self.assertRaises(ProtocolError):
            comments_page({"has_more": False}, "n")

    def test_wrong_note_id_is_rejected(self):
        with self.assertRaises(ProtocolError):
            note_detail({"items": [{"id": "wrong", "note_card": {"title": "a"}}]}, "expected")

    def test_invalid_configuration_does_not_silently_reduce_delays(self):
        for field, value in [("request_interval_seconds", [1, 2]), ("network_retry_seconds", 1),
                             ("keywords", []), ("data_dir", "../elsewhere")]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "config.json"
                path.write_text(json.dumps({field: value}))
                with self.assertRaises(SetupError):
                    Config.load(path)

    def test_schedule_is_disabled_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config(root=Path(temp))
            with self.assertRaises(SetupError):
                create_schedule(Path(temp) / "config.json", config)
            self.assertFalse((Path(temp) / "schedule").exists())



if __name__ == "__main__":
    unittest.main()
