"""Mint a Gmail refresh token with the installed-app loopback OAuth flow."""

from __future__ import annotations

import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

from common import PROJECT_DIR, load_env

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
PORT = 8765
REDIRECT = f"http://localhost:{PORT}"
result: dict[str, str] = {}


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        result.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Done. Return to the terminal.</h2>")

    def log_message(self, _format: str, *args: Any) -> None:
        pass


def _exchange_code(client_id: str, client_secret: str, code: str) -> dict[str, Any]:
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": REDIRECT,
                "grant_type": "authorization_code",
            }
        ).encode(),
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _write_refresh_token(refresh_token: str) -> None:
    env_file = PROJECT_DIR / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    out: list[str] = []
    found = False
    for line in lines:
        if line.startswith("GMAIL_REFRESH_TOKEN="):
            out.append(f"GMAIL_REFRESH_TOKEN={refresh_token}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"GMAIL_REFRESH_TOKEN={refresh_token}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(env_file, 0o600)


def main() -> None:
    env = load_env()
    client_id = env.get("GMAIL_CLIENT_ID")
    client_secret = env.get("GMAIL_CLIENT_SECRET")
    if not (client_id and client_secret):
        sys.exit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.")

    server = http.server.HTTPServer(("127.0.0.1", PORT), OAuthCallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    state = secrets.token_hex(8)
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    print("Opening browser for Google consent...\n", auth_url)
    webbrowser.open(auth_url)

    for _ in range(180):
        if result:
            break
        time.sleep(1)
    if result.get("state") != state or "code" not in result:
        sys.exit("Gmail authorization failed, was denied, or timed out.")

    token_response = _exchange_code(client_id, client_secret, result["code"])
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        sys.exit("No refresh token returned. Revoke the prior grant and retry.")

    _write_refresh_token(str(refresh_token))
    print(f"Refresh token written to {PROJECT_DIR / '.env'} (not shown). Scope: {SCOPE}")


if __name__ == "__main__":
    main()
