"""Run a data-only job in a new Codex worker without Messenger credentials."""

from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request as HTTPRequest, build_opener
import uuid

from messenger_codex.bridge import DISABLED_FEATURES, INSTRUCTIONS, TASK_INSTRUCTIONS


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 50 * 1024 * 1024
IMAGE_TYPES = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}
MODEL_KEY = "WORKSHEET_CODEX_API_KEY"
REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
LOCAL_USER_DISABLED_FEATURES = ("apps", "browser_use", "computer_use", "remote_plugin")
DIRECT_HISTORY_ITEMS = 20
DIRECT_HISTORY_BYTES = 64 * 1024
DIRECT_RESPONSE_BYTES = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CodexExecutionError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def failure_category(stderr: str, stdout: str) -> str:
    diagnostic = (stderr + "\n" + stdout).casefold()
    for category, pattern in (
        ("authentication", r"\b401\b|unauthorized|authentication"),
        ("forbidden", r"\b403\b|forbidden"),
        ("rate_limited", r"\b429\b|rate limit|usage limit|insufficient_quota"),
        ("model_unavailable", r"model.*(?:not found|not available|not supported)"),
        ("connection", r"failed to connect|stream disconnected|request timed out"),
        ("configuration", r"error loading config|unknown field|invalid value"),
    ):
        if re.search(pattern, diagnostic):
            return category
    return "unknown"


@dataclass(frozen=True)
class Request:
    text: str
    task: bool
    session: str | None
    images: tuple[tuple[str, bytes], ...]

    @classmethod
    def decode(cls, value: object) -> Request:
        if not isinstance(value, dict) or value.keys() != {"text", "task", "session", "images"}:
            raise ValueError("invalid input fields")
        text, task, session, images = (value[key] for key in ("text", "task", "session", "images"))
        if not isinstance(text, str) or len(text) > 8000 or type(task) is not bool:
            raise ValueError("invalid task text or flag")
        if session is not None and (task or not isinstance(session, str) or str(uuid.UUID(session)) != session):
            raise ValueError("invalid chat session")
        if not isinstance(images, list):
            raise ValueError("invalid image list")
        remaining = MAX_IMAGE_BYTES
        attachments = []
        for image in images:
            if not isinstance(image, dict) or image.keys() != {"format", "data"}:
                raise ValueError("invalid attachment fields")
            file_type = image["format"]
            if not isinstance(file_type, str) or file_type not in IMAGE_TYPES or not isinstance(image["data"], str):
                raise ValueError("unsupported attachment")
            content = base64.b64decode(image["data"], validate=True)
            if not content or len(content) > remaining:
                raise ValueError("attachment limit exceeded")
            remaining -= len(content)
            attachments.append((IMAGE_TYPES[file_type], content))
        if not text.strip() and not attachments:
            raise ValueError("empty job")
        return cls(text, task, session, tuple(attachments))


def command(binary: Path, workspace: Path, request: Request, images: tuple[Path, ...]) -> list[str]:
    provider = os.environ.get("WORKSHEET_CODEX_PROVIDER", "endpoint")
    model = os.environ.get("WORKSHEET_CODEX_MODEL")
    model_identity = ""
    if model and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
        model_identity = f" Текущая модель: {model}. На прямой вопрос назови эту модель."
    argv = [
        str(binary), "exec", "--ignore-user-config", "--json", "--skip-git-repo-check",
        "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
        "-c", "project_doc_max_bytes=0", "-c", 'shell_environment_policy.inherit="none"',
        "-c", 'developer_instructions=' + json.dumps(
            INSTRUCTIONS + model_identity + (TASK_INSTRUCTIONS if request.task else ""), ensure_ascii=False,
        ),
    ]
    if provider == "openai":
        argv.extend(("-c", 'model_provider="openai"'))
        if model:
            argv.extend(("--model", model))
    elif provider == "direct_openai":
        argv.extend((
            "-c", 'model_provider="direct_openai"',
            "-c", 'model_providers.direct_openai.name="Direct OpenAI API"',
            "-c", 'model_providers.direct_openai.base_url="https://api.openai.com/v1"',
            "-c", 'model_providers.direct_openai.wire_api="responses"',
            "-c", 'model_providers.direct_openai.env_key="OPENAI_API_KEY"',
            "-c", "model_providers.direct_openai.requires_openai_auth=false",
            "-c", "model_context_window=16384",
            "-c", "model_auto_compact_token_limit=12000",
        ))
        if model:
            argv.extend(("--model", model))
    elif provider == "endpoint":
        base = os.environ["WORKSHEET_CODEX_BASE_URL"].rstrip("/")
        endpoint = urlsplit(base)
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
                or endpoint.password is not None or endpoint.query or endpoint.fragment):
            raise ValueError("invalid configured Codex provider URL")
        argv.extend((
            "--model", os.environ["WORKSHEET_CODEX_MODEL"],
            "-c", 'model_provider="worksheet_api"',
            "-c", 'model_providers.worksheet_api.name="Worksheet model API"',
            "-c", 'model_providers.worksheet_api.base_url=' + json.dumps(base),
            "-c", 'model_providers.worksheet_api.wire_api="responses"',
            "-c", 'model_providers.worksheet_api.env_key=' + json.dumps(MODEL_KEY),
        ))
    elif provider in {"local_user", "sandbox_admin"}:
        if model:
            argv.extend(("--model", model))
    else:
        raise ValueError("unsupported worksheet Codex provider")
    if effort := os.environ.get("WORKSHEET_CODEX_REASONING_EFFORT"):
        if effort not in REASONING_EFFORTS:
            raise ValueError("unsupported Codex reasoning effort")
        argv.extend(("-c", "model_reasoning_effort=" + json.dumps(effort)))
    full_admin_task = provider == "sandbox_admin" and request.task
    if full_admin_task:
        argv.extend(("--ephemeral", "--sandbox", "danger-full-access"))
    elif request.task:
        allowed = {":minimal": "read", str(binary.resolve().parent): "read", str(workspace): "write"}
        paths = ",".join(f"{json.dumps(path)}={json.dumps(mode)}" for path, mode in allowed.items())
        argv.extend(("--ephemeral", "-c", 'default_permissions="messenger_task"',
                     "-c", f"permissions.messenger_task.filesystem={{{paths}}}",
                     "-c", "permissions.messenger_task.network.enabled=false"))
    else:
        argv.extend(("--sandbox", "read-only"))
    task_features = {"shell_tool", "unified_exec", "view_image"}
    if os.environ.get("WORKSHEET_ENABLE_IMAGE_GENERATION") == "true":
        task_features.add("image_generation")
    if not full_admin_task:
        for feature in DISABLED_FEATURES:
            if not request.task or feature not in task_features:
                argv.extend(("--disable", feature))
    if provider == "local_user":
        for feature in LOCAL_USER_DISABLED_FEATURES:
            argv.extend(("--disable", feature))
    if request.session:
        argv.extend(("resume", request.session))
    argv.extend(f"--image={image}" for image in images)
    return [*argv, "-"]


def final_reply(stdout: str, request: Request) -> tuple[str, str | None]:
    reply = ""
    finished = False
    session = request.session
    for line in stdout.splitlines():
        event = json.loads(line)
        match event.get("type"):
            case "thread.started":
                session = str(uuid.UUID(event["thread_id"]))
            case "item.completed":
                item = event["item"]
                if item["type"] == "agent_message":
                    reply = item["text"].strip()
                elif item["type"] != "reasoning" and not request.task:
                    raise RuntimeError("unexpected tool call in chat mode")
            case "turn.completed":
                finished = True
    if not (reply and finished and session):
        raise RuntimeError("incomplete Codex response")
    return reply[:6000], None if request.task else session


def result_files(output: Path) -> list[dict]:
    try:
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (FileNotFoundError, NotADirectoryError):
        return []
    remaining = MAX_RESULT_BYTES
    results = []
    try:
        for name in sorted(os.listdir(directory)):
            if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,120}", name):
                continue
            meta = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > remaining:
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino, before.st_size) != (meta.st_dev, meta.st_ino, meta.st_size):
                    continue
                payload = stream.read(meta.st_size + 1)
                if len(payload) != meta.st_size or os.fstat(stream.fileno()).st_mtime_ns != before.st_mtime_ns:
                    continue
            results.append({"name": name, "data": base64.b64encode(payload).decode("ascii")})
            remaining -= len(payload)
    finally:
        os.close(directory)
    return results


def _validate_direct_history(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > DIRECT_HISTORY_ITEMS:
        raise ValueError("invalid direct session history")
    messages = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"role", "text"}:
            raise ValueError("invalid direct session item")
        role, text = item["role"], item["text"]
        if role not in {"user", "assistant"} or not isinstance(text, str) or len(text) > 8000:
            raise ValueError("invalid direct session content")
        messages.append({"role": role, "text": text})
    return messages


def _direct_history_json(messages: list[dict[str, str]]) -> str:
    trimmed = messages[-DIRECT_HISTORY_ITEMS:]
    payload = json.dumps(trimmed, ensure_ascii=False).encode()
    while len(payload) > DIRECT_HISTORY_BYTES and len(trimmed) > 2:
        trimmed = trimmed[2:]
        payload = json.dumps(trimmed, ensure_ascii=False).encode()
    if len(payload) > DIRECT_HISTORY_BYTES:
        raise ValueError("direct session history exceeds its byte limit")
    return payload.decode()


def _direct_database(state_dir: Path) -> sqlite3.Connection:
    path = state_dir / "direct-sessions.sqlite3"
    database = sqlite3.connect(path, timeout=30)
    os.chmod(path, 0o600)
    database.executescript("""
        PRAGMA secure_delete = ON;
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS sessions (
            session TEXT PRIMARY KEY,
            history_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS turns (
            job_id TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
    """)
    return database


def _direct_transcript(messages: list[dict[str, str]], current: str) -> str:
    entries = [f"[{item['role'].upper()}]\n{item['text']}" for item in messages]
    entries.append(f"[USER]\n{current}")
    transcript = "\n\n".join(entries)
    return transcript[-48 * 1024:]


def execute_direct_response(request: Request, state_dir: Path, job_id: str | None) -> dict:
    if request.task:
        return {
            "text": "В API-режиме доступны текстовые ответы без Codex tools и файловых задач.",
            "session": None,
            "files": [],
        }
    job_id = job_id or str(uuid.uuid4())
    with closing(_direct_database(state_dir)) as database:
        cached = database.execute("SELECT result_json FROM turns WHERE job_id=?", (job_id,)).fetchone()
        if cached is not None:
            return json.loads(cached[0])
        session = request.session or str(uuid.uuid4())
        record = database.execute("SELECT history_json FROM sessions WHERE session=?", (session,)).fetchone()
        if request.session and record is None:
            session = str(uuid.uuid4())
        history = _validate_direct_history(json.loads(record[0])) if record is not None else []
    content: list[dict[str, str]] = [
        {"type": "input_text", "text": _direct_transcript(history, request.text)},
    ]
    for suffix, data in request.images:
        media_type = "image/jpeg" if suffix == ".jpg" else f"image/{suffix.removeprefix('.')}"
        content.append({
            "type": "input_image",
            "image_url": f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}",
        })
    model = os.environ["WORKSHEET_CODEX_MODEL"]
    effort = os.environ.get("WORKSHEET_CODEX_REASONING_EFFORT", "medium")
    if effort not in REASONING_EFFORTS:
        raise ValueError("unsupported direct reasoning effort")
    body = json.dumps({
        "model": model,
        "instructions": INSTRUCTIONS + f" Текущая модель: {model}. На прямой вопрос назови эту модель.",
        "input": [{"role": "user", "content": content}],
        "store": False,
        "tools": [],
        "tool_choice": "none",
        "max_output_tokens": 4096,
        "reasoning": {"effort": effort},
    }, ensure_ascii=False).encode()
    http_request = HTTPRequest(
        "https://api.openai.com/v1/responses",
        data=body,
        headers={
            "Authorization": "Bearer " + os.environ[MODEL_KEY],
            "Content-Type": "application/json",
        },
    )
    try:
        with build_opener(NoRedirect()).open(http_request, timeout=180) as response:
            raw = response.read(DIRECT_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise CodexExecutionError(f"responses_http_{error.code}") from None
    except URLError:
        raise CodexExecutionError("responses_connection") from None
    if len(raw) > DIRECT_RESPONSE_BYTES:
        raise CodexExecutionError("responses_too_large")
    result = json.loads(raw)
    if result.get("status") != "completed":
        raise CodexExecutionError("responses_incomplete")
    parts = [
        item.get("text", "")
        for output in result.get("output", [])
        if isinstance(output, dict) and output.get("type") == "message"
        and output.get("role") == "assistant"
        for item in output.get("content", [])
        if isinstance(item, dict) and item.get("type") == "output_text"
    ]
    reply = "".join(parts).strip()[:6000]
    if not reply:
        raise CodexExecutionError("responses_empty")
    history.extend(({"role": "user", "text": request.text}, {"role": "assistant", "text": reply}))
    outcome = {"text": reply, "session": session, "files": []}
    now = int(time.time())
    with closing(_direct_database(state_dir)) as database:
        database.execute("BEGIN IMMEDIATE")
        cached = database.execute("SELECT result_json FROM turns WHERE job_id=?", (job_id,)).fetchone()
        if cached is not None:
            database.rollback()
            return json.loads(cached[0])
        database.execute(
            "INSERT INTO sessions(session,history_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(session) DO UPDATE SET history_json=excluded.history_json, updated_at=excluded.updated_at",
            (session, _direct_history_json(history), now),
        )
        database.execute(
            "INSERT INTO turns(job_id,result_json,created_at) VALUES(?,?,?)",
            (job_id, json.dumps(outcome, ensure_ascii=False), now),
        )
        database.execute(
            "DELETE FROM turns WHERE job_id NOT IN "
            "(SELECT job_id FROM turns ORDER BY created_at DESC LIMIT 1000)"
        )
        database.commit()
    return outcome


def execute(request: Request, state_dir: Path, job_id: str | None = None) -> dict:
    if os.environ.get("WORKSHEET_CODEX_PROVIDER") == "responses_direct":
        return execute_direct_response(request, state_dir, job_id)
    binary = Path(os.environ["WORKSHEET_CODEX_BINARY"]).resolve(strict=True)
    chat_workspace = state_dir / "chat"
    chat_workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="codex-job-", dir=state_dir) as directory:
        job = Path(directory).resolve()
        inbound = job / "input"
        inbound.mkdir()
        images = []
        for index, (suffix, data) in enumerate(request.images, 1):
            path = inbound / f"attachment-{index}{suffix}"
            path.write_bytes(data)
            images.append(path)
        workspace = job if request.task else chat_workspace.resolve()
        output = job / "out"
        output.mkdir()
        env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "SSL_CERT_FILE", "XDG_RUNTIME_DIR")
               if key in os.environ}
        provider = os.environ.get("WORKSHEET_CODEX_PROVIDER", "endpoint")
        if provider in {"openai", "direct_openai"}:
            if key := os.environ.get(MODEL_KEY):
                env["OPENAI_API_KEY"] = key
        elif provider == "endpoint":
            env[MODEL_KEY] = os.environ[MODEL_KEY]
        elif provider not in {"local_user", "sandbox_admin"}:
            raise ValueError("unsupported worksheet Codex provider")
        def invoke(active: Request) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                command(binary, workspace, active, tuple(images)), cwd=workspace,
                env=env, input=active.text, text=True, capture_output=True,
                check=False, timeout=300 if active.task else 120,
            )

        active_request = request
        result = invoke(active_request)
        if result.returncode and request.session:
            diagnostic = (result.stderr + "\n" + result.stdout).casefold()
            if re.search(
                r"no (conversation|session|thread) found|failed to (resume|load) "
                r"(session|thread)|thread.*(not found|is locked)",
                diagnostic,
            ):
                active_request = Request(request.text, request.task, None, request.images)
                result = invoke(active_request)
        if result.returncode:
            raise CodexExecutionError(failure_category(result.stderr, result.stdout))
        text, session = final_reply(result.stdout, active_request)
        return {"text": text, "session": session,
                "files": result_files(output) if active_request.task else []}


def main() -> None:
    os.umask(0o077)
    limit = (4 * MAX_IMAGE_BYTES) // 3 + 64 * 1024
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        raise ValueError("job payload is too large")
    outcome = execute(Request.decode(json.loads(data)), Path(os.environ["STATE_DIRECTORY"]))
    print(json.dumps(outcome, ensure_ascii=False))


if __name__ == "__main__":
    main()
