from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import unittest

from messenger_codex.bridge import Bridge


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
        bridge.progress(0, 123, 123, "Сообщение принято …")
        bridge.progress(0, 123, 123, "Задача выполняется …")
        bridge.progress(0, 123, 123, "Готово.", final=True)
        self.assertEqual(len(messenger.sent), 1)
        self.assertTrue(messenger.sent[0][1].startswith("`[-]`"))
        self.assertEqual(len(messenger.edited), 2)
        self.assertTrue(messenger.edited[0][2].startswith("`[\\]`"))
        self.assertEqual(messenger.edited[1][2], "`[+]` Готово.")


if __name__ == "__main__":
    unittest.main()
