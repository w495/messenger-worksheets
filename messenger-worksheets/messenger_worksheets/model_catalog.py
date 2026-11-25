"""Получить ограниченную публичную проекцию каталога моделей текущего worker."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import html
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from .model_http import JobCancelled, check_cancellation, post_model_response


CACHE_TTL = 60.0
FETCH_TIMEOUT = 15.0
MAX_NATIVE_BYTES = 16 * 1024 * 1024
MAX_API_BYTES = 1024 * 1024
MAX_MODELS = 256
MAX_PUBLIC_BYTES = 60 * 1024
MAX_CONTEXT_WINDOW = 100_000_000
DETAIL_SOURCES = frozenset({"codex_catalog", "openai_docs", "api_metadata", "unavailable"})
MODALITIES = ("text", "image", "audio", "video")
OFFICIAL_HOSTS = frozenset({"openai.com", "www.openai.com", "platform.openai.com", "developers.openai.com"})
BASE_MODEL_FIELDS = frozenset({"id", "is_default", "reasoning_efforts", "default_reasoning_effort"})
DETAIL_MODEL_FIELDS = frozenset({"description", "context_window", "max_context_window", "input_modalities", "details_source", "details_url", "reasoning_source", "reasoning_descriptions"})
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
    Старые записи из четырёх полей дополняются неизвестными metadata, полная
    запись содержит двенадцать полей; предыдущая версия из одиннадцати полей
    принимается с пустыми reasoning descriptions. Описания — plain text, context bounded,
    ссылки только официальные HTTPS. После нормализации применяется общий лимит.
    Сеть и ФС не используются; инструкции и credentials не пропускаются.
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
    if len(json.dumps(value, ensure_ascii=False).encode()) > 64 * 1024:
        raise ValueError("model catalog exceeds limit")
    checked = deepcopy(value)
    identifiers = set()
    normalized = []
    for item in checked["models"]:
        if (not isinstance(item, dict) or item.keys() not in (BASE_MODEL_FIELDS, BASE_MODEL_FIELDS | (DETAIL_MODEL_FIELDS - {"reasoning_descriptions"}), BASE_MODEL_FIELDS | DETAIL_MODEL_FIELDS)
                or not isinstance(item["id"], str) or MODEL_ID.fullmatch(item["id"]) is None or item["id"] in identifiers
                or type(item["is_default"]) is not bool or not isinstance(item["reasoning_efforts"], list)
                or len(item["reasoning_efforts"]) > len(EFFORTS)
                or any(not isinstance(level, str) or level not in EFFORTS for level in item["reasoning_efforts"])
                or len(set(item["reasoning_efforts"])) != len(item["reasoning_efforts"])
                or (item["default_reasoning_effort"] is not None and _safe_effort(item["default_reasoning_effort"]) is None)):
            raise ValueError("invalid catalog model")
        if item.keys() == BASE_MODEL_FIELDS:
            item.update(_empty_details())
            if value["source"] == "codex_active_catalog" and item["reasoning_efforts"]:
                item["reasoning_source"] = "codex_catalog"
        item.setdefault("reasoning_descriptions", {})
        if (not isinstance(item["description"], str) or len(item["description"]) > 600
                or _plain_description(item["description"]) != item["description"]
                or any(item[key] is not None and _context_window(item[key]) is None for key in ("context_window", "max_context_window"))
                or not isinstance(item["input_modalities"], list) or len(item["input_modalities"]) > len(MODALITIES)
                or any(not isinstance(mode, str) or mode not in MODALITIES for mode in item["input_modalities"])
                or len(set(item["input_modalities"])) != len(item["input_modalities"])
                or not isinstance(item["reasoning_descriptions"], dict)
                or not item["reasoning_descriptions"].keys() <= set(item["reasoning_efforts"])
                or any(not isinstance(text, str) or len(text) > 300 or _plain_description(text) != text
                       for text in item["reasoning_descriptions"].values())
                or not isinstance(item["details_source"], str) or item["details_source"] not in DETAIL_SOURCES
                or not isinstance(item["reasoning_source"], str) or item["reasoning_source"] not in DETAIL_SOURCES
                or (item["details_url"] is not None and _official_url(item["details_url"]) is None)):
            raise ValueError("invalid catalog model details")
        identifiers.add(item["id"])
        normalized.append(item)
    checked["models"], extra_truncation = _bounded_models(normalized)
    checked["truncated"] = checked["truncated"] or extra_truncation
    return checked


def _empty_details() -> dict:
    """Вернуть независимые пустые поля описания и происхождения модели.

    Параметров нет. None и пустые списки означают отсутствие подтверждённых
    данных, не отсутствие возможности у модели. Сеть и ФС не используются;
    новый dict можно безопасно дополнять без изменения соседних записей.
    """
    return {"description": "", "context_window": None, "max_context_window": None,
            "input_modalities": [], "details_source": "unavailable", "details_url": None,
            "reasoning_source": "unavailable", "reasoning_descriptions": {}}


def _plain_description(value: object) -> str:
    """Свести одно описание каталога к максимум 600 символам обычного текста.

    Нестроковые значения дают пустую строку. В ограниченном префиксе удаляются
    HTML/Markdown-разметка, управляющие и скрытые Unicode-символы, схлопываются
    пробелы. Функция не выполняет текст, не следует ссылкам и не читает env/ФС;
    базовые инструкции и произвольные поля модельной записи сюда не передаются.
    """
    if not isinstance(value, str):
        return ""
    text = html.unescape(value[:8192])
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = "".join(" " if char.isspace() else char for char in text if char.isspace() or not unicodedata.category(char).startswith("C"))
    text = re.sub(r"[*_`#<>\[\]\\]", "", text)
    return " ".join(text.split())[:600].rstrip()


def _context_window(value: object) -> int | None:
    """Проверить необязательное число токенов без преобразования строк или bool.

    Принимает произвольное значение, возвращает только положительный int до
    MAX_CONTEXT_WINDOW либо None. Граница защищает публичную schema от мусорных
    метаданных; сетевых обращений и изменений состояния функция не выполняет.
    """
    return value if type(value) is int and 0 < value <= MAX_CONTEXT_WINDOW else None


def _official_url(value: object) -> str | None:
    """Пропустить только короткий HTTPS URL официального сайта OpenAI без секретов.

    Принимает произвольное значение. Возвращает исходную строку только для
    allowlisted host, без userinfo, нестандартного порта, query, fragment и
    управляющих символов; иначе None. URL служит ссылкой на источник, функция
    не открывает его и не позволяет задавать сетевой маршрут из чата.
    """
    if not isinstance(value, str) or len(value) > 600 or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname in OFFICIAL_HOSTS and url.port in (None, 443)
                and url.username is None and url.password is None and not url.query and not url.fragment
                and (not url.path or re.fullmatch(r"/[A-Za-z0-9_./%-]*", url.path) is not None)
                and not any(char in value for char in "<>`\\")):
            return value
    except ValueError:
        pass
    return None


def _bounded_models(models: list[dict]) -> tuple[list[dict], bool]:
    """Ограничить готовую публичную проекцию числом записей и размером JSON.

    Принимает уже проверенные записи, сохраняет их порядок и возвращает список
    плюс признак усечения. Учитывает все поля описаний, чтобы расширенная schema
    и нормализация старых worker оставались в MAX_PUBLIC_BYTES/MAX_MODELS.
    Ввод-вывод и изменение исходного списка отсутствуют.
    """
    result = []
    for candidate in models:
        if len(result) >= MAX_MODELS or len(json.dumps([*result, candidate], ensure_ascii=False).encode()) > MAX_PUBLIC_BYTES:
            return result, True
        result.append(candidate)
    return result, False


def _model_card(identifier: str) -> dict:
    """Получить ограниченное обогащение ровно этого API ID из официальных карточек.

    Принимает уже проверенный безопасный ID. Не ищет похожие семейства и не
    генерирует характеристики; подключённый model_cards выполняет cached lookup
    с одним ограниченным обновлением индекса. При отсутствии helper возвращает
    пустой dict. Ошибка справочного источника не скрывает API каталог; отмена
    job передаётся вызывающему. Проекция повторно очищает все поля и ссылки.
    """
    try:
        from .model_cards import model_card
    except ImportError:
        return {}
    try:
        return model_card(identifier)
    except JobCancelled:
        raise
    except Exception:
        check_cancellation()
        return {}


def _details(entry: dict, *, native: bool) -> dict:
    """Очистить только разрешённые метаданные одной API/native записи каталога.

    Возвращает описание, context windows и известные input modalities с точной
    меткой происхождения. Неверные/отсутствующие значения становятся неизвестными.
    Произвольные поля, base instructions и URL из исходного каталога игнорируются;
    сетевых обращений, env-чтения и изменения записи функция не выполняет.
    """
    details = _empty_details()
    details["description"] = _plain_description(entry.get("description"))
    for key in ("context_window", "max_context_window"):
        details[key] = _context_window(entry.get(key))
    modes = entry.get("input_modalities")
    if isinstance(modes, list):
        details["input_modalities"] = [mode for mode in MODALITIES if mode in modes]
    if any(details[key] for key in ("description", "context_window", "max_context_window", "input_modalities")):
        details["details_source"] = "codex_catalog" if native else "api_metadata"
    return details


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



def _created_timestamp(value: object) -> int | float | None:
    """Проверить реальный API created без преобразования строк, bool или дат из имени.

    Принимает недоверенное значение эпохи Unix. Возвращает только положительный
    конечный int/float в диапазоне календаря до 9999 года, иначе None. NaN,
    бесконечности, отрицательные значения и bool не задают приоритет. Сеть, ФС
    и исходное значение не меняются; поле остаётся внутренним для сортировки.
    """
    return value if type(value) in (int, float) and 0 < value <= 253402300799 else None


def _entry_timestamp(entry: dict) -> int | float | None:
    """Получить дату API записи: достоверный created, затем только явный ISO suffix.

    Принимает уже отфильтрованную API запись с безопасным id. Сначала использует
    проверенный created; если его нет, разбирает конечный YYYY-MM-DD и проверяет
    реальность календарной даты. Число поколения не превращает в дату. Возвращает
    Unix epoch либо None; сеть, schema ответа и исходный dict не изменяются.
    """
    created = _created_timestamp(entry.get("created"))
    if created is not None:
        return created
    suffix = re.search(r"-(\d{4}-\d{2}-\d{2})$", entry["id"])
    if suffix is None:
        return None
    try:
        timestamp = (date.fromisoformat(suffix[1]) - date(1970, 1, 1)).days * 86400
        return timestamp if timestamp > 0 else None
    except ValueError:
        return None


def _gpt_generation(identifier: str) -> tuple[int, ...] | None:
    """Разобрать числовое поколение GPT естественно: 5.10 новее 5.9, 6.2 новее 6.

    Параметр identifier — проверенный ID. Возвращает tuple целых компонентов
    только после префикса gpt-; завершающие нули не создают отдельное поколение.
    Иные семейства возвращают None: o4 и gpt-4 по номеру не сравниваются.
    Функция не выводит релизную дату из имени и не выполняет ввод-вывод.
    """
    match = re.match(r"gpt-(\d+(?:\.\d+)*)", identifier)
    if match is None:
        return None
    generation = [int(part) for part in match[1].split(".")]
    while len(generation) > 1 and generation[-1] == 0:
        generation.pop()
    return tuple(generation)


def _generation_order(entries: list[dict], *, native: bool) -> list[dict]:
    """Упорядочить только GPT позиции, сохранив provider priority прочих семейств.

    Принимает уникальные eligible записи в исходном порядке и тип каталога.
    Возвращает новую последовательность: GPT поколения по убыванию, одинаковые
    поколения устойчиво в порядке provider; неизвестные семейства остаются на
    своих позициях. Не сравнивает номера o-моделей с GPT и не создаёт даты.
    Исходные записи, внешнее состояние и публичную schema не изменяет.
    """
    key = "slug" if native else "id"
    positions = [index for index, entry in enumerate(entries) if _gpt_generation(entry[key]) is not None]
    ranked = sorted((entries[index] for index in positions), key=lambda entry: _gpt_generation(entry[key]), reverse=True)
    ordered = list(entries)
    for index, entry in zip(positions, ranked):
        ordered[index] = entry
    return ordered


def _ordered_entries(entries: list[dict], *, native: bool) -> list[dict]:
    """Отсортировать и устранить дубли до проекции и лимитов публичного каталога.

    Принимает eligible записи с проверенными ID. Native сортирует GPT поколения,
    сохраняя priority внутри поколения и порядок неизвестных семейств. API ставит
    датированные записи первыми по created/ISO suffix убыванию вне зависимости от
    семейства; недатированный остаток использует естественный GPT порядок.
    Для дубля API выбирает наиболее новый достоверный created; при равенстве или
    отсутствии created сохраняет первую запись provider. Native сохраняет первый
    дубль. Сырьевые даты не выходят в schema, список не мутируется, I/O отсутствует.
    """
    key = "slug" if native else "id"
    unique = {}
    for entry in entries:
        identifier = entry[key]
        previous = unique.get(identifier)
        if previous is None:
            unique[identifier] = entry
        elif not native:
            current_date = _created_timestamp(entry.get("created"))
            previous_date = _created_timestamp(previous.get("created"))
            if current_date is not None and (previous_date is None or current_date > previous_date):
                unique[identifier] = entry
    candidates = list(unique.values())
    if native:
        return _generation_order(candidates, native=True)
    dated = [entry for entry in candidates if _entry_timestamp(entry) is not None]
    undated = [entry for entry in candidates if _entry_timestamp(entry) is None]
    return sorted(dated, key=_entry_timestamp, reverse=True) + _generation_order(undated, native=False)

def _project(entries: object, *, native: bool, configured: str | None) -> tuple[list[dict], bool]:
    """Построить ограниченную публичную проекцию недоверенного списка моделей.

    Параметры: entries — список записей JSON; native выбирает схему Codex,
    configured — настроенный ID для отметки default. Возвращает пару списка
    моделей и признака усечения. Пропускает скрытые native-записи, нетекстовые
    API-модели, неизвестные поля и небезопасные ID; reasoning проверяет по
    allowlist. API metadata могут дополняться точной официальной карточкой через
    cached helper с ограниченным обновлением индекса, без запросов к каждой
    модели. CatalogFailure означает неверный корневой тип. Число и полный размер
    результата ограничены MAX_MODELS и MAX_PUBLIC_BYTES; raw инструкции исключены.
    Сырьевые eligible записи дедуплицируются и сортируются до обогащения и лимитов,
    чтобы старые элементы не вытесняли более новые при усечении. Даты в schema
    не добавляются; порядок готового списка сохраняет _bounded_models.
    """
    if not isinstance(entries, list):
        raise CatalogFailure("invalid_response")
    models = {}
    eligible = []
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
        eligible.append(entry)
    for entry in _ordered_entries(eligible, native=native):
        identifier = entry["slug" if native else "id"]
        details = _details(entry, native=native)
        levels = entry.get("supported_reasoning_levels", [])
        efforts = {effort for item in levels if isinstance(item, dict)
                   and (effort := _safe_effort(item.get("effort"))) is not None} if isinstance(levels, list) else set()
        if not native and isinstance(entry.get("reasoning_efforts"), list):
            efforts.update(effort for item in entry["reasoning_efforts"] if (effort := _safe_effort(item)) is not None)
        default = _safe_effort(entry.get("default_reasoning_level" if native else "default_reasoning_effort"))
        descriptions = {item["effort"]: _plain_description(item.get("description"))[:300].rstrip()
                        for item in levels if isinstance(item, dict) and _safe_effort(item.get("effort")) is not None
                        and _plain_description(item.get("description"))} if isinstance(levels, list) else {}
        if not native and isinstance(entry.get("reasoning_descriptions"), dict):
            descriptions.update({level: _plain_description(text)[:300].rstrip() for level, text in entry["reasoning_descriptions"].items()
                                 if _safe_effort(level) is not None and _plain_description(text)})
        if efforts or default is not None:
            details["reasoning_source"] = "codex_catalog" if native else "api_metadata"
        if not native:
            card = _model_card(identifier)
            if isinstance(card, dict) and card.get("details_source") == "openai_docs" and _official_url(card.get("details_url")):
                card_details = _details(card, native=False)
                for key in ("description", "context_window", "max_context_window", "input_modalities"):
                    if card_details[key]:
                        details[key] = card_details[key]
                details.update(details_source="openai_docs", details_url=_official_url(card["details_url"]))
                card_efforts = card.get("reasoning_efforts")
                if isinstance(card_efforts, list):
                    documented = {effort for item in card_efforts if (effort := _safe_effort(item)) is not None}
                    if documented:
                        efforts = documented
                        default = _safe_effort(card.get("default_reasoning_effort"))
                        details["reasoning_source"] = "openai_docs"
                        raw_descriptions = card.get("reasoning_descriptions", {})
                        descriptions = {level: _plain_description(text)[:300].rstrip() for level, text in raw_descriptions.items()
                                        if _safe_effort(level) is not None and _plain_description(text)} if isinstance(raw_descriptions, dict) else {}
        details["reasoning_descriptions"] = {level: text for level, text in descriptions.items() if level in efforts}
        models[identifier] = {"id": identifier, "is_default": identifier == configured,
                              "reasoning_efforts": [item for item in EFFORTS if item in efforts],
                              "default_reasoning_effort": default, **details}
    return _bounded_models(list(models.values()))


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
