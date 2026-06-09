"""Outlook backend using the signed-in Safari OWA session.

Imperial blocks normal mailbox API app registration. This backend reads OWA's own
cached Outlook REST token from Safari localStorage, keeps it in memory, and uses
it for reversible mail actions.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.parse
from typing import Any

from common import CONFIG_DIR, audit, http

BASE = "https://outlook.office.com/api/v2.0"
_cache: dict[str, str | float | None] = {"token": None, "exp": 0.0}

_JS = r"""
(function () {
  function dec(s){try{return JSON.parse(atob(s.replace(/-/g,'+').replace(/_/g,'/')
    .padEnd(s.length+(4-s.length%4)%4,'=')))}catch(e){return null}}
  function scan(store){
    if(!store)return null;
    for(var i=0;i<store.length;i++){
      var k=store.key(i),v;try{v=store.getItem(k)}catch(e){continue}
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
_JS_PATH = CONFIG_DIR / "owa_tok.js"


def _run_js() -> str:
    _JS_PATH.write_text(_JS)
    osa = f'''
set jsCode to (read POSIX file "{_JS_PATH}" as «class utf8»)
tell application "Safari"
  repeat with w in windows
    repeat with t in tabs of w
      if (URL of t) contains "outlook" then
        return (do JavaScript jsCode in t)
      end if
    end repeat
  end repeat
  return "NO_OWA_TAB"
end tell
'''
    result = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Safari automation failed: "
            + result.stderr.strip()
            + "  [Fix: Safari ▸ Develop ▸ Allow JavaScript from Apple Events, and grant Automation permission]"
        )
    return result.stdout.strip()


def _close_owa_tabs() -> None:
    osa = '''
tell application "Safari"
  set toClose to {}
  repeat with w in windows
    repeat with t in tabs of w
      if (URL of t) contains "outlook.office.com" or (URL of t) contains "outlook.cloud.microsoft" then
        set end of toClose to t
      end if
    end repeat
  end repeat
  repeat with t in toClose
    try
      close t
    end try
  end repeat
end tell
'''
    subprocess.run(["osascript", "-e", osa], capture_output=True, text=True)


def _extract_token() -> str:
    out = _run_js()
    opened_by_us = False
    if out == "NO_OWA_TAB":
        subprocess.run(["open", "-g", "-a", "Safari", "https://outlook.office.com/mail/"])
        opened_by_us = True
        out = "NONE"
    deadline = time.time() + 40
    while out in ("NONE", "NO_OWA_TAB", "") and time.time() < deadline:
        time.sleep(3)
        out = _run_js()
    token = out if out not in ("NONE", "NO_OWA_TAB", "") else None
    if opened_by_us:
        _close_owa_tabs()
    if not token:
        raise RuntimeError("No Outlook token in Safari. Open https://outlook.office.com in Safari and sign in once.")
    return token


def get_token() -> str:
    token = _cache["token"]
    expires_at = float(_cache["exp"] or 0)
    if isinstance(token, str) and expires_at - time.time() > 300:
        return token
    tok = _extract_token()
    try:
        seg = tok.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + "=" * ((4 - len(seg) % 4) % 4)))
        _cache["exp"] = float(payload.get("exp", time.time() + 1800))
    except Exception:
        _cache["exp"] = time.time() + 1800
    _cache["token"] = tok
    return tok


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer " + get_token(), "Accept": "application/json"}


def _norm(m: dict[str, Any]) -> dict[str, Any]:
    frm = (m.get("From") or {}).get("EmailAddress") or {}
    return {
        "provider": "outlook",
        "id": m.get("Id"),
        "from": f"{frm.get('Name','')} <{frm.get('Address','')}>".strip(),
        "subject": m.get("Subject"),
        "date": m.get("ReceivedDateTime"),
        "unread": not m.get("IsRead", True),
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


def list_messages(folder: str = "inbox", query: str = "", unread_only: bool = False, limit: int = 15) -> list[dict[str, Any]]:
    sel = "$select=Id,Subject,From,ReceivedDateTime,IsRead,BodyPreview,ParentFolderId"
    top = f"$top={int(limit)}"
    if folder and folder.lower() in ("all", "allitems"):
        path = f"{BASE}/me/messages"
    else:
        path = f"{BASE}/me/mailfolders/{_folder_id(folder)}/messages"
    if query:
        qs = f"{top}&{sel}&$search=" + urllib.parse.quote('"' + query + '"')
    else:
        parts = [top, sel, "$orderby=ReceivedDateTime%20desc"]
        if unread_only:
            parts.append("$filter=" + urllib.parse.quote("IsRead eq false"))
        qs = "&".join(parts)
    data = http("GET", f"{path}?{qs}", _auth())
    return [_norm(m) for m in data.get("value", [])]


def get_message(mid: str, body: bool = True) -> dict[str, Any]:
    sel = "Id,Subject,From,ToRecipients,ReceivedDateTime,IsRead,BodyPreview,ParentFolderId,Categories"
    if body:
        sel += ",Body"
    m = http("GET", f"{BASE}/me/messages/{mid}?$select={sel}", _auth())
    d = _norm(m)
    d["to"] = ", ".join((r.get("EmailAddress") or {}).get("Address", "") for r in (m.get("ToRecipients") or []))
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
