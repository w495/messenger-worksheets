#!/usr/bin/env python3
"""Read-only verification of an official Yandex Messenger Team bot."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from messenger_worksheets.bot_api import BotApiClient


def read_env(path: Path) -> dict[str, str]:
    """Прочитать root-owned env-файл Team-бота без исполнения его содержимого.

    Параметры:
        path: Путь к конфигурации в формате ``KEY=VALUE``.

    Результат:
        Словарь строковых параметров, включая секреты только в памяти процесса.

    Побочные эффекты:
        Читает указанный файл; значения не печатаются и не журналируются.

    Инварианты безопасности:
        Строки не передаются shell и не интерпретируются как команды.
    """
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def main() -> None:
    """Проверить identity и членство Team-бота без отправки сообщений.

    Исключения:
        Завершает работу ошибкой при несовпадении имени, организации или чатов.

    Побочные эффекты:
        Выполняет только read-only HTTPS-запросы Bot API и печатает безопасное
        подтверждение без OAuth-токена.

    Инварианты безопасности:
        Секрет берётся из root-owned env и никогда не включается в вывод.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-file", type=Path, required=True)
    args = parser.parse_args()
    values = read_env(args.environment_file)
    token = values["WORKSHEET_BOT_TOKEN"]
    client = BotApiClient(
        token,
        auth_scheme=values.get("WORKSHEET_BOT_AUTH_SCHEME", "OAuth"),
        base_url=values.get(
            "WORKSHEET_BOT_BASE_URL", "https://botapi.messenger.yandex.net/bot/v1/",
        ),
    )
    identity = client.identity()
    expected = values["WORKSHEET_BOT_EXPECTED_NAME"]
    if identity.get("display_name") != expected:
        raise ValueError("Team bot display name mismatch")
    expected_organization = values.get("WORKSHEET_BOT_EXPECTED_ORGANIZATION_ID")
    if (expected_organization is not None
            and int(expected_organization) not in (identity.get("organizations") or [])):
        raise ValueError("Team bot organization mismatch")
    chats = {chat.get("id") for chat in client.chats() if isinstance(chat, dict)}
    required = {
        values["WORKSHEET_BOT_API_CHAT_ID"],
        values["WORKSHEET_BOT_SANDBOX_CHAT_ID"],
    }
    missing = required - chats
    if missing:
        raise ValueError("Team bot is not a member of every configured chat")
    print(
        f"bot_identity_ok login={identity['login']} display_name={identity.get('display_name')} "
        f"configured_chats={len(required)}",
    )


if __name__ == "__main__":
    main()
