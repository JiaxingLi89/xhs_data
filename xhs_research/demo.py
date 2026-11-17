"""Deterministic synthetic adapter: never imports Playwright or sends network traffic."""
from datetime import datetime
from zoneinfo import ZoneInfo

from .errors import AuthExpired


class FakeClock:
    def __init__(self, now=None):
        self.timestamp = now if now is not None else datetime(2026, 11, 13, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    def now(self):
        return self.timestamp

    async def sleep(self, seconds):
        self.timestamp += seconds


class DemoAdapter:
    def __init__(self, clock, authenticated=True):
        self.clock = clock
        self.authenticated = authenticated
        self.calls = []
        self.login_windows = 0
        self.reopens = 0
        self.login_succeeds = True
        self.failures = {}

    def record(self, kind, note_id="", cursor=""):
        self.calls.append((self.clock.now(), kind, note_id, cursor))
        errors = self.failures.get(kind, [])
        if errors:
            error = errors.pop(0)
            if error is not None:
                raise error

    async def open(self):
        pass

    async def close(self):
        pass

    async def reopen(self):
        self.reopens += 1

    async def sync_cookies(self):
        pass

    async def check_auth(self):
        self.record("auth_check")
        if not self.authenticated:
            raise AuthExpired("demo_expired")
        return True

    async def show_login(self):
        self.record("login_page")
        self.login_windows += 1

    async def login_candidate(self):
        if self.login_succeeds:
            self.authenticated = True
            return True
        return False

    async def search(self, keyword, page, search_id, sort):
        self.record("search")
        start = (page - 1) * 20
        return {"items": [{"id": "demo%04d" % n, "xsec_token": "DEMO_TOKEN_DO_NOT_EXPORT", "xsec_source": "pc_search"}
                          for n in range(start, start + 20)], "has_more": page < 10}

    async def detail(self, note_id, token, source):
        self.record("detail", note_id)
        return {"items": [{"id": note_id, "note_card": {
            "note_id": note_id, "title": "模拟研究笔记 " + note_id, "desc": "仅用于离线测试，不是真实小红书内容。",
            "type": "normal", "time": 1700000000000, "interact_info": {"liked_count": "12", "comment_count": "60"},
            "user": {"nickname": "不应导出", "user_id": "PRIVATE_DEMO_USER"}, "tag_list": [{"name": "模拟数据"}]}}]}

    async def comments(self, note_id, token, cursor):
        self.record("comments", note_id, cursor)
        offset = int(cursor or "0")
        return {"comments": [{"id": note_id + "c%03d" % n, "note_id": note_id, "content": "模拟一级评论 %d" % n,
                              "create_time": 1700000000000 + n, "like_count": "0",
                              "user_info": {"nickname": "不应导出"}, "sub_comments": [{"id": "not-collected"}]}
                             for n in range(offset, min(offset + 20, 60))],
                "cursor": str(offset + 20), "has_more": offset + 20 < 60}

