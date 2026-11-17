import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from xhs_research.adapter import MediaCrawlerAdapter
from xhs_research.config import Config
from xhs_research.control import RequestGate, SessionManager
from xhs_research.demo import DemoAdapter, FakeClock
from xhs_research.errors import ProtocolError, RiskBlocked
from xhs_research.store import Store


class Response:
    status = 200
    headers = {}

    def __init__(self, data):
        self.data = data
        self.disposed = False

    async def json(self):
        return self.data

    async def dispose(self):
        self.disposed = True


class BrowserRequest:
    def __init__(self):
        self.calls = []
        self.response = Response({"success": True, "data": {"result": {"success": True}}})

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class Context:
    def __init__(self):
        self.request = BrowserRequest()
        self.session = "old-session"

    async def cookies(self, urls):
        return [{"name": "web_session", "value": self.session}, {"name": "a1", "value": "device-cookie"}]


class Page:
    def is_closed(self):
        return False


class Signer:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def sign(self, **kwargs):
        self.calls.append((self.clock.now(), kwargs))
        return {"x-s": "signature", "x-t": str(self.clock.now()), "x-s-common": "common"}

    sign_headers_get = sign
    sign_headers_post = sign


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name), request_interval_seconds=[120, 120])
        self.clock = FakeClock()
        self.store = Store(self.config.state_dir)
        self.adapter = MediaCrawlerAdapter(self.config, self.store, self.clock)
        self.adapter.context = Context()
        self.adapter.page = Page()
        self.adapter.signer = Signer(self.clock)
        self.adapter.user_agent = "ActualBrowser/123"
        self.gate = RequestGate(self.store, self.config, self.clock, lambda *_: None)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_real_transport_reads_rotated_cookie_and_signs_after_wait(self):
        await self.gate.call("auth_check", self.adapter.check_auth)
        first = self.clock.now()
        self.adapter.context.session = "new-session"
        await self.gate.call("auth_check", self.adapter.check_auth)
        signed_at, signed_args = self.adapter.signer.calls[-1]
        self.assertGreaterEqual(signed_at, first + 120)
        self.assertIn("new-session", signed_args["cookies"])
        self.assertNotIn("old-session", signed_args["cookies"])
        _, options = self.adapter.context.request.calls[-1]
        self.assertEqual(options["max_retries"], 0)
        self.assertEqual(options["max_redirects"], 0)
        self.assertEqual(options["headers"]["User-Agent"], "ActualBrowser/123")
        self.assertTrue(self.adapter.context.request.response.disposed)

    async def test_signed_post_payload_and_transmitted_body_are_identical(self):
        self.adapter.context.request.response = Response({"success": True, "data": {"items": [], "has_more": False}})
        await self.gate.call("search", lambda: self.adapter.search("母乳 空格", 1, "session", "general"))
        _, signing = self.adapter.signer.calls[-1]
        _, transport = self.adapter.context.request.calls[-1]
        self.assertEqual(json.loads(transport["data"]), signing["payload"])
        self.assertEqual(signing["payload"]["page_size"], 20)

    async def test_query_token_is_encoded_without_changing_signed_value(self):
        self.adapter.context.request.response = Response({"success": True, "data": {"comments": [], "has_more": False}})
        await self.gate.call("comments", lambda: self.adapter.comments("note", "a+/= token", "cursor+1"), "note")
        url, _ = self.adapter.context.request.calls[-1]
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["xsec_token"], ["a+/= token"])
        self.assertEqual(query["cursor"], ["cursor+1"])

    async def test_http_200_security_error_uses_global_pause(self):
        self.adapter.context.request.response = Response({"code": 300011, "success": False, "msg": "secret server detail"})
        with self.assertRaises(RiskBlocked):
            await self.gate.call("auth_check", self.adapter.check_auth)
        self.assertEqual(self.store.get("pause")["reason"], "platform_security_restriction")
        self.assertTrue(self.adapter.context.request.response.disposed)
        self.assertNotIn("secret server detail", str(self.store.status(self.gate.day)))

    async def test_dom_challenge_during_login_is_persistently_paused(self):
        adapter = DemoAdapter(self.clock, authenticated=False)
        async def challenge():
            raise RiskBlocked("manual_verification_required")
        adapter.login_candidate = challenge
        session = SessionManager(adapter, self.gate, self.store, self.config, self.clock, lambda *_: None)
        with self.assertRaises(RiskBlocked):
            await session.ensure_ready()
        self.assertEqual(self.store.get("pause")["reason"], "manual_verification_required")
        self.assertTrue(self.store.get("auth_waiting"))

    async def test_exact_comment_limit_on_final_page_is_not_marked_truncated(self):
        self.store.register_keywords(self.config)
        self.store.add_search_page(self.store.next_keyword(self.config), [{"id": "n", "token": "x", "source": "pc_search"}], False, self.clock.now(), "v")
        self.store.save_comments_page("n", "", "", [{"id": "c%d" % n, "content": "text"} for n in range(50)], False, self.clock.now(), self.config)
        row = self.store.db.execute("SELECT state,reason FROM notes WHERE note_id='n'").fetchone()
        self.assertEqual(tuple(row), ("complete", "exhausted"))


if __name__ == "__main__":
    unittest.main()

