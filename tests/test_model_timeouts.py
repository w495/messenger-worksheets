from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from deployment import provision_instances
from messenger_worksheets import codex_rpc, codex_worker, worksheet


ROOT = Path(__file__).resolve().parents[1]


class ModelTimeoutTest(unittest.TestCase):
    def test_default_timeouts_and_validation(self):
        """Проверяет получасовые лимиты модели и отказ при невалидной настройке."""
        with patch.dict(os.environ, {}, clear=True):
            for task in (False, True):
                with self.subTest(task=task):
                    self.assertEqual(codex_worker.execution_timeout(task), 1800)
                    self.assertEqual(codex_rpc.rpc_timeout({"task": task}), 1860)
        for name in (worksheet.CHAT_TIMEOUT_ENV, worksheet.TASK_TIMEOUT_ENV):
            for value in ("0", "3601", "invalid"):
                with self.subTest(name=name, value=value), patch.dict(
                    os.environ, {name: value}, clear=True,
                ):
                    with self.assertRaises(ValueError):
                        codex_worker.execution_timeout(name == worksheet.TASK_TIMEOUT_ENV)

    def test_direct_response_passes_default_and_configured_timeout(self):
        """Проверяет фактический timeout HTTP-вызова прямого Responses API."""
        for configured, expected in (({}, 1800), ({worksheet.CHAT_TIMEOUT_ENV: "712"}, 712)):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                body = json.dumps({
                    "status": "completed",
                    "output": [{
                        "type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }],
                }).encode()
                transport = Mock(return_value=body)
                with patch.dict(os.environ, {
                    "WORKSHEET_CODEX_MODEL": "test-model",
                    "WORKSHEET_CODEX_API_KEY": "test-key",
                    **configured,
                }, clear=True), patch.object(codex_worker, "post_model_response", transport):
                    result = codex_worker.execute_direct_response(
                        codex_worker.Request("hello", False, None, ()), Path(directory), None,
                    )
                self.assertEqual(result["text"], "ok")
                self.assertEqual(transport.call_args.kwargs["timeout"], expected)

    def test_worksheet_passes_default_and_configured_timeout(self):
        """Проверяет фактический timeout HTTP-запроса генерации рабочего листа."""
        for configured, expected in (({}, 1800), ({worksheet.TASK_TIMEOUT_ENV: "843"}, 843)):
            with self.subTest(expected=expected):
                document = {"title": "Test", "instructions": "", "exercises": [
                    {"question": "1+1?", "answer": "2"},
                ]}
                body = json.dumps({
                    "status": "completed", "output": [{
                        "type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps(document)}],
                    }],
                }).encode()
                transport = Mock(return_value=body)
                with patch.dict(os.environ, {
                    "WORKSHEET_MODEL_ENDPOINT": "https://example.test/v1/responses",
                    "WORKSHEET_MODEL": "test-model", "WORKSHEET_MODEL_TOKEN": "test-key",
                    **configured,
                }, clear=True), patch.object(worksheet, "post_model_response", transport):
                    result = worksheet.generate_worksheet("Create worksheet")
                self.assertEqual(result.title, "Test")
                self.assertEqual(transport.call_args.kwargs["timeout"], expected)

    def test_session_fallback_shares_request_deadline(self):
        """Проверяет остаток общего бюджета при повторном запуске без сессии."""
        for task in (False, True):
            for configured, elapsed, expected in ((None, 1200, 600), (100, 40, 60)):
                with self.subTest(task=task, configured=configured), tempfile.TemporaryDirectory() as directory:
                    environment = {
                        "WORKSHEET_CODEX_PROVIDER": "local_user",
                        "WORKSHEET_CODEX_BINARY": sys.executable,
                    }
                    if configured is not None:
                        environment[worksheet.TASK_TIMEOUT_ENV if task else worksheet.CHAT_TIMEOUT_ENV] = str(configured)
                    with patch.dict(os.environ, environment, clear=True), \
                            patch.object(codex_worker.time, "monotonic", side_effect=[100, 100, 100 + elapsed]), \
                            patch.object(codex_worker, "command", return_value=["test-codex"]) as command, \
                            patch.object(codex_worker, "run_codex_process", side_effect=[
                                subprocess.CompletedProcess(["test-codex"], 1, "", "no session found"),
                                subprocess.CompletedProcess(["test-codex"], 0, "", ""),
                            ]) as process, \
                            patch.object(codex_worker, "final_reply", return_value=("ok", None)):
                        result = codex_worker.execute(
                            codex_worker.Request("hello", task, "existing-session", ()), Path(directory),
                        )
                    self.assertEqual(result["text"], "ok")
                    self.assertEqual(process.call_args_list[0].kwargs["timeout"], configured or 1800)
                    self.assertEqual(process.call_args_list[1].kwargs["timeout"], expected)
                    self.assertEqual(command.call_args_list[0].args[2].session, "existing-session")
                    self.assertIsNone(command.call_args_list[1].args[2].session)

    def test_expired_request_deadline_prevents_new_process(self):
        """Проверяет отказ до запуска процесса при исчерпании общего времени запроса."""
        for clock, calls in (([100, 1900], 0), ([100, 100, 1900], 1)):
            with self.subTest(calls=calls), tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {
                    "WORKSHEET_CODEX_PROVIDER": "local_user",
                    "WORKSHEET_CODEX_BINARY": sys.executable,
                }, clear=True), \
                        patch.object(codex_worker.time, "monotonic", side_effect=clock), \
                        patch.object(codex_worker, "command", return_value=["test-codex"]), \
                        patch.object(codex_worker, "run_codex_process", return_value=
                            subprocess.CompletedProcess(["test-codex"], 1, "", "no session found"),
                        ) as process:
                    with self.assertRaises(subprocess.TimeoutExpired) as error:
                        codex_worker.execute(
                            codex_worker.Request("hello", False, "existing-session", ()), Path(directory),
                        )
                self.assertEqual(process.call_count, calls)
                self.assertEqual(error.exception.timeout, 1800)

    def test_provision_generated_profiles_use_uniform_timeouts(self):
        """Проверяет генерируемые env всех профилей без доступа к боевым конфигам."""
        generated = {}
        token = "test-transport-token-" * 4
        legacy = {
            "WORKSHEET_CODEX_TRANSPORT_TOKEN": token,
            "WORKSHEET_MESSENGER_TOKEN": "test-messenger-token",
        }
        worker = {"WORKSHEET_CODEX_TRANSPORT_TOKEN": token}
        messenger = Mock()
        messenger.resolve_invite.side_effect = [
            {"found": True, "chat": {"id": "0/0/11111111-1111-4111-8111-111111111111"}},
            {"found": True, "chat": {"id": "0/0/22222222-2222-4222-8222-222222222222"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({
                "expected_email": "test@example.test",
                "instances": {"api": {"invite": "api"}, "local_user": {"invite": "local"}},
                "bot": {"expected_name": "test-bot", "token_file": "/test-token", "base_url": ""},
            }))
            with patch.object(provision_instances.os, "geteuid", return_value=0), \
                    patch.object(sys, "argv", ["provision_instances.py", "--config", str(config)]), \
                    patch.object(provision_instances, "ETC", Path(directory)), \
                    patch.object(provision_instances, "read_env", side_effect=[legacy, worker]), \
                    patch.object(provision_instances, "read_token_file", return_value="test-bot-token"), \
                    patch.object(provision_instances, "local_user_profile", return_value=("test-model", "high")), \
                    patch.object(provision_instances, "native_codex", return_value="/test/codex"), \
                    patch.object(provision_instances, "write_env", side_effect=lambda path, values: generated.update({path.name: values.copy()})), \
                    patch("messenger_user_client.MessengerUserClient", return_value=messenger), \
                    redirect_stdout(StringIO()):
                provision_instances.main()
        self.assertEqual(set(generated), {
            "worker.env", "worker-local-user.env", "worker-sandbox-admin.env",
            "gateway-api.env", "gateway-local-user.env", "gateway-sandbox-admin.env", "gateway-bot.env",
        })
        for name, values in generated.items():
            with self.subTest(profile=name):
                if name.startswith("worker"):
                    self.assertEqual(values[worksheet.CHAT_TIMEOUT_ENV], "1800")
                    self.assertEqual(values[worksheet.TASK_TIMEOUT_ENV], "1800")
                else:
                    self.assertEqual(values[codex_rpc.CHAT_RPC_TIMEOUT_ENV], "1860")
                    self.assertEqual(values[codex_rpc.TASK_RPC_TIMEOUT_ENV], "1860")

    def test_installer_generated_env_timeouts(self):
        """Проверяет вывод блоков env installer без запуска установки или чтения секретов."""
        source = (ROOT / "deployment/install.sh").read_text()
        source = source.split("transport_token=$(openssl rand -hex 32)", 1)[1]
        for marker, expected in (
            ("worker_tmp", {worksheet.CHAT_TIMEOUT_ENV: "1800", worksheet.TASK_TIMEOUT_ENV: "1800"}),
            ("gateway_tmp", {codex_rpc.CHAT_RPC_TIMEOUT_ENV: "1860", codex_rpc.TASK_RPC_TIMEOUT_ENV: "1860"}),
        ):
            prefix, source = source.split('} > "${' + marker + '}"', 1)
            block = prefix.rsplit("{\n", 1)[1]
            output = subprocess.run(
                ["bash", "-c", block], check=True, capture_output=True, text=True, env={},
            ).stdout
            generated = dict(line.split("=", 1) for line in output.splitlines())
            with self.subTest(profile=marker):
                for name, timeout in expected.items():
                    self.assertEqual(generated[name], timeout)


if __name__ == "__main__":
    unittest.main()
