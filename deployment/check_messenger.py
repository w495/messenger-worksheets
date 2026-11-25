#!/usr/bin/env python3
"""Verify the configured personal Messenger identity without sending messages."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


REQUIRED = {
    "WORKSHEET_MESSENGER_CHAT_ID",
    "WORKSHEET_MESSENGER_EMAIL",
    "WORKSHEET_MESSENGER_TOKEN",
}
OPTIONAL = {"WORKSHEET_MESSENGER_BACKEND"}


def load_gateway_environment(env_file: Path) -> None:
    """Загрузить только allowlisted поля gateway env в текущий процесс.

    Параметры:
        env_file: Root-owned файл выбранного Messenger gateway.

    Исключения:
        ``RuntimeError`` при отсутствии обязательного chat, email или OAuth.

    Побочные эффекты:
        Записывает разрешённые значения в ``os.environ`` текущего процесса.

    Инварианты безопасности:
        Не исполняет файл и игнорирует все ключи вне ``REQUIRED | OPTIONAL``.
    """
    found: set[str] = set()
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key in REQUIRED | OPTIONAL:
            os.environ[key] = value
            if key in REQUIRED:
                found.add(key)
    if found != REQUIRED:
        raise RuntimeError("gateway environment is incomplete")


def main() -> None:
    """Проверить точную Messenger identity и future-only polling без мутаций.

    Исключения:
        Отклоняет непривилегированный запуск, неполный env, неизвестный backend
        и любой неожиданный ответ будущей history-границы.

    Побочные эффекты:
        Выполняет read-only identity/history запросы и печатает backend, email
        и chat id; OAuth и ответы сервера не выводятся.

    Инварианты безопасности:
        Переключение ``--backend`` действует только внутри процесса проверки и
        не изменяет root-owned конфигурацию или runtime-state.
    """
    if os.geteuid() != 0:
        raise SystemExit("Run this identity check with sudo.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--backend", choices=("cleanroom",))
    args = parser.parse_args()
    load_gateway_environment(args.environment_file)
    if args.backend is not None:
        os.environ["WORKSHEET_MESSENGER_BACKEND"] = args.backend
    backend = os.environ.get("WORKSHEET_MESSENGER_BACKEND", "cleanroom")
    from messenger_codex.bridge import timestamp_now
    from messenger_worksheets.personal_messenger import CHAT, EMAIL, PersonalMessenger

    messenger = PersonalMessenger()
    future = timestamp_now() + 3_600_000_000
    if messenger.new_messages(parent=0, after=future):
        raise RuntimeError("future-only Messenger probe unexpectedly returned messages")
    print(f"messenger_identity_ok backend={backend} expected_email={EMAIL} chat_id={CHAT}")


if __name__ == "__main__":
    main()
