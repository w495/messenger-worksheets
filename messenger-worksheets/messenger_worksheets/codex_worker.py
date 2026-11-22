"""Run a data-only job in a new Codex worker without Messenger credentials."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
import uuid

from messenger_codex.bridge import DISABLED_FEATURES, INSTRUCTIONS, TASK_INSTRUCTIONS


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 50 * 1024 * 1024
IMAGE_TYPES = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}
MODEL_KEY = "WORKSHEET_CODEX_API_KEY"


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
    argv = [
        str(binary), "exec", "--ignore-user-config", "--json", "--skip-git-repo-check",
        "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
        "-c", "project_doc_max_bytes=0", "-c", 'shell_environment_policy.inherit="none"',
        "-c", 'developer_instructions=' + json.dumps(
            INSTRUCTIONS + (TASK_INSTRUCTIONS if request.task else ""), ensure_ascii=False,
        ),
    ]
    provider = os.environ.get("WORKSHEET_CODEX_PROVIDER", "endpoint")
    if provider == "openai":
        argv.extend(("-c", 'model_provider="openai"'))
        if model := os.environ.get("WORKSHEET_CODEX_MODEL"):
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
    else:
        raise ValueError("unsupported worksheet Codex provider")
    if request.task:
        allowed = {":minimal": "read", str(binary.resolve().parent): "read", str(workspace): "write"}
        paths = ",".join(f"{json.dumps(path)}={json.dumps(mode)}" for path, mode in allowed.items())
        argv.extend(("--ephemeral", "-c", 'default_permissions="messenger_task"',
                     "-c", f"permissions.messenger_task.filesystem={{{paths}}}",
                     "-c", "permissions.messenger_task.network.enabled=false"))
    else:
        argv.extend(("--sandbox", "read-only"))
    for feature in DISABLED_FEATURES:
        if not request.task or feature not in {"shell_tool", "unified_exec", "view_image"}:
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


def execute(request: Request, state_dir: Path) -> dict:
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
        if os.environ.get("WORKSHEET_CODEX_PROVIDER", "endpoint") == "openai":
            if key := os.environ.get(MODEL_KEY):
                env["OPENAI_API_KEY"] = key
        else:
            env[MODEL_KEY] = os.environ[MODEL_KEY]
        result = subprocess.run(command(binary, workspace, request, tuple(images)), cwd=workspace,
                                env=env, input=request.text, text=True, capture_output=True,
                                check=False, timeout=300 if request.task else 120)
        if result.returncode:
            raise RuntimeError(f"Codex exited with status {result.returncode}")
        text, session = final_reply(result.stdout, request)
        return {"text": text, "session": session, "files": result_files(output) if request.task else []}


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
