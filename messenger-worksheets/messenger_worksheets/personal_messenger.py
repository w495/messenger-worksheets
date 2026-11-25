"""Bind the new gateway to exactly one personal account and one chat."""

from __future__ import annotations

import os

from messenger_codex.bridge import Messenger, valid_chat


CHAT = valid_chat(os.environ["WORKSHEET_MESSENGER_CHAT_ID"])
EMAIL = os.environ["WORKSHEET_MESSENGER_EMAIL"].strip().casefold()
if "@" not in EMAIL or len(EMAIL) > 254:
    raise ValueError("invalid expected Messenger email")
BACKEND = os.environ.get("WORKSHEET_MESSENGER_BACKEND", "cleanroom")
if BACKEND != "cleanroom":
    raise ValueError("WORKSHEET_MESSENGER_BACKEND must be cleanroom")


class PersonalIdentityError(ValueError):
    """The provider did not confirm the required primary account."""


class PersonalMessenger(Messenger):
    def __init__(self) -> None:
        """Привязать собственный клиент к настроенным EMAIL и CHAT.

        Читает OAuth из окружения, затем проверяет личность через сеть.
        Несовпадение email блокирует работу до чтения сообщений. Произвольные
        модули и внешние каталоги клиента не загружаются.

        Исключения конфигурации и проверки личности передаются вызывающему
        коду; токен не включается в диагностику.
        """
        super().__init__(CHAT)
