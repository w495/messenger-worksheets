#!/usr/bin/env python3
"""Exercise the local TLS RPC and one real Codex turn without printing secrets."""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import uuid

from messenger_worksheets.codex_rpc import submit


REQUIRED = {
    "WORKSHEET_CODEX_TRANSPORT_TOKEN",
    "WORKSHEET_CODEX_WORKER_CA",
    "WORKSHEET_CODEX_WORKER_URL",
}


def load_rpc_environment(env_file: Path) -> None:
    found: set[str] = set()
    for line in env_file.read_text(encoding="utf-8").splitlines():
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--task", action="store_true")
    args = parser.parse_args()
    load_rpc_environment(args.environment_file)
    job = {
        "text": "Создай файл out/hello.txt с точным текстом SANDBOX_OK",
        "task": True,
        "session": None,
        "images": [],
    } if args.task else {
        "text": "Ответь ровно одним словом: WORKER_OK",
        "task": False,
        "session": None,
        "images": [],
    }
    result = submit(
        os.environ["WORKSHEET_CODEX_WORKER_URL"],
        str(uuid.uuid4()),
        job,
    )
    if not isinstance(result, dict) or result.keys() != {"text", "session", "files"}:
        raise RuntimeError("worker returned an invalid response")
    if not isinstance(result["text"], str) or not result["text"].strip():
        raise RuntimeError("worker returned an empty response")
    if args.task:
        if result["session"] is not None or not isinstance(result["files"], list):
            raise RuntimeError("task smoke test returned invalid session/files")
        files = {
            item["name"]: base64.b64decode(item["data"], validate=True)
            for item in result["files"]
            if isinstance(item, dict) and set(item) == {"name", "data"}
        }
        if files.get("hello.txt", b"").strip() != b"SANDBOX_OK":
            raise RuntimeError("task smoke test did not return hello.txt")
        print("worker_task_smoke_test_ok file=hello.txt")
    else:
        if result["files"] != []:
            raise RuntimeError("chat smoke test unexpectedly returned files")
        uuid.UUID(result["session"])
        print(f"worker_smoke_test_ok text_chars={len(result['text'])} session_created=yes")


if __name__ == "__main__":
    main()
