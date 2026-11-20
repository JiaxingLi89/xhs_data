import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xhs_research.config import Config
from xhs_research.control import RequestGate, SessionManager
from xhs_research.demo import DemoAdapter, FakeClock
from xhs_research.engine import Engine
from xhs_research.errors import StopRun, ProtocolError
from xhs_research.runtime import local_day, next_midnight, process_lock
from xhs_research.store import Store, SCHEMA
from xhs_research.validation import comments_page, note_detail


class MacFeatures(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name), daily_notes_pilot=None,
                             daily_notes_formal=None, request_interval_seconds=[120,120])
        self.store = Store(self.config.state_dir)
        self.clock = FakeClock()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def seed(self):
        self.store.register_keywords(self.config)
        job = self.store.next_keyword(self.config)
        self.store.add_search_page(job, [{'id':'old','token':'t','source':'pc_search'}], False, 1, 'v')
        self.store.save_detail('old', {'title':'old title','text':'old text','tags':[]}, 2)
        self.store.save_comments_page('old','','', [{'id':'c','content':'old comment'}],False,3,self.config)
        self.store.db.execute("UPDATE notes SET detail=? WHERE note_id='old'", (json.dumps({'title':'old title','text':'old text','tags':[]}),))
        self.store.db.execute('UPDATE comments SET identity_observed_at=NULL')
        self.store.prepare_identity_backfill(4)

    async def test_daily_300_includes_history_failures_and_next_day(self):
        day=local_day(self.clock.now())
        self.store.db.executemany('INSERT INTO requests(day,at,kind,outcome) VALUES(?,?,?,?)',
                                 [(day,self.clock.now(),'comments','network')]*299)
        gate=RequestGate(self.store,self.config,self.clock,lambda *_:None)
        adapter=DemoAdapter(self.clock)
        await gate.call('search',lambda:adapter.search('k',1,'s','general'))
        with self.assertRaisesRegex(StopRun,'daily_request_limit'):
            await gate.call('auth_check',adapter.check_auth)
        self.assertEqual(self.store.quota_status(self.clock.now(),self.config)['daily_requests_remaining'],0)
        self.clock.timestamp=next_midnight(self.clock.now(),'Asia/Shanghai')+1
        gate=RequestGate(self.store,self.config,self.clock,lambda *_:None)
        await gate.call('auth_check',adapter.check_auth)
        self.assertEqual(self.store.count_requests(local_day(self.clock.now())),1)

    async def test_daily_batch_limit_and_multiple_searches(self):
        gate=RequestGate(self.store,self.config,self.clock,lambda *_:None)
        adapter=DemoAdapter(self.clock)
        for page in range(30):
            await gate.call('search',lambda:adapter.search('k',page,'s','general'))
        with self.assertRaisesRegex(StopRun,'per_run_request_limit'):
            await gate.call('auth_check',adapter.check_auth)
        self.assertEqual(self.store.count_requests(local_day(self.clock.now())),30)

    async def test_engine_refills_empty_queue_in_same_batch(self):
        adapter=DemoAdapter(self.clock)
        async def search(keyword,page,search_id,sort):
            return {'items':[{'id':'page%d' % page,'xsec_token':'t'}], 'has_more':page<3}
        async def comments(note_id,token,cursor):
            return {'comments':[], 'cursor':'', 'has_more':False}
        adapter.search=search
        adapter.comments=comments
        gate=RequestGate(self.store,self.config,self.clock,lambda *_:None)
        session=SessionManager(adapter,gate,self.store,self.config,self.clock,lambda *_:None)
        engine=Engine(self.config,self.store,session,self.clock,lambda *_:None)
        with self.assertRaisesRegex(StopRun,'no_pending_candidates'):
            await engine.run()
        self.assertEqual(self.store.complete_count(),3)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests WHERE kind='search'").fetchone()[0],3)

    def test_identity_backfill_preserves_original_and_ignores_new_comments(self):
        self.seed()
        before=dict(self.store.db.execute("SELECT * FROM notes WHERE note_id='old'").fetchone())
        self.store.save_identity_detail('old',{'title':'changed','text':'changed','author_user_id':'001','author_ip_location':'浙江'},5)
        self.store.save_identity_comments('old','','next',[{'id':'new','content':'new'}, {'id':'c','content':'changed','commenter_user_id':'002','commenter_ip_location':'江苏'}],True,6)
        after=dict(self.store.db.execute("SELECT * FROM notes WHERE note_id='old'").fetchone())
        self.assertEqual({k:v for k,v in before.items() if k!='detail'}, {k:v for k,v in after.items() if k!='detail'})
        self.assertEqual(json.loads(after['detail'])['text'],'old text')
        self.assertEqual(json.loads(after['detail'])['author_user_id'],'001')
        rows=self.store.db.execute('SELECT * FROM comments').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['content'],'old comment')
        self.assertEqual(rows[0]['commenter_user_id'],'002')
        self.assertIsNone(self.store.next_identity_job())
        self.assertEqual(self.store.prepare_identity_backfill(10),0)

    def test_backfill_restart_page_limit_and_no_infinite_missing_field_retry(self):
        self.seed()
        self.store.save_identity_detail('old',{},5)
        self.store.save_identity_comments('old','','next',[{'id':'other','content':'new'}],True,6,max_pages=2)
        self.store.close(); self.store=Store(self.config.state_dir)
        self.assertEqual(self.store.next_identity_job()['cursor'],'next')
        self.store.save_identity_comments('old','next','last',[{'id':'other','content':'new'}],True,7,max_pages=2)
        self.assertIsNone(self.store.next_identity_job())
        self.assertEqual(self.store.db.execute('SELECT reason FROM identity_jobs').fetchone()[0],'comment_page_limit')

    async def test_backfill_at_pilot_target_uses_normal_batch_limit(self):
        self.seed(); self.config.pilot_notes=1; self.config.requests_per_run=2
        adapter=DemoAdapter(self.clock)
        gate=RequestGate(self.store,self.config,self.clock,lambda *_:None)
        session=SessionManager(adapter,gate,self.store,self.config,self.clock,lambda *_:None)
        engine=Engine(self.config,self.store,session,self.clock,lambda *_:None)
        with self.assertRaisesRegex(StopRun,'per_run_request_limit'):
            await engine.run()
        self.assertEqual(self.store.complete_count(),1)
        self.assertEqual(self.store.next_identity_job()['phase'],'comments')
        self.assertEqual(gate.run_requests,2)

    def test_identity_validation_optional_and_separate_fields(self):
        detail=note_detail({'items':[{'id':'n','note_card':{'title':'t','user':{'user_id':'001'},'ip_location':'浙江'}}]},'n')
        self.assertEqual(detail['author_user_id'],'001')
        comments,_,_=comments_page({'comments':[{'id':'c','content':'ok','user_info':{'user_id':12},'ip_location':None}], 'has_more':False},'n')
        self.assertIsNone(comments[0]['commenter_user_id'])
        self.assertIsNone(comments[0]['commenter_ip_location'])

    def test_v1_migration_backup_lock_and_preserved_data(self):
        directory=Path(self.temp.name)/'legacy'; directory.mkdir()
        path=directory/'research.sqlite3'; db=sqlite3.connect(path)
        db.executescript(SCHEMA)
        db.execute('INSERT INTO meta VALUES(?,?)',('schema_version','1'))
        db.execute("INSERT INTO notes(note_id,token,source,found_at,state) VALUES('n','t','s',1,'complete')")
        db.execute("INSERT INTO requests(day,at,kind,outcome) VALUES('2026-11-18',1,'search','ok')")
        db.commit(); db.close()
        with process_lock(directory):
            with self.assertRaises(StopRun):
                Store(directory)
        migrated=Store(directory)
        self.assertEqual(migrated.get('schema_version'),3)
        self.assertEqual(migrated.complete_count(),1)
        self.assertEqual(migrated.count_requests('2026-11-18'),1)
        self.assertEqual(migrated.identity_status()['jobs']['pending'],0)
        migrated.close()
        backups=list(directory.glob('research.pre-identity-*.sqlite3'))
        self.assertEqual(len(backups),1)
        backup=sqlite3.connect(backups[0]); self.assertEqual(backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],'1'); backup.close()
        migrated=Store(directory); migrated.close()
        self.assertEqual(len(list(directory.glob('research.pre-identity-*.sqlite3'))),1)
