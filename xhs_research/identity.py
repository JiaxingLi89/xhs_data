"""Identity enrichment with independent, durable cursors and no profile requests."""
import json

from .errors import ProtocolError, StaleLink, Unavailable
from .validation import comments_page, note_detail


IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_jobs (
 note_id TEXT PRIMARY KEY REFERENCES notes(note_id),
 state TEXT NOT NULL DEFAULT 'pending', phase TEXT NOT NULL DEFAULT 'detail',
 cursor TEXT NOT NULL DEFAULT '', pages INTEGER NOT NULL DEFAULT 0,
 reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, finished_at REAL);
CREATE TABLE IF NOT EXISTS identity_cursors (
 note_id TEXT NOT NULL, cursor TEXT NOT NULL, PRIMARY KEY(note_id,cursor));
"""


class IdentityStoreMixin:
    def prepare_identity_backfill(self, now):
        """Only called by collection while holding the worker lock."""
        with self.transaction():
            if self.get('identity_backfill_prepared', False):
                return 0
            added = 0
            for row in self.db.execute('SELECT note_id,detail FROM notes WHERE detail IS NOT NULL').fetchall():
                missing = self.db.execute(
                    'SELECT 1 FROM comments WHERE note_id=? AND identity_observed_at IS NULL LIMIT 1',
                    (row['note_id'],)).fetchone()
                if json.loads(row['detail']).get('identity_observed_at') is None or missing:
                    added += self.db.execute(
                        'INSERT OR IGNORE INTO identity_jobs(note_id,created_at) VALUES(?,?)',
                        (row['note_id'], now)).rowcount
            self.set('identity_backfill_prepared', True)
            self.event(now, 'identity_backfill_prepared:%d' % added)
            return added

    def next_identity_job(self):
        return self.db.execute(
            "SELECT j.*,n.token,n.source FROM identity_jobs j JOIN notes n USING(note_id) "
            "WHERE j.state IN ('pending','running') ORDER BY j.created_at,j.rowid LIMIT 1").fetchone()

    def save_identity_detail(self, note_id, detail, now):
        with self.transaction():
            old = json.loads(self.db.execute('SELECT detail FROM notes WHERE note_id=?', (note_id,)).fetchone()[0])
            changed = False
            for key in ('author_user_id', 'author_ip_location'):
                if not old.get(key) and detail.get(key):
                    old[key] = detail[key]
                    changed = True
            if changed or old.get('identity_observed_at') is None:
                old['identity_observed_at'] = now
            self.db.execute('UPDATE notes SET detail=? WHERE note_id=?',
                            (json.dumps(old, ensure_ascii=False), note_id))
            self.db.execute("UPDATE identity_jobs SET state='running',phase='comments' WHERE note_id=?", (note_id,))
            if not self.db.execute('SELECT 1 FROM comments WHERE note_id=? AND identity_observed_at IS NULL LIMIT 1', (note_id,)).fetchone():
                self.finish_identity_job(note_id, now, 'existing_comments_observed', 'complete')

    def upsert_comment(self, note_id, item, now):
        old = self.db.execute('SELECT * FROM comments WHERE comment_id=?', (item['id'],)).fetchone()
        if old:
            if old['note_id'] != note_id:
                raise ProtocolError('comment_belongs_to_another_note')
            uid = old['commenter_user_id'] or item.get('commenter_user_id')
            location = old['commenter_ip_location'] or item.get('commenter_ip_location')
            changed = uid != old['commenter_user_id'] or location != old['commenter_ip_location']
            observed = now if changed or old['identity_observed_at'] is None else old['identity_observed_at']
            self.db.execute('UPDATE comments SET commenter_user_id=?,commenter_ip_location=?,identity_observed_at=? '
                            'WHERE comment_id=?', (uid, location, observed, item['id']))
            return 0
        self.db.execute('INSERT INTO comments '
                        '(comment_id,note_id,content,published_at,like_count,fetched_at,'
                        'commenter_user_id,commenter_ip_location,identity_observed_at) VALUES(?,?,?,?,?,?,?,?,?)',
                        (item['id'], note_id, item['content'], str(item.get('time', '')),
                         str(item.get('like_count', '')), now, item.get('commenter_user_id'),
                         item.get('commenter_ip_location'), now))
        return 1

    def finish_identity_job(self, note_id, now, reason, state='error'):
        self.db.execute('UPDATE identity_jobs SET state=?,reason=?,finished_at=? WHERE note_id=?',
                        (state, reason, now, note_id))

    def save_identity_comments(self, note_id, requested_cursor, cursor, comments, more, now, max_pages=20):
        with self.transaction():
            for item in comments:
                if self.db.execute('SELECT 1 FROM comments WHERE comment_id=? AND note_id=?',
                                   (item['id'], note_id)).fetchone():
                    self.upsert_comment(note_id, item, now)
            self.db.execute('INSERT OR IGNORE INTO identity_cursors VALUES(?,?)', (note_id, requested_cursor))
            self.db.execute("UPDATE identity_jobs SET state='running',cursor=?,pages=pages+1 WHERE note_id=?",
                            (cursor, note_id))
            remaining = self.db.execute('SELECT 1 FROM comments WHERE note_id=? AND identity_observed_at IS NULL LIMIT 1', (note_id,)).fetchone()
            pages = self.db.execute('SELECT pages FROM identity_jobs WHERE note_id=?', (note_id,)).fetchone()[0]
            if not remaining:
                self.finish_identity_job(note_id, now, 'existing_comments_observed', 'complete')
            elif not more:
                self.finish_identity_job(note_id, now, 'exhausted', 'complete')
            elif not cursor or self.db.execute('SELECT 1 FROM identity_cursors WHERE note_id=? AND cursor=?',
                                               (note_id, cursor)).fetchone():
                self.finish_identity_job(note_id, now, 'repeated_cursor')
            elif pages >= max_pages:
                self.finish_identity_job(note_id, now, 'comment_page_limit')
            elif not comments:
                self.finish_identity_job(note_id, now, 'empty_page_with_more')

    def identity_status(self):
        jobs = dict(self.db.execute('SELECT state,count(*) FROM identity_jobs GROUP BY state'))
        missing = dict(author_user_id=0, author_ip_location=0)
        for row in self.db.execute('SELECT detail FROM notes WHERE detail IS NOT NULL'):
            detail = json.loads(row[0])
            for key in missing:
                missing[key] += not bool(detail.get(key))
        for key in ('commenter_user_id', 'commenter_ip_location'):
            missing[key] = self.db.execute('SELECT count(*) FROM comments WHERE ' + key + " IS NULL OR " + key + "=''").fetchone()[0]
        return {'jobs': {state: jobs.get(state, 0) for state in ('pending', 'running', 'complete', 'error')},
                'missing_fields': missing}


async def backfill_step(store, session, clock, progress=print, validator=None):
    job = store.next_identity_job()
    if job is None:
        return False
    note_id = job['note_id']
    validate = validator or (lambda kind, fn, *args: fn(*args))
    try:
        if job['phase'] == 'detail':
            raw = await session.call('detail', lambda: session.adapter.detail(note_id, job['token'], job['source']),
                                     note_id, 'identity:detail')
            store.save_identity_detail(note_id, validate("detail", note_detail, raw, note_id), clock.now())
        else:
            raw = await session.call('comments', lambda: session.adapter.comments(note_id, job['token'], job['cursor']),
                                     note_id, 'identity:' + job['cursor'])
            comments, cursor, more = validate("comments", comments_page, raw, note_id)
            store.save_identity_comments(note_id, job['cursor'], cursor, comments, more, clock.now(), session.config.max_comment_pages)
        progress('用户 ID 与 IP 属地补采：' + json.dumps(store.identity_status(), ensure_ascii=False))
    except Unavailable:
        store.finish_identity_job(note_id, clock.now(), 'note_unavailable')
    except StaleLink:
        store.finish_identity_job(note_id, clock.now(), 'link_expired')
    return True
