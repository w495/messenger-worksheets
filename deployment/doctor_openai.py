#!/usr/bin/env python3
"""Run the official redacted Codex network doctor for the API-key worker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import signal
import subprocess

from diagnose_openai import load_worker_environment, resolve_native_codex


STATE_DIR = Path("/var/lib/messenger-worksheets-worker")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this diagnostic with sudo.")
    values = load_worker_environment()
    binary = resolve_native_codex(values["WORKSHEET_CODEX_BINARY"])
    account = pwd.getpwnam("worksheet-worker")
    command = [
        "/usr/bin/setpriv",
        f"--reuid={account.pw_uid}",
        f"--regid={account.pw_gid}",
        "--clear-groups",
        "--",
        binary,
        "doctor",
        "--json",
        "--network-only",
        "-c", 'model="gpt-5.3-codex"',
        "-c", 'model_provider="openai"',
    ]
    environment = {
        "HOME": str(STATE_DIR),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "OPENAI_API_KEY": values["WORKSHEET_CODEX_API_KEY"],
    }
    process = subprocess.Popen(
        command,
        cwd=STATE_DIR,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(timeout=75)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise SystemExit("codex_doctor=timeout")
    try:
        report = json.loads(stdout)
    except (TypeError, ValueError):
        raise SystemExit(f"codex_doctor=invalid_json exit={process.returncode}") from None
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if process.returncode:
        raise SystemExit(f"codex_doctor=failed exit={process.returncode}")


if __name__ == "__main__":
    main()
