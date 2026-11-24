"""Выполнить запрос модели в отдельном процессе с общим жёстким deadline."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


AUTHORIZATION_ENV = "WORKSHEET_MODEL_HTTP_AUTHORIZATION"
TRANSPORT_ENV = (
    "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "no_proxy",
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Отклонить HTTP-перенаправление без обращения к новому адресу.

        Параметры передаёт urllib при обработке redirect. Всегда возвращает None,
        чтобы запрос с Authorization не был отправлен на другой origin.
        Сеть и состояние метод не изменяет.
        """
        return None


def checked_endpoint(endpoint: str) -> str:
    """Проверить адрес модели до запуска дочернего процесса.

    Параметр endpoint — настроенный оператором URL. Возвращает его без изменения
    либо выбрасывает ValueError для HTTP, отсутствующего host, userinfo, query
    или fragment. Не обращается к сети и не изменяет состояние.
    """
    url = urlsplit(endpoint)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ValueError("invalid configured model endpoint")
    return endpoint


def post_model_response(request: Request, *, timeout: float, max_bytes: int) -> bytes:
    """Получить ограниченное тело модели до общего deadline, уничтожив процесс при timeout.

    Параметры: request — HTTPS-запрос модели; timeout — общий бюджет в секундах;
    max_bytes — максимум тела без контрольного дополнительного байта.
    Результат: Не более max_bytes + 1 байта для проверки размера вызывающим кодом.
    Исключения: TimeoutExpired при истечении бюджета; безопасные HTTPError/URLError;
    ValueError для endpoint или ограничений.
    Безопасность: Authorization передаётся только через env ребёнка; stderr закрыт,
    остальные credentials окружения не наследуются. При отказе процесс завершён и собран.
    """
    deadline = time.monotonic() + timeout
    endpoint = checked_endpoint(request.full_url)
    if timeout <= 0 or max_bytes < 1:
        raise ValueError("invalid model transport limits")
    headers = {key: value for key, value in request.header_items()
               if key.casefold() != "authorization"}
    payload = json.dumps({
        "url": endpoint, "method": request.get_method(), "headers": headers,
        "body": base64.b64encode(request.data or b"").decode("ascii"),
        "timeout": timeout, "max_bytes": max_bytes,
    }).encode()
    environment = {key: os.environ[key] for key in TRANSPORT_ENV if key in os.environ}
    environment[AUTHORIZATION_ENV] = request.get_header("Authorization", "")
    argv = [sys.executable, "-I", str(Path(__file__).resolve())]
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(argv, timeout)
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=environment, start_new_session=True,
    )
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        output, _ = process.communicate(payload, timeout=remaining)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
    if process.returncode:
        raise URLError("model transport failed")
    try:
        result = json.loads(output)
        if result.get("status") == "http_error" and type(result.get("code")) is int:
            raise HTTPError(endpoint, result["code"], "model HTTP failure", None, None)
        if result.get("status") != "ok":
            raise ValueError("transport failure")
        body = base64.b64decode(result["body"], validate=True)
        if len(body) > max_bytes + 1:
            raise ValueError("invalid transport body size")
        return body
    except (ValueError, KeyError, TypeError, AttributeError):
        raise URLError("model transport failed") from None


def child_response(payload: dict) -> dict:
    """Исполнить один HTTPS-запрос дочернего процесса без журналирования его данных.

    Параметр payload содержит URL, метод, обычные заголовки, base64-тело,
    сетевой timeout и предел ответа. Authorization берётся отдельно из env.
    Возвращает ограниченное base64-тело либо фиксированный тип ошибки и HTTP-код.
    Сетевые ошибки и ошибки входа не пробрасываются с исходной диагностикой.
    Выполняет один сетевой запрос; redirects запрещены, тело HTTP-ошибки не читается.
    """
    try:
        headers = dict(payload["headers"])
        headers["Authorization"] = os.environ.get(AUTHORIZATION_ENV, "")
        request = Request(
            checked_endpoint(payload["url"]), method=payload["method"], headers=headers,
            data=base64.b64decode(payload["body"], validate=True),
        )
        with build_opener(NoRedirect()).open(request, timeout=payload["timeout"]) as response:
            body = response.read(payload["max_bytes"] + 1)
        return {"status": "ok", "body": base64.b64encode(body).decode("ascii")}
    except HTTPError as error:
        return {"status": "http_error", "code": error.code}
    except Exception:
        return {"status": "connection_error"}


def main() -> None:
    """Обслужить один внутренний запрос из stdin и завершить дочерний процесс.

    Не принимает аргументов. Читает JSON, вызывает child_response и пишет только
    ответ внутреннего протокола в stdout. При повреждённом входе возвращает
    connection_error без traceback, URL, заголовков или значения токена.
    """
    try:
        result = child_response(json.load(sys.stdin.buffer))
    except Exception:
        result = {"status": "connection_error"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
