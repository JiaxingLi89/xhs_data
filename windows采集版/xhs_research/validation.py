"""Validate responses and select research fields. Never persist full platform responses."""
import re

from .errors import AuthExpired, ProtocolError


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ProtocolError("invalid_identifier")
    return value


def flag(data, key):
    value = data.get(key)
    if type(value) is not bool:
        raise ProtocolError("missing_pagination_flag")
    return value


def optional_text(value):
    # Optional platform fields must never discard otherwise valid research data.
    return value if isinstance(value, str) and value.strip() else None


def user_id(value):
    return optional_text(value.get('user_id')) if isinstance(value, dict) else None


def search_page(data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ProtocolError("missing_search_items")
    has_more = flag(data, "has_more")
    items = []
    for row in data["items"]:
        if not isinstance(row, dict):
            raise ProtocolError("invalid_search_item")
        if row.get("model_type") in {"rec_query", "hot_query", "user", "query"}:
            continue
        note_id = identifier(row.get("id"))
        token = row.get("xsec_token")
        if not isinstance(token, str) or not token:
            raise ProtocolError("missing_note_link_token")
        items.append({"id": note_id, "token": token, "source": row.get("xsec_source") or "pc_search"})
    return items, has_more


def note_detail(data, expected_id):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not data["items"]:
        raise ProtocolError("empty_detail_response")
    first = data["items"][0]
    card = first.get("note_card") if isinstance(first, dict) else None
    if not isinstance(card, dict):
        raise ProtocolError("missing_note_card")
    actual = card.get("note_id") or first.get("id")
    if actual and str(actual) != expected_id:
        raise ProtocolError("detail_id_mismatch")
    title, desc = card.get("title", ""), card.get("desc", "")
    if not isinstance(title, str) or not isinstance(desc, str) or not (title or desc):
        raise ProtocolError("empty_note_text")
    counts = card.get("interact_info", {})
    if not isinstance(counts, dict):
        raise ProtocolError("invalid_interactions")
    return {"title": title, "text": desc, "type": card.get("type", ""),
            "author_user_id": user_id(card.get('user')),
            "author_ip_location": optional_text(card.get('ip_location')),
            "published_at": card.get("time", ""),
            "tags": [t["name"] for t in card.get("tag_list", []) if isinstance(t, dict) and isinstance(t.get("name"), str)],
            **{name: str(counts.get(name, "")) for name in ["liked_count", "collected_count", "comment_count", "share_count"]}}


def comments_page(data, expected_id):
    if not isinstance(data, dict) or not isinstance(data.get("comments"), list):
        raise ProtocolError("missing_comments")
    has_more = flag(data, "has_more")
    cursor = data.get("cursor", "")
    if not isinstance(cursor, str):
        raise ProtocolError("invalid_comment_cursor")
    comments = []
    for row in data["comments"]:
        if not isinstance(row, dict) or not isinstance(row.get("content"), str):
            raise ProtocolError("invalid_comment")
        if row.get("note_id") and row["note_id"] != expected_id:
            raise ProtocolError("comment_note_mismatch")
        comments.append({"id": identifier(row.get("id")), "content": row["content"],
                         "commenter_user_id": user_id(row.get('user_info')),
                         "commenter_ip_location": optional_text(row.get('ip_location')),
                         "time": row.get("create_time", ""), "like_count": row.get("like_count", "")})
    return comments, cursor, has_more


def authenticated(data):
    if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
        raise ProtocolError("unknown_auth_response")
    success = data["result"].get("success")
    if success is False:
        raise AuthExpired("not_logged_in")
    if success is not True:
        raise ProtocolError("unknown_auth_response")
    return True
