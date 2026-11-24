from __future__ import annotations

import base64
from io import BytesIO
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from messenger_worksheets import model_http


class ModelHttpTest(unittest.TestCase):
    def test_parent_passes_authorization_only_in_child_env(self):
        """Проверяет изоляцию секрета, ограничение deadline и безопасное тело ответа."""
        secret = "synthetic-test-authorization"
        request = Request("https://example.test/v1/responses", data=b'{"input":"hello"}', headers={
            "Authorization": secret, "Content-Type": "application/json",
        })
        process = Mock(pid=12345, returncode=0)
        process.communicate.return_value = (
            json.dumps({"status": "ok", "body": base64.b64encode(b"ok").decode()}).encode(), None,
        )
        with patch.dict(os.environ, {"UNRELATED_CREDENTIAL": "synthetic", "SSL_CERT_FILE": "/test/ca"}, clear=True), \
                patch.object(model_http.time, "monotonic", side_effect=[100, 102, 103]), \
                patch.object(model_http.subprocess, "Popen", return_value=process) as popen, \
                patch.object(model_http.os, "killpg") as killpg:
            result = model_http.post_model_response(request, timeout=10, max_bytes=32)
        self.assertEqual(result, b"ok")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["env"], {
            model_http.AUTHORIZATION_ENV: secret, "SSL_CERT_FILE": "/test/ca",
        })
        self.assertNotIn(secret, str(popen.call_args.args))
        input_data = process.communicate.call_args_list[0].args[0]
        self.assertNotIn(secret.encode(), input_data)
        self.assertEqual(json.loads(input_data)["headers"], {"Content-type": "application/json"})
        self.assertEqual(process.communicate.call_args_list[0].kwargs["timeout"], 7)
        killpg.assert_called_once_with(12345, signal.SIGKILL)

    def test_parent_timeout_kills_group_and_reaps_child(self):
        """Проверяет принудительное завершение и сбор ребёнка после общего timeout."""
        process = Mock(pid=12345)
        process.communicate.side_effect = [subprocess.TimeoutExpired(["test"], 7), (b"", None)]
        with patch.object(model_http.time, "monotonic", side_effect=[100, 102, 103]), \
                patch.object(model_http.subprocess, "Popen", return_value=process), \
                patch.object(model_http.os, "killpg") as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                model_http.post_model_response(Request("https://example.test"), timeout=10, max_bytes=32)
        self.assertEqual(process.communicate.call_count, 2)
        self.assertEqual(process.communicate.call_args_list[0].kwargs["timeout"], 7)
        self.assertEqual(process.communicate.call_args_list[1], call())
        killpg.assert_called_once_with(12345, signal.SIGKILL)

    def test_trickle_child_output_cannot_extend_deadline(self):
        """Проверяет реальное прекращение ребёнка с непрерывным потоком мелких порций."""
        original_popen = subprocess.Popen
        children = []

        def trickle_process(argv, **kwargs):
            """Подменить только HTTP-работу ребёнка непрерывной записью небольших порций."""
            code = (
                "import sys,time;sys.stdin.buffer.read();"
                "exec('while True:\\n sys.stdout.write(\"x\");sys.stdout.flush();time.sleep(0.02)')"
            )
            process = original_popen([sys.executable, "-I", "-c", code], **kwargs)
            children.append(process)
            return process

        started = time.monotonic()
        with patch.object(model_http.subprocess, "Popen", side_effect=trickle_process):
            with self.assertRaises(subprocess.TimeoutExpired):
                model_http.post_model_response(Request("https://example.test"), timeout=0.25, max_bytes=32)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_child_limits_read_and_keeps_transport_contract(self):
        """Проверяет HTTPS, запрет redirect, env Authorization и максимум чтения ребёнка."""
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"response"
        opener = Mock()
        opener.open.return_value = response
        payload = {
            "url": "https://example.test/v1/responses", "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": base64.b64encode(b"request").decode(), "timeout": 10, "max_bytes": 32,
        }
        with patch.dict(os.environ, {model_http.AUTHORIZATION_ENV: "synthetic-secret"}, clear=True), \
                patch.object(model_http, "build_opener", return_value=opener) as build:
            result = model_http.child_response(payload)
        self.assertEqual(result, {"status": "ok", "body": base64.b64encode(b"response").decode()})
        self.assertIsInstance(build.call_args.args[0], model_http.NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.data, b"request")
        self.assertEqual(request.get_header("Authorization"), "synthetic-secret")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
        response.read.assert_called_once_with(33)
        self.assertIsNone(model_http.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))

    def test_child_errors_never_serialize_diagnostics_or_error_body(self):
        """Проверяет безопасный протокол HTTP и сетевых ошибок без исходных диагностик."""
        payload = {"url": "https://example.test", "method": "POST", "headers": {},
                   "body": "", "timeout": 10, "max_bytes": 32}
        cases = (
            (HTTPError(payload["url"], 401, "synthetic-secret", {}, BytesIO(b"synthetic-secret")),
             {"status": "http_error", "code": 401}),
            (URLError("synthetic-secret"), {"status": "connection_error"}),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(model_http, "build_opener", return_value=opener):
                    result = model_http.child_response(payload)
                self.assertEqual(result, expected)
                self.assertNotIn("synthetic-secret", json.dumps(result))

    def test_parent_restores_safe_http_and_connection_errors(self):
        """Проверяет восстановление только безопасных категорий ошибок процесса."""
        for protocol, expected in ((b'{"status":"http_error","code":403}', HTTPError),
                                   (b'{"status":"connection_error"}', URLError),
                                   (b'invalid protocol', URLError)):
            with self.subTest(protocol=protocol):
                process = Mock(pid=12345, returncode=0)
                process.communicate.return_value = (protocol, None)
                with patch.object(model_http.subprocess, "Popen", return_value=process), \
                        patch.object(model_http.os, "killpg"):
                    with self.assertRaises(expected) as error:
                        model_http.post_model_response(Request("https://example.test"), timeout=10, max_bytes=32)
                if expected is HTTPError:
                    self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
