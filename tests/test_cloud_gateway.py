from __future__ import annotations

import os
import unittest
from unittest.mock import patch
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
        """Проверяет преобразование HTTP-ошибки worker в ограниченную категорию CodexTurnError."""
        bridge = object.__new__(CloudBridge)
        bridge.progress = lambda *args, **kwargs: None

        def fail(*args, **kwargs):
            """Имитирует HTTP 503 при передаче задачи worker."""
            raise HTTPError("https://127.0.0.1/", 503, "unavailable", {}, None)

        bridge.transfer = fail
        with self.assertRaisesRegex(CodexTurnError, "worker_http_503"):
            bridge._task(0, 123, {"text": "task"}, delivery_parent=123)

    def test_transfer_passes_profile_specific_ca_to_rpc(self):
        """Проверяет pin выбранной CA при обращении к отдельному worker route."""
        bridge = object.__new__(CloudBridge)
        bridge.messenger = type("Messenger", (), {"download_images": lambda *args: ()})()
        expected = {"text": "ok", "session": None, "files": []}
        with patch(
            "messenger_worksheets.cloud_gateway.submit", return_value=expected,
        ) as submit:
            result = bridge.transfer(
                0, 123, {"text": "task", "media": []}, task=True, session=None,
                worker_url="https://127.0.0.1:8445/v1/codex-worksheet-jobs",
                transport_token="test-token", ca_file="/test/ca-sandbox.crt",
            )
        self.assertEqual(result, expected)
        self.assertEqual(submit.call_args.kwargs["ca_file"], "/test/ca-sandbox.crt")


if __name__ == "__main__":
    unittest.main()
