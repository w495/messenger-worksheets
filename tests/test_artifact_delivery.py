from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from messenger_codex.bridge import Messenger


class FakeClient:
    def __init__(self):
        self.operations = []

    def send_image_with_caption(self, **kwargs):
        self.operations.append(("image", kwargs))
        if "confirm_fingerprint" not in kwargs:
            return {"fingerprint": "image-fingerprint"}
        return {"outcome": "confirmed"}

    def send_document(self, **kwargs):
        self.operations.append(("document", kwargs))
        if "confirm_fingerprint" not in kwargs:
            return {"fingerprint": "document-fingerprint"}
        return {"outcome": "confirmed"}


class RejectingImageClient(FakeClient):
    def send_image_with_caption(self, **kwargs):
        self.operations.append(("image", kwargs))
        raise ValueError("invalid test PNG")


class MissingTimestampClient:
    guid = "00000000-0000-0000-0000-000000000099"

    def __init__(self):
        self.history_calls = 0

    def send_message(self, text, **kwargs):
        if "confirm_fingerprint" not in kwargs:
            return {"fingerprint": "message-fingerprint"}
        return {"outcome": "confirmed", "chat_id": kwargs["chat_id"], "message_info": None}

    def identity(self):
        return {"guid": self.guid}

    def read_history(self, chat_id, limit):
        self.history_calls += 1
        if self.history_calls < 3:
            return {"messages": []}
        return {"messages": [{
            "payload_id": "00000000-0000-0000-0000-000000000004",
            "author_guid": self.guid,
            "ts": 987654,
        }]}


class ArtifactDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.messenger = object.__new__(Messenger)
        self.messenger.chat = "0/0/00000000-0000-0000-0000-000000000000"
        self.messenger.thread_chat_id = lambda chat, parent: chat
        self.messenger.client = FakeClient()

    def tearDown(self):
        self.temp.cleanup()

    def test_png_uses_native_image(self):
        image = self.root / "result.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
        self.messenger.send_file(0, image, "00000000-0000-0000-0000-000000000001", self.root)
        self.assertEqual(
            [kind for kind, _ in self.messenger.client.operations],
            ["image", "image", "document", "document"],
        )

    def test_non_png_uses_document(self):
        document = self.root / "result.txt"
        document.write_text("ok", encoding="utf-8")
        self.messenger.send_file(0, document, "00000000-0000-0000-0000-000000000002", self.root)
        self.assertEqual([kind for kind, _ in self.messenger.client.operations], ["document", "document"])

    def test_invalid_png_falls_back_to_document(self):
        image = self.root / "broken.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-png")
        self.messenger.client = RejectingImageClient()
        self.messenger.send_file(0, image, "00000000-0000-0000-0000-000000000003", self.root)
        self.assertEqual(
            [kind for kind, _ in self.messenger.client.operations],
            ["image", "document", "document"],
        )

    def test_send_resolves_missing_timestamp_from_history(self):
        self.messenger.client = MissingTimestampClient()
        timestamp = self.messenger.send(
            0,
            "status",
            "00000000-0000-0000-0000-000000000004",
        )
        self.assertEqual(timestamp, 987654)
        self.assertEqual(self.messenger.client.history_calls, 3)


if __name__ == "__main__":
    unittest.main()
