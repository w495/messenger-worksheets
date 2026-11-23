from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from messenger_codex.bridge import Bridge, CodexTurnError, Messenger, PROGRESS_SECONDS


class FakeMessenger:
    chat = "0/0/00000000-0000-0000-0000-000000000000"
    identity = {"guid": "00000000-0000-0000-0000-000000000099"}

    def __init__(self):
        self.sent = []
        self.edited = []
        self.reactions = []

    def send(self, parent, text, payload):
        self.sent.append((parent, text, payload))
        return 777

    def edit(self, parent, timestamp, text):
        self.edited.append((parent, timestamp, text))

    def react(self, parent, timestamp, reaction, *, replace=False, remove=False):
        self.reactions.append((parent, timestamp, reaction, replace, remove))


class ThreadDiscoveryMessenger(FakeMessenger):
    def new_messages(self, *, parent, after):
        self.discovery_call = (parent, after)
        return [
            (101, {"thread_exists": True, "deleted": True}),
            (102, {"thread_exists": True, "deleted": False}),
            (103, {"thread_exists": False, "deleted": False}),
        ]


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

    def test_spinner_updates_every_100_milliseconds(self):
        self.assertEqual(PROGRESS_SECONDS, 0.1)

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
        self.assertEqual(messenger.edited[1][2], "✅ Готово.")
        self.assertTrue(bridge._is_our_reply({"payload_id": messenger.sent[0][2]}))

    def test_own_attachment_is_ignored_even_with_derived_payload(self):
        database = sqlite3.connect(":memory:")
        messenger = FakeMessenger()
        bridge = Bridge(database, messenger, Path("/tmp"))
        self.assertTrue(bridge._is_our_reply({
            "author_guid": messenger.identity["guid"],
            "payload_id": "00000000-0000-0000-0000-000000000098",
            "media": [{"kind": "miscfile"}],
        }))

    def test_failed_task_finishes_with_error_and_removes_eyes(self):
        database = sqlite3.connect(":memory:")
        messenger = FakeMessenger()
        bridge = Bridge(database, messenger, Path("/tmp"))
        bridge._is_owner_task = lambda message: True

        def fail_task(*args, **kwargs):
            raise CodexTurnError("worker_http_503")

        bridge._task = fail_task
        bridge._respond(0, 123, "prompt", {"text": "создай файл"}, None)
        self.assertEqual(messenger.edited[-1][2], "❌ Ошибка.")
        self.assertEqual(messenger.reactions, [
            (0, 123, "👀", False, False),
            (0, 123, "👀", False, True),
        ])

    def test_history_skips_inclusive_cursor_boundaries(self):
        messenger = object.__new__(Messenger)
        messenger.chat = "0/0/00000000-0000-0000-0000-000000000000"
        messenger.client = BoundaryHistoryClient()
        messenger.thread_chat_id = lambda chat, parent: chat
        messenger.render = lambda raw: {"text": "message"}
        messages = messenger.new_messages(parent=0, after=100)
        self.assertEqual([stamp for stamp, _ in messages], [101])

    def test_thread_discovery_removes_deleted_parent_feeds(self):
        database = sqlite3.connect(":memory:")
        messenger = ThreadDiscoveryMessenger()
        bridge = Bridge(database, messenger, Path("/tmp"))
        with database:
            database.execute("INSERT INTO feeds VALUES (101, 999, NULL)")
        bridge._discover_threads()
        self.assertEqual(messenger.discovery_call, (0, bridge.started))
        feeds = database.execute("SELECT parent, cursor FROM feeds ORDER BY parent").fetchall()
        self.assertEqual(feeds, [(0, bridge.started), (102, 102)])


if __name__ == "__main__":
    unittest.main()
