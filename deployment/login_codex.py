#!/usr/bin/env python3
"""Log the isolated worker into Codex using the root-only API key."""

from __future__ import annotations

import os
from pathlib import Path
import pwd
import signal
import subprocess


ENV_FILE = Path("/etc/messenger-worksheets/worker.env")
STATE_DIR = Path("/var/lib/messenger-worksheets-worker")
REQUIRED = {"WORKSHEET_CODEX_API_KEY", "WORKSHEET_CODEX_BINARY"}


def load_worker_environment() -> dict[str, str]:
    """Извлечь API key и Codex binary из root-owned worker env.

    Результат:
        Словарь ровно двух обязательных параметров.

    Исключения:
        ``RuntimeError`` при отсутствии любого обязательного поля.

    Инварианты безопасности:
        Файл не исполняется, неизвестные поля и комментарии игнорируются.
    """
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key in REQUIRED:
            values[key] = value
    if values.keys() != REQUIRED:
        raise RuntimeError("worker environment is incomplete")
    return values


def worker_command(binary: str, *arguments: str, stdin: str | None = None) -> int:
    """Выполнить Codex-команду под системным worker без shell.

    Параметры:
        binary: Абсолютный путь к Codex.
        arguments: Аргументы CLI как отдельные строки.
        stdin: Необязательный секретный ввод, передаваемый через pipe.

    Результат:
        Код завершения либо 124 после контролируемого таймаута.

    Побочные эффекты:
        Запускает process group с пониженными uid/gid и при зависании убивает её.

    Инварианты безопасности:
        stdout/stderr не печатаются; секрет не попадает в argv или shell.
    """
    account = pwd.getpwnam("worksheet-worker")
    command = [
        "/usr/bin/setpriv",
        f"--reuid={account.pw_uid}",
        f"--regid={account.pw_gid}",
        "--clear-groups",
        "--",
        binary,
        *arguments,
    ]
    environment = {
        "HOME": str(STATE_DIR),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    process = subprocess.Popen(
        command,
        cwd=STATE_DIR,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        process.communicate(stdin, timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return 124
    return process.returncode


def main() -> None:
    """Авторизовать изолированный Codex worker API key и проверить статус.

    Исключения:
        Завершает работу ошибкой без root или при любом ненулевом exit code.

    Побочные эффекты:
        Передаёт API key через stdin официальному ``codex login`` и изменяет
        auth-state только системного worker HOME.

    Инварианты безопасности:
        Ключ не печатается, не передаётся аргументом и не сохраняется скриптом.
    """
    if os.geteuid() != 0:
        raise SystemExit("Run this login helper with sudo.")
    values = load_worker_environment()
    binary = values["WORKSHEET_CODEX_BINARY"]
    status = worker_command(
        binary,
        "login",
        "--with-api-key",
        stdin=values["WORKSHEET_CODEX_API_KEY"] + "\n",
    )
    if status != 0:
        raise SystemExit(f"codex_login=failed exit={status}")
    print("codex_login=ok")
    status = worker_command(binary, "login", "status")
    if status != 0:
        raise SystemExit(f"codex_login_status=failed exit={status}")
    print("codex_login_status=authenticated")


if __name__ == "__main__":
    main()
