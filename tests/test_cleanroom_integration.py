from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from messenger_codex.bridge import Messenger, message_prompt
from messenger_user_client import MessengerError, render_message


os.environ.setdefault(
    "WORKSHEET_MESSENGER_CHAT_ID",
    "0/0/00000000-0000-0000-0000-000000000000",
)
os.environ.setdefault("WORKSHEET_MESSENGER_EMAIL", "user@example.test")


class CleanroomDownloadClient:
    def download_media(self, file_id, *, target, max_bytes, confirm_fingerprint=None):
        """Имитирует preview/confirm скачивания cleanroom-медиа в локальный файл."""
        if confirm_fingerprint is None:
            return {"fingerprint": "download-fingerprint"}
        Path(target).write_bytes(b"\x89PNG\r\n\x1a\ncontent")
        return {"outcome": "confirmed"}


class CleanroomSelectorClient:
    options = None

    def __init__(self, token, **options):
        """Записывает параметры выбора cleanroom-аккаунта."""
        type(self).options = options

    def identity(self):
        """Возвращает идентичность с неверным email для fail-closed проверки."""
        return {"guid": "00000000-0000-0000-0000-000000000099"}


class CleanroomIntegrationTest(unittest.TestCase):
    def test_normalized_document_is_accepted_by_prompt_builder(self):
        """Проверяет принятие нормализованного документа построителем промпта."""
        message = render_message({
            "ServerMessage": {
                "ClientMessage": {"Plain": {"MiscFile": {"FileInfo": {
                    "Id2": "file-id", "Name": "report.txt", "Size": 5,
                }}}},
                "ServerMessageInfo": {"Timestamp": 10},
            }
        })
        prompt = json.loads(message_prompt(message, 0))
        self.assertEqual(prompt["attachment_types"], ["document"])

    def test_normalized_gallery_is_downloaded_without_legacy_import(self):
        """Проверяет скачивание нормализованной галереи без legacy-клиента."""
        messenger = object.__new__(Messenger)
        messenger.client = CleanroomDownloadClient()
        messenger.client_error = MessengerError
        with tempfile.TemporaryDirectory() as directory:
            paths = messenger.download_images(
                [{"type": "gallery", "file_id": "image-id", "size": 15}],
                Path(directory),
            )
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].suffix, ".png")

    def test_personal_messenger_cleanroom_selector_is_fail_closed_by_email(self):
        """Проверяет fail-closed выбор cleanroom-клиента при несовпадении email."""
        import messenger_user_client
        from messenger_worksheets import personal_messenger

        with mock.patch.object(personal_messenger, "BACKEND", "cleanroom"), \
                mock.patch.object(
                    messenger_user_client, "MessengerUserClient", CleanroomSelectorClient,
                ), mock.patch.dict(
                    os.environ, {"WORKSHEET_MESSENGER_TOKEN": "test-token"}, clear=False,
                ):
            messenger = personal_messenger.PersonalMessenger()
        self.assertEqual(
            CleanroomSelectorClient.options,
            {
                "account": "personal",
                "expected_email": personal_messenger.EMAIL,
                "enforce_account_class": False,
                "timeout": 12,
            },
        )
        self.assertIs(messenger.client_error, MessengerError)


if __name__ == "__main__":
    unittest.main()
