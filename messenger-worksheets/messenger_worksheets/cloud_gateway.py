"""Expose the existing chat behavior through a scoped remote Codex worker."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError, URLError
import uuid

from messenger_codex.bridge import (
    Bridge, CodexTurnError, POLL_SECONDS, PROGRESS_SECONDS, message_prompt,
)
from . import codex_rpc
from .codex_rpc import submit
from .chat_commands import ChatCommandMixin, COMMAND_POLL_SECONDS
from .codex_worker import MAX_RESULT_BYTES
from .personal_messenger import CHAT, PersonalMessenger


FORMATS = {".png": "png", ".jpg": "jpeg", ".gif": "gif", ".webp": "webp"}


class CloudBridge(ChatCommandMixin, Bridge):
    uses_thread_context = True

    @staticmethod
    def _worker_job_control(*args, **kwargs) -> dict:
        """Передать контроль конкретной задачи строгому аутентифицированному RPC-helper.

        Аргументы формируются самим gateway из сохранённого UUID и текущего
        маршрута; произвольные адреса или credentials из чата не принимаются.
        """
        return codex_rpc.job_control(*args, **kwargs)

    @staticmethod
    def _worker_capabilities(*args, **kwargs) -> dict:
        """Прочитать разрешённые metadata worker без обращения к модели.

        Ответ содержит модель, reasoning и режим исполнения; значения env,
        токены, пути к конфигурации и пользовательские промпты не публикуются.
        """
        return codex_rpc.worker_capabilities(*args, **kwargs)

    def _job_identifier(self, parent: int, stamp: int) -> str:
        """Построить стабильный UUID из настроенного чата и точного сообщения.

        Один и тот же UUID используется для отправки, /status и /stop.
        В другом чате даже совпавшие timestamp дают другой идентификатор.
        """
        chat = getattr(getattr(self, "messenger", None), "chat", CHAT)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chat}/{parent}/{stamp}"))

    @staticmethod
    def _history_files(snapshot: dict) -> list[dict]:
        """Закодировать приватные копии предыдущих файлов с повторной проверкой ФС.

        Чтение использует O_NOFOLLOW/O_NONBLOCK и допускает только обычный
        односсылочный файл. Общий бюджет 50 MiB, сравнение fstat до/после
        исключает изменение файла в процессе; пути gateway в RPC не передаются.
        """
        files, remaining = [], 50 * 1024 * 1024
        for item in snapshot.get("files", ()):
            descriptor = os.open(item["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > remaining:
                    raise ValueError("invalid thread history file")
                chunks, size = [], 0
                while True:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining - size + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > remaining:
                        raise ValueError("history file budget exceeded")
                after = os.fstat(descriptor)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                ) or size != before.st_size:
                    raise ValueError("history file changed while reading")
                files.append({
                    "name": item["name"],
                    "original_name": item.get("original_name", item["name"]),
                    "data": base64.b64encode(b"".join(chunks)).decode("ascii"),
                })
                remaining -= size
            finally:
                os.close(descriptor)
        return files

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
            snapshot = message.get("_thread_context")
            if snapshot is not None:
                job["session"] = None
                job["context"] = snapshot["messages"]
                job["history_files"] = self._history_files(snapshot) if task else []
            if message.get("_model_options"):
                job["options"] = dict(message["_model_options"])
            identifier = self._job_identifier(parent, stamp)
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
        result = self._run_transfer(
            parent, stamp, message, task=False, session=session,
            delivery_parent=self._delivery_parent(parent, stamp),
        )
        return result["text"], result["session"]

    def _run_transfer(
        self, parent: int, stamp: int, message: dict, *, task: bool,
        session: str | None, delivery_parent: int, worker_url: str | None = None,
        transport_token: str | None = None, ca_file: str | None = None,
    ) -> dict:
        """Ожидать remote model в фоне и обслуживать команды в основном потоке.

        SQLite и Messenger history для команд используются только foreground.
        Обычные сообщения остаются в очереди; control polling выполняется не
        чаще раза в секунду независимо от частоты спиннера. HTTP409 означает
        подтверждённую worker остановку и преобразуется в безопасную категорию.
        """
        tracked = hasattr(self, "db")
        if tracked:
            options = self._model_options(parent or stamp, task)
            if message.get("_request_mode") in {"plan", "review"}:
                options["mode"] = message["_request_mode"]
            options = self._begin_job(parent, stamp, self._job_identifier(parent, stamp), task, options)
            message = {**message, "_model_options": options}
            self._control_active_thread = parent or stamp
        state, next_commands = "running", 0.0
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="worksheet-request") as executor:
                future = executor.submit(
                    self.transfer, parent, stamp, message, task=task, session=session,
                    worker_url=worker_url, transport_token=transport_token, ca_file=ca_file,
                )
                while True:
                    try:
                        result = future.result(timeout=PROGRESS_SECONDS)
                        state = "completed"
                        return result
                    except FutureTimeout:
                        if future.done():
                            result = future.result()
                            state = "completed"
                            return result
                        if tracked and time.monotonic() >= next_commands:
                            try:
                                self._poll_commands(parent or stamp, stamp)
                            except Exception as error:
                                print(f"command_poll_failed {type(error).__name__}", flush=True)
                            next_commands = time.monotonic() + COMMAND_POLL_SECONDS
                        try:
                            self.progress(parent, stamp, delivery_parent, "Задача выполняется …")
                        except Exception as error:
                            print(f"progress_update_failed {type(error).__name__}", flush=True)
        except HTTPError as error:
            if error.code == 409:
                state = "cancelled"
                raise CodexTurnError("worker_cancelled") from None
            state = "failed" if error.code in {503, 504} else "unknown"
            raise CodexTurnError(f"worker_http_{error.code}") from None
        except URLError:
            state = "running"
            raise CodexTurnError("worker_connection_failed") from None
        finally:
            if tracked:
                self._control_active_thread = None
                try:
                    self._finish_job(parent or stamp, state, self._job_identifier(parent, stamp))
                except Exception as error:
                    print(f"command_completion_failed {type(error).__name__}", flush=True)

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
        result = self._run_transfer(
            parent, stamp, message, task=True, session=None,
            delivery_parent=delivery_parent, worker_url=task_url,
            transport_token=task_token, ca_file=task_ca,
        )
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
