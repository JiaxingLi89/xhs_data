import tempfile
import unittest
from pathlib import Path
from xhs_research.config import Config
from xhs_research.control import RequestGate
from xhs_research.demo import FakeClock
from xhs_research.errors import StopRun, RiskBlocked
from xhs_research.runtime import local_day
from xhs_research.store import Store


class BatchPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name), notes_per_run=15, requests_per_run=90,
                             daily_requests_pilot=700, request_interval_seconds=[120, 120],
                             run_timeout_seconds=50000)
        self.store = Store(self.config.state_dir)
        self.clock = FakeClock()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def gate(self):
        return RequestGate(self.store, self.config, self.clock, lambda *_: None)

    async def test_batch_and_daily_limits_survive_restart(self):
        async def ok(): return True
        gate = self.gate()
        for _ in range(90):
            await gate.call("auth_check", ok)
        with self.assertRaisesRegex(StopRun, "per_run_request_limit"):
            await gate.call("auth_check", ok)
        gate = self.gate()
        await gate.call("auth_check", ok)
        day = local_day(self.clock.now(), self.config.timezone)
        self.assertEqual(self.store.count_requests(day), 91)
        for _ in range(609):
            self.store.db.execute("INSERT INTO requests(day,at,kind) VALUES(?,?,?)", (day,self.clock.now(),"auth_check"))
        with self.assertRaisesRegex(StopRun, "daily_request_limit"):
            await self.gate().call("auth_check", ok)
        self.assertEqual(self.store.count_requests(day), 700)

    async def test_note_limit_and_failed_request_count(self):
        gate = self.gate()
        gate.run_notes = {str(i) for i in range(15)}
        async def fail(): raise RiskBlocked("test")
        with self.assertRaisesRegex(StopRun, "per_run_note_limit"):
            await gate.call("detail", fail, "sixteenth")
        self.assertEqual(gate.run_requests, 0)
        with self.assertRaises(RiskBlocked):
            await gate.call("auth_check", fail)
        self.assertEqual(gate.run_requests, 1)

    def test_migration_preserves_manual_pause_and_is_once_only(self):
        self.store.set("browser_cooldown_2h_migrated", False)
        self.store.set("pause", dict(reason="browser_page_restricted", since=1000, until=87400))
        self.store.migrate_browser_cooldown()
        self.assertEqual(self.store.get("pause")["until"], 8200)
        with self.assertRaisesRegex(StopRun, "cooldown_not_finished"):
            self.store.resume(8199)
        with self.assertRaisesRegex(StopRun, "paused"):
            self.store.check_pause(8201)
        self.store.resume(8201)
        self.store.set("pause", dict(reason="browser_page_restricted", since=9000, until=20000))
        self.store.migrate_browser_cooldown()
        self.assertEqual(self.store.get("pause")["until"], 20000)

    def test_other_pause_not_changed(self):
        self.store.set("browser_cooldown_2h_migrated", False)
        pause = dict(reason="other", since=1000, until=87400)
        self.store.set("pause", pause)
        self.store.migrate_browser_cooldown()
        self.assertEqual(self.store.get("pause"), pause)
