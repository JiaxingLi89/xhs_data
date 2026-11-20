from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager, nullcontext
from uuid import uuid4
from .identity import IdentityStoreMixin, IDENTITY_SCHEMA
from .promotion import SCHEMA as PROMOTION_SCHEMA, safe_classify
from pathlib import Path

from .errors import ProtocolError, StopRun
from .mediacrawler_protocol import new_search_id
from .runtime import local_day, next_midnight, process_lock


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
 id INTEGER PRIMARY KEY, day TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
 note_id TEXT, outcome TEXT NOT NULL DEFAULT 'reserved');
CREATE INDEX IF NOT EXISTS request_day ON requests(day);
CREATE INDEX IF NOT EXISTS request_time ON requests(at);
CREATE TABLE IF NOT EXISTS daily_notes (day TEXT, note_id TEXT, PRIMARY KEY(day,note_id));
CREATE TABLE IF NOT EXISTS daily_search (day TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS slots (day TEXT, slot INTEGER, PRIMARY KEY(day,slot));
CREATE TABLE IF NOT EXISTS keywords (
 keyword TEXT, sort TEXT, page INTEGER NOT NULL DEFAULT 1, search_id TEXT NOT NULL,
 exhausted INTEGER NOT NULL DEFAULT 0, empty_pages INTEGER NOT NULL DEFAULT 0,
 last_used REAL NOT NULL DEFAULT 0, PRIMARY KEY(keyword,sort));
CREATE TABLE IF NOT EXISTS notes (
 note_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending', token TEXT NOT NULL,
 source TEXT NOT NULL, cursor TEXT NOT NULL DEFAULT '', detail TEXT,
 found_at REAL NOT NULL, fetched_at REAL, finished_at REAL, reason TEXT NOT NULL DEFAULT '',
 pages INTEGER NOT NULL DEFAULT 0, refresh_attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sources (
 note_id TEXT, keyword TEXT, sort TEXT, page INTEGER, rank INTEGER, discovered_at REAL,
 search_id TEXT, sampling_version TEXT,
 PRIMARY KEY(note_id,keyword,sort,page,discovered_at));
CREATE TABLE IF NOT EXISTS comments (
 comment_id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(note_id),
 content TEXT NOT NULL, published_at TEXT, like_count TEXT, fetched_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS comment_note ON comments(note_id);
CREATE TABLE IF NOT EXISTS cursors (note_id TEXT, cursor TEXT, PRIMARY KEY(note_id,cursor));
CREATE TABLE IF NOT EXISTS page_traffic (day TEXT PRIMARY KEY, requests INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL NOT NULL, code TEXT NOT NULL);
"""


class Store(IdentityStoreMixin):
    def __init__(self, directory, migration_locked=False):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.path = directory / "research.sqlite3"
        self.db = sqlite3.connect(str(self.path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        try:
            has_meta = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
            version = self.get('schema_version', 1) if has_meta else 0
            if version not in (0, 1, 2, 3):
                raise StopRun('unsupported_database_version')
            if version < 3:
                with nullcontext() if migration_locked else process_lock(directory):
                    # Recheck after acquiring the lock: another opener may have migrated.
                    has_meta = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
                    version = self.get('schema_version', 1) if has_meta else 0
                    if version not in (0, 1, 2, 3):
                        raise StopRun('unsupported_database_version')
                    if version < 3:
                        if version in (1, 2):
                            prefix = 'research.pre-identity-' if version == 1 else 'research.pre-promotion-'
                            backup_path = directory / (prefix + uuid4().hex + '.sqlite3')
                            backup = sqlite3.connect(str(backup_path))
                            try:
                                self.db.backup(backup)
                            finally:
                                backup.close()
                            os.chmod(backup_path, 0o600)
                        with self.transaction():
                            for statement in (SCHEMA + IDENTITY_SCHEMA + PROMOTION_SCHEMA).split(';'):
                                if statement.strip():
                                    self.db.execute(statement)
                            columns = {row[1] for row in self.db.execute('PRAGMA table_info(comments)')}
                            for name, kind in [('commenter_user_id', 'TEXT'), ('commenter_ip_location', 'TEXT'),
                                               ('identity_observed_at', 'REAL')]:
                                if name not in columns:
                                    self.db.execute('ALTER TABLE comments ADD COLUMN ' + name + ' ' + kind)
                            self.set('schema_version', 3)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))

    def event(self, now, code):
        self.db.execute("INSERT INTO events(at,code) VALUES(?,?)", (now, code))

    def pause(self, now, reason, until=0):
        previous = self.get("pause") or {}
        if previous.get("reason") == reason:
            if until > previous.get("until", 0):
                self.set("pause", {**previous, "until": until})
            return
        self.set("pause", {"reason": reason, "until": max(until, previous.get("until", 0)), "since": now})
        self.event(now, reason)

    def check_pause(self, now):
        pause = self.get("pause")
        if pause:
            raise StopRun("paused:" + pause["reason"])
        if now < self.get("network_until", 0):
            raise StopRun("network_backoff")

    def resume(self, now):
        pause = self.get("pause")
        if pause and now < pause.get("until", 0):
            raise StopRun("cooldown_not_finished")
        self.set("pause", None)
        self.set("network_until", 0)
        self.set("network_streak", 0)
        self.db.execute("DELETE FROM meta WHERE key LIKE 'retry:%'")
        self.event(now, "manual_resume")

    def count_requests(self, day):
        return self.db.execute("SELECT count(*) FROM requests WHERE day=?", (day,)).fetchone()[0]

    def rolling_usage(self, now):
        row = self.db.execute("SELECT count(*),min(at) FROM requests WHERE at>?", (now - 86400,)).fetchone()
        outcomes = dict(self.db.execute("SELECT outcome,count(*) FROM requests WHERE at>? GROUP BY outcome", (now - 86400,)))
        return {"used": row[0], "next_release_at": row[1] + 86400 if row[1] is not None else None,
                "outcomes": outcomes}

    def check_request_budget(self, now, day, config):
        if config.quota_mode == "rolling":
            if self.rolling_usage(now)["used"] >= config.rolling_requests_limit:
                raise StopRun("rolling_request_limit")
        elif self.count_requests(day) >= config.daily_requests:
            raise StopRun("daily_request_limit")

    def quota_status(self, now, config):
        result = self.status(local_day(now, config.timezone))
        usage = self.rolling_usage(now)
        result.update(quota_mode=config.quota_mode, continuous_daily=config.continuous_daily,
                      notes_per_run=None if config.continuous_daily else config.notes_per_run,
                      requests_per_run=None if config.continuous_daily else config.requests_per_run,
                      rolling_requests_limit=config.rolling_requests_limit if config.quota_mode == "rolling" else None,
                      rolling_requests_used=usage["used"],
                      rolling_requests_remaining=max(0, config.rolling_requests_limit - usage["used"]) if config.quota_mode == "rolling" else None,
                      rolling_outcomes=usage["outcomes"], next_release_at=usage["next_release_at"])
        if config.quota_mode == "daily":
            result.update(daily_requests_limit=config.daily_requests,
                          daily_requests_remaining=max(0, config.daily_requests-result['business_requests_today']),
                          next_release_at=next_midnight(now, config.timezone))
        return result

    def count_daily_notes(self, day):
        return self.db.execute("SELECT count(*) FROM daily_notes WHERE day=?", (day,)).fetchone()[0]

    def reserve_request(self, day, now, kind, note_id, config, next_request_at):
        # Reserve BEFORE sending: a crash can under-use a budget, never erase it.
        with self.transaction():
            self.check_pause(now)
            self.check_request_budget(now, day, config)
            if note_id:
                seen = self.db.execute("SELECT 1 FROM daily_notes WHERE day=? AND note_id=?", (day, note_id)).fetchone()
                if config.quota_mode == "daily" and config.daily_notes is not None and not seen and self.count_daily_notes(day) >= config.daily_notes:
                    raise StopRun("daily_note_limit")
                self.db.execute("INSERT OR IGNORE INTO daily_notes VALUES(?,?)", (day, note_id))
            row = self.db.execute("INSERT INTO requests(day,at,kind,note_id) VALUES(?,?,?,?)", (day, now, kind, note_id))
            self.set("next_request_at", next_request_at)
            return row.lastrowid

    def finish_request(self, request_id, outcome):
        self.db.execute("UPDATE requests SET outcome=? WHERE id=?", (outcome, request_id))

    def search_used(self, day):
        return bool(self.db.execute("SELECT 1 FROM daily_search WHERE day=?", (day,)).fetchone())

    def reserve_slot(self, day, slot):
        try:
            self.db.execute("INSERT INTO slots VALUES(?,?)", (day, slot))
        except sqlite3.IntegrityError as exc:
            raise StopRun("scheduled_slot_already_used") from exc

    def page_request(self, day):
        self.db.execute("INSERT INTO page_traffic VALUES(?,1) ON CONFLICT(day) DO UPDATE SET requests=requests+1", (day,))

    def complete_count(self):
        return self.db.execute("SELECT count(*) FROM notes WHERE state='complete'").fetchone()[0]

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM notes WHERE state IN ('pending','comments')").fetchone()[0]

    def register_keywords(self, config):
        for keyword in config.keywords:
            self.db.execute("INSERT OR IGNORE INTO keywords(keyword,sort,search_id) VALUES(?,?,?)",
                            (keyword, config.sort, new_search_id()))

    def next_keyword(self, config):
        placeholders = ",".join("?" for _ in config.keywords)
        return self.db.execute("SELECT * FROM keywords WHERE exhausted=0 AND sort=? AND keyword IN (" + placeholders + ") ORDER BY last_used, rowid LIMIT 1",
                               [config.sort] + config.keywords).fetchone()

    def add_search_page(self, job, items, has_more, now, version):
        with self.transaction():
            inserted = 0
            for rank, item in enumerate(items, 1):
                result = self.db.execute("INSERT OR IGNORE INTO notes(note_id,token,source,found_at) VALUES(?,?,?,?)",
                                         (item["id"], item["token"], item["source"], now))
                inserted += result.rowcount
                self.db.execute("UPDATE notes SET token=?,source=?,state=CASE WHEN state='needs_link' THEN CASE WHEN detail IS NULL THEN 'pending' ELSE 'comments' END ELSE state END WHERE note_id=?",
                                (item["token"], item["source"], item["id"]))
                self.db.execute("INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?,?,?,?)",
                                (item["id"], job["keyword"], job["sort"], job["page"], rank, now, job["search_id"], version))
            empty = 0 if inserted else job["empty_pages"] + 1
            self.db.execute("UPDATE keywords SET page=page+1,exhausted=?,empty_pages=?,last_used=? WHERE keyword=? AND sort=?",
                            (int(not has_more or empty >= 2), empty, now, job["keyword"], job["sort"]))

    def next_note(self, day, daily_limit=None):
        if daily_limit is not None and self.count_daily_notes(day) >= daily_limit:
            return self.db.execute("SELECT * FROM notes WHERE state IN ('pending','comments') AND note_id IN (SELECT note_id FROM daily_notes WHERE day=?) ORDER BY found_at,rowid LIMIT 1", (day,)).fetchone()
        return self.db.execute("SELECT * FROM notes WHERE state IN ('pending','comments') ORDER BY found_at,rowid LIMIT 1").fetchone()

    def save_detail(self, note_id, detail, now):
        detail = {**detail, "identity_observed_at": now}
        self.db.execute("UPDATE notes SET detail=?,fetched_at=?,state='comments' WHERE note_id=?",
                        (json.dumps(detail, ensure_ascii=False), now, note_id))

        safe_classify(self, note_id, detail, now)

    def mark_note(self, note_id, state, reason, now):
        self.db.execute("UPDATE notes SET state=?,reason=?,finished_at=? WHERE note_id=?", (state, reason, now, note_id))

    def save_comments_page(self, note_id, requested_cursor, next_cursor, comments, has_more, now, config):
        with self.transaction():
            existing = {row[0] for row in self.db.execute("SELECT comment_id FROM comments WHERE note_id=?", (note_id,))}
            count = len(existing)
            truncated_page = len({item["id"] for item in comments} - existing) > max(0, config.comments_per_note - count)
            for item in comments:
                if count >= config.comments_per_note:
                    break
                count += self.upsert_comment(note_id, item, now)
            self.db.execute("INSERT OR IGNORE INTO cursors VALUES(?,?)", (note_id, requested_cursor))
            pages = self.db.execute("SELECT pages FROM notes WHERE note_id=?", (note_id,)).fetchone()[0] + 1
            if count >= config.comments_per_note:
                state, reason = "complete", "comment_cap" if has_more or truncated_page else "exhausted"
            elif not has_more:
                state, reason = "complete", "exhausted"
            elif not next_cursor or self.db.execute("SELECT 1 FROM cursors WHERE note_id=? AND cursor=?", (note_id, next_cursor)).fetchone():
                state, reason = "needs_review", "repeated_cursor"
            elif pages >= config.max_comment_pages:
                state, reason = "needs_review", "comment_page_limit"
            elif not comments:
                state, reason = "needs_review", "empty_page_with_more"
            else:
                state, reason = "comments", ""
            self.db.execute("UPDATE notes SET cursor=?,pages=?,state=?,reason=?,finished_at=? WHERE note_id=?",
                            (next_cursor, pages, state, reason, now if state != "comments" else None, note_id))

    def link_refresh_job(self):
        return self.db.execute("SELECT n.note_id,n.refresh_attempts,s.keyword,s.sort,s.page,s.search_id,0 AS empty_pages FROM notes n JOIN sources s ON n.note_id=s.note_id WHERE n.state='needs_link' ORDER BY s.discovered_at DESC LIMIT 1").fetchone()

    def refresh_links(self, job, items, now):
        with self.transaction():
            matched = next((item for item in items if item["id"] == job["note_id"]), None)
            if matched:
                self.db.execute("UPDATE notes SET token=?,source=?,state=CASE WHEN detail IS NULL THEN 'pending' ELSE 'comments' END,refresh_attempts=refresh_attempts+1 WHERE note_id=?",
                                (matched["token"], matched["source"], job["note_id"]))
            else:
                self.mark_note(job["note_id"], "needs_review", "link_refresh_not_found", now)

    def status(self, day):
        counts = {row[0]: row[1] for row in self.db.execute("SELECT state,count(*) FROM notes GROUP BY state")}
        traffic = self.db.execute("SELECT requests FROM page_traffic WHERE day=?", (day,)).fetchone()
        return {"day": day, "notes": counts, "business_requests_today": self.count_requests(day),
                "notes_touched_today": self.count_daily_notes(day), "page_requests_today": traffic[0] if traffic else 0,
                "identity_backfill": self.identity_status(),
                "last_protocol_error": self.get("last_protocol_error"),
                "pause": self.get("pause"), "network_until": self.get("network_until", 0),
                "auth_waiting": self.get("auth_waiting", False), "next_request_at": self.get("next_request_at", 0)}

