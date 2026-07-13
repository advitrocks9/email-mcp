"""Platform-specific token access for a signed-in Outlook web session."""

from __future__ import annotations

import json
import os
import pathlib
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from websocket import create_connection

from common import CONFIG_DIR, load_env

OWA_URL = "https://outlook.office.com/mail/"
TOKEN_MISSING = {"", "NONE", "NO_OWA_TAB"}
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


def _safari_run_js() -> str:
    script_path = CONFIG_DIR / "owa_token.js"
    script_path.write_text(_JS, encoding="utf-8")
    apple_script = f'''
set jsCode to (read POSIX file "{script_path}" as «class utf8»)
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
    result = subprocess.run(["osascript", "-e", apple_script], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Safari automation failed: "
            + result.stderr.strip()
            + " [Enable Develop > Allow JavaScript from Apple Events and grant Automation permission.]"
        )
    return result.stdout.strip()


def _open_safari() -> None:
    subprocess.run(["open", "-g", "-a", "Safari", OWA_URL], check=False)


def _safari_token() -> str:
    token = _safari_run_js()
    if token == "NO_OWA_TAB":
        _open_safari()
    deadline = time.monotonic() + 45
    while token in TOKEN_MISSING and time.monotonic() < deadline:
        time.sleep(3)
        token = _safari_run_js()
    if token in TOKEN_MISSING:
        raise RuntimeError(f"No Outlook token found. Sign in at {OWA_URL} in Safari and retry.")
    return token


def _edge_executable() -> pathlib.Path:
    env = load_env()
    configured = env.get("OUTLOOK_EDGE_PATH")
    candidates = [pathlib.Path(configured)] if configured else []
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        if root := os.environ.get(variable):
            candidates.append(pathlib.Path(root) / "Microsoft/Edge/Application/msedge.exe")
    discovered = shutil.which("msedge")
    if discovered:
        candidates.append(pathlib.Path(discovered))
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise RuntimeError("Microsoft Edge was not found. Set OUTLOOK_EDGE_PATH in .env to msedge.exe.")


def _edge_profile() -> pathlib.Path:
    configured = load_env().get("OUTLOOK_EDGE_PROFILE")
    return pathlib.Path(configured).expanduser() if configured else CONFIG_DIR / "edge-profile"


def _active_port(profile: pathlib.Path) -> tuple[int, str] | None:
    path = profile / "DevToolsActivePort"
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return int(lines[0]), lines[1]
    except (IndexError, OSError, ValueError):
        return None


def _targets(port: int) -> list[dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
            value = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _launch_edge(profile: pathlib.Path) -> tuple[int, str]:
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        str(_edge_executable()),
        "--no-first-run",
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        OWA_URL,
    ]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        active = _active_port(profile)
        if active and _targets(active[0]):
            return active
        time.sleep(0.5)
    raise RuntimeError("Edge started but its local DevTools endpoint did not become ready.")


def _edge_endpoint() -> tuple[int, str]:
    profile = _edge_profile()
    active = _active_port(profile)
    if active and _targets(active[0]):
        return active
    return _launch_edge(profile)


def _cdp_call(websocket_url: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    connection = create_connection(websocket_url, timeout=15, suppress_origin=True)
    try:
        connection.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            response = json.loads(connection.recv())
            if response.get("id") != 1:
                continue
            if response.get("error"):
                raise RuntimeError(f"Edge DevTools error: {response['error']}")
            return response.get("result") or {}
    finally:
        connection.close()


def _open_edge_tab(port: int, browser_path: str) -> None:
    _cdp_call(f"ws://127.0.0.1:{port}{browser_path}", "Target.createTarget", {"url": OWA_URL})


def _edge_token() -> str:
    port, browser_path = _edge_endpoint()
    deadline = time.monotonic() + 60
    opened = False
    while time.monotonic() < deadline:
        tabs = [
            target
            for target in _targets(port)
            if target.get("type") == "page" and "outlook" in str(target.get("url", "")).lower()
        ]
        if not tabs and not opened:
            _open_edge_tab(port, browser_path)
            opened = True
        for tab in tabs:
            result = _cdp_call(
                str(tab["webSocketDebuggerUrl"]),
                "Runtime.evaluate",
                {"expression": _JS, "returnByValue": True},
            )
            token = str((result.get("result") or {}).get("value") or "")
            if token not in TOKEN_MISSING:
                return token
        time.sleep(2)
    raise RuntimeError(f"No Outlook token found. Sign in at {OWA_URL} in the Edge window and retry.")


def provider_name() -> str:
    configured = load_env().get("OUTLOOK_TOKEN_PROVIDER", "auto").lower()
    if configured != "auto":
        if configured not in {"safari", "edge"}:
            raise RuntimeError("OUTLOOK_TOKEN_PROVIDER must be auto, safari, or edge.")
        return configured
    system = platform.system()
    if system == "Darwin":
        return "safari"
    if system == "Windows":
        return "edge"
    raise RuntimeError("Outlook token access supports macOS and Windows only.")


def extract_token() -> str:
    return _safari_token() if provider_name() == "safari" else _edge_token()


def setup_token_source() -> str:
    provider = provider_name()
    if provider == "edge":
        _edge_endpoint()
        return f"Edge is ready. Sign in at {OWA_URL}, then run this command again to verify the token."
    _open_safari()
    return "Safari is open. Sign in to Outlook and enable Develop > Allow JavaScript from Apple Events."
