"""Bind the new gateway to exactly one personal account and one chat."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from messenger_codex.bridge import Messenger


CHAT = "0/0/00000000-0000-0000-0000-000000000000"
EMAIL = "user@example.test"


class PersonalIdentityError(ValueError):
    """The provider did not confirm the required primary account."""


class PersonalMessenger(Messenger):
    def __init__(self) -> None:
        client_dir = Path(os.environ["WORKSHEET_MESSENGER_CLIENT_DIR"]).resolve(strict=True)
        sys.path.insert(0, str(client_dir))
        from client import MessengerUserClient, render_message, thread_chat_id

        token = os.environ["WORKSHEET_MESSENGER_TOKEN"]
        # Membership in an organization can also be returned for a personal address.
        probe = MessengerUserClient(token, account="personal", timeout=12)
        subject = probe.registry("request_user", {}).get("user")
        if not isinstance(subject, dict):
            raise PersonalIdentityError("Мессенджер не вернул владельца токена.")
        email = subject.get("email")
        if not isinstance(email, str) or email.strip().casefold() != EMAIL:
            raise PersonalIdentityError(f"Поле email не подтвердило {EMAIL}.")
        uid = subject.get("uid")
        if isinstance(uid, bool) or not isinstance(uid, (int, str)) or not str(uid).strip():
            raise PersonalIdentityError("Мессенджер не вернул идентификатор владельца токена.")
        self.client = MessengerUserClient(
            token, account="personal", expected_email=EMAIL, expected_uid=str(uid),
            enforce_account_class=False, timeout=12,
        )
        self.identity = self.client.identity()
        self.chat = CHAT
        self.render = render_message
        self.thread_chat_id = thread_chat_id
