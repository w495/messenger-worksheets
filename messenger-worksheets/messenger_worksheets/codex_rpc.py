"""Private, authenticated transport for Codex jobs and bounded results."""

from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import ssl
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, Request as HTTPRequest, build_opener
import uuid

from .codex_worker import MAX_IMAGE_BYTES, MAX_RESULT_BYTES, Request, execute
from .worksheet import NoRedirect


JOB_PATH = "/v1/codex-worksheet-jobs"
MAX_INPUT_BYTES = ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 128 * 1024
MAX_OUTPUT_BYTES = ((MAX_RESULT_BYTES + 2) // 3) * 4 + 128 * 1024
TRANSPORT_KEY = "WORKSHEET_CODEX_TRANSPORT_TOKEN"


def check_size(data: bytes, maximum: int) -> bytes:
    if len(data) > maximum:
        raise ValueError("RPC message exceeds its limit")
    return data


def submit(endpoint: str, job_id: str, job: dict) -> dict:
    url = urlsplit(endpoint)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or url.path != JOB_PATH):
        raise ValueError("invalid worksheet worker address")
    body = check_size(json.dumps({"job_id": job_id, "input": job}, ensure_ascii=False).encode(), MAX_INPUT_BYTES)
    context = ssl.create_default_context(cafile=os.environ["WORKSHEET_CODEX_WORKER_CA"])
    opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
    request = HTTPRequest(endpoint, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + os.environ[TRANSPORT_KEY],
    })
    with opener.open(request, timeout=330) as result:
        return json.loads(check_size(result.read(MAX_OUTPUT_BYTES + 1), MAX_OUTPUT_BYTES))


def run_request(body: bytes, state_dir: Path) -> dict:
    spec = json.loads(body)
    if not isinstance(spec, dict) or spec.keys() != {"job_id", "input"}:
        raise ValueError("invalid RPC envelope")
    job_id = spec["job_id"]
    if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
        raise ValueError("invalid job identifier")
    cache = state_dir / "results"
    cache.mkdir(mode=0o700, exist_ok=True)
    destination = cache / f"{job_id}.json"
    digest = hashlib.sha256(body).hexdigest()
    if destination.exists():
        stored = json.loads(destination.read_text(encoding="utf-8"))
        if stored["digest"] != digest:
            raise ValueError("conflicting job replay")
        return stored["result"]
    result = execute(Request.decode(spec["input"]), state_dir)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache, delete=False) as stream:
        json.dump({"digest": digest, "result": result}, stream)
        temporary = Path(stream.name)
    temporary.replace(destination)
    return result


class JobHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # HTTP URLs, headers, and client-supplied data are never logged.
        return

    def send_json(self, status: int, body: dict) -> None:
        payload = check_size(json.dumps(body, ensure_ascii=False).encode(), MAX_OUTPUT_BYTES)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
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
            result = run_request(self.rfile.read(size), Path(os.environ["STATE_DIRECTORY"]))
            self.send_json(200, result)
        except (ValueError, TypeError, KeyError):
            self.send_json(422, {"error": "invalid job"})
        except Exception as error:
            print(f"codex_job_failed {type(error).__name__}", flush=True)
            self.send_json(503, {"error": "worker error"})


def main() -> None:
    os.umask(0o077)
    if len(os.environ[TRANSPORT_KEY]) < 32:
        raise ValueError("transport token is too short")
    address = os.environ["WORKSHEET_CODEX_LISTEN"]
    port = int(os.environ.get("WORKSHEET_CODEX_PORT", "8443"))
    with HTTPServer((address, port), JobHandler) as server:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(os.environ["WORKSHEET_CODEX_TLS_CERT"],
                                os.environ["WORKSHEET_CODEX_TLS_KEY"])
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
