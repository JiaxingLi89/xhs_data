"""Allowlisted browser diagnostics: never persist raw URLs or payloads."""
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

SAFE_PATHS = {
    "/", "/explore", "/api/sns/web/v1/user/selfinfo",
    "/api/sns/web/v1/search/notes", "/api/sns/web/v1/feed",
    "/api/sns/web/v2/comment/page",
}
SAFE_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com", "edith.xiaohongshu.com"}
SAFE_TYPES = {"document", "stylesheet", "image", "media", "font", "script",
              "texttrack", "xhr", "fetch", "eventsource", "websocket", "manifest", "other"}


def record_restriction(directory, response, now, phase):
    """Logging failures must never prevent the existing risk pause."""
    try:
        parsed = urlparse(response.url)
        path = parsed.path
        if path not in SAFE_PATHS:
            if path.startswith("/explore/"):
                path = "/explore/<redacted>"
            elif path.startswith("/user/profile/"):
                path = "/user/profile/<redacted>"
            else:
                path = "<redacted>"
        resource_type = response.request.resource_type
        row = {
            "time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "event": "browser_page_restricted",
            "status": response.status,
            "host": parsed.hostname if parsed.hostname in SAFE_HOSTS else "<subdomain>.xiaohongshu.com",
            "path": path,
            "resource_type": resource_type if resource_type in SAFE_TYPES else "other",
            "phase": phase if phase in {"startup", "login_page", "auth_check", "collection"} else "unknown",
        }
        target = directory / "browser-diagnostics.jsonl"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        print("浏览器受限诊断：" + json.dumps(row, ensure_ascii=False)
              + "；已保存至 browser-diagnostics.jsonl（采集数据目录）。", file=sys.stderr)
    except Exception:
        # Do not print exception text: it may include a raw URL or credentials.
        print("浏览器受限诊断写入失败；原有暂停规则仍生效。", file=sys.stderr)
