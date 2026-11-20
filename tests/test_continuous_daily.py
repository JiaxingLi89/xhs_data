import asyncio
import tempfile
import unittest
from pathlib import Path

from xhs_research.config import Config
from xhs_research.control import RequestGate, SessionManager
from xhs_research.demo import DemoAdapter, FakeClock
from xhs_research.engine import Engine
from xhs_research.errors import NetworkFailure, StopRun
from xhs_research.runtime import local_day, next_midnight
from xhs_research.store import Store


class ContinuousDailyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name), continuous_daily=True,
                             daily_requests_pilot=700, daily_requests_formal=700,
                             daily_notes_pilot=None, daily_notes_formal=None,
                             request_interval_seconds=[120, 120])
        self.clock = FakeClock()
        self.store = Store(self.config.state_dir)
        self.adapter = DemoAdapter(self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def gate(self):
        return RequestGate(self.store, self.config, self.clock, lambda *_: None)

    def seed_requests(self, count):
        self.store.db.executemany('INSERT INTO requests(day,at,kind,outcome) VALUES(?,?,?,?)',
                                 [(local_day(self.clock.now()), self.clock.now(), 'comments', 'network')] * count)

    async def test_700th_request_then_wait_across_restart_and_midnight(self):
        midnight = next_midnight(self.clock.now(), self.config.timezone)
        self.clock.timestamp = midnight - 60
        day = local_day(self.clock.now())
        self.seed_requests(699)
        await self.gate().call('auth_check', self.adapter.check_auth)
        self.assertEqual(self.store.count_requests(day), 700)
        self.store.close()
        self.store = Store(self.config.state_dir)
        observed = []
        async def sleep(seconds):
            observed.append((self.clock.now(), len(self.adapter.calls)))
            self.assertLessEqual(seconds, 30)
            self.clock.timestamp += seconds
        self.clock.sleep = sleep
        await self.gate().call('auth_check', self.adapter.check_auth)
        self.assertTrue(observed)
        self.assertTrue(all(count == 1 for _, count in observed))
        self.assertEqual(self.adapter.calls[-1][0], midnight + 60)
        self.assertEqual(self.store.count_requests(day), 700)
        self.assertEqual(self.store.count_requests(local_day(self.clock.now())), 1)

    async def test_full_budget_at_start_waits_without_requests(self):
        self.seed_requests(700)
        midnight = next_midnight(self.clock.now(), self.config.timezone)
        await self.gate().call('auth_check', self.adapter.check_auth)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.calls[0][0], midnight)

    async def test_batch_limits_timeout_and_midnight_do_not_stop(self):
        self.clock.timestamp = next_midnight(self.clock.now(), self.config.timezone) - 60
        gate = self.gate()
        for index in range(122):
            await gate.call('detail', lambda: self.adapter.detail('n', 't', 's'), str(index))
        self.assertEqual(gate.run_requests, 122)
        self.assertEqual(len(gate.run_notes), 122)
        self.assertGreater(self.clock.now() - self.adapter.calls[0][0], 14400)

    async def test_pause_during_quota_wait_is_not_cleared(self):
        self.seed_requests(700)
        async def sleep(seconds):
            self.clock.timestamp += seconds
            self.store.pause(self.clock.now(), 'risk')
        self.clock.sleep = sleep
        with self.assertRaisesRegex(StopRun, 'paused:risk'):
            await self.gate().call('auth_check', self.adapter.check_auth)
        self.assertFalse(self.adapter.calls)
        self.assertEqual(self.store.get('pause')['reason'], 'risk')

    async def test_quota_wait_can_be_cancelled(self):
        self.seed_requests(700)
        async def sleep(seconds):
            raise asyncio.CancelledError()
        self.clock.sleep = sleep
        with self.assertRaises(asyncio.CancelledError):
            await self.gate().call('auth_check', self.adapter.check_auth)
        self.assertFalse(self.adapter.calls)
        self.assertEqual(self.store.count_requests(local_day(self.clock.now())), 700)

    async def test_failure_charged_and_transaction_still_enforces_budget(self):
        self.seed_requests(699)
        self.adapter.failures['auth_check'] = [NetworkFailure('offline')]
        with self.assertRaises(NetworkFailure):
            await self.gate().call('auth_check', self.adapter.check_auth)
        now = self.clock.now()
        self.assertEqual(self.store.count_requests(local_day(now)), 700)
        self.store.set('network_until', 0)
        with self.assertRaisesRegex(StopRun, 'daily_request_limit'):
            self.store.reserve_request(local_day(now), now, 'search', None, self.config, now + 120)

    async def test_engine_searches_multiple_pages_and_stops_at_pilot_review(self):
        async def search(keyword, page, search_id, sort):
            return {'items': [{'id': 'n%d' % page, 'xsec_token': 't'}], 'has_more': True}
        async def comments(note_id, token, cursor):
            return {'comments': [], 'cursor': '', 'has_more': False}
        self.adapter.search = search
        self.adapter.comments = comments
        session = SessionManager(self.adapter, self.gate(), self.store, self.config, self.clock, lambda *_: None)
        engine = Engine(self.config, self.store, session, self.clock, lambda *_: None)
        with self.assertRaisesRegex(StopRun, 'pilot_complete_review_required'):
            await engine.run()
        self.assertEqual(self.store.complete_count(), 50)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests WHERE kind='search'").fetchone()[0], 50)

    def test_config_compatibility_validation_and_status(self):
        self.assertFalse(Config().continuous_daily)
        self.config.validate()
        report = self.store.quota_status(self.clock.now(), self.config)
        self.assertTrue(report['continuous_daily'])
        self.assertIsNone(report['requests_per_run'])
        self.assertIsNone(report['notes_per_run'])
        self.assertEqual(report['daily_requests_limit'], 700)
        self.config.quota_mode = 'rolling'
        with self.assertRaises(ValueError):
            self.config.validate()
        self.config.quota_mode = 'daily'
        self.config.continuous_daily = 'true'
        with self.assertRaises(ValueError):
            self.config.validate()
