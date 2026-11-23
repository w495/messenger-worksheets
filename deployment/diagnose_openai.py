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
from tempfile import TemporaryFile
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


ENV_FILE = Path("/etc/messenger-worksheets/worker.env")
STATE_DIR = Path("/var/lib/messenger-worksheets-worker")
REQUIRED = {"WORKSHEET_CODEX_API_KEY", "WORKSHEET_CODEX_BINARY"}
TARGET_MODEL = "gpt-5.3-codex"
TARGET_REASONING_EFFORT = "high"
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


def api_probe(key: str) -> tuple[str, list[str]]:
    request = Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": "Bearer " + key},
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
            body = response.read(2 * 1024 * 1024)
            payload = json.loads(body)
            models = [
                item["id"]
                for item in payload.get("data", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            return f"openai_api_http={response.status}", models
    except HTTPError as error:
        return f"openai_api_http={error.code}", []
    except URLError as error:
        return f"openai_api_network_error={type(error.reason).__name__}", []


def relevant_text_models(models: list[str]) -> list[str]:
    excluded = ("audio", "image", "realtime", "search", "transcribe", "tts")
    return sorted(
        model for model in models
        if model.startswith("gpt-")
        and not any(fragment in model for fragment in excluded)
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model)
    )


def safe_identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
        return "unknown"
    return value


def safe_rate_headers(headers: object) -> str:
    values = []
    for name in (
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
    ):
        value = headers.get(name) if hasattr(headers, "get") else None
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9. -]{1,100}", value):
            values.append(name.removeprefix("x-ratelimit-").replace("-", "_") + "=" + value.replace(" ", "_"))
    return " ".join(values)


def responses_probe(key: str, model: str) -> str:
    body = json.dumps({
        "model": model,
        "input": "Reply exactly API_OK",
        "max_output_tokens": 128,
        "reasoning": {"effort": TARGET_REASONING_EFFORT},
        "store": False,
    }).encode()
    request = Request(
        "https://api.openai.com/v1/responses",
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=45) as response:
            response.read(1024 * 1024)
            limits = safe_rate_headers(response.headers)
            return f"responses_api_http={response.status}" + (" " + limits if limits else "")
    except HTTPError as error:
        raw = error.read(256 * 1024)
        try:
            details = json.loads(raw).get("error", {})
        except (TypeError, ValueError):
            details = {}
        error_type = safe_identifier(details.get("type"))
        error_code = safe_identifier(details.get("code"))
        limits = safe_rate_headers(error.headers)
        result = f"responses_api_http={error.code} error_type={error_type} error_code={error_code}"
        return result + (" " + limits if limits else "")
    except URLError as error:
        return f"responses_api_network_error={type(error.reason).__name__}"


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


def classify_diagnostic(stderr: str, stdout: str, returncode: int) -> str:
    diagnostic = (stderr + "\n" + stdout).casefold()
    categories = (
        ("login_required", r"not logged in|codex login|login required"),
        ("authentication", r"\b401\b|unauthorized|invalid[_ ]api[_ ]key|authentication"),
        ("forbidden", r"\b403\b|forbidden"),
        ("quota_or_rate_limit", r"\b429\b|insufficient_quota|rate limit|usage limit|billing"),
        ("model_unavailable", r"model.*(?:not found|does not exist|not available|not supported|access)"),
        ("connection", r"failed to connect|connection refused|stream disconnected|request timed out"),
        ("configuration", r"error loading config|unknown field|invalid value|no model selected"),
        ("permission", r"permission denied|operation not permitted"),
    )
    for name, pattern in categories:
        if re.search(pattern, diagnostic):
            return name
    return f"unknown_exit_{returncode}"


def structured_signals(stderr: str, stdout: str) -> str:
    codes: set[str] = set()
    types: set[str] = set()
    statuses: set[str] = set()

    def visit(value: object, in_error: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                child_error = in_error or key == "error"
                if in_error and key == "code" and safe_identifier(item) != "unknown":
                    codes.add(str(item))
                elif in_error and key == "type" and safe_identifier(item) != "unknown":
                    types.add(str(item))
                elif in_error and key in {"status", "status_code", "http_status"} and isinstance(item, int):
                    statuses.add(str(item))
                visit(item, child_error)
        elif isinstance(value, list):
            for item in value:
                visit(item, in_error)

    for line in stdout.splitlines():
        try:
            visit(json.loads(line))
        except (TypeError, ValueError):
            continue
    lowered = (stderr + "\n" + stdout).casefold()
    for token in ("insufficient_quota", "rate_limit_exceeded", "usage_limit_reached", "model_not_found"):
        if token in lowered:
            codes.add(token)
    for status in re.findall(r"(?<!\d)(401|403|404|429|500|502|503)(?!\d)", lowered):
        statuses.add(status)
    dimensions = []
    for name, pattern in (
        ("tpm", r"tokens per min|\btpm\b"),
        ("rpm", r"requests per min|\brpm\b"),
        ("tpd", r"tokens per day|\btpd\b"),
        ("rpd", r"requests per day|\brpd\b"),
    ):
        if re.search(pattern, lowered):
            dimensions.append(name)
    numbers = []
    for label in ("limit", "used", "requested"):
        match = re.search(rf"\b{label}\s*[:=]?\s*(\d+)", lowered)
        if match:
            numbers.append(f"{label}={match.group(1)}")
    parts = []
    if codes:
        parts.append("codes=" + ",".join(sorted(codes)))
    if types:
        parts.append("types=" + ",".join(sorted(types)))
    if statuses:
        parts.append("statuses=" + ",".join(sorted(statuses)))
    if dimensions:
        parts.append("dimensions=" + ",".join(dimensions))
    parts.extend(numbers)
    return " ".join(parts) if parts else "signals=none"


def codex_probe(binary: str, key: str, model: str, *, direct: bool = False) -> str:
    account = pwd.getpwnam("worksheet-worker")
    provider_config = ["-c", 'model_provider="openai"']
    if direct:
        provider_config = [
            "-c", 'model_provider="direct_openai"',
            "-c", 'model_providers.direct_openai.name="Direct OpenAI API"',
            "-c", 'model_providers.direct_openai.base_url="https://api.openai.com/v1"',
            "-c", 'model_providers.direct_openai.wire_api="responses"',
            "-c", 'model_providers.direct_openai.env_key="OPENAI_API_KEY"',
            "-c", "model_providers.direct_openai.requires_openai_auth=false",
            "-c", "model_context_window=16384",
            "-c", "model_auto_compact_token_limit=12000",
        ]
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
        *provider_config,
        "-c", f'model_reasoning_effort="{TARGET_REASONING_EFFORT}"',
        "--model", model,
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

    with TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file, \
            TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=STATE_DIR,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
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
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(1024 * 1024)
        stderr = stderr_file.read(1024 * 1024)
    if process.returncode:
        reason = classify_diagnostic(stderr, stdout, process.returncode)
        signals = structured_signals(stderr, stdout)
        return f"codex_cli=exit_{process.returncode} reason={reason} {signals}"
    return "codex_cli=ok"


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this diagnostic with sudo.")
    values = load_worker_environment()
    api_status, models = api_probe(values["WORKSHEET_CODEX_API_KEY"])
    print(api_status)
    available = relevant_text_models(models)
    print("available_text_models=" + (",".join(available) if available else "none"))
    model = TARGET_MODEL
    print("target_model=" + TARGET_MODEL)
    print("target_model_listed=" + str(TARGET_MODEL in models).lower())
    print("responses_model=" + safe_identifier(model))
    print(responses_probe(values["WORKSHEET_CODEX_API_KEY"], model))
    binary = resolve_native_codex(values["WORKSHEET_CODEX_BINARY"])
    print("codex_binary=native")
    print("codex_provider=openai")
    primary = codex_probe(binary, values["WORKSHEET_CODEX_API_KEY"], model)
    print(primary)
    if primary != "codex_cli=ok":
        for fallback in ("gpt-5.4", "gpt-5.2", "gpt-4.1"):
            if fallback not in models:
                continue
            result = codex_probe(binary, values["WORKSHEET_CODEX_API_KEY"], fallback)
            print(f"fallback_model={fallback} {result}")
            if result == "codex_cli=ok":
                break
        print("direct_provider_probe=" + codex_probe(
            binary, values["WORKSHEET_CODEX_API_KEY"], model, direct=True,
        ))


if __name__ == "__main__":
    main()
