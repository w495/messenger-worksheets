from __future__ import annotations

import os
import unittest
from urllib.error import HTTPError

from messenger_codex.bridge import CodexTurnError

os.environ.setdefault(
    "WORKSHEET_MESSENGER_CHAT_ID",
    "0/0/00000000-0000-0000-0000-000000000000",
)
os.environ.setdefault("WORKSHEET_MESSENGER_EMAIL", "user@example.test")

from messenger_worksheets.cloud_gateway import CloudBridge


class CloudGatewayFailureTest(unittest.TestCase):
    def test_worker_http_error_becomes_bounded_codex_error(self):
        bridge = object.__new__(CloudBridge)
        bridge.progress = lambda *args, **kwargs: None

        def fail(*args, **kwargs):
            raise HTTPError("https://127.0.0.1/", 503, "unavailable", {}, None)

        bridge.transfer = fail
        with self.assertRaisesRegex(CodexTurnError, "worker_http_503"):
            bridge._task(0, 123, {"text": "task"}, delivery_parent=123)


if __name__ == "__main__":
    unittest.main()
