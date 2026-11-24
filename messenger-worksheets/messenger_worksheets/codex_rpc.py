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
from tempfile import NamedTemporaryFile
import threading
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, Request as HTTPRequest, build_opener
import uuid

from .codex_worker import CodexExecutionError, MAX_IMAGE_BYTES, MAX_RESULT_BYTES, Request, execute
from .worksheet import NoRedirect


JOB_PATH = "/v1/codex-worksheet-jobs"
MAX_INPUT_BYTES = ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 128 * 1024
MAX_OUTPUT_BYTES = ((MAX_RESULT_BYTES + 2) // 3) * 4 + 128 * 1024
TRANSPORT_KEY = "WORKSHEET_CODEX_TRANSPORT_TOKEN"
TASK_RPC_TIMEOUT_ENV = "WORKSHEET_CODEX_TASK_RPC_TIMEOUT"
CHAT_RPC_TIMEOUT_ENV = "WORKSHEET_CODEX_CHAT_RPC_TIMEOUT"
MAX_CONCURRENT_JOBS_ENV = "WORKSHEET_CODEX_MAX_CONCURRENT_JOBS"
MAX_CACHE_FILES = 256
MAX_CACHE_BYTES = 1024 * 1024 * 1024
IDEMPOTENCY_MAX_PAGES = 16_384


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
        1860 if task else 150,
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


def prune_result_cache(cache: Path, keep: Path) -> None:
    """Ограничить число и суммарный размер сохранённых RPC results.

    Параметры: cache — каталог cache; keep — текущий result.
    Результат: Ничего; keep сохранён, а остальные cache entries уложены в retention насколько позволяет порядок обхода.
    Исключения: OSError при glob/lstat/unlink.
    Побочные эффекты: Удаляет старейшие regular JSON сверх 256 файлов или 1 GiB.
    Безопасность: Учитывает только regular nlink=1, игнорирует symlink/non-regular и никогда не удаляет keep."""

    entries = []
    for path in cache.glob("*.json"):
        metadata = path.lstat()
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
        Tombstone не содержит prompt, result или credentials; удаление тяжёлого
        cache result не разрешает повторно выполнить тот же UUID.
    """

    path = state_dir / "idempotency.sqlite3"
    database = sqlite3.connect(path, timeout=30)
    os.chmod(path, 0o600)
    database.execute(f"PRAGMA max_page_count={IDEMPOTENCY_MAX_PAGES}")
    database.execute(
        "CREATE TABLE IF NOT EXISTS jobs ("
        "job_id TEXT PRIMARY KEY, digest TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    database.commit()
    return database


def run_request(body: bytes, state_dir: Path) -> dict:
    """Проверить RPC-конверт, обеспечить идемпотентность job и сохранить результат атомарно.

    Параметры: body — RPC body/JSON response; state_dir — приватный state dir.
    Результат: Результат execute либо ранее сохранённый результат идентичного job body.
    Исключения: JSONDecodeError; ValueError для envelope/UUID/conflicting replay; OSError/flock/ошибки execute и cache serialization.
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
        with closing(idempotency_database(state_dir)) as database:
            row = database.execute(
                "SELECT digest FROM jobs WHERE job_id=?", (job_id,),
            ).fetchone()
            if row is None:
                database.execute(
                    "INSERT INTO jobs(job_id,digest,created_at) "
                    "VALUES(?,?,unixepoch())",
                    (job_id, digest),
                )
                database.commit()
            elif row[0] != digest:
                raise ValueError("conflicting job replay")
            elif not destination.exists():
                raise ValueError("job result expired")
        if destination.exists():
            stored = json.loads(destination.read_text(encoding="utf-8"))
            if stored["digest"] != digest:
                raise ValueError("conflicting cached result")
            return stored["result"]
        result = execute(Request.decode(spec["input"]), state_dir, job_id=job_id)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache, delete=False) as stream:
            json.dump({"digest": digest, "result": result}, stream)
            temporary = Path(stream.name)
        temporary.replace(destination)
        prune_result_cache(cache, destination)
        return result


class JobHandler(BaseHTTPRequestHandler):
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
        Результат: Ничего; клиент получает 200, 401, 404, 422, 429 или 503.
        Исключения: Ошибки валидации/worker перехватываются и преобразуются в bounded JSON; OSError send_json/connection может выйти наружу.
        Побочные эффекты: Проверяет path/token/length, захватывает semaphore, читает body, запускает run_request, пишет ответ и освобождает slot.
        Безопасность: Bearer сравнивается compare_digest; body ограничен до чтения, concurrency fail-fast, журнал содержит только тип и безопасную Codex category."""
        acquired = False
        if self.path != JOB_PATH:
            self.send_json(404, {"error": "unknown path"})
            return
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + os.environ[TRANSPORT_KEY]
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            self.send_json(401, {"error": "unauthorized"})
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
