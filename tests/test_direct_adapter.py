from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from messenger_worksheets import codex_worker


class FakeResponse(BytesIO):
    def __enter__(self):
        """Возвращает Responses API дубль как контекстный менеджер."""
        return self

    def __exit__(self, *args):
        """Завершает контекст Responses API дубля без подавления исключений."""
        self.close()


class FakeOpener:
    def __init__(self):
        """Создаёт opener-дубль с последовательностью ответов модели."""
        self.requests = []

    def open(self, request, timeout):
        """Записывает запрос модели и возвращает следующий подготовленный ответ."""
        self.requests.append(json.loads(request.data))
        payload = {
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "DIRECT_OK"}],
            }],
        }
        return FakeResponse(json.dumps(payload).encode())


class DirectAdapterTest(unittest.TestCase):
    def setUp(self):
        """Создаёт приватное состояние и настраивает direct Responses provider."""
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.opener = FakeOpener()
        self.original_opener = codex_worker.build_opener
        codex_worker.build_opener = lambda *args: self.opener
        os.environ.update(
            WORKSHEET_CODEX_PROVIDER="responses_direct",
            WORKSHEET_CODEX_MODEL="gpt-5.3-codex",
            WORKSHEET_CODEX_REASONING_EFFORT="high",
            WORKSHEET_CODEX_API_KEY="test-only-key",
        )

    def tearDown(self):
        """Восстанавливает окружение после сценария direct adapter."""
        codex_worker.build_opener = self.original_opener
        self.temp.cleanup()

    def test_new_resume_and_replay(self):
        """Проверяет создание, продолжение и идемпотентный replay прямой сессии."""
        request = codex_worker.Request("hello", False, None, ())
        job_id = str(uuid.uuid4())
        first = codex_worker.execute(request, self.state, job_id=job_id)
        self.assertEqual(first["text"], "DIRECT_OK")
        uuid.UUID(first["session"])
        self.assertEqual(len(self.opener.requests), 1)

        replay = codex_worker.execute(request, self.state, job_id=job_id)
        self.assertEqual(replay, first)
        self.assertEqual(len(self.opener.requests), 1)

        resumed = codex_worker.execute(
            codex_worker.Request("again", False, first["session"], ()),
            self.state,
            job_id=str(uuid.uuid4()),
        )
        self.assertEqual(resumed["session"], first["session"])
        self.assertEqual(len(self.opener.requests), 2)
        transcript = self.opener.requests[-1]["input"][0]["content"][0]["text"]
        self.assertIn("[ASSISTANT]\nDIRECT_OK", transcript)
        self.assertEqual((self.state / "direct-sessions.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_image_and_task_contracts(self):
        """Проверяет data URL изображения и запрет файловых задач в direct-режиме."""
        image = (".png", b"\x89PNG\r\n\x1a\ncontent")
        outcome = codex_worker.execute(
            codex_worker.Request("see image", False, None, (image,)),
            self.state,
            job_id=str(uuid.uuid4()),
        )
        self.assertEqual(outcome["files"], [])
        content = self.opener.requests[-1]["input"][0]["content"]
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))

        calls = len(self.opener.requests)
        task = codex_worker.execute(
            codex_worker.Request("make file", True, None, ()),
            self.state,
            job_id=str(uuid.uuid4()),
        )
        self.assertIsNone(task["session"])
        self.assertEqual(task["files"], [])
        self.assertEqual(len(self.opener.requests), calls)


if __name__ == "__main__":
    unittest.main()
