#!/usr/bin/env python3
"""Diagnose OpenAI API and Codex CLI without printing credentials or raw bodies."""

from __future__ import annotations

import os
from pathlib import Path
import pwd
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


def resolve_native_codex(configured: str) -> str:
    path = Path(configured).resolve(strict=True)
    if path.suffix != ".js":
        return str(path)
    package = path.parent.parent
    candidates = sorted(package.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
    executable = [candidate for candidate in candidates if candidate.is_file() and os.access(candidate, os.X_OK)]
    if len(executable) != 1:
        raise RuntimeError("could not resolve exactly one native Codex binary")
    return str(executable[0])


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
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        process.communicate("Reply exactly CODEX_OK", timeout=45)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return "codex_cli=timeout"
    if process.returncode:
        return f"codex_cli=exit_{process.returncode}"
    return "codex_cli=ok"


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this diagnostic with sudo.")
    values = load_worker_environment()
    print(api_probe(values["WORKSHEET_CODEX_API_KEY"]))
    binary = resolve_native_codex(values["WORKSHEET_CODEX_BINARY"])
    print("codex_binary=native")
    print(codex_probe(binary, values["WORKSHEET_CODEX_API_KEY"]))


if __name__ == "__main__":
    main()
