"""Prepare and verify the platform Outlook token source."""

from __future__ import annotations

import sys

from outlook_tokens import extract_token, provider_name, setup_token_source


def main() -> None:
    provider = provider_name()
    print(f"Outlook provider: {provider}")
    print(setup_token_source())
    try:
        extract_token()
    except RuntimeError as exc:
        sys.exit(str(exc))
    print("Outlook token access verified.")


if __name__ == "__main__":
    main()
