"""Expose the existing chat behavior through a scoped remote Codex worker."""

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
    Bridge, CodexTurnError, POLL_SECONDS, PROGRESS_SECONDS, message_prompt,
)
from .codex_rpc import submit
from .codex_worker import MAX_RESULT_BYTES
from .personal_messenger import CHAT, PersonalMessenger


FORMATS = {".png": "png", ".jpg": "jpeg", ".gif": "gif", ".webp": "webp"}


class CloudBridge(Bridge):
    def transfer(
        self, parent: int, stamp: int, message: dict, *, task: bool, session: str | None,
        worker_url: str | None = None, transport_token: str | None = None,
        ca_file: str | None = None,
    ) -> dict:
        """Передать нормализованное сообщение и изображения удалённому Codex worker и проверить результат.

        Параметры: parent — ID треда; 0 означает основной chat; stamp — timestamp входящего сообщения; message — нормализованное сообщение/update; task — режим файловой задачи; session — UUID chat session или None; worker_url — URL chat worker; transport_token — token выбранного route; ca_file — CA-файл выбранного worker route.
        Результат: Строго проверенный dict text/session/files от worker.
        Исключения: ValueError для полей, текста, списка файлов, task/session или chat UUID; ошибки download/base64/submit.
        Побочные эффекты: Временно скачивает изображения, кодирует base64 и выполняет HTTPS RPC.
        Безопасность: Job UUID детерминирован из фиксированного CHAT/parent/stamp; наружу передаётся только data-only payload."""
        if message_prompt(message, parent) is None:
            raise ValueError("message has no model content")
        with TemporaryDirectory(prefix="cloud-gateway-images-") as directory:
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
            identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{CHAT}/{parent}/{stamp}"))
            result = submit(
                worker_url or os.environ["WORKSHEET_CODEX_WORKER_URL"],
                identifier,
                job,
                transport_token=transport_token,
                ca_file=ca_file,
            )
        if not isinstance(result, dict) or result.keys() != {"text", "session", "files"}:
            raise ValueError("invalid Codex response fields")
        if not isinstance(result["text"], str) or not result["text"].strip() or len(result["text"]) > 6000:
            raise ValueError("invalid Codex response text")
        if not isinstance(result["files"], list):
            raise ValueError("invalid result file list")
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
        """Получить обычный ответ удалённого worker и новую идентичность сессии.

        Параметры: parent — ID треда; 0 означает основной chat; prompt — готовый prompt; message — нормализованное сообщение/update; session — UUID chat session или None.
        Результат: Пару text и канонического UUID session chat worker.
        Исключения: Ошибки CloudBridge.transfer.
        Побочные эффекты: Выполняет один chat RPC.
        Безопасность: Всегда task=False; route и транспортная аутентификация берутся из настроенного worker."""
        stamp = message["ts"]
        result = self.transfer(parent, stamp, message, task=False, session=session)
        return result["text"], result["session"]

    def _task(
        self, parent: int, stamp: int, message: dict, *, delivery_parent: int,
    ) -> tuple[str, tuple[Path, ...]]:
        """Выполнить удалённую файловую задачу с периодическим обновлением прогресса.

        Параметры: parent — ID треда; 0 означает основной chat; stamp — timestamp входящего сообщения; message — нормализованное сообщение/update; delivery_parent — тред доставки.
        Результат: Текст результата и кортеж безопасно сохранённых артефактов.
        Исключения: ValueError для неполного task route/невалидного файла/бюджета; CodexTurnError для HTTP/connection; binascii.Error и ошибки progress/RPC/ФС.
        Побочные эффекты: Запускает task RPC в thread, обновляет progress, декодирует файлы во временный out и вызывает _preserve_files.
        Безопасность: Task route задаётся полной URL+token парой; basename/control chars/base64 и общий MAX_RESULT_BYTES проверяются до сохранения."""
        self.progress(
            parent, stamp, delivery_parent, "Задача запущена …",
        )
        task_url = os.environ.get("WORKSHEET_TASK_WORKER_URL")
        task_token = os.environ.get("WORKSHEET_TASK_WORKER_TRANSPORT_TOKEN")
        task_ca = os.environ.get("WORKSHEET_TASK_WORKER_CA")
        if len({bool(task_url), bool(task_token), bool(task_ca)}) != 1:
            raise ValueError("task worker routing is incomplete")
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="worksheet-task") as executor:
            future = executor.submit(
                self.transfer,
                parent,
                stamp,
                message,
                task=True,
                session=None,
                worker_url=task_url,
                transport_token=task_token,
                ca_file=task_ca,
            )
            while True:
                try:
                    result = future.result(timeout=PROGRESS_SECONDS)
                    break
                except FutureTimeout:
                    if future.done():
                        # Это может быть TimeoutError самой завершившейся
                        # операции, а не истечение короткого ожидания progress.
                        result = future.result()
                        break
                    try:
                        self.progress(parent, stamp, delivery_parent, "Задача выполняется …")
                    except Exception as error:
                        print(f"progress_update_failed {type(error).__name__}", flush=True)
                except HTTPError as error:
                    raise CodexTurnError(f"worker_http_{error.code}") from None
                except URLError:
                    raise CodexTurnError("worker_connection_failed") from None
        with TemporaryDirectory(prefix="cloud-gateway-results-") as directory:
            output = Path(directory)
            remaining = MAX_RESULT_BYTES
            for file in result["files"]:
                if not isinstance(file, dict) or file.keys() != {"name", "data"}:
                    raise ValueError("invalid result artifact")
                name = file["name"]
                encoded = file["data"]
                if (not isinstance(name, str) or Path(name).name != name or name in ("", ".", "..")
                        or len(name.encode()) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in name)
                        or not isinstance(encoded, str)
                        or len(encoded) > 4 * ((remaining + 2) // 3)):
                    raise ValueError("invalid result file")
                content = base64.b64decode(encoded, validate=True)
                if len(content) > remaining:
                    raise ValueError("result file budget exceeded")
                with (output / name).open("xb") as stream:
                    stream.write(content)
                remaining -= len(content)
            return result["text"], self._preserve_files(output)


def main() -> None:
    """Запустить персональный cloud gateway с эксклюзивной блокировкой состояния.

    Параметры: нет.
    Результат: Ничего; обслуживает персональный gateway до остановки процесса.
    Исключения: Ошибки PersonalMessenger/flock/sqlite3 до loop; ошибки poll внутри loop журналируются и повторяются.
    Побочные эффекты: Устанавливает umask 077, создаёт workspace/SQLite, берёт flock и выполняет polling.
    Безопасность: Эксклюзивный lock и PersonalMessenger привязывают state к одному процессу, аккаунту и chat."""
    os.umask(0o077)
    state_dir = Path(os.environ["STATE_DIRECTORY"])
    workspace = state_dir / "gateway"
    workspace.mkdir(mode=0o700, exist_ok=True)
    messenger = PersonalMessenger()
    with (state_dir / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(state_dir / "gateway.sqlite3") as database:
            bridge = CloudBridge(database, messenger, workspace)
            print("personal_cloud_gateway_ready", flush=True)
            while True:
                try:
                    bridge.poll()
                except Exception as error:
                    print(f"personal_cloud_gateway_retry {type(error).__name__}", flush=True)
                time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
