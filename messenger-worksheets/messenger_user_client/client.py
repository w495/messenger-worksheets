"""Минимальный user-authorized клиент Яндекс Мессенджера.

Модуль намеренно не предоставляет CLI и не хранит токены. Сетевые границы
собраны в небольших приватных методах, чтобы unit-тесты не выполняли live-
мутаций, а вызывающий код мог явно обработать неизвестный исход операции.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import stat
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class MessengerError(RuntimeError):
    """Базовая безопасная ошибка клиента без тела удалённого ответа."""


class AuthenticationError(MessengerError):
    """Токен отсутствует, отвергнут или identity невозможно подтвердить."""


class PermissionDenied(MessengerError):
    """Сервер запретил операцию подтверждённому пользователю."""


class ConfirmationError(MessengerError):
    """Fingerprint отсутствует, имеет неверный формат или не совпадает."""


class OperationUncertain(MessengerError):
    """Запрос мутации мог быть принят сервером, но ответ не был получен."""


class _FanoutTransportError(Exception):
    """Внутренняя очищенная ошибка WebSocket-транспорта."""

    def __init__(self, category: str) -> None:
        """Создать очищенную транспортную ошибку с безопасной категорией.

        Параметры:
            category: Нормализованная категория сбоя без текста удалённого ответа.

        Побочные эффекты:
            Инициализирует базовое исключение только значением категории.

        Безопасность:
            Метод намеренно не принимает исходное исключение, тело ответа,
            заголовки или OAuth-токен, поэтому эти данные не могут попасть в
            публичное сообщение ошибки.
        """
        self.category = category
        super().__init__(category)


_REGISTRY_BASE = {
    "prod": "https://messenger.360.yandex.ru/api/",
    "alpha": "https://api.messenger.alpha.yandex.ru/api/",
}
_FANOUT_URL = "wss://push.yandex.ru/v2/subscribe/websocket"
_FANOUT_SERVICE = {"prod": "messenger-prod:version5", "alpha": "messenger:version5"}
_MEDIA_UPLOAD_BASE = "https://files.messenger.yandex.ru/media_upload/"
_MEDIA_DOWNLOAD_BASE = "https://files.messenger.yandex.ru/file_shortterm/"
_REGISTRY_METHODS = frozenset({"request_user", "get_chats_info"})
_FANOUT_PATHS = frozenset({"history", "message_info", "push"})
_READ_ONLY_FANOUT = frozenset({"history", "message_info"})
_REACTIONS = {"✅": 100101, "👀": 100110, "💔": 100116}
_REACTION_ACTIONS = {"add": 0, "remove": 1, "replace": 2}
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_MAX_WIRE_JSON = 4 * 1024 * 1024
_CHAT_RE = re.compile(r"^[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+(?:_[0-9]+)?$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Запрещает urllib автоматически переносить OAuth на другой origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        """Запретить urllib автоматически следовать HTTP-redirect.

        Параметры передаются стандартной библиотекой при обработке redirect и
        намеренно не анализируются. Метод всегда возвращает ``None``, чем
        заставляет вызывающий код обработать перенаправление явно.

        Безопасность:
            Запрет не позволяет urllib перенести OAuth-заголовок на новый origin.
        """
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())


def _compact_json(value: Any) -> bytes:
    """Сериализовать значение в детерминированный компактный JSON UTF-8.

    Параметры:
        value: JSON-совместимое значение.

    Возвращает:
        Байты UTF-8 с отсортированными ключами и без незначащих пробелов.

    Исключения:
        ``TypeError`` или ``ValueError`` от ``json.dumps`` для
        несериализуемого значения.

    Безопасность:
        Каноническая форма используется при расчёте checksum и fingerprint;
        функция не логирует и не сохраняет исходное значение.
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    """Вернуть значение первого присутствующего ключа из заданного порядка.

    Параметры:
        mapping: Отображение с wire-данными.
        *keys: Приоритетный список допустимых имён поля.

    Возвращает:
        Значение первого найденного ключа либо ``None``.

    Безопасность:
        Функция не перебирает и не копирует неизвестные поля отображения.
    """
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Оставить значение только если оно является отображением.

    Параметры:
        value: Недоверенное wire-значение.

    Возвращает:
        Исходное отображение или новый пустой словарь для любого другого типа.

    Безопасность:
        Fail-closed преобразование не пытается интерпретировать произвольные
        объекты как словари.
    """
    return value if isinstance(value, Mapping) else {}


def _safe_scalar(value: Any) -> str | int | bool | None:
    """Пропустить только безопасный скаляр из недоверенного ответа.

    Параметры:
        value: Любое wire-значение.

    Возвращает:
        Строку, целое число, булево значение или ``None``; сложные объекты
        заменяются на ``None``.

    Безопасность:
        Функция не возвращает вложенные структуры, URL или заголовки целиком.
    """
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return None


def _transport_category(exc: BaseException) -> str:
    """Свести транспортный или handshake-сбой к безопасной категории.

    Параметры:
        exc: Исключение транспортной библиотеки; его текст считается
            недоверенным и не читается.

    Возвращает:
        ``http_<код>``, ``timeout``, ``tls``, ``connection`` либо уже очищенную
        категорию ``_FanoutTransportError``.

    Безопасность:
        Текст исключения, response body и заголовки не включаются в результат;
        рекурсивно рассматривается только типизированная причина ``reason``.
    """

    if isinstance(exc, _FanoutTransportError):
        return exc.category
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        return f"http_{status}"
    class_name = type(exc).__name__.lower()
    if isinstance(exc, TimeoutError) or "timeout" in class_name:
        return "timeout"
    if isinstance(exc, ssl.SSLError) or any(
        marker in class_name for marker in ("ssl", "tls", "certificate")
    ):
        return "tls"
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException) and reason is not exc:
        nested = _transport_category(reason)
        if nested in {"timeout", "tls"} or nested.startswith("http_"):
            return nested
    return "connection"


def _normalize_login(value: Any) -> str | int | bool | None:
    """Нормализовать только корпоративный login, не затрагивая email.

    Суффикс ``@yandex-team.ru`` удаляется только из login/nickname. Значения с
    другими доменами возвращаются дословно, чтобы не приписывать аккаунту
    неподтверждённый короткий login.

    Параметры:
        value: Значение login/nickname из недоверенного identity-ответа.

    Возвращает:
        Безопасный скаляр с удалённым корпоративным суффиксом либо исходный
        безопасный скаляр; сложное значение превращается в ``None``.

    Безопасность:
        Email не синтезируется и домены, отличные от точного корпоративного,
        никогда не сокращаются.
    """

    scalar = _safe_scalar(value)
    suffix = "@yandex-team.ru"
    if isinstance(scalar, str) and scalar.endswith(suffix) and len(scalar) > len(suffix):
        return scalar[: -len(suffix)]
    return scalar


def _sanitize_media_item(value: Any, media_type: str) -> dict[str, Any]:
    """Сформировать allowlisted публичные метаданные одного media-объекта.

    Параметры:
        value: Недоверенный media-блок или ``FileInfo`` из wire-ответа.
        media_type: Нормализованный тип, который будет записан в ``type``.

    Возвращает:
        Словарь с типом и только разрешёнными скалярными полями файла.

    Безопасность:
        URL загрузки, заголовки, токены и неизвестные расширения протокола
        отбрасываются, вложенные значения не копируются.
    """
    item = _as_mapping(value)
    file_info = _as_mapping(_first(item, "FileInfo", "file_info"))
    if file_info:
        item = {**item, **file_info}
    result: dict[str, Any] = {"type": media_type}
    fields = {
        "file_id": ("file_id", "FileId", "Id2", "id", "Id"),
        "name": ("name", "Name", "FileName"),
        "size": ("size", "Size", "FileSize"),
        "width": ("width", "Width"),
        "height": ("height", "Height"),
        "content_type": ("content_type", "ContentType", "MimeType"),
    }
    for target, sources in fields.items():
        field = _safe_scalar(_first(item, *sources))
        if field is not None:
            result[target] = field
    return result


def render_message(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Нормализовать wire-сообщение, не возвращая неизвестные поля.

    Из ответа выбирается только фиксированный набор скалярных полей и безопасные
    метаданные media. URL, заголовки и любые непонятные расширения протокола не
    копируются. Наличие незнакомого content помечается ``unknown_content``.

    Параметры:
        raw: Недоверенное wire-сообщение или envelope MessageInfo.

    Возвращает:
        Нормализованный словарь с координатами, автором, текстом, PayloadId,
        признаком удаления и allowlisted media-метаданными.

    Исключения:
        ``TypeError`` если ``raw`` не является отображением.

    Безопасность:
        Результат строится по allowlist; неизвестный content лишь отмечается
        флагом и не переносится в публичный результат.
    """

    if not isinstance(raw, Mapping):
        raise TypeError("raw должен быть mapping")

    # Фактический envelope: ServerMessage -> ClientMessage.Plain и
    # ServerMessageInfo. Fallback на сам raw оставлен для MessageInfo-ответа,
    # который уже может указывать на внутренний ServerMessage.
    server = _as_mapping(_first(raw, "ServerMessage")) or raw
    client_message = _as_mapping(_first(server, "ClientMessage"))
    plain = _as_mapping(_first(client_message, "Plain"))
    info = _as_mapping(_first(server, "ServerMessageInfo", "MessageInfo"))
    if not info:
        info = server
    if not plain:
        plain = _as_mapping(_first(server, "Plain"))

    author_value = _first(info, "From", "Author", "author")
    author_block = _as_mapping(author_value)
    author = (
        _safe_scalar(_first(author_block, "name", "Name", "DisplayName", "login", "Login"))
        if author_block
        else _safe_scalar(author_value)
    )
    author_guid = _safe_scalar(
        _first(info, "author_guid", "AuthorGuid", "FromGuid")
    )
    if author_guid is None and author_block:
        author_guid = _safe_scalar(
            _first(author_block, "guid", "Guid", "uid", "Uid")
        )
    elif author_guid is None and isinstance(author_value, (str, int)):
        author_guid = author_value
    result: dict[str, Any] = {
        "ts": _safe_scalar(_first(info, "ts", "timestamp", "Timestamp")),
        "seq": _safe_scalar(_first(info, "seq", "sequence", "SeqNo", "Seq")),
        "version": _safe_scalar(_first(info, "version", "Version")),
        "author": author,
        "author_guid": author_guid,
        "text": None,
        "payload_id": _safe_scalar(
            _first(plain, "payload_id", "PayloadId", "PayloadID")
        ),
        "deleted": bool(_first(info, "deleted", "Deleted", "IsDeleted") or False),
    }
    text_block = _as_mapping(_first(plain, "text", "Text"))
    text = _first(text_block, "message_text", "MessageText", "text", "Text")
    if not text_block:
        text = _first(plain, "MessageText")
    result["text"] = text if isinstance(text, str) else None

    media: list[dict[str, Any]] = []
    recognized_content = result["text"] is not None or not plain
    misc_file = _first(plain, "MiscFile")
    if misc_file is not None:
        media.append(_sanitize_media_item(misc_file, "document"))
        recognized_content = True
    gallery = _as_mapping(_first(plain, "Gallery"))
    if gallery:
        recognized_content = True
        gallery_text = _first(gallery, "Text")
        if result["text"] is None and isinstance(gallery_text, str):
            result["text"] = gallery_text
        elif result["text"] is None and isinstance(gallery_text, Mapping):
            nested_text = _first(gallery_text, "MessageText", "Text")
            if isinstance(nested_text, str):
                result["text"] = nested_text
        entries = _first(gallery, "Items")
        if isinstance(entries, list):
            for entry in entries:
                entry_map = _as_mapping(entry)
                image = _first(entry_map, "Image")
                if image is not None:
                    media.append(_sanitize_media_item(image, "gallery"))
    if media:
        result["media"] = media

    known_plain_keys = {"ChatId", "Text", "PayloadId", "Timestamp", "Gallery", "MiscFile"}
    if plain and (
        not recognized_content or any(key not in known_plain_keys for key in plain)
    ):
        result["unknown_content"] = True
    return result


def thread_chat_id(parent_chat_id: str, timestamp: int | str) -> str:
    """Построить id треда группового чата по правилу ``(n+100)/k/id_ts``.

    Параметры:
        parent_chat_id: Валидный id родительского, а не тредового чата.
        timestamp: Неотрицательная целочисленная метка родительского сообщения.

    Возвращает:
        Канонический chat id треда.

    Исключения:
        ``ValueError`` для некорректного chat id, тредового родителя,
        нечислового timestamp или нечислового первого сегмента.

    Безопасность:
        Валидация не позволяет использовать путь или произвольный wire-id.
    """

    _validate_chat_id(parent_chat_id, allow_thread=False)
    ts = str(timestamp)
    if not ts.isdigit() or len(ts) > 20:
        raise ValueError("timestamp должен быть неотрицательным целым")
    first, second, third = parent_chat_id.split("/")
    if not first.isdigit():
        raise ValueError("первый сегмент chat_id должен быть числом")
    return f"{int(first) + 100}/{second}/{third}_{ts}"


def _validate_chat_id(value: str, *, allow_thread: bool = True) -> None:
    """Проверить chat id по фиксированному синтаксису проекта.

    Параметры:
        value: Проверяемый идентификатор чата.
        allow_thread: Разрешать ли суффикс timestamp треда.

    Исключения:
        ``ValueError`` при неверном типе, формате или запрещённом тредовом id.

    Безопасность:
        Полный regex исключает разделители пути и управляющие символы.
    """
    if not isinstance(value, str) or not _CHAT_RE.fullmatch(value):
        raise ValueError("некорректный chat_id")
    if not allow_thread and re.search(r"_[0-9]+$", value):
        raise ValueError("ожидался id родительского чата")


def _validate_plain_text(value: str, *, field: str, limit: int = 100_000) -> None:
    """Проверить обязательное текстовое поле и его UTF-8-размер.

    Параметры:
        value: Непустой текст.
        field: Безопасное имя поля для диагностического сообщения.
        limit: Максимальный размер текста в байтах UTF-8.

    Исключения:
        ``ValueError`` для неверного типа, пустой строки, превышения лимита или
        наличия NUL.

    Безопасность:
        Само значение не включается в текст исключения.
    """
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError(f"некорректное поле {field}")
    if "\x00" in value:
        raise ValueError(f"поле {field} содержит NUL")


def _murmurhash2(data: bytes, seed: int = 0) -> int:
    """Вычислить 32-битный MurmurHash2 для checksum DATA frame.

    Параметры:
        data: Байты канонического JSON payload.
        seed: Начальное значение алгоритма.

    Возвращает:
        Беззнаковую 32-битную checksum, совместимую с Fanout wire-форматом.

    Безопасность:
        Это checksum совместимости, а не криптографический MAC; функция не
        должна использоваться для аутентификации данных.
    """

    m = 0x5BD1E995
    value = (seed ^ len(data)) & 0xFFFFFFFF
    offset = 0
    while offset + 4 <= len(data):
        word = struct.unpack_from("<I", data, offset)[0]
        word = (word * m) & 0xFFFFFFFF
        word ^= word >> 24
        word = (word * m) & 0xFFFFFFFF
        value = (value * m) & 0xFFFFFFFF
        value ^= word
        offset += 4
    tail = data[offset:]
    if len(tail) == 3:
        value ^= tail[2] << 16
    if len(tail) >= 2:
        value ^= tail[1] << 8
    if tail:
        value ^= tail[0]
        value = (value * m) & 0xFFFFFFFF
    value ^= value >> 13
    value = (value * m) & 0xFFFFFFFF
    value ^= value >> 15
    return value & 0xFFFFFFFF


def _msgpack_int(value: int) -> bytes:
    """Закодировать неотрицательный request id в минимальный msgpack uint.

    Параметры:
        value: Целое число в диапазоне uint64.

    Возвращает:
        Wire-представление fixint/uint8/uint16/uint32/uint64.

    Исключения:
        ``ValueError`` если значение отрицательное или превышает uint64.
    """
    if 0 <= value <= 0x7F:
        return bytes([value])
    if 0 <= value <= 0xFF:
        return b"\xcc" + struct.pack("B", value)
    if 0 <= value <= 0xFFFF:
        return b"\xcd" + struct.pack(">H", value)
    if 0 <= value <= 0xFFFFFFFF:
        return b"\xce" + struct.pack(">I", value)
    if 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        return b"\xcf" + struct.pack(">Q", value)
    raise ValueError("request id вне диапазона uint64")


def _msgpack_string(value: str) -> bytes:
    """Закодировать строку пути в поддерживаемое msgpack-представление.

    Параметры:
        value: Строка, кодируемая в UTF-8.

    Возвращает:
        Байты fixstr, str8 или str16 вместе с UTF-8 payload.

    Исключения:
        ``UnicodeEncodeError`` для некодируемой строки и ``ValueError`` при
        размере свыше uint16.
    """
    encoded = value.encode("utf-8")
    size = len(encoded)
    if size <= 31:
        return bytes([0xA0 | size]) + encoded
    if size <= 0xFF:
        return b"\xd9" + struct.pack("B", size) + encoded
    if size <= 0xFFFF:
        return b"\xda" + struct.pack(">H", size) + encoded
    raise ValueError("строка msgpack слишком длинная")


def _encode_data_frame(request_id: int, path: str, payload: Mapping[str, Any]) -> bytes:
    """Собрать бинарный DATA frame согласно wire-формату Fanout.

    Заголовок состоит из маркера 0x01, компактного msgpack-массива
    ``[0, request_id, path]``, little-endian версии 5 и uint64 checksum, у
    которого старшие 32 бита нулевые, а младшие равны MurmurHash2 JSON payload.

    Параметры:
        request_id: Положительный идентификатор wire-запроса в пределах uint64.
        path: Один из разрешённых Fanout path.
        payload: JSON-совместимый словарь запроса.

    Возвращает:
        Полный бинарный DATA frame.

    Исключения:
        ``ValueError`` для path вне allowlist, слишком большого payload или
        неподдерживаемых значений заголовка; ошибки JSON-сериализации
        пробрасываются вызывающему коду.

    Безопасность:
        Path ограничен allowlist, а payload — максимальным wire-размером.
    """

    if path not in _FANOUT_PATHS:
        raise ValueError("fanout path отсутствует в allowlist")
    body = _compact_json(payload)
    if len(body) > _MAX_WIRE_JSON:
        raise ValueError("fanout payload слишком велик")
    header = b"\x93\x00" + _msgpack_int(request_id) + _msgpack_string(path)
    return b"\x01" + header + struct.pack("<I", 5) + struct.pack(
        "<Q", _murmurhash2(body)
    ) + body


def _decode_data_frame(frame: bytes) -> dict[str, Any]:
    """Извлечь первый полностью декодируемый JSON object из DATA frame.

    Служебный заголовок Fanout имеет переменную длину. Поэтому декодер перебирает
    только позиции байта ``{`` и принимает хвост лишь при полном UTF-8/JSON
    разборе. Частичный JSON и значения, отличные от object, отклоняются.

    Параметры:
        frame: Недоверенный бинарный DATA frame Fanout.

    Возвращает:
        Первый полностью декодированный JSON object из payload.

    Исключения:
        ``MessengerError`` для неверного типа/маркера или отсутствия полного
        JSON object.

    Безопасность:
        Частичный JSON, массивы и скаляры не принимаются; диагностическое
        сообщение не содержит сырых байтов ответа.
    """

    if not isinstance(frame, (bytes, bytearray)) or not frame or frame[0] != 1:
        raise MessengerError("некорректный DATA frame")
    raw = bytes(frame)
    for offset, byte in enumerate(raw[1:], start=1):
        if byte != 0x7B:
            continue
        try:
            candidate = json.loads(raw[offset:].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(candidate, dict):
            return candidate
    raise MessengerError("DATA frame не содержит полного JSON object")


class MessengerUserClient:
    """User-authorized адаптер с fail-closed identity и preview мутаций.

    Токен используется только в заголовке OAuth, не включается в fingerprint,
    repr, receipt или исключения. Любая операция чтения и записи сначала
    подтверждает identity и заданные ожидания аккаунта.
    """

    def __init__(
        self,
        token: str,
        account: str = "work",
        environment: str = "prod",
        expected_login: str | None = None,
        expected_email: str | None = None,
        expected_uid: str | None = None,
        expected_organization_id: str | None = None,
        enforce_account_class: bool = True,
        timeout: float = 30,
    ) -> None:
        """Создать клиент, не выполняя сетевых запросов.

        Expectations сравниваются дословно при первом обращении к identity.
        Неподтверждённый или неожиданный аккаунт блокирует все дальнейшие
        операции чтения и записи.

        Параметры:
            token: Непустой OAuth-токен без переводов строки.
            account: Ожидаемый класс аккаунта: ``work`` или ``personal``.
            environment: Контур ``prod`` либо ``alpha``.
            expected_login: Опциональный точный login владельца токена.
            expected_email: Опциональный подтверждённый email владельца.
            expected_uid: Опциональный точный UID владельца.
            expected_organization_id: Опциональная ожидаемая организация.
            enforce_account_class: Проверять соответствие account организации.
            timeout: Сетевой timeout в секундах из диапазона ``(0, 300]``.

        Исключения:
            ``AuthenticationError`` для некорректного токена; ``ValueError``
            для неподдерживаемого account, environment или timeout.

        Побочные эффекты:
            Создаёт только локальные кеши identity и preview; сеть и файловая
            система не используются.

        Безопасность:
            Токен хранится приватно и не должен попадать в repr, fingerprint,
            receipt или сообщения исключений.
        """

        if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
            raise AuthenticationError("некорректный OAuth token")
        if account not in {"work", "personal"}:
            raise ValueError("account должен быть work или personal")
        if environment not in _REGISTRY_BASE:
            raise ValueError("environment должен быть prod или alpha")
        if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 300:
            raise ValueError("timeout должен быть в диапазоне (0, 300]")
        self._token = token
        self.account = account
        self.environment = environment
        self.expected_login = expected_login
        self.expected_email = expected_email
        self.expected_uid = expected_uid
        self.expected_organization_id = expected_organization_id
        self.enforce_account_class = bool(enforce_account_class)
        self.timeout = float(timeout)
        self._identity_cache: dict[str, Any] | None = None
        self._pending: dict[str, dict[str, Any]] = {}

    def __repr__(self) -> str:
        """Вернуть безопасное диагностическое представление клиента.

        Возвращает:
            Строку с классом аккаунта и контуром, но с маркером ``<redacted>``
            вместо OAuth-токена.

        Безопасность:
            Метод никогда не сериализует фактическое значение токена.
        """
        return (
            f"{type(self).__name__}(account={self.account!r}, "
            f"environment={self.environment!r}, token=<redacted>)"
        )

    def _oauth_headers(self, *, json_accept: bool = True) -> dict[str, str]:
        """Собрать OAuth-заголовки для запроса к доверенному endpoint.

        Параметры:
            json_accept: Добавлять ли ``Accept: application/json``.

        Возвращает:
            Новый словарь с Authorization и идентификатором приложения.

        Безопасность:
            Словарь содержит секрет и предназначен только для прямого запроса
            к фиксированному allowlisted host; его нельзя логировать, включать
            в fingerprint или переносить на redirect.
        """
        headers = {
            "Authorization": f"OAuth {self._token}",
            "X-Application-Id": "Yamb-web",
        }
        if json_accept:
            headers["Accept"] = "application/json"
        return headers

    def _registry(self, method: str, params: Mapping[str, Any]) -> Any:
        """Выполнить один allowlisted POST-вызов Registry без redirect.

        Параметры:
            method: Имя метода из ``_REGISTRY_METHODS``.
            params: JSON-совместимые параметры Registry.

        Возвращает:
            Значение поля ``data`` успешного Registry-ответа.

        Исключения:
            ``ValueError`` для метода вне allowlist; ``AuthenticationError`` и
            ``PermissionDenied`` для HTTP 401/403; ``MessengerError`` для
            сетевых, HTTP-, size-, JSON- и schema-сбоев.

        Побочные эффекты:
            Выполняет один HTTPS POST к endpoint выбранного контура.

        Безопасность:
            OAuth передаётся только заголовком на фиксированный origin,
            redirect запрещён, размер ответа ограничен, а сырое тело ошибки не
            включается в исключение.
        """
        if method not in _REGISTRY_METHODS:
            raise ValueError("registry method отсутствует в allowlist")
        request_value = _compact_json({"method": method, "params": dict(params)})
        body = urllib.parse.urlencode({"request": request_value.decode("utf-8")}).encode()
        headers = self._oauth_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            _REGISTRY_BASE[self.environment], data=body, headers=headers, method="POST"
        )
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise MessengerError(f"registry HTTP {response.status}")
                raw = response.read(_MAX_WIRE_JSON + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                error = AuthenticationError if exc.code == 401 else PermissionDenied
                raise error(f"registry HTTP {exc.code}") from None
            raise MessengerError(f"registry HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MessengerError(f"registry transport error: {type(exc).__name__}") from None
        if len(raw) > _MAX_WIRE_JSON:
            raise MessengerError("registry response слишком велик")
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MessengerError("registry вернул некорректный JSON") from None
        if not isinstance(decoded, Mapping):
            raise MessengerError("registry вернул неожиданный ответ")
        status_value = decoded.get("status")
        if status_value not in {"ok", "success", 200, True}:
            raise MessengerError("registry отклонил запрос")
        if "data" not in decoded:
            raise MessengerError("registry response не содержит data")
        return decoded["data"]

    def identity(self, refresh: bool = False) -> dict[str, Any]:
        """Получить и строго проверить identity владельца OAuth-токена.

        UID и GUID обязательны. Email никогда не синтезируется из nickname.
        При mismatch expectations или класса аккаунта метод завершает работу
        ошибкой и не кеширует частично проверенные данные.

        Параметры:
            refresh: Игнорировать кеш и заново запросить владельца токена.

        Возвращает:
            Копию проверенного identity с uid, guid, login, email,
            organization_id и ``is_org_account``.

        Исключения:
            ``AuthenticationError`` для неполного identity, несовпадения любого
            expectation или класса аккаунта; ошибки Registry пробрасываются.

        Побочные эффекты:
            При отсутствии подходящего кеша выполняет Registry-запрос; после
            полной проверки обновляет локальный кеш. Неуспешный refresh очищает
            старое доказательство identity.

        Безопасность:
            UID/GUID обязательны, email не синтезируется из nickname, все явно
            заданные ожидания проверяются до кеширования результата.
        """

        if self._identity_cache is not None and not refresh:
            return dict(self._identity_cache)
        if refresh:
            # После неуспешного refresh старое identity нельзя использовать как
            # доказательство актуального владельца токена.
            self._identity_cache = None
        data = self._registry("request_user", {})
        source = _as_mapping(data)
        nested = _as_mapping(_first(source, "user", "User"))
        if nested:
            source = nested
        uid = _safe_scalar(_first(source, "uid", "Uid", "UID"))
        guid = _safe_scalar(_first(source, "guid", "Guid", "GUID"))
        if uid in {None, ""} or guid in {None, ""}:
            raise AuthenticationError("identity не содержит подтверждённые uid/guid")
        organization = _as_mapping(_first(source, "organization", "Organization"))
        organization_id = _safe_scalar(
            _first(source, "organization_id", "OrganizationId", "org_id", "OrgId")
        )
        if organization_id is None:
            organization_id = _safe_scalar(_first(organization, "id", "Id"))
        organizations_raw = _first(source, "organizations", "Organizations")
        organization_ids: list[str] = []
        if isinstance(organizations_raw, list):
            for item in organizations_raw:
                item_map = _as_mapping(item)
                candidate = (
                    _safe_scalar(
                        _first(
                            item_map,
                            "id",
                            "Id",
                            "organization_id",
                            "OrganizationId",
                        )
                    )
                    if item_map
                    else _safe_scalar(item)
                )
                if candidate not in {None, ""}:
                    organization_ids.append(str(candidate))
        if organization_id in {None, ""} and organization_ids:
            organization_id = organization_ids[0]

        direct_email = _safe_scalar(_first(source, "email", "Email"))
        contact_emails: list[str] = []
        contacts = _first(source, "contacts", "Contacts")
        if isinstance(contacts, list):
            for contact in contacts:
                contact_map = _as_mapping(contact)
                contact_type = _safe_scalar(_first(contact_map, "type", "Type"))
                contact_value = _safe_scalar(_first(contact_map, "value", "Value"))
                if (
                    isinstance(contact_type, str)
                    and contact_type.lower() == "email"
                    and isinstance(contact_value, str)
                    and contact_value
                ):
                    contact_emails.append(contact_value)
        email = direct_email if isinstance(direct_email, str) else (
            contact_emails[0] if contact_emails else None
        )
        identity = {
            "uid": str(uid),
            "guid": str(guid),
            "login": _normalize_login(_first(source, "login", "Login", "nickname")),
            "email": email,
            "organization_id": organization_id,
        }
        explicit_org = _first(source, "is_org_account", "IsOrgAccount")
        identity["is_org_account"] = (
            bool(organizations_raw)
            if isinstance(organizations_raw, list)
            else (
                explicit_org
                if isinstance(explicit_org, bool)
                else identity["organization_id"] not in {None, ""}
            )
        )
        checks = (("login", self.expected_login), ("uid", self.expected_uid))
        for field, expected in checks:
            if expected is not None and str(identity.get(field)) != str(expected):
                raise AuthenticationError(f"identity mismatch: {field}")
        if self.expected_email is not None and self.expected_email not in {
            value for value in [identity["email"], *contact_emails] if value is not None
        }:
            raise AuthenticationError("identity mismatch: email")
        known_organizations = {
            value
            for value in [
                str(identity["organization_id"])
                if identity["organization_id"] not in {None, ""}
                else None,
                *organization_ids,
            ]
            if value is not None
        }
        if (
            self.expected_organization_id is not None
            and str(self.expected_organization_id) not in known_organizations
        ):
            raise AuthenticationError("identity mismatch: organization_id")
        has_org = identity["is_org_account"]
        if self.enforce_account_class:
            if self.account == "work" and not has_org:
                raise AuthenticationError("work token не подтверждён организацией")
            if self.account == "personal" and has_org:
                raise AuthenticationError("personal token относится к организации")
        self._identity_cache = identity
        return dict(identity)

    def resolve_invite(self, invite: str) -> dict[str, Any]:
        """Разрешить invite через Registry ``get_chats_info``.

        Публичный shape стабилен: исходный ``invite_hash``, флаг ``found`` и
        найденный chat либо ``None``. В Registry передаётся только hash, а не
        произвольный URL.

        Параметры:
            invite: Bare invite id или HTTPS URL на разрешённом Yandex-host.

        Возвращает:
            Словарь с очищенным ``invite_hash``, ``found`` и первой найденной
            карточкой чата либо ``None``.

        Исключения:
            ``ValueError`` для пустого или слишком длинного значения,
            неподдержанного URL либо небезопасного invite id;
            ``MessengerError`` для неверной схемы Registry; identity- и
            Registry-ошибки пробрасываются.

        Побочные эффекты:
            Проверяет identity и выполняет read-only Registry-запрос.

        Безопасность:
            Host и HTTPS проверяются до сети; Registry получает только
            allowlisted id, автоматического вступления в чат нет.
        """

        self.identity()
        if not isinstance(invite, str) or not invite or len(invite) > 2048:
            raise ValueError("некорректный invite")
        parsed = urllib.parse.urlsplit(invite)
        if parsed.scheme:
            if parsed.scheme != "https" or not _is_yandex_host(parsed.hostname):
                raise ValueError("invite URL не входит в HTTPS allowlist")
            if parsed.fragment:
                fragment = parsed.fragment.lstrip("/")
                parts = fragment.split("/")
                if len(parts) != 2 or parts[0] != "join":
                    raise ValueError("некорректный fragment invite URL")
                invite_value = urllib.parse.unquote(parts[1])
            else:
                invite_value = urllib.parse.unquote(
                    parsed.path.rstrip("/").rsplit("/", 1)[-1]
                )
        else:
            invite_value = invite
        if not _SAFE_ID_RE.fullmatch(invite_value):
            raise ValueError("некорректный invite id")
        data = self._registry("get_chats_info", {"invite_hash": invite_value})
        if isinstance(data, list):
            chats = data
        else:
            data_map = _as_mapping(data)
            chats = _first(data_map, "Chats", "chats")
        if chats is None:
            chats = []
        if not isinstance(chats, list):
            raise MessengerError("get_chats_info не содержит список Chats")
        chat = next((dict(item) for item in chats if isinstance(item, Mapping)), None)
        return {"invite_hash": invite_value, "found": chat is not None, "chat": chat}

    def _fanout_once(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Выполнить один WebSocket round-trip с обязательной TLS-проверкой.

        Параметры:
            path: Уже проверенный Fanout path.
            payload: Уже проверенный wire-payload запроса.

        Возвращает:
            Декодированный JSON object первого DATA-ответа.

        Исключения:
            ``MessengerError`` при отсутствии websocket-client или неверном
            DATA frame; ``TimeoutError`` при истечении deadline;
            ``_FanoutTransportError`` с очищенной категорией для остальных
            транспортных сбоев.

        Побочные эффекты:
            Подтверждает identity, открывает TLS WebSocket, отправляет не более
            одного DATA frame после ping/subscribed и всегда пытается закрыть
            соединение.

        Безопасность:
            TLS hostname и certificate проверяются; OAuth передаётся только
            Fanout endpoint. Текст handshake-ошибок и отражённые заголовки не
            выходят наружу, ошибка close не подменяет основной исход.
        """

        try:
            import websocket  # type: ignore[import-not-found]
        except ImportError:
            raise MessengerError("для Fanout требуется пакет websocket-client") from None
        actor = self.identity()
        query = urllib.parse.urlencode(
            {
                "service": _FANOUT_SERVICE[self.environment],
                "client": "messenger-user-client",
                "user": actor["uid"],
                "session": str(uuid.uuid4()),
            }
        )
        ws = None
        sent = False
        try:
            ws = websocket.create_connection(
                f"{_FANOUT_URL}?{query}",
                header=[f"Authorization: OAuth {self._token}"],
                timeout=self.timeout,
                sslopt={"cert_reqs": 2, "check_hostname": True},
                suppress_origin=True,
            )
            deadline = time.monotonic() + self.timeout
            request_id = secrets.randbelow(0xFFFFFFFF) + 1
            while time.monotonic() < deadline:
                incoming = ws.recv()
                if isinstance(incoming, bytes) and incoming[:1] == b"\x01":
                    return _decode_data_frame(incoming)
                event: Any = None
                if isinstance(incoming, str):
                    try:
                        event = json.loads(incoming)
                    except json.JSONDecodeError:
                        event = incoming
                event_name = (
                    event.get("operation") if isinstance(event, Mapping) else event
                )
                if not sent and event_name in {"ping", "subscribed"}:
                    ws.send_binary(_encode_data_frame(request_id, path, payload))
                    sent = True
            raise TimeoutError("fanout timeout")
        except MessengerError:
            raise
        except Exception as exc:
            # Ни str(exc), ни атрибуты response/headers не попадают наружу:
            # handshake может содержать тело ответа или отражённые заголовки.
            raise _FanoutTransportError(_transport_category(exc)) from None
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    # Ошибка close не должна раскрывать библиотечное сообщение
                    # и подменять уже классифицированный исход операции.
                    pass

    def _fanout_request(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Выполнить allowlisted Fanout-вызов с заданной retry semantics.

        ``history`` и ``message_info`` повторяются один раз при транспортной
        ошибке. ``push`` не повторяется: потеря ответа после передачи запроса
        преобразуется в :class:`OperationUncertain`.

        Параметры:
            path: Allowlisted ``history``, ``message_info`` или ``push``.
            payload: JSON-совместимый payload точной wire-схемы path.

        Возвращает:
            Декодированный словарь Fanout-ответа.

        Исключения:
            ``ValueError`` или ``TypeError`` для path, payload или размера;
            ``OperationUncertain`` при транспортном сбое push;
            ``MessengerError`` после исчерпания read-only retry или при
            неожиданном ответе; identity-ошибки пробрасываются.

        Побочные эффекты:
            Проверяет identity и выполняет один сетевой вызов для push либо до
            двух для read-only операций.

        Безопасность:
            Wire-поля проверяются по allowlist; мутация никогда не повторяется
            автоматически, а сообщения об ошибках содержат только категорию.
        """

        if path not in _FANOUT_PATHS:
            raise ValueError("fanout path отсутствует в allowlist")
        if not isinstance(payload, Mapping):
            raise TypeError("payload должен быть mapping")
        _validate_fanout_payload(path, payload)
        try:
            encoded_payload = _compact_json(payload)
        except (TypeError, ValueError):
            raise ValueError("fanout payload не является JSON") from None
        if len(encoded_payload) > _MAX_WIRE_JSON:
            raise ValueError("fanout payload слишком велик")
        self.identity()
        attempts = 2 if path in _READ_ONLY_FANOUT else 1
        for attempt in range(attempts):
            try:
                response = self._fanout_once(path, payload)
                if not isinstance(response, dict):
                    raise MessengerError("fanout вернул неожиданный ответ")
                return response
            except (TimeoutError, OSError, urllib.error.URLError, _FanoutTransportError) as exc:
                category = _transport_category(exc)
                if path == "push":
                    raise OperationUncertain(
                        f"push outcome unknown: {category}"
                    ) from None
                if attempt + 1 == attempts:
                    raise MessengerError(
                        f"fanout transport error: {category}"
                    ) from None
        raise MessengerError("fanout failed")

    def fanout(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Вызвать read-only Fanout path ``history`` или ``message_info``.

        Публичный прямой ``push`` запрещён, поскольку он обходил бы обязательный
        preview fingerprint. Высокоуровневые методы вызывают push только после
        успешной constant-time проверки точного подтверждения.

        Параметры:
            path: Только ``history`` или ``message_info``.
            payload: Проверяемый wire-payload чтения.

        Возвращает:
            Ответ внутреннего Fanout-вызова.

        Исключения:
            ``ConfirmationError`` при попытке публичного ``push``; остальные
            ошибки валидации, identity и транспорта пробрасываются.

        Побочные эффекты:
            Выполняет read-only сетевой запрос.

        Безопасность:
            Публичный интерфейс не позволяет обойти двухфазное подтверждение
            мутаций прямым push.
        """

        if path == "push":
            raise ConfirmationError("прямой push запрещён; используйте метод мутации")
        return self._fanout_request(path, payload)

    def _confirmed_push(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Отправить подтверждённый content в точном Fanout push envelope.

        Параметры:
            payload: Ровно один блок ``Plain`` или ``Reaction``.

        Возвращает:
            Декодированный ответ Fanout push.

        Исключения:
            ``ValueError`` для неизвестного content type;
            ``OperationUncertain`` и прочие ошибки Fanout пробрасываются.

        Побочные эффекты:
            Выполняет одну неидемпотентную сетевую мутацию.

        Безопасность:
            Формирует канонический envelope с фиксированным ``UserIp`` и не
            принимает произвольные верхнеуровневые wire-поля.
        """

        if len(payload) != 1 or next(iter(payload)) not in {"Plain", "Reaction"}:
            raise ValueError("подтверждённый push имеет неизвестный content type")
        envelope = {"ClientMessage": dict(payload), "UserIp": "::1"}
        return self._fanout_request("push", envelope)

    def read_history(
        self, chat_id: str, limit: int = 30, before_ts: int | str | None = None
    ) -> dict[str, Any]:
        """Прочитать историю и вернуть совместимый нормализованный result.

        Ответ Fanout обязан содержать ``Chats`` и комнату с точным запрошенным
        ``ChatId``. Несовпадение комнаты отклоняется fail-closed.

        Параметры:
            chat_id: Точный id основного чата или треда.
            limit: Число сообщений от 1 до 100.
            before_ts: Опциональная верхняя граница timestamp для пагинации.

        Возвращает:
            Словарь с chat id, нормализованными сообщениями, их числом и
            минимальным timestamp как ``next_before_ts``.

        Исключения:
            ``ValueError`` для координат или лимита; ``MessengerError`` для
            несовместимой схемы ответа или несовпадающего ChatId; ошибки
            identity и Fanout пробрасываются.

        Побочные эффекты:
            Выполняет read-only Fanout-запрос.

        Безопасность:
            Принимается ровно одна комната с точным id; сообщения проходят
            allowlisted ``render_message`` до возврата вызывающему коду.
        """

        _validate_chat_id(chat_id)
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit должен быть в диапазоне 1..100")
        payload: dict[str, Any] = {"ChatId": chat_id, "Limit": limit}
        if before_ts is not None:
            ts = str(before_ts)
            if not ts.isdigit() or len(ts) > 20:
                raise ValueError("некорректный before_ts")
            payload["MaxTimestamp"] = ts
        response = self.fanout("history", payload)
        chats = response.get("Chats")
        if not isinstance(chats, list):
            raise MessengerError("history response не содержит Chats")
        matching = [
            item
            for item in chats
            if isinstance(item, Mapping) and item.get("ChatId") == chat_id
        ]
        if len(matching) != 1:
            raise MessengerError("history response не содержит ровно один точный ChatId")
        values = matching[0].get("Messages", [])
        if not isinstance(values, list):
            raise MessengerError("history response не содержит список сообщений")
        messages = [render_message(item) for item in values if isinstance(item, Mapping)]
        timestamps = [message["ts"] for message in messages if message.get("ts") is not None]
        oldest = min(timestamps, key=_timestamp_sort_key) if timestamps else None
        return {
            "chat_id": chat_id,
            "messages": messages,
            "count": len(messages),
            "next_before_ts": oldest,
        }

    def _fingerprint(self, operation: str, request: Mapping[str, Any]) -> str:
        """Связать мутацию с actor, аккаунтом, контуром и точным запросом.

        Параметры:
            operation: Каноническое имя высокоуровневой операции.
            request: Канонический JSON-совместимый запрос мутации.

        Возвращает:
            Нижнерегистровый SHA-256 fingerprint длиной 64 hex-символа.

        Исключения:
            Ошибки identity и JSON-сериализации пробрасываются.

        Побочные эффекты:
            При отсутствии кеша может выполнить read-only identity-запрос.

        Безопасность:
            В envelope входят только uid/guid и координаты операции; OAuth-токен
            никогда не хешируется и не сериализуется.
        """
        actor = self.identity()
        envelope = {
            "actor": {"uid": actor["uid"], "guid": actor["guid"]},
            "account": self.account,
            "environment": self.environment,
            "operation": operation,
            "request": dict(request),
        }
        return hashlib.sha256(_compact_json(envelope)).hexdigest()

    def _pending_field(
        self, fingerprint: str | None, operation: str, field: str
    ) -> Any:
        """Восстановить поле ранее показанного preview для confirm-вызова.

        Параметры:
            fingerprint: Fingerprint подтверждаемого preview или ``None``.
            operation: Ожидаемое имя операции.
            field: Имя поля канонического request.

        Возвращает:
            Сохранённое значение поля либо ``None`` при отсутствии точного
            pending-preview.

        Безопасность:
            Значение возвращается только при совпадении fingerprint и операции,
            что предотвращает подмену сгенерированного payload id между preview.
        """
        if fingerprint is None:
            return None
        pending = self._pending.get(fingerprint)
        if pending and pending.get("operation") == operation:
            return _as_mapping(pending.get("request")).get(field)
        return None

    def _confirm_or_preview(
        self,
        operation: str,
        request: Mapping[str, Any],
        confirm_fingerprint: str | None,
        *,
        extras: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Реализовать двухфазный security-инвариант всех мутаций.

        SHA-256 связывает подтверждённого actor, класс аккаунта, окружение,
        название операции и точный канонический request. Сравнение подтверждения
        выполняется constant-time. В preview не раскрывается полный request.

        Параметры:
            operation: Каноническое имя мутации.
            request: Полный канонический запрос для fingerprint.
            confirm_fingerprint: Ранее показанный fingerprint или ``None``.
            extras: Безопасные дополнительные поля публичного preview.

        Возвращает:
            Preview-словарь без side effect либо ``None`` после успешного
            подтверждения.

        Исключения:
            ``ConfirmationError`` для неверного формата или несовпадающего
            fingerprint; ошибки identity/сериализации пробрасываются.

        Побочные эффекты:
            Preview сохраняется в ограниченном локальном pending-кеше; успешный
            confirm удаляет соответствующую запись. Внешняя мутация здесь не
            выполняется.

        Безопасность:
            Сравнение constant-time и привязка к actor/account/environment не
            позволяют подтвердить другой запрос; полный request не раскрывается.
        """

        fingerprint = self._fingerprint(operation, request)
        if confirm_fingerprint is None:
            if len(self._pending) >= 256:
                self._pending.pop(next(iter(self._pending)))
            self._pending[fingerprint] = {
                "operation": operation,
                "request": dict(request),
            }
            preview: dict[str, Any] = {
                "status": "preview_not_applied",
                "operation": operation,
                "fingerprint": fingerprint,
            }
            if extras:
                preview.update(extras)
            return preview
        if not isinstance(confirm_fingerprint, str) or not _HEX_SHA256_RE.fullmatch(
            confirm_fingerprint
        ):
            raise ConfirmationError("confirm_fingerprint имеет неверный формат")
        if not hmac.compare_digest(confirm_fingerprint, fingerprint):
            raise ConfirmationError("confirm_fingerprint не совпадает")
        self._pending.pop(confirm_fingerprint, None)
        return None

    def send_message(
        self,
        text: str,
        chat_id: str,
        thread_ts: int | str | None = None,
        payload_id: str | None = None,
        confirm_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Показать preview или отправить Plain-сообщение после подтверждения.

        Параметры:
            text: Непустой текст сообщения без NUL.
            chat_id: Id целевого чата; при ``thread_ts`` — родительского.
            thread_ts: Опциональный timestamp родителя для отправки в тред.
            payload_id: Канонический UUID идемпотентности или ``None``.
            confirm_fingerprint: Fingerprint точного preview или ``None``.

        Возвращает:
            Preview с fingerprint/payload_id либо результат с outcome, chat id,
            нормализованным MessageInfo и payload id.

        Исключения:
            ``ValueError`` для текста, координат или UUID;
            ``ConfirmationError`` для несовпавшего preview; ошибки identity и
            транспорта пробрасываются, кроме неопределённого push-исхода.

        Побочные эффекты:
            Preview не использует сеть мутации. После confirm выполняется ровно
            один Fanout push; неопределённый транспортный исход возвращается как
            ``outcome=unknown`` без повтора.

        Безопасность:
            Сгенерированный payload id восстанавливается из pending-preview, а
            точный текст, адресат и actor защищены fingerprint.
        """

        _validate_plain_text(text, field="text")
        _validate_chat_id(chat_id, allow_thread=False if thread_ts is not None else True)
        target = thread_chat_id(chat_id, thread_ts) if thread_ts is not None else chat_id
        if payload_id is None:
            payload_id = self._pending_field(
                confirm_fingerprint, "send_message", "payload_id"
            ) or str(uuid.uuid4())
        _validate_uuid(payload_id, "payload_id")
        request = {"chat_id": target, "text": text, "payload_id": payload_id}
        preview = self._confirm_or_preview(
            "send_message",
            request,
            confirm_fingerprint,
            extras={"payload_id": payload_id},
        )
        if preview is not None:
            return preview
        try:
            response = self._confirmed_push({
                "Plain": {
                    "ChatId": target,
                    "Text": {"MessageText": text},
                    "PayloadId": payload_id,
                }
            })
        except OperationUncertain:
            response = None
        outcome, message_info = _normalize_mutation_response(response)
        return {
            "outcome": outcome,
            "chat_id": target,
            "message_info": message_info,
            "payload_id": payload_id,
        }

    def _message_info(self, chat_id: str, timestamp: int | str) -> dict[str, Any]:
        """Прочитать и нормализовать одно сообщение по точным координатам.

        Параметры:
            chat_id: Валидный id чата или треда.
            timestamp: Неотрицательная целочисленная метка сообщения.

        Возвращает:
            Allowlisted представление сообщения из ``render_message``.

        Исключения:
            ``ValueError`` для координат; ``MessengerError`` если response не
            содержит сообщение; ошибки identity/Fanout пробрасываются.

        Побочные эффекты:
            Выполняет read-only Fanout-запрос.

        Безопасность:
            Неизвестные поля wire-ответа не попадают в результат.
        """
        ts = _validate_timestamp(timestamp)
        response = self.fanout(
            "message_info", {"ChatId": chat_id, "Timestamp": ts}
        )
        raw = _extract_message_envelope(response)
        if not isinstance(raw, Mapping):
            raise MessengerError("message_info не содержит сообщение")
        return render_message(raw)

    def edit_message(
        self,
        chat_id: str,
        timestamp: int | str,
        text: str,
        confirm_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Отредактировать только своё сообщение с исходным PayloadId.

        Перед preview и перед применением сообщение перечитывается. Автор
        сравнивается с подтверждёнными uid/guid; отсутствие author или PayloadId
        трактуется fail-closed.

        Параметры:
            chat_id: Id чата, содержащего исходное сообщение.
            timestamp: Timestamp редактируемого сообщения.
            text: Новый непустой текст.
            confirm_fingerprint: Fingerprint точного preview или ``None``.

        Возвращает:
            Preview либо результат с outcome и нормализованным MessageInfo.

        Исключения:
            ``PermissionDenied`` для чужого сообщения; ``MessengerError`` при
            отсутствии исходного PayloadId; ошибки валидации, подтверждения,
            identity и чтения пробрасываются.

        Побочные эффекты:
            Дважды может читать MessageInfo (preview и confirm); после confirm
            выполняет один push редактирования. Неопределённый push не повторяет.

        Безопасность:
            Автор проверяется заново на каждом вызове, а wire-мутация использует
            исходный PayloadId и точный fingerprint.
        """

        _validate_chat_id(chat_id)
        _validate_plain_text(text, field="text")
        ts = _validate_timestamp(timestamp)
        actor = self.identity()
        original = self._message_info(chat_id, ts)
        if str(original.get("author_guid")) not in {actor["guid"], actor["uid"]}:
            raise PermissionDenied("редактирование чужого сообщения запрещено")
        payload_id = original.get("payload_id")
        if not isinstance(payload_id, str) or not payload_id:
            raise MessengerError("исходное сообщение не содержит PayloadId")
        request = {
            "chat_id": chat_id,
            "timestamp": ts,
            "text": text,
            "payload_id": payload_id,
        }
        preview = self._confirm_or_preview(
            "edit_message", request, confirm_fingerprint
        )
        if preview is not None:
            return preview
        try:
            response = self._confirmed_push({
                "Plain": {
                    "ChatId": chat_id,
                    "Timestamp": ts,
                    "Text": {"MessageText": text},
                    "PayloadId": payload_id,
                }
            })
        except OperationUncertain:
            response = None
        outcome, message_info = _normalize_mutation_response(response)
        return {"outcome": outcome, "message_info": message_info}

    def set_reaction(
        self,
        chat_id: str,
        timestamp: int | str,
        reaction: str,
        operation: str = "add",
        confirm_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Добавить, удалить или заменить allowlisted реакцию.

        Параметры:
            chat_id: Id чата с сообщением.
            timestamp: Timestamp сообщения.
            reaction: Один из разрешённых символов ``✅``, ``👀`` или ``💔``.
            operation: ``add``, ``remove`` или ``replace``.
            confirm_fingerprint: Fingerprint точного preview или ``None``.

        Возвращает:
            Preview либо результат с outcome и координатами реакции.

        Исключения:
            ``ValueError`` для координат, реакции или операции;
            ``ConfirmationError`` и ошибки identity/Fanout пробрасываются.

        Побочные эффекты:
            После confirm выполняет один Fanout push; неопределённый исход
            классифицируется как ``unknown`` без автоматического повтора.

        Безопасность:
            Wire Type/Action получаются только из фиксированных allowlist, а
            адресат и действие связаны fingerprint.
        """

        _validate_chat_id(chat_id)
        ts = _validate_timestamp(timestamp)
        if reaction not in _REACTIONS:
            raise ValueError("поддерживаются только реакции ✅, 👀 и 💔")
        if operation not in _REACTION_ACTIONS:
            raise ValueError("operation должна быть add/remove/replace")
        request = {
            "chat_id": chat_id,
            "timestamp": ts,
            "reaction_type": _REACTIONS[reaction],
            "action": _REACTION_ACTIONS[operation],
        }
        preview = self._confirm_or_preview(
            "set_reaction", request, confirm_fingerprint
        )
        if preview is not None:
            return preview
        try:
            response = self._confirmed_push({
                "Reaction": {
                    "ChatId": chat_id,
                    "Timestamp": ts,
                    "Type": _REACTIONS[reaction],
                    "Action": _REACTION_ACTIONS[operation],
                }
            })
        except OperationUncertain:
            response = None
        outcome, _ = _normalize_mutation_response(response)
        return {
            "outcome": outcome,
            "chat_id": chat_id,
            "timestamp": ts,
            "reaction": reaction,
        }

    def send_document(
        self,
        chat_id: str,
        path: str | os.PathLike[str],
        story_id: str,
        payload_id: str | None = None,
        confirm_fingerprint: str | None = None,
        receipt_dir: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        """Показать preview или безопасно загрузить и отправить MiscFile.

        Локальный файл открывается с ``O_NOFOLLOW``, проверяется до/после чтения
        и ограничивается 50 MiB. Перед upload и перед push обновляется приватная
        receipt, позволяющая reconcile неизвестный исход без хранения OAuth.

        Параметры:
            chat_id: Id целевого чата.
            path: Абсолютный путь существующего обычного файла.
            story_id: Безопасный идентификатор истории доставки.
            payload_id: Канонический UUID либо ``None`` для генерации.
            confirm_fingerprint: Fingerprint точного preview или ``None``.
            receipt_dir: Абсолютный каталог receipt или ``None`` для приватного
                каталога по умолчанию.

        Возвращает:
            Preview с хешем/размером файла либо результат доставки с outcome,
            file id и путём receipt.

        Исключения:
            Ошибки валидации, безопасного чтения файла, identity и подтверждения
            пробрасываются; сетевые ошибки upload/push превращаются в
            ``outcome=unknown`` с receipt для сверки.

        Побочные эффекты:
            Confirm создаёт/обновляет receipt, загружает файл и выполняет push.

        Безопасность:
            Preview не загружает файл; symlink/TOCTOU и размер проверяются,
            OAuth и содержимое файла в receipt не записываются.
        """

        return self._send_file(
            kind="document",
            chat_id=chat_id,
            path=path,
            text=None,
            story_id=story_id,
            payload_id=payload_id,
            confirm_fingerprint=confirm_fingerprint,
            receipt_dir=receipt_dir,
        )

    def send_image_with_caption(
        self,
        chat_id: str,
        path: str | os.PathLike[str],
        text: str,
        story_id: str,
        payload_id: str | None = None,
        confirm_fingerprint: str | None = None,
        receipt_dir: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        """Показать preview или отправить проверенный PNG как Gallery.

        Параметры:
            chat_id: Id целевого чата.
            path: Абсолютный путь к статическому PNG.
            text: Непустая подпись изображения.
            story_id: Безопасный идентификатор истории доставки.
            payload_id: Канонический UUID либо ``None`` для генерации.
            confirm_fingerprint: Fingerprint точного preview или ``None``.
            receipt_dir: Абсолютный каталог receipt или ``None``.

        Возвращает:
            Preview с hash/size/payload id либо результат Gallery-доставки.

        Исключения:
            ``ValueError`` для текста или некорректного/анимированного PNG;
            остальные ошибки безопасной отправки файла пробрасываются.

        Побочные эффекты:
            Только confirm создаёт receipt, выполняет upload и Fanout push.

        Безопасность:
            PNG проверяется по signature, CRC, IHDR, размерам и запрету APNG;
            подпись и точный файл связаны fingerprint.
        """

        _validate_plain_text(text, field="text")
        return self._send_file(
            kind="image",
            chat_id=chat_id,
            path=path,
            text=text,
            story_id=story_id,
            payload_id=payload_id,
            confirm_fingerprint=confirm_fingerprint,
            receipt_dir=receipt_dir,
        )

    def _send_file(
        self,
        *,
        kind: str,
        chat_id: str,
        path: str | os.PathLike[str],
        text: str | None,
        story_id: str,
        payload_id: str | None,
        confirm_fingerprint: str | None,
        receipt_dir: str | os.PathLike[str] | None,
    ) -> dict[str, Any]:
        """Реализовать общий двухфазный pipeline документа или изображения.

        Параметры:
            kind: Внутренний тип ``document`` либо ``image``.
            chat_id: Id целевого чата.
            path: Абсолютный путь проверяемого файла.
            text: Подпись изображения или ``None`` для документа.
            story_id: Безопасный идентификатор источника.
            payload_id: Канонический UUID либо ``None``.
            confirm_fingerprint: Fingerprint preview либо ``None``.
            receipt_dir: Каталог приватной receipt либо ``None``.

        Возвращает:
            Preview без внешней мутации либо безопасный результат доставки.

        Исключения:
            Ошибки координат, id, файла, PNG, подтверждения и локальной receipt
            пробрасываются. Ошибки upload и неопределённый push фиксируются как
            ``outcome=unknown`` вместо слепого повтора.

        Побочные эффекты:
            Всегда читает локальный файл для привязки preview; confirm создаёт
            receipt, загружает media, делает один push и обновляет receipt.

        Безопасность:
            SHA-256, размер, путь, подпись и размеры PNG входят в fingerprint;
            wire-pоля строятся из фиксированного шаблона, содержимое файла и
            OAuth не сохраняются в receipt.
        """
        _validate_chat_id(chat_id)
        _validate_safe_id(story_id, "story_id")
        file_info = _read_secure_file(path)
        dimensions: tuple[int, int] | None = None
        if kind == "image":
            dimensions = _validate_png(file_info["data"])
        operation = "send_document" if kind == "document" else "send_image_with_caption"
        if payload_id is None:
            payload_id = self._pending_field(
                confirm_fingerprint, operation, "payload_id"
            ) or str(uuid.uuid4())
        _validate_uuid(payload_id, "payload_id")
        request = {
            "chat_id": chat_id,
            "path": file_info["path"],
            "sha256": file_info["sha256"],
            "size": file_info["size"],
            "story_id": story_id,
            "payload_id": payload_id,
            "text": text,
        }
        if dimensions is not None:
            request["width"], request["height"] = dimensions
        preview = self._confirm_or_preview(
            operation,
            request,
            confirm_fingerprint,
            extras={
                "payload_id": payload_id,
                "file_sha256": file_info["sha256"],
                "file_size": file_info["size"],
            },
        )
        if preview is not None:
            return preview
        receipt = self._write_receipt(request, receipt_dir, "before_upload")
        filename = Path(file_info["path"]).name
        try:
            uploaded = self._upload_media(
                file_info["data"], filename, kind, chat_id, payload_id
            )
        except (OperationUncertain, MessengerError):
            self._update_receipt(receipt, request, "unknown")
            return _media_result(
                "unknown", kind, request, None, receipt
            )
        self._update_receipt(receipt, request, "before_push")
        file_info_wire = {
            "Id2": uploaded["file_id"],
            "Name": filename,
            "Size": file_info["size"],
            "Source": 0,
        }
        plain: dict[str, Any] = {"ChatId": chat_id, "PayloadId": payload_id}
        if kind == "document":
            plain["MiscFile"] = {"FileInfo": file_info_wire}
        else:
            assert dimensions is not None
            plain["Gallery"] = {
                "Text": text,
                "Items": [
                    {
                        "Image": {
                            "Width": dimensions[0],
                            "Height": dimensions[1],
                            "Animated": False,
                            "FileInfo": file_info_wire,
                        }
                    }
                ],
            }
        try:
            response = self._confirmed_push({"Plain": plain})
        except OperationUncertain:
            response = None
        outcome, _ = _normalize_mutation_response(response)
        self._update_receipt(receipt, request, outcome)
        return _media_result(outcome, kind, request, uploaded["file_id"], receipt)

    def _receipt_directory(self, value: str | os.PathLike[str] | None) -> Path:
        """Подготовить приватный каталог receipt без symlink-компонентов.

        Параметры:
            value: Абсолютный путь каталога либо ``None`` для temp-каталога.

        Возвращает:
            Проверенный ``Path`` существующей директории с mode ``0700``.

        Исключения:
            ``ValueError`` для небезопасного пути; ``MessengerError`` если
            итоговый объект не обычная директория; файловые ошибки пробрасываются.

        Побочные эффекты:
            Может создать дерево каталогов и выставляет mode ``0700``.

        Безопасность:
            Symlink-компоненты проверяются до и после mkdir, чтобы receipt не
            была перенаправлена в другой каталог.
        """
        if value is None:
            value = Path(tempfile.gettempdir()) / "messenger-user-client-receipts"
        directory = _absolute_path(value, existing=False)
        _reject_symlink_components(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _reject_symlink_components(directory)
        if directory.is_symlink() or not directory.is_dir():
            raise MessengerError("receipt_dir должен быть обычной директорией")
        os.chmod(directory, 0o700)
        return directory

    def _write_receipt(
        self,
        request: Mapping[str, Any],
        receipt_dir: str | os.PathLike[str] | None,
        stage: str,
    ) -> Path:
        """Создать приватную receipt без токена и произвольных wire-полей.

        Параметры:
            request: Канонический безопасный запрос доставки.
            receipt_dir: Каталог receipt либо ``None``.
            stage: Текущая стадия локального журнала.

        Возвращает:
            Путь нового уникального JSON-файла receipt.

        Исключения:
            Ошибки пути, эксклюзивного открытия, записи и fsync пробрасываются.

        Побочные эффекты:
            Создаёт один файл через ``O_EXCL``/``O_NOFOLLOW``, записывает JSON,
            синхронизирует его и выставляет mode ``0600``.

        Безопасность:
            Receipt содержит только allowlisted request и stage; OAuth, URL
            ответа и сырые wire-поля в неё не передаются.
        """

        directory = self._receipt_directory(receipt_dir)
        target = directory / f"{uuid.uuid4()}.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target, flags, 0o600)
        try:
            os.write(fd, _compact_json({"stage": stage, "request": dict(request)}))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(target, 0o600)
        return target

    def _update_receipt(
        self, target: Path, request: Mapping[str, Any], stage: str
    ) -> None:
        """Атомарно по дескриптору обновить стадию существующей receipt.

        Параметры:
            target: Ранее безопасно созданный файл receipt.
            request: Канонический запрос доставки.
            stage: Новая стадия или outcome.

        Исключения:
            Ошибки открытия, записи и fsync пробрасываются.

        Побочные эффекты:
            Обрезает и перезаписывает файл, затем синхронизирует его на диск.

        Безопасность:
            ``O_NOFOLLOW`` запрещает подмену файла symlink; сериализуются только
            stage и уже очищенный request, без OAuth и удалённых ответов.
        """
        flags = os.O_WRONLY | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target, flags)
        try:
            os.write(fd, _compact_json({"stage": stage, "request": dict(request)}))
            os.fsync(fd)
        finally:
            os.close(fd)

    def _upload_media(
        self,
        data: bytes,
        filename: str,
        kind: str,
        chat_id: str,
        payload_id: str,
    ) -> dict[str, str]:
        """Загрузить исходные bytes на фиксированный media_upload URL.

        Каждый path segment quote-ится отдельно; OAuth остаётся только в
        заголовке, redirect запрещён. Multipart не используется: тело запроса
        полностью совпадает с bytes проверенного локального файла.

        Параметры:
            data: Проверенные байты локального файла.
            filename: Базовое имя файла для URL и имени вложения.
            kind: ``image`` или тип документа.
            chat_id: Валидный id целевого чата.
            payload_id: Канонический UUID доставки.

        Возвращает:
            Словарь с проверенным безопасным ``file_id``.

        Исключения:
            ``OperationUncertain`` для HTTP/транспортного сбоя upload;
            ``MessengerError`` для слишком большого, не-JSON или неполного
            ответа; ошибки построения запроса пробрасываются.

        Побочные эффекты:
            Выполняет один HTTPS POST с raw body файла.

        Безопасность:
            Сегменты URL percent-encode-ятся, redirect запрещён, OAuth остаётся
            заголовком фиксированного host, а response file id проверяется regex.
        """

        chat_path = "/".join(
            urllib.parse.quote(part, safe="") for part in chat_id.split("/")
        )
        url = (
            _MEDIA_UPLOAD_BASE
            + chat_path
            + "/"
            + urllib.parse.quote(payload_id, safe="")
            + "/"
            + urllib.parse.quote(filename, safe="")
        )
        # Документ должен сохранять исходные bytes: image/* на upload включает
        # серверную оптимизацию, даже если последующий push использует MiscFile.
        mime = "image/png" if kind == "image" else "application/octet-stream"
        headers = self._oauth_headers()
        headers["Content-Type"] = mime
        request = urllib.request.Request(
            url, data=data, headers=headers, method="POST"
        )
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_WIRE_JSON + 1)
        except urllib.error.HTTPError as exc:
            raise OperationUncertain(f"upload HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OperationUncertain(
                f"upload outcome unknown: {type(exc).__name__}"
            ) from None
        if len(raw) > _MAX_WIRE_JSON:
            raise MessengerError("upload response слишком велик")
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MessengerError("upload вернул некорректный JSON") from None
        data_map = _as_mapping(decoded)
        nested = _as_mapping(_first(data_map, "data", "Data"))
        if nested:
            data_map = nested
        file_id = _safe_scalar(_first(data_map, "file_id", "FileId", "id", "Id"))
        try:
            _validate_media_id(file_id)
        except ValueError:
            raise MessengerError("upload response не содержит безопасный file_id")
        return {"file_id": file_id}

    def download_media(
        self,
        file_id: str,
        target: str | os.PathLike[str],
        max_bytes: int,
        confirm_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Показать preview или скачать media в новый локальный файл.

        Файл создаётся через ``O_EXCL|O_NOFOLLOW``. Partial удаляется при любой
        ошибке. OAuth используется только на начальном фиксированном host;
        разрешённые ручные redirect остаются HTTPS внутри доменов Yandex и уже
        не содержат заголовок Authorization.

        Параметры:
            file_id: Безопасный id удалённого media-файла.
            target: Абсолютный путь нового локального файла.
            max_bytes: Максимальный допустимый размер от 1 до 50 MiB.
            confirm_fingerprint: Fingerprint точного preview или ``None``.

        Возвращает:
            Preview либо результат с путём, размером и SHA-256 скачанного файла.

        Исключения:
            ``ValueError`` для id, лимита или пути; ``FileExistsError`` если
            target уже существует; ``MessengerError`` для redirect, HTTP или
            превышения размера; файловые ошибки пробрасываются.

        Побочные эффекты:
            После confirm создаёт target mode ``0600``, выполняет до пяти
            ручных redirect и записывает/``fsync``-ит тело. При ошибке удаляет
            созданный partial.

        Безопасность:
            Target открывается эксклюзивно без symlink; OAuth передаётся только
            первому фиксированному host и снимается со всех redirect, которые
            дополнительно ограничены HTTPS и Yandex host allowlist.
        """

        _validate_media_id(file_id)
        if not isinstance(max_bytes, int) or not 1 <= max_bytes <= _MAX_UPLOAD_BYTES:
            raise ValueError("max_bytes вне допустимого диапазона")
        target_path = _absolute_path(target, existing=False)
        _validate_parent_components(target_path)
        request_data = {
            "file_id": file_id,
            "target": str(target_path),
            "max_bytes": max_bytes,
        }
        preview = self._confirm_or_preview(
            "download_media", request_data, confirm_fingerprint
        )
        if preview is not None:
            return preview
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target_path, flags, 0o600)
        total = 0
        digest = hashlib.sha256()
        try:
            url = _MEDIA_DOWNLOAD_BASE + urllib.parse.quote(file_id, safe="/._-")
            headers = self._oauth_headers(json_accept=False)
            for redirect_count in range(6):
                request = urllib.request.Request(url, headers=headers, method="GET")
                try:
                    response = _NO_REDIRECT_OPENER.open(request, timeout=self.timeout)
                except urllib.error.HTTPError as exc:
                    if exc.code in {301, 302, 303, 307, 308}:
                        location = exc.headers.get("Location")
                        if not location or redirect_count == 5:
                            raise MessengerError("некорректный media redirect") from None
                        url = urllib.parse.urljoin(url, location)
                        parsed = urllib.parse.urlsplit(url)
                        if parsed.scheme != "https" or not _is_yandex_host(parsed.hostname):
                            raise MessengerError("media redirect вышел за HTTPS allowlist")
                        headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                        continue
                    raise MessengerError(f"media HTTP {exc.code}") from None
                with response:
                    while True:
                        chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise MessengerError("media превышает max_bytes")
                        os.write(fd, chunk)
                        digest.update(chunk)
                break
            else:
                raise MessengerError("слишком много media redirect")
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            fd = -1
            try:
                os.unlink(target_path)
            except FileNotFoundError:
                pass
            raise
        finally:
            if fd >= 0:
                os.close(fd)
        return {
            "status": "downloaded",
            "path": str(target_path),
            "size": total,
            "sha256": digest.hexdigest(),
        }


def _validate_uuid(value: str, field: str) -> None:
    """Проверить каноническое нижнерегистровое UUID-поле.

    Параметры:
        value: Проверяемая строка UUID.
        field: Безопасное имя поля для диагностики.

    Исключения:
        ``ValueError`` если значение не UUID или отличается от канонической
        lowercase-записи.

    Безопасность:
        Значение не включается в сообщение ошибки, что не раскрывает
        идентификатор в журнале исключений.
    """
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{field} должен быть UUID") from None
    if str(parsed) != value.lower():
        raise ValueError(f"{field} должен быть каноническим UUID")


def _validate_safe_id(value: str, field: str) -> None:
    """Проверить короткий идентификатор по фиксированному ASCII allowlist.

    Параметры:
        value: Проверяемое значение длиной до 256 символов.
        field: Безопасное имя поля для диагностики.

    Исключения:
        ``ValueError`` для неверного типа или символов вне allowlist.

    Безопасность:
        Regex исключает разделители пути, пробелы и управляющие символы.
    """
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"некорректный {field}")


def _timestamp_sort_key(value: Any) -> tuple[int, int | str]:
    """Построить стабильный ключ сортировки timestamp.

    Параметры:
        value: Числовая или произвольная скалярная метка времени.

    Возвращает:
        Кортеж, сортирующий digit-only значения численно раньше остальных,
        которые сравниваются строково.
    """

    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def _extract_message_envelope(value: Any) -> Mapping[str, Any] | None:
    """Найти известный message envelope без копирования иных полей.

    Параметры:
        value: Недоверенная вложенная структура ответа мутации.

    Возвращает:
        Mapping с Message/ServerMessage/MessageInfo либо ``None``.

    Безопасность:
        Поиск проходит только по фиксированным ключам и не возвращает соседние
        поля response, которые могут содержать URL, заголовки или расширения.
    """

    if not isinstance(value, Mapping):
        return None
    if "ServerMessage" in value and isinstance(value["ServerMessage"], Mapping):
        return {"ServerMessage": value["ServerMessage"]}
    for key in ("Message", "MessageInfo", "Result", "Status", "Data"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            found = _extract_message_envelope(nested)
            if found is not None:
                return found
    for key in ("FullyCommitted", "Duplicate"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            found = _extract_message_envelope(nested)
            if found is not None:
                return found
    if "ClientMessage" in value or "ServerMessageInfo" in value:
        return value
    return None


def _normalize_mutation_response(
    response: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any] | None]:
    """Классифицировать wire-ответ мутации в compatibility outcome.

    ``FullyCommitted``, ``Duplicate``, числовые результаты 1/2 и успешный
    MessageInfo считаются confirmed. Наличие ``Details`` означает rejected;
    транспортная или семантическая неоднозначность возвращает unknown.

    Параметры:
        response: Декодированный ответ мутации либо ``None`` при неизвестном
            транспортном исходе.

    Возвращает:
        Пару ``(outcome, message_info)``; outcome равен ``confirmed``,
        ``rejected`` или ``unknown``, а message_info очищен или отсутствует.

    Безопасность:
        Классификация использует только известные маркеры ограниченной глубины,
        а сообщение проходит через allowlisted ``render_message``.
    """

    if response is None:
        return "unknown", None
    markers = _mutation_markers(response)
    if "details" in markers:
        return "rejected", None
    envelope = _extract_message_envelope(response)
    message_info = render_message(envelope) if envelope is not None else None
    if markers & {"fullycommitted", "duplicate", "numeric_success"}:
        return "confirmed", message_info
    if message_info is not None:
        return "confirmed", message_info
    return "unknown", None


def _mutation_markers(value: Any, depth: int = 0) -> set[str]:
    """Собрать известные outcome-маркеры из ограниченной глубины JSON.

    Параметры:
        value: Недоверенный фрагмент ответа мутации.
        depth: Текущая глубина рекурсивного обхода.

    Возвращает:
        Множество нормализованных маркеров ``fullycommitted``, ``duplicate``,
        ``details`` и ``numeric_success``.

    Безопасность:
        Обход обрывается после глубины 5, не копирует значения и игнорирует
        неизвестные ключи, ограничивая стоимость обработки чужого JSON.
    """

    if depth > 5:
        return set()
    if isinstance(value, Mapping):
        result: set[str] = set()
        for key, nested in value.items():
            lowered = str(key).lower()
            if lowered in {"fullycommitted", "duplicate"}:
                result.add(lowered)
            elif lowered == "details" and nested:
                result.add("details")
            if lowered in {"result", "status", "commitstatus"}:
                if isinstance(nested, int) and not isinstance(nested, bool) \
                        and nested in {1, 2}:
                    result.add("numeric_success")
                elif isinstance(nested, str) \
                        and nested.lower() in {"fullycommitted", "duplicate"}:
                    result.add(nested.lower())
            result.update(_mutation_markers(nested, depth + 1))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for nested in value:
            result.update(_mutation_markers(nested, depth + 1))
        return result
    return set()


def _media_result(
    outcome: str,
    kind: str,
    request: Mapping[str, Any],
    file_id: str | None,
    receipt: Path,
) -> dict[str, Any]:
    """Собрать безопасный результат доставки без содержимого файла.

    Параметры:
        outcome: Нормализованный исход мутации.
        kind: Тип доставки ``document`` или ``image``.
        request: Канонический request с координатами и метаданными файла.
        file_id: Подтверждённый server file id либо ``None``.
        receipt: Путь локальной receipt для последующей сверки.

    Возвращает:
        Словарь с outcome, координатами, file id и проверочными метаданными.

    Безопасность:
        Байты файла, OAuth, удалённые URL и произвольный wire-response в
        результат не включаются.
    """

    return {
        "outcome": outcome,
        "chat_id": request["chat_id"],
        "payload_id": request["payload_id"],
        "story_id": request["story_id"],
        "file_id": file_id,
        "delivery": {"kind": kind, "file_id": file_id},
        "verification": {
            "sha256": request["sha256"],
            "size": request["size"],
            "receipt_path": str(receipt),
        },
    }


def _validate_fanout_payload(path: str, payload: Mapping[str, Any]) -> None:
    """Проверить wire-поля Fanout по фиксированному allowlist проекта.

    Параметры:
        path: Уже проверенный Fanout path.
        payload: Wire-payload чтения или push.

    Исключения:
        ``ValueError`` для неизвестных/пропущенных полей, неверного ChatId,
        envelope, UserIp или content type.

    Безопасность:
        Read-only paths и push имеют отдельные точные схемы; push допускает
        ровно один блок Plain/Reaction и фиксированный loopback UserIp.
    """

    allowed = {
        "history": {"ChatId", "Limit", "MinTimestamp", "MaxTimestamp"},
        "message_info": {"ChatId", "Timestamp"},
        "push": {"ClientMessage", "UserIp"},
    }[path]
    if not payload or any(key not in allowed for key in payload):
        raise ValueError("fanout payload содержит запрещённые поля")
    if path in {"history", "message_info"}:
        chat_id = payload.get("ChatId")
        if not isinstance(chat_id, str):
            raise ValueError("fanout payload не содержит ChatId")
        _validate_chat_id(chat_id)
    else:
        if set(payload) != {"ClientMessage", "UserIp"} or payload["UserIp"] != "::1":
            raise ValueError("push envelope не соответствует compatibility contract")
        client_message = payload["ClientMessage"]
        if not isinstance(client_message, Mapping) or len(client_message) != 1:
            raise ValueError("ClientMessage должен содержать один content type")
        if next(iter(client_message)) not in {"Plain", "Reaction"}:
            raise ValueError("неизвестный ClientMessage content type")
        if not isinstance(next(iter(client_message.values())), Mapping):
            raise ValueError("ClientMessage content должен быть mapping")


def _validate_media_id(value: Any) -> None:
    """Проверить namespace media ID из безопасных сегментов без traversal.

    Параметры:
        value: Непустая строка ID длиной до 4096 символов; слеш разделяет
            namespace-сегменты, состоящие из ASCII букв, цифр, точки, _ и -.

    Исключения:
        ValueError при неверном типе, размере, пустом сегменте, URL, query,
        процентном кодировании либо сегменте ``.`` или ``..``.

    Инварианты безопасности:
        ID используется только на фиксированном media host; namespace-разделители
        сохраняются в URL. Прямые пути, credentials и URL не принимаются.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError("некорректный media file_id")
    if any(
        part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]+", part)
        for part in value.split("/")
    ):
        raise ValueError("некорректный media file_id")


def _validate_timestamp(value: int | str) -> int:
    """Нормализовать timestamp в bounded integer для Fanout wire.

    Параметры:
        value: Целое число или строка timestamp.

    Возвращает:
        Неотрицательное целое число длиной не более 20 десятичных цифр.

    Исключения:
        ``ValueError`` для отрицательных, нечисловых или слишком длинных меток.
    """
    result = str(value)
    if not result.isdigit() or len(result) > 20:
        raise ValueError("некорректный timestamp")
    return int(result)


def _is_yandex_host(host: str | None) -> bool:
    """Проверить принадлежность hostname разрешённым Yandex DNS-зонам.

    Параметры:
        host: Hostname из разобранного URL или ``None``.

    Возвращает:
        ``True`` только для поддоменов ``.yandex.ru`` или ``.yandex.net``.

    Безопасность:
        Проверка нормализует регистр/конечную точку и использует границу зоны,
        поэтому lookalike вроде ``yandex.net.evil`` не принимается.
    """
    if not host:
        return False
    lowered = host.rstrip(".").lower()
    return lowered.endswith(".yandex.ru") or lowered.endswith(".yandex.net")


def _absolute_path(
    value: str | os.PathLike[str], *, existing: bool
) -> Path:
    """Преобразовать значение в лексически безопасный абсолютный Path.

    Параметры:
        value: Строка или path-like значение.
        existing: Требовать существования конечного объекта.

    Возвращает:
        Исходный абсолютный ``Path`` без разрешения symlink.

    Исключения:
        ``ValueError`` для относительного пути или ``..``;
        ``FileNotFoundError`` при обязательном отсутствии объекта.

    Безопасность:
        Функция не нормализует путь через ``resolve`` и тем самым не следует по
        symlink; дальнейшие проверки компонентов выполняются отдельно.
    """
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("разрешён только абсолютный путь без '..'")
    if existing and not path.exists():
        raise FileNotFoundError(path)
    return path


def _validate_parent_components(path: Path) -> None:
    """Запретить symlink во всех родительских компонентах локального пути.

    Параметры:
        path: Абсолютный путь, родители которого должны существовать.

    Исключения:
        ``ValueError`` если родитель отсутствует, является symlink или не
        директорией; ошибки ``lstat`` пробрасываются.

    Безопасность:
        Компоненты проверяются через ``lstat`` без следования по symlink.
    """

    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            raise ValueError("родительская директория не существует") from None
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlink в локальном пути запрещён")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("компонент пути не является директорией")


def _reject_symlink_components(path: Path) -> None:
    """Проверить все существующие компоненты, не следуя по symlink.

    Параметры:
        path: Абсолютный путь, часть компонентов которого может отсутствовать.

    Исключения:
        ``ValueError`` для symlink или промежуточного не-каталога; остальные
        ошибки ``lstat`` пробрасываются.

    Безопасность:
        Отсутствующие компоненты допускаются для последующего mkdir, но любой
        уже существующий symlink отклоняется.
    """

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlink в локальном пути запрещён")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("компонент пути не является директорией")


def _read_secure_file(path_value: str | os.PathLike[str]) -> dict[str, Any]:
    """Прочитать regular file без symlink и обнаружить TOCTOU-подмену.

    Каждый компонент проверяется через lstat, сам файл открывается с
    ``O_NOFOLLOW``. До чтения и после него сравниваются device, inode, размер и
    mtime; изменение любого значения отклоняет файл.

    Параметры:
        path_value: Абсолютный путь существующего локального файла.

    Возвращает:
        Словарь с путём, размером, SHA-256 и прочитанными байтами.

    Исключения:
        ``ValueError`` для пути, symlink, типа или размера свыше 50 MiB;
        ``MessengerError`` при TOCTOU-подмене или изменившемся размере;
        файловые ошибки пробрасываются.

    Побочные эффекты:
        Открывает и полностью читает файл без его изменения.

    Безопасность:
        ``lstat``/``O_NOFOLLOW`` и сравнение metadata до/после чтения защищают
        от symlink и подмены; размер ограничен до накопления в памяти.
    """

    path = _absolute_path(path_value, existing=True)
    _validate_parent_components(path)
    lexical = os.lstat(path)
    if stat.S_ISLNK(lexical.st_mode) or not stat.S_ISREG(lexical.st_mode):
        raise ValueError("ожидался обычный файл, не symlink")
    if lexical.st_size > _MAX_UPLOAD_BYTES:
        raise ValueError("файл превышает 50 MiB")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_UPLOAD_BYTES:
            raise ValueError("некорректный тип или размер файла")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, _MAX_UPLOAD_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_UPLOAD_BYTES:
                raise ValueError("файл превышает 50 MiB")
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or (lexical.st_dev, lexical.st_ino) != (
        before.st_dev,
        before.st_ino,
    ):
        raise MessengerError("файл изменился во время чтения")
    data = b"".join(chunks)
    if len(data) != before.st_size:
        raise MessengerError("размер файла изменился во время чтения")
    return {
        "path": str(path),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": data,
    }


def _validate_png(data: bytes) -> tuple[int, int]:
    """Проверить PNG signature, IHDR, CRC, размеры и отсутствие APNG.

    Параметры:
        data: Полное содержимое предполагаемого PNG.

    Возвращает:
        Пару ``(width, height)`` подтверждённого статического изображения.

    Исключения:
        ``ValueError`` для неверной сигнатуры, chunk length/CRC, IHDR,
        цветового формата, чрезмерных размеров, APNG или хвоста после IEND.

    Безопасность:
        Размеры и число пикселей ограничены; каждый chunk проверяется в границах
        входа и по CRC, а анимированные PNG отклоняются fail-closed.
    """

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("изображение не является PNG")
    offset = 8
    seen_ihdr = False
    seen_iend = False
    width = height = 0
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        if length > len(data) - offset - 12:
            raise ValueError("повреждённый PNG chunk")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if not hmac.compare_digest(
            struct.pack(">I", expected_crc), struct.pack(">I", actual_crc)
        ):
            raise ValueError("PNG CRC mismatch")
        if not seen_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("IHDR должен быть первым PNG chunk")
            width, height = struct.unpack_from(">II", chunk_data)
            if not (1 <= width <= 32768 and 1 <= height <= 32768):
                raise ValueError("неразумные размеры PNG")
            if width * height > 100_000_000:
                raise ValueError("PNG содержит слишком много пикселей")
            bit_depth, color_type, compression, filtering, interlace = chunk_data[8:]
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if bit_depth not in valid_depths.get(color_type, set()):
                raise ValueError("некорректные bit depth/color type PNG")
            if compression != 0 or filtering != 0 or interlace not in {0, 1}:
                raise ValueError("некорректные параметры IHDR")
            seen_ihdr = True
        if chunk_type in {b"acTL", b"fcTL", b"fdAT"}:
            raise ValueError("APNG не поддерживается")
        if chunk_type == b"IEND":
            if length != 0:
                raise ValueError("некорректный IEND")
            seen_iend = True
            offset += 12
            break
        offset += 12 + length
    if not seen_ihdr or not seen_iend or offset != len(data):
        raise ValueError("PNG не содержит корректный IEND")
    return width, height


__all__ = [
    "AuthenticationError",
    "ConfirmationError",
    "MessengerError",
    "MessengerUserClient",
    "OperationUncertain",
    "PermissionDenied",
    "render_message",
    "thread_chat_id",
]
