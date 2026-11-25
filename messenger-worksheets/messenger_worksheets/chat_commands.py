"""Команды треда без обращения к модели и без прерывания чужих запросов."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import os
import json
import re
import time

from messenger_codex.bridge import strip_prompt_comments


COMMANDS = frozenset({"stop", "status", "reset", "help", "model", "models", "effort", "efforts", "plan", "review"})
EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
ACTIVE_STATES = frozenset({"running", "cancelling", "unknown"})
TERMINAL_STATES = frozenset({"cancelled", "completed", "failed"})
COMMAND_POLL_SECONDS = 1.0
MAX_CATALOG_PENDING = 8
EFFORT_PURPOSES = {
    "none": "без отдельного этапа размышлений, когда модель поддерживает этот режим",
    "minimal": "минимальный бюджет размышлений для быстрых простых ответов",
    "low": "небольшой бюджет для простых запросов и быстрого ответа",
    "medium": "средний бюджет для повседневных задач",
    "high": "повышенный бюджет для сложных задач",
    "xhigh": "дополнительный бюджет для более глубокого разбора сложных задач",
    "max": "максимальный бюджет размышлений для наиболее сложных задач",
    "ultra": "дополнительный режим большого бюджета; особенности определяет провайдер",
}
CATALOG_TEXT_RU = {
    "Fast, go-to model for coding and everyday work.": "Быстрая модель для программирования и повседневных задач.",
    "Our most capable model for complex coding and professional work.": "Сложное программирование и профессиональные задачи; в каталоге отнесена к наиболее мощным.",
    "Efficient model that excels at visual understanding and computer use.": "Эффективная модель для анализа изображений и работы с компьютером.",
    "Previous generation model for coding and everyday work.": "Предыдущее поколение для программирования и повседневных задач.",
    "Previous generation model for complex coding and professional work.": "Предыдущее поколение для сложного программирования и профессиональных задач.",
    "Legacy model for coding and everyday work.": "Модель предыдущих поколений для программирования и повседневных задач.",
    "Fast and affordable model for easier tasks.": "Быстрая экономичная модель для несложных задач.",
    "Legacy balanced model for agentic coding.": "Предыдущее поколение: сбалансированная работа над программными задачами с инструментами.",
    "Legacy fast model for simpler tasks.": "Предыдущее поколение: быстрая модель для простых задач.",
    "Fast responses with lighter reasoning": "Быстрые ответы с небольшим бюджетом размышлений.",
    "Balances speed and reasoning depth for everyday tasks": "Баланс скорости и глубины для повседневных задач.",
    "Greater reasoning depth for complex problems": "Более глубокий разбор сложных задач.",
    "Extra high reasoning depth for complex problems": "Дополнительная глубина разбора сложных задач.",
    "Maximum reasoning depth for the hardest problems": "Максимальная глубина для наиболее трудных задач.",
    "Maximum reasoning with automatic task delegation": "Максимальное размышление с автоматической делегацией, когда она доступна в выбранном режиме.",
}


def catalog_display_text(value: str) -> str:
    """Подготовить короткую каталожную подпись, сохранив источник и безопасную разметку.

    Известные штатные английские подписи переводятся по точным совпадениям;
    другие тексты показываются как предоставлены источником. Убирает переносы
    строк и экранирует Markdown, чтобы описание не стало ссылкой или разметкой.
    Ввода-вывода и генерации описаний языковой моделью нет.
    """
    text = " ".join(CATALOG_TEXT_RU.get(value, value).split())
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", text)


def parse_chat_command(text: str) -> tuple[str, str] | None:
    """Распознать управляющий префикс после удаления человеческих комментариев.

    Принимает только целиком совпавшие /stop, /status, /reset, /help и команды
    /model, /models, /effort, /efforts, /plan, /review с необязательным аргументом.
    Парные Markdown-обёртки допустимы
    вокруг команды или всего сообщения. Цитаты и похожие имена не совпадают.
    Функция не меняет вход и не обращается к сети или состоянию треда.
    """
    cleaned = strip_prompt_comments(text).strip()
    for wrapper in ("**", "__", "`", "*", "_"):
        if cleaned.startswith(wrapper) and cleaned.endswith(wrapper):
            cleaned = cleaned[len(wrapper):-len(wrapper)].strip()
            break
        prefix = re.match(re.escape(wrapper) + r"(/(?:model|models|effort|efforts|plan|review))" + re.escape(wrapper), cleaned, re.IGNORECASE)
        if prefix:
            cleaned = prefix[1] + cleaned[prefix.end():]
            break
    match = re.fullmatch(r"/(stop|status|reset|help|models|model|efforts|effort|plan|review)(?:\s+([\s\S]*))?", cleaned, re.IGNORECASE)
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
            for column, sql_type in (("stop_job_id", "TEXT"), ("stop_task", "INTEGER"), ("catalog_snapshot", "TEXT")):
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
            return "Формат: /model [chat|task] MODEL [none|minimal|low|medium|high|xhigh|max|ultra] либо /model default."
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

    def _effort_command(self, thread: int, arguments: str) -> str:
        """Изменить только бюджет размышлений следующих запросов выбранных маршрутов.

        Параметры: thread — текущий тред; arguments — необязательные chat/task
        и один уровень из EFFORTS. Без аргументов возвращает текущие настройки.
        Сохраняет явно выбранную модель; при отсутствии override независимо
        запрашивает реальные defaults нужных worker через capabilities. Сначала
        разрешает и проверяет все ID, затем одной SQLite-транзакцией записывает
        route 0/1: частичный сбой не меняет настройки. Ошибки metadata дают
        безопасный текст без диагностики; ошибки SQLite передаются вызывающему.
        Каталог, модель и текущие pinned job options команда не вызывает/не меняет.
        """
        parts = arguments.split()
        if not parts:
            return self._model_settings_text(thread)
        tasks = (False, True)
        if parts[0].casefold() in {"chat", "task"}:
            tasks = (parts.pop(0).casefold() == "task",)
        if len(parts) != 1 or parts[0] not in EFFORTS:
            return "Формат: /effort [chat|task] none|minimal|low|medium|high|xhigh|max|ultra. Без аргументов — текущие настройки."
        effort = parts[0]
        resolved = []
        for task in tasks:
            options = self._model_options(thread, task)
            model = options.get("model")
            if model is None:
                try:
                    route = self._job_route(task)
                    metadata = self._worker_capabilities(route.pop("endpoint"), **route)
                    model = metadata.get("model")
                except Exception:
                    model = None
            if not isinstance(model, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", model) is None:
                label = "/task" if task else "обычных сообщений"
                return (
                    f"Не удалось определить текущую модель для {label}. Настройки сохранены без изменений. "
                    "Повторите команду после восстановления worker или сначала выберите модель через /model."
                )
            resolved.append((task, model))
        with self.db:
            for task, model in resolved:
                self.db.execute(
                    "INSERT INTO chat_models VALUES(?,?,?,?) ON CONFLICT(thread,route) DO UPDATE SET "
                    "model=excluded.model,effort=excluded.effort",
                    (thread, int(task), model, effort),
                )
        chosen = "; ".join(("/task" if task else "обычные сообщения") + ": " + model for task, model in resolved)
        return (
            f"Для следующих запросов этого треда задан уровень размышлений {effort}. "
            f"Модели сохранены: {chosen}. Поддержку уровня определяет выбранная модель и провайдер."
        )

    def _current_task_route(self, thread: int) -> bool:
        """Определить текущий маршрут треда по последнему запросу после последнего reset.

        Читает только chat_jobs и лёгкий reset-barrier SQLite. Новый или
        сброшенный тред выбирает обычные ответы; task-запрос сохраняет task
        маршрут для дальнейших реплик. Файлы контекста и сеть не читаются,
        настройки и курсор не меняются. Ошибки SQLite передаются вызывающему.
        """
        job = self._latest_job(thread)
        if job is None:
            return False
        table = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_context_resets'").fetchone()
        if table:
            barrier = self.db.execute("SELECT cutoff FROM thread_context_resets WHERE thread=?", (thread,)).fetchone()
            if barrier and job["incoming"] <= barrier[0]:
                return False
        return bool(job["task"])

    def _prepare_catalog_command(self, thread: int, command: str, arguments: str) -> dict:
        """Снять маршруты и выбор модели в основном потоке до фонового HTTP.

        Принимает тред, models/efforts и аргументы. Возвращает снимок обычных
        dict без SQLite-объектов; credentials остаются только в оперативной
        памяти connection и не записываются в receipt или журнал. Неверные
        аргументы дают дешёвый usage. Ошибка одного route не скрывает другой.
        """
        selected = arguments.strip().casefold()
        if selected not in {"", "chat", "task"}:
            return {"usage": f"Формат: /{command} [chat|task]."}
        if command == "efforts":
            tasks = (selected == "task" if selected else self._current_task_route(thread),)
        else:
            tasks = (selected == "task",) if selected else (False, True)
        snapshots = []
        for task in tasks:
            options = dict(self._model_options(thread, task))
            try:
                connection = self._job_route(task)
            except Exception:
                connection = None
            snapshots.append({"task": task, "options": options, "connection": connection})
        return {"command": command, "routes": snapshots}

    def _fetch_catalog_command(self, prepared: dict) -> list:
        """Прочитать только capabilities/каталоги по готовому снимку без БД.

        Этот метод допускается в двух фоновых потоках. Выполняет штатные
        ограниченные RPC; SQLite, Messenger, настройки и delivery не читает
        и не меняет. Возвращает данные либо None при отказе без диагностики,
        адресов или credentials в пользовательском результате.
        """
        responses = []
        for snapshot in prepared["routes"]:
            response = {"metadata": {}, "catalog": None}
            connection = snapshot["connection"]
            if connection is not None:
                route = dict(connection)
                endpoint = route.pop("endpoint")
                if prepared["command"] == "efforts":
                    try:
                        response["metadata"] = self._worker_capabilities(endpoint, **route)
                    except Exception:
                        pass
                try:
                    response["catalog"] = self._worker_models(endpoint, **route)
                except Exception:
                    pass
            responses.append(response)
        return responses

    def _defer_catalog_command(self, parent: int, stamp: int, thread: int, command: str, arguments: str, *, prepared: dict | None = None) -> bool:
        """Поставить одну справку в ограниченную очередь во время активной задачи.

        Вызывается только в основном потоке после записи receipt. Не более
        восьми ожидающих команд и двух HTTP-потоков; повторный poll одного
        parent/stamp не создаёт дубль. При переполнении сохраняет и доставляет
        короткий отказ. Неверный синтаксис оставляет дешёвому sync usage.
        """
        pending = getattr(self, "_catalog_pending", None)
        if pending is None:
            pending = self._catalog_pending = {}
        key = (parent, stamp)
        if key in pending:
            return True
        prepared = prepared if prepared is not None else self._prepare_catalog_command(thread, command, arguments)
        if prepared.get("usage"):
            return False
        if len(pending) >= MAX_CATALOG_PENDING:
            result = "Справка о моделях уже загружается. Повторите команду после получения предыдущих ответов."
            with self.db:
                self.db.execute("UPDATE chat_commands SET result=? WHERE parent=? AND incoming=?", (result, parent, stamp))
            self._send_command_result(parent, stamp, result)
            return True
        snapshot = {"command": command, "routes": [
            {"task": route["task"], "options": route["options"]} for route in prepared["routes"]]}
        with self.db:
            self.db.execute("UPDATE chat_commands SET catalog_snapshot=? WHERE parent=? AND incoming=?",
                            (json.dumps(snapshot, sort_keys=True), parent, stamp))
        executor = getattr(self, "_catalog_executor", None)
        if executor is None:
            executor = self._catalog_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="worksheet-catalog")
        future = executor.submit(self._fetch_catalog_command, prepared)
        pending[key] = (future, prepared, thread)
        return True

    def _recover_catalog_commands(self) -> None:
        """Восстановить недоставленные загрузки после restart без исходных credentials.

        В основном потоке читает не более восьми сохранённых снимков выбора
        модели, проверяет их data-only форму и добавляет текущие операторские
        connection-профили. При повреждении сохраняет безопасный отказ; сеть
        выполняется только executor. Уже работающие receipts не дублируются.
        """
        rows = self.db.execute(
            "SELECT parent,incoming,thread,name,catalog_snapshot,result FROM chat_commands "
            "WHERE catalog_snapshot IS NOT NULL ORDER BY incoming LIMIT ?",
            (MAX_CATALOG_PENDING,),
        ).fetchall()
        for parent, stamp, thread, command, raw, saved in rows:
            pending = getattr(self, "_catalog_pending", {})
            if (parent, stamp) in pending:
                continue
            if saved is not None:
                try:
                    self._send_command_result(parent, stamp, saved)
                except Exception as error:
                    print(f"catalog_delivery_retry {type(error).__name__}", flush=True)
                    continue
                with self.db:
                    self.db.execute("UPDATE chat_commands SET catalog_snapshot=NULL WHERE parent=? AND incoming=?", (parent, stamp))
                continue
            if len(pending) >= MAX_CATALOG_PENDING:
                break
            try:
                snapshot = json.loads(raw)
                if (command not in {"models", "efforts"} or not isinstance(snapshot, dict)
                        or snapshot.keys() != {"command", "routes"} or snapshot["command"] != command
                        or not isinstance(snapshot["routes"], list)
                        or len(snapshot["routes"]) not in ((1,) if command == "efforts" else (1, 2))):
                    raise ValueError("invalid catalog snapshot")
                for route in snapshot["routes"]:
                    if (not isinstance(route, dict) or route.keys() != {"task", "options"}
                            or type(route["task"]) is not bool or not isinstance(route["options"], dict)
                            or not route["options"].keys() <= {"model", "reasoning_effort"}):
                        raise ValueError("invalid catalog route")
                    options = route["options"]
                    if ("model" in options and (not isinstance(options["model"], str)
                            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", options["model"]) is None)):
                        raise ValueError("invalid catalog model")
                    if "reasoning_effort" in options and options["reasoning_effort"] not in EFFORTS:
                        raise ValueError("invalid catalog effort")
                    try:
                        route["connection"] = self._job_route(route["task"])
                    except Exception:
                        route["connection"] = None
                self._defer_catalog_command(parent, stamp, thread, command, "", prepared=snapshot)
            except (ValueError, TypeError):
                result = "Не удалось восстановить справку о модели. Повторите команду."
                with self.db:
                    self.db.execute("UPDATE chat_commands SET result=?,catalog_snapshot=NULL WHERE parent=? AND incoming=?",
                                    (result, parent, stamp))
                self._send_command_result(parent, stamp, result)

    def _flush_catalog_commands(self) -> None:
        """Сохранить и доставить готовые справки только в основном SQLite-потоке.

        Каталожные HTTP-запросы здесь не запускаются: future читается после done. Ответ
        сначала фиксируется в receipt, затем использует идемпотентную delivery;
        ошибка отправки оставляет готовую запись для следующего poll. Снимок
        выбора модели сохраняется, даже если /model изменён во время загрузки.
        """
        pending = getattr(self, "_catalog_pending", {})
        for (parent, stamp), (future, prepared, thread) in list(pending.items()):
            if not future.done():
                continue
            row = self.db.execute("SELECT result FROM chat_commands WHERE parent=? AND incoming=?", (parent, stamp)).fetchone()
            result = row[0] if row else None
            if result is None:
                try:
                    fetched = future.result()
                    render = self._models_text if prepared["command"] == "models" else self._efforts_text
                    result = render(thread, "", prepared=prepared, fetched=fetched)
                except Exception:
                    result = "Не удалось получить справку о модели. Повторите команду позже."
                with self.db:
                    self.db.execute("UPDATE chat_commands SET result=? WHERE parent=? AND incoming=?", (result, parent, stamp))
            try:
                self._send_command_result(parent, stamp, result)
            except Exception as error:
                print(f"catalog_delivery_retry {type(error).__name__}", flush=True)
                continue
            with self.db:
                self.db.execute("UPDATE chat_commands SET catalog_snapshot=NULL WHERE parent=? AND incoming=?", (parent, stamp))
            pending.pop((parent, stamp), None)

    def _efforts_text(self, thread: int, arguments: str, *, prepared: dict | None = None, fetched: list | None = None) -> str:
        """Показать уровни только выбранной модели и одного конкретного маршрута.

        Необязательный chat/task задаёт режим; без него используется режим
        последнего запроса треда, а после reset — обычный. Текущая модель и
        уровень берутся из overrides и metadata worker. Публикуются лишь
        уровни, сообщённые каталогом или официальной карточкой этой модели.
        При неизвестном наборе возвращается честное пояснение без подстановки
        универсальных восьми значений. Модель не вызывается; настройки и
        текущий job не меняются. HTTP-ошибки не раскрывают исходный текст.
        """
        prepared = prepared if prepared is not None else self._prepare_catalog_command(thread, "efforts", arguments)
        if prepared.get("usage"):
            return prepared["usage"]
        fetched = fetched if fetched is not None else self._fetch_catalog_command(prepared)
        snapshot = prepared["routes"][0]
        task, options = snapshot["task"], snapshot["options"]
        route_name = "task" if task else "chat"
        label = "задачи (/task, /plan, /review)" if task else "обычные ответы"
        metadata = fetched[0]["metadata"]
        model = options.get("model") or metadata.get("model")
        effort = options.get("reasoning_effort") or metadata.get("reasoning_effort")
        lines = [f"Режим: {label} ({route_name})."]
        backend = metadata.get("mode")
        if backend == "responses_direct":
            lines.append("Обработка: OpenAI API.")
        elif backend in {"openai", "direct_openai", "endpoint", "local_user", "sandbox_admin"}:
            lines.append("Обработка: Codex CLI.")
        if not isinstance(model, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", model) is None:
            return "\n".join([*lines, "Не удалось определить выбранную модель. Попробуйте ещё раз или выберите её через /model."])
        lines.extend((f"Выбранная модель: `{model}`.",
                      f"Уровень следующих запросов: `{effort}`." if effort else "Уровень следующих запросов сейчас не определён."))
        catalog = fetched[0]["catalog"]
        item = next((entry for entry in catalog["models"] if entry["id"] == model), None) if catalog and catalog["available"] else None
        levels = item.get("reasoning_efforts", []) if item else []
        if levels:
            lines.append("Доступные уровни этой модели:")
            meanings = item.get("reasoning_descriptions", {})
            for level in levels:
                explanation = meanings.get(level) or EFFORT_PURPOSES[level]
                suffix = " — выбран" if level == effort else ""
                lines.append(f"• `{level}`{suffix}: {catalog_display_text(explanation)}")
            if effort and effort not in levels:
                lines.append("Выбранный уровень не входит в опубликованный для этой модели набор.")
            source = item.get("reasoning_source", "")
            origin = {"codex_catalog": "каталог Codex", "openai_docs": "официальная документация OpenAI", "api_metadata": "метаданные API"}.get(source)
            if origin is None:
                origin = "каталог Codex" if catalog.get("source") == "codex_active_catalog" else "каталог провайдера"
            lines.append("Источник: " + origin + ".")
            if item.get("details_url"):
                lines.append(item["details_url"])
        else:
            lines.append("Для этой модели источник не сообщил точный набор уровней. Список сейчас недоступен.")
        lines.append(f"Изменить уровень этого режима: /effort {route_name} уровень.")
        lines.append("Настройка действует на следующие запросы. Доступность делегации задаётся режимом; больший бюджет может увеличить время и стоимость.")
        return "\n".join(lines)

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
            "/models [chat|task] — каталоги моделей для обычных ответов и задач.",
            "/effort [chat|task] УРОВЕНЬ — размышления следующих запросов с сохранением модели; без аргументов — текущие настройки.",
            "/efforts [chat|task] — уровни выбранной модели текущего режима и их назначение.",
            "/plan <запрос> — составить план без изменения файлов.",
            "/review <запрос> — проверить код или материалы без исправлений.",
            "",
        ]
        lines.append(self._model_settings_text(thread))
        return "\n".join(lines)

    def _models_text(self, thread: int, arguments: str, *, prepared: dict | None = None, fetched: list | None = None) -> str:
        """Показать каталоги обоих маршрутов, reasoning и выбранную модель треда.

        Необязательный аргумент chat/task выбирает один маршрут; другие значения
        возвращают usage без сети. Сбой одного worker не скрывает второй список.
        Имена и уровни предварительно проверены RPC schema. Каталог не является
        проверкой доступа к каждой модели и так обозначается участникам.
        """
        prepared = prepared if prepared is not None else self._prepare_catalog_command(thread, "models", arguments)
        if prepared.get("usage"):
            return prepared["usage"]
        fetched = fetched if fetched is not None else self._fetch_catalog_command(prepared)
        lines = []
        for snapshot, response in zip(prepared["routes"], fetched):
            task = snapshot["task"]
            label = "Обычные ответы" if not task else "/task, /plan, /review"
            if lines:
                lines.append("")
            lines.append(label + ":")
            try:
                catalog = response["catalog"]
                if catalog is None:
                    raise ValueError("catalog unavailable")
                if not catalog["available"]:
                    lines.append("Каталог сейчас недоступен. Повторите /models позже.")
                    continue
                source = "OpenAI Models API" if catalog["source"] == "openai_models_api" else "Codex CLI"
                lines.append("Источник: " + source + ".")
                options = snapshot["options"]
                current = options.get("model")
                for item in catalog["models"]:
                    chosen = item["id"] == current if current else item["is_default"]
                    line = "• `" + item["id"] + "`" + (" — выбрана" if chosen else "")
                    if item["reasoning_efforts"]:
                        line += "; размышления: " + ", ".join(item["reasoning_efforts"])
                    lines.append(line)
                    description = item.get("description")
                    lines.append("  Назначение: " + catalog_display_text(description) if description else "  Назначение: источник не сообщил описание.")
                    features = []
                    context = item.get("context_window")
                    maximum = item.get("max_context_window")
                    if context:
                        features.append("контекст модели " + f"{context:,}".replace(",", " ") + " токенов")
                    if maximum and maximum != context:
                        features.append("максимум в каталоге " + f"{maximum:,}".replace(",", " "))
                    modalities = {"text": "текст", "image": "изображения", "audio": "аудио", "video": "видео"}
                    if item.get("input_modalities"):
                        features.append("вход: " + ", ".join(modalities[value] for value in item["input_modalities"]))
                    if features:
                        lines.append("  Особенности: " + "; ".join(features) + ".")
                    elif not item["reasoning_efforts"]:
                        lines.append("  Особенности: подробные характеристики источник не сообщил.")
                    if item.get("details_url"):
                        lines.append("  Карточка: " + item["details_url"])
                if not catalog["models"]:
                    lines.append("Провайдер не вернул текстовых моделей.")
                if current and not any(item["id"] == current for item in catalog["models"]):
                    lines.append("Выбрана `" + current + "`; в этом каталоге её нет.")
                if catalog["truncated"]:
                    lines.append("Каталог показан не полностью из-за лимита размера ответа.")
                if catalog["source"] == "openai_models_api":
                    lines.append("Описания и уровни дополняются по официальной документации: Models API их не перечисляет.")
            except Exception:
                lines.append("Не удалось получить каталог моделей. Повторите /models позже.")
        lines.append("\nСписок получен из каталога. Доступ к конкретной модели проверяется при запуске.")
        lines.append("Выбор: /model [chat|task] имя [уровень].")
        lines.append("Особенности относятся к модели; доступные инструменты и предел истории задаются настройками чата.")
        return "\n".join(lines)

    def _send_command_result(self, parent: int, stamp: int, text: str) -> None:
        """Доставить полный ответ команды безопасными идемпотентными частями.

        Короткий ответ сохраняет прежний stage command. Длинный каталог делится
        по строкам на сообщения до 5000 символов; stage каждой части стабилен.
        Повтор после сбоя отправляет только недоставленные части и не вызывает
        модель. Ошибки доставки передаются, сохраняя receipt команды в SQLite.
        """
        destination = self._delivery_parent(parent, stamp)
        if len(text) <= 6000:
            self.status(parent, stamp, "command", text, delivery_parent=destination)
            return
        chunks, current = [], ""
        for line in text.splitlines(keepends=True):
            if len(current) + len(line) > 5000 and current:
                chunks.append(current)
                current = ""
            while len(line) > 5000:
                chunks.append(line[:5000])
                line = line[5000:]
            current += line
        if current:
            chunks.append(current)
        for index, chunk in enumerate(chunks):
            self.status(parent, stamp, f"command-{index}", chunk, delivery_parent=destination)

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
            if (parent, stamp) in getattr(self, "_catalog_pending", {}):
                return
            if command in {"models", "efforts"} and getattr(self, "_control_active_thread", None) is not None:
                arguments = parse_chat_command(message.get("text") or "")[1]
                if self._defer_catalog_command(parent, stamp, thread, command, arguments):
                    return
            if command == "status":
                result = self._job_status_text(thread)
            elif command == "help":
                result = self._help_text(thread)
            elif command == "model":
                result = self._model_command(thread, parse_chat_command(message.get("text") or "")[1])
            elif command == "models":
                result = self._models_text(thread, parse_chat_command(message.get("text") or "")[1])
            elif command == "effort":
                result = self._effort_command(thread, parse_chat_command(message.get("text") or "")[1])
            elif command == "efforts":
                result = self._efforts_text(thread, parse_chat_command(message.get("text") or "")[1])
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
        self._send_command_result(parent, stamp, result)
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
        Готовые фоновые справки доставляются и после завершения model job.
        """
        self._command_schema()
        self._recover_pending_stops()
        self._recover_catalog_commands()
        self._flush_catalog_commands()
        return super().poll()

    def _poll_commands(self, thread: int, after: int) -> None:
        """Прочитать только команды активного треда, не двигая cursor его очереди.

        Обычные followup-сообщения остаются нетронутыми до завершения текущей
        задачи. Повторный проход использует command receipts и status payload,
        поэтому ответ команды не публикуется дважды и /reset не повторяется.
        /stop из пачки имеет приоритет; models/efforts загружаются в фоне и
        их готовые ответы доставляются после обработки управляющих сообщений.
        """
        self._recover_pending_stops(thread)
        controls = []
        for stamp, message in self.messenger.new_messages(parent=thread, after=after):
            if self._is_our_reply(message):
                continue
            command = chat_command(message.get("text") or "")
            if command is not None and command not in {"plan", "review"}:
                controls.append((stamp, message, command))
        # Остановка из уже прочитанной пачки обрабатывается до справок и delivery.
        controls.sort(key=lambda item: item[2] != "stop")
        for stamp, message, command in controls:
            self._handle_command(thread, stamp, message, command)
        self._recover_catalog_commands()
        self._flush_catalog_commands()

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
