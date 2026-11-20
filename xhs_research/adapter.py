from __future__ import annotations

import importlib.metadata
import json
import math
import os
import sys
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse

from .errors import (AuthExpired, BrowserDisconnected, NetworkFailure, ProtocolError,
                     RiskBlocked, SetupError, StaleLink, Unavailable)
from .mediacrawler_protocol import auth_request, comments_request, detail_request, search_request
from .browser_diagnostics import record_restriction
from .runtime import local_day
from .validation import authenticated


PINNED_DEPENDENCIES = {"playwright": "1.61.0", "xhshow": "0.2.0"}


def live_dependencies():
    issues = []
    if sys.version_info < (3, 10):
        issues.append("实采需要 Python >= 3.10，建议 Python 3.12。")
    for package, version in PINNED_DEPENDENCIES.items():
        try:
            found = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            found = None
        if found != version:
            issues.append("需要 %s==%s，当前为 %s。" % (package, version, found or "未安装"))
    return issues


def retry_after(value, now):
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 86400
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0, parsed.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 86400


def classify_response(status, headers, body, now):
    try:
        return _classify_response(status, headers, body, now)
    except ProtocolError as exc:
        exc.diagnostic.update(stage="response", http_status=status,
                              body_type=type(body).__name__)
        if isinstance(body, dict):
            code = str(body.get("code", ""))
            # Only numeric platform codes; never copy arbitrary messages or values.
            if code.lstrip("-").isdigit() and len(code) <= 16:
                exc.diagnostic["platform_code"] = code
            exc.diagnostic["data_type"] = type(body.get("data")).__name__
            exc.diagnostic["success_type"] = type(body.get("success")).__name__
        raise


def _classify_response(status, headers, body, now):
    """Return validated outer data; never put a response body in exceptions or logs."""
    headers = {str(k).lower(): str(v) for k, v in headers.items()}
    if status == 401:
        raise AuthExpired("http_401")
    if status == 429:
        raise RiskBlocked("rate_limited", retry_after(headers.get("retry-after"), now))
    if status in {403, 461, 471}:
        raise RiskBlocked("verification_or_access_restriction")
    if status >= 500:
        raise NetworkFailure("server_error")
    if 300 <= status < 400:
        if "login" in headers.get("location", "").lower():
            raise AuthExpired("login_redirect")
        raise ProtocolError("unexpected_redirect")
    if status != 200 or not isinstance(body, dict):
        raise ProtocolError("unexpected_response_format")
    code = str(body.get("code", ""))
    message = str(body.get("msg", ""))
    if code in {"300011", "300012"} or any(s in message for s in ["访问受限", "安全验证", "请求频繁", "访问频繁"]):
        raise RiskBlocked("platform_security_restriction")
    if any(s in message for s in ["登录过期", "登录已过期", "未登录", "请登录", "登录失效"]):
        raise AuthExpired("session_expired")
    if ("xsec" in message.lower() or "链接" in message) and any(s in message for s in ["失效", "过期", "invalid", "expired"]):
        raise StaleLink("link_expired")
    if code in {"-510000", "-510001"}:
        raise Unavailable("note_unavailable")
    if body.get("success") is not True or not isinstance(body.get("data"), dict):
        raise ProtocolError("platform_business_error")
    return body["data"]


class MediaCrawlerAdapter:
    """Small vendored read-only MediaCrawler protocol, with a shared browser cookie jar.

    RequestGate owns every call. This adapter has NO retries or internal pagination.
    """

    def __init__(self, config, store, clock):
        self.config, self.store, self.clock = config, store, clock
        self.playwright = self.context = self.page = self.signer = None
        self.external_risk = False
        self.browser_phase = "startup"
        self.login_cookie_before = None
        self.cookies = {}

    async def open(self):
        problems = live_dependencies()
        if problems:
            raise SetupError(" ".join(problems) + " 运行 python3.12 scripts/setup_live.py 安装。")
        from playwright.async_api import async_playwright
        from xhshow import Xhshow
        self.signer = Xhshow()
        profile = self.config.state_dir / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        self.playwright = await async_playwright().start()
        try:
            options = {"user_data_dir": str(profile), "headless": False, "chromium_sandbox": True}
            if self.config.browser_channel != "chromium":
                options["channel"] = self.config.browser_channel
            self.context = await self.playwright.chromium.launch_persistent_context(**options)
            self.context.on("request", self._page_request)
            self.context.on("response", self._page_response)
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.user_agent = await self.page.evaluate("navigator.userAgent")
            await self.sync_cookies()
        except RiskBlocked:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise SetupError("浏览器未能启动。检查 Chrome 安装、专用配置目录占用及图形桌面会话。") from exc

    def _page_request(self, request):
        # Count all browser page traffic separately, without recording URLs or headers.
        self.store.page_request(local_day(self.clock.now(), self.config.timezone))

    def _page_response(self, response):
        host = urlparse(response.url).hostname or ""
        if (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")) and response.status in {403, 429, 461, 471}:
            self.external_risk = True
            record_restriction(self.config.state_dir, response, self.clock.now(), self.browser_phase)

    def _check_browser(self):
        if self.external_risk:
            raise RiskBlocked("browser_page_restricted", 86400)
        if not self.context or not self.page or self.page.is_closed():
            raise BrowserDisconnected("browser_disconnected")

    async def has_auth_credentials(self):
        """Local cookie inspection only; presence does not prove authentication."""
        await self.sync_cookies()
        return bool(self.cookies.get("web_session") and self.cookies.get("a1"))

    async def sync_cookies(self):
        self._check_browser()
        try:
            rows = await self.context.cookies(["https://www.xiaohongshu.com", "https://edith.xiaohongshu.com"])
            self.cookies = {row["name"]: row["value"] for row in rows}
        except Exception as exc:
            raise BrowserDisconnected("cookie_context_disconnected") from exc

    async def _request(self, request):
        self._check_browser()
        await self.sync_cookies()
        if not self.cookies.get("web_session") or not self.cookies.get("a1"):
            raise AuthExpired("missing_session")
        cookie_string = "; ".join(k + "=" + v for k, v in self.cookies.items())
        # This code executes ONLY after RequestGate has slept and reserved the request.
        try:
            if request.method == "GET":
                signed = self.signer.sign_headers_get(uri=request.path, cookies=cookie_string, params=request.data)
            else:
                signed = self.signer.sign_headers_post(uri=request.path, cookies=cookie_string, payload=request.data)
        except Exception as exc:
            raise ProtocolError("signing_dependency_error", stage="signing") from exc
        if not all(signed.get(key) for key in ["x-s", "x-t", "x-s-common"]):
            raise ProtocolError("missing_signature_headers", stage="signing")
        url = "https://edith.xiaohongshu.com" + request.path
        headers = {**signed, "User-Agent": self.user_agent, "Accept": "application/json",
                   "Origin": "https://www.xiaohongshu.com", "Referer": "https://www.xiaohongshu.com/"}
        kwargs = {}
        if request.method == "GET" and request.data:
            url += "?" + "&".join(str(k) + "=" + quote(str(v), safe=",") for k, v in request.data.items())
        elif request.method == "POST":
            headers["Content-Type"] = "application/json;charset=UTF-8"
            kwargs["data"] = json.dumps(request.data, ensure_ascii=False, separators=(",", ":"))
        try:
            response = await self.context.request.fetch(url, method=request.method, headers=headers,
                                                       timeout=30000, max_retries=0, max_redirects=0, **kwargs)
        except Exception as exc:
            if self.page.is_closed() or "TargetClosed" in type(exc).__name__:
                raise BrowserDisconnected("browser_disconnected") from exc
            raise NetworkFailure("request_transport_error") from exc
        try:
            try:
                body = await response.json()
            except Exception:
                body = None
            return classify_response(response.status, response.headers, body, self.clock.now())
        finally:
            await response.dispose()

    async def check_auth(self):
        previous = self.browser_phase
        self.browser_phase = "auth_check"
        try:
            data = await self._request(auth_request())
            result = authenticated(data)
        finally:
            self.browser_phase = previous
        if result:
            self.browser_phase = "collection"
        return result

    async def search(self, keyword, page, search_id, sort):
        return await self._request(search_request(keyword, page, search_id, sort))

    async def detail(self, note_id, token, source):
        return await self._request(detail_request(note_id, token, source))

    async def comments(self, note_id, token, cursor):
        return await self._request(comments_request(note_id, token, cursor))

    async def show_login(self):
        self.browser_phase = "login_page"
        self._check_browser()
        await self.sync_cookies()
        self.login_cookie_before = self.cookies.get("web_session")
        try:
            await self.page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=30000)
            # Interact only with the platform's normal login UI. Never request SMS automatically.
            if not await self.page.locator(".login-container, img.qrcode-img").count():
                button = self.page.get_by_text("登录", exact=True)
                if await button.count():
                    await button.first.click(timeout=5000)
            await self.page.bring_to_front()
        except Exception as exc:
            raise NetworkFailure("login_page_unavailable") from exc

    async def login_candidate(self):
        self._check_browser()
        try:
            text = await self.page.locator("body").inner_text(timeout=3000)
            if any(term in text for term in ["请通过验证", "访问受限", "安全验证"]):
                raise RiskBlocked("manual_verification_required")
            await self.sync_cookies()
            changed = self.cookies.get("web_session") and self.cookies.get("web_session") != self.login_cookie_before
            profile = await self.page.locator("a[href*='/user/profile/']").filter(has_text="我").count()
            return bool(changed or profile)
        except (RiskBlocked, BrowserDisconnected):
            raise
        except Exception as exc:
            raise BrowserDisconnected("login_window_disconnected") from exc

    async def reopen(self):
        await self.close()
        await self.open()

    async def close(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

