"""Shared helpers for configuration, HTTP calls, audit logging, and dates."""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

PROJECT_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_DIR = pathlib.Path.home() / ".config" / "email-mcp"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(CONFIG_DIR, 0o700)
except OSError:
    pass
AUDIT_LOG = CONFIG_DIR / "actions.log"
HTTP_TIMEOUT_SECONDS = 30


def load_env() -> dict[str, str]:
    """Read KEY=VALUE pairs from ./.env; real environment overrides file."""
    env: dict[str, str] = {}
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in os.environ.items():
        if key.startswith(("GMAIL_", "OUTLOOK_")):
            env[key] = value
    return env


def http(
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    data: Mapping[str, Any] | None = None,
    form: bool = False,
) -> dict[str, Any]:
    headers = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(data).encode()
            headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, body, headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:600]
        raise RuntimeError(f"HTTP {exc.code} {method} {url.split('?')[0]} :: {detail}") from exc


def audit(entry: Mapping[str, Any]) -> None:
    entry = dict(entry)
    entry.setdefault("action_id", uuid.uuid4().hex)
    entry["ts"] = datetime.datetime.now().isoformat(timespec="microseconds")
    descriptor = os.open(AUDIT_LOG, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    try:
        os.chmod(AUDIT_LOG, 0o600)
    except OSError:
        pass


def read_audit() -> list[dict[str, Any]]:
    if not AUDIT_LOG.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def parse_date(value: object) -> float:
    if not value:
        return 0.0
    date_text = str(value)
    try:
        if "T" in date_text:
            return datetime.datetime.fromisoformat(date_text.replace("Z", "+00:00")).timestamp()
        return parsedate_to_datetime(date_text).timestamp()
    except Exception:
        return 0.0
