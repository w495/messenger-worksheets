#!/usr/bin/env python3
"""Проверить управляющий RPC и чтение history-файлов настоящим Codex worker."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time
from urllib.error import HTTPError
import uuid

from smoke_test_worker import load_rpc_environment
from messenger_worksheets.codex_rpc import job_control, submit, worker_capabilities


def main() -> None:
    """Выполнить фиксированный live smoke sandbox-route без отправки в Messenger.

    Читает только RPC-профиль sandbox gateway, не выводя токен и адрес.
    Plan/review должны инструментами прочитать случайный маркер из history-файла;
    маркер не передаётся в тексте запроса. Затем отдельный job отменяется по UUID
    с проверкой terminal cancelled и HTTP409. Соседние задания не затрагиваются.
    Любое несоответствие вызывает RuntimeError; stdout содержит только итоги.
    """
    load_rpc_environment(Path("/etc/messenger-worksheets/gateway-sandbox-admin.env"))
    endpoint = os.environ["WORKSHEET_CODEX_WORKER_URL"]
    metadata = worker_capabilities(endpoint)
    if not metadata["task_supported"]:
        raise RuntimeError("control smoke requires a task worker")
    print("worker_capabilities_smoke_ok", flush=True)
    for mode in ("plan", "review"):
        marker = "READONLY_OK_" + uuid.uuid4().hex
        source = f"# {marker}\ndef ratio(x):\n    return x / 0\n".encode()
        job = {
            "text": (
                "Сначала инструментом команд прочитай файл input/history/check.py. "
                "Включи в ответ точное слово READONLY_OK с его полным суффиксом из первой строки файла. "
                "Затем составь план исправления дефекта." if mode == "plan" else
                "Инструментом команд прочитай input/history/check.py. Укажи дефект и точное слово "
                "READONLY_OK с его полным суффиксом из первой строки файла. Ничего не исправляй."
            ),
            "task": True, "session": None, "images": [], "context": [],
            "history_files": [{"name": "check.py", "original_name": "check.py",
                               "data": base64.b64encode(source).decode("ascii")}],
            "options": {"mode": mode},
        }
        result = submit(endpoint, str(uuid.uuid4()), job)
        if marker not in result["text"] or result["files"] or result["session"] is not None:
            raise RuntimeError(f"{mode} smoke did not prove readonly history access")
        print(f"worker_{mode}_smoke_ok history_file_read=yes artifacts=no", flush=True)
    identifier = str(uuid.uuid4())
    job = {"text": "Выполни команду sleep 60 и затем ответь SLEEP_COMPLETE.",
           "task": True, "session": None, "images": []}
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(submit, endpoint, identifier, job)
        deadline = time.monotonic() + 20
        while job_control(endpoint, identifier)["state"] != "running":
            if pending.done() or time.monotonic() >= deadline:
                raise RuntimeError("cancellation smoke did not start")
            time.sleep(0.2)
        job_control(endpoint, identifier, action="cancel")
        try:
            pending.result(timeout=20)
        except HTTPError as error:
            if error.code != 409:
                raise RuntimeError("cancelled submit returned an unexpected status") from None
        else:
            raise RuntimeError("cancelled submit unexpectedly succeeded")
        if job_control(endpoint, identifier)["state"] != "cancelled":
            raise RuntimeError("worker did not confirm cancellation")
    print("worker_cancel_smoke_ok terminal=cancelled", flush=True)


if __name__ == "__main__":
    main()
