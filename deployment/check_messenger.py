#!/usr/bin/env python3
"""Verify the configured personal Messenger identity without sending messages."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


REQUIRED = {
    "WORKSHEET_MESSENGER_CLIENT_DIR",
    "WORKSHEET_MESSENGER_TOKEN",
}


def load_gateway_environment(env_file: Path) -> None:
    found: set[str] = set()
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key in REQUIRED:
            os.environ[key] = value
            found.add(key)
    if found != REQUIRED:
        raise RuntimeError("gateway environment is incomplete")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this identity check with sudo.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-file", type=Path, required=True)
    args = parser.parse_args()
    load_gateway_environment(args.environment_file)
    from messenger_codex.bridge import timestamp_now
    from messenger_worksheets.personal_messenger import CHAT, EMAIL, PersonalMessenger

    messenger = PersonalMessenger()
    future = timestamp_now() + 3_600_000_000
    if messenger.new_messages(parent=0, after=future):
        raise RuntimeError("future-only Messenger probe unexpectedly returned messages")
    print(f"messenger_identity_ok expected_email={EMAIL} chat_id={CHAT}")


if __name__ == "__main__":
    main()
