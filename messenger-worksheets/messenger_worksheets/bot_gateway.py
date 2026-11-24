"""Route two Messenger chats through the official public Bot API."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import fcntl
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError, URLError
import uuid

from messenger_codex.bridge import (
    Bridge, CodexTurnError, PROGRESS_SECONDS, message_prompt,
)
from .bot_api import BotApiClient, BotApiError
from .codex_rpc import submit
from .codex_worker import MAX_RESULT_BYTES


FORMATS = {".png": "png", ".jpg": "jpeg", ".gif": "gif", ".webp": "webp"}
POLL_SECONDS = 1.0


class BotMessenger:
    def __init__(self, client: BotApiClient, chat: str, identity: dict) -> None:
        """Адаптировать Bot API клиента к интерфейсу Messenger моста для одного чата.

        Параметры: client — проверенный BotApiClient; chat — ID целевого чата; identity — подтверждённая identity бота.
        Результат: Адаптер с client/chat и сокращённой identity guid/login.
        Исключения: KeyError, если identity не содержит id или login.
        Побочные эффекты: Сохраняет ссылки и копирует два поля identity.
        Безопасность: Не переносит в мост прочие поля identity или token клиента."""
        self.client = client
        self.chat = chat
        self.identity = {"guid": identity["id"], "login": identity["login"]}

    def send(self, parent: int, reply: str, payload_id: str) -> int:
        """Отправить текст в чат или тред через Bot API.

        Параметры: parent — ID треда; 0 означает основной chat; reply — текст ответа; payload_id — UUID идемпотентности.
        Результат: Положительный message_id ответа Bot API.
        Исключения: ValueError/BotApiError из send_text.
        Побочные эффекты: Отправляет текст в chat либо parent-тред.
        Безопасность: Целевой chat зафиксирован в адаптере, payload_id проверяется BotApiClient."""
        return self.client.send_text(
            self.chat, reply, payload_id=payload_id, thread_id=parent or None,
        )

    def edit(self, parent: int, timestamp: int, text: str) -> None:
        """Идемпотентно заменить текст ранее отправленного сообщения.

        Параметры: parent — ID треда; 0 означает основной chat; timestamp — message_id; text — новый текст.
        Результат: Ничего; исходный timestamp подтверждён ответом API.
        Исключения: ValueError/BotApiError из send_text; BotApiError edit_message_mismatch при другом ID.
        Побочные эффекты: Отправляет edit через sendText с детерминированным UUID payload.
        Безопасность: UUID связывает правку с chat/parent/timestamp/text, а ответ обязан сохранить исходный message_id."""
        payload = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"bot-edit:{self.chat}:{parent}:{timestamp}:{text}",
        ))
        result = self.client.send_text(
            self.chat, text, payload_id=payload, thread_id=parent or None,
            message_id=timestamp,
        )
        if result != timestamp:
            raise BotApiError("edit_message_mismatch")

    def react(
        self, parent: int, timestamp: int, reaction: str, *,
        replace: bool = False, remove: bool = False,
    ) -> None:
        """Преобразовать разрешённый emoji в реакцию Bot API и применить операцию.

        Параметры: parent — ID треда; 0 означает основной chat; timestamp — message_id; reaction — emoji реакции; replace — заменить прежнюю реакцию; remove — удалить реакцию.
        Результат: Ничего; требуемая реакция применена либо удалена.
        Исключения: ValueError для конфликтующих флагов/неподдерживаемого emoji; BotApiError от API.
        Побочные эффекты: Может сначала удалить прежнюю реакцию, затем установить новую или null.
        Безопасность: Разрешены только явно сопоставленные eyes/check имена из конфигурации."""
        if replace and remove:
            raise ValueError("reaction cannot be replaced and removed together")
        mapping = {
            "👀": os.environ.get("WORKSHEET_BOT_EYES_REACTION", "eyes"),
            "💔": os.environ.get("WORKSHEET_BOT_ERROR_REACTION", "broken_heart"),
            "✅": os.environ.get("WORKSHEET_BOT_SUCCESS_REACTION", "check"),
        }
        name = mapping.get(reaction)
        if not name:
            raise ValueError("unsupported bot reaction")
        if replace:
            self.client.send_reaction(
                self.chat, timestamp, reaction_name=None, thread_id=parent or None,
            )
        self.client.send_reaction(
            self.chat, timestamp,
            reaction_name=None if remove else name,
            thread_id=parent or None,
        )

    def send_file(self, parent: int, path: Path, payload: str, receipts: Path) -> None:
        """Отправить PNG как изображение с подписью и любой файл как документ.

        Параметры: parent — ID треда; 0 означает основной chat; path — локальный artifact; payload — UUID этапа; receipts — каталог receipts.
        Результат: Ничего; PNG отправлен image+document, прочий файл — document.
        Исключения: OSError чтения; ValueError/BotApiError из send_media.
        Побочные эффекты: Читает magic bytes и выполняет один или два media POST.
        Безопасность: Классифицирует PNG по сигнатуре, а BotApiClient повторно проверяет regular file и размер."""
        del payload, receipts
        with path.open("rb") as stream:
            is_png = stream.read(8) == b"\x89PNG\r\n\x1a\n"
        if is_png:
            self.client.send_media(
                self.chat, path, image=True, thread_id=parent or None,
                caption=f"Изображение: {path.name}",
            )
        self.client.send_media(
            self.chat, path, image=False, thread_id=parent or None,
        )

    def download_images(self, media: list[dict], directory: Path) -> tuple[Path, ...]:
        """Скачать и распознать допустимые изображения обновления в пределах общего бюджета.

        Параметры: media — нормализованные вложения; directory — временный каталог.
        Результат: Кортеж путей распознанных изображений в directory.
        Исключения: Ошибки download/OSError/ValueError отдельных вложений подавляются; OSError возможен при последующем read/rename/unlink/stat.
        Побочные эффекты: Скачивает image-вложения, проверяет magic bytes, удаляет неизвестные и переименовывает допустимые.
        Безопасность: Общий бюджет 20 MiB; file_id обязателен, расширение определяется содержимым."""
        paths: list[Path] = []
        remaining = 20 * 1024 * 1024
        for item in media:
            if item.get("kind") != "image" or not remaining:
                continue
            file_id = item.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                continue
            path = directory / f"attachment-{len(paths) + 1}.bin"
            try:
                self.client.download_file(file_id, path, max_bytes=remaining)
            except (BotApiError, OSError, ValueError):
                continue
            header = path.read_bytes()[:12]
            if header.startswith(b"\x89PNG\r\n\x1a\n"):
                suffix = ".png"
            elif header.startswith(b"\xff\xd8\xff"):
                suffix = ".jpg"
            elif header.startswith((b"GIF87a", b"GIF89a")):
                suffix = ".gif"
            elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
                suffix = ".webp"
            else:
                path.unlink(missing_ok=True)
                continue
            remaining -= path.stat().st_size
            paths.append(path.rename(path.with_suffix(suffix)))
        return tuple(paths)


class BotCloudBridge(Bridge):
    def __init__(
        self, db: sqlite3.Connection, messenger: BotMessenger, workspace: Path, *,
        worker_url: str, worker_token: str, worker_ca: str,
        task_url: str | None = None, task_token: str | None = None,
        task_ca: str | None = None,
    ) -> None:
        """Настроить мост чата с отдельными маршрутами worker для чата и задач.

        Параметры: db — SQLite-соединение; messenger — Messenger adapter; workspace — приватный workspace; worker_url — URL chat worker; worker_token — token chat worker; worker_ca — CA-файл chat worker; task_url — URL task worker или None; task_token — token task worker или None; task_ca — CA-файл task worker или None.
        Результат: Bridge с отдельными URL/token для chat и task worker.
        Исключения: Исключения Bridge.__init__.
        Побочные эффекты: Инициализирует SQLite bridge и сохраняет маршруты worker.
        Безопасность: Task route целиком наследует chat route, если отдельная пара не передана."""
        super().__init__(db, messenger, workspace)
        self.worker_url = worker_url
        self.worker_token = worker_token
        self.worker_ca = worker_ca
        self.task_url = task_url or worker_url
        self.task_token = task_token or worker_token
        self.task_ca = task_ca or worker_ca

    def transfer(
        self, parent: int, stamp: int, message: dict, *, task: bool,
        session: str | None, worker_url: str, transport_token: str, ca_file: str,
    ) -> dict:
        """Собрать data-only job, отправить его worker и строго проверить ответ.

        Параметры: parent — ID треда; 0 означает основной chat; stamp — timestamp входящего сообщения; message — нормализованное сообщение/update; task — режим файловой задачи; session — UUID chat session или None; worker_url — URL chat worker; transport_token — token выбранного route; ca_file — CA-файл выбранного worker route.
        Результат: Строго проверенный dict с ключами text, session и files.
        Исключения: ValueError для структуры/типов/UUID результата; ошибки download/base64/submit, включая HTTPError/URLError.
        Побочные эффекты: Во временный каталог скачивает изображения, читает/base64-кодирует их и делает HTTPS RPC.
        Безопасность: Job ID детерминирован из chat/parent/stamp; worker получает только JSON prompt и ограниченные изображения."""
        with TemporaryDirectory(prefix="bot-gateway-images-") as directory:
            paths = self.messenger.download_images(message.get("media", []), Path(directory))
            images = [
                {"format": FORMATS[path.suffix],
                 "data": base64.b64encode(path.read_bytes()).decode("ascii")}
                for path in paths
            ]
            job = {
                "text": message_prompt(message, parent, len(images)),
                "task": task,
                "session": session,
                "images": images,
            }
            chat_id = message["chat_id"]
            identifier = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"bot:{chat_id}:{parent}:{stamp}",
            ))
            result = submit(
                worker_url, identifier, job,
                transport_token=transport_token, ca_file=ca_file,
            )
        if not isinstance(result, dict) or result.keys() != {"text", "session", "files"}:
            raise ValueError("invalid Codex response fields")
        if not isinstance(result["text"], str) or not result["text"].strip() \
                or len(result["text"]) > 6000 or not isinstance(result["files"], list):
            raise ValueError("invalid Codex response")
        if task:
            if result["session"] is not None:
                raise ValueError("a task returned a chat session")
        elif (result["files"] or not isinstance(result["session"], str)
              or str(uuid.UUID(result["session"])) != result["session"]):
            raise ValueError("invalid chat outcome")
        return result

    def _draft(
        self, parent: int, prompt: str, message: dict, session: str | None,
    ) -> tuple[str, str]:
        """Получить текстовый ответ удалённого worker с продолжением сессии.

        Параметры: parent — ID треда; 0 означает основной chat; prompt — готовый prompt; message — нормализованное сообщение/update; session — UUID chat session или None.
        Результат: Пару text и канонического UUID session из chat worker.
        Исключения: Ошибки BotCloudBridge.transfer.
        Побочные эффекты: Выполняет один RPC к chat worker.
        Безопасность: Принудительно task=False и использует только настроенные chat URL/token."""
        del prompt
        result = self.transfer(
            parent, message["ts"], message, task=False, session=session,
            worker_url=self.worker_url, transport_token=self.worker_token,
            ca_file=self.worker_ca,
        )
        return result["text"], result["session"]

    def _task(
        self, parent: int, stamp: int, message: dict, *, delivery_parent: int,
    ) -> tuple[str, tuple[Path, ...]]:
        """Выполнить удалённую файловую задачу, показывая прогресс и проверяя артефакты.

        Параметры: parent — ID треда; 0 означает основной chat; stamp — timestamp входящего сообщения; message — нормализованное сообщение/update; delivery_parent — тред доставки.
        Результат: Текст результата и кортеж файлов, сохранённых через _preserve_files.
        Исключения: CodexTurnError для HTTP/connection worker; ValueError/binascii.Error для невалидных артефактов; ошибки progress/RPC/ФС.
        Побочные эффекты: Запускает transfer в одном thread, обновляет progress, декодирует файлы во временный каталог и сохраняет их.
        Безопасность: Task route отделён от chat; имена должны быть basename <=200 bytes, base64 строгий, общий размер <=MAX_RESULT_BYTES."""
        self.progress(parent, stamp, delivery_parent, "Задача запущена …")
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="bot-task") as executor:
            future = executor.submit(
                self.transfer, parent, stamp, message, task=True, session=None,
                worker_url=self.task_url, transport_token=self.task_token,
                ca_file=self.task_ca,
            )
            while True:
                try:
                    result = future.result(timeout=PROGRESS_SECONDS)
                    break
                except FutureTimeout:
                    try:
                        self.progress(parent, stamp, delivery_parent, "Задача выполняется …")
                    except Exception as error:
                        print(f"bot_progress_update_failed {type(error).__name__}", flush=True)
                except HTTPError as error:
                    raise CodexTurnError(f"worker_http_{error.code}") from None
                except URLError:
                    raise CodexTurnError("worker_connection_failed") from None
        with TemporaryDirectory(prefix="bot-gateway-results-") as directory:
            output = Path(directory)
            remaining = MAX_RESULT_BYTES
            for file in result["files"]:
                if not isinstance(file, dict) or file.keys() != {"name", "data"}:
                    raise ValueError("invalid result artifact")
                name, encoded = file["name"], file["data"]
                if (not isinstance(name, str) or Path(name).name != name or name in ("", ".", "..")
                        or len(name.encode()) > 200 or not isinstance(encoded, str)):
                    raise ValueError("invalid result file")
                content = base64.b64decode(encoded, validate=True)
                if len(content) > remaining:
                    raise ValueError("result file budget exceeded")
                (output / name).write_bytes(content)
                remaining -= len(content)
            return result["text"], self._preserve_files(output)

    def handle_update(self, update: dict) -> None:
        """Нормализовать одно Bot API update и передать допустимое сообщение в общий pipeline.

        Параметры: update — Bot API update.
        Результат: Ничего; допустимый update обработан и cursor/session feed обновлены либо update проигнорирован.
        Исключения: Ошибки SQLite и _respond для принятого сообщения.
        Побочные эффекты: Нормализует update, может создать feed, отправить ответ и обновить SQLite.
        Безопасность: Игнорирует invalid message_id и собственный sender; изображения берёт только из структурных variants с file_id."""
        message_id = update.get("message_id")
        if type(message_id) is not int or message_id <= 0:
            return
        sender = update.get("from") or {}
        if not isinstance(sender, dict) or sender.get("id") == self.messenger.identity["guid"]:
            return
        thread_id = update.get("thread_id")
        parent = thread_id if type(thread_id) is int and thread_id > 0 else 0
        images = update.get("images") or []
        media: list[dict] = []
        if isinstance(images, list):
            for variants in images:
                if not isinstance(variants, list):
                    continue
                originals = [item for item in variants if isinstance(item, dict) and item.get("file_id")]
                if originals:
                    image = originals[-1]
                    media.append({
                        "kind": "image", "file_id": image["file_id"],
                        "size": image.get("size"), "name": image.get("name"),
                    })
        file = update.get("file")
        if isinstance(file, dict):
            media.append({"kind": "miscfile", **file})
        message = {
            "chat_id": self.messenger.chat,
            "ts": message_id,
            "text": update.get("text") if isinstance(update.get("text"), str) else "",
            "author_guid": sender.get("id"),
            "author_login": sender.get("login"),
            "media": media,
            "unknown_content": bool(file or update.get("sticker") or update.get("bot_request")),
            "deleted": False,
        }
        prompt = message_prompt(message, parent)
        if prompt is None:
            return
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO feeds VALUES (?, ?, NULL)",
                (parent, message_id),
            )
        row = self.db.execute("SELECT session FROM feeds WHERE parent=?", (parent,)).fetchone()
        session = row[0] if row else None
        session = self._respond(parent, message_id, prompt, message, session)
        with self.db:
            self.db.execute(
                "UPDATE feeds SET cursor=?, session=? WHERE parent=?",
                (message_id, session, parent),
            )


def _profile(prefix: str) -> tuple[str, str, str, str | None, str | None, str | None]:
    """Прочитать согласованный набор URL и токенов worker для профиля чата.

    Параметры: prefix — API или SANDBOX profile.
    Результат: Кортеж worker_url, worker_token, task_url и task_token для prefix.
    Исключения: KeyError для отсутствующих обязательных env; ValueError, если задана только половина task route.
    Побочные эффекты: Читает переменные окружения.
    Безопасность: Отдельный task URL разрешён только вместе с отдельным token."""
    worker_url = os.environ[f"WORKSHEET_BOT_{prefix}_WORKER_URL"]
    worker_token = os.environ[f"WORKSHEET_BOT_{prefix}_WORKER_TOKEN"]
    worker_ca = os.environ[f"WORKSHEET_BOT_{prefix}_WORKER_CA"]
    task_url = os.environ.get(f"WORKSHEET_BOT_{prefix}_TASK_URL")
    task_token = os.environ.get(f"WORKSHEET_BOT_{prefix}_TASK_TOKEN")
    task_ca = os.environ.get(f"WORKSHEET_BOT_{prefix}_TASK_CA")
    if len({bool(task_url), bool(task_token), bool(task_ca)}) != 1:
        raise ValueError(f"incomplete {prefix} task routing")
    return worker_url, worker_token, worker_ca, task_url, task_token, task_ca


def main() -> None:
    """Запустить Bot API gateway для двух разрешённых чатов и хранить offset обновлений.

    Параметры: нет.
    Результат: Ничего; обслуживает два Bot API chat до остановки процесса.
    Исключения: KeyError/ValueError конфигурации и identity; OSError/flock/sqlite3.Error; BotApiError invalid_update_id; остальные ошибки polling перехватываются с retry-log.
    Побочные эффекты: Создаёт clients/SQLite/workspaces, берёт flock, long-poll updates и коммитит offset после каждого update.
    Безопасность: Проверяет display_name/organization, требует два разных chat ID и продвигает offset только после обработки update."""
    os.umask(0o077)
    state = Path(os.environ["STATE_DIRECTORY"])
    token = os.environ["WORKSHEET_BOT_TOKEN"]
    client = BotApiClient(
        token,
        auth_scheme=os.environ.get("WORKSHEET_BOT_AUTH_SCHEME", "OAuth"),
        base_url=os.environ.get(
            "WORKSHEET_BOT_BASE_URL", "https://botapi.messenger.yandex.net/bot/v1/",
        ),
    )
    identity = client.identity()
    expected_name = os.environ.get("WORKSHEET_BOT_EXPECTED_NAME", "Lang Env Bot")
    if identity.get("display_name") != expected_name:
        raise ValueError("Bot API identity does not match expected display name")
    expected_organization = os.environ.get("WORKSHEET_BOT_EXPECTED_ORGANIZATION_ID")
    if (expected_organization is not None
            and int(expected_organization) not in (identity.get("organizations") or [])):
        raise ValueError("Bot API identity does not match expected organization")
    chats = {
        os.environ["WORKSHEET_BOT_API_CHAT_ID"]: "API",
        os.environ["WORKSHEET_BOT_SANDBOX_CHAT_ID"]: "SANDBOX",
    }
    if len(chats) != 2:
        raise ValueError("Bot API chats must be distinct")
    connections: list[sqlite3.Connection] = []
    bridges: dict[str, BotCloudBridge] = {}
    for chat_id, prefix in chats.items():
        database = sqlite3.connect(state / f"{prefix.casefold()}.sqlite3")
        connections.append(database)
        messenger = BotMessenger(client, chat_id, identity)
        worker_url, worker_token, worker_ca, task_url, task_token, task_ca = _profile(prefix)
        workspace = state / prefix.casefold()
        workspace.mkdir(mode=0o700, exist_ok=True)
        bridges[chat_id] = BotCloudBridge(
            database, messenger, workspace,
            worker_url=worker_url, worker_token=worker_token, worker_ca=worker_ca,
            task_url=task_url, task_token=task_token, task_ca=task_ca,
        )
    cursor = sqlite3.connect(state / "updates.sqlite3")
    connections.append(cursor)
    cursor.execute("CREATE TABLE IF NOT EXISTS cursor (singleton INTEGER PRIMARY KEY, offset INTEGER)")
    cursor.execute("INSERT OR IGNORE INTO cursor VALUES (1, 0)")
    cursor.commit()
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("bot_gateway_ready", flush=True)
        while True:
            offset = cursor.execute("SELECT offset FROM cursor WHERE singleton=1").fetchone()[0]
            try:
                updates = sorted(client.get_updates(offset), key=lambda item: item.get("update_id", 0))
                for update in updates:
                    update_id = update.get("update_id")
                    if type(update_id) is not int or update_id < offset:
                        raise BotApiError("invalid_update_id")
                    chat = update.get("chat") or {}
                    bridge = bridges.get(chat.get("id") if isinstance(chat, dict) else None)
                    if bridge is not None:
                        bridge.handle_update(update)
                    with cursor:
                        cursor.execute(
                            "UPDATE cursor SET offset=? WHERE singleton=1",
                            (update_id + 1,),
                        )
            except Exception as error:
                print(f"bot_gateway_retry {type(error).__name__}", flush=True)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
