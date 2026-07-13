"""Outlook backend using a user-owned Outlook web session."""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from common import audit, http, load_env

BASE = "https://outlook.office.com/api/v2.0"
_cache: dict[str, str | float | None] = {"token": None, "exp": 0.0}

_JS = r"""
(function () {
  function dec(s){try{return JSON.parse(atob(s.replace(/-/g,'+').replace(/_/g,'/')
    .padEnd(s.length+(4-s.length%4)%4,'=')))}catch(e){return null}}
  function scan(store){
    if(!store)return null;
    for(var i=0;i<store.length;i++){
      var v;try{v=store.getItem(store.key(i))}catch(e){continue}
      if(typeof v!=='string')continue;
      var re=/eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+/g,m;
      while((m=re.exec(v))!==null){
        var p=dec(m[0].split('.')[1]);
        if(p && p.aud==='https://outlook.office.com' && p.scp &&
           p.scp.indexOf('Mail.Read')>=0){ return m[0]; }
      }
    }
    return null;
  }
  return scan(window.localStorage)||scan(window.sessionStorage)||"NONE";
})()
"""


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("Chrome DevTools closed the connection.")
        data += chunk
    return data


def _send_frame(sock: socket.socket, payload: bytes) -> None:
    mask = os.urandom(4)
    size = len(payload)
    if size < 126:
        header = bytes([0x81, 0x80 | size])
    elif size < 65536:
        header = bytes([0x81, 0xFE]) + size.to_bytes(2, "big")
    else:
        header = bytes([0x81, 0xFF]) + size.to_bytes(8, "big")
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    sock.sendall(header + mask + masked)


def _read_frame(sock: socket.socket) -> bytes:
    first, second = _read_exact(sock, 2)
    size = second & 0x7F
    if size == 126:
        size = int.from_bytes(_read_exact(sock, 2), "big")
    elif size == 127:
        size = int.from_bytes(_read_exact(sock, 8), "big")
    masked = bool(second & 0x80)
    mask = _read_exact(sock, 4) if masked else b""
    payload = _read_exact(sock, size)
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    if first & 0x0F == 0x8:
        raise RuntimeError("Chrome DevTools closed the connection.")
    return payload


def _evaluate(websocket_url: str, expression: str) -> str:
    parsed = urllib.parse.urlparse(websocket_url)
    if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Outlook browser bridge must use a local Chrome DevTools endpoint.")
    with socket.create_connection((parsed.hostname, parsed.port), timeout=10) as sock:
        sock.settimeout(15)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parsed.path or '/'} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode())
        if b" 101 " not in sock.recv(4096).split(b"\r\n", 1)[0]:
            raise RuntimeError("Could not connect to Chrome DevTools.")
        _send_frame(sock, json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True}}).encode())
        while True:
            response = json.loads(_read_frame(sock))
            if response.get("id") == 1:
                return str(((response.get("result") or {}).get("result") or {}).get("value") or "")


def _extract_token() -> str:
    env = load_env()
    port = env.get("OUTLOOK_DEBUG_PORT", "9222")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json", timeout=5) as response:
            tabs = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError(
            "Outlook browser bridge is unavailable. Start Chrome or Edge with remote debugging enabled, "
            "open outlook.office.com, then set OUTLOOK_DEBUG_PORT if you did not use 9222."
        ) from exc
    for tab in tabs:
        if tab.get("type") == "page" and "outlook" in str(tab.get("url", "")).lower():
            token = _evaluate(str(tab.get("webSocketDebuggerUrl", "")), _JS)
            if token and token != "NONE":
                return token
    raise RuntimeError("No signed-in Outlook web tab found in the browser bridge.")


def get_token() -> str:
    token = _cache["token"]
    expires_at = float(_cache["exp"] or 0)
    if isinstance(token, str) and expires_at - time.time() > 300:
        return token
    token = _extract_token()
    try:
        segment = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * ((4 - len(segment) % 4) % 4)))
        _cache["exp"] = float(payload.get("exp", time.time() + 1800))
    except (IndexError, ValueError, json.JSONDecodeError):
        _cache["exp"] = time.time() + 1800
    _cache["token"] = token
    return token


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer " + get_token(), "Accept": "application/json"}


def _recipients(rows: list[dict[str, Any]] | None) -> str:
    return ", ".join((row.get("EmailAddress") or {}).get("Address", "") for row in rows or [])


def _norm(m: dict[str, Any]) -> dict[str, Any]:
    frm = (m.get("From") or {}).get("EmailAddress") or {}
    flag = m.get("Flag") or {}
    return {
        "provider": "outlook",
        "id": m.get("Id"),
        "thread_id": m.get("ConversationId"),
        "from": f"{frm.get('Name','')} <{frm.get('Address','')}>".strip(),
        "to": _recipients(m.get("ToRecipients")),
        "cc": _recipients(m.get("CcRecipients")),
        "subject": m.get("Subject"),
        "date": m.get("ReceivedDateTime"),
        "unread": not m.get("IsRead", True),
        "has_attachment": bool(m.get("HasAttachments")),
        "importance": m.get("Importance") or "",
        "flag_status": flag.get("FlagStatus", ""),
        "categories": m.get("Categories") or [],
        "snippet": str(m.get("BodyPreview") or "")[:200],
        "folders": [m.get("ParentFolderId")] if m.get("ParentFolderId") else [],
    }


_WELL_KNOWN = {
    "inbox": "inbox", "drafts": "drafts", "draft": "drafts",
    "sent": "sentitems", "sentitems": "sentitems",
    "trash": "deleteditems", "deleted": "deleteditems", "deleteditems": "deleteditems",
    "junk": "junkemail", "spam": "junkemail", "junkemail": "junkemail", "archive": "archive",
}


def _folder_id(name: str) -> str:
    if not name:
        return "inbox"
    key = name.lower().replace(" ", "")
    if key in _WELL_KNOWN:
        return _WELL_KNOWN[key]
    for f in list_folders():
        if (f["name"] or "").lower() == name.lower():
            return f["id"]
    return name


def list_folders() -> list[dict[str, Any]]:
    data = http("GET", BASE + "/me/mailfolders?$top=100&$select=Id,DisplayName,UnreadItemCount,TotalItemCount", _auth())
    return [{"id": f["Id"], "name": f.get("DisplayName"),
             "unread": f.get("UnreadItemCount"), "total": f.get("TotalItemCount")}
            for f in data.get("value", [])]


def list_messages_page(
    folder: str = "inbox",
    query: str = "",
    unread_only: bool = False,
    limit: int = 15,
    cursor: str = "",
) -> dict[str, Any]:
    if cursor:
        data = http("GET", cursor, _auth())
        return {
            "provider": "outlook",
            "messages": [_norm(m) for m in data.get("value", [])],
            "next_cursor": data.get("@odata.nextLink", ""),
            "total_count": data.get("@odata.count"),
        }

    sel = "$select=Id,Subject,From,ToRecipients,CcRecipients,ReceivedDateTime,IsRead,BodyPreview,ParentFolderId,Categories,HasAttachments,Importance,ConversationId,Flag"
    top = f"$top={int(limit)}"
    if folder and folder.lower() in ("all", "allitems"):
        path = f"{BASE}/me/messages"
    else:
        path = f"{BASE}/me/mailfolders/{_folder_id(folder)}/messages"
    if query:
        qs = f"{top}&{sel}&$count=true&$search=" + urllib.parse.quote('"' + query + '"')
    else:
        parts = [top, sel, "$count=true", "$orderby=ReceivedDateTime%20desc"]
        if unread_only:
            parts.append("$filter=" + urllib.parse.quote("IsRead eq false"))
        qs = "&".join(parts)
    data = http("GET", f"{path}?{qs}", _auth())
    return {
        "provider": "outlook",
        "messages": [_norm(m) for m in data.get("value", [])],
        "next_cursor": data.get("@odata.nextLink", ""),
        "total_count": data.get("@odata.count"),
    }


def list_messages(folder: str = "inbox", query: str = "", unread_only: bool = False, limit: int = 15) -> list[dict[str, Any]]:
    return list_messages_page(folder=folder, query=query, unread_only=unread_only, limit=limit)["messages"]


def get_message(mid: str, body: bool = True) -> dict[str, Any]:
    sel = "Id,Subject,From,ToRecipients,ReceivedDateTime,IsRead,BodyPreview,ParentFolderId,Categories"
    if body:
        sel += ",Body"
    m = http("GET", f"{BASE}/me/messages/{mid}?$select={sel}", _auth())
    d = _norm(m)
    d["to"] = _recipients(m.get("ToRecipients"))
    d["categories"] = m.get("Categories") or []
    if body:
        b = m.get("Body") or {}
        d["body"] = b.get("Content", "")
        d["body_type"] = b.get("ContentType", "")
    return d


def _message_state(mid: str) -> dict[str, Any] | None:
    try:
        return http("GET", f"{BASE}/me/messages/{mid}?$select=ParentFolderId,IsRead,Categories,Flag", _auth())
    except Exception:
        return None


def _current_folder(mid: str) -> str | None:
    try:
        return http("GET", f"{BASE}/me/messages/{mid}?$select=ParentFolderId", _auth()).get("ParentFolderId")
    except Exception:
        return None


def move_message(mid: str, to_folder: str) -> dict[str, Any]:
    before = _current_folder(mid)
    r = http("POST", f"{BASE}/me/messages/{mid}/move", _auth(), {"DestinationId": _folder_id(to_folder)})
    audit({"provider": "outlook", "op": "move", "id": mid, "new_id": r.get("Id"),
           "to": to_folder, "from_folder": before})
    return {"ok": True, "new_id": r.get("Id"), "moved_to": to_folder}


def tag_message(mid: str, labels: list[str] | str, mode: str = "add") -> dict[str, Any]:
    if mode not in {"add", "remove", "set"}:
        raise ValueError("mode must be 'add', 'remove', or 'set'.")
    cats = labels if isinstance(labels, list) else [labels]
    m = http("GET", f"{BASE}/me/messages/{mid}?$select=Categories", _auth())
    cur = set(m.get("Categories") or [])
    if mode == "remove":
        new = cur - set(cats)
    elif mode == "set":
        new = set(cats)
    else:
        new = cur | set(cats)
    http("PATCH", f"{BASE}/me/messages/{mid}", _auth(), {"Categories": sorted(new)})
    audit({"provider": "outlook", "op": "tag", "id": mid, "before": sorted(cur), "after": sorted(new)})
    return {"ok": True, "categories": sorted(new)}


def mark_read(mid: str, read: bool = True) -> dict[str, Any]:
    before = _message_state(mid)
    http("PATCH", f"{BASE}/me/messages/{mid}", _auth(), {"IsRead": bool(read)})
    audit({
        "provider": "outlook",
        "op": "mark_read",
        "id": mid,
        "read": bool(read),
        "before_is_read": before.get("IsRead") if before else None,
    })
    return {"ok": True, "read": bool(read)}


def flag_message(mid: str, on: bool = True) -> dict[str, Any]:
    before = _message_state(mid)
    http("PATCH", f"{BASE}/me/messages/{mid}", _auth(),
         {"Flag": {"FlagStatus": "Flagged" if on else "NotFlagged"}})
    before_flag = (before.get("Flag") or {}).get("FlagStatus") if before else None
    audit({"provider": "outlook", "op": "flag", "id": mid, "on": bool(on), "before_flag_status": before_flag})
    return {"ok": True, "flagged": bool(on)}


def trash_message(mid: str) -> dict[str, Any]:
    before = _current_folder(mid)
    r = http("POST", f"{BASE}/me/messages/{mid}/move", _auth(), {"DestinationId": "deleteditems"})
    audit({"provider": "outlook", "op": "trash", "id": mid, "new_id": r.get("Id"), "from_folder": before})
    return {"ok": True, "new_id": r.get("Id"), "trashed": True}


def create_draft(to: str, subject: str, body: str, cc: str = "") -> dict[str, Any]:
    msg: dict[str, Any] = {
        "Subject": subject,
        "Body": {"ContentType": "Text", "Content": body},
        "ToRecipients": [{"EmailAddress": {"Address": a.strip()}} for a in to.split(",") if a.strip()],
    }
    if cc:
        msg["CcRecipients"] = [{"EmailAddress": {"Address": a.strip()}} for a in cc.split(",") if a.strip()]
    r = http("POST", f"{BASE}/me/messages", _auth(), msg)
    audit({"provider": "outlook", "op": "create_draft", "id": r.get("Id"), "subject": subject})
    return {"ok": True, "draft_id": r.get("Id")}


def undo(e: dict[str, Any]) -> bool:
    op = e.get("op")
    if op in ("move", "trash"):
        nid = e.get("new_id") or e.get("id")
        http("POST", f"{BASE}/me/messages/{nid}/move", _auth(),
             {"DestinationId": e.get("from_folder") or "inbox"})
        return True
    if op == "tag":
        http("PATCH", f"{BASE}/me/messages/{e['id']}", _auth(), {"Categories": e.get("before", [])})
        return True
    if op == "flag":
        http("PATCH", f"{BASE}/me/messages/{e['id']}", _auth(),
             {"Flag": {"FlagStatus": e.get("before_flag_status") or ("NotFlagged" if e.get("on") else "Flagged")}})
        return True
    if op == "mark_read":
        before_is_read = e.get("before_is_read")
        http(
            "PATCH",
            f"{BASE}/me/messages/{e['id']}",
            _auth(),
            {"IsRead": before_is_read if isinstance(before_is_read, bool) else not e.get("read")},
        )
        return True
    return False


def _send_message(to: str, subject: str, body: str, cc: str = "") -> dict[str, Any]:
    msg: dict[str, Any] = {
        "Message": {
            "Subject": subject,
            "Body": {"ContentType": "Text", "Content": body},
            "ToRecipients": [{"EmailAddress": {"Address": a.strip()}} for a in to.split(",") if a.strip()],
        }
    }
    if cc:
        msg["Message"]["CcRecipients"] = [{"EmailAddress": {"Address": a.strip()}} for a in cc.split(",") if a.strip()]
    return http("POST", f"{BASE}/me/sendmail", _auth(), msg)


def _permanent_delete(mid: str) -> dict[str, Any]:
    return http("DELETE", f"{BASE}/me/messages/{mid}", _auth())


def _empty_trash() -> None:
    raise NotImplementedError("disabled by design")
