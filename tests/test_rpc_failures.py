from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

from messenger_worksheets import codex_rpc, codex_worker


class RpcFailuresTest(unittest.TestCase):
    def body(self, job_id=None, text="ordinary English request"):
        """Собрать валидный RPC-конверт с независимым UUID."""
        return json.dumps({
            "job_id": job_id or str(uuid.uuid4()),
            "input": {"text": text, "task": False, "session": None, "images": []},
        }).encode()

    def assert_failure(self, body, state, code, status):
        """Проверить безопасные тип, код и HTTP-статус terminal failure."""
        with self.assertRaises(codex_rpc.WorkerJobFailure) as raised:
            codex_rpc.run_request(body, state)
        self.assertEqual(raised.exception.error_code, code)
        self.assertEqual(raised.exception.status, status)
        self.assertEqual(str(raised.exception), code)

    def test_timeout_is_persisted_and_replayed_without_execution(self):
        """После первого timeout тот же UUID возвращает 504 без второго запуска."""
        body = self.body()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            codex_rpc, "execute",
            side_effect=subprocess.TimeoutExpired("private command", 120, stderr="private stderr"),
        ) as execute:
            state = Path(directory)
            for _ in range(3):
                self.assert_failure(body, state, "worker_timeout", 504)
            execute.assert_called_once()
            with sqlite3.connect(state / "idempotency.sqlite3") as database:
                row = database.execute("SELECT state,error_code FROM jobs").fetchone()
                self.assertEqual(row, ("failed", "worker_timeout"))
                self.assertNotIn("private", repr(database.execute("SELECT * FROM jobs").fetchall()))
            self.assertEqual(list((state / "results").glob("*.json")), [])

    def test_execution_failure_is_persisted_without_raw_diagnostics(self):
        """Любая ошибка execute сохраняется фиксированным безопасным кодом 503."""
        for error in (codex_worker.CodexExecutionError("private diagnostic"), RuntimeError("private stderr")):
            with self.subTest(error_type=type(error).__name__), tempfile.TemporaryDirectory() as directory, patch.object(
                codex_rpc, "execute", side_effect=error,
            ) as execute:
                body = self.body()
                state = Path(directory)
                self.assert_failure(body, state, "worker_execution_failed", 503)
                self.assert_failure(body, state, "worker_execution_failed", 503)
                execute.assert_called_once()
                self.assertNotIn(b"private", (state / "idempotency.sqlite3").read_bytes())

    def test_old_schema_migrates_and_keeps_existing_cached_success(self):
        """Миграция старой схемы сохраняет digest, cache и ограничение страниц."""
        body = self.body()
        job_id = json.loads(body)["job_id"]
        outcome = {"text": "cached", "session": None, "files": []}
        with tempfile.TemporaryDirectory() as directory, patch.object(codex_rpc, "execute") as execute:
            state = Path(directory)
            with sqlite3.connect(state / "idempotency.sqlite3") as database:
                database.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY,digest TEXT NOT NULL,created_at INTEGER NOT NULL)")
                database.execute("INSERT INTO jobs VALUES(?,?,123)", (job_id, hashlib.sha256(body).hexdigest()))
            cache = state / "results"
            cache.mkdir()
            (cache / f"{job_id}.json").write_text(json.dumps({
                "digest": hashlib.sha256(body).hexdigest(), "result": outcome,
            }))
            self.assertEqual(codex_rpc.run_request(body, state), outcome)
            execute.assert_not_called()
            database = codex_rpc.idempotency_database(state)
            try:
                self.assertEqual(database.execute("SELECT state,error_code,created_at FROM jobs").fetchone(), ("unknown", None, 123))
                self.assertEqual(database.execute("PRAGMA max_page_count").fetchone()[0], codex_rpc.IDEMPOTENCY_MAX_PAGES)
            finally:
                database.close()

    def test_old_unknown_outcome_never_executes(self):
        """Старая запись без result остаётся fail-closed после миграции."""
        body = self.body()
        with tempfile.TemporaryDirectory() as directory, patch.object(codex_rpc, "execute") as execute:
            state = Path(directory)
            with sqlite3.connect(state / "idempotency.sqlite3") as database:
                database.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY,digest TEXT NOT NULL,created_at INTEGER NOT NULL)")
                database.execute("INSERT INTO jobs VALUES(?,?,123)", (json.loads(body)["job_id"], hashlib.sha256(body).hexdigest()))
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "job result expired"):
                    codex_rpc.run_request(body, state)
            execute.assert_not_called()

    def test_conflicting_failure_replay_never_executes(self):
        """Другой body для сохранённого UUID отклоняется до replay отказа."""
        body = self.body()
        with tempfile.TemporaryDirectory() as directory, patch.object(codex_rpc, "execute", side_effect=TimeoutError) as execute:
            state = Path(directory)
            self.assert_failure(body, state, "worker_timeout", 504)
            execute.reset_mock()
            with self.assertRaisesRegex(ValueError, "conflicting job replay"):
                codex_rpc.run_request(self.body(json.loads(body)["job_id"], "different"), state)
            execute.assert_not_called()

    def test_success_cache_and_retention_race_never_repeat_execution(self):
        """Успешный cache читается, а исчезнувший при чтении result не запускается снова."""
        body = self.body()
        outcome = {"text": "ok", "session": None, "files": []}
        with tempfile.TemporaryDirectory() as directory, patch.object(codex_rpc, "execute", return_value=outcome) as execute:
            state = Path(directory)
            self.assertEqual(codex_rpc.run_request(body, state), outcome)
            self.assertEqual(codex_rpc.run_request(body, state), outcome)
            with patch.object(Path, "read_text", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(ValueError, "job result expired"):
                    codex_rpc.run_request(body, state)
            execute.assert_called_once()
            with sqlite3.connect(state / "idempotency.sqlite3") as database:
                self.assertEqual(database.execute("SELECT state FROM jobs").fetchone()[0], "succeeded")

    def test_retention_failure_preserves_success_and_replay(self):
        """Ошибка необязательной очистки сохраняет успех и не повторяет execute."""
        body = self.body()
        outcome = {"text": "ok", "session": None, "files": []}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            codex_rpc, "execute", return_value=outcome,
        ) as execute, patch.object(
            codex_rpc, "prune_result_cache", side_effect=OSError("private diagnostic"),
        ) as prune, patch("builtins.print") as log:
            state = Path(directory)
            self.assertEqual(codex_rpc.run_request(body, state), outcome)
            self.assertEqual(codex_rpc.run_request(body, state), outcome)
            execute.assert_called_once()
            prune.assert_called_once()
            log.assert_called_once_with("codex_result_cache_failed category=retention_io", flush=True)
            with sqlite3.connect(state / "idempotency.sqlite3") as database:
                self.assertEqual(database.execute("SELECT state,error_code FROM jobs").fetchone(), ("succeeded", None))

    def test_concurrent_failure_replay_executes_once(self):
        """Два одновременных запроса UUID получают один сохранённый отказ."""
        body = self.body()
        def fail(*args, **kwargs):
            """Дать второму потоку дождаться flock и завершить первый timeout."""
            time.sleep(0.05)
            raise TimeoutError("private stderr")
        with tempfile.TemporaryDirectory() as directory, patch.object(codex_rpc, "execute", side_effect=fail) as execute:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(codex_rpc.run_request, body, Path(directory)) for _ in range(2)]
                for future in futures:
                    with self.assertRaises(codex_rpc.WorkerJobFailure) as raised:
                        future.result(timeout=5)
                    self.assertEqual(raised.exception.error_code, "worker_timeout")
            execute.assert_called_once()

    def test_default_rpc_timeouts_cover_chat_and_task_execution(self):
        """Сетевой timeout по умолчанию покрывает 1800 секунд обоих режимов."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(codex_rpc.rpc_timeout({"task": False}), 1860)
            self.assertEqual(codex_rpc.rpc_timeout({"task": True}), 1860)

    def test_http_failures_authentication_and_slot_release(self):
        """HTTP возвращает 504/503, повторяет отказ и освобождает единственный slot."""
        token = "test-only-transport-token"
        outcome = {"text": "ok", "session": None, "files": []}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            codex_rpc.TRANSPORT_KEY: token, "STATE_DIRECTORY": directory,
        }), patch.object(codex_rpc, "execute", side_effect=[
            subprocess.TimeoutExpired("private command", 120), RuntimeError("private stderr"), outcome,
        ]) as execute, patch("builtins.print") as log:
            server = codex_rpc.BoundedThreadingHTTPServer(("127.0.0.1", 0), codex_rpc.JobHandler, max_concurrent_jobs=1)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def post(body, auth=token):
                """Отправить loopback-запрос и разобрать bounded JSON даже при HTTP error."""
                request = Request(f"http://127.0.0.1:{server.server_port}{codex_rpc.JOB_PATH}", data=body,
                                  headers={"Authorization": "Bearer " + auth})
                try:
                    response = urlopen(request, timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, json.loads(response.read())
            try:
                body = self.body()
                self.assertEqual(post(body, "wrong")[0], 401)
                execute.assert_not_called()
                self.assertEqual(post(body), (504, {"error": "worker_timeout"}))
                self.assertEqual(post(body), (504, {"error": "worker_timeout"}))
                failure_body = self.body()
                self.assertEqual(post(failure_body), (503, {"error": "worker_execution_failed"}))
                self.assertEqual(post(failure_body), (503, {"error": "worker_execution_failed"}))
                self.assertEqual(post(self.body()), (200, outcome))
                self.assertEqual(execute.call_count, 3)
                self.assertNotIn("private", repr(log.call_args_list))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)


if __name__ == "__main__":
    unittest.main()
