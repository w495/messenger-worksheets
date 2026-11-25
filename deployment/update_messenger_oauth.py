#!/usr/bin/env python3
"""Validate and atomically replace the Messenger OAuth without printing it."""

from __future__ import annotations

import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile


ETC = Path("/etc/messenger-worksheets")
TARGETS = (
    ETC / "gateway.env",
    ETC / "gateway-api.env",
    ETC / "gateway-local-user.env",
    ETC / "gateway-sandbox-admin.env",
)


def read_env(path: Path) -> dict[str, str]:
    """Прочитать env-файл как пары строк без shell-интерпретации.

    Параметры:
        path: Существующий root-owned gateway env.

    Результат:
        Словарь значений для атомарного обновления OAuth/email.

    Инварианты безопасности:
        Содержимое не печатается и не исполняется.
    """
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    """Атомарно заменить gateway env приватным root-owned файлом.

    Параметры:
        path: Цель в ``/etc/messenger-worksheets``.
        values: Полный набор параметров с новым OAuth.

    Исключения:
        Отклоняет CR/LF в значениях.

    Побочные эффекты:
        Создаёт mode 0600 temporary и заменяет цель через rename.

    Инварианты безопасности:
        Секрет не попадает в stdout и частично записанный файл не публикуется.
    """
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError("invalid environment value")
            stream.write(f"{key}={value}\n")
        temporary = Path(stream.name)
    try:
        os.chmod(temporary, 0o600)
        os.chown(temporary, 0, 0)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    """Проверить новый personal OAuth и обновить все существующие gateway env.

    Исключения:
        Отклоняет запуск без root, пустой token, неверную identity и config.

    Побочные эффекты:
        Скрыто читает OAuth, делает read-only identity probe и атомарно заменяет
        token/email в существующих env-файлах.

    Инварианты безопасности:
        Token не передаётся в argv и не печатается; запись начинается только
        после точного совпадения ожидаемого email.
    """
    if os.geteuid() != 0:
        raise SystemExit("Run this OAuth updater with sudo.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    expected_email = config.get("expected_email")
    backend = config.get("messenger_backend", "cleanroom")
    if not isinstance(expected_email, str):
        raise ValueError("instance config has no expected_email")
    if backend != "cleanroom":
        raise ValueError("messenger_backend must be cleanroom")
    token = getpass("Personal Yandex Messenger OAuth: ")
    if not token or any(char.isspace() for char in token):
        raise ValueError("OAuth is empty or contains whitespace")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "messenger-worksheets"))
    from messenger_user_client import MessengerUserClient
    client_options = {
        "account": "personal", "expected_email": expected_email,
        "enforce_account_class": False, "timeout": 12,
    }

    client = MessengerUserClient(token, **client_options)
    client.identity()
    for path in TARGETS:
        if not path.exists():
            continue
        values = read_env(path)
        values["WORKSHEET_MESSENGER_TOKEN"] = token
        values["WORKSHEET_MESSENGER_EMAIL"] = expected_email
        write_env(path, values)
    print(
        f"messenger_oauth_updated backend={backend} "
        f"expected_email={expected_email} account=personal"
    )


if __name__ == "__main__":
    main()
