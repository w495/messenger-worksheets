from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

from messenger_worksheets import codex_rpc, codex_worker, model_http


class JobControlTest(unittest.TestCase):
    def body(self, identifier: str, text: str = "native") -> bytes:
        """Собрать минимальный RPC job без реальных credentials и пользовательских данных."""
        return json.dumps({"job_id": identifier, "input": {
            "text": text, "task": True, "session": None, "images": [],
        }}).encode()

    def wait_for(self, predicate, *, timeout: float = 5) -> None:
        """Дождаться проверяемого состояния до короткого deadline, иначе провалить тест."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("expected state was not reached")

    def running(self, process_id: int) -> bool:
        """Проверить, выполняется ли Linux process, считая zombie уже остановленным."""
        try:
            return Path(f"/proc/{process_id}/stat").read_text().split(") ", 1)[1][0] != "Z"
        except FileNotFoundError:
            return False

    def test_cancel_before_submit_persists_and_never_executes(self):
        """Предварительная отмена не даёт запустить опоздавший submit и его повтор."""
        identifier = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory, patch.object(codex_rpc, "execute") as execute:
            state = Path(directory)
            self.assertEqual(codex_rpc.job_state(state, identifier)["state"], "unknown")
            self.assertEqual(codex_rpc.job_state(state, identifier, cancel=True)["state"], "cancelled")
            for _ in range(2):
                with self.assertRaises(codex_rpc.WorkerJobFailure) as caught:
                    codex_rpc.run_request(self.body(identifier), state)
                self.assertEqual((caught.exception.status, caught.exception.error_code), (409, "worker_cancelled"))
            execute.assert_not_called()

    def test_native_cancel_kills_own_process_tree_and_keeps_other_job(self):
        """Отмена убивает настоящую группу job, сохраняя независимый параллельный запрос."""
        identifier, sibling = str(uuid.uuid4()), str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            marker = state / "processes.json"
            command = (
                "import json,os,subprocess,sys,time;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                "open(sys.argv[1],'w').write(json.dumps([os.getpid(),child.pid]));"
                "time.sleep(60)"
            )

            def execute(request, state_dir, job_id=None):
                """Запустить реальное дерево только первого job, второму вернуть результат."""
                if request.text == "sibling":
                    time.sleep(0.4)
                    return {"text": "sibling complete", "session": None, "files": []}
                codex_worker.run_codex_process(
                    [sys.executable, "-c", command, str(marker)], workspace=state_dir,
                    environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    text="synthetic prompt", timeout=60,
                )
                self.fail("cancelled process unexpectedly completed")

            with patch.object(codex_rpc, "execute", side_effect=execute), ThreadPoolExecutor(max_workers=2) as pool:
                cancelled = pool.submit(codex_rpc.run_request, self.body(identifier), state)
                completed = pool.submit(codex_rpc.run_request, self.body(sibling, "sibling"), state)
                self.wait_for(lambda: marker.exists() and marker.stat().st_size > 0)
                process_ids = json.loads(marker.read_text())
                self.assertTrue(all(self.running(pid) for pid in process_ids))
                self.assertEqual(codex_rpc.job_state(state, identifier)["state"], "running")
                requested = codex_rpc.job_state(state, identifier, cancel=True)
                self.assertEqual(requested["state"], "cancelling")
                with self.assertRaises(codex_rpc.WorkerJobFailure) as caught:
                    cancelled.result(timeout=5)
                self.assertEqual(caught.exception.error_code, "worker_cancelled")
                self.assertEqual(completed.result(timeout=5)["text"], "sibling complete")
                self.wait_for(lambda: all(not self.running(pid) for pid in process_ids))
                self.assertEqual(codex_rpc.job_state(state, identifier)["state"], "cancelled")
                self.assertEqual(codex_rpc.job_state(state, sibling, cancel=True)["state"], "completed")
                with self.assertRaises(codex_rpc.WorkerJobFailure):
                    codex_rpc.run_request(self.body(identifier), state)

    def test_direct_http_cancel_reaps_its_real_child(self):
        """Отмена прерывает прямой HTTP transport независимо от полного сетевого timeout."""
        identifier = str(uuid.uuid4())
        original = subprocess.Popen
        children = []

        def fake_http(argv, **kwargs):
            """Заменить только сеть ребёнка долгим процессом, сохранив Popen/pipe contract."""
            process = original([sys.executable, "-c", "import sys,time;sys.stdin.buffer.read();time.sleep(60)"], **kwargs)
            children.append(process)
            return process

        def execute(request, state_dir, job_id=None):
            """Вызвать настоящий изолированный transport без запроса к внешней сети."""
            model_http.post_model_response(Request("https://example.test"), timeout=60, max_bytes=32)

        with tempfile.TemporaryDirectory() as directory, patch.object(codex_rpc, "execute", side_effect=execute), \
                patch.object(model_http.subprocess, "Popen", side_effect=fake_http), ThreadPoolExecutor(max_workers=1) as pool:
            state = Path(directory)
            future = pool.submit(codex_rpc.run_request, self.body(identifier, "direct"), state)
            self.wait_for(lambda: bool(children))
            self.assertEqual(codex_rpc.job_state(state, identifier, cancel=True)["state"], "cancelling")
            with self.assertRaises(codex_rpc.WorkerJobFailure) as caught:
                future.result(timeout=5)
            self.assertEqual(caught.exception.status, 409)
            self.assertIsNotNone(children[0].poll())
            self.assertEqual(codex_rpc.job_state(state, identifier)["state"], "cancelled")

    def test_control_authentication_paths_and_no_execution_slot(self):
        """Status/cancel доступны при занятом semaphore и требуют правильный bearer."""
        identifier = str(uuid.uuid4())
        token = "synthetic-control-token"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            codex_rpc.TRANSPORT_KEY: token, "STATE_DIRECTORY": directory,
            "WORKSHEET_CODEX_PROVIDER": "responses_direct", "WORKSHEET_CODEX_MODEL": "test-model",
        }, clear=True):
            server = codex_rpc.BoundedThreadingHTTPServer(("127.0.0.1", 0), codex_rpc.JobHandler, max_concurrent_jobs=1)
            server.job_slots.acquire()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(suffix, *, method="GET", auth=token):
                """Прочитать bounded локальный HTTP response, включая ожидаемый HTTPError."""
                incoming = Request(f"http://127.0.0.1:{server.server_port}{codex_rpc.JOB_PATH}{suffix}",
                                   method=method, data=b"" if method == "POST" else None,
                                   headers={"Authorization": "Bearer " + auth})
                try:
                    response = urlopen(incoming, timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, json.loads(response.read(16385))

            try:
                self.assertEqual(request("/" + identifier, auth="wrong")[0], 401)
                self.assertEqual(request("/capabilities", auth="wrong")[0], 401)
                self.assertEqual(request("/models", auth="wrong")[0], 401)
                self.assertEqual(request("/" + identifier + "/cancel", method="POST", auth="wrong")[0], 401)
                self.assertEqual(request("/" + identifier)[1]["state"], "unknown")
                self.assertEqual(request("/" + identifier + "/cancel", method="POST")[1]["state"], "cancelled")
                self.assertEqual(request("/" + identifier)[1]["state"], "cancelled")
                self.assertEqual(request("/capabilities"), (200, {
                    "model": "test-model", "reasoning_effort": "medium",
                    "mode": "responses_direct", "task_supported": False,
                }))
                self.assertEqual(request("/" + identifier + "/cancel")[0], 404)
                self.assertEqual(request("/capabilities", method="POST")[0], 404)
                from messenger_worksheets import model_catalog
                safe_catalog = model_catalog._empty("responses_direct")
                with patch.object(model_catalog, "effective_models", return_value=safe_catalog) as fetch:
                    self.assertEqual(request("/models"), (200, safe_catalog))
                    fetch.assert_called_once()
            finally:
                server.job_slots.release()
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_effective_native_settings_do_not_claim_ignored_user_config(self):
        """Capabilities показывает применённый env, а не игнорируемый CLI user config."""
        with patch.dict(os.environ, {"WORKSHEET_CODEX_PROVIDER": "local_user"}, clear=True):
            self.assertEqual(codex_rpc.effective_capabilities(), {
                "model": None, "reasoning_effort": None, "mode": "local_user", "task_supported": True,
            })
        with patch.dict(os.environ, {"WORKSHEET_CODEX_PROVIDER": "sandbox_admin",
                                    "WORKSHEET_CODEX_MODEL": "test-model",
                                    "WORKSHEET_CODEX_REASONING_EFFORT": "high"}, clear=True):
            self.assertEqual(codex_rpc.effective_capabilities()["reasoning_effort"], "high")

    def test_cancel_after_execute_before_cache_commit_suppresses_result(self):
        """Отмена между execute и cache commit не фиксирует успех и не доставляет результат."""
        identifier = str(uuid.uuid4())
        serialized, release = threading.Event(), threading.Event()
        original_dump = json.dump

        def pause_dump(value, stream, **kwargs):
            """Остановить запись временного результата перед атомарным rename/commit."""
            original_dump(value, stream, **kwargs)
            serialized.set()
            self.assertTrue(release.wait(5))

        with tempfile.TemporaryDirectory() as directory, patch.object(codex_rpc, "execute", return_value={
            "text": "prepared result", "session": None, "files": [],
        }), patch.object(codex_rpc.json, "dump", side_effect=pause_dump), ThreadPoolExecutor(max_workers=1) as pool:
            state = Path(directory)
            future = pool.submit(codex_rpc.run_request, self.body(identifier), state)
            self.assertTrue(serialized.wait(5))
            self.assertEqual(codex_rpc.job_state(state, identifier, cancel=True)["state"], "cancelling")
            release.set()
            with self.assertRaises(codex_rpc.WorkerJobFailure) as caught:
                future.result(timeout=5)
            self.assertEqual(caught.exception.error_code, "worker_cancelled")
            self.assertFalse((state / "results" / f"{identifier}.json").exists())
            self.assertEqual(codex_rpc.job_state(state, identifier)["state"], "cancelled")

    def test_concurrent_same_uuid_replays_cancellation_without_new_execute(self):
        """Два submit одного UUID получают одну отмену без повторного запуска модели."""
        identifier = str(uuid.uuid4())
        entered = threading.Event()

        def execute(request, state_dir, job_id=None):
            """Ждать отмены в cancellation scope, имитируя долгую локальную операцию."""
            entered.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                model_http.check_cancellation()
                time.sleep(0.01)
            self.fail("cancellation was not observed")

        with tempfile.TemporaryDirectory() as directory, patch.object(codex_rpc, "execute", side_effect=execute) as run, \
                ThreadPoolExecutor(max_workers=2) as pool:
            state = Path(directory)
            body = self.body(identifier)
            first = pool.submit(codex_rpc.run_request, body, state)
            self.assertTrue(entered.wait(5))
            second = pool.submit(codex_rpc.run_request, body, state)
            codex_rpc.job_state(state, identifier, cancel=True)
            for future in (first, second):
                with self.assertRaises(codex_rpc.WorkerJobFailure) as caught:
                    future.result(timeout=5)
                self.assertEqual(caught.exception.error_code, "worker_cancelled")
            run.assert_called_once()

    def test_registry_and_persistent_status_are_scoped_to_state_directory(self):
        """Одинаковый UUID в другом state-dir не получает чужую отмену или живой статус."""
        identifier = str(uuid.uuid4())
        entered, release = threading.Event(), threading.Event()

        def execute(request, state_dir, job_id=None):
            """Дождаться проверки второго state-dir и вернуть независимый успешный ответ."""
            entered.set()
            self.assertTrue(release.wait(5))
            model_http.check_cancellation()
            return {"text": "independent", "session": None, "files": []}

        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir, \
                patch.object(codex_rpc, "execute", side_effect=execute), ThreadPoolExecutor(max_workers=1) as pool:
            first, second = Path(first_dir), Path(second_dir)
            future = pool.submit(codex_rpc.run_request, self.body(identifier), first)
            self.assertTrue(entered.wait(5))
            self.assertEqual(codex_rpc.job_state(second, identifier, cancel=True)["state"], "cancelled")
            self.assertEqual(codex_rpc.job_state(first, identifier)["state"], "running")
            release.set()
            self.assertEqual(future.result(timeout=5)["text"], "independent")

    def test_orphaned_running_record_reports_unknown_after_restart(self):
        """Без живого registry старый running не выдаётся за выполняющийся или отменённый job."""
        identifier = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            database = codex_rpc.idempotency_database(state)
            with database:
                database.execute("INSERT INTO jobs(job_id,digest,created_at,state) VALUES(?,'digest',123,'running')", (identifier,))
            database.close()
            for cancel in (False, True):
                status = codex_rpc.job_state(state, identifier, cancel=cancel)
                self.assertEqual(status["state"], "unknown")
                self.assertEqual(status["created_at"], 123)


if __name__ == "__main__":
    unittest.main()
