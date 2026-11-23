from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from messenger_codex.bridge import Bridge, Messenger


class FakeMessenger:
    chat = "0/0/00000000-0000-0000-0000-000000000000"

    def __init__(self):
        self.sent = []
        self.edited = []

    def send(self, parent, text, payload):
        self.sent.append((parent, text, payload))
        return 777

    def edit(self, parent, timestamp, text):
        self.edited.append((parent, timestamp, text))


class BoundaryHistoryClient:
    def fanout(self, operation, payload):
        chat = payload["ChatId"]
        return {
            "Chats": [{
                "ChatId": chat,
                "Messages": [
                    {"ServerMessage": {"ServerMessageInfo": {"Timestamp": 99}}},
                    {"ServerMessage": {"ServerMessageInfo": {"Timestamp": 100}}},
                    {"ServerMessage": {"ServerMessageInfo": {"Timestamp": 101}}},
                ],
            }],
        }


class ThreadRoutingTest(unittest.TestCase):
    def setUp(self):
        self.bridge = object.__new__(Bridge)

    def tearDown(self):
        os.environ.pop("WORKSHEET_REPLY_IN_THREADS", None)

    def test_main_chat_reply_uses_incoming_message_as_thread_parent(self):
        os.environ["WORKSHEET_REPLY_IN_THREADS"] = "true"
        self.assertEqual(self.bridge._delivery_parent(0, 123456), 123456)

    def test_existing_thread_keeps_original_parent(self):
        os.environ["WORKSHEET_REPLY_IN_THREADS"] = "true"
        self.assertEqual(self.bridge._delivery_parent(111, 222), 111)

    def test_disabled_thread_routing_keeps_main_chat(self):
        self.assertEqual(self.bridge._delivery_parent(0, 123456), 0)

    def test_progress_edits_one_message_and_finishes(self):
        database = sqlite3.connect(":memory:")
        messenger = FakeMessenger()
        bridge = Bridge(database, messenger, Path("/tmp"))
        with patch("messenger_codex.bridge.secrets.randbelow", return_value=0):
            bridge.progress(0, 123, 123, "Сообщение принято …")
            bridge.progress(0, 123, 123, "Задача выполняется …")
            bridge.progress(0, 123, 123, "Готово.", final=True)
        self.assertEqual(len(messenger.sent), 1)
        self.assertTrue(messenger.sent[0][1].startswith("`-`"))
        self.assertEqual(len(messenger.edited), 2)
        self.assertTrue(messenger.edited[0][2].startswith("`\\`"))
        self.assertEqual(messenger.edited[1][2], "`[+]` Готово.")
        self.assertTrue(bridge._is_our_reply({"payload_id": messenger.sent[0][2]}))

    def test_history_skips_inclusive_cursor_boundaries(self):
        messenger = object.__new__(Messenger)
        messenger.chat = "0/0/00000000-0000-0000-0000-000000000000"
        messenger.client = BoundaryHistoryClient()
        messenger.thread_chat_id = lambda chat, parent: chat
        messenger.render = lambda raw: {"text": "message"}
        messages = messenger.new_messages(parent=0, after=100)
        self.assertEqual([stamp for stamp, _ in messages], [101])


if __name__ == "__main__":
    unittest.main()
