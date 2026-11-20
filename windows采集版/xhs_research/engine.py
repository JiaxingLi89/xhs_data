from .errors import ProtocolError, StaleLink, StopRun, Unavailable
from .runtime import local_day
from .validation import comments_page, note_detail, search_page
from .identity import backfill_step


class Engine:
    def __init__(self, config, store, session, clock, progress=print):
        self.config, self.store, self.session, self.clock = config, store, session, clock
        self.progress = progress

    def preflight(self):
        self.session.gate.preflight()
        if self.store.next_identity_job() is not None:
            return
        if self.config.stage == "formal" and (self.config.reviewed_notes < 10 or
                (self.store.complete_count() < self.config.pilot_notes and
                 not self.store.get("pilot_review_accepted", False))):
            raise StopRun("pilot_review_required")
        if self.store.complete_count() >= self.config.collection_limit:
            raise StopRun("pilot_complete_review_required" if self.config.stage == "pilot" else "target_reached")
        day = local_day(self.clock.now(), self.config.timezone)
        if (self.config.daily_notes is not None and self.store.count_daily_notes(day) >= self.config.daily_notes
                and not self.store.next_note(day, self.config.daily_notes)):
            raise StopRun("daily_note_limit")

    async def discover(self):
        job = self.store.link_refresh_job()
        refresh = job is not None
        if not refresh and self.store.pending_count() >= self.config.candidate_low_watermark:
            return False
        if not refresh:
            job = self.store.next_keyword(self.config)
        if job is None:
            return False
        if refresh and job["refresh_attempts"] >= 1:
            self.store.mark_note(job["note_id"], "needs_review", "link_refresh_exhausted", self.clock.now())
            return True
        raw = await self.session.call("search", lambda: self.session.adapter.search(job["keyword"], job["page"], job["search_id"], job["sort"]),
                                      key=job["keyword"] + ":" + str(job["page"]))
        items, has_more = search_page(raw)
        if refresh:
            self.store.refresh_links(job, items, self.clock.now())
        else:
            self.store.add_search_page(job, items, has_more, self.clock.now(), self.config.sampling_version)
        self.progress("候选发现完成；详情与评论按队列限额处理。")
        return True

    async def run(self, already_ready=False):
        self.store.prepare_identity_backfill(self.clock.now())
        self.preflight()
        self.store.register_keywords(self.config)
        if not already_ready:
            await self.session.ensure_ready()
        try:
            while True:
                self.preflight()
                if await backfill_step(self.store, self.session, self.clock, self.progress):
                    continue
                discovered = await self.discover()
                day = local_day(self.clock.now(), self.config.timezone)
                note = self.store.next_note(day, self.config.daily_notes)
                if note is None:
                    if discovered:
                        continue
                    raise StopRun("candidates_exhausted")
                note_id = note["note_id"]
                try:
                    if note["state"] == "pending":
                        raw = await self.session.call("detail", lambda: self.session.adapter.detail(note_id, note["token"], note["source"]), note_id)
                        self.store.save_detail(note_id, note_detail(raw, note_id), self.clock.now())
                    raw = await self.session.call("comments", lambda: self.session.adapter.comments(note_id, note["token"], note["cursor"]), note_id, note["cursor"])
                    comments, cursor, more = comments_page(raw, note_id)
                    self.store.save_comments_page(note_id, note["cursor"], cursor, comments, more, self.clock.now(), self.config)
                    saved = self.store.db.execute(
                        'SELECT pages,state FROM notes WHERE note_id=?', (note_id,)).fetchone()
                    count = self.store.db.execute(
                        'SELECT count(*) FROM comments WHERE note_id=?', (note_id,)).fetchone()[0]
                    self.progress("当前笔记 %s：正文已保存，评论%d页 / %d条，%s；完整完成%d篇。" %
                                  (note_id, saved['pages'], count,
                                   '评论采集中' if saved['state'] == 'comments' else saved['state'],
                                   self.store.complete_count()))
                except Unavailable:
                    self.store.mark_note(note_id, "unavailable", "note_unavailable", self.clock.now())
                except StaleLink:
                    self.store.mark_note(note_id, "needs_link", "link_expired", self.clock.now())
        except ProtocolError:
            self.store.pause(self.clock.now(), "protocol_error")
            raise
