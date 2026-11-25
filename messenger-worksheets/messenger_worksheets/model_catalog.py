"""Получить ограниченную публичную проекцию каталога моделей текущего worker."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .model_http import check_cancellation, post_model_response


CACHE_TTL = 60.0
FETCH_TIMEOUT = 15.0
MAX_NATIVE_BYTES = 16 * 1024 * 1024
MAX_API_BYTES = 1024 * 1024
MAX_MODELS = 256
MAX_PUBLIC_BYTES = 60 * 1024
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z", re.ASCII)
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
PROVIDERS = frozenset({"responses_direct", "local_user", "sandbox_admin", "endpoint", "openai", "direct_openai"})
_LOCK = threading.Lock()
_CACHE: dict[tuple[str, ...], tuple[float, dict]] = {}


class CatalogFailure(RuntimeError):
    """Передать только фиксированную безопасную категорию ошибки каталога."""


def validate_catalog(value: object) -> dict:
    """Проверить публичную schema каталога до вывода ответа worker в Messenger.

    Принимает недоверенный JSON-объект с фиксированными полями происхождения,
    времени и списка моделей. Допускает до 256 уникальных безопасных ID,
    булевы признаки и только известные уровни reasoning. Возвращает независимую
    копию; неизвестные поля, роли источника, типы и лимиты вызывают ValueError.
    Сеть и ФС не используются; инструкции, адреса и credentials не пропускаются.
    """
    fields = {"available", "provider", "source", "scope", "fetched_at", "models", "truncated", "error"}
    errors = {None, "configuration", "invalid_response", "timeout", "response_too_large", "execution_failed",
              "authentication", "forbidden", "rate_limited", "connection", "unsupported_provider"}
    if (not isinstance(value, dict) or value.keys() != fields
            or type(value["available"]) is not bool or type(value["truncated"]) is not bool
            or not isinstance(value["provider"], str) or value["provider"] not in PROVIDERS | {"unknown"}
            or not isinstance(value["source"], str) or value["source"] not in {"openai_models_api", "codex_active_catalog", "unavailable"}
            or value["scope"] != "catalogue" or type(value["fetched_at"]) is not int or value["fetched_at"] < 0
            or not isinstance(value["models"], list) or len(value["models"]) > MAX_MODELS
            or (value["error"] is not None and not isinstance(value["error"], str)) or value["error"] not in errors):
        raise ValueError("invalid worker model catalog")
    if value["available"] != (value["source"] != "unavailable") or (not value["available"] and value["models"]):
        raise ValueError("inconsistent model catalog")
    if value["available"] and value["error"] is not None:
        raise ValueError("successful model catalog contains an error")
    identifiers = set()
    for item in value["models"]:
        if (not isinstance(item, dict) or item.keys() != {"id", "is_default", "reasoning_efforts", "default_reasoning_effort"}
                or not isinstance(item["id"], str) or MODEL_ID.fullmatch(item["id"]) is None or item["id"] in identifiers
                or type(item["is_default"]) is not bool or not isinstance(item["reasoning_efforts"], list)
                or len(item["reasoning_efforts"]) > len(EFFORTS)
                or any(not isinstance(level, str) or level not in EFFORTS for level in item["reasoning_efforts"])
                or len(set(item["reasoning_efforts"])) != len(item["reasoning_efforts"])
                or (item["default_reasoning_effort"] is not None and _safe_effort(item["default_reasoning_effort"]) is None)):
            raise ValueError("invalid catalog model")
        identifiers.add(item["id"])
    if len(json.dumps(value).encode()) > 64 * 1024:
        raise ValueError("model catalog exceeds limit")
    return deepcopy(value)


def _empty(provider: str) -> dict:
    """Создать исходную безопасную форму ответа для заданного provider.

    Параметр provider — уже проверенная метка маршрута. Возвращает новый dict
    с пустым каталогом и текущим временем Unix; ввода-вывода и сетевых запросов
    нет. Исходная диагностика, адреса, пути и credentials в ответ не входят.
    """
    return {"available": False, "provider": provider, "source": "unavailable",
            "scope": "catalogue", "fetched_at": int(time.time()), "models": [],
            "truncated": False, "error": None}


def _safe_effort(value: object) -> str | None:
    """Проверить одно недоверенное значение уровня reasoning по allowlist.

    Параметр value может иметь любой JSON-тип. Возвращает исходную строку,
    только если она точно совпала с известным уровнем, иначе None. Исключения
    не создаёт; окружение, сеть и файлы не читает и не изменяет.
    """
    return value if isinstance(value, str) and value in EFFORTS else None


def _project(entries: object, *, native: bool, configured: str | None) -> tuple[list[dict], bool]:
    """Построить ограниченную публичную проекцию недоверенного списка моделей.

    Параметры: entries — список записей JSON; native выбирает схему Codex,
    configured — настроенный ID для отметки default. Возвращает пару списка
    моделей и признака усечения. Пропускает скрытые native-записи, нетекстовые
    API-модели, неизвестные поля и небезопасные ID; reasoning проверяет по
    allowlist. CatalogFailure означает неверный корневой тип. Сети и ФС нет;
    число и размер результата ограничены MAX_MODELS и MAX_PUBLIC_BYTES.
    """
    if not isinstance(entries, list):
        raise CatalogFailure("invalid_response")
    models = {}
    excluded = ("embedding", "audio", "image", "transcrib", "whisper", "tts", "realtime", "search", "moderation", "dall-e", "sora")
    for entry in entries:
        if not isinstance(entry, dict) or (native and entry.get("visibility") != "list"):
            continue
        identifier = entry.get("slug" if native else "id")
        if not isinstance(identifier, str) or MODEL_ID.fullmatch(identifier) is None:
            continue
        if not native and (not re.match(r"(?:gpt-|o[1-9](?:[.-]|$))", identifier)
                           or any(word in identifier.casefold() for word in excluded)):
            continue
        levels = entry.get("supported_reasoning_levels", []) if native else []
        efforts = {effort for item in levels if isinstance(item, dict)
                   and (effort := _safe_effort(item.get("effort"))) is not None} if isinstance(levels, list) else set()
        models[identifier] = {"id": identifier, "is_default": identifier == configured,
                              "reasoning_efforts": [item for item in EFFORTS if item in efforts],
                              "default_reasoning_effort": _safe_effort(entry.get("default_reasoning_level")) if native else None}
    result = []
    truncated = False
    for identifier in sorted(models):
        candidate = models[identifier]
        if len(result) >= MAX_MODELS or len(json.dumps([*result, candidate]).encode()) > MAX_PUBLIC_BYTES:
            truncated = True
            break
        result.append(candidate)
    return result, truncated


def _native_output(binary: str) -> bytes:
    """Прочитать ограниченный каталог отдельным native Codex без task tools.

    Параметр binary — настроенный оператором путь; чат не задаёт executable
    или аргументы. Возвращает bytes stdout до MAX_NATIVE_BYTES за FETCH_TIMEOUT.
    Provider фиксирован как openai, как у worker с ignore-user-config; debug
    models может использовать свой cache, поэтому свежесть и entitlement этим
    вызовом не доказываются. Наследует только разрешённые HOME/PATH/LANG/TLS/env,
    stderr закрыт. При отмене, превышении размера или deadline убивает свою
    process group и собирает процесс. Ошибки пути/процесса передаются вызывающему,
    CatalogFailure содержит только фиксированную категорию, исходный stderr
    и инструкции каталога не журналируются и не возвращаются участникам чата.
    """
    executable = Path(binary).resolve(strict=True)
    if not executable.is_file():
        raise CatalogFailure("configuration")
    environment = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "SSL_CERT_FILE", "XDG_RUNTIME_DIR") if key in os.environ}
    check_cancellation()
    process = subprocess.Popen([str(executable), "-c", 'model_provider="openai"', "debug", "models"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
                               cwd=str(executable.parent), start_new_session=True)
    chunks = bytearray()
    deadline = time.monotonic() + FETCH_TIMEOUT
    try:
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            eof = False
            while not eof or process.poll() is None:
                check_cancellation()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CatalogFailure("timeout")
                if eof:
                    try:
                        process.wait(timeout=min(0.2, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                    continue
                for key, _ in selector.select(min(0.2, remaining)):
                    piece = os.read(key.fd, min(65536, MAX_NATIVE_BYTES + 1 - len(chunks)))
                    if not piece:
                        eof = True
                        selector.unregister(key.fileobj)
                        break
                    chunks.extend(piece)
                    if len(chunks) > MAX_NATIVE_BYTES:
                        raise CatalogFailure("response_too_large")
        if process.returncode:
            raise CatalogFailure("execution_failed")
        return bytes(chunks)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.stdout is not None:
            process.stdout.close()
        process.wait(timeout=1)


def _fetch(provider: str) -> dict:
    """Получить каталог текущего разрешённого provider без пользовательских URL.

    Параметр provider — проверенная метка маршрута. Возвращает безопасный dict
    с проекцией моделей или фиксированной категорией отказа. responses_direct
    выполняет ограниченный GET к api.openai.com с ключом только через защищённый
    HTTP-helper; local_user/sandbox_admin запускают bounded native debug models.
    Другие provider возвращают unsupported_provider. Транспортные и JSON-ошибки
    очищаются; отмена job передаётся вызывающему. Сырой JSON, stderr, endpoints,
    пути и токены в публичный результат не входят; меняется только время ответа.
    """
    result = _empty(provider)
    if provider not in {"responses_direct", "local_user", "sandbox_admin"}:
        result["error"] = "unsupported_provider"
        return result
    try:
        native = provider != "responses_direct"
        if native:
            binary = os.environ.get("WORKSHEET_CODEX_BINARY", "")
            if not binary:
                raise CatalogFailure("configuration")
            raw = _native_output(binary)
            source = "codex_active_catalog"
        else:
            credential = os.environ.get("WORKSHEET_CODEX_API_KEY", "")
            if not credential:
                raise CatalogFailure("configuration")
            request = Request("https://api.openai.com/v1/models", method="GET",
                              headers={"Authorization": "Bearer " + credential, "Accept": "application/json"})
            raw = post_model_response(request, timeout=FETCH_TIMEOUT, max_bytes=MAX_API_BYTES)
            if len(raw) > MAX_API_BYTES:
                raise CatalogFailure("response_too_large")
            source = "openai_models_api"
        payload = json.loads(raw)
        entries = payload.get("models" if native else "data") if isinstance(payload, dict) else None
        models, truncated = _project(entries, native=native, configured=os.environ.get("WORKSHEET_CODEX_MODEL"))
        result.update(available=True, source=source, models=models, truncated=truncated)
    except CatalogFailure as error:
        result["error"] = str(error)
    except HTTPError as error:
        result["error"] = {401: "authentication", 403: "forbidden", 429: "rate_limited"}.get(error.code, "connection")
    except subprocess.TimeoutExpired:
        result["error"] = "timeout"
    except URLError:
        result["error"] = "connection"
    except (ValueError, TypeError, RecursionError):
        result["error"] = "invalid_response"
    except OSError:
        result["error"] = "execution_failed"
    result["fetched_at"] = int(time.time())
    return result


def effective_models() -> dict:
    """Вернуть каталог текущего provider с TTL 60 секунд и одним одновременным fetch.

    Не принимает параметров; provider, auth HOME, binary и default читает из
    операторского env. Возвращает независимый безопасный dict. Ключ кеша учитывает
    маршрут и смену API key через digest; сам токен в кеше не сохраняется. Lock
    объединяет одновременные fetch, ожидающий поток проверяет отмену каждые 200 мс.
    Fetch ограничен 15 секундами; результат и фиксированные ошибки кешируются
    60 секунд. Каталог не доказывает entitlement или успешный вызов каждой модели.
    В ответ попадают только ID, известные уровни reasoning и происхождение.
    Адреса и токены из чата не принимаются; сырой JSON и stderr не публикуются.
    """
    raw_provider = os.environ.get("WORKSHEET_CODEX_PROVIDER", "endpoint")
    provider = raw_provider if raw_provider in PROVIDERS else "unknown"
    credential_digest = hashlib.sha256(os.environ.get("WORKSHEET_CODEX_API_KEY", "").encode()).hexdigest() if provider == "responses_direct" else ""
    key = (provider, os.environ.get("WORKSHEET_CODEX_BINARY", ""), os.environ.get("HOME", ""),
           os.environ.get("WORKSHEET_CODEX_MODEL", ""), credential_digest)
    while not _LOCK.acquire(timeout=0.2):
        check_cancellation()
    try:
        check_cancellation()
        cached = _CACHE.get(key)
        if cached is not None and time.monotonic() - cached[0] < CACHE_TTL:
            return deepcopy(cached[1])
        result = _fetch(provider)
        _CACHE.clear()
        _CACHE[key] = (time.monotonic(), deepcopy(result))
        return result
    finally:
        _LOCK.release()
