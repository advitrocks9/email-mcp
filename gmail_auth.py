"""Mint a Gmail refresh token with the installed-app loopback OAuth flow."""

from __future__ import annotations

import http.server
import json
import base64
import hashlib
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

from common import env_file, load_env, set_env_value

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
result: dict[str, str] = {}


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        result.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Done. Return to the terminal.</h2>")

    def log_message(self, _format: str, *args: Any) -> None:
        pass


def _exchange_code(client_id: str, client_secret: str, code: str, redirect: str, verifier: str) -> dict[str, Any]:
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }).encode(),
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> None:
    env = load_env()
    client_id = env.get("GMAIL_CLIENT_ID")
    client_secret = env.get("GMAIL_CLIENT_SECRET")
    if not (client_id and client_secret):
        sys.exit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.")

    server = http.server.HTTPServer(("127.0.0.1", 0), OAuthCallbackHandler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.handle_request, daemon=True).start()
    state = secrets.token_hex(8)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Opening browser for Google consent...\n", auth_url)
    webbrowser.open(auth_url)

    for _ in range(180):
        if result:
            break
        time.sleep(1)
    if result.get("state") != state or "code" not in result:
        sys.exit(f"auth failed: {result}")

    token_response = _exchange_code(client_id, client_secret, result["code"], redirect, verifier)
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        sys.exit(f"No refresh_token returned. Revoke the prior grant and retry: {token_response}")

    set_env_value("GMAIL_REFRESH_TOKEN", str(refresh_token))
    print(f"Refresh token written to {env_file()} (not shown). Scope: {SCOPE}")


if __name__ == "__main__":
    main()
