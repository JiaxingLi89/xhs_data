import csv
import dataclasses
import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from xhs_research.cli import stop_message
from xhs_research.config import Config
from xhs_research.control import RequestGate, SessionManager
from xhs_research.demo import DemoAdapter, FakeClock
from xhs_research.engine import Engine
from xhs_research.errors import SetupError, StopRun
from xhs_research.export import export_data, sheets
from xhs_research.store import Store


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name), request_interval_seconds=[120, 120])
        self.store = Store(self.config.state_dir)
        self.clock = FakeClock(datetime(2026, 11, 13, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
        self.adapter = DemoAdapter(self.clock)
        self.reset_session()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reset_session(self):
        self.gate = RequestGate(self.store, self.config, self.clock, lambda *_: None)
        self.session = SessionManager(self.adapter, self.gate, self.store, self.config,
                                      self.clock, lambda *_: None)
        self.engine = Engine(self.config, self.store, self.session, self.clock, lambda *_: None)

    def seed(self, ids):
        self.store.register_keywords(self.config)
        job = self.store.next_keyword(self.config)
        self.store.add_search_page(job, [{'id': n, 'token': 'TEST', 'source': 'pc_search'} for n in ids],
                                   False, self.clock.now(), self.config.sampling_version)
        for note_id in ids:
            self.store.save_detail(note_id, {'title': note_id}, self.clock.now())

    async def test_700_requests_survive_restart_stage_change_and_next_day(self):
        for _ in range(700):
            await self.gate.call('search', self.adapter.sync_cookies)
        day = self.gate.day
        # Failed attempts are charged just like successful ones.
        self.store.db.execute("UPDATE requests SET outcome='network' WHERE id=1")
        self.store.close()
        self.store = Store(self.config.state_dir)
        self.config.stage = 'formal'
        self.reset_session()
        with self.assertRaisesRegex(StopRun, 'daily_request_limit'):
            await self.gate.call('search', self.adapter.sync_cookies)
        self.assertEqual(self.store.count_requests(day), 700)
        self.assertEqual(self.store.status(day, self.config)['requests_remaining'], 0)
        self.clock.timestamp = self.gate.midnight
        self.gate.preflight()
        self.assertEqual(self.store.count_requests(day), 700)
        # The same session continues into the new day's budget.
        await self.gate.call('search', self.adapter.sync_cookies)
        self.assertEqual(self.store.count_requests(self.gate.day), 1)
        self.assertEqual(self.store.status(self.gate.day, self.config)['requests_remaining'], 699)

    async def test_midnight_wait_keeps_interval_and_uses_new_day(self):
        self.clock.timestamp = self.gate.midnight - 60
        old_day = self.gate.day
        await self.gate.call('search', self.adapter.sync_cookies)
        first = self.clock.now()
        await self.gate.call('search', self.adapter.sync_cookies)
        self.assertEqual(self.clock.now() - first, 120)
        self.assertNotEqual(self.gate.day, old_day)
        self.assertEqual(self.store.count_requests(old_day), 1)
        self.assertEqual(self.store.count_requests(self.gate.day), 1)

    async def test_run_timeout_spans_midnight_without_reset(self):
        self.clock.timestamp = self.gate.midnight - 60
        self.config.run_timeout_seconds = 240
        self.reset_session()
        deadline = self.gate.deadline
        await self.gate.call('search', self.adapter.sync_cookies)
        await self.gate.call('search', self.adapter.sync_cookies)
        self.assertEqual(self.gate.deadline, deadline)
        with self.assertRaisesRegex(StopRun, 'run_window_finished'):
            await self.gate.call('search', self.adapter.sync_cookies)

    async def test_partial_comments_continue_across_midnight_without_repeated_detail(self):
        self.seed(['long'])
        self.clock.timestamp = self.gate.midnight - 180
        self.reset_session()
        with self.assertRaisesRegex(StopRun, 'candidates_exhausted'):
            await self.engine.run()
        calls = [c for c in self.adapter.calls if c[1] == 'comments']
        self.assertEqual([c[3] for c in calls], ['', '20', '40'])
        self.assertFalse(any(c[1] == 'detail' for c in self.adapter.calls))
        self.assertEqual(self.store.complete_count(), 1)
        self.assertEqual(self.store.status(self.gate.day)['comments_saved'], 60)

    async def test_default_run_exceeds_old_limits_and_stops_for_pilot_review(self):
        start = self.clock.now()
        with self.assertRaisesRegex(StopRun, 'pilot_complete_review_required'):
            await self.engine.run()
        self.assertEqual(self.store.complete_count(), 50)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM comments').fetchone()[0], 3000)
        self.assertGreater(self.clock.now() - start, 4 * 3600)
        self.assertGreater(len([c for c in self.adapter.calls if c[1] == 'search']), 1)
        before = len(self.adapter.calls)
        self.reset_session()
        with self.assertRaisesRegex(StopRun, 'pilot_complete_review_required'):
            await self.engine.run()
        self.assertEqual(len(self.adapter.calls), before)

    async def test_search_pages_resume_and_exhausted_keywords_do_not_restart(self):
        self.config.keywords = ['甲', '乙']
        self.config.daily_requests_pilot = 4
        jobs = []

        async def search(keyword, page, search_id, sort):
            jobs.append((keyword, page, search_id))
            return {'items': [], 'has_more': page < 2}

        self.adapter.search = search
        with self.assertRaisesRegex(StopRun, 'daily_request_limit'):
            await self.engine.run()
        self.assertEqual([(k, p) for k, p, _ in jobs], [('甲', 1), ('乙', 1), ('甲', 2)])
        self.clock.timestamp = self.gate.midnight
        self.reset_session()
        with self.assertRaisesRegex(StopRun, 'candidates_exhausted'):
            await self.engine.run()
        self.assertEqual([(k, p) for k, p, _ in jobs], [('甲', 1), ('乙', 1), ('甲', 2), ('乙', 2)])
        self.assertEqual(jobs[1][2], jobs[3][2])
        self.reset_session()
        with self.assertRaisesRegex(StopRun, 'candidates_exhausted'):
            await self.engine.run()
        self.assertEqual(len(jobs), 4)

    async def test_more_than_twenty_comment_pages_and_resume(self):
        self.seed(['long'])
        cursors = []

        async def comments(note_id, token, cursor):
            page = int(cursor or 0)
            cursors.append(page)
            return {'comments': [{'id': 'c%d' % n, 'content': str(n)}
                                 for n in range(page * 3, page * 3 + 3)],
                    'cursor': str(page + 1), 'has_more': page < 20}

        self.adapter.comments = comments
        self.config.daily_requests_pilot = 12
        with self.assertRaisesRegex(StopRun, 'daily_request_limit'):
            await self.engine.run()
        self.assertEqual(cursors, list(range(11)))
        self.store.close()
        self.store = Store(self.config.state_dir)
        self.clock.timestamp = self.gate.midnight
        self.reset_session()
        with self.assertRaisesRegex(StopRun, 'candidates_exhausted'):
            await self.engine.run()
        self.assertEqual(cursors, list(range(21)))
        row = self.store.db.execute('SELECT * FROM notes').fetchone()
        self.assertEqual((row['state'], row['reason'], row['pages']), ('complete', 'exhausted', 21))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM comments').fetchone()[0], 63)

    def test_migration_restarts_truncated_pages_without_losing_data_or_review(self):
        self.seed(['cap', 'pagecap', 'ended', 'risk'])
        limited = dataclasses.replace(self.config, comments_per_note=2)
        page = [{'id': 'c%d' % i, 'content': str(i)} for i in range(5)]
        self.store.save_comments_page('cap', '', 'next', page, True, self.clock.now(), limited)
        self.store.mark_note('pagecap', 'needs_review', 'comment_page_limit', self.clock.now())
        self.store.mark_note('ended', 'complete', 'exhausted', self.clock.now())
        self.store.mark_note('risk', 'needs_review', 'repeated_cursor', self.clock.now())
        self.store.reserve_request(self.gate.day, self.clock.now(), 'comments', 'cap', self.config, self.clock.now() + 120)
        self.store.pause(self.clock.now(), 'test_pause', self.clock.now() + 900)
        self.config.stage = 'formal'
        self.config.pilot_notes = 2
        self.config.reviewed_notes = 10
        self.assertEqual(self.store.prepare_unlimited_comments(limited, self.clock.now()), 0)
        self.assertEqual(self.store.prepare_unlimited_comments(self.config, self.clock.now()), 2)
        self.assertEqual(self.store.count_requests(self.gate.day), 1)
        self.assertEqual(self.store.get('pause')['reason'], 'test_pause')
        self.assertTrue(self.store.get('pilot_review_accepted'))
        self.assertEqual(self.store.db.execute("SELECT state FROM notes WHERE note_id='risk'").fetchone()[0], 'needs_review')
        self.assertEqual(self.store.db.execute("SELECT state FROM notes WHERE note_id='ended'").fetchone()[0], 'complete')
        self.store.save_comments_page('cap', '', 'next', page, True, self.clock.now(), self.config)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM comments WHERE note_id='cap'").fetchone()[0], 5)
        self.assertEqual(self.store.prepare_unlimited_comments(self.config, self.clock.now()), 0)
        self.assertEqual(self.store.db.execute("SELECT cursor FROM notes WHERE note_id='cap'").fetchone()[0], 'next')
        self.clock.timestamp += 901
        self.store.resume(self.clock.now())
        self.engine.preflight()  # Backfill doesn't revoke an already earned review.

    def test_unknown_cursor_and_empty_page_still_stop_in_unlimited_mode(self):
        self.seed(['repeat', 'empty'])
        item = [{'id': 'one', 'content': 'one'}]
        self.store.save_comments_page('repeat', '', 'x', item, True, self.clock.now(), self.config)
        self.store.save_comments_page('repeat', 'x', 'x', item, True, self.clock.now(), self.config)
        self.store.save_comments_page('empty', '', 'x', [], True, self.clock.now(), self.config)
        self.assertEqual(self.store.db.execute("SELECT reason FROM notes WHERE note_id='repeat'").fetchone()[0], 'repeated_cursor')
        self.assertEqual(self.store.db.execute("SELECT reason FROM notes WHERE note_id='empty'").fetchone()[0], 'empty_page_with_more')

    def test_sorting_numeric_time_missing_ties_and_export_consistency(self):
        self.seed(['n'])
        rows = [
            {'id': 'a', 'content': 'a', 'like_count': '9', 'time': '1700000002'},
            {'id': 'b', 'content': 'b', 'like_count': '100', 'time': '1700000000000'},
            {'id': 'c', 'content': 'c', 'like_count': '1.2万', 'time': '2023-11-14T22:13:21Z'},
            {'id': 'd', 'content': 'd', 'like_count': '100', 'time': '1700000000'},
            {'id': 'e', 'content': 'e', 'like_count': 'unknown', 'time': 'unknown'},
            {'id': 'f', 'content': 'f', 'like_count': '0', 'time': '2023-11-15T06:13:21'},
        ]
        self.store.save_comments_page('n', '', '', rows, False, self.clock.now(), self.config)
        expected = {'likes_desc': ['c', 'b', 'd', 'a', 'f', 'e'],
                    'time_asc': ['b', 'd', 'c', 'f', 'a', 'e'],
                    'time_desc': ['a', 'c', 'f', 'b', 'd', 'e']}
        for order, ids in expected.items():
            self.config.comment_sort = order
            self.assertEqual([r[0] for r in sheets(self.store, order)['comments'][1:]], ids)
            directory = export_data(self.store, self.config, self.clock.now())
            with (directory / 'comments.csv').open(encoding='utf-8-sig', newline='') as stream:
                csv_rows = list(csv.reader(stream))
            with zipfile.ZipFile(directory / 'research.xlsx') as archive:
                root = ElementTree.fromstring(archive.read('xl/worksheets/sheet2.xml'))
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            xlsx_rows = [[cell.find('s:is/s:t', ns).text or '' for cell in row]
                         for row in root.find('s:sheetData', ns)]
            self.assertEqual(csv_rows, xlsx_rows)
            self.assertEqual([r[0] for r in csv_rows[1:]], ids)
            manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['comment_sort'], order)
            self.assertEqual(manifest['comment_status'][0]['interface_exhausted'], 1)
            self.clock.timestamp += 1

    def test_config_null_limits_and_legacy_integers(self):
        config_path = Path(self.temp.name) / 'config.json'
        config_path.write_text('{}', encoding='utf-8')
        current = Config.load(config_path)
        self.assertEqual(current.daily_requests, 700)
        self.assertIsNone(current.comments_per_note)
        for value in [None, 5]:
            config_path.write_text(json.dumps({'notes_per_run': value, 'max_comment_pages': value}), encoding='utf-8')
            self.assertEqual(Config.load(config_path).notes_per_run, value)
        for data in [{'notes_per_run': 0}, {'comments_per_note': True}, {'daily_requests_pilot': None},
                     {'comment_sort': 'unknown'}, {'run_timeout_seconds': -1}]:
            config_path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(SetupError):
                Config.load(config_path)

    def test_stop_reasons_are_actionable(self):
        self.assertIn('次日', stop_message('daily_request_limit'))
        self.assertIn('次日', stop_message('day_finished'))
        self.assertIn('promote', stop_message('pilot_complete_review_required'))
        self.assertIn('target_notes', stop_message('target_reached'))
        self.assertIn('resume', stop_message('paused:rate_limited'))


if __name__ == '__main__':
    unittest.main()
