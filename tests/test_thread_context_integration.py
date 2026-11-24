"""Проверки сквозной памяти треда без Messenger-сети и настоящей модели."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from messenger_codex.bridge import Bridge, CodexTurnError, Messenger


class ContextBridge(Bridge):
    """Включить каноническую память у общего моста для офлайн-проверок."""

    uses_thread_context = True


class ThreadContextIntegrationTest(unittest.TestCase):
    def setUp(self):
        """Создать отдельное состояние, каталог и минимальный Messenger-дубль."""
        directory = self.enterContext(TemporaryDirectory())
        self.database = sqlite3.connect(":memory:")
        self.addCleanup(self.database.close)
        self.messenger = Mock()
        self.messenger.chat = "0/0/00000000-0000-0000-0000-000000000000"
        self.messenger.identity = {"guid": "service"}
        self.messenger.send.return_value = 777
        self.messenger.context_messages.return_value = []
        self.bridge = ContextBridge(self.database, self.messenger, Path(directory) / "gateway")
        self.bridge._draft = Mock(return_value=("Первый ответ", "native-session"))
        self.bridge._task = Mock(return_value=("Файл подготовлен", ()))

    def test_root_isolated_and_followup_has_both_roles(self):
        """Новый корень не получает чужую native session; продолжение видит исходный запрос и ответ."""
        self.bridge._respond(0, 100, "", {"text": "Первый запрос"}, "shared-main-session")
        self.bridge._respond(100, 200, "", {"text": "Уточнение"}, "old-thread-session")
        call = self.bridge._draft.call_args
        self.assertIsNone(call.args[3])
        self.assertEqual(call.args[2]["_thread_context"]["messages"], [
            {"role": "user", "text": "Первый запрос"},
            {"role": "assistant", "text": "Первый ответ"},
        ])
        self.bridge._respond(0, 300, "", {"text": "Независимый запрос"}, "shared-main-session")
        self.assertEqual(self.bridge._draft.call_args.args[2]["_thread_context"]["messages"], [])

    def test_file_survives_delivery_and_followup_stays_task(self):
        """Файл сохраняется до удаления delivery-копии и передаётся следующему поручению того же треда."""
        output = self.bridge.workspace.parent / "job" / "out"
        output.mkdir(parents=True)
        (output / "hello.txt").write_text("CONTEXT_OK")
        preserved = self.bridge._preserve_files(output)
        self.bridge._task.side_effect = [("Готов hello.txt", preserved), ("Обновлено", ())]
        self.bridge._send_artifacts = Mock(side_effect=lambda *args, **kwargs: self._delete_delivery(preserved))
        with patch.dict(os.environ, {"WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true"}):
            self.bridge._respond(0, 100, "", {"text": "/task Создай hello.txt"}, None)
            self.assertFalse(preserved[0].exists())
            self.bridge._respond(100, 200, "", {"text": "Добавь вторую строку"}, None)
        self.assertEqual(self.bridge._task.call_count, 2)
        snapshot = self.bridge._task.call_args.args[2]["_thread_context"]
        self.assertEqual(Path(snapshot["files"][0]["path"]).read_text(), "CONTEXT_OK")
        self.assertTrue(snapshot["task"])

    def _delete_delivery(self, paths):
        """Удалить сохранённые delivery-копии так же, как успешная отправка артефактов."""
        for path in paths:
            self.bridge._discard_artifact_file(str(path))
        return 0

    def test_bootstrap_excludes_comments_controls_and_service_progress(self):
        """История существующего треда включает полезный HTML, но исключает комментарии и статусы."""
        self.messenger.context_messages.return_value = [
            (100, {"text": "Исходный запрос\n# private note", "author_guid": "human"}),
            (110, {"text": "`|` Задача выполняется …", "author_guid": "service"}),
            (120, {"text": "<html>/* сохранить код */</html>", "author_guid": "service"}),
            (130, {"text": "/status", "author_guid": "human"}),
            (135, {"text": "**/model** example-model high", "author_guid": "human"}),
            (140, {"text": "# Только для людей", "author_guid": "human"}),
        ]
        prepared = self.bridge._prepare_thread_context(100, 200, {"text": "Уточнение"})
        self.assertEqual(prepared["_thread_context"]["messages"], [
            {"role": "user", "text": "Исходный запрос\n"},
            {"role": "assistant", "text": "<html>/* сохранить код */</html>"},
        ])

    def test_reset_does_not_bootstrap_old_thread(self):
        """Сброс исключает старую историю и task-признак; следующий запрос начинается с пустого контекста."""
        self.bridge._prepare_thread_context(100, 200, {"text": "Старый запрос"})
        self.bridge._reset_thread_context(100, 250)
        self.messenger.context_messages.reset_mock()
        result = self.bridge._prepare_thread_context(100, 300, {"text": "Новый запрос"})
        self.assertEqual(result["_thread_context"]["messages"], [])
        self.assertFalse(result["_thread_context"]["task"])
        self.messenger.context_messages.assert_not_called()

    def test_cancelled_job_has_explicit_terminal_status_without_success(self):
        """Подтверждённая отмена не выдаётся за успех или неизвестную ошибку и не запускает модель повторно."""
        self.bridge._draft.side_effect = CodexTurnError("worker_cancelled")
        self.bridge._respond(0, 100, "", {"text": "Долгая задача"}, None)
        self.messenger.edit.assert_called_with(0, 777, "⏹ Остановлено.")
        self.messenger.react.assert_called_with(0, 100, "👀", remove=True)
        self.assertEqual(self.database.execute("SELECT failed FROM replies").fetchone(), (2,))

    def test_optional_context_cache_failure_does_not_hide_ready_result(self):
        """Ошибка памяти после model call не блокирует ответ и не повторяет модель при delivery retry."""
        store = self.bridge._context_store()
        with patch.object(store, "remember_files", side_effect=OSError("private diagnostic")):
            output = self.bridge.workspace.parent / "output"
            output.mkdir()
            (output / "hello.txt").write_text("OK")
            paths = self.bridge._preserve_files(output)
            self.bridge._task.return_value = ("Готовый результат", paths)
            self.bridge._send_artifacts = Mock(return_value=0)
            with patch.dict(os.environ, {"WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true"}):
                self.bridge._respond(0, 100, "", {"text": "/task создать файл"}, None)
        self.bridge._task.assert_called_once()
        self.assertEqual(self.messenger.send.call_args.args[1], "Готовый результат")
        self.messenger.edit.assert_called_with(0, 777, "✅ Готово.")

    def test_context_history_keeps_exact_root_and_exclusive_bounds(self):
        """Чтение bootstrap использует точный корень и отбрасывает включённые граничные сообщения."""
        messenger = object.__new__(Messenger)
        messenger.chat = self.messenger.chat
        messenger.thread_chat_id = lambda chat, parent: f"{chat}/{parent}"
        messenger.client = Mock()
        messenger.client._message_info.return_value = {"text": "root"}
        messenger.render = lambda raw: {"text": str(raw["ServerMessageInfo"]["Timestamp"])}
        messenger.client.fanout.return_value = {"Chats": [{
            "ChatId": messenger.feed(100),
            "Messages": [{"ServerMessageInfo": {"Timestamp": stamp}} for stamp in (100, 150, 200)],
        }]}
        result = messenger.context_messages(100, 200)
        self.assertEqual([stamp for stamp, message in result], [100, 150])
        messenger.client._message_info.assert_called_once_with(messenger.chat, 100)


if __name__ == "__main__":
    unittest.main()
