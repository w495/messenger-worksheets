#!/usr/bin/env python3
"""Log the isolated worker into Codex using the root-only API key."""

from __future__ import annotations

import os
from pathlib import Path
import pwd
import signal
import subprocess


ENV_FILE = Path("/etc/messenger-worksheets/worker.env")
STATE_DIR = Path("/var/lib/messenger-worksheets-worker")
REQUIRED = {"WORKSHEET_CODEX_API_KEY", "WORKSHEET_CODEX_BINARY"}


def load_worker_environment() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key in REQUIRED:
            values[key] = value
    if values.keys() != REQUIRED:
        raise RuntimeError("worker environment is incomplete")
    return values


def worker_command(binary: str, *arguments: str, stdin: str | None = None) -> int:
    account = pwd.getpwnam("worksheet-worker")
    command = [
        "/usr/bin/setpriv",
        f"--reuid={account.pw_uid}",
        f"--regid={account.pw_gid}",
        "--clear-groups",
        "--",
        binary,
        *arguments,
    ]
    environment = {
        "HOME": str(STATE_DIR),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    process = subprocess.Popen(
        command,
        cwd=STATE_DIR,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        process.communicate(stdin, timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return 124
    return process.returncode


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this login helper with sudo.")
    values = load_worker_environment()
    binary = values["WORKSHEET_CODEX_BINARY"]
    status = worker_command(
        binary,
        "login",
        "--with-api-key",
        stdin=values["WORKSHEET_CODEX_API_KEY"] + "\n",
    )
    if status != 0:
        raise SystemExit(f"codex_login=failed exit={status}")
    print("codex_login=ok")
    status = worker_command(binary, "login", "status")
    if status != 0:
        raise SystemExit(f"codex_login_status=failed exit={status}")
    print("codex_login_status=authenticated")


if __name__ == "__main__":
    main()
