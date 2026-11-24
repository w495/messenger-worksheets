"""Small public Yandex Messenger Bot API client used by the gateway."""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import stat
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request as HTTPRequest, build_opener
import uuid


DEFAULT_BASE_URL = "https://botapi.messenger.yandex.net/bot/v1/"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_MEDIA_BYTES = 50 * 1024 * 1024


class BotApiError(RuntimeError):
    """A safe, token-free Bot API failure category."""


class NoRedirect(HTTPRedirectHandler):
    def http_error_301(self, req, fp, code, msg, headers):
        """Запретить HTTP-перенаправление Bot API и заменить его безопасной категорией ошибки.

        Параметры: req — исходный HTTP request; fp — поток ответа; code — HTTP status; msg — описание status; headers — HTTP headers.
        Результат: Функция не возвращается: перенаправление отклоняется.
        Исключения: BotApiError с категорией unexpected_redirect.
        Побочные эффекты: Прерывает обработку HTTP-ответа до перехода по Location.
        Безопасность: Ни токен, ни новый URL не передаются перенаправленному узлу."""
        raise BotApiError("unexpected_redirect")

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def _positive_int(value: object, name: str) -> int:
    """Проверить, что значение является строго положительным целым числом.

    Параметры: value — проверяемое значение; name — имя поля.
    Результат: Исходное значение как int после проверки положительности.
    Исключения: ValueError для bool, не-int или значения <= 0.
    Побочные эффекты: Побочных эффектов нет.
    Безопасность: Не выполняет неявное приведение внешнего значения к integer."""
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class BotApiClient:
    def __init__(
        self, token: str, *, timeout: int = 15, auth_scheme: str = "OAuth",
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        """Создать клиент Bot API с проверенным токеном, URL и схемой авторизации.

        Параметры: token — Bot API token; timeout — timeout в секундах; auth_scheme — схема Authorization; base_url — базовый URL API.
        Результат: Инициализированный клиент с нормализованным base_url и opener без redirect.
        Исключения: ValueError для пустого/пробельного token, неизвестной auth_scheme или недоверенного base_url.
        Побочные эффекты: Сохраняет token в объекте и создаёт urllib opener.
        Безопасность: Разрешает только HTTPS Yandex host без userinfo/query/fragment и схемы OAuth/OAuthTeam."""
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise ValueError("invalid Bot API token")
        self._token = token
        if auth_scheme not in {"OAuth", "OAuthTeam"}:
            raise ValueError("unsupported Bot API auth scheme")
        self.auth_scheme = auth_scheme
        endpoint = urlsplit(base_url)
        if (endpoint.scheme != "https" or not endpoint.hostname
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment
                or not endpoint.hostname.endswith((".yandex.net", ".yandex.ru"))):
            raise ValueError("invalid Bot API base URL")
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self._opener = build_opener(NoRedirect())

    def _open(
        self, path: str, *, method: str = "GET", params: dict[str, object] | None = None,
        data: bytes | None = None, content_type: str | None = None,
    ):
        """Открыть один HTTPS-запрос к разрешённому относительному пути Bot API.

        Параметры: path — относительный API path или локальный файл; method — HTTP-метод; params — query parameters; data — тело запроса; content_type — Content-Type.
        Результат: Открытый HTTPResponse доверенного Bot API; закрывает его вызывающий код.
        Исключения: ValueError для абсолютного/пустого/path-traversal path; BotApiError http_<status> или connection_failed.
        Побочные эффекты: Выполняет один HTTPS-запрос с Authorization и настроенным timeout.
        Безопасность: Path присоединяется только к проверенному base_url; redirect запрещён, token остаётся в заголовке."""
        if not path or path.startswith("/") or ".." in path:
            raise ValueError("invalid Bot API path")
        url = self.base_url + path.strip("/")
        if params:
            url += "?" + urlencode(params)
        headers = {"Authorization": self.auth_scheme + " " + self._token}
        if content_type:
            headers["Content-Type"] = content_type
        request = HTTPRequest(url, data=data, headers=headers, method=method)
        try:
            return self._opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            raise BotApiError(f"http_{error.code}") from None
        except URLError:
            raise BotApiError("connection_failed") from None

    def _json(
        self, path: str, *, method: str = "GET", params: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        """Выполнить запрос Bot API и разобрать ограниченный успешный JSON-объект.

        Параметры: path — относительный API path или локальный файл; method — HTTP-метод; params — query parameters; body — JSON body.
        Результат: Словарь успешного ответа Bot API с ok=true.
        Исключения: BotApiError от транспорта, при ответе >16 MiB, невалидном JSON или ok!=true.
        Побочные эффекты: Выполняет HTTPS-запрос и полностью читает ограниченное тело ответа.
        Безопасность: Ограничивает размер до JSON decode и не возвращает HTTP headers/token."""
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        with self._open(
            path, method=method, params=params, data=data,
            content_type="application/json" if body is not None else None,
        ) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise BotApiError("response_too_large")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BotApiError("invalid_json") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise BotApiError("api_rejected")
        return result

    def identity(self) -> dict[str, Any]:
        """Получить и проверить идентичность текущего бота.

        Параметры: нет.
        Результат: Проверенный словарь identity с строковыми id и login.
        Исключения: BotApiError при транспортном/API-сбое или отсутствии строковых id/login.
        Побочные эффекты: Вызывает self/get по HTTPS.
        Безопасность: Не принимает identity из конфигурации: использует ответ аутентифицированного Bot API."""
        result = self._json("self/get")
        if not isinstance(result.get("id"), str) or not isinstance(result.get("login"), str):
            raise BotApiError("invalid_identity")
        return result

    def chats(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """Получить проверенный список доступных боту чатов.

        Параметры: limit — лимит элементов.
        Результат: Список словарей чатов из chats либо корпоративного поля data.
        Исключения: BotApiError при сбое API или если контейнер/элементы имеют неверный тип.
        Побочные эффекты: Вызывает chats/get по HTTPS.
        Безопасность: Возвращает только структурно проверенный список, не интерпретируя вложенные поля как команды."""
        result = self._json("chats/get", params={"limit": limit})
        chats = result.get("chats", result.get("data"))
        if not isinstance(chats, list) or any(not isinstance(chat, dict) for chat in chats):
            raise BotApiError("invalid_chats")
        return chats

    def get_updates(self, offset: int, *, limit: int = 100) -> list[dict[str, Any]]:
        """Получить проверенную порцию обновлений начиная с заданного offset.

        Параметры: offset — cursor updates; limit — лимит элементов.
        Результат: Список словарей updates из updates либо корпоративного поля data.
        Исключения: ValueError для отрицательного/non-int offset; BotApiError при сбое API или неверной структуре списка.
        Побочные эффекты: Вызывает messages/getUpdates с offset и limit.
        Безопасность: Cursor не приводится неявно; тело ответа ограничено общим JSON-лимитом."""
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        result = self._json("messages/getUpdates", params={"limit": limit, "offset": offset})
        updates = result.get("updates", result.get("data"))
        if not isinstance(updates, list) or any(not isinstance(item, dict) for item in updates):
            raise BotApiError("invalid_updates")
        return updates

    def send_text(
        self, chat_id: str, text: str, *, payload_id: str,
        thread_id: int | None = None, message_id: int | None = None,
    ) -> int:
        """Отправить или отредактировать текстовое сообщение и вернуть его идентификатор.

        Параметры: chat_id — ID чата; text — текст сообщения; payload_id — UUID идемпотентности; thread_id — ID треда или None; message_id — ID сообщения или None.
        Результат: Положительный message_id созданного или изменённого сообщения.
        Исключения: ValueError для текста вне 1..6000, невалидного payload UUID, thread_id/message_id; BotApiError при отказе API.
        Побочные эффекты: Отправляет POST messages/sendText.
        Безопасность: Payload канонизируется как UUID, числовые ID проверяются строго положительными."""
        if not isinstance(text, str) or not 0 < len(text) <= 6000:
            raise ValueError("text must contain 1..6000 characters")
        body: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "payload_id": str(uuid.UUID(payload_id)),
        }
        if thread_id is not None:
            body["thread_id"] = _positive_int(thread_id, "thread_id")
        if message_id is not None:
            body["message_id"] = _positive_int(message_id, "message_id")
        result = self._json("messages/sendText", method="POST", body=body)
        return _positive_int(result.get("message_id"), "message_id")

    def send_reaction(
        self, chat_id: str, message_id: int, *, reaction_name: str | None,
        thread_id: int | None = None,
    ) -> None:
        """Установить либо удалить стандартную реакцию у сообщения.

        Параметры: chat_id — ID чата; message_id — ID сообщения или None; reaction_name — имя реакции или None; thread_id — ID треда или None.
        Результат: Ничего; успешный возврат означает ok=true от API.
        Исключения: ValueError для неположительных message_id/thread_id; BotApiError при транспортном или API-отказе.
        Побочные эффекты: Отправляет POST messages/sendReaction.
        Безопасность: Удаление кодируется reaction=null; целевые ID проходят строгую integer-проверку."""
        body: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": _positive_int(message_id, "message_id"),
            "reaction": (
                None if reaction_name is None
                else {"type": "default_reaction", "name": reaction_name}
            ),
        }
        if thread_id is not None:
            body["thread_id"] = _positive_int(thread_id, "thread_id")
        self._json("messages/sendReaction", method="POST", body=body)

    @staticmethod
    def _multipart(
        fields: dict[str, object], *, file_field: str, path: Path,
    ) -> tuple[bytes, str]:
        """Собрать multipart-тело из полей и одного проверенного локального файла.

        Параметры: fields — multipart-поля; file_field — имя file part; path — относительный API path или локальный файл.
        Результат: Байтовое multipart/form-data тело и соответствующий Content-Type с boundary.
        Исключения: FileNotFoundError/OSError при resolve/stat/read; ValueError для не-regular файла или размера >50 MiB.
        Побочные эффекты: Читает файл целиком и генерирует случайный boundary.
        Безопасность: Принимает только regular file в лимите; CR/LF/кавычки в filename заменяются, поля кодируются без shell."""
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_MEDIA_BYTES
                or resolved.is_symlink()):
            raise ValueError("invalid media file")
        boundary = "----messenger-worksheets-" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend((
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode(), b"\r\n",
            ))
        mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        safe_name = resolved.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'.encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            resolved.read_bytes(), b"\r\n", f"--{boundary}--\r\n".encode(),
        ))
        return b"".join(chunks), "multipart/form-data; boundary=" + boundary

    def send_media(
        self, chat_id: str, path: Path, *, image: bool, thread_id: int | None = None,
        caption: str | None = None,
    ) -> int:
        """Отправить изображение, галерею с подписью или документ.

        Параметры: chat_id — ID чата; path — относительный API path или локальный файл; image — отправлять как image; thread_id — ID треда или None; caption — подпись или None.
        Результат: Положительный message_id отправленного изображения, галереи или документа.
        Исключения: ValueError для ID или недопустимого файла; OSError чтения; BotApiError при транспортном сбое, невалидном JSON либо ok!=true.
        Побочные эффекты: Читает файл, строит multipart и отправляет POST выбранного media endpoint.
        Безопасность: Тип endpoint задаётся флагом image, а файл проходит regular/size и filename-проверки _multipart."""
        fields: dict[str, object] = {"chat_id": chat_id}
        if thread_id is not None:
            fields["thread_id"] = _positive_int(thread_id, "thread_id")
        endpoint = "messages/sendImage"
        field = "image"
        if image and caption:
            endpoint = "messages/sendGallery"
            field = "images"
            fields["text"] = caption[:6000]
        elif not image:
            endpoint = "messages/sendFile"
            field = "document"
        data, content_type = self._multipart(fields, file_field=field, path=path)
        with self._open(endpoint, method="POST", data=data, content_type=content_type) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BotApiError("invalid_json") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise BotApiError("api_rejected")
        return _positive_int(result.get("message_id"), "message_id")

    def download_file(self, file_id: str, target: Path, *, max_bytes: int) -> Path:
        """Скачать файл Bot API в новый локальный путь с ограничением размера.

        Параметры: file_id — ID файла API; target — новый локальный путь; max_bytes — максимум скачиваемых байтов.
        Результат: Resolved Path нового файла с mode 0600.
        Исключения: ValueError для пустого file_id; FileExistsError для существующего target; BotApiError при транспорте или превышении max_bytes; OSError записи/chmod.
        Побочные эффекты: Скачивает до max_bytes+1, создаёт parent 0700 и эксклюзивно записывает target.
        Безопасность: Не перезаписывает существующий путь; размер проверяется до записи, итоговые права принудительно 0600."""
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("invalid file_id")
        target = target.resolve()
        if target.exists():
            raise FileExistsError(target)
        with self._open("messages/getFile", params={"file_id": file_id}) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise BotApiError("file_too_large")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        os.chmod(target, 0o600)
        return target
