"""Проверить безопасную диагностику и независимость production smoke-проверок."""
from __future__ import annotations

import base64
from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import os
from pathlib import Path
import re
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


class CommandSmokeSafetyTest(unittest.TestCase):
    def setUp(self):
        """Загрузить deployment-модуль без root, конфигурации или сетевых вызовов."""
        directory = Path(__file__).resolve().parents[1] / "deployment"
        spec = importlib.util.spec_from_file_location("command_smoke_under_test", directory / "smoke_test_commands.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(directory), *sys.path]):
            spec.loader.exec_module(self.module)

    def test_failed_synthetic_reply_masks_credentials_urls_and_control_characters(self):
        """Ошибка собственного smoke не выдаёт ключи, bearer, signed query или терминальные escape-последовательности."""
        secret = "sk-" + "x" * 40
        text = (f"Bearer {secret} token=private-value https://example.test/path?sig=private-signature#fragment "
                "https://user:password@example.test/private \x1b[31m\nline")
        result = self.module.safe_smoke_reply(text)
        for forbidden in (secret, "private-value", "private-signature", "password", "#fragment", "\x1b", "\n"):
            self.assertNotIn(forbidden, result)
        self.assertIn("<masked>", result)
        self.assertIn("example.test/path", result)

    def test_readonly_failure_does_not_skip_review_or_confirmed_cancellation(self):
        """Неуспешный plan сохраняет диагностику, но review и отмена всё равно проверяются."""
        module = self.module
        stopped = threading.Event()
        modes = []

        def submit(endpoint, identifier, job):
            """Подменить RPC синтетическими исходами, не обращаясь к сети или credentials."""
            mode = job.get("options", {}).get("mode")
            modes.append(mode)
            if mode == "plan":
                return {"text": "bwrap: loopback failure", "files": [], "session": None}
            if mode == "review":
                marker = re.search(r"def ([a-z_]+)\(", base64.b64decode(job["history_files"][0]["data"]).decode())[1]
                return {"text": marker, "files": [], "session": None}
            if not stopped.wait(2):
                raise RuntimeError("test cancellation did not arrive")
            raise HTTPError("https://example.test", 409, "cancelled", None, None)

        def control(endpoint, identifier, action="status"):
            """Связать отмену только с ожидающим потоком синтетического job."""
            if action == "cancel":
                stopped.set()
            return {"state": "cancelled" if stopped.is_set() else "running"}

        output = StringIO()
        with patch.object(sys, "argv", ["smoke_test_commands.py"]), \
                patch.dict(os.environ, {"WORKSHEET_CODEX_WORKER_URL": "https://example.test"}), \
                patch.object(module, "catalog_smoke", return_value=[]), \
                patch.object(module, "load_rpc_environment"), \
                patch.object(module, "worker_capabilities", return_value={"task_supported": True}), \
                patch.object(module, "submit", side_effect=submit), \
                patch.object(module, "job_control", side_effect=control), redirect_stdout(output), \
                self.assertRaisesRegex(RuntimeError, "command smoke failed: plan"):
            module.main()
        self.assertEqual(modes, ["plan", "review", None])
        self.assertIn("category=bwrap_loopback", output.getvalue())
        self.assertIn("worker_review_smoke_ok", output.getvalue())
        self.assertIn("worker_cancel_smoke_ok terminal=cancelled", output.getvalue())

    def test_read_proof_is_code_identifier_available_only_inside_file(self):
        """Проверка требует относящееся к дефекту имя функции, а не непрозрачный служебный token."""
        for mode in ("plan", "review"):
            job, symbol = self.module.readonly_smoke_job(mode)
            source = base64.b64decode(job["history_files"][0]["data"]).decode()
            self.assertIn(f"def {symbol}(x):", source)
            self.assertNotIn(symbol, job["text"])
            self.assertNotIn(symbol, job["history_files"][0]["name"])
            self.assertNotIn("READONLY_OK", source)
            self.assertEqual(job["options"]["mode"], mode)


if __name__ == "__main__":
    unittest.main()
