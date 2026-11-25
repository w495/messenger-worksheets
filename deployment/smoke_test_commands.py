#!/usr/bin/env python3
"""Проверить управляющий RPC и чтение history-файлов настоящим Codex worker."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
import uuid

from smoke_test_worker import load_rpc_environment
from messenger_worksheets.codex_rpc import job_control, submit, worker_capabilities, worker_models


def readonly_smoke_job(mode: str) -> tuple[dict, str]:
    """Создать проверку чтения по имени функции, которое отсутствует в самом запросе.

    Принимает plan/review и возвращает RPC job с синтетическим Python-файлом
    и ожидаемое полное имя функции. Имя составлено из обычных слов и относится
    непосредственно к дефекту, который модель должна разобрать. Оно находится
    только в файле; из текста запроса или metadata узнать его нельзя. Никаких
    служебных токенов, credentials, сетевых или файловых операций здесь нет.
    """
    if mode not in {"plan", "review"}:
        raise ValueError("invalid readonly smoke mode")
    words = ("amber", "birch", "cedar", "coral", "delta", "elm", "fern", "iris",
             "maple", "moss", "oak", "olive", "opal", "pine", "reed", "willow")
    symbol = "divide_" + "_".join(secrets.choice(words) for _ in range(4))
    source = f"def {symbol}(x):\n    return x / 0\n".encode()
    text = (
        "Составь план исправления дефекта в input/history/check.py. Сначала прочитай файл инструментом команд. "
        "В заголовке плана укажи полное имя функции из объявления def. Файл не меняй."
        if mode == "plan" else
        "Проверь input/history/check.py инструментом команд. Укажи полное имя функции из объявления def "
        "и конкретный подтверждённый дефект. Ничего не исправляй."
    )
    return {"text": text, "task": True, "session": None, "images": [], "context": [],
            "history_files": [{"name": "check.py", "original_name": "check.py",
                               "data": base64.b64encode(source).decode("ascii")}],
            "options": {"mode": mode}}, symbol


def readonly_failure_category(text: str) -> str:
    """Свести ответ синтетической проверки к фиксированной причине без раскрытия текста.

    Принимает только ответ собственного smoke-job и возвращает безопасную
    категорию известных ошибок sandbox либо отсутствие доказательства чтения.
    Исходный текст модели, пути, случайный маркер и credentials не публикуются.
    Функция не обращается к сети и не меняет состояние.
    """
    lowered = text.casefold()
    if "bwrap" in lowered and ("uid" in lowered or "uid_map" in lowered):
        return "bwrap_uid_map"
    if "bwrap" in lowered and ("loopback" in lowered or "rtm_newaddr" in lowered):
        return "bwrap_loopback"
    if "bwrap" in lowered:
        return "bwrap_other"
    if "permission denied" in lowered or "отказано в доступе" in lowered:
        return "permission_denied"
    if "no such file" in lowered or "файл не найден" in lowered:
        return "file_not_found"
    return "read_not_proved"


def safe_smoke_reply(text: str) -> str:
    """Подготовить короткую диагностику только собственного синтетического smoke-job.

    Входы проверки не содержат переписки или пользовательских документов.
    Перед выводом дополнительно маскируются типичные ключи, bearer, JWT,
    значения token/password-полей и query/fragment URL. Возвращается максимум
    1200 символов с экранированными переносами строк. Произвольные ответы
    production-задач через этот диагностический путь не проходят.
    """
    redacted = re.sub(r"(?i)\bBearer\s+\S+|\b(?:sk-|gh[pousr]_|y[01]_|t[01]_|AQAD-)[A-Za-z0-9_.-]{12,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<masked>", text)
    redacted = re.sub(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+", r"\1<masked>", redacted)
    def clean_url(match):
        """Убрать query, userinfo и fragment диагностической ссылки без сетевого обращения."""
        try:
            parsed = urlsplit(match[0])
            if parsed.username is not None or parsed.password is not None:
                return "<url-masked>"
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<query-masked>" if parsed.query else "", ""))
        except ValueError:
            return "<url-masked>"
    redacted = re.sub(r"https?://[^\s`<>]+", clean_url, redacted)
    return "".join(char if ord(char) >= 32 and ord(char) != 127 else f"\\x{ord(char):02x}" for char in redacted[:1200])


def catalog_smoke() -> list[str]:
    """Проверить каталоги обоих production-route без вызова языковой модели.

    Профили жёстко заданы, токены загружаются только штатным env-loader и не
    печатаются. Сбой одного каталога не пропускает проверку второго. Возвращает
    список безопасных обозначений неудачных проверок; последним остаётся env
    sandbox-route для дальнейших tool/cancel smoke-запросов.
    """
    failed = []
    for name, filename in (("api", "gateway-api.env"), ("task", "gateway-sandbox-admin.env")):
        try:
            load_rpc_environment(Path("/etc/messenger-worksheets") / filename)
            catalog = worker_models(os.environ["WORKSHEET_CODEX_WORKER_URL"])
            if not catalog["available"] or not catalog["models"]:
                raise RuntimeError("model catalogue unavailable")
            print(f"worker_models_smoke_ok route={name} count={len(catalog['models'])}", flush=True)
        except Exception:
            print(f"worker_models_smoke_failed route={name}", flush=True)
            failed.append("models_" + name)
    return failed


def main() -> None:
    """Выполнить фиксированный live smoke sandbox-route без отправки в Messenger.

    Читает только RPC-профиль sandbox gateway, не выводя токен и адрес.
    Plan/review должны инструментами прочитать имя функции из history-файла;
    имя не передаётся в тексте запроса. Затем отдельный job отменяется по UUID
    с проверкой terminal cancelled и HTTP409. Соседние задания не затрагиваются.
    Любое несоответствие вызывает RuntimeError; stdout содержит только итоги.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-only", action="store_true")
    args = parser.parse_args()
    failed = catalog_smoke()
    if args.models_only:
        if failed:
            raise RuntimeError("model catalog smoke failed")
        return
    load_rpc_environment(Path("/etc/messenger-worksheets/gateway-sandbox-admin.env"))
    endpoint = os.environ["WORKSHEET_CODEX_WORKER_URL"]
    metadata = worker_capabilities(endpoint)
    if not metadata["task_supported"]:
        raise RuntimeError("control smoke requires a task worker")
    print("worker_capabilities_smoke_ok", flush=True)
    for mode in ("plan", "review"):
        job, marker = readonly_smoke_job(mode)
        try:
            result = submit(endpoint, str(uuid.uuid4()), job)
            valid = marker in result["text"] and not result["files"] and result["session"] is None
            if valid:
                print(f"worker_{mode}_smoke_ok history_file_read=yes artifacts=no", flush=True)
            else:
                print(f"worker_{mode}_smoke_failed category={readonly_failure_category(result['text'])}", flush=True)
                print(f"worker_{mode}_smoke_reply={safe_smoke_reply(result['text'])}", flush=True)
                failed.append(mode)
        except Exception:
            print(f"worker_{mode}_smoke_failed category=rpc_failed", flush=True)
            failed.append(mode)
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
    if failed:
        raise RuntimeError("command smoke failed: " + ",".join(failed))


if __name__ == "__main__":
    main()
