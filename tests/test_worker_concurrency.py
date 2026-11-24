from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, call, patch
from urllib.request import Request, urlopen
import uuid

from messenger_worksheets import codex_rpc, codex_worker


class WorkerConcurrencyTest(unittest.TestCase):
    def test_configured_task_and_rpc_timeouts(self):
        """Проверяет раздельные настраиваемые timeout исполнения и RPC."""
        with patch.dict(os.environ, {
            "WORKSHEET_CODEX_TASK_TIMEOUT": "1234",
            "WORKSHEET_CODEX_CHAT_TIMEOUT": "987",
            "WORKSHEET_CODEX_TASK_RPC_TIMEOUT": "1300",
            "WORKSHEET_CODEX_CHAT_RPC_TIMEOUT": "1000",
        }):
            self.assertEqual(codex_worker.execution_timeout(True), 1234)
            self.assertEqual(codex_worker.execution_timeout(False), 987)
            self.assertEqual(codex_rpc.rpc_timeout({"task": True}), 1300)
            self.assertEqual(codex_rpc.rpc_timeout({"task": False}), 1000)

    def test_two_task_requests_execute_concurrently(self):
        """Проверяет параллельное исполнение двух job потоковым worker."""
        token = "t" * 64
        barrier = threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        active = 0
        maximum_active = 0

        def fake_run_request(body: bytes, state: Path) -> dict:
            """Блокирует два job на барьере и возвращает их независимые результаты."""
            nonlocal active, maximum_active
            self.assertTrue(state.is_dir())
            json.loads(body)
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                barrier.wait()
                time.sleep(0.05)
                return {"text": "ok", "session": None, "files": []}
            finally:
                with lock:
                    active -= 1

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            codex_rpc.TRANSPORT_KEY: token,
            "STATE_DIRECTORY": directory,
        }), patch.object(codex_rpc, "run_request", side_effect=fake_run_request):
            server = codex_rpc.BoundedThreadingHTTPServer(
                ("127.0.0.1", 0), codex_rpc.JobHandler,
                max_concurrent_jobs=4,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.shutdown)
            endpoint = f"http://127.0.0.1:{server.server_port}{codex_rpc.JOB_PATH}"

            def submit_one() -> dict:
                """Отправляет один локальный POST job и читает JSON-ответ."""
                body = json.dumps({
                    "job_id": str(uuid.uuid4()),
                    "input": {"text": "task", "task": True, "session": None, "images": []},
                }).encode()
                request = Request(endpoint, data=body, headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                })
                with urlopen(request, timeout=5) as response:
                    return json.loads(response.read())

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: submit_one(), range(2)))
            self.assertEqual([result["text"] for result in results], ["ok", "ok"])
            self.assertEqual(maximum_active, 2)

    def test_same_job_id_executes_once_under_concurrent_replay(self):
        """Проверяет сериализацию одинакового job UUID без двойного side effect."""
        job_id = str(uuid.uuid4())
        body = json.dumps({
            "job_id": job_id,
            "input": {"text": "task", "task": True, "session": None, "images": []},
        }).encode()
        calls = 0
        calls_lock = threading.Lock()

        def fake_execute(request, state_dir, job_id=None):
            """Фиксирует единственное исполнение и имитирует дорогую задачу."""
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.1)
            return {"text": "ok", "session": None, "files": []}

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(codex_rpc, "execute", side_effect=fake_execute):
            state = Path(directory)
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda _: codex_rpc.run_request(body, state), range(2),
                ))
        self.assertEqual(calls, 1)
        self.assertEqual(results[0], results[1])

    def test_codex_process_uses_private_subreaper_supervisor(self):
        """Проверяет отдельную сессию supervisor и сохранение native argv/stdin/output."""
        process = Mock()
        process.pid = 4321
        process.args = ["/usr/local/bin/codex", "exec"]
        process.returncode = 0
        process.poll.return_value = 0
        process.communicate.return_value = ("stdout", "stderr")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(codex_worker.subprocess, "Popen", return_value=process) as popen, \
                patch.object(codex_worker.os, "killpg") as killpg:
            result = codex_worker.run_codex_process(
                process.args,
                workspace=Path(directory),
                environment={"PATH": "/usr/bin:/bin"},
                text="prompt",
                timeout=10,
            )
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_not_called()
        wrapper = popen.call_args.args[0]
        self.assertEqual(wrapper[1], "-I")
        self.assertTrue(wrapper[2].endswith("/process_supervisor.py"))
        self.assertEqual(wrapper[3:], ["--", *process.args])
        self.assertEqual(process.communicate.call_args_list[0].args, ("prompt",))
        self.assertEqual((result.stdout, result.stderr), ("stdout", "stderr"))

    def test_result_cache_prunes_old_entries_to_bounded_count(self):
        """Проверяет удаление старых RPC results сверх retention-лимита."""
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            for index in range(codex_rpc.MAX_CACHE_FILES + 4):
                path = cache / f"{index:04d}.json"
                path.write_text("{}", encoding="utf-8")
                os.utime(path, ns=(index + 1, index + 1))
            keep = cache / f"{codex_rpc.MAX_CACHE_FILES + 3:04d}.json"
            codex_rpc.prune_result_cache(cache, keep)
            remaining = tuple(cache.glob("*.json"))
        self.assertLessEqual(len(remaining), codex_rpc.MAX_CACHE_FILES)
        self.assertIn(keep.name, {path.name for path in remaining})

    def test_evicted_result_tombstone_blocks_job_reexecution(self):
        """Проверяет fail-closed replay UUID после удаления тяжёлого result."""
        job_id = str(uuid.uuid4())
        body = json.dumps({
            "job_id": job_id,
            "input": {"text": "task", "task": True, "session": None, "images": []},
        }).encode()
        outcome = {"text": "ok", "session": None, "files": []}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            codex_rpc, "execute", return_value=outcome,
        ) as execute:
            state = Path(directory)
            self.assertEqual(codex_rpc.run_request(body, state), outcome)
            (state / "results" / f"{job_id}.json").unlink()
            execute.reset_mock()
            with self.assertRaisesRegex(ValueError, "job result expired"):
                codex_rpc.run_request(body, state)
            conflicting = json.dumps({
                "job_id": job_id,
                "input": {
                    "text": "different", "task": True,
                    "session": None, "images": [],
                },
            }).encode()
            with self.assertRaisesRegex(ValueError, "conflicting job replay"):
                codex_rpc.run_request(conflicting, state)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
