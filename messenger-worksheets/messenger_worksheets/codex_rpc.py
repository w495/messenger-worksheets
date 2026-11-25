"""Private, authenticated transport for Codex jobs and bounded results."""

from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import sqlite3
import stat
import subprocess
from tempfile import NamedTemporaryFile
import threading
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, Request as HTTPRequest, build_opener
import uuid

from .codex_worker import CodexExecutionError, MAX_HISTORY_BYTES, MAX_IMAGE_BYTES, MAX_RESULT_BYTES, Request, execute
from .model_http import JobCancelled, JobCleanupFailed, cancellation_scope, check_cancellation
from .worksheet import NoRedirect


JOB_PATH = "/v1/codex-worksheet-jobs"
MAX_INPUT_BYTES = ((MAX_IMAGE_BYTES + MAX_HISTORY_BYTES + 2) // 3) * 4 + 512 * 1024
MAX_OUTPUT_BYTES = ((MAX_RESULT_BYTES + 2) // 3) * 4 + 128 * 1024
TRANSPORT_KEY = "WORKSHEET_CODEX_TRANSPORT_TOKEN"
TASK_RPC_TIMEOUT_ENV = "WORKSHEET_CODEX_TASK_RPC_TIMEOUT"
CHAT_RPC_TIMEOUT_ENV = "WORKSHEET_CODEX_CHAT_RPC_TIMEOUT"
MAX_CONCURRENT_JOBS_ENV = "WORKSHEET_CODEX_MAX_CONCURRENT_JOBS"
MAX_CACHE_FILES = 256
MAX_CACHE_BYTES = 1024 * 1024 * 1024
IDEMPOTENCY_MAX_PAGES = 16_384
TERMINAL_FAILURE_STATUSES = {
    "worker_timeout": 504,
    "worker_execution_failed": 503,
    "worker_cancelled": 409,
}
_ACTIVE_JOBS: dict[tuple[str, str], threading.Event] = {}
_ACTIVE_LOCK = threading.RLock()
JOB_STATES = {"unknown", "running", "cancelling", "cancelled", "completed", "failed"}


class WorkerJobFailure(RuntimeError):
    """Представить сохранённый terminal failure безопасным фиксированным кодом."""

    def __init__(self, error_code: str):
        """Создать сохранённую ошибку модели с фиксированным HTTP-статусом.

        Параметр error_code — worker_timeout либо worker_execution_failed.
        Объект хранит этот код и status 504/503; другой код вызывает ValueError.
        Исходные исключения, prompt и stderr в объект не копируются.
        """
        if error_code not in TERMINAL_FAILURE_STATUSES:
            raise ValueError("invalid worker failure code")
        super().__init__(error_code)
        self.error_code = error_code
        self.status = TERMINAL_FAILURE_STATUSES[error_code]


def check_size(data: bytes, maximum: int) -> bytes:
    """Проверить размер сериализованного RPC-сообщения и вернуть его без копирования.

    Параметры: data — RPC bytes; maximum — максимум байтов/значение.
    Результат: Исходный bytes-объект, если len(data) не превышает maximum.
    Исключения: ValueError при превышении maximum.
    Побочные эффекты: Побочных эффектов нет.
    Безопасность: Проверка выполняется до JSON decode/HTTP write и не создаёт дополнительную неограниченную копию."""
    if len(data) > maximum:
        raise ValueError("RPC message exceeds its limit")
    return data


def bounded_environment_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Прочитать целую настройку окружения строго в разрешённом диапазоне.

    Параметры: name — имя env; default — значение по умолчанию; minimum — нижняя граница; maximum — максимум байтов/значение.
    Результат: Целое значение env либо default в диапазоне minimum..maximum.
    Исключения: ValueError для нецелой строки или выхода из диапазона.
    Побочные эффекты: Читает одну переменную окружения.
    Безопасность: Не принимает неограниченные timeout/concurrency значения и не делает silent clamp."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in range {minimum}..{maximum}")
    return value


def rpc_timeout(job: dict) -> int:
    """Выбрать ограниченный сетевой timeout отдельно для чата и файловой задачи.

    Параметры: job — data-only job.
    Результат: Task или chat HTTP timeout в секундах.
    Исключения: ValueError из bounded_environment_int при неверной конфигурации.
    Побочные эффекты: Читает соответствующую переменную окружения.
    Безопасность: Выбор зависит только от строгого job.task is True; итог ограничен 1..3660."""
    task = job.get("task") is True
    return bounded_environment_int(
        TASK_RPC_TIMEOUT_ENV if task else CHAT_RPC_TIMEOUT_ENV,
        1860,
        1,
        3660,
    )


def submit(
    endpoint: str,
    job_id: str,
    job: dict,
    *,
    transport_token: str | None = None,
    ca_file: str | None = None,
) -> dict:
    """Аутентифицированно отправить job на HTTPS worker и разобрать ограниченный JSON-ответ.

    Параметры: endpoint — HTTPS worker endpoint; job_id — канонический UUID job; job — data-only job; transport_token — profile bearer token или None; ca_file — profile CA path или None.
    Результат: JSON-объект ответа worker размером не более MAX_OUTPUT_BYTES.
    Исключения: ValueError для endpoint/входного или выходного размера/timeout; KeyError для отсутствующего token/CA; ssl.SSLError, HTTPError/URLError и JSONDecodeError при TLS/HTTP/JSON сбое.
    Побочные эффекты: Создаёт TLS context/opener и выполняет один HTTPS POST с bearer token.
    Безопасность: Проверяет точный HTTPS JOB_PATH без userinfo/query/fragment, запрещает redirect и использует переданный profile ca_file либо настроенный CA."""
    url = urlsplit(endpoint)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or url.path != JOB_PATH):
        raise ValueError("invalid worksheet worker address")
    body = check_size(json.dumps({"job_id": job_id, "input": job}, ensure_ascii=False).encode(), MAX_INPUT_BYTES)
    context = ssl.create_default_context(cafile=ca_file or os.environ["WORKSHEET_CODEX_WORKER_CA"])
    opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
    request = HTTPRequest(endpoint, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + (transport_token or os.environ[TRANSPORT_KEY]),
    })
    with opener.open(request, timeout=rpc_timeout(job)) as result:
        return json.loads(check_size(result.read(MAX_OUTPUT_BYTES + 1), MAX_OUTPUT_BYTES))


def control_request(
    endpoint: str, suffix: str, *, method: str,
    transport_token: str | None = None, ca_file: str | None = None,
    max_bytes: int = 16 * 1024, timeout: int = 10,
) -> dict:
    """Выполнить короткий управляющий запрос к тому же проверенному HTTPS worker.

    Параметры: endpoint — настроенный JOB_PATH, suffix — внутренний маршрут,
    method — GET/POST; token/CA выбирают тот же профиль, что и submit. Возвращает
    JSON-объект не более max_bytes (по умолчанию 16 KiB, максимум 128 KiB).
    Timeout ограничен 1..30 секундами. ValueError, HTTP/TLS/JSON-ошибки передаются
    вызывающему. Redirect запрещён; credentials не попадают в URL или журнал.
    """
    url = urlsplit(endpoint)
    if type(max_bytes) is not int or not 1 <= max_bytes <= 128 * 1024 or type(timeout) is not int or not 1 <= timeout <= 30:
        raise ValueError("invalid control limits")
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or url.path != JOB_PATH):
        raise ValueError("invalid worksheet worker address")
    context = ssl.create_default_context(cafile=ca_file or os.environ["WORKSHEET_CODEX_WORKER_CA"])
    opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
    request = HTTPRequest(endpoint + suffix, method=method,
                          data=b"" if method == "POST" else None, headers={
        "Authorization": "Bearer " + (transport_token or os.environ[TRANSPORT_KEY]),
    })
    with opener.open(request, timeout=timeout) as response:
        result = json.loads(check_size(response.read(max_bytes + 1), max_bytes))
    if not isinstance(result, dict):
        raise ValueError("invalid worker control result")
    return result


def worker_models(
    endpoint: str, *, transport_token: str | None = None, ca_file: str | None = None,
) -> dict:
    """Получить очищенный каталог моделей конкретного worker по его TLS-профилю.

    Параметры соответствуют submit; произвольный provider URL из чата не
    принимается. Возвращает каталог после строгой проверки безопасной schema.
    Ответ ограничен 128 KiB и 25 секундами; ошибки сети и schema передаются,
    а недоступный каталог не подменяется встроенным списком моделей.
    """
    from .model_catalog import validate_catalog

    result = control_request(endpoint, "/models", method="GET",
                             transport_token=transport_token, ca_file=ca_file,
                             max_bytes=128 * 1024, timeout=25)
    return validate_catalog(result)


def job_control(
    endpoint: str, job_id: str, *, action: str = "status",
    transport_token: str | None = None, ca_file: str | None = None,
) -> dict:
    """Прочитать статус UUID либо запросить отмену только этой задачи.

    Параметры: endpoint/token/CA — профиль worker; job_id — канонический UUID;
    action — status/cancel. Возвращает проверенные job_id/state и безопасные
    времена/код ошибки. Отмена может вернуть cancelling: остановка подтверждена
    только state=cancelled. Неверные аргументы/ответ вызывают ValueError;
    сетевые ошибки не скрываются и не трактуются как успешная остановка.
    """
    if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
        raise ValueError("invalid job identifier")
    if action not in {"status", "cancel"}:
        raise ValueError("invalid job control action")
    result = control_request(
        endpoint, "/" + job_id + ("/cancel" if action == "cancel" else ""),
        method="POST" if action == "cancel" else "GET",
        transport_token=transport_token, ca_file=ca_file,
    )
    if (result.keys() != {"job_id", "state", "created_at", "updated_at", "error_code"}
            or result.get("job_id") != job_id or result.get("state") not in JOB_STATES
            or any(value is not None and type(value) is not int
                   for value in (result.get("created_at"), result.get("updated_at")))
            or result.get("error_code") not in {None, *TERMINAL_FAILURE_STATUSES}):
        raise ValueError("invalid worker job status")
    return result


def worker_capabilities(
    endpoint: str, *, transport_token: str | None = None, ca_file: str | None = None,
) -> dict:
    """Прочитать действующие model/effort/mode worker без выдачи приватных настроек.

    Параметры задают тот же профиль, что submit. Возвращает только четыре
    allowlist-поля; отсутствующие model/effort остаются None. Повреждённый ответ
    вызывает ValueError; сетевые ошибки передаются. Не загружает конфиги шлюза
    и не раскрывает endpoint модели, ключи, HOME или пути пользователя.
    """
    result = control_request(endpoint, "/capabilities", method="GET",
                             transport_token=transport_token, ca_file=ca_file)
    if (result.keys() != {"model", "reasoning_effort", "mode", "task_supported"}
            or result["mode"] not in {"responses_direct", "openai", "direct_openai", "endpoint", "local_user", "sandbox_admin"}
            or type(result["task_supported"]) is not bool
            or any(value is not None and (not isinstance(value, str) or not value or len(value) > 100)
                   for value in (result["model"], result["reasoning_effort"]))):
        raise ValueError("invalid worker capabilities")
    return result


def effective_capabilities() -> dict:
    """Вернуть только настройки, фактически передаваемые модели текущим worker.

    Не принимает параметров. Model/effort берутся из рабочего env; native CLI
    запускается с --ignore-user-config, поэтому изменения ~/.codex/config.toml
    не выдаются за применённые до provisioning/restart. Для CLI без override
    возвращается None, поскольку собственный default CLI здесь неизвестен.
    API direct имеет явный default effort=medium. Побочных эффектов нет.
    """
    mode = os.environ.get("WORKSHEET_CODEX_PROVIDER", "endpoint")
    if mode not in {"responses_direct", "openai", "direct_openai", "endpoint", "local_user", "sandbox_admin"}:
        raise ValueError("unsupported worksheet Codex provider")
    model = os.environ.get("WORKSHEET_CODEX_MODEL") or None
    effort = os.environ.get("WORKSHEET_CODEX_REASONING_EFFORT") or (
        "medium" if mode == "responses_direct" else None
    )
    if model is not None and (len(model) > 100 or any(not (c.isascii() and (c.isalnum() or c in "_.-")) for c in model)):
        raise ValueError("invalid effective model")
    if effort is not None and effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError("invalid effective effort")
    return {"model": model, "reasoning_effort": effort, "mode": mode,
            "task_supported": mode != "responses_direct"}


def prune_result_cache(cache: Path, keep: Path) -> None:
    """Ограничить число и суммарный размер сохранённых RPC results.

    Параметры: cache — каталог cache; keep — текущий result.
    Результат: Ничего; keep сохранён, а остальные cache entries уложены в retention насколько позволяет порядок обхода.
    Исключения: OSError при glob/lstat/unlink.
    Побочные эффекты: Удаляет старейшие regular JSON сверх 256 файлов или 1 GiB.
    Безопасность: Учитывает только regular nlink=1, игнорирует symlink/non-regular и никогда не удаляет keep."""

    entries = []
    for path in cache.glob("*.json"):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            entries.append((metadata.st_mtime_ns, path, metadata.st_size))
    entries.sort(reverse=True)
    files = 0
    total = 0
    for _, path, size in entries:
        files += 1
        total += size
        if path == keep or (files <= MAX_CACHE_FILES and total <= MAX_CACHE_BYTES):
            continue
        path.unlink(missing_ok=True)


def idempotency_database(state_dir: Path) -> sqlite3.Connection:
    """Открыть bounded SQLite-журнал UUID/digest для fail-closed replay.

    Параметры:
        state_dir: Приватный StateDirectory worker.

    Результат:
        Соединение с таблицей jobs и жёстким пределом 16384 страниц.

    Побочные эффекты:
        Создаёт либо открывает ``idempotency.sqlite3`` с mode 0600.

    Исключения:
        Ошибка SQLite, включая достижение page limit, пробрасывается до RPC и
        не позволяет выполнить job без предварительной фиксации UUID/digest.

    Инварианты безопасности:
        Tombstone содержит только UUID/digest, state и фиксированный error_code; удаление тяжёлого
        cache result не разрешает повторно выполнить тот же UUID.
    """

    path = state_dir / "idempotency.sqlite3"
    database = sqlite3.connect(path, timeout=30)
    os.chmod(path, 0o600)
    database.execute(f"PRAGMA max_page_count={IDEMPOTENCY_MAX_PAGES}")
    try:
        # Разные UUID имеют разные flock: миграция сериализуется самой SQLite.
        with database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "job_id TEXT PRIMARY KEY, digest TEXT NOT NULL, created_at INTEGER NOT NULL, "
                "state TEXT NOT NULL DEFAULT 'unknown', error_code TEXT)"
            )
            columns = {row[1] for row in database.execute("PRAGMA table_info(jobs)")}
            if "state" not in columns:
                database.execute("ALTER TABLE jobs ADD COLUMN state TEXT NOT NULL DEFAULT 'unknown'")
            if "error_code" not in columns:
                database.execute("ALTER TABLE jobs ADD COLUMN error_code TEXT")
            if "updated_at" not in columns:
                database.execute("ALTER TABLE jobs ADD COLUMN updated_at INTEGER")
    except Exception:
        database.close()
        raise

    return database


def job_state(state_dir: Path, job_id: str, *, cancel: bool = False) -> dict:
    """Прочитать состояние job либо атомарно запросить его отмену по UUID.

    Параметры: state_dir — приватное состояние worker, job_id — канонический
    UUID, cancel — запрос остановки. Возвращает безопасный статус с временами.
    Отмена неизвестного UUID создаёт tombstone, не позволяющий позднему submit
    запустить job. Активный Event отмечается только для этого state-dir/UUID;
    cancelled выставляется исполнителем после cleanup. Старый running без
    локального исполнителя возвращает unknown, не обещая остановку. Ошибки
    SQLite/ФС и UUID передаются; команды/credentials/файлы результата не читаются.
    """
    if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
        raise ValueError("invalid job identifier")
    key = (str(state_dir.resolve()), job_id)
    with _ACTIVE_LOCK, closing(idempotency_database(state_dir)) as database:
        row = database.execute(
            "SELECT state,created_at,updated_at,error_code FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        active = _ACTIVE_JOBS.get(key)
        if cancel and row is None:
            database.execute(
                "INSERT INTO jobs(job_id,digest,created_at,state,error_code,updated_at) "
                "VALUES(?,'',unixepoch(),'cancelled','worker_cancelled',unixepoch())", (job_id,),
            )
            database.commit()
        elif cancel and row[0] in {"running", "cancelling"} and active is not None:
            database.execute(
                "UPDATE jobs SET state='cancelling',updated_at=unixepoch() WHERE job_id=?", (job_id,),
            )
            database.commit()
            active.set()
        row = database.execute(
            "SELECT state,created_at,updated_at,error_code FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if row is None:
            state, created, updated, error = "unknown", None, None, None
        else:
            state, created, updated, error = row
            if state in {"running", "cancelling"} and active is None:
                state = "unknown"
            elif state == "succeeded":
                state = "completed"
            elif state not in JOB_STATES:
                state = "unknown"
        return {"job_id": job_id, "state": state, "created_at": created,
                "updated_at": updated, "error_code": error}


def run_request(body: bytes, state_dir: Path) -> dict:
    """Проверить RPC-конверт, обеспечить идемпотентность job и сохранить результат атомарно.

    Параметры: body — RPC body/JSON response; state_dir — приватный state dir.
    Результат: Результат execute либо ранее сохранённый результат идентичного job body; terminal failure повторяется без execute.
    Исключения: JSONDecodeError; ValueError для envelope/UUID/conflicting replay/expired result; WorkerJobFailure для сохранённого отказа; ошибки ФС/SQLite.
    Побочные эффекты: Создаёт results 0700, shard-lock 0600, берёт flock, выполняет job, атомарно заменяет cache-файл и запускает retention.
    Безопасность: Имя/lock выводятся только из канонического UUID; SHA-256 запрещает replay того же job_id с другим body."""
    spec = json.loads(body)
    if not isinstance(spec, dict) or spec.keys() != {"job_id", "input"}:
        raise ValueError("invalid RPC envelope")
    job_id = spec["job_id"]
    if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
        raise ValueError("invalid job identifier")
    cache = state_dir / "results"
    cache.mkdir(mode=0o700, exist_ok=True)
    destination = cache / f"{job_id}.json"
    lock_path = cache / f".lock-{uuid.UUID(job_id).int % 256:03d}"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        digest = hashlib.sha256(body).hexdigest()
        active_key = (str(state_dir.resolve()), job_id)
        with closing(idempotency_database(state_dir)) as database:
            with _ACTIVE_LOCK:
                row = database.execute(
                    "SELECT digest,state,error_code FROM jobs WHERE job_id=?", (job_id,),
                ).fetchone()
                if row is not None and row[1] == "cancelled" and row[0] == "":
                    raise WorkerJobFailure("worker_cancelled")
                if row is not None and row[0] != digest:
                    raise ValueError("conflicting job replay")
                if row is not None and row[1] in {"failed", "cancelled"}:
                    raise WorkerJobFailure(row[2])
                try:
                    stored = json.loads(destination.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    if row is not None:
                        raise ValueError("job result expired") from None
                else:
                    if stored["digest"] != digest:
                        raise ValueError("conflicting cached result")
                    return stored["result"]
                request = Request.decode(spec["input"])
                cancelled = threading.Event()
                database.execute(
                    "INSERT INTO jobs(job_id,digest,created_at,state,updated_at) "
                    "VALUES(?,?,unixepoch(),'running',unixepoch())",
                    (job_id, digest),
                )
                database.commit()
                _ACTIVE_JOBS[active_key] = cancelled
            temporary = None
            try:
                with cancellation_scope(cancelled):
                    result = execute(request, state_dir, job_id=job_id)
                    check_cancellation()
                    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache, delete=False) as stream:
                        temporary = Path(stream.name)
                        json.dump({"digest": digest, "result": result}, stream)
                    with _ACTIVE_LOCK:
                        check_cancellation()
                        temporary.replace(destination)
                        database.execute(
                            "UPDATE jobs SET state='succeeded',error_code=NULL,updated_at=unixepoch() WHERE job_id=?", (job_id,),
                        )
                        database.commit()
            except Exception as error:
                error_code = (
                    "worker_execution_failed" if isinstance(error, JobCleanupFailed)
                    else
                    "worker_cancelled" if isinstance(error, JobCancelled) or cancelled.is_set()
                    else
                    "worker_timeout" if isinstance(error, (subprocess.TimeoutExpired, TimeoutError))
                    else "worker_execution_failed"
                )
                with _ACTIVE_LOCK:
                    database.execute(
                        "UPDATE jobs SET state=?,error_code=?,updated_at=unixepoch() WHERE job_id=?",
                        ("cancelled" if error_code == "worker_cancelled" else "failed", error_code, job_id),
                    )
                    database.commit()
                raise WorkerJobFailure(error_code) from None
            finally:
                with _ACTIVE_LOCK:
                    _ACTIVE_JOBS.pop(active_key, None)
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            try:
                prune_result_cache(cache, destination)
            except OSError:
                # Необязательная очистка не меняет уже сохранённый успех.
                print("codex_result_cache_failed category=retention_io", flush=True)
            return result


class JobHandler(BaseHTTPRequestHandler):
    def authorized(self) -> bool:
        """Проверить bearer управляющего запроса и отправить 401 при несовпадении.

        Возвращает bool; compare_digest не раскрывает правильный token. Не пишет
        заголовки в журнал, читает ровно одну настройку worker. При успехе HTTP
        ответ ещё не отправлен; ошибки socket/config передаются вызывающему.
        """
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + os.environ[TRANSPORT_KEY]
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            self.send_json(401, {"error": "unauthorized"})
            return False
        return True

    def control(self, *, cancel: bool = False) -> None:
        """Обработать короткое чтение статуса/возможностей либо отмену одного UUID.

        Параметр cancel выбирает POST cancel; GET допускает также capabilities.
        Ответ 200 содержит только безопасный allowlist JSON, 401/404/422/503 —
        фиксированную ошибку. Метод не занимает job semaphore, поэтому /stop и
        /status доступны при полной загрузке. Управление чужим worker невозможно
        без его отдельного transport token; данные prompt/result не возвращаются.
        """
        if not self.authorized():
            return
        try:
            self.connection.settimeout(20)
            if self.headers.get("Transfer-Encoding") or int(self.headers.get("Content-Length", "0")) != 0:
                raise ValueError("unexpected control body")
            if not cancel and self.path == JOB_PATH + "/capabilities":
                self.send_json(200, effective_capabilities())
                return
            if not cancel and self.path == JOB_PATH + "/models":
                from .model_catalog import effective_models

                self.send_json(200, effective_models())
                return
            prefix = JOB_PATH + "/"
            suffix = "/cancel" if cancel else ""
            if not self.path.startswith(prefix) or (cancel and not self.path.endswith(suffix)):
                self.send_json(404, {"error": "unknown path"})
                return
            identifier = self.path[len(prefix):-len(suffix)] if cancel else self.path[len(prefix):]
            if not identifier or "/" in identifier:
                self.send_json(404, {"error": "unknown path"})
                return
            result = job_state(Path(os.environ["STATE_DIRECTORY"]), identifier, cancel=cancel)
            self.send_json(200, result)
        except (ValueError, TypeError, KeyError):
            self.send_json(422, {"error": "invalid control request"})
        except Exception:
            self.send_json(503, {"error": "worker control failed"})

    def do_GET(self) -> None:
        """Обслужить только аутентифицированные GET status/capabilities worker.

        Не принимает параметров; делегирует ограниченную обработку control.
        Не запускает модель, не занимает job slot и не выдаёт данные запроса.
        Ошибки записи socket могут быть переданы базовому HTTP server.
        """
        self.control()

    def log_message(self, format, *args):
        # HTTP URLs, headers, and client-supplied data are never logged.
        """Отключить стандартный HTTP-журнал, способный раскрыть URL и пользовательские данные.

        Параметры: format — access-log format; args — access-log args.
        Результат: None; стандартная запись BaseHTTPRequestHandler подавлена.
        Исключения: Не генерирует собственных исключений.
        Побочные эффекты: Преднамеренно не пишет HTTP access log.
        Безопасность: URL, headers и данные клиента не попадают в стандартный журнал."""
        return

    def send_json(self, status: int, body: dict) -> None:
        """Отправить ограниченный JSON-ответ с корректными HTTP-заголовками.

        Параметры: status — HTTP status; body — RPC body/JSON response.
        Результат: Ничего; JSON body и headers записаны в HTTP connection.
        Исключения: TypeError при несериализуемом body; ValueError при превышении MAX_OUTPUT_BYTES; OSError при записи сокета.
        Побочные эффекты: Отправляет status, Content-Type/Length и payload в wfile.
        Безопасность: Размер проверяется до send_response; лишние headers и credentials в ответ не добавляются."""
        payload = check_size(json.dumps(body, ensure_ascii=False).encode(), MAX_OUTPUT_BYTES)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        """Аутентифицировать и выполнить один POST job в пределах лимита конкурентности.

        Параметры: нет.
        Результат: Ничего; клиент получает 200, 401, 404, 422, 429, 503 или 504.
        Исключения: Ошибки валидации/worker перехватываются и преобразуются в bounded JSON; OSError send_json/connection может выйти наружу.
        Побочные эффекты: Проверяет path/token/length, захватывает semaphore, читает body, запускает run_request, пишет ответ и освобождает slot.
        Безопасность: Bearer сравнивается compare_digest; body ограничен до чтения, concurrency fail-fast, журнал содержит только тип и безопасную Codex category."""
        acquired = False
        if self.path.startswith(JOB_PATH + "/"):
            self.control(cancel=True)
            return
        if self.path != JOB_PATH:
            self.send_json(404, {"error": "unknown path"})
            return
        if not self.authorized():
            return
        try:
            self.connection.settimeout(20)
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_INPUT_BYTES:
                raise ValueError("invalid content length")
            if not self.server.job_slots.acquire(blocking=False):
                self.send_json(429, {"error": "worker concurrency limit"})
                return
            acquired = True
            result = run_request(self.rfile.read(size), Path(os.environ["STATE_DIRECTORY"]))
            self.send_json(200, result)
        except WorkerJobFailure as error:
            print(f"codex_job_failed code={error.error_code}", flush=True)
            self.send_json(error.status, {"error": error.error_code})
        except (ValueError, TypeError, KeyError):
            self.send_json(422, {"error": "invalid job"})
        except Exception as error:
            category = f" category={error.category}" if isinstance(error, CodexExecutionError) else ""
            print(f"codex_job_failed {type(error).__name__}{category}", flush=True)
            self.send_json(503, {"error": "worker error"})
        finally:
            if acquired:
                self.server.job_slots.release()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Параллельный loopback worker с жёстким лимитом активных jobs."""

    daemon_threads = True

    def __init__(self, server_address, handler_class, *, max_concurrent_jobs: int):
        """Создать потоковый HTTPS-сервер с жёстким семафором активных jobs.

        Параметры: server_address — listen address; handler_class — HTTP handler class; max_concurrent_jobs — лимит jobs.
        Результат: Инициализированный ThreadingHTTPServer с BoundedSemaphore job_slots.
        Исключения: ValueError для неположительного max_concurrent_jobs; OSError при bind/listen.
        Побочные эффекты: Создаёт semaphore и слушающий socket базового сервера.
        Безопасность: Каждый handler обязан получить slot; daemon threads не обходят общий жёсткий лимит."""
        self.job_slots = threading.BoundedSemaphore(max_concurrent_jobs)
        super().__init__(server_address, handler_class)


def main() -> None:
    """Запустить TLS worker с проверенным токеном, сертификатом и лимитом конкурентности.

    Параметры: нет.
    Результат: Ничего; TLS server обслуживает запросы до shutdown/прерывания.
    Исключения: KeyError/ValueError конфигурации; OSError bind; ssl.SSLError сертификата; исключения serve_forever.
    Побочные эффекты: Устанавливает umask 077, создаёт server, загружает cert/key, оборачивает socket TLS и запускает loop.
    Безопасность: Требует transport token >=32, TLS >=1.2 и concurrency 1..16; private key не читается приложением как текст."""
    os.umask(0o077)
    if len(os.environ[TRANSPORT_KEY]) < 32:
        raise ValueError("transport token is too short")
    address = os.environ["WORKSHEET_CODEX_LISTEN"]
    port = int(os.environ.get("WORKSHEET_CODEX_PORT", "8443"))
    max_jobs = bounded_environment_int(MAX_CONCURRENT_JOBS_ENV, 4, 1, 16)
    with BoundedThreadingHTTPServer(
        (address, port), JobHandler, max_concurrent_jobs=max_jobs,
    ) as server:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(os.environ["WORKSHEET_CODEX_TLS_CERT"],
                                os.environ["WORKSHEET_CODEX_TLS_KEY"])
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
