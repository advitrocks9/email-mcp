"""Read-only backend smoke test."""

from __future__ import annotations

from typing import Protocol

import gmail
import outlook


class ReadableMailbox(Protocol):
    def list_messages(self, folder: str = "inbox", query: str = "", unread_only: bool = False, limit: int = 15) -> list[dict]: ...


def _print_sample(name: str, backend: ReadableMailbox) -> None:
    print(f"\n===== {name} =====")
    try:
        messages = backend.list_messages(folder="inbox", limit=5)
        print(f"inbox sample ({len(messages)}):")
        for message in messages:
            dot = "*" if message.get("unread") else " "
            date = str(message.get("date") or "")[:25]
            sender = str(message.get("from") or "")[:34]
            subject = str(message.get("subject") or "")[:46]
            print(f"  {dot} {date:25.25}  {sender:34.34}  {subject}")
    except Exception as exc:
        print(f"  unavailable: {exc}")


def main() -> None:
    for name, backend in (("OUTLOOK", outlook), ("GMAIL", gmail)):
        _print_sample(name, backend)


if __name__ == "__main__":
    main()
