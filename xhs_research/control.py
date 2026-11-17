from __future__ import annotations

import hashlib
import random

from .errors import (AuthExpired, BrowserDisconnected, NetworkFailure, ProtocolError,
                     RiskBlocked, StaleLink, StopRun, Unavailable)
from .runtime import local_day, next_midnight


class RequestGate:
    """One durable budget for every controlled request; signing happens inside operation."""

    def __init__(self, store, config, clock, progress=print):
        self.store, self.config, self.clock = store, config, clock
        self.progress = progress
        self.run_notes = set()
        self.day = local_day(clock.now(), config.timezone)
        self.deadline = min(clock.now() + config.run_timeout_seconds,
                            next_midnight(clock.now(), config.timezone))

    def preflight(self):
        now = self.clock.now()
        self.store.check_pause(now)
        if now >= self.deadline or local_day(now, self.config.timezone) != self.day:
            raise StopRun("run_window_finished")
        if self.store.count_requests(self.day) >= self.config.daily_requests:
            raise StopRun("daily_request_limit")

    async def wait(self):
        announced = False
        while True:
            self.preflight()
            remaining = self.store.get("next_request_at", 0) - self.clock.now()
            if remaining <= 0:
                return
            if self.clock.now() + remaining >= self.deadline:
                raise StopRun("run_window_finished")
            if not announced:
                self.progress("限速等待约%d秒；已保存当前进度。" % remaining)
                announced = True
            await self.clock.sleep(min(30, remaining))

    async def call(self, kind, operation, note_id=None, key=""):
        self.preflight()
        if note_id and note_id not in self.run_notes and len(self.run_notes) >= self.config.notes_per_run:
            raise StopRun("per_run_note_limit")
        retry_key = "retry:" + hashlib.sha256((kind + ":" + (note_id or "") + ":" + key).encode()).hexdigest()
        if self.store.get(retry_key, 0) >= 2:
            self.store.pause(self.clock.now(), "network_retry_exhausted")
            raise StopRun("network_retry_exhausted")
        await self.wait()
        now = self.clock.now()
        interval = random.uniform(*self.config.request_interval_seconds)
        request_id = self.store.reserve_request(self.day, now, kind, note_id, self.config, now + interval)
        if note_id:
            self.run_notes.add(note_id)
        try:
            result = await operation()
        except RiskBlocked as exc:
            self.store.finish_request(request_id, "risk")
            self.store.pause(self.clock.now(), str(exc), self.clock.now() + exc.cooldown)
            raise
        except NetworkFailure:
            self.store.finish_request(request_id, "network")
            failures = self.store.get(retry_key, 0) + 1
            streak = self.store.get("network_streak", 0) + 1
            self.store.set(retry_key, failures)
            self.store.set("network_streak", streak)
            self.store.set("network_until", self.clock.now() + self.config.network_retry_seconds)
            if streak >= 3 or failures >= 2:
                self.store.pause(self.clock.now(), "network_retry_exhausted")
            raise
        except AuthExpired:
            self.store.finish_request(request_id, "auth_expired")
            raise
        except (Unavailable, StaleLink):
            self.store.finish_request(request_id, "unavailable")
            self.store.set("network_streak", 0)
            raise
        except ProtocolError:
            self.store.finish_request(request_id, "protocol_error")
            self.store.pause(self.clock.now(), "protocol_error")
            raise
        except BaseException:
            self.store.finish_request(request_id, "interrupted")
            raise
        self.store.finish_request(request_id, "ok")
        self.store.set(retry_key, 0)
        self.store.set("network_streak", 0)
        return result


class SessionManager:
    def __init__(self, adapter, gate, store, config, clock, notify=print):
        self.adapter, self.gate, self.store = adapter, gate, store
        self.config, self.clock, self.notify = config, clock, notify
        self.opened = False
        self.rebuilt = False
        self.auth_recoveries = 0

    async def open(self):
        self.gate.preflight()
        if not self.opened:
            await self.adapter.open()
            self.opened = True

    async def check(self):
        async def check_current_cookies():
            await self.adapter.sync_cookies()
            return await self.adapter.check_auth()
        return await self.gate.call("auth_check", check_current_cookies)

    async def ensure_ready(self, manual=False):
        self.gate.preflight()
        if self.store.get("auth_waiting", False) and not manual:
            raise StopRun("waiting_for_login: run login")
        await self.open()
        try:
            await self.check()
            self.store.set("auth_waiting", False)
            return
        except AuthExpired:
            pass
        # One fresh read of the existing browser cookies. A network error is NOT caught as auth failure.
        try:
            await self.check()
            self.store.set("auth_waiting", False)
            return
        except AuthExpired:
            pass
        if self.auth_recoveries >= 1:
            self.store.set("auth_waiting", True)
            raise StopRun("login_failed_again")
        day = local_day(self.clock.now(), self.config.timezone)
        if not manual and self.store.get("auto_login_day") == day:
            self.store.set("auth_waiting", True)
            raise StopRun("automatic_login_limit")
        self.auth_recoveries += 1
        self.store.set("auth_waiting", True)  # Durable before opening UI, including crashes.
        if not manual:
            self.store.set("auto_login_day", day)
        await self.gate.call("login_page", self.adapter.show_login)
        self.notify("小红书需要登录：请在打开的浏览器中扫码，程序最多等待10分钟。")
        expires = min(self.clock.now() + self.config.login_wait_seconds, self.gate.deadline)
        while self.clock.now() < expires:
            self.gate.preflight()
            # DOM / cookie inspection only; this method must never issue HTTP requests.
            try:
                candidate = await self.adapter.login_candidate()
            except RiskBlocked as exc:
                self.store.pause(self.clock.now(), str(exc), self.clock.now() + exc.cooldown)
                raise
            if candidate:
                try:
                    await self.check()
                except AuthExpired:
                    raise StopRun("login_not_confirmed")
                self.store.set("auth_waiting", False)
                self.store.event(self.clock.now(), "login_restored")
                self.notify("登录已确认，继续原任务；配额和暂停状态保持有效。")
                return
            await self.clock.sleep(min(3, expires - self.clock.now()))
        raise StopRun("login_wait_timeout")

    async def call(self, kind, operation, note_id=None, key=""):
        try:
            return await self.gate.call(kind, operation, note_id, key)
        except AuthExpired:
            await self.ensure_ready()
            try:
                return await self.gate.call(kind, operation, note_id, key)
            except AuthExpired:
                self.store.set("auth_waiting", True)
                raise StopRun("login_failed_again")
        except BrowserDisconnected:
            # Rebuild once; the failed request remains charged and backoff is retained.
            if not self.rebuilt:
                self.rebuilt = True
                await self.adapter.reopen()
                self.opened = True
            raise

    async def close(self):
        if self.opened:
            await self.adapter.close()
            self.opened = False

