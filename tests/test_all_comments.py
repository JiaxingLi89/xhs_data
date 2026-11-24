import contextlib
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_collector as helpers
from xhs_research import cli
from xhs_research.config import Config
from xhs_research.errors import StopRun, StaleLink, Unavailable
from xhs_research.export import export_data
from xhs_research.validation import comments_page, note_detail


class AllCommentsTests(unittest.IsolatedAsyncioTestCase):
    setUp = helpers.CollectorTests.setUp
    tearDown = helpers.CollectorTests.tearDown
    reset_session = helpers.CollectorTests.reset_session
    seed = helpers.CollectorTests.seed

    async def test_batch_summary_counts_completions_after_reopening_and_on_restart(self):
        self.seed(4)
        for i in range(4):
            self.store.save_detail('n%d' % i, {'title': 'test'}, self.clock.now())
            self.store.mark_note('n%d' % i, 'complete', 'comment_cap' if i < 3 else 'exhausted', self.clock.now())
        for completed_id, expected_current in [('n0', 2), ('n1', 3)]:
            async def finish_one(**kwargs):
                self.store.mark_note(completed_id, 'complete', 'exhausted', self.clock.now())
                raise StopRun('run_window_finished')
            output = io.StringIO()
            with patch.object(cli, 'Clock', return_value=self.clock), \
                    patch.object(cli, 'MediaCrawlerAdapter', return_value=self.adapter), \
                    patch.object(cli.Engine, 'run', side_effect=finish_one), \
                    contextlib.redirect_stdout(output):
                await cli.collect(self.config, self.store, 'run')
            text = output.getvalue()
            self.assertIn('本批完成 1 篇（含旧帖补采完成）', text)
            self.assertIn('当前完整完成 %d 篇' % expected_current, text)
            self.assertIn('原因：本次运行时间已结束', text)
            if completed_id == 'n0':
                self.assertIn('原已完成的 3 篇旧帖已转入评论补采', text)
            else:
                self.assertNotIn('旧帖已转入评论补采', text)

    async def test_batch_summary_zero_when_backfill_interrupted_before_completion(self):
        await self.capped_note()
        self.config.requests_per_run = 1
        output = io.StringIO()
        with patch.object(cli, 'Clock', return_value=self.clock), \
                patch.object(cli, 'MediaCrawlerAdapter', return_value=self.adapter), \
                contextlib.redirect_stdout(output):
            await cli.collect(self.config, self.store, 'run')
        self.assertIn('本批完成 0 篇（含旧帖补采完成）', output.getvalue())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM comments').fetchone()[0], 50)

    async def capped_note(self):
        self.seed(1)
        self.store.save_detail('n0', note_detail(await self.adapter.detail('n0', '', ''), 'n0'), self.clock.now())
        self.config.comments_per_note = 50
        for offset in ('', '20', '40'):
            comments, cursor, more = comments_page(await self.adapter.comments('n0', '', offset), 'n0')
            self.store.save_comments_page('n0', offset, cursor, comments, more, self.clock.now(), self.config)
        self.config.comments_per_note = None
        self.assertEqual(self.store.db.execute("SELECT reason FROM notes").fetchone()[0], 'comment_cap')

    async def test_backfill_at_target_recovers_truncated_final_page_without_discovery(self):
        await self.capped_note()
        self.config.pilot_notes = 1
        self.adapter.calls.clear()
        with self.assertRaisesRegex(StopRun, 'pilot_complete_review_required'):
            await self.engine.run()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM comments').fetchone()[0], 60)
        self.assertEqual(self.store.db.execute('SELECT reason FROM notes').fetchone()[0], 'exhausted')
        self.assertFalse(any(c[1] in ('search', 'detail') for c in self.adapter.calls))
        self.assertEqual([c[3] for c in self.adapter.calls if c[1] == 'comments'], ['', '20', '40'])
        backups = list(self.config.state_dir.glob('research.pre-all-comments-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(backup.execute('SELECT count(*) FROM comments').fetchone()[0], 50)
        manifest = json.loads((export_data(self.store, self.config, self.clock.now()) / 'manifest.json').read_text())
        self.assertIsNone(manifest['comments_per_note'])
        self.assertEqual(manifest['comment_status'][0]['interface_exhausted'], 1)

    async def test_restart_resumes_without_reset_and_honors_budget(self):
        await self.capped_note()
        self.config.pilot_notes = 1
        self.config.requests_per_run = 2
        with self.assertRaisesRegex(StopRun, 'per_run_request_limit'):
            await self.engine.run()
        self.assertEqual(self.store.db.execute('SELECT cursor FROM notes').fetchone()[0], '20')
        self.reset_session()
        self.config.requests_per_run = 10
        self.adapter.calls.clear()
        with self.assertRaisesRegex(StopRun, 'pilot_complete_review_required'):
            await self.engine.run()
        self.assertEqual(next(c[3] for c in self.adapter.calls if c[1] == 'comments'), '20')
        self.assertEqual(len(list(self.config.state_dir.glob('research.pre-all-comments-*'))), 1)

    async def test_failed_backfill_does_not_replace_sample_at_formal_target(self):
        await self.capped_note()
        self.config.stage = 'formal'
        self.config.pilot_notes = self.config.target_notes = 1
        self.config.reviewed_notes = 10
        self.adapter.calls.clear()
        self.adapter.failures['comments'] = [Unavailable('deleted')]
        with self.assertRaisesRegex(StopRun, 'target_reached'):
            await self.engine.run()
        self.assertEqual(self.store.complete_count(), 0)
        self.assertEqual(self.store.collection_count(), 1)
        self.assertFalse(any(c[1] == 'search' for c in self.adapter.calls))

    async def test_stale_backfill_refresh_does_not_insert_new_candidates(self):
        await self.capped_note()
        self.seed(2)
        self.config.pilot_notes = 1
        self.adapter.failures['comments'] = [StaleLink('expired')]
        with self.assertRaisesRegex(StopRun, 'pilot_complete_review_required'):
            await self.engine.run()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM notes').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT reason FROM notes WHERE note_id='n0'").fetchone()[0], 'link_refresh_not_found')
        self.assertFalse(any(c[1] == 'detail' and c[2] == 'n1' for c in self.adapter.calls))

    async def test_backfill_preserves_pause_and_daily_note_limit(self):
        await self.capped_note()
        self.adapter.calls.clear()
        self.store.pause(self.clock.now(), 'test')
        with self.assertRaises(StopRun):
            await self.engine.run()
        self.assertEqual(self.adapter.calls, [])
        self.store.resume(self.clock.now())
        self.config.daily_notes_pilot = 1
        self.store.db.execute("INSERT INTO daily_notes(day,note_id) VALUES(?, 'other')", (self.gate.day,))
        with self.assertRaisesRegex(StopRun, 'daily_note_limit'):
            await self.engine.run()
        self.assertEqual(self.adapter.calls, [])

    async def test_more_than_twenty_pages_and_fifty_comments(self):
        self.seed(1)
        for page in range(25):
            rows = [{'id': 'c%d' % n, 'content': 'text'} for n in range(page * 20, (page + 1) * 20)]
            self.store.save_comments_page('n0', str(page), str(page + 1), rows, page < 24, self.clock.now(), self.config)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM comments').fetchone()[0], 500)
        self.assertEqual(self.store.db.execute('SELECT reason FROM notes').fetchone()[0], 'exhausted')

    async def test_page_limit_upgrade_only_reopens_cap_stops(self):
        self.seed(2)
        self.config.max_comment_pages = 1
        self.store.save_comments_page('n0', '', '20', [{'id': 'c', 'content': 'text'}], True, self.clock.now(), self.config)
        self.store.mark_note('n1', 'needs_review', 'repeated_cursor', self.clock.now())
        self.config.max_comment_pages = None
        self.store.prepare_comment_backfill(self.clock.now(), self.config)
        self.assertEqual(self.store.next_comment_backfill()['note_id'], 'n0')
        self.assertEqual(self.store.db.execute("SELECT state FROM notes WHERE note_id='n1'").fetchone()[0], 'needs_review')

    async def test_empty_pages_and_identity_unlimited(self):
        self.seed(2)
        self.store.save_comments_page('n0', '', '', [], False, self.clock.now(), self.config)
        self.store.save_comments_page('n1', '', '20', [], True, self.clock.now(), self.config)
        self.assertEqual(self.store.db.execute("SELECT reason FROM notes WHERE note_id='n0'").fetchone()[0], 'exhausted')
        self.assertEqual(self.store.db.execute("SELECT reason FROM notes WHERE note_id='n1'").fetchone()[0], 'empty_page_with_more')
        self.store.db.execute("INSERT INTO identity_jobs(note_id,created_at) VALUES('n0',0)")
        self.store.db.execute("INSERT INTO comments(comment_id,note_id,content,fetched_at) VALUES('missing','n0','text',0)")
        self.store.save_identity_comments('n0', '', 'next', [], True, self.clock.now(), None)
        self.assertEqual(self.store.db.execute('SELECT reason FROM identity_jobs').fetchone()[0], 'empty_page_with_more')

    def test_nullable_config_and_strategy_version(self):
        config = Config()
        config.validate()
        version = config.sampling_version
        config.max_comment_pages = 20
        config.validate()
        self.assertNotEqual(version, config.sampling_version)
        for key in ('comments_per_note', 'max_comment_pages'):
            for bad in (0, -1, True, 'all', 1.5):
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    Config(**{key: bad}).validate()
