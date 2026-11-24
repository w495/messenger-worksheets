from __future__ import annotations

import json
from concurrent.futures import Future
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
import uuid

from messenger_codex.bridge import Bridge, CodexTurnError, codex_answer, model_failure

os.environ.setdefault("WORKSHEET_MESSENGER_CHAT_ID", "0/0/00000000-0000-0000-0000-000000000000")
os.environ.setdefault("WORKSHEET_MESSENGER_EMAIL", "user@example.test")

from messenger_worksheets.cloud_gateway import CloudBridge
from messenger_worksheets.bot_gateway import BotCloudBridge


class GatewayFailureTest(unittest.TestCase):
    def setUp(self):
        """Перехватить служебные логи без засорения вывода параметризованных тестов."""
        self.output = StringIO()
        self.enterContext(redirect_stdout(self.output))

    def bridge(self, gateway=Bridge):
        """Создать проверяемый общий мост без сети, модели и постоянных файлов."""
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        messenger = Mock()
        messenger.chat = "0/0/00000000-0000-0000-0000-000000000000"
        messenger.identity = {"guid": "service"}
        messenger.send.return_value = 777
        bridge = object.__new__(gateway)
        Bridge.__init__(bridge, database, messenger, Path("/tmp/codex"))
        bridge._draft = Mock(return_value=("OK", "session"))
        bridge._task = Mock(return_value=("OK", ()))
        return bridge

    def test_all_model_paths_finish_errors_and_advance_cursor(self):
        """HTTP, timeout, connection и прочие model failures завершают оба режима во всех шлюзах."""
        errors = (
            HTTPError("https://worker.invalid/", 422, "PRIVATE_DIAGNOSTIC", {}, None),
            HTTPError("https://worker.invalid/", 503, "PRIVATE_DIAGNOSTIC", {}, None),
            HTTPError("https://worker.invalid/", 504, "PRIVATE_DIAGNOSTIC", {}, None),
            URLError("PRIVATE_DIAGNOSTIC"),
            TimeoutError("PRIVATE_DIAGNOSTIC"),
            subprocess.TimeoutExpired("PRIVATE_DIAGNOSTIC", 1800),
            ValueError("PRIVATE_DIAGNOSTIC"),
            RuntimeError("PRIVATE_DIAGNOSTIC"),
            CodexTurnError("worker_http_503"),
        )
        for gateway in (Bridge, CloudBridge, BotCloudBridge):
            for task in (False, True):
                for error in errors:
                    with self.subTest(gateway=gateway.__name__, task=task, error=type(error).__name__):
                        bridge = self.bridge(gateway)
                        operation = bridge._task if task else bridge._draft
                        operation.side_effect = error
                        text = "/task hello" if task else "hello"
                        with patch.dict(os.environ, {"WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true"}):
                            bridge._respond(0, 123, "prompt", {"text": text}, "old-session")
                        operation.assert_called_once()
                        bridge.messenger.edit.assert_called_once_with(0, 777, "❌ Ошибка.")
                        self.assertEqual(bridge.messenger.react.call_args.args, (0, 123, "💔"))
                        self.assertTrue(bridge.messenger.react.call_args.kwargs["replace"])
                        self.assertEqual(
                            bridge.db.execute("SELECT failed,draft FROM replies").fetchone(), (1, None),
                        )
                        self.assertEqual(
                            bridge.db.execute("SELECT cursor FROM feeds WHERE parent=0").fetchone()[0], 123,
                        )
                        reply = bridge.messenger.send.call_args.args[1]
                        self.assertIn("не получен", reply)
                        self.assertNotIn("PRIVATE_DIAGNOSTIC", reply)
                        self.assertNotIn("PRIVATE_DIAGNOSTIC", self.output.getvalue())

    def test_finished_future_timeout_reaches_terminal_handler(self):
        """TimeoutError завершившегося future не превращается в бесконечное ожидание progress."""
        for gateway, module in (
            (CloudBridge, "messenger_worksheets.cloud_gateway"),
            (BotCloudBridge, "messenger_worksheets.bot_gateway"),
        ):
            with self.subTest(gateway=gateway.__name__):
                bridge = self.bridge(gateway)
                del bridge._task
                bridge.task_url = "https://127.0.0.1:8445/v1/codex-worksheet-jobs"
                bridge.task_token = "test-token"
                bridge.task_ca = "/test/ca.crt"
                future = Future()
                future.set_exception(TimeoutError("PRIVATE_DIAGNOSTIC"))
                executor = Mock()
                executor.__enter__ = Mock(return_value=executor)
                executor.__exit__ = Mock(return_value=False)
                executor.submit.return_value = future
                with patch(module + ".ThreadPoolExecutor", return_value=executor), \
                        patch.dict(os.environ, {"WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true"}):
                    bridge._respond(0, 123, "prompt", {"text": "/task hello"}, None)
                self.assertEqual(bridge.messenger.edit.call_args.args[2], "❌ Ошибка.")
                self.assertEqual(bridge.db.execute("SELECT failed FROM replies").fetchone()[0], 1)

    def test_failure_reply_delivery_retry_does_not_repeat_model(self):
        """Сохранённый ошибочный ответ доставляется повторно без нового model call."""
        bridge = self.bridge()
        bridge._draft.side_effect = HTTPError("https://worker.invalid/", 422, "", {}, None)
        bridge.messenger.send.side_effect = [777, RuntimeError("delivery unavailable"), 888]
        with self.assertRaisesRegex(RuntimeError, "delivery unavailable"):
            bridge._respond(0, 123, "prompt", {"text": "hello"}, "session")
        self.assertEqual(bridge.db.execute("SELECT failed FROM replies").fetchone()[0], 1)
        bridge._respond(0, 123, "prompt", {"text": "hello"}, "session")
        bridge._draft.assert_called_once()
        self.assertEqual(bridge.db.execute("SELECT draft FROM replies").fetchone()[0], None)
        self.assertEqual(bridge.messenger.edit.call_args.args[2], "❌ Ошибка.")

    def test_poll_continues_after_failed_message_without_retrying_it(self):
        """После ошибки первого сообщения очередь переходит к следующему и не крутит старый UUID."""
        bridge = self.bridge()
        started = bridge.started
        messages = [
            (started + 1, {"text": "first"}),
            (started + 2, {"text": "second"}),
        ]
        bridge.messenger.new_messages.side_effect = (
            lambda *, parent, after: [item for item in messages if item[0] > after]
        )
        bridge._draft.side_effect = [
            HTTPError("https://worker.invalid/", 422, "", {}, None), ("OK", "next-session"),
        ]
        bridge._poll_feed(0, started, "old-session")
        self.assertEqual(bridge._draft.call_count, 2)
        self.assertEqual(
            bridge.db.execute("SELECT cursor,session FROM feeds WHERE parent=0").fetchone(),
            (started + 2, "next-session"),
        )
        bridge._poll_feed(0, started + 2, "next-session")
        self.assertEqual(bridge._draft.call_count, 2)
        self.assertEqual(
            bridge.db.execute("SELECT failed FROM replies ORDER BY incoming").fetchall(), [(1,), (0,)],
        )

    def test_error_projection_never_echoes_untrusted_exception_text(self):
        """Сообщения об ошибке не содержат URL, token-like text и raw stderr из исключения."""
        reason, text = model_failure(CodexTurnError("OAuth PRIVATE_DIAGNOSTIC"))
        self.assertEqual(reason, "model_error")
        self.assertNotIn("PRIVATE_DIAGNOSTIC", text)
        for error in (TimeoutError(), HTTPError("https://worker.invalid/", 504, "", {}, None)):
            self.assertIn("Время", model_failure(error)[1])

    def test_explicit_task_command_accepts_markdown_formatting(self):
        """Команды /task с Markdown-выделением или после комментария выбирают task."""
        bridge = self.bridge()
        with patch.dict(os.environ, {"WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true"}):
            for text in ("/task\nhello", "**/task**\nhello", "__/task__\nhello",
                         "`/task` hello", "*/task* hello", "# note\n**/task**\nhello"):
                with self.subTest(text=text):
                    self.assertTrue(bridge._is_owner_task({"text": text}))
            for text in ("/taskforce hello", "**/taskforce** hello", "# **/task** hello"):
                with self.subTest(text=text):
                    self.assertFalse(bridge._is_owner_task({"text": text}))

    def test_legacy_codex_fallback_shares_thirty_minute_deadline(self):
        """Повтор старой CLI-сессии получает остаток бюджета вместо новых 30 минут."""
        session = str(uuid.uuid4())
        stdout = "\n".join(json.dumps(event) for event in (
            {"type": "thread.started", "thread_id": session},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
            {"type": "turn.completed"},
        ))
        results = [
            subprocess.CompletedProcess([], 1, "", "ModelProviderNotConfigured"),
            subprocess.CompletedProcess([], 0, stdout, ""),
        ]
        with patch("messenger_codex.bridge.time.monotonic", side_effect=[0, 0, 1200]), \
                patch("messenger_codex.bridge.subprocess.run", side_effect=results) as run:
            text, _ = codex_answer(Path("/tmp"), "hello", session)
        self.assertEqual(text, "OK")
        self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [1800, 600])


if __name__ == "__main__":
    unittest.main()
