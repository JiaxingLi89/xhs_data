import csv
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from xhs_research.config import Config
from xhs_research.control import RequestGate, SessionManager
from xhs_research.demo import DemoAdapter, FakeClock
from xhs_research.engine import Engine
from xhs_research.errors import ProtocolError, StopRun, Unavailable
from xhs_research.export import export_data
from xhs_research.identity import backfill_step
from xhs_research.runtime import process_lock
from xhs_research.store import SCHEMA, Store
from xhs_research.validation import comments_page, note_detail


def legacy_database(directory):
    db = sqlite3.connect(str(Path(directory) / 'research.sqlite3'))
    db.executescript(SCHEMA)
    db.execute('INSERT INTO meta VALUES(?,?)', ('schema_version', '1'))
    db.execute('INSERT INTO meta VALUES(?,?)', ('next_request_at', '120'))
    db.execute("INSERT INTO requests(day,at,kind,outcome) VALUES('2026-11-13',1,'detail','ok')")
    db.execute("INSERT INTO notes(note_id,state,token,source,cursor,detail,found_at,fetched_at,finished_at,pages) "
               "VALUES('n','complete','TEST','pc_search','old',?,1,2,3,4)", (json.dumps({'title': 'original', 'text': 'original'}),))
    db.execute("INSERT INTO comments VALUES('nc000','n','original comment','1','9',2)")
    db.commit()
    db.close()


class MigrationTests(unittest.TestCase):
    def test_backup_migration_repeat_and_state_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_database(directory)
            store = Store(directory)
            try:
                self.assertEqual(store.get('schema_version'), 3)
                self.assertEqual(store.get('next_request_at'), 120)
                self.assertEqual(store.count_requests('2026-11-13'), 1)
                self.assertEqual(store.complete_count(), 1)
                self.assertEqual(store.db.execute('SELECT cursor FROM notes').fetchone()[0], 'old')
                self.assertIsNone(store.db.execute('SELECT commenter_user_id FROM comments').fetchone()[0])
                self.assertEqual(store.identity_status()['jobs']['pending'], 0)
            finally:
                store.close()
            backups = list(Path(directory).glob('research.pre-identity-*.sqlite3'))
            self.assertEqual(len(backups), 1)
            db = sqlite3.connect(str(backups[0]))
            try:
                self.assertNotIn('commenter_user_id', [r[1] for r in db.execute('PRAGMA table_info(comments)')])
                self.assertEqual(db.execute('SELECT content FROM comments').fetchone()[0], 'original comment')
            finally:
                db.close()
            Store(directory).close()
            self.assertEqual(len(list(Path(directory).glob('research.pre-identity-*.sqlite3'))), 1)

    def test_migration_refuses_active_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_database(directory)
            with process_lock(Path(directory)):
                with self.assertRaises(StopRun):
                    Store(directory)
                store = Store(directory, migration_locked=True)
                store.close()

    def test_migration_rolls_back_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_database(directory)
            with patch.object(Store, 'set', side_effect=RuntimeError('disk failure')):
                with self.assertRaises(RuntimeError):
                    Store(directory)
            db = sqlite3.connect(str(Path(directory) / 'research.sqlite3'))
            try:
                self.assertEqual(db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '1')
                self.assertNotIn('commenter_user_id', [r[1] for r in db.execute('PRAGMA table_info(comments)')])
            finally:
                db.close()
            Store(directory).close()


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        legacy_database(self.temp.name)
        self.config = Config(root=Path(self.temp.name), data_dir='.', request_interval_seconds=[120, 120])
        self.store = Store(self.temp.name)
        self.clock = FakeClock()
        self.adapter = DemoAdapter(self.clock)
        self.reset_session()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reset_session(self):
        gate = RequestGate(self.store, self.config, self.clock, lambda *_: None)
        self.session = SessionManager(self.adapter, gate, self.store, self.config, self.clock, lambda *_: None)
        self.engine = Engine(self.config, self.store, self.session, self.clock, lambda *_: None)

    async def step(self):
        return await backfill_step(self.store, self.session, self.clock, lambda *_: None)

    async def test_optional_identity_fields_are_separate_and_tolerate_bad_types(self):
        detail = await self.adapter.detail('n', '', '')
        detail['items'][0]['note_card']['user']['ip_location'] = 'must not use profile value'
        parsed = note_detail(detail, 'n')
        self.assertEqual(parsed['author_user_id'], 'DEMO_AUTHOR')
        self.assertEqual(parsed['author_ip_location'], '浙江')
        raw = await self.adapter.comments('n', '', '')
        parsed_comments, _, _ = comments_page(raw, 'n')
        self.assertEqual(parsed_comments[0]['commenter_ip_location'], '江苏')
        for value in (None, 123, [], {}, ''):
            detail['items'][0]['note_card'].update(user=value, ip_location=value)
            parsed = note_detail(detail, 'n')
            self.assertIsNone(parsed['author_user_id'])
            self.assertIsNone(parsed['author_ip_location'])
            raw['comments'][0].update(user_info=value, ip_location=value)
            parsed_comments, _, _ = comments_page(raw, 'n')
            self.assertIsNone(parsed_comments[0]['commenter_user_id'])
            self.assertIsNone(parsed_comments[0]['commenter_ip_location'])

    async def test_backfill_restart_midnight_dedup_and_original_progress(self):
        self.assertEqual(self.store.prepare_identity_backfill(self.clock.now()), 1)
        original = dict(self.store.db.execute('SELECT * FROM notes').fetchone())
        await self.step()  # detail
        await self.step()  # first page
        self.store.close()
        self.store = Store(self.temp.name)
        self.clock.timestamp = self.session.gate.midnight
        self.reset_session()
        self.assertEqual(self.store.prepare_identity_backfill(self.clock.now()), 0)
        while await self.step():
            pass
        self.assertEqual([c[3] for c in self.adapter.calls if c[1] == 'comments'], ['', '20', '40'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM comments').fetchone()[0], 60)
        note = dict(self.store.db.execute('SELECT * FROM notes').fetchone())
        for key in ('cursor', 'state', 'pages', 'fetched_at', 'finished_at'):
            self.assertEqual(note[key], original[key])
        detail = json.loads(note['detail'])
        self.assertEqual(detail['title'], 'original')
        self.assertEqual(detail['author_user_id'], 'DEMO_AUTHOR')
        old = self.store.db.execute("SELECT * FROM comments WHERE comment_id='nc000'").fetchone()
        self.assertEqual((old['content'], old['like_count'], old['fetched_at']), ('original comment', '9', 2))
        self.assertEqual(old['commenter_user_id'], 'DEMO_COMMENTER')
        self.assertEqual(self.store.identity_status()['jobs']['complete'], 1)
        self.assertEqual(self.store.count_requests(self.session.gate.day), 2)

    async def test_backfill_allowed_at_pilot_limit_but_new_notes_still_blocked(self):
        self.config.pilot_notes = 1
        with self.assertRaisesRegex(StopRun, 'pilot_complete_review_required'):
            await self.engine.run()
        self.assertEqual(self.store.identity_status()['jobs']['complete'], 1)
        self.assertFalse(any(c[1] == 'search' for c in self.adapter.calls))
        self.assertEqual(self.store.complete_count(), 1)

    async def test_quota_interrupts_backfill_without_losing_cursor(self):
        self.config.daily_requests_pilot = 4  # existing request + auth + detail + first page
        with self.assertRaisesRegex(StopRun, 'daily_request_limit'):
            await self.engine.run()
        self.assertEqual(self.store.next_identity_job()['cursor'], '20')
        self.assertEqual(self.store.count_requests(self.session.gate.day), 4)

    async def test_missing_fields_end_once_and_preserve_nonempty_values(self):
        self.store.prepare_identity_backfill(self.clock.now())
        self.store.save_identity_detail('n', {'author_user_id': 'first', 'author_ip_location': '上海'}, 10)
        self.store.save_identity_detail('n', {'author_user_id': None, 'author_ip_location': None}, 20)
        self.store.save_identity_comments('n', '', '', [{'id': 'nc000', 'content': 'changed'}], False, 20)
        self.assertIsNone(self.store.next_identity_job())
        self.assertEqual(self.store.prepare_identity_backfill(30), 0)
        detail = json.loads(self.store.db.execute('SELECT detail FROM notes').fetchone()[0])
        self.assertEqual((detail['author_user_id'], detail['author_ip_location'], detail['identity_observed_at']), ('first', '上海', 10))
        self.assertEqual(self.store.identity_status()['missing_fields']['commenter_user_id'], 1)

    async def test_repeated_cursor_unavailable_and_cross_note_errors(self):
        self.store.prepare_identity_backfill(self.clock.now())
        self.store.save_identity_comments('n', '', 'next', [], True, 10)
        self.assertEqual(self.store.db.execute('SELECT reason FROM identity_jobs').fetchone()[0], 'empty_page_with_more')
        self.store.save_identity_comments('n', 'next', 'next', [{'id': 'nc000', 'content': 'x'}], True, 20)
        self.assertEqual(self.store.db.execute('SELECT reason FROM identity_jobs').fetchone()[0], 'repeated_cursor')
        with self.assertRaises(ProtocolError):
            self.store.upsert_comment('different', {'id': 'nc000', 'content': 'x'}, 30)
        self.store.db.execute("UPDATE identity_jobs SET state='pending'")
        self.adapter.failures['detail'] = [Unavailable('removed')]
        await self.step()
        self.assertEqual(self.store.db.execute('SELECT reason FROM identity_jobs').fetchone()[0], 'note_unavailable')
        self.assertEqual(self.store.complete_count(), 1)

    async def test_exports_include_text_ids_and_locations_but_no_credentials(self):
        self.store.prepare_identity_backfill(self.clock.now())
        while await self.step():
            pass
        long_id = '0012345678901234567890'
        self.store.db.execute('UPDATE comments SET commenter_user_id=?', (long_id,))
        directory = export_data(self.store, self.config, self.clock.now())
        with (directory / 'comments.csv').open(encoding='utf-8-sig', newline='') as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row['commenter_user_id'], long_id)
        self.assertEqual(row['commenter_ip_location'], '江苏')
        with zipfile.ZipFile(directory / 'research.xlsx') as archive:
            xml = archive.read('xl/worksheets/sheet2.xml').decode()
            self.assertIn(long_id, xml)
            self.assertIn('t="inlineStr"', xml)
            self.assertNotIn('DEMO_TOKEN', xml)
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['identity_backfill']['jobs']['complete'], 1)
