# Adapted from MediaCrawler, Copyright (c) 2025 relakkes@gmail.com.
# NON-COMMERCIAL LEARNING LICENSE 1.1; see third_party/MediaCrawler.LICENSE.
# Source: NanmiCoder/MediaCrawler main, reviewed 2026-11-13.
# Related upstream fix: d594c20c13f46c368bfec133de3a4f1a97cd4d79.
# The local adaptation is content-pinned in upstream.lock.json.
# Only the read-only endpoint payloads and signing-library integration are reused.
# Batch loops, automatic retries, proxy rotation and upstream login loops are excluded.
from dataclasses import dataclass
import secrets
import time


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    data: dict


def new_search_id():
    # Format used by MediaCrawler media_platform/xhs/help.py:get_search_id.
    value = (int(time.time() * 1000) << 64) + secrets.randbelow(2147483647)
    result = ""
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    while value:
        value, digit = divmod(value, 36)
        result = alphabet[digit] + result
    return result or "0"


def auth_request():
    return Request("GET", "/api/sns/web/v1/user/selfinfo", {})


def search_request(keyword, page, search_id, sort):
    return Request("POST", "/api/sns/web/v1/search/notes", {
        "keyword": keyword, "page": page, "page_size": 20,
        "search_id": search_id, "sort": sort, "note_type": 0,
    })


def detail_request(note_id, token, source):
    return Request("POST", "/api/sns/web/v1/feed", {
        "source_note_id": note_id, "image_formats": ["jpg", "webp", "avif"],
        "extra": {"need_body_topic": 1}, "xsec_source": source or "pc_search", "xsec_token": token,
    })


def comments_request(note_id, token, cursor):
    return Request("GET", "/api/sns/web/v2/comment/page", {
        "note_id": note_id, "cursor": cursor, "top_comment_id": "",
        "image_formats": "jpg,webp,avif", "xsec_token": token,
    })

