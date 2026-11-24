import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xhs_research.cli import collect, promote, quota_report
from xhs_research.config import Config
from xhs_research.control import RequestGate, SessionManager
from xhs_research.demo import DemoAdapter, FakeClock
from xhs_research.engine import Engine
from xhs_research.errors import NetworkFailure, RiskBlocked, SetupError, StopRun
from xhs_research.runtime import local_day, next_midnight
from xhs_research.schedule import create_schedule
from xhs_research.store import Store


class RollingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name), quota_mode='rolling', request_interval_seconds=[120, 120])
        self.store = Store(self.config.state_dir)
        self.clock = FakeClock()
        self.adapter = DemoAdapter(self.clock)
        self.new_batch()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def new_batch(self):
        self.gate = RequestGate(self.store, self.config, self.clock, lambda *_: None)
        self.session = SessionManager(self.adapter, self.gate, self.store, self.config, self.clock, lambda *_: None)
        self.engine = Engine(self.config, self.store, self.session, self.clock, lambda *_: None)

    async def run_batch(self):
        try:
            await self.engine.run()
        except StopRun as exc:
            return str(exc)

    def add_requests(self, count, at=None, outcome='ok'):
        at = self.clock.now() if at is None else at
        self.store.db.executemany('INSERT INTO requests(day,at,kind,outcome) VALUES(?,?,?,?)',
                                 [(local_day(at), at, 'test', outcome)] * count)

    async def test_two_batches_same_day_keep_comments_and_wait(self):
        self.assertEqual(await self.run_batch(), 'per_run_note_limit')
        self.assertEqual(self.store.complete_count(), 5)
        previous_call = self.adapter.calls[-1][0]
        previous_length = len(self.adapter.calls)
        self.new_batch()
        self.assertEqual(await self.run_batch(), 'per_run_note_limit')
        self.assertEqual(self.store.complete_count(), 10)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM comments').fetchone()[0], 600)
        self.assertEqual(len({c[2] for c in self.adapter.calls if c[1] == 'detail'}), 10)
        self.assertGreaterEqual(self.adapter.calls[previous_length][0] - previous_call, 120)
        self.assertEqual(len([c for c in self.adapter.calls if c[1] == 'search']), 2)

    async def test_per_batch_thirty_requests_and_restart(self):
        for _ in range(30):
            await self.gate.call('auth_check', self.adapter.check_auth)
        with self.assertRaisesRegex(StopRun, 'per_run_request_limit'):
            await self.gate.call('auth_check', self.adapter.check_auth)
        self.new_batch()
        await self.gate.call('auth_check', self.adapter.check_auth)
        self.assertEqual(self.store.rolling_usage(self.clock.now())['used'], 31)
        self.assertEqual(self.gate.run_requests, 1)

    async def test_2999_limit_restart_and_no_browser(self):
        self.add_requests(2999)
        await self.gate.call('auth_check', self.adapter.check_auth)
        self.assertEqual(self.store.rolling_usage(self.clock.now())['used'], 3000)
        self.store.close()
        self.store = Store(self.config.state_dir)
        self.new_batch()
        with patch.object(self.adapter, 'open') as opened:
            with self.assertRaisesRegex(StopRun, 'rolling_request_limit'):
                await self.session.ensure_ready(manual=True)
            opened.assert_not_called()
        before = self.store.count_requests(local_day(self.clock.now()))
        with self.assertRaisesRegex(StopRun, 'rolling_request_limit'):
            self.store.reserve_request(local_day(self.clock.now()), self.clock.now(), 'test', None, self.config, 0)
        self.assertEqual(self.store.count_requests(local_day(self.clock.now())), before)

    async def test_midnight_keeps_usage_but_day_stats_change(self):
        self.clock.timestamp = next_midnight(self.clock.now(), 'Asia/Shanghai') - 60
        self.new_batch()
        first_day = local_day(self.clock.now())
        await self.gate.call('auth_check', self.adapter.check_auth)
        await self.gate.call('auth_check', self.adapter.check_auth)
        self.assertNotEqual(first_day, local_day(self.clock.now()))
        self.assertEqual(self.store.count_requests(first_day), 1)
        self.assertEqual(self.store.count_requests(local_day(self.clock.now())), 1)
        self.assertEqual(self.store.rolling_usage(self.clock.now())['used'], 2)

    def test_exact_window_boundary_and_release_report(self):
        now = self.clock.now()
        self.add_requests(1, now - 86400)
        self.add_requests(1, now - 86400 + 1, 'network')
        self.add_requests(1, now, 'reserved')
        report = quota_report(self.store, self.config, now)
        self.assertEqual(report['rolling_requests_used'], 2)
        self.assertEqual(report['rolling_outcomes'], {'network': 1, 'reserved': 1})
        self.assertEqual(report['next_release_at'], now + 1)
        self.assertIsNotNone(report['next_release_local'])
        self.assertEqual(self.store.rolling_usage(now + 1)['used'], 1)

    async def test_failure_pause_and_interrupted_reservations_count(self):
        async def interrupt():
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            await self.gate.call('test', interrupt)
        self.adapter.failures['auth_check'] = [NetworkFailure('test')]
        with self.assertRaises(NetworkFailure):
            await self.gate.call('auth_check', self.adapter.check_auth)
        self.new_batch()
        with self.assertRaisesRegex(StopRun, 'network_backoff'):
            self.gate.preflight()
        self.clock.timestamp += 900
        self.adapter.failures['auth_check'] = [RiskBlocked('test_restriction', 3600)]
        with self.assertRaises(RiskBlocked):
            await self.gate.call('auth_check', self.adapter.check_auth)
        self.new_batch()
        with self.assertRaisesRegex(StopRun, 'paused'):
            self.gate.preflight()
        self.assertEqual(self.store.rolling_usage(self.clock.now())['used'], 3)
        with self.assertRaises(StopRun):
            self.store.resume(self.clock.now())
        self.clock.timestamp += 3600
        self.store.resume(self.clock.now())
        self.assertEqual(self.store.rolling_usage(self.clock.now())['used'], 3)

    async def test_resume_partial_comments_next_batch(self):
        self.config.requests_per_run = 4
        self.assertEqual(await self.run_batch(), 'per_run_request_limit')
        self.assertEqual(self.store.complete_count(), 0)
        self.config.requests_per_run = 30
        self.new_batch()
        await self.run_batch()
        self.assertEqual(self.store.complete_count(), 5)
        first = [c for c in self.adapter.calls if c[1] == 'comments' and c[2] == 'demo0000']
        self.assertEqual([c[3] for c in first], ['', '20', '40'])

    async def test_run_summary_and_scheduled_run_disabled(self):
        with patch('xhs_research.cli.Clock', return_value=self.clock), patch('xhs_research.cli.MediaCrawlerAdapter', return_value=self.adapter):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                await collect(self.config, self.store, 'run')
            self.assertIn('新增完成 5 篇', output.getvalue())
            self.assertIn('本批笔记处理名额已用完', output.getvalue())
            before = len(self.adapter.calls)
            with contextlib.redirect_stdout(io.StringIO()):
                await collect(self.config, self.store, 'run', scheduled=True)
            self.assertEqual(len(self.adapter.calls), before)
        with self.assertRaises(SetupError):
            create_schedule(self.config.root / 'config.json', self.config)
        self.assertFalse((self.config.root / 'schedule').exists())

    def test_configuration_compatibility_and_validation(self):
        path = self.config.root / 'config.json'
        path.write_text('{}')
        self.assertEqual(Config.load(path).quota_mode, 'daily')
        for invalid in ({'quota_mode': 'other'}, {'requests_per_run': 0}, {'rolling_requests_limit': True}):
            path.write_text(json.dumps(invalid))
            with self.assertRaises(SetupError):
                Config.load(path)

    async def test_formal_promotion_preserves_rolling_history(self):
        self.config.pilot_notes = 5
        await self.run_batch()
        before = self.store.rolling_usage(self.clock.now())['used']
        path = self.config.root / 'config.json'
        path.write_text(json.dumps({'quota_mode': 'rolling', 'pilot_notes': 5, 'rolling_requests_limit': 3000}))
        with contextlib.redirect_stdout(io.StringIO()):
            promote(path, self.config, self.store, 10)
        self.config = Config.load(path)
        self.assertEqual(self.config.stage, 'formal')
        self.assertEqual(self.config.rolling_requests_limit, 3000)
        self.assertEqual(self.store.rolling_usage(self.clock.now())['used'], before)
        self.new_batch()
        await self.run_batch()
        self.assertEqual(self.store.complete_count(), 10)

    async def test_wait_still_obeys_run_timeout(self):
        self.config.run_timeout_seconds = 60
        self.new_batch()
        self.store.set('next_request_at', self.clock.now() + 120)
        with self.assertRaisesRegex(StopRun, 'run_window_finished'):
            await self.gate.call('auth_check', self.adapter.check_auth)
        self.assertEqual(self.adapter.calls, [])
