#!/usr/bin/env python3
"""Diagnose OpenAI API and Codex CLI without printing credentials or raw bodies."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


ENV_FILE = Path("/etc/messenger-worksheets/worker.env")
STATE_DIR = Path("/var/lib/messenger-worksheets-worker")
REQUIRED = {"WORKSHEET_CODEX_API_KEY", "WORKSHEET_CODEX_BINARY"}
DISABLED = (
    "shell_tool", "unified_exec", "hooks", "plugins", "multi_agent",
    "multi_agent_v2", "image_generation", "view_image", "skill_search",
    "skill_mcp_dependency_install",
)


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


def api_probe(key: str) -> str:
    request = Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": "Bearer " + key},
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
            return f"openai_api_http={response.status}"
    except HTTPError as error:
        return f"openai_api_http={error.code}"
    except URLError as error:
        return f"openai_api_network_error={type(error.reason).__name__}"


def classify_diagnostic(stderr: str, stdout: str, returncode: int) -> str:
    diagnostic = (stderr + "\n" + stdout).casefold()
    categories = (
        ("authentication", r"\b401\b|unauthorized|invalid[_ ]api[_ ]key|authentication"),
        ("forbidden", r"\b403\b|forbidden"),
        ("quota_or_rate_limit", r"\b429\b|insufficient_quota|rate limit|usage limit|billing"),
        ("model_unavailable", r"model.*(?:not found|does not exist|not available|access)"),
        ("connection", r"failed to connect|connection refused|stream disconnected|request timed out"),
        ("configuration", r"error loading config|unknown field|invalid value"),
    )
    for name, pattern in categories:
        if re.search(pattern, diagnostic):
            return name
    return f"exit_{returncode}"


def codex_probe(binary: str, key: str) -> str:
    account = pwd.getpwnam("worksheet-worker")
    command = [
        "/usr/bin/setpriv",
        f"--reuid={account.pw_uid}",
        f"--regid={account.pw_gid}",
        "--clear-groups",
        "--",
        binary, "exec", "--ignore-user-config", "--json", "--skip-git-repo-check",
        "-c", 'approval_policy="never"',
        "-c", 'web_search="disabled"',
        "-c", "project_doc_max_bytes=0",
        "-c", 'shell_environment_policy.inherit="none"',
        "-c", 'developer_instructions="Reply exactly CODEX_OK"',
        "-c", 'model_provider="openai"',
        "--sandbox", "read-only",
    ]
    for feature in DISABLED:
        command.extend(("--disable", feature))
    command.append("-")
    environment = {
        "HOME": str(STATE_DIR),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "OPENAI_API_KEY": key,
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
        stdout, stderr = process.communicate("Reply exactly CODEX_OK", timeout=75)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return "codex_cli=timeout"
    if process.returncode:
        return "codex_cli=" + classify_diagnostic(stderr, stdout, process.returncode)
    try:
        events = [json.loads(line) for line in stdout.splitlines()]
    except (TypeError, ValueError):
        return "codex_cli=invalid_json"
    completed = any(event.get("type") == "turn.completed" for event in events)
    replied = any(
        event.get("type") == "item.completed"
        and event.get("item", {}).get("type") == "agent_message"
        and bool(event.get("item", {}).get("text", "").strip())
        for event in events
    )
    return "codex_cli=ok" if completed and replied else "codex_cli=incomplete"


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this diagnostic with sudo.")
    values = load_worker_environment()
    print(api_probe(values["WORKSHEET_CODEX_API_KEY"]))
    print(codex_probe(values["WORKSHEET_CODEX_BINARY"], values["WORKSHEET_CODEX_API_KEY"]))


if __name__ == "__main__":
    main()
