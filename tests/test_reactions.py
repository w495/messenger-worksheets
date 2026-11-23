from __future__ import annotations

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "messenger-client"))
from client import MessengerUserClient  # noqa: E402


class Preview:
    def to_dict(self):
        return {"fingerprint": "reaction-fingerprint"}


class ReactionTest(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(MessengerUserClient)
        self.client._preview = lambda operation, request: Preview()
        self.client._confirmed = lambda preview, fingerprint: fingerprint == "reaction-fingerprint"
        self.payloads = []
        self.client.fanout = self._fanout

    def _fanout(self, operation, payload):
        self.payloads.append((operation, payload))
        return {"CommitStatus": "FullyCommitted"}

    def test_eyes_add_and_checkmark_replace_wire_payloads(self):
        chat = "0/0/00000000-0000-0000-0000-000000000000"
        timestamp = 1795460000000000
        preview = self.client.set_reaction(chat, timestamp, "👀")
        self.assertEqual(preview["fingerprint"], "reaction-fingerprint")
        eyes = self.client.set_reaction(
            chat, timestamp, "👀", confirm_fingerprint="reaction-fingerprint",
        )
        self.assertEqual(eyes["outcome"], "confirmed")
        check = self.client.set_reaction(
            chat, timestamp, "✅", operation="replace",
            confirm_fingerprint="reaction-fingerprint",
        )
        self.assertEqual(check["outcome"], "confirmed")
        self.assertEqual(self.payloads, [
            ("push", {"ClientMessage": {"Reaction": {
                "ChatId": chat, "Timestamp": timestamp, "Action": 0, "Type": 100110,
            }}, "UserIp": "::1"}),
            ("push", {"ClientMessage": {"Reaction": {
                "ChatId": chat, "Timestamp": timestamp, "Action": 2, "Type": 100101,
            }}, "UserIp": "::1"}),
        ])


if __name__ == "__main__":
    unittest.main()
