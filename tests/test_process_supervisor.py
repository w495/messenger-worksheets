from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from messenger_worksheets import codex_rpc, codex_worker, model_http


DAEMON = (
    "import json,os,signal,sys,time;os.setsid();"
    "child=os.fork();"
    "\nif child: os._exit(0)"
    "\nsignal.signal(signal.SIGTERM,signal.SIG_IGN);"
    "open(sys.argv[1],'w').write(json.dumps([os.getpid(),os.getppid(),os.getpgrp(),os.getsid(0)]));"
    "time.sleep(60)"
)
NATIVE = (
    "import os,subprocess,sys,time;"
    "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]);"
    "\nwhile not os.path.exists(sys.argv[1]) or not os.path.getsize(sys.argv[1]): time.sleep(0.01)"
    "\nif sys.argv[3]=='wait': time.sleep(60)"
    "\nprint('NATIVE_OUTPUT',flush=True)"
)


class ProcessSupervisorTest(unittest.TestCase):
    def wait_for(self, predicate, *, timeout=5):
        """Дождаться фактического состояния реальных тестовых процессов до короткого deadline."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("expected process state was not reached")

    def running(self, process_id):
        """Проверить отсутствие живого процесса, отдельно допуская кратковременный zombie."""
        try:
            return Path(f"/proc/{process_id}/stat").read_text().rsplit(") ", 1)[1][0] != "Z"
        except FileNotFoundError:
            return False

    def test_normal_return_reaps_setsid_doublefork_daemon(self):
        """Обычное завершение native Codex убирает и усыновлённого daemon вне своей group."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "daemon.json"
            result = codex_worker.run_codex_process(
                [sys.executable, "-c", NATIVE, str(marker), DAEMON, "return"],
                workspace=Path(directory), environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                text="PROMPT", timeout=10,
            )
            daemon, old_parent, group, session = json.loads(marker.read_text())
            self.assertEqual(result.stdout.strip(), "NATIVE_OUTPUT")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(group, session)
            self.assertNotEqual(daemon, group)
            self.wait_for(lambda: not self.running(daemon))
            self.assertFalse(Path(f"/proc/{daemon}").exists(), "supervisor must reap adopted zombie")

    def test_cancel_reaps_escaped_daemon_and_preserves_sibling(self):
        """RPC /stop завершает setsid/double-fork потомка и не трогает параллельный job."""
        identifier, sibling = str(uuid.uuid4()), str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            marker = state / "cancel-daemon.json"

            def execute(request, state_dir, job_id=None):
                """Запустить реальный supervisor для отменяемого job и независимый соседний процесс."""
                if request.text == "sibling":
                    return codex_worker.run_codex_process(
                        [sys.executable, "-c", "import time;time.sleep(0.6);print('SIBLING_OK')"],
                        workspace=state_dir, environment={}, text="", timeout=10,
                    ).stdout
                codex_worker.run_codex_process(
                    [sys.executable, "-c", NATIVE, str(marker), DAEMON, "wait"],
                    workspace=state_dir, environment={}, text="", timeout=60,
                )

            def body(job, text):
                """Собрать bounded RPC-конверт без ключей и внешнего сетевого вызова."""
                return json.dumps({"job_id": job, "input": {
                    "text": text, "task": True, "session": None, "images": [],
                }}).encode()

            with patch.object(codex_rpc, "execute", side_effect=execute), ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(codex_rpc.run_request, body(identifier, "daemon"), state)
                other = pool.submit(codex_rpc.run_request, body(sibling, "sibling"), state)
                self.wait_for(lambda: marker.exists() and marker.stat().st_size > 0)
                daemon = json.loads(marker.read_text())[0]
                self.assertTrue(self.running(daemon))
                codex_rpc.job_state(state, identifier, cancel=True)
                with self.assertRaises(codex_rpc.WorkerJobFailure) as caught:
                    pending.result(timeout=5)
                self.assertEqual(caught.exception.error_code, "worker_cancelled")
                self.assertEqual(other.result(timeout=5).strip(), "SIBLING_OK")
                self.assertEqual(codex_rpc.job_state(state, identifier)["state"], "cancelled")
                self.assertFalse(Path(f"/proc/{daemon}").exists())

    def test_timeout_cleans_escaped_descendant_before_returning_error(self):
        """Общий deadline не оставляет PTY-like daemon, сохраняя исходную TimeoutExpired."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "timeout-daemon.json"
            with self.assertRaises(subprocess.TimeoutExpired):
                codex_worker.run_codex_process(
                    [sys.executable, "-c", NATIVE, str(marker), DAEMON, "wait"],
                    workspace=Path(directory), environment={}, text="", timeout=0.3,
                )
            self.assertFalse(Path(f"/proc/{json.loads(marker.read_text())[0]}").exists())

    def test_cleanup_failure_cannot_be_reported_as_cancelled(self):
        """Отсутствие подтверждения supervisor даёт failed даже после запроса /stop."""
        identifier = str(uuid.uuid4())
        entered, release = threading.Event(), threading.Event()

        def execute(request, state_dir, job_id=None):
            """Имитировать подтверждённую ошибку уборки после полученной отмены."""
            entered.set()
            self.assertTrue(release.wait(5))
            raise model_http.JobCleanupFailed("synthetic-cleanup-failure")

        with tempfile.TemporaryDirectory() as directory, patch.object(codex_rpc, "execute", side_effect=execute), \
                ThreadPoolExecutor(max_workers=1) as pool:
            state = Path(directory)
            body = json.dumps({"job_id": identifier, "input": {
                "text": "synthetic", "task": True, "session": None, "images": [],
            }}).encode()
            pending = pool.submit(codex_rpc.run_request, body, state)
            self.assertTrue(entered.wait(5))
            codex_rpc.job_state(state, identifier, cancel=True)
            release.set()
            with self.assertRaises(codex_rpc.WorkerJobFailure) as caught:
                pending.result(timeout=5)
            self.assertEqual(caught.exception.error_code, "worker_execution_failed")
            self.assertEqual(codex_rpc.job_state(state, identifier)["state"], "failed")

    def test_unconfirmed_supervisor_exit_is_failure(self):
        """Signal-выход supervisor не принимается за доказательство удаления потомков."""
        for returncode in (-signal.SIGKILL, 125):
            process = Mock(returncode=returncode)
            process.poll.return_value = returncode
            with self.subTest(returncode=returncode), self.assertRaises(model_http.JobCleanupFailed):
                codex_worker.finish_supervised_process(process)


if __name__ == "__main__":
    unittest.main()
