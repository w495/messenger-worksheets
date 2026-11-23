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


class PersonalIdentityError(ValueError):
    """The provider did not confirm the required primary account."""


class PersonalMessenger(Messenger):
    def __init__(self) -> None:
        client_dir = Path(os.environ["WORKSHEET_MESSENGER_CLIENT_DIR"]).resolve(strict=True)
        sys.path.insert(0, str(client_dir))
        from client import MessengerUserClient, render_message, thread_chat_id

        token = os.environ["WORKSHEET_MESSENGER_TOKEN"]
        self.client = MessengerUserClient(
            token, account="personal", expected_email=EMAIL, timeout=12,
        )
        self.identity = self.client.identity()
        self.chat = CHAT
        self.render = render_message
        self.thread_chat_id = thread_chat_id
