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
            ("/effort high", "effort"), ("**/effort** task ultra", "effort"),
            ("`/efforts`", "efforts"), ("/* comment */ /EFFORTS chat", "efforts"),
            ("# people\n/effort minimal", "effort"),
        ):
            with self.subTest(text=text):
                self.assertEqual(chat_command(text), name)
        for text in ("/stop all", "say /stop", "> /stop", "# /stop", "// /status", "/stopper", "https://example.test/stop", "/effortless", "/efforts-extra", "> /effort high", "# /efforts"):
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
        for name in ("task", "stop", "status", "reset", "help", "model", "models", "effort", "efforts", "plan", "review"):
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

    def test_effort_preserves_explicit_models_and_is_idempotent_without_worker_calls(self):
        """Изменение effort сохраняет разные выбранные модели, replay не делает новых запросов."""
        self.command("/model chat chosen-chat low", stamp=190)
        self.command("/model task chosen-task medium", stamp=191)
        self.bridge._worker_capabilities.reset_mock()
        self.command("/effort high")
        self.assertEqual(self.bridge._model_options(100, False), {"model": "chosen-chat", "reasoning_effort": "high"})
        self.assertEqual(self.bridge._model_options(100, True), {"model": "chosen-task", "reasoning_effort": "high"})
        self.assertEqual(self.bridge._model_options(101, False), {})
        sent = self.bridge.messenger.send.call_count
        self.command("/effort high")
        self.assertEqual(self.bridge.messenger.send.call_count, sent)
        self.bridge._worker_capabilities.assert_not_called()
        self.bridge._worker_models.assert_not_called()
        self.bridge._draft.assert_not_called()
        self.bridge._task.assert_not_called()

    def test_effort_resolves_defaults_independently_and_commits_both_routes(self):
        """Без override API и native сохраняют собственные реальные defaults, не одну общую модель."""
        self.bridge._worker_capabilities.side_effect = [
            {"model": "api-default", "reasoning_effort": "medium"},
            {"model": "native-default", "reasoning_effort": "high"},
        ]
        with patch.dict(os.environ, {"WORKSHEET_CODEX_WORKER_URL": "https://api.invalid/v1/codex-worksheet-jobs",
                                    "WORKSHEET_TASK_WORKER_URL": "https://task.invalid/v1/codex-worksheet-jobs",
                                    "WORKSHEET_TASK_WORKER_TRANSPORT_TOKEN": "synthetic", "WORKSHEET_TASK_WORKER_CA": "/synthetic/ca"}):
            self.command("/effort ultra")
        self.assertEqual(self.bridge._model_options(100, False), {"model": "api-default", "reasoning_effort": "ultra"})
        self.assertEqual(self.bridge._model_options(100, True), {"model": "native-default", "reasoning_effort": "ultra"})
        self.assertEqual([call.args[0] for call in self.bridge._worker_capabilities.call_args_list],
                         ["https://api.invalid/v1/codex-worksheet-jobs", "https://task.invalid/v1/codex-worksheet-jobs"])
        self.command("/effort ultra")
        self.assertEqual(self.bridge._worker_capabilities.call_count, 2)
        self.assertEqual(self.bridge.messenger.send.call_count, 1)
        self.bridge._worker_models.assert_not_called()

    def test_effort_unknown_default_or_endpoint_failure_does_not_partially_update(self):
        """Сбой второго маршрута оставляет первый без override и скрывает исходную диагностику."""
        for stamp, second in enumerate(({}, {"model": None}, {"model": "unsafe\nmodel"}, URLError("PRIVATE secret")), 200):
            self.bridge._worker_capabilities.side_effect = [{"model": "api-default"}, second]
            self.command("/effort high", stamp=stamp)
            self.assertEqual(self.bridge.db.execute("SELECT * FROM chat_models").fetchall(), [])
            text = self.bridge.messenger.send.call_args.args[1]
            self.assertIn("без изменений", text)
            self.assertNotIn("PRIVATE", text)
            self.assertNotIn("unsafe", text)
        self.bridge._draft.assert_not_called()
        self.bridge._task.assert_not_called()

    def test_effort_route_only_accepts_all_levels_and_preserves_common_model(self):
        """Каждый допустимый уровень изменяет только task effort, общий chat остаётся прежним."""
        self.command("/model selected-model low", stamp=190)
        for stamp, level in enumerate(("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"), 200):
            self.command("/effort task " + level, stamp=stamp)
            self.assertEqual(self.bridge._model_options(100, True), {"model": "selected-model", "reasoning_effort": level})
            self.assertEqual(self.bridge._model_options(100, False), {"model": "selected-model", "reasoning_effort": "low"})
        self.bridge._worker_capabilities.assert_not_called()

    def test_effort_rejected_values_and_current_settings_do_not_write_model_rows(self):
        """Неверные аргументы не обращаются к worker, no-args показывает текущие metadata."""
        for stamp, value in enumerate(("/effort task", "/effort unknown high", "/effort infinite", "/effort high extra", "/effort $(id)"), 200):
            self.command(value, stamp=stamp)
            self.assertIn("Формат", self.bridge.messenger.send.call_args.args[1])
        self.bridge._worker_capabilities.assert_not_called()
        self.command("/effort", stamp=210)
        reply = self.bridge.messenger.send.call_args.args[1]
        self.assertIn("configured-model", reply)
        self.assertIn("reasoning_effort=high", reply)
        self.assertEqual(self.bridge._worker_capabilities.call_count, 2)
        self.assertEqual(self.bridge.db.execute("SELECT * FROM chat_models").fetchall(), [])

    def test_effort_database_failure_rolls_back_both_route_updates(self):
        """Ошибка SQLite второго route откатывает уже подготовленную запись первого."""
        self.command("/model selected low", stamp=190)
        self.bridge.db.execute("CREATE TRIGGER reject_task BEFORE INSERT ON chat_models WHEN NEW.route=1 BEGIN SELECT RAISE(ABORT, 'synthetic'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.bridge._effort_command(100, "high")
        self.assertEqual(self.bridge.db.execute("SELECT route,model,effort FROM chat_models").fetchall(), [(-1, "selected", "low")])

    def test_efforts_explains_only_selected_models_levels_without_mutation_and_replays_once(self):
        """Справка адресуется текущей модели одного маршрута и не подставляет универсальные уровни."""
        self.bridge._worker_models.return_value["models"][0]["id"] = "configured-model"
        self.command("/efforts task")
        text = self.bridge.messenger.send.call_args.args[1]
        self.assertIn("(task)", text)
        self.assertIn("`configured-model`", text)
        self.assertIn("• `low`:", text)
        self.assertIn("• `high` — выбран:", text)
        for level in ("none", "minimal", "medium", "xhigh", "max", "ultra"):
            self.assertNotIn("• `" + level + "`", text)
        self.assertIn("/effort task уровень", text)
        self.assertEqual(self.bridge.db.execute("SELECT * FROM chat_models").fetchall(), [])
        self.command("/efforts task")
        self.assertEqual(self.bridge.messenger.send.call_count, 1)
        self.command("/efforts unknown", stamp=201)
        self.assertIn("Формат", self.bridge.messenger.send.call_args.args[1])
        self.bridge._worker_models.assert_called_once()
        self.bridge._worker_capabilities.assert_called_once()
        self.bridge._draft.assert_not_called()
        self.bridge._task.assert_not_called()

    def test_efforts_current_mode_follows_last_job_and_reset_barrier(self):
        """Без аргументов выбирается режим последнего запроса; reset возвращает обычный маршрут."""
        self.bridge._begin_job(100, 150, "job", True)
        self.bridge._worker_models.return_value["models"][0]["id"] = "configured-model"
        self.command("/efforts", stamp=200)
        self.assertIn("(task)", self.bridge.messenger.send.call_args.args[1])
        self.bridge.db.execute("CREATE TABLE thread_context_resets(thread INTEGER PRIMARY KEY, cutoff INTEGER)")
        self.bridge.db.execute("INSERT INTO thread_context_resets VALUES(100,250)")
        self.bridge.db.commit()
        self.command("/efforts", stamp=300)
        self.assertIn("(chat)", self.bridge.messenger.send.call_args.args[1])

    def test_efforts_uses_explicit_selected_model_instead_of_catalogue_default(self):
        """Для выбранной модели берутся её уровни и описания, даже если default worker другой."""
        self.command("/model task chosen-model medium", stamp=190)
        self.bridge._worker_models.return_value["models"].append({
            "id": "chosen-model", "is_default": False, "reasoning_efforts": ["medium", "high"],
            "default_reasoning_effort": "low", "reasoning_source": "codex_catalog",
            "reasoning_descriptions": {"medium": "Balances speed and reasoning depth for everyday tasks"},
        })
        self.command("/efforts task")
        text = self.bridge.messenger.send.call_args.args[1]
        self.assertIn("`chosen-model`", text)
        self.assertIn("• `medium` — выбран: Баланс скорости", text)
        self.assertNotIn("• `low`", text)
        self.assertNotIn("`listed-model`", text)

    def test_efforts_unknown_capabilities_does_not_guess_eight_levels(self):
        """Отсутствие списка для конкретной модели явно обозначается и не заменяется всеми уровнями."""
        self.bridge._worker_models.return_value["models"] = []
        self.command("/efforts chat")
        text = self.bridge.messenger.send.call_args.args[1]
        self.assertIn("`configured-model`", text)
        self.assertIn("не сообщил точный набор", text)
        self.assertNotIn("• `", text)

    def test_models_show_provider_purpose_and_concrete_capabilities(self):
        """Назначение, контекст и входы относятся к строке модели; отсутствующие сведения не угадываются."""
        item = self.bridge._worker_models.return_value["models"][0]
        item.update(description="Fast, go-to model for coding and everyday work.", context_window=500000,
                    max_context_window=872000, input_modalities=["text", "image"])
        self.command("/models task")
        text = self.bridge.messenger.send.call_args.args[1]
        self.assertIn("Назначение: Быстрая модель для программирования", text)
        self.assertIn("500 000 токенов", text)
        self.assertIn("872 000", text)
        self.assertIn("вход: текст, изображения", text)
        self.assertIn("предел истории задаются настройками чата", text)

    def test_slow_catalog_does_not_delay_later_stop_or_change_captured_model(self):
        """Медленная справка не мешает позднему /stop и сохраняет выбор на момент запроса."""
        for offset, command in enumerate(("/models task", "/efforts task")):
            with self.subTest(command=command):
                bridge = self.bridge
                bridge._control_active_thread = 100
                bridge._begin_job(100, 150 + offset, "job-" + str(offset), True)
                bridge._model_command(100, "task captured-model high")
                started, release = threading.Event(), threading.Event()
                foreground = threading.get_ident()
                source = {**bridge._worker_models.return_value, "models": [
                    {"id": "captured-model", "is_default": False, "reasoning_efforts": ["high"],
                     "default_reasoning_effort": "high"},
                    {"id": "new-model", "is_default": False, "reasoning_efforts": ["low"],
                     "default_reasoning_effort": "low"},
                ]}
                def slow_catalog(*args, **kwargs):
                    """Задержать только HTTP-замену и проверить отделение от SQLite-потока."""
                    self.assertNotEqual(threading.get_ident(), foreground)
                    started.set()
                    self.assertTrue(release.wait(3))
                    return source
                bridge._worker_models.reset_mock()
                bridge._worker_models.side_effect = slow_catalog
                bridge._worker_job_control.return_value = {"state": "cancelled"}
                stamp = 400 + offset * 10
                message = {"text": command, "author_guid": "human"}
                bridge.messenger.new_messages.return_value = [(stamp, message)]
                try:
                    bridge._poll_commands(100, 150)
                    self.assertTrue(started.wait(1))
                    self.assertFalse(release.is_set())
                    # Смена модели и следующий poll выполняются, пока RPC справки ещё ждёт.
                    bridge._model_command(100, "task new-model low")
                    bridge.messenger.new_messages.return_value = [
                        (stamp, message),
                        (stamp + 1, {"text": "ordinary followup", "author_guid": "human"}),
                        (stamp + 2, {"text": "/stop", "author_guid": "human"}),
                    ]
                    bridge._poll_commands(100, 150)
                    self.assertEqual(bridge._worker_models.call_count, 1)
                    self.assertEqual(bridge._worker_job_control.call_args.kwargs["action"], "cancel")
                    self.assertFalse(release.is_set())
                    self.assertIsNone(bridge.db.execute(
                        "SELECT result FROM chat_commands WHERE incoming=?", (stamp,)).fetchone()[0])
                    bridge._draft.assert_not_called()
                    bridge._task.assert_not_called()
                    bridge._control_active_thread = None
                    # Повтор receipt после завершения задачи также не создаёт второй fetch.
                    bridge._handle_command(100, stamp, message, command.split()[0][1:])
                    self.assertEqual(bridge._worker_models.call_count, 1)
                    release.set()
                    future = bridge._catalog_pending[(100, stamp)][0]
                    future.result(timeout=2)
                    send_threads = []
                    bridge.messenger.send.side_effect = lambda *a, **k: send_threads.append(threading.get_ident()) or 777
                    with patch.object(Bridge, "poll", return_value=None):
                        bridge.poll()
                    result = bridge.db.execute("SELECT result FROM chat_commands WHERE incoming=?", (stamp,)).fetchone()[0]
                    self.assertIn("captured-model", result)
                    self.assertNotIn("`new-model` — выбрана", result)
                    if command.startswith("/efforts"):
                        self.assertNotIn("Выбранная модель: `new-model`", result)
                        self.assertIn("Выбранная модель: `captured-model`", result)
                    self.assertEqual(send_threads, [foreground])
                    sent = bridge.messenger.send.call_count
                    bridge._flush_catalog_commands()
                    bridge._handle_command(100, stamp, message, command.split()[0][1:])
                    self.assertEqual(bridge.messenger.send.call_count, sent)
                    self.assertEqual(bridge._worker_models.call_count, 1)
                finally:
                    release.set()
                    bridge.messenger.send.side_effect = None
                    bridge._catalog_executor.shutdown(wait=True)
                    del bridge._catalog_executor
                    bridge._worker_job_control.reset_mock()

    def test_catalog_queue_is_bounded_and_batch_stop_precedes_fetch(self):
        """Два фоновых HTTP-потока и восемь receipts не лишают /stop приоритета."""
        bridge = self.bridge
        bridge._control_active_thread = 100
        bridge._begin_job(100, 150, "job", True)
        release, two_started = threading.Event(), threading.Event()
        lock = threading.Lock()
        events = []
        def fetch(*args, **kwargs):
            """Сдержать первые два RPC, чтобы проверить реальную границу concurrency."""
            with lock:
                events.append("fetch")
                if events.count("fetch") == 2:
                    two_started.set()
            self.assertTrue(release.wait(3))
            return bridge._worker_models.return_value
        def stop(*args, **kwargs):
            """Отметить момент реального control RPC относительно загрузок справки."""
            events.append("stop")
            return {"state": "cancelled"}
        bridge._worker_models.side_effect = fetch
        bridge._worker_job_control.side_effect = stop
        bridge.messenger.new_messages.return_value = [
            *[(200 + index, {"text": "/models task", "author_guid": "human"}) for index in range(9)],
            (220, {"text": "/stop", "author_guid": "human"}),
        ]
        try:
            bridge._poll_commands(100, 150)
            self.assertTrue(two_started.wait(1))
            self.assertEqual(events[0], "stop")
            self.assertEqual(events.count("fetch"), 2)
            self.assertEqual(len(bridge._catalog_pending), 8)
            rejection = bridge.db.execute("SELECT result FROM chat_commands WHERE incoming=208").fetchone()[0]
            self.assertIn("уже загружается", rejection)
            self.assertNotIn("token", rejection)
            release.set()
            for future, _, _ in bridge._catalog_pending.values():
                future.result(timeout=2)
            bridge._flush_catalog_commands()
            self.assertEqual(len(bridge._catalog_pending), 0)
            self.assertEqual(events.count("fetch"), 8)
        finally:
            release.set()
            bridge._catalog_executor.shutdown(wait=True)

    def test_catalog_result_is_saved_before_delivery_and_retried_without_refetch(self):
        """Потеря доставки сохраняет ответ receipt и не меняет его после /model."""
        bridge = self.bridge
        bridge._control_active_thread = 100
        bridge._model_command(100, "task configured-model high")
        bridge._worker_models.return_value["models"][0]["id"] = "configured-model"
        bridge._handle_command(100, 200, {"text": "/efforts task"}, "efforts")
        try:
            bridge._catalog_pending[(100, 200)][0].result(timeout=2)
            original_send = bridge._send_command_result
            with patch.object(bridge, "_send_command_result", side_effect=RuntimeError("synthetic delivery failure")):
                bridge._flush_catalog_commands()
            saved = bridge.db.execute("SELECT result FROM chat_commands WHERE incoming=200").fetchone()[0]
            self.assertIn("Выбранная модель: `configured-model`", saved)
            bridge._model_command(100, "task different-model low")
            with patch.object(bridge, "_send_command_result", wraps=original_send) as send:
                bridge._flush_catalog_commands()
                self.assertEqual(send.call_args.args[2], saved)
                self.assertEqual(threading.get_ident(), threading.main_thread().ident)
            bridge._worker_models.assert_called_once()
            self.assertFalse(bridge._catalog_pending)
        finally:
            bridge._catalog_executor.shutdown(wait=True)

    def test_catalog_snapshot_recovers_after_process_state_is_lost_without_storing_credentials(self):
        """Restart восстанавливает выбор модели, но берёт connection из текущего профиля."""
        bridge = self.bridge
        bridge._control_active_thread = 100
        bridge._model_command(100, "task captured-model high")
        bridge._worker_models.return_value["models"][0]["id"] = "captured-model"
        connection = {"endpoint": "https://worker.example.test/job", "transport_token": "synthetic-private-token", "ca_file": "/private/ca"}
        with patch.object(bridge, "_job_route", return_value=connection):
            bridge._handle_command(100, 200, {"text": "/efforts task"}, "efforts")
            bridge._catalog_pending[(100, 200)][0].result(timeout=2)
            bridge._catalog_executor.shutdown(wait=True)
            del bridge._catalog_pending
            del bridge._catalog_executor
            stored = bridge.db.execute("SELECT catalog_snapshot FROM chat_commands WHERE incoming=200").fetchone()[0]
            self.assertIn("captured-model", stored)
            for private in ("synthetic-private-token", "worker.example.test", "/private/ca", "connection"):
                self.assertNotIn(private, stored)
            bridge._control_active_thread = None
            bridge._model_command(100, "task changed-model low")
            try:
                with patch.object(Bridge, "poll", return_value=None):
                    bridge.poll()
                pending = bridge._catalog_pending.get((100, 200))
                if pending is not None:
                    pending[0].result(timeout=2)
                    bridge._flush_catalog_commands()
                saved = bridge.db.execute("SELECT result,catalog_snapshot FROM chat_commands WHERE incoming=200").fetchone()
                self.assertIn("Выбранная модель: `captured-model`", saved[0])
                self.assertIsNone(saved[1])
                self.assertEqual(bridge._worker_models.call_count, 2)
            finally:
                bridge._catalog_executor.shutdown(wait=True)

    def test_catalog_delivery_retry_survives_restart_without_new_http(self):
        """Сохранённый ответ после сбоя доставки восстанавливается напрямую из receipt."""
        bridge = self.bridge
        bridge._control_active_thread = 100
        bridge._worker_models.return_value["models"][0]["id"] = "configured-model"
        bridge._handle_command(100, 200, {"text": "/efforts task"}, "efforts")
        bridge._catalog_pending[(100, 200)][0].result(timeout=2)
        try:
            with patch.object(bridge, "_send_command_result", side_effect=RuntimeError("synthetic delivery failure")):
                bridge._flush_catalog_commands()
            saved = bridge.db.execute("SELECT result,catalog_snapshot FROM chat_commands WHERE incoming=200").fetchone()
            self.assertIsNotNone(saved[0])
            self.assertIsNotNone(saved[1])
            bridge._catalog_executor.shutdown(wait=True)
            del bridge._catalog_pending
            del bridge._catalog_executor
            bridge._control_active_thread = None
            with patch.object(Bridge, "poll", return_value=None):
                bridge.poll()
            self.assertEqual(bridge.messenger.send.call_args.args[1], saved[0])
            self.assertIsNone(bridge.db.execute("SELECT catalog_snapshot FROM chat_commands WHERE incoming=200").fetchone()[0])
            bridge._worker_models.assert_called_once()
        finally:
            executor = getattr(bridge, "_catalog_executor", None)
            if executor is not None:
                executor.shutdown(wait=True)

    def test_effort_during_active_job_changes_only_next_uuid_options(self):
        """Polling применяет effort к следующему UUID; replay активного сохраняет исходные опции."""
        self.command("/model chosen low", stamp=190)
        initial = self.bridge._begin_job(100, 150, "active-job", True, self.bridge._model_options(100, True))
        self.bridge.messenger.new_messages.return_value = [(201, {"text": "/effort task high", "author_guid": "human"})]
        self.bridge._poll_commands(100, 150)
        updated = self.bridge._model_options(100, True)
        replay = self.bridge._begin_job(100, 150, "active-job", True, updated)
        following = self.bridge._begin_job(100, 250, "following-job", True, updated)
        self.assertEqual(initial, {"model": "chosen", "reasoning_effort": "low"})
        self.assertEqual(replay, initial)
        self.assertEqual(following, {"model": "chosen", "reasoning_effort": "high"})
        self.bridge._draft.assert_not_called()
        self.bridge._task.assert_not_called()

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
