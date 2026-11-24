"""Команды треда без обращения к модели и без прерывания чужих запросов."""

from __future__ import annotations

import os
import json
import re
import time

from messenger_codex.bridge import strip_prompt_comments


COMMANDS = frozenset({"stop", "status", "reset", "help", "model", "plan", "review"})
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
ACTIVE_STATES = frozenset({"running", "cancelling", "unknown"})
TERMINAL_STATES = frozenset({"cancelled", "completed", "failed"})
COMMAND_POLL_SECONDS = 1.0


def parse_chat_command(text: str) -> tuple[str, str] | None:
    """Распознать управляющий префикс после удаления человеческих комментариев.

    Принимает только целиком совпавшие /stop, /status, /reset, /help и команды
    /model, /plan, /review с аргументом. Парные Markdown-обёртки допустимы
    вокруг команды или всего сообщения. Цитаты и похожие имена не совпадают.
    Функция не меняет вход и не обращается к сети или состоянию треда.
    """
    cleaned = strip_prompt_comments(text).strip()
    for wrapper in ("**", "__", "`", "*", "_"):
        if cleaned.startswith(wrapper) and cleaned.endswith(wrapper):
            cleaned = cleaned[len(wrapper):-len(wrapper)].strip()
            break
        prefix = re.match(re.escape(wrapper) + r"(/(?:model|plan|review))" + re.escape(wrapper), cleaned, re.IGNORECASE)
        if prefix:
            cleaned = prefix[1] + cleaned[prefix.end():]
            break
    match = re.fullmatch(r"/(stop|status|reset|help|model|plan|review)(?:\s+([\s\S]*))?", cleaned, re.IGNORECASE)
    if match is None:
        return None
    name, arguments = match[1].lower(), (match[2] or "").strip()
    if name in {"stop", "status", "reset", "help"} and arguments:
        return None
    return name, arguments


def chat_command(text: str) -> str | None:
    """Вернуть имя распознанной команды, не копируя её аргументы в логи.

    Используется polling-кодом и тестами для проверки управляющих сообщений;
    полный разбор аргументов выполняет parse_chat_command без побочных эффектов.
    """
    parsed = parse_chat_command(text)
    return parsed[0] if parsed else None


class ChatCommandMixin:
    """Обслуживать команды того же треда в основном SQLite-потоке gateway."""

    def _command_schema(self) -> None:
        """Лениво создать таблицы команд и маршрутов без хранения credentials.

        Команды имеют уникальный parent/incoming и сохранённый текст ответа.
        Задачи привязаны к треду, исходному сообщению и детерминированному UUID;
        в БД записывается только task-флаг маршрута, а токен берётся из env.
        SQLite-соединение используется только вызывающим основным потоком.
        """
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chat_commands (
                parent INTEGER NOT NULL, incoming INTEGER NOT NULL,
                thread INTEGER NOT NULL, name TEXT NOT NULL, result TEXT,
                pending_stop INTEGER NOT NULL DEFAULT 0,
                stop_job_id TEXT, stop_task INTEGER,
                PRIMARY KEY(parent,incoming));
            CREATE TABLE IF NOT EXISTS chat_jobs (
                thread INTEGER PRIMARY KEY, parent INTEGER NOT NULL,
                incoming INTEGER NOT NULL, job_id TEXT NOT NULL,
                task INTEGER NOT NULL, started REAL NOT NULL,
                state TEXT NOT NULL, options_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS chat_models (
                thread INTEGER NOT NULL, route INTEGER NOT NULL,
                model TEXT NOT NULL, effort TEXT,
                PRIMARY KEY(thread,route));
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(chat_commands)")}
        with self.db:
            for column, sql_type in (("stop_job_id", "TEXT"), ("stop_task", "INTEGER")):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE chat_commands ADD COLUMN {column} {sql_type}")
            job_columns = {row[1] for row in self.db.execute("PRAGMA table_info(chat_jobs)")}
            if "options_json" not in job_columns:
                self.db.execute("ALTER TABLE chat_jobs ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'")

    def _respond(self, parent, stamp, prompt, message, session):
        """Перехватить точную управляющую команду до модели и обычного прогресса.

        Остальные сообщения обрабатываются базовым Bridge без изменения
        очереди. /reset возвращает None вместо старой native-сессии; команды
        и их ответы не входят в модельный контекст и не создают /task.
        """
        parsed = parse_chat_command(message.get("text") or "")
        if parsed is None:
            return super()._respond(parent, stamp, prompt, message, session)
        command, arguments = parsed
        self._command_schema()
        if command in {"plan", "review"} and arguments:
            message = {**message, "text": arguments, "_request_mode": command}
            return super()._respond(parent, stamp, prompt, message, session)
        self._handle_command(parent, stamp, message, command)
        if command == "reset":
            row = self.db.execute(
                "SELECT result FROM chat_commands WHERE parent=? AND incoming=?",
                (parent, stamp),
            ).fetchone()
            if row and row[0] == "Контекст треда очищен. Следующий запрос начнётся заново.":
                return None
        return session

    def _model_options(self, thread: int, task: bool) -> dict:
        """Получить настройки модели текущего треда для фактического маршрута.

        Отдельный chat/task override имеет приоритет над общим. Значения
        возвращаются data-only объектом до запуска фонового transfer; worker
        повторно проверяет синтаксис и допустимость режима.
        """
        self._command_schema()
        row = self.db.execute(
            "SELECT model,effort FROM chat_models WHERE thread=? AND route IN (?, -1) "
            "ORDER BY route DESC LIMIT 1", (thread, int(task)),
        ).fetchone()
        if row is None:
            return {}
        options = {"model": row[0]}
        if row[1] is not None:
            options["reasoning_effort"] = row[1]
        return options

    def _model_command(self, thread: int, arguments: str) -> str:
        """Показать или изменить явно заданную модель одного треда.

        /model MODEL [EFFORT] задаёт общий override; необязательный chat/task
        выбирает отдельный route. /model default удаляет настройки треда.
        Модель валидируется по синтаксису, доступность окончательно проверяет
        provider при следующем запросе; команда не обещает наличие модели.
        """
        parts = arguments.split()
        if not parts:
            return self._model_settings_text(thread)
        route = -1
        if parts[0] in {"chat", "task"}:
            route = int(parts.pop(0) == "task")
        if parts == ["default"]:
            with self.db:
                if route == -1:
                    self.db.execute("DELETE FROM chat_models WHERE thread=?", (thread,))
                else:
                    self.db.execute("DELETE FROM chat_models WHERE thread=? AND route=?", (thread, route))
            return (
                "Настройки модели треда сброшены к настройкам worker." if route == -1 else
                "Отдельная настройка маршрута удалена. Действует общая настройка треда либо worker."
            )
        if not 1 <= len(parts) <= 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", parts[0]) or (
            len(parts) == 2 and parts[1] not in EFFORTS
        ):
            return "Формат: /model [chat|task] MODEL [low|medium|high|xhigh|max|ultra] либо /model default."
        with self.db:
            if route == -1:
                self.db.execute("DELETE FROM chat_models WHERE thread=?", (thread,))
            self.db.execute(
                "INSERT INTO chat_models VALUES(?,?,?,?) ON CONFLICT(thread,route) DO UPDATE SET "
                "model=excluded.model,effort=excluded.effort",
                (thread, route, parts[0], parts[1] if len(parts) == 2 else None),
            )
        target = "для обоих режимов" if route == -1 else ("для /task" if route else "для обычных сообщений")
        return (
            f"Для следующих запросов этого треда {target} задана модель {parts[0]}"
            + (f", reasoning_effort={parts[1]}" if len(parts) == 2 else "")
            + ". Доступность модели проверяется при обращении к provider."
        )

    def _model_settings_text(self, thread: int) -> str:
        """Показать worker metadata с учётом явных настроек текущего треда.

        При недоступности worker подтверждённым считается только сохранённый
        override; отсутствующие defaults обозначаются как неизвестные.
        """
        lines = []
        for task, label in ((False, "Обычные сообщения"), (True, "/task")):
            options = self._model_options(thread, task)
            try:
                route = self._job_route(task)
                metadata = self._worker_capabilities(route.pop("endpoint"), **route)
            except Exception:
                metadata = {}
            model = options.get("model") or metadata.get("model") or "не определена worker"
            effort = options.get("reasoning_effort") or metadata.get("reasoning_effort") or "не определён worker"
            source = " (настройка треда)" if options else ""
            lines.append(f"{label}: модель {model}; reasoning_effort={effort}{source}.")
        return "\n".join(lines)

    def _job_route(self, task: bool) -> dict:
        """Вернуть текущие настройки нужного route, не сохраняя токены в БД.

        Полный отдельный task-route обязателен, если задан любой его параметр.
        При отсутствии отдельного маршрута используется основной worker.
        Значения env передаются HTTP-helper напрямую и не журналируются.
        """
        separate = (
            os.environ.get("WORKSHEET_TASK_WORKER_URL"),
            os.environ.get("WORKSHEET_TASK_WORKER_TRANSPORT_TOKEN"),
            os.environ.get("WORKSHEET_TASK_WORKER_CA"),
        ) if task else (None, None, None)
        if any(separate) and not all(separate):
            raise ValueError("task worker routing is incomplete")
        return {
            "endpoint": separate[0] or os.environ.get("WORKSHEET_CODEX_WORKER_URL", ""),
            "transport_token": separate[1],
            "ca_file": separate[2],
        }

    def _latest_job(self, thread: int) -> dict | None:
        """Прочитать последнее задание только указанного треда из локальной БД.

        Результат содержит UUID, исходные timestamp, task-флаг, время старта
        и безопасное состояние. Сведения из другого треда не выбираются.
        """
        row = self.db.execute(
            "SELECT parent,incoming,job_id,task,started,state FROM chat_jobs WHERE thread=?",
            (thread,),
        ).fetchone()
        return None if row is None else dict(zip(
            ("parent", "incoming", "job_id", "task", "started", "state"), row,
        ))

    def _remote_job(self, job: dict, action: str = "status") -> dict:
        """Вызвать контроль worker с тем же UUID и аутентификацией, что у задачи.

        helper наследника проверяет HTTPS endpoint и ограничивает ответ.
        Команда не принимает от пользователя произвольный UUID или адрес.
        """
        route = self._job_route(bool(job["task"]))
        return self._worker_job_control(
            route.pop("endpoint"), job["job_id"], action=action, **route,
        )

    def _job_status_text(self, thread: int) -> str:
        """Показать подтверждённое состояние и возраст последнего задания треда.

        Активное локальное состояние уточняется на worker: после перезапуска
        gateway старый running не выдаётся за доказательство живого процесса.
        Ошибка запроса даёт явное сообщение о невозможности проверки.
        """
        job = self._latest_job(thread)
        if job is None:
            return "В этом треде нет запущенной задачи."
        state = job["state"]
        if state in ACTIVE_STATES:
            try:
                state = self._remote_job(job)["state"]
            except Exception:
                return "Не удалось проверить состояние задачи на worker. Повторите /status."
            with self.db:
                self.db.execute("UPDATE chat_jobs SET state=? WHERE thread=?", (state, thread))
            if state in TERMINAL_STATES:
                try:
                    self._finish_job(thread, state)
                except Exception as error:
                    print(f"stop_confirmation_retry {type(error).__name__}", flush=True)
        labels = {
            "running": "Задача выполняется",
            "cancelling": "Задача останавливается",
            "cancelled": "Задача остановлена",
            "completed": "Задача завершена; состояние доставки смотрите в ответах треда",
            "failed": "Задача завершилась ошибкой",
            "unknown": "Worker не подтвердил состояние задачи",
        }
        label = labels.get(state, "Состояние задачи неизвестно")
        elapsed = max(0, int(time.time() - job["started"]))
        return f"{label}. С момента запуска: {elapsed // 60} мин {elapsed % 60} с."

    def _help_text(self, thread: int) -> str:
        """Перечислить команды и прочитать реальные настройки обоих worker-route.

        Модель и reasoning берутся только из metadata worker, а не из
        предположений gateway. Неопределённые CLI defaults и недоступность
        endpoint явно обозначаются; credentials и внутренние пути не выводятся.
        """
        lines = [
            "/task <запрос> — выполнить задачу с командами и файлами.",
            "/status — состояние последней задачи этого треда.",
            "/stop — остановить выполняющуюся задачу этого треда.",
            "/reset — очистить контекст треда; выполняющуюся задачу сначала остановите.",
            "/help — команды и текущие настройки моделей.",
            "/model [chat|task] MODEL [EFFORT] — модель следующих запросов треда; /model default — сброс.",
            "/plan <запрос> — составить план без изменения файлов.",
            "/review <запрос> — проверить код или материалы без исправлений.",
            "",
        ]
        lines.append(self._model_settings_text(thread))
        return "\n".join(lines)

    def _handle_command(self, parent: int, stamp: int, message: dict, command: str) -> None:
        """Идемпотентно выполнить одну команду и отправить служебный ответ в тред.

        Команды разрешены участникам согласно действующему режиму чата.
        /stop адресуется только последнему заданию данного треда; успешный
        текст об остановке появляется лишь после состояния cancelled worker.
        /reset отклоняется при активной задаче, исключая возврат старого контекста.
        """
        thread = parent or stamp
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO chat_commands(parent,incoming,thread,name) VALUES(?,?,?,?)",
                (parent, stamp, thread, command),
            )
        row = self.db.execute(
            "SELECT result,pending_stop,stop_job_id,stop_task FROM chat_commands WHERE parent=? AND incoming=?",
            (parent, stamp),
        ).fetchone()
        result, pending, stop_job_id, stop_task = row
        if result is None:
            if command == "status":
                result = self._job_status_text(thread)
            elif command == "help":
                result = self._help_text(thread)
            elif command == "model":
                result = self._model_command(thread, parse_chat_command(message.get("text") or "")[1])
            elif command in {"plan", "review"}:
                result = f"Формат: /{command} <запрос>. Режим работает без изменения файлов."
            else:
                job = self._latest_job(thread)
                if command == "reset":
                    locally_active = getattr(self, "_control_active_thread", None) == thread
                    if not locally_active and job is not None and job["state"] in ACTIVE_STATES:
                        try:
                            job["state"] = self._remote_job(job)["state"]
                            with self.db:
                                self.db.execute("UPDATE chat_jobs SET state=? WHERE thread=?", (job["state"], thread))
                        except Exception:
                            pass
                    if locally_active or (job is not None and job["state"] in ACTIVE_STATES):
                        result = "Задача ещё выполняется. Сначала отправьте /stop, затем /reset."
                    else:
                        self._reset_thread_context(thread, stamp)
                        result = "Контекст треда очищен. Следующий запрос начнётся заново."
                elif job is None or job["state"] in TERMINAL_STATES:
                    result = "В этом треде нет выполняющейся задачи."
                else:
                    try:
                        state = self._remote_job(job, "cancel")["state"]
                        pending = int(state in ACTIVE_STATES or state == "unknown")
                        stop_job_id, stop_task = job["job_id"], job["task"]
                        if state == "cancelled":
                            result = "Задача остановлена."
                        elif state in {"completed", "failed"}:
                            result = "Задача уже завершилась; отменять нечего."
                        else:
                            result = "Остановка запрошена. Подтверждение появится после завершения процесса."
                        with self.db:
                            self.db.execute("UPDATE chat_jobs SET state=? WHERE thread=?", (state, thread))
                    except Exception:
                        result = "Не удалось запросить остановку. Повторите /stop или проверьте /status."
            with self.db:
                self.db.execute(
                    "UPDATE chat_commands SET result=?,pending_stop=?,stop_job_id=?,stop_task=? "
                    "WHERE parent=? AND incoming=?",
                    (result, pending, stop_job_id, stop_task, parent, stamp),
                )
        self.status(parent, stamp, "command", result, delivery_parent=self._delivery_parent(parent, stamp))
        if pending:
            self._recover_pending_stops(thread)

    def _recover_pending_stops(self, thread: int | None = None) -> None:
        """Восстановить подтверждения /stop по UUID команды после сетевого сбоя.

        Каждый pending receipt привязан к собственным job UUID и task-route,
        а не к последнему заданию треда. Поэтому завершение следующей задачи
        не может ошибочно подтвердить остановку предыдущей. Недоступность
        worker оставляет receipt ожидающим, не объявляя остановку успешной.
        """
        rows = self.db.execute(
            "SELECT parent,incoming,thread,stop_job_id,stop_task FROM chat_commands "
            "WHERE pending_stop=1 AND stop_job_id IS NOT NULL "
            + ("AND thread=? " if thread is not None else "")
            + "ORDER BY incoming LIMIT 32",
            (thread,) if thread is not None else (),
        ).fetchall()
        outcomes = {}
        for parent, stamp, saved_thread, job_id, task in rows:
            key = (job_id, task)
            if key not in outcomes:
                try:
                    outcomes[key] = self._remote_job({"job_id": job_id, "task": task})["state"]
                except Exception:
                    outcomes[key] = None
            state = outcomes[key]
            if state in TERMINAL_STATES:
                try:
                    self._finish_stop_receipt(parent, stamp, saved_thread, state)
                except Exception as error:
                    print(f"stop_confirmation_retry {type(error).__name__}", flush=True)

    def poll(self) -> None:
        """Восстановить ожидающие подтверждения команд и выполнить обычный polling.

        Проверка отдельных stop receipts безопасна после restart gateway и не
        продвигает cursors; ошибки сети обрабатываются внутри восстановления.
        """
        self._command_schema()
        self._recover_pending_stops()
        return super().poll()

    def _poll_commands(self, thread: int, after: int) -> None:
        """Прочитать только команды активного треда, не двигая cursor его очереди.

        Обычные followup-сообщения остаются нетронутыми до завершения текущей
        задачи. Повторный проход использует command receipts и status payload,
        поэтому ответ команды не публикуется дважды и /reset не повторяется.
        """
        self._recover_pending_stops(thread)
        for stamp, message in self.messenger.new_messages(parent=thread, after=after):
            if self._is_our_reply(message):
                continue
            command = chat_command(message.get("text") or "")
            if command is not None and command not in {"plan", "review"}:
                self._handle_command(thread, stamp, message, command)

    def _begin_job(self, parent: int, stamp: int, job_id: str, task: bool, options: dict | None = None) -> dict:
        """Записать UUID выполняемого запроса для команд и восстановления после restart.

        Перезапись разрешена только внутри одного треда. Время существующего
        UUID и его модельные настройки сохраняются при повторном transport-
        запросе; /model влияет лишь на следующие UUID, не меняя replay digest.
        Метод возвращает закреплённые options; чужие треды не меняются.
        """
        self._command_schema()
        with self.db:
            self.db.execute(
                "INSERT INTO chat_jobs(thread,parent,incoming,job_id,task,started,state,options_json) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(thread) DO UPDATE SET "
                "parent=excluded.parent,incoming=excluded.incoming,job_id=excluded.job_id,"
                "task=excluded.task,started=CASE WHEN chat_jobs.job_id=excluded.job_id "
                "THEN chat_jobs.started ELSE excluded.started END,"
                "options_json=CASE WHEN chat_jobs.job_id=excluded.job_id THEN chat_jobs.options_json "
                "ELSE excluded.options_json END,state='running'",
                (parent or stamp, parent, stamp, job_id, int(task), time.time(), "running", json.dumps(options or {}, sort_keys=True)),
            )
        return json.loads(self.db.execute(
            "SELECT options_json FROM chat_jobs WHERE thread=? AND job_id=?", (parent or stamp, job_id),
        ).fetchone()[0])

    def _finish_job(self, thread: int, state: str, job_id: str | None = None) -> None:
        """Зафиксировать terminal outcome и завершить ожидающие ответы /stop.

        Состояние cancelled приходит только из подтверждённого HTTP409 worker.
        Остановка не объявляется на основании Future.cancel или сетевого сбоя.
        Идемпотентные отдельные status stages предотвращают повтор сообщения
        при повторной доставке команд и не влияют на model/delivery cursor.
        """
        job = self._latest_job(thread)
        job_id = job_id or (job["job_id"] if job else None)
        with self.db:
            self.db.execute("UPDATE chat_jobs SET state=? WHERE thread=? AND job_id=?", (state, thread, job_id))
        if state not in TERMINAL_STATES:
            return
        pending = self.db.execute(
            "SELECT parent,incoming FROM chat_commands WHERE thread=? AND pending_stop=1 AND stop_job_id=?",
            (thread, job_id),
        ).fetchall()
        for parent, stamp in pending:
            self._finish_stop_receipt(parent, stamp, thread, state)

    def _finish_stop_receipt(self, parent: int, stamp: int, thread: int, state: str) -> None:
        """Доставить финальный ответ одной команды после проверки её UUID на worker.

        UUID payload хранится Bridge.status, поэтому повтор после неизвестного
        исхода отправки не создаёт дубль. Только cancelled означает реальную
        остановку; completed/failed описывают иной подтверждённый исход.
        """
        text = "Задача остановлена." if state == "cancelled" else (
            "Задача завершилась до подтверждения остановки." if state == "completed" else
            "Задача завершилась ошибкой до подтверждения остановки."
        )
        self.status(parent, stamp, "command-final", text, delivery_parent=thread)
        with self.db:
            self.db.execute(
                "UPDATE chat_commands SET pending_stop=0 WHERE parent=? AND incoming=?",
                (parent, stamp),
            )
