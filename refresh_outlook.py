"""Pre-warm the Outlook token for the optional LaunchAgent."""

from __future__ import annotations

import outlook


def main() -> None:
    try:
        outlook.get_token()
        print("outlook token refreshed")
    except Exception as exc:
        print("refresh failed:", exc)


if __name__ == "__main__":
    main()
