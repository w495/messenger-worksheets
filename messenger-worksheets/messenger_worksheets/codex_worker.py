"""Run a data-only job in a new Codex worker without Messenger credentials."""

from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
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

from messenger_codex.bridge import (
    DISABLED_FEATURES, INSTRUCTIONS, NETWORK_TASK_INSTRUCTIONS,
    OFFLINE_TASK_INSTRUCTIONS, TASK_INSTRUCTIONS,
)


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 50 * 1024 * 1024
IMAGE_TYPES = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}
MODEL_KEY = "WORKSHEET_CODEX_API_KEY"
REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
LOCAL_USER_DISABLED_FEATURES = ("apps", "browser_use", "computer_use", "remote_plugin")
DIRECT_HISTORY_ITEMS = 20
DIRECT_HISTORY_BYTES = 64 * 1024
DIRECT_RESPONSE_BYTES = 2 * 1024 * 1024
TASK_TIMEOUT_ENV = "WORKSHEET_CODEX_TASK_TIMEOUT"
CHAT_TIMEOUT_ENV = "WORKSHEET_CODEX_CHAT_TIMEOUT"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Запретить следование HTTP-перенаправлениям при обращении к модели.

        Параметры: req — HTTP request; fp — response stream; code — HTTP status; msg — status text; headers — headers; newurl — redirect URL.
        Результат: None, поэтому urllib не следует HTTP redirect.
        Исключения: Не генерирует собственных исключений.
        Побочные эффекты: Побочных эффектов нет.
        Безопасность: Authorization и request body не пересылаются на новый URL."""
        return None


class CodexExecutionError(RuntimeError):
    def __init__(self, category: str):
        """Создать ошибку исполнения с безопасной категорией без диагностических секретов.

        Параметры: category — безопасная error category.
        Результат: Объект исключения с безопасной category.
        Исключения: Не генерирует собственных исключений.
        Побочные эффекты: Изменяет только создаваемый объект.
        Безопасность: Хранит категорию, а не stderr/stdout или API credentials."""
        super().__init__(category)
        self.category = category


def execution_timeout(task: bool) -> int:
    """Вернуть ограниченный timeout Codex для чата или задачи.

    Параметры: task — режим task.
    Результат: Task/chat timeout в секундах в диапазоне 1..3600.
    Исключения: ValueError для нецелой или выходящей за диапазон env-настройки.
    Побочные эффекты: Читает одну переменную окружения.
    Безопасность: Невалидная конфигурация отклоняется, а не превращается в неограниченное выполнение."""
    name = TASK_TIMEOUT_ENV if task else CHAT_TIMEOUT_ENV
    default = 1800 if task else 120
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not 1 <= value <= 3600:
        raise ValueError(f"{name} must be in range 1..3600")
    return value


def failure_category(stderr: str, stdout: str) -> str:
    """Сопоставить stderr/stdout Codex с безопасной публичной категорией отказа.

    Параметры: stderr — process stderr; stdout — JSON event stream.
    Результат: Одну из authentication, forbidden, rate_limited, model_unavailable, connection, configuration или unknown.
    Исключения: Не генерирует собственных исключений.
    Побочные эффекты: Побочных эффектов нет.
    Безопасность: Возвращает только фиксированную категорию; исходный stderr/stdout не публикуется."""
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
        """Строго декодировать data-only job, UUID сессии и ограниченные изображения.

        Параметры: value — JSON job/history.
        Результат: Неизменяемый Request с текстом, режимом, UUID и кортежем декодированных изображений.
        Исключения: ValueError для полей/типов/UUID/пустого job/лимита; binascii.Error для невалидного base64.
        Побочные эффекты: Декодирует изображения в память; входной объект не изменяет.
        Безопасность: Допускает только четыре формата, общий бюджет 20 MiB и запрещает session у task."""
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
    """Собрать argv Codex с минимальными разрешениями для выбранного provider и режима.

    Параметры: binary — Codex binary; workspace — job workspace; request — валидированный Request; images — локальные image paths.
    Результат: Список argv Codex, заканчивающийся stdin-маркером -.
    Исключения: KeyError для обязательной provider env; ValueError для provider URL, provider или reasoning effort.
    Побочные эффекты: Читает env и resolve пути binary; процессов не запускает.
    Безопасность: Не использует shell; chat read-only, обычный task ограничен workspace без сети, admin task включается только явным provider."""
    provider = os.environ.get("WORKSHEET_CODEX_PROVIDER", "endpoint")
    full_admin_task = provider == "sandbox_admin" and request.task
    task_instructions = ""
    if request.task:
        task_instructions = TASK_INSTRUCTIONS + (
            NETWORK_TASK_INSTRUCTIONS if full_admin_task else OFFLINE_TASK_INSTRUCTIONS
        )
    model = os.environ.get("WORKSHEET_CODEX_MODEL")
    model_identity = ""
    if model and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
        model_identity = f" Текущая модель: {model}. На прямой вопрос назови эту модель."
    argv = [
        str(binary), "exec", "--ignore-user-config", "--json", "--skip-git-repo-check",
        "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
        "-c", "project_doc_max_bytes=0", "-c", 'shell_environment_policy.inherit="none"',
        "-c", 'developer_instructions=' + json.dumps(
            INSTRUCTIONS + model_identity + task_instructions, ensure_ascii=False,
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
    """Разобрать поток JSON-событий Codex и извлечь завершённый ответ и сессию.

    Параметры: stdout — JSON event stream; request — валидированный Request.
    Результат: Текст до 6000 символов и UUID session для chat либо None для task.
    Исключения: JSONDecodeError/KeyError/ValueError при повреждённых событиях/UUID; RuntimeError для tool call в chat или незавершённого ответа.
    Побочные эффекты: Побочных эффектов нет.
    Безопасность: В chat принимает только agent_message/reasoning; task не возвращает reusable session."""
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
    """Безопасно прочитать регулярные неизменившиеся файлы output в общем лимите.

    Параметры: output — out directory.
    Результат: Список dict name/base64 для допустимых файлов в общем лимите 50 MiB.
    Исключения: OSError при directory/stat/open/read, кроме обработанных отсутствующего/не-каталога output.
    Побочные эффекты: Открывает directory/file descriptors и читает содержимое в память; ничего не изменяет.
    Безопасность: O_NOFOLLOW, regular+nlink=1, имя без control chars, сверка inode/size/mtime и общий budget защищают от подмены."""
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
    """Проверить структуру и ограничения истории прямой Responses-сессии.

    Параметры: value — JSON job/history.
    Результат: Новый список только с role/text для не более 20 валидных сообщений.
    Исключения: ValueError для неверного контейнера, ключей, role, типа или текста >8000.
    Побочные эффекты: Создаёт копии словарей; вход не изменяет.
    Безопасность: Не пропускает дополнительные поля и произвольные роли в model transcript."""
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
    """Обрезать историю по числу записей и байтовому лимиту и сериализовать её.

    Параметры: messages — проверенная history.
    Результат: UTF-8 JSON-строку последних сообщений размером не более 64 KiB.
    Исключения: ValueError, если даже минимальная оставшаяся история превышает byte limit.
    Побочные эффекты: Сериализует и локально обрезает копию списка; вход не изменяет.
    Безопасность: Ограничивает и число записей, и байтовый размер persistent history."""
    trimmed = messages[-DIRECT_HISTORY_ITEMS:]
    payload = json.dumps(trimmed, ensure_ascii=False).encode()
    while len(payload) > DIRECT_HISTORY_BYTES and len(trimmed) > 2:
        trimmed = trimmed[2:]
        payload = json.dumps(trimmed, ensure_ascii=False).encode()
    if len(payload) > DIRECT_HISTORY_BYTES:
        raise ValueError("direct session history exceeds its byte limit")
    return payload.decode()


def _direct_database(state_dir: Path) -> sqlite3.Connection:
    """Открыть приватную SQLite-базу прямых сессий и создать защищённую схему.

    Параметры: state_dir — приватный state dir.
    Результат: Открытое sqlite3.Connection с созданной схемой sessions/turns.
    Исключения: sqlite3.Error или OSError chmod.
    Побочные эффекты: Создаёт/открывает direct-sessions.sqlite3, chmod 0600, включает secure_delete/WAL и создаёт таблицы.
    Безопасность: Файл состояния принудительно приватный; SQL использует фиксированную схему."""
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
    """Собрать ограниченный текстовый транскрипт истории и текущего сообщения.

    Параметры: messages — проверенная history; current — текущий text.
    Результат: Последние не более 48 KiB текста с role labels и текущим USER сообщением.
    Исключения: KeyError/TypeError только при нарушении проверенного внутреннего history contract.
    Побочные эффекты: Создаёт строку в памяти; входы не изменяет.
    Безопасность: Обрезает transcript перед API и маркирует роли самостоятельно, не доверяя role-разметке внутри текста."""
    entries = [f"[{item['role'].upper()}]\n{item['text']}" for item in messages]
    entries.append(f"[USER]\n{current}")
    transcript = "\n\n".join(entries)
    return transcript[-48 * 1024:]


def execute_direct_response(request: Request, state_dir: Path, job_id: str | None) -> dict:
    """Выполнить текстовый ход через Responses API без tools и идемпотентно сохранить историю.

    Параметры: request — валидированный Request; state_dir — приватный state dir; job_id — канонический UUID job.
    Результат: Dict text/session/files; task получает отказ без API, chat — ответ и UUID session.
    Исключения: KeyError/ValueError конфигурации; sqlite3.Error/JSONDecodeError; CodexExecutionError для HTTP/connection/size/status/empty; OSError TLS transport.
    Побочные эффекты: Читает/обновляет SQLite, отправляет HTTPS Responses request, base64-кодирует images и кеширует turn по job_id.
    Безопасность: store=false, tools=[], tool_choice=none; history bounded, результат job идемпотентен, API key только в Authorization."""
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


def terminate_process_group(process_id: int) -> None:
    """Завершить оставшихся потомков в process group с TERM→KILL escalation.

    Параметры: process_id — PID/PGID лидера.
    Результат: Ничего; отсутствующая process group считается уже завершённой.
    Исключения: PermissionError/OSError signal; ProcessLookupError подавляется.
    Побочные эффекты: Посылает SIGTERM process group, ждёт 50 мс и при необходимости SIGKILL.
    Безопасность: Сигналы адресуются PGID запущенного job; setsid-потомки остаются под внешней cgroup policy."""

    try:
        os.killpg(process_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(0.05)
    try:
        os.killpg(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_codex_process(
    argv: list[str], *, workspace: Path, environment: dict[str, str],
    text: str, timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Запустить Codex в отдельной process group и гарантировать уборку потомков.

    Параметры: argv — argv без shell; workspace — job workspace; environment — allowlist env; text — stdin prompt; timeout — жёсткий timeout.
    Результат: CompletedProcess с args, returncode, stdout и stderr.
    Исключения: OSError при Popen; subprocess.TimeoutExpired после kill всей group; ошибки communicate/signal.
    Побочные эффекты: Запускает новую session/process group, передаёт text в stdin, собирает output и всегда выполняет TERM→KILL cleanup потомков.
    Безопасность: Shell не используется; argv/env/workspace уже подготовлены, cleanup охватывает обычных потомков group."""

    process = subprocess.Popen(
        argv, cwd=workspace, env=environment, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(text, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    finally:
        terminate_process_group(process.pid)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def execute(request: Request, state_dir: Path, job_id: str | None = None) -> dict:
    """Выполнить проверенный job через direct API либо изолированный процесс Codex.

    Параметры: request — валидированный Request; state_dir — приватный state dir; job_id — канонический UUID job.
    Результат: Dict с text, session и files; files заполнены только для успешного task.
    Исключения: KeyError/ValueError конфигурации; OSError/TimeoutExpired процесса и ФС; CodexExecutionError при nonzero exit; ошибки direct режима.
    Побочные эффекты: Создаёт chat workspace или временный task job/input/out, пишет images, запускает Codex/direct API и читает результаты.
    Безопасность: Messenger credentials не передаются; env allowlist зависит от provider, task files берутся только из проверенного out."""
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
            """Запустить один подготовленный процесс Codex для активного запроса.

            Параметры: active — активный Request после session fallback.
            Результат: CompletedProcess одного запуска Codex для active Request.
            Исключения: ValueError/KeyError из command/timeout; OSError или TimeoutExpired из run_codex_process.
            Побочные эффекты: Строит argv и запускает отдельную process group с подготовленным env/workspace.
            Безопасность: Использует active request после возможного сброса session и не расширяет allowlist env."""
            return run_codex_process(
                command(binary, workspace, active, tuple(images)),
                workspace=workspace,
                environment=env,
                text=active.text,
                timeout=execution_timeout(active.task),
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
    """Прочитать ограниченный job из stdin, выполнить его и вывести JSON-результат.

    Параметры: нет.
    Результат: Ничего; пишет один JSON result в stdout.
    Исключения: ValueError для stdin сверх лимита/невалидного job; JSONDecodeError; исключения execute.
    Побочные эффекты: Устанавливает umask 077, читает ограниченный stdin, выполняет job и печатает JSON.
    Безопасность: Читает не более base64 image budget+64 KiB и не выводит API key/окружение."""
    os.umask(0o077)
    limit = (4 * MAX_IMAGE_BYTES) // 3 + 64 * 1024
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        raise ValueError("job payload is too large")
    outcome = execute(Request.decode(json.loads(data)), Path(os.environ["STATE_DIRECTORY"]))
    print(json.dumps(outcome, ensure_ascii=False))


if __name__ == "__main__":
    main()
