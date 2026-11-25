"""Проверки команд треда без сети и без настоящего вызова модели."""

from __future__ import annotations

from concurrent.futures import Future
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

os.environ.setdefault("WORKSHEET_MESSENGER_CHAT_ID", "0/0/00000000-0000-0000-0000-000000000000")
os.environ.setdefault("WORKSHEET_MESSENGER_EMAIL", "user@example.test")

from messenger_codex.bridge import Bridge, CodexTurnError
from messenger_worksheets.chat_commands import chat_command, parse_chat_command
from messenger_worksheets.cloud_gateway import CloudBridge


class ChatCommandsTest(unittest.TestCase):
    """Проверить идемпотентность, изоляцию тредов и настоящую отмену RPC."""

    def setUp(self):
        """Создать реальную SQLite и приватный workspace с поддельным Messenger."""
        self.directory = self.enterContext(TemporaryDirectory())
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        messenger = Mock()
        messenger.chat = os.environ["WORKSHEET_MESSENGER_CHAT_ID"]
        messenger.identity = {"guid": "service"}
        messenger.send.return_value = 777
        messenger.new_messages.return_value = []
        self.bridge = CloudBridge(database, messenger, Path(self.directory))
        self.bridge._draft = Mock(return_value=("model answer", None))
        self.bridge._task = Mock(return_value=("model answer", ()))
        self.bridge._reset_thread_context = Mock()
        self.bridge._worker_job_control = Mock()
        self.bridge._worker_capabilities = Mock(return_value={
            "model": "configured-model", "reasoning_effort": "high", "mode": "local_user",
        })
        self.bridge._worker_models = Mock(return_value={
            "available": True, "provider": "sandbox_admin", "source": "codex_active_catalog",
            "scope": "catalogue", "fetched_at": 123, "truncated": False, "error": None,
            "models": [{"id": "listed-model", "is_default": True,
                        "reasoning_efforts": ["low", "high"], "default_reasoning_effort": "low"}],
        })
        self.bridge._command_schema()

    def command(self, text, *, parent=100, stamp=200, session="old"):
        """Направить сообщение прямо в публичный путь команд CloudBridge._respond."""
        return self.bridge._respond(parent, stamp, "ignored", {"text": text, "author_guid": "human"}, session)

    def test_only_exact_controls_and_explicit_modes_are_commands(self):
        """Отличить команды от цитат, URL, человеческих комментариев и похожих слов."""
        for text, name in (
            ("/stop", "stop"), ("`/status`", "status"), ("**/reset**", "reset"),
            ("# people\n/help", "help"), ("/* ignored */ /status", "status"),
            ("/model gpt-6.2-sol high", "model"), ("**/plan** explain", "plan"),
            ("`/review inspect files`", "review"),
            ("/models", "models"), ("**/models** task", "models"),
        ):
            with self.subTest(text=text):
                self.assertEqual(chat_command(text), name)
        for text in ("/stop all", "say /stop", "> /stop", "# /stop", "// /status", "/stopper", "https://example.test/stop"):
            with self.subTest(text=text):
                self.assertIsNone(chat_command(text))

    def test_control_receipts_are_idempotent_and_bypass_model(self):
        """Двойной проход команды не вызывает модель и не создаёт второй ответ."""
        for stamp in (200, 200):
            self.command("/status", stamp=stamp)
        self.bridge._draft.assert_not_called()
        self.bridge._task.assert_not_called()
        self.assertEqual(self.bridge.messenger.send.call_count, 1)
        self.assertIn("нет запущенной", self.bridge.messenger.send.call_args.args[1])

    def test_models_show_both_routes_and_reasoning_without_model_call(self):
        """Каталоги берутся у двух worker, команда не попадает в модель и не повторяет fetch при replay."""
        self.command("/models")
        text = self.bridge.messenger.send.call_args.args[1]
        self.assertIn("Обычные ответы", text)
        self.assertIn("/task, /plan, /review", text)
        self.assertIn("`listed-model` — выбрана", text)
        self.assertIn("low, high", text)
        self.assertIn("из каталога", text)
        self.command("/models")
        self.assertEqual(self.bridge._worker_models.call_count, 2)
        self.assertEqual(self.bridge.messenger.send.call_count, 1)
        self.bridge._draft.assert_not_called()
        self.bridge._task.assert_not_called()

    def test_model_catalog_failures_are_independent_and_route_argument_is_validated(self):
        """Сбой каталога первого маршрута не скрывает второй, неверный аргумент не вызывает HTTP."""
        valid = self.bridge._worker_models.return_value
        self.bridge._worker_models.side_effect = [RuntimeError("private diagnostic"), valid]
        self.command("/models")
        text = self.bridge.messenger.send.call_args.args[1]
        self.assertIn("Не удалось", text)
        self.assertIn("listed-model", text)
        self.assertNotIn("private diagnostic", text)
        self.bridge._worker_models.reset_mock(side_effect=True)
        self.command("/models task", stamp=201)
        self.assertEqual(self.bridge._worker_models.call_count, 1)
        self.command("/models unknown", stamp=202)
        self.assertEqual(self.bridge._worker_models.call_count, 1)
        self.assertIn("Формат", self.bridge.messenger.send.call_args.args[1])

    def test_large_catalog_delivers_every_identifier_in_idempotent_chunks(self):
        """Большой список не обрезается лимитом сообщения и повтор не создаёт дубликатов частей."""
        models = [{"id": "gpt-" + str(index) + "x" * 80, "is_default": False,
                   "reasoning_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
                   "default_reasoning_effort": "low"} for index in range(100)]
        self.bridge._worker_models.return_value["models"] = models
        self.command("/models task")
        count = self.bridge.messenger.send.call_count
        self.assertGreater(count, 1)
        parts = [call.args[1] for call in self.bridge.messenger.send.call_args_list]
        self.assertTrue(all(len(part) <= 5000 for part in parts))
        joined = "".join(parts)
        for item in models:
            self.assertIn(item["id"], joined)
        self.command("/models task")
        self.assertEqual(self.bridge.messenger.send.call_count, count)
        self.bridge._worker_models.assert_called_once()

    def test_reset_clears_only_requested_thread_and_native_session(self):
        """Сброс передаёт точный тред/cutoff и возвращает None для native session."""
        self.assertIsNone(self.command("/reset"))
        self.bridge._reset_thread_context.assert_called_once_with(100, 200)
        self.command("/reset")
        self.bridge._reset_thread_context.assert_called_once()
        self.assertEqual(self.command("/status", parent=101, stamp=201), "old")

    def test_reset_rejected_while_foreground_job_still_runs(self):
        """Даже terminal metadata не разрешает сброс до возврата текущего результата."""
        self.bridge._begin_job(100, 150, "job", True)
        self.bridge._control_active_thread = 100
        self.bridge._worker_job_control.return_value = {"state": "completed"}
        self.assertEqual(self.command("/reset"), "old")
        self.bridge._reset_thread_context.assert_not_called()
        self.assertIn("Сначала отправьте /stop", self.bridge.messenger.send.call_args.args[1])

    def test_stop_is_scoped_to_thread_and_confirms_terminal_state(self):
        """Команда не адресует чужой UUID и подтверждает только cancelled worker."""
        self.bridge._begin_job(101, 150, "other-job", True)
        self.command("/stop")
        self.bridge._worker_job_control.assert_not_called()
        self.bridge._begin_job(100, 151, "correct-job", True)
        self.bridge._worker_job_control.return_value = {"state": "cancelling"}
        self.command("/stop", stamp=201)
        self.assertEqual(self.bridge._worker_job_control.call_args.args[1], "correct-job")
        self.assertEqual(self.bridge._worker_job_control.call_args_list[0].kwargs["action"], "cancel")
        self.assertIn("Остановка запрошена", self.bridge.messenger.send.call_args.args[1])
        self.bridge._finish_job(100, "running")
        self.assertEqual(self.bridge.messenger.send.call_count, 2)
        self.bridge._finish_job(100, "cancelled")
        self.assertEqual(self.bridge.messenger.send.call_args.args[1], "Задача остановлена.")
        self.bridge._finish_job(100, "cancelled")
        self.assertEqual(self.bridge.messenger.send.call_count, 3)

    def test_status_refreshes_stale_running_after_gateway_restart(self):
        """Локальный running не выдаётся за факт живого процесса после restart."""
        self.bridge._begin_job(100, 150, "job", False)
        self.bridge._worker_job_control.return_value = {"state": "completed"}
        self.command("/status")
        self.assertIn("Задача завершена", self.bridge.messenger.send.call_args.args[1])
        self.assertEqual(self.bridge._latest_job(100)["state"], "completed")
        self.command("/reset", stamp=201)
        self.bridge._reset_thread_context.assert_called_once_with(100, 201)

    def test_pending_stop_recovers_its_uuid_after_new_job_replaces_latest(self):
        """Поздний исход нового UUID не подтверждает остановку предыдущей задачи."""
        self.bridge._begin_job(100, 150, "old-job", True)
        self.bridge._worker_job_control.return_value = {"state": "cancelling"}
        self.command("/stop", stamp=201)
        self.bridge._begin_job(100, 250, "new-job", False)
        self.bridge._finish_job(100, "cancelled", "new-job")
        self.assertEqual(self.bridge.db.execute("SELECT pending_stop FROM chat_commands").fetchone()[0], 1)
        self.bridge._worker_job_control.return_value = {"state": "cancelled"}
        self.bridge._recover_pending_stops(100)
        self.assertEqual(self.bridge._worker_job_control.call_args.args[1], "old-job")
        self.assertEqual(self.bridge.db.execute("SELECT pending_stop FROM chat_commands").fetchone()[0], 0)
        self.assertEqual(self.bridge.messenger.send.call_args.args[1], "Задача остановлена.")

    def test_help_reports_actual_worker_metadata_and_every_command(self):
        """Справка перечисляет реализованные команды и значения remote worker."""
        self.command("/help")
        reply = self.bridge.messenger.send.call_args.args[1]
        for name in ("task", "stop", "status", "reset", "help", "model", "plan", "review"):
            self.assertIn("/" + name, reply)
        self.assertIn("configured-model", reply)
        self.assertIn("reasoning_effort=high", reply)
        self.assertEqual(self.bridge._worker_capabilities.call_count, 2)
        self.bridge._worker_capabilities.side_effect = URLError("PRIVATE")
        self.command("/help", stamp=201)
        reply = self.bridge.messenger.send.call_args.args[1]
        self.assertIn("не определена worker", reply)
        self.assertNotIn("PRIVATE", reply)

    def test_model_overrides_are_thread_scoped_and_route_specific(self):
        """Общий override и отдельный task override не меняют соседний тред."""
        self.command("/model gpt-test medium")
        self.command("/model task gpt-task high", stamp=201)
        self.assertEqual(self.bridge._model_options(100, False), {"model": "gpt-test", "reasoning_effort": "medium"})
        self.assertEqual(self.bridge._model_options(100, True), {"model": "gpt-task", "reasoning_effort": "high"})
        self.assertEqual(self.bridge._model_options(101, True), {})
        self.command("/model", stamp=202)
        self.assertIn("gpt-task", self.bridge.messenger.send.call_args.args[1])
        self.command("/model default", stamp=203)
        self.assertEqual(self.bridge._model_options(100, True), {})

    def test_invalid_model_values_never_enter_database(self):
        """Пробелы, shell-синтаксис и неизвестный effort отклоняются до worker."""
        for stamp, text in enumerate(("/model $(id)", "/model gpt-test invalid", "/model gpt x y"), 201):
            self.command(text, stamp=stamp)
        self.assertEqual(self.bridge._model_options(100, False), {})
        self.bridge._draft.assert_not_called()

    def test_common_model_replaces_old_route_specific_overrides(self):
        """Обещание сменить модель обоих режимов соответствует фактическому выбору."""
        self.command("/model chat old-chat")
        self.command("/model task old-task", stamp=201)
        self.command("/model new-common high", stamp=202)
        self.assertEqual(self.bridge._model_options(100, False)["model"], "new-common")
        self.assertEqual(self.bridge._model_options(100, True)["model"], "new-common")

    def test_retry_keeps_first_job_options_after_model_changes(self):
        """Повтор UUID сохраняет digest опций даже после команды /model в треде."""
        first = self.bridge._begin_job(100, 150, "job-one", True, {"model": "old"})
        replay = self.bridge._begin_job(100, 150, "job-one", True, {"model": "new"})
        following = self.bridge._begin_job(100, 250, "job-two", True, {"model": "new"})
        self.assertEqual(first, {"model": "old"})
        self.assertEqual(replay, first)
        self.assertEqual(following, {"model": "new"})

    def test_plan_review_transform_into_explicit_read_only_task_modes(self):
        """Полезные Codex режимы передаются как protocol options, а не обещания модели."""
        for stamp, mode in enumerate(("plan", "review"), 201):
            with patch.object(Bridge, "_respond", return_value="session") as respond:
                self.command(f"/{mode} Inspect project", stamp=stamp)
            message = respond.call_args.args[3]
            self.assertEqual(message["_request_mode"], mode)
            self.assertEqual(message["text"], "Inspect project")
        self.command("/plan", stamp=210)
        self.bridge._draft.assert_not_called()

    def test_polling_controls_preserves_ordinary_queue_cursor(self):
        """Команды обходят обычную очередь без потери предыдущих followup-запросов."""
        started = self.bridge.started
        self.bridge.messenger.new_messages.return_value = [
            (201, {"text": "ordinary followup", "author_guid": "human"}),
            (202, {"text": "/status", "author_guid": "human"}),
            (203, {"text": "/plan later task", "author_guid": "human"}),
        ]
        self.bridge._poll_commands(100, 150)
        self.assertEqual(self.bridge.db.execute("SELECT cursor FROM feeds WHERE parent=0").fetchone()[0], started)
        self.assertEqual(self.bridge.db.execute("SELECT incoming FROM chat_commands").fetchall(), [(202,)])
        self.bridge._draft.assert_not_called()

    def test_running_transfer_polls_stop_without_background_sqlite_access(self):
        """Долгий RPC обрабатывает /stop, завершается 409 и не вызывает модель повторно."""
        del self.bridge._draft
        finish = threading.Event()
        main_thread = threading.get_ident()
        observations = []

        def transfer(*args, **kwargs):
            """Имитировать работающий worker до аутентифицированной отмены."""
            self.assertNotEqual(threading.get_ident(), main_thread)
            if not finish.wait(2):
                raise AssertionError("stop command was not polled")
            raise HTTPError("https://worker.invalid/", 409, "", {}, None)

        def cancel(*args, **kwargs):
            """Подтвердить, что control RPC вызывается из основного SQLite-потока."""
            observations.append(threading.get_ident())
            finish.set()
            return {"state": "cancelling"}

        self.bridge.transfer = transfer
        self.bridge._worker_job_control.side_effect = cancel
        self.bridge.messenger.new_messages.return_value = [(201, {"text": "/stop", "author_guid": "human"})]
        with patch("messenger_worksheets.cloud_gateway.PROGRESS_SECONDS", 0.001):
            with self.assertRaisesRegex(CodexTurnError, "worker_cancelled"):
                self.bridge._run_transfer(100, 150, {"text": "work"}, task=True, session=None, delivery_parent=100)
        self.assertTrue(observations)
        self.assertEqual(set(observations), {main_thread})
        self.assertEqual(self.bridge._latest_job(100)["state"], "cancelled")
        self.assertEqual(self.bridge.messenger.send.call_args.args[1], "Задача остановлена.")

    def test_transfer_uses_canonical_history_files_and_explicit_options(self):
        """RPC получает переносимые файлы/историю/опции без native session и пути gateway."""
        source = Path(self.directory) / "private-cache"
        source.write_text("FILE_CONTENT", encoding="utf-8")
        message = {
            "text": "use the previous file", "media": [],
            "_thread_context": {
                "messages": [{"role": "user", "text": "original task"}],
                "files": [{"name": "stable-name", "original_name": "hello.txt", "path": str(source)}],
            },
            "_model_options": {"model": "chosen", "mode": "review"},
        }
        self.bridge.messenger.download_images.return_value = ()
        with patch("messenger_worksheets.cloud_gateway.submit", return_value={"text": "ok", "session": None, "files": []}) as submit:
            self.bridge.transfer(100, 150, message, task=True, session=None, worker_url="https://worker.invalid/v1/codex-worksheet-jobs")
        job = submit.call_args.args[2]
        self.assertIsNone(job["session"])
        self.assertEqual(job["context"], message["_thread_context"]["messages"])
        self.assertEqual(job["options"], message["_model_options"])
        self.assertEqual(job["history_files"][0]["original_name"], "hello.txt")
        self.assertNotIn("path", job["history_files"][0])

    def test_context_transfer_rejects_symlink_and_nonregular_sources(self):
        """Файлы истории проверяются повторно до передачи worker, включая O_NOFOLLOW."""
        source = Path(self.directory) / "original"
        source.write_text("content", encoding="utf-8")
        alias = Path(self.directory) / "alias"
        alias.symlink_to(source)
        with self.assertRaises(OSError):
            self.bridge._history_files({"files": [{"name": "alias", "path": alias}]})
        with self.assertRaises(ValueError):
            self.bridge._history_files({"files": [{"name": "directory", "path": Path(self.directory)}]})


if __name__ == "__main__":
    unittest.main()
