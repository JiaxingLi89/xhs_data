from .errors import ProtocolError, StaleLink, StopRun, Unavailable
from .runtime import local_day
from .identity import backfill_step
from .validation import comments_page, note_detail, search_page
from .total import total_note_ids


class Engine:
    def __init__(self, config, store, session, clock, progress=print):
        self.config, self.store, self.session, self.clock = config, store, session, clock
        self.progress = progress

    def validate(self, kind, validator, raw, *args):
        try:
            return validator(raw, *args)
        except ProtocolError as exc:
            exc.diagnostic.update(stage="validation", request_kind=kind)
            self.store.set("last_protocol_error", {"at": self.clock.now(), **exc.diagnostic})
            raise

    def preflight(self):
        self.session.gate.preflight()
        if self.store.next_identity_job() is not None:
            return
        if self.store.next_comment_backfill() is not None:
            day = local_day(self.clock.now(), self.config.timezone)
            if self.config.quota_mode == "daily" and self.store.next_note(day, self.config.daily_notes) is None:
                raise StopRun("daily_note_limit")
            return
        if self.config.stage == "formal" and (self.config.reviewed_notes < 10 or self.store.collection_count() < self.config.pilot_notes):
            raise StopRun("pilot_review_required")
        if self.store.collection_count() >= self.config.collection_limit:
            raise StopRun("pilot_complete_review_required" if self.config.stage == "pilot" else "target_reached")
        day = local_day(self.clock.now(), self.config.timezone)
        if self.config.quota_mode == "daily" and self.config.daily_notes is not None and self.store.count_daily_notes(day) >= self.config.daily_notes and not self.store.next_note(day, self.config.daily_notes):
            raise StopRun("daily_note_limit")

    async def discover(self):
        backfill = self.store.next_comment_backfill()
        if backfill is not None and backfill['state'] != 'needs_link':
            return False
        job = self.store.link_refresh_job()
        refresh = job is not None
        if not refresh and self.store.next_comment_backfill() is not None:
            return False
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
        items, has_more = self.validate("search", search_page, raw)
        if refresh:
            self.store.refresh_links(job, items, self.clock.now())
        else:
            self.store.add_search_page(job, items, has_more, self.clock.now(), self.config.sampling_version)
        self.progress("候选发现完成；详情与评论按队列限额处理。")

        return True

    async def run(self, already_ready=False):
        if self.config.total_research_file is not None:
            self.store.load_external_note_ids(total_note_ids(self.config.total_research_file))
        self.store.prepare_comment_backfill(self.clock.now(), self.config)
        self.store.prepare_identity_backfill(self.clock.now())
        self.preflight()
        self.store.register_keywords(self.config)
        if not already_ready:
            await self.session.ensure_ready()
        try:
            discovered_once = False
            while True:
                self.preflight()
                if await backfill_step(self.store, self.session, self.clock, self.progress, self.validate):
                    continue
                day = local_day(self.clock.now(), self.config.timezone)
                next_note = self.store.next_note(day, self.config.daily_notes if self.config.quota_mode == "daily" else None)
                if not self.config.continuous_daily and len(self.session.gate.run_notes) >= self.config.notes_per_run and (next_note is None or next_note['note_id'] not in self.session.gate.run_notes):
                    raise StopRun("per_run_note_limit")
                had_backfill = self.store.next_comment_backfill() is not None
                discovered = False
                if not discovered_once or next_note is None or next_note["state"] == "needs_link":
                    discovered = await self.discover()
                    discovered_once = True
                if had_backfill and self.store.next_comment_backfill() is None:
                    continue
                day = local_day(self.clock.now(), self.config.timezone)
                note = self.store.next_note(day, self.config.daily_notes if self.config.quota_mode == "daily" else None)
                if note is None:
                    if discovered:
                        continue
                    raise StopRun("no_pending_candidates")
                if note["state"] == "needs_link":
                    raise StopRun("link_refresh_pending")
                note_id = note["note_id"]
                try:
                    if note["state"] == "pending":
                        raw = await self.session.call("detail", lambda: self.session.adapter.detail(note_id, note["token"], note["source"]), note_id)
                        self.store.save_detail(note_id, self.validate("detail", note_detail, raw, note_id), self.clock.now())
                    raw = await self.session.call("comments", lambda: self.session.adapter.comments(note_id, note["token"], note["cursor"]), note_id, note["cursor"])
                    comments, cursor, more = self.validate("comments", comments_page, raw, note_id)
                    self.store.save_comments_page(note_id, note["cursor"], cursor, comments, more, self.clock.now(), self.config)
                    self.progress("已保存一页评论；完成笔记数：%s" % self.store.complete_count())
                except Unavailable:
                    self.store.mark_note(note_id, "unavailable", "note_unavailable", self.clock.now())
                except StaleLink:
                    self.store.mark_note(note_id, "needs_link", "link_expired", self.clock.now())
        except ProtocolError:
            self.store.pause(self.clock.now(), "protocol_error")
            raise

