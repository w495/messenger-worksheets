#!/usr/bin/env python3
"""Validate and atomically replace the Messenger OAuth without printing it."""

from __future__ import annotations

import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile


ETC = Path("/etc/messenger-worksheets")
TARGETS = (
    ETC / "gateway.env",
    ETC / "gateway-api.env",
    ETC / "gateway-local-user.env",
)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError("invalid environment value")
            stream.write(f"{key}={value}\n")
        temporary = Path(stream.name)
    try:
        os.chmod(temporary, 0o600)
        os.chown(temporary, 0, 0)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this OAuth updater with sudo.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    expected_email = config.get("expected_email")
    if not isinstance(expected_email, str):
        raise ValueError("instance config has no expected_email")
    legacy = read_env(ETC / "gateway.env")
    token = getpass("Personal Yandex Messenger OAuth: ")
    if not token or any(char.isspace() for char in token):
        raise ValueError("OAuth is empty or contains whitespace")

    client_dir = Path(legacy["WORKSHEET_MESSENGER_CLIENT_DIR"]).resolve(strict=True)
    sys.path.insert(0, str(client_dir))
    from client import MessengerUserClient

    client = MessengerUserClient(
        token,
        account="personal",
        expected_email=expected_email,
        timeout=12,
    )
    client.identity()
    for path in TARGETS:
        if not path.exists():
            continue
        values = read_env(path)
        values["WORKSHEET_MESSENGER_TOKEN"] = token
        values["WORKSHEET_MESSENGER_EMAIL"] = expected_email
        write_env(path, values)
    print(f"messenger_oauth_updated expected_email={expected_email} account=personal")


if __name__ == "__main__":
    main()
