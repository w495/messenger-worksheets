"""Bind the new gateway to exactly one personal account and one chat."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from messenger_codex.bridge import Messenger, valid_chat


CHAT = valid_chat(os.environ["WORKSHEET_MESSENGER_CHAT_ID"])
EMAIL = os.environ["WORKSHEET_MESSENGER_EMAIL"].strip().casefold()
if "@" not in EMAIL or len(EMAIL) > 254:
    raise ValueError("invalid expected Messenger email")
BACKEND = os.environ.get("WORKSHEET_MESSENGER_BACKEND", "cleanroom")
if BACKEND not in {"legacy", "cleanroom"}:
    raise ValueError("WORKSHEET_MESSENGER_BACKEND must be legacy or cleanroom")


class PersonalIdentityError(ValueError):
    """The provider did not confirm the required primary account."""


class PersonalMessenger(Messenger):
    def __init__(self) -> None:
        """Подключить выбранный backend только к ожидаемой персональной учётной записи и чату.

        Параметры: нет.
        Результат: Адаптер Messenger, привязанный к глобальным CHAT/EMAIL и выбранному backend.
        Исключения: KeyError/ImportError/FileNotFoundError конфигурации/backend; ошибки клиента identity, включая несовпадение expected_email.
        Побочные эффекты: Может resolve legacy dir и изменить sys.path; импортирует backend, читает token env, создаёт client и запрашивает identity.
        Безопасность: Backend ограничен module-level allowlist; expected_email передаётся клиенту fail-closed, token не журналируется и chat уже канонизирован."""
        if BACKEND == "legacy":
            client_dir = Path(os.environ["WORKSHEET_MESSENGER_CLIENT_DIR"]).resolve(strict=True)
            sys.path.insert(0, str(client_dir))
            from client import (
                MessengerError, MessengerUserClient, render_message, thread_chat_id,
            )
            client_options = {"account": "personal", "expected_email": EMAIL, "timeout": 12}
        else:
            from messenger_user_client import (
                MessengerError, MessengerUserClient, render_message, thread_chat_id,
            )
            client_options = {
                "account": "personal",
                "expected_email": EMAIL,
                "enforce_account_class": False,
                "timeout": 12,
            }

        token = os.environ["WORKSHEET_MESSENGER_TOKEN"]
        self.client = MessengerUserClient(token, **client_options)
        self.identity = self.client.identity()
        self.client_error = MessengerError
        self.chat = CHAT
        self.render = render_message
        self.thread_chat_id = thread_chat_id
