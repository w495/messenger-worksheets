#!/usr/bin/env python3
"""Exercise the local TLS RPC and one real Codex turn without printing secrets."""

from __future__ import annotations

import os
from pathlib import Path
import uuid

from messenger_worksheets.codex_rpc import submit


ENV_FILE = Path("/etc/messenger-worksheets/gateway.env")
REQUIRED = {
    "WORKSHEET_CODEX_TRANSPORT_TOKEN",
    "WORKSHEET_CODEX_WORKER_CA",
    "WORKSHEET_CODEX_WORKER_URL",
}


def load_rpc_environment() -> None:
    found: set[str] = set()
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key in REQUIRED:
            os.environ[key] = value
            found.add(key)
    missing = REQUIRED - found
    if missing:
        raise RuntimeError("gateway environment is incomplete")


def main() -> None:
    load_rpc_environment()
    result = submit(
        os.environ["WORKSHEET_CODEX_WORKER_URL"],
        str(uuid.uuid4()),
        {
            "text": "Ответь ровно одним словом: WORKER_OK",
            "task": False,
            "session": None,
            "images": [],
        },
    )
    if not isinstance(result, dict) or result.keys() != {"text", "session", "files"}:
        raise RuntimeError("worker returned an invalid response")
    if not isinstance(result["text"], str) or not result["text"].strip():
        raise RuntimeError("worker returned an empty response")
    if result["files"] != []:
        raise RuntimeError("chat smoke test unexpectedly returned files")
    uuid.UUID(result["session"])
    print(f"worker_smoke_test_ok text_chars={len(result['text'])} session_created=yes")


if __name__ == "__main__":
    main()
