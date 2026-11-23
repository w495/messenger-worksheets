from __future__ import annotations

import os
import unittest

from messenger_codex.bridge import Bridge


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


if __name__ == "__main__":
    unittest.main()
