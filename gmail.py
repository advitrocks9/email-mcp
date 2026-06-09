"""Gmail backend using the official OAuth refresh-token flow.

The expected token scope is ``https://www.googleapis.com/auth/gmail.modify``:
read, label, trash, untrash, and draft access without permanent delete access.
"""

from __future__ import annotations

import base64
import time
import urllib.parse
from email.message import EmailMessage
from typing import Any

from common import audit, http, load_env

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_cache: dict[str, str | float | None] = {"token": None, "exp": 0.0}


def _creds() -> tuple[str, str, str]:
    env = load_env()
    client_id = env.get("GMAIL_CLIENT_ID")
    client_secret = env.get("GMAIL_CLIENT_SECRET")
    refresh_token = env.get("GMAIL_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        raise RuntimeError(
            "Gmail is not configured: set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, "
            "and GMAIL_REFRESH_TOKEN in email-mcp/.env."
        )
    return client_id, client_secret, refresh_token


def get_token() -> str:
    token = _cache["token"]
    expires_at = float(_cache["exp"] or 0)
    if isinstance(token, str) and expires_at - time.time() > 120:
        return token

    client_id, client_secret, refresh_token = _creds()
    response = http(
        "POST",
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        form=True,
    )
    _cache["token"] = str(response["access_token"])
    _cache["exp"] = time.time() + float(response.get("expires_in", 3600))
    return str(_cache["token"])


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer " + get_token()}


_FOLDER_LABEL: dict[str, str | None] = {
    "inbox": "INBOX",
    "sent": "SENT",
    "drafts": "DRAFT",
    "draft": "DRAFT",
    "trash": "TRASH",
    "deleted": "TRASH",
    "spam": "SPAM",
    "junk": "SPAM",
    "archive": None,
    "starred": "STARRED",
    "important": "IMPORTANT",
    "unread": "UNREAD",
    "all": None,
    "allitems": None,
}


def _labels() -> list[dict[str, Any]]:
    return http("GET", API + "/labels", _auth()).get("labels", [])


def _label_id(name: str | None) -> str | None:
    if not name:
        return None
    key = name.lower()
    if key in _FOLDER_LABEL:
        return _FOLDER_LABEL[key]
    for label in _labels():
        if str(label.get("name") or "").lower() == name.lower():
            return str(label["id"])
    return name


def _hdr(headers: list[dict[str, Any]], name: str) -> str:
    for header in headers:
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value", ""))
    return ""


def _has_attachment(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    if payload.get("filename") or (payload.get("body") or {}).get("attachmentId"):
        return True
    return any(_has_attachment(part) for part in payload.get("parts", []) or [])


def _norm(message: dict[str, Any]) -> dict[str, Any]:
    headers = (message.get("payload") or {}).get("headers", [])
    labels = message.get("labelIds", []) or []
    list_unsubscribe = _hdr(headers, "List-Unsubscribe")
    return {
        "provider": "gmail",
        "id": message["id"],
        "thread_id": message.get("threadId"),
        "from": _hdr(headers, "From"),
        "to": _hdr(headers, "To"),
        "cc": _hdr(headers, "Cc"),
        "subject": _hdr(headers, "Subject"),
        "date": _hdr(headers, "Date"),
        "unread": "UNREAD" in labels,
        "has_attachment": _has_attachment(message.get("payload")),
        "importance": "important" if "IMPORTANT" in labels else "",
        "list_unsubscribe": list_unsubscribe,
        "list_unsubscribe_post": _hdr(headers, "List-Unsubscribe-Post") if list_unsubscribe else "",
        "snippet": str(message.get("snippet") or "")[:200],
        "folders": labels,
    }


def list_folders() -> list[dict[str, Any]]:
    return [{"id": label["id"], "name": label.get("name")} for label in _labels()]


def list_messages_page(
    folder: str = "inbox",
    query: str = "",
    unread_only: bool = False,
    limit: int = 15,
    cursor: str = "",
) -> dict[str, Any]:
    params = {"maxResults": int(limit)}
    gmail_query = query or ""
    if unread_only:
        gmail_query = (gmail_query + " is:unread").strip()
    if folder and folder.lower() == "archive":
        gmail_query = (gmail_query + " -in:inbox -in:sent -in:drafts -in:trash -in:spam").strip()
    if gmail_query:
        params["q"] = gmail_query
    label_id = _label_id(folder)
    if label_id:
        params["labelIds"] = label_id
    if cursor:
        params["pageToken"] = cursor

    response = http("GET", f"{API}/messages?{urllib.parse.urlencode(params)}", _auth())
    listed = response.get("messages", []) or []
    messages: list[dict[str, Any]] = []
    for item in listed[: int(limit)]:
        message = http(
            "GET",
            f"{API}/messages/{item['id']}?format=metadata"
            "&metadataHeaders=From&metadataHeaders=To&metadataHeaders=Cc"
            "&metadataHeaders=Subject&metadataHeaders=Date"
            "&metadataHeaders=List-Unsubscribe&metadataHeaders=List-Unsubscribe-Post",
            _auth(),
        )
        messages.append(_norm(message))
    return {
        "provider": "gmail",
        "messages": messages,
        "next_cursor": response.get("nextPageToken", ""),
        "result_size_estimate": response.get("resultSizeEstimate", len(messages)),
    }


def list_messages(folder: str = "inbox", query: str = "", unread_only: bool = False, limit: int = 15) -> list[dict[str, Any]]:
    return list_messages_page(folder=folder, query=query, unread_only=unread_only, limit=limit)["messages"]


def _extract_body(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    if str(payload.get("mimeType", "")).startswith("text/plain"):
        data = (payload.get("body") or {}).get("data")
        if data:
            return base64.urlsafe_b64decode(str(data) + "==").decode("utf-8", "ignore")
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text
    return ""


def get_message(mid: str, body: bool = True) -> dict[str, Any]:
    message = http("GET", f"{API}/messages/{mid}?format={'full' if body else 'metadata'}", _auth())
    headers = (message.get("payload") or {}).get("headers", [])
    result = _norm(message)
    result["to"] = _hdr(headers, "To")
    if body:
        result["body"] = _extract_body(message.get("payload"))
    return result


def _current_labels(mid: str) -> list[str]:
    labels = http("GET", f"{API}/messages/{mid}?format=minimal", _auth()).get("labelIds", []) or []
    return [str(label) for label in labels]


def _modify(mid: str, add: list[str] | None = None, remove: list[str] | None = None) -> dict[str, Any]:
    return http("POST", f"{API}/messages/{mid}/modify", _auth(),
                {"addLabelIds": add or [], "removeLabelIds": remove or []})


def _restore_labels(mid: str, labels: list[str]) -> None:
    current = set(_current_labels(mid))
    desired = set(labels)
    _modify(mid, add=sorted(desired - current), remove=sorted(current - desired))


def move_message(mid: str, to_folder: str) -> dict[str, Any]:
    dest = _label_id(to_folder)
    before = _current_labels(mid)
    _modify(mid, add=[dest] if dest else [], remove=["INBOX"])
    audit({"provider": "gmail", "op": "move", "id": mid, "to": to_folder, "dest": dest, "before_labels": before})
    return {"ok": True, "moved_to": to_folder}


def tag_message(mid: str, labels: list[str] | str, mode: str = "add") -> dict[str, Any]:
    if mode not in {"add", "remove"}:
        raise ValueError("mode must be 'add' or 'remove'.")
    label_names = labels if isinstance(labels, list) else [labels]
    ids = [label_id for label_id in (_label_id(label) for label in label_names) if label_id]
    before = _current_labels(mid)
    if mode == "remove":
        _modify(mid, remove=ids)
    else:
        _modify(mid, add=ids)
    audit({"provider": "gmail", "op": "tag", "id": mid, "mode": mode, "labels": ids, "before_labels": before})
    return {"ok": True, "labels": ids, "mode": mode}


def mark_read(mid: str, read: bool = True) -> dict[str, Any]:
    before = _current_labels(mid)
    _modify(mid, remove=["UNREAD"] if read else None, add=None if read else ["UNREAD"])
    audit({"provider": "gmail", "op": "mark_read", "id": mid, "read": bool(read), "before_labels": before})
    return {"ok": True, "read": bool(read)}


def flag_message(mid: str, on: bool = True) -> dict[str, Any]:
    before = _current_labels(mid)
    _modify(mid, add=["STARRED"] if on else None, remove=None if on else ["STARRED"])
    audit({"provider": "gmail", "op": "flag", "id": mid, "on": bool(on), "before_labels": before})
    return {"ok": True, "starred": bool(on)}


def trash_message(mid: str) -> dict[str, Any]:
    before = _current_labels(mid)
    http("POST", f"{API}/messages/{mid}/trash", _auth())
    audit({"provider": "gmail", "op": "trash", "id": mid, "before_labels": before})
    return {"ok": True, "trashed": True}


def create_draft(to: str, subject: str, body: str, cc: str = "") -> dict[str, Any]:
    em = EmailMessage()
    em["To"] = to
    if cc:
        em["Cc"] = cc
    em["Subject"] = subject
    em.set_content(body)
    raw = base64.urlsafe_b64encode(em.as_bytes()).decode()
    r = http("POST", f"{API}/drafts", _auth(), {"message": {"raw": raw}})
    audit({"provider": "gmail", "op": "create_draft", "id": r.get("id"), "subject": subject})
    return {"ok": True, "draft_id": r.get("id")}


def undo(e: dict[str, Any]) -> bool:
    op, mid = e.get("op"), e.get("id")
    if not isinstance(mid, str):
        return False
    if op == "trash":
        http("POST", f"{API}/messages/{mid}/untrash", _auth())
        if e.get("before_labels"):
            _restore_labels(mid, e["before_labels"])
        return True
    if op == "move":
        _restore_labels(mid, e.get("before_labels", ["INBOX"]))
        return True
    if op == "flag":
        _restore_labels(mid, e.get("before_labels", []))
        return True
    if op == "mark_read":
        _restore_labels(mid, e.get("before_labels", []))
        return True
    if op == "tag":
        _restore_labels(mid, e.get("before_labels", []))
        return True
    return False


def _send_message(to: str, subject: str, body: str, cc: str = "") -> dict[str, Any]:
    em = EmailMessage()
    em["To"] = to
    if cc:
        em["Cc"] = cc
    em["Subject"] = subject
    em.set_content(body)
    raw = base64.urlsafe_b64encode(em.as_bytes()).decode()
    return http("POST", f"{API}/messages/send", _auth(), {"raw": raw})


def _permanent_delete(mid: str) -> dict[str, Any]:
    return http("DELETE", f"{API}/messages/{mid}", _auth())
