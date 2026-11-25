from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from messenger_worksheets import model_catalog as catalog


class ModelCatalogTest(unittest.TestCase):
    def setUp(self):
        """Изолировать env и cache каждого теста без чтения реальных credentials."""
        catalog._CACHE.clear()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_direct_api_projects_text_models_without_secret_or_raw_metadata(self):
        """GET использует фиксированный origin и публикует только безопасные текстовые ID."""
        os.environ.update(WORKSHEET_CODEX_PROVIDER="responses_direct", WORKSHEET_CODEX_API_KEY="synthetic-secret", WORKSHEET_CODEX_MODEL="gpt-6.2")
        payload = {"data": [{"id": value, "instructions": "private instructions"} for value in
                            ("gpt-6.2", "o3", "gpt-audio", "gpt-6-search-preview", "text-embedding-3-large", "gpt-image-1", "gpt-realtime", "gpt-x\nsecret", "unknown", "o1-mini")]}
        with patch.object(catalog, "post_model_response", return_value=json.dumps(payload).encode()) as transport:
            result = catalog.effective_models()
        self.assertEqual([item["id"] for item in result["models"]], ["gpt-6.2", "o1-mini", "o3"])
        self.assertTrue(result["models"][0]["is_default"])
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-secret")
        self.assertEqual(transport.call_args.kwargs, {"timeout": 15.0, "max_bytes": catalog.MAX_API_BYTES})
        self.assertEqual(result["scope"], "catalogue")
        self.assertEqual(result["source"], "openai_models_api")
        self.assertIsNone(result["error"])
        self.assertIsInstance(result["fetched_at"], int)
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertNotIn("private instructions", json.dumps(result))

    def test_native_projects_visible_slug_and_allowlisted_reasoning(self):
        """Скрытые записи и произвольные поля native catalogue не выходят наружу."""
        os.environ.update(WORKSHEET_CODEX_PROVIDER="sandbox_admin", WORKSHEET_CODEX_BINARY="/synthetic/codex")
        payload = {"models": [{"slug": "gpt-6.2-sol", "visibility": "list",
                               "supported_reasoning_levels": [{"effort": "high", "description": "secret"}, {"effort": "medium"}, {"effort": "invented"}],
                               "default_reasoning_level": "medium", "instructions": "secret"},
                              {"slug": "gpt-hidden", "visibility": "hide"}, {"slug": "gpt-bad\n", "visibility": "list"}]}
        with patch.object(catalog, "_native_output", return_value=json.dumps(payload).encode()) as native:
            result = catalog.effective_models()
        native.assert_called_once_with("/synthetic/codex")
        self.assertEqual(result["models"], [{"id": "gpt-6.2-sol", "is_default": False,
                                            "reasoning_efforts": ["medium", "high"], "default_reasoning_effort": "medium"}])
        self.assertEqual(result["source"], "codex_active_catalog")
        self.assertNotIn("secret", json.dumps(result))

    def test_response_and_projection_limits_fail_closed_or_truncate(self):
        """Большое тело отклоняется, публичный каталог ограничен числом и размером."""
        os.environ.update(WORKSHEET_CODEX_PROVIDER="responses_direct", WORKSHEET_CODEX_API_KEY="synthetic")
        with patch.object(catalog, "post_model_response", return_value=b"x" * (catalog.MAX_API_BYTES + 1)):
            self.assertEqual(catalog.effective_models()["error"], "response_too_large")
        entries = [{"id": f"gpt-{index:04d}"} for index in range(300)]
        models, truncated = catalog._project(entries, native=False, configured=None)
        self.assertEqual(len(models), 256)
        self.assertTrue(truncated)
        with patch.object(catalog, "MAX_PUBLIC_BYTES", 200):
            models, truncated = catalog._project(entries, native=False, configured=None)
        self.assertLessEqual(len(json.dumps(models).encode()), 200)
        self.assertTrue(truncated)

    def test_failure_is_safe_and_unsupported_provider_never_queries(self):
        """Ошибка HTTP и неизвестный provider не раскрывают исходную диагностику."""
        os.environ.update(WORKSHEET_CODEX_PROVIDER="responses_direct", WORKSHEET_CODEX_API_KEY="synthetic")
        with patch.object(catalog, "post_model_response", side_effect=HTTPError("https://secret.example", 401, "raw secret", None, None)):
            result = catalog.effective_models()
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "authentication")
        self.assertNotIn("secret", json.dumps(result))
        os.environ["WORKSHEET_CODEX_PROVIDER"] = "https://attacker.example"
        with patch.object(catalog, "post_model_response") as transport, patch.object(catalog, "_native_output") as native:
            result = catalog.effective_models()
        self.assertEqual(result["provider"], "unknown")
        self.assertEqual(result["error"], "unsupported_provider")
        transport.assert_not_called()
        native.assert_not_called()

    def test_public_catalog_schema_rejects_raw_fields_duplicate_ids_and_unknown_efforts(self):
        """Gateway допускает только публичную schema и не пропускает инструкции или неизвестные уровни."""
        value = catalog._empty("sandbox_admin")
        value.update(available=True, source="codex_active_catalog", models=[{
            "id": "gpt-test", "is_default": True, "reasoning_efforts": ["low", "high"],
            "default_reasoning_effort": "low",
        }])
        checked = catalog.validate_catalog(value)
        checked["models"].clear()
        self.assertEqual(len(value["models"]), 1)
        bad_values = [
            {**value, "base_instructions": "private"},
            {**value, "models": value["models"] * 2},
            {**value, "models": [{**value["models"][0], "id": "bad\nID"}]},
            {**value, "models": [{**value["models"][0], "reasoning_efforts": ["invented"]}]},
            {**value, "error": ["untrusted"]},
            {**value, "fetched_at": True},
        ]
        for bad in bad_values:
            with self.subTest(keys=list(bad)), self.assertRaises(ValueError):
                catalog.validate_catalog(bad)

    def test_cache_serializes_fetch_and_returns_independent_values(self):
        """Одновременные вызовы делают один fetch, кеш не меняется через ответ клиента."""
        os.environ["WORKSHEET_CODEX_PROVIDER"] = "sandbox_admin"
        started = threading.Event()
        release = threading.Event()
        def fetch(provider):
            """Удержать первый fetch до запуска остальных потоков теста."""
            started.set()
            self.assertTrue(release.wait(2))
            return catalog._empty(provider)
        with patch.object(catalog, "_fetch", side_effect=fetch) as mocked, ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(catalog.effective_models) for _ in range(8)]
            self.assertTrue(started.wait(2))
            release.set()
            results = [future.result(timeout=3) for future in futures]
        self.assertEqual(mocked.call_count, 1)
        results[0]["models"].append({"id": "tampered"})
        self.assertEqual(catalog.effective_models()["models"], [])
        os.environ["WORKSHEET_CODEX_MODEL"] = "changed"
        with patch.object(catalog, "_fetch", return_value=catalog._empty("sandbox_admin")) as mocked:
            catalog.effective_models()
        mocked.assert_called_once()

    def _executable(self, directory, body):
        """Создать локальную synthetic CLI без сетевых вызовов и реальных credentials."""
        executable = Path(directory) / "codex"
        executable.write_text(f"#!{sys.executable}\n" + body)
        executable.chmod(0o700)
        return str(executable)

    def test_native_process_uses_only_fixed_command_and_environment(self):
        """Native вызов не получает посторонние секреты и не запускает exec или bundled."""
        os.environ.update(HOME="/synthetic/home", PATH="/usr/bin:/bin", LANG="C.UTF-8", UNRELATED_SECRET="must-not-leak")
        with TemporaryDirectory() as directory:
            binary = self._executable(directory, "import json, os, sys\nprint(json.dumps({'args': sys.argv[1:], 'keys': sorted(os.environ)}))\n")
            output = json.loads(catalog._native_output(binary))
        self.assertEqual(output["args"], ["-c", 'model_provider="openai"', "debug", "models"])
        self.assertNotIn("UNRELATED_SECRET", output["keys"])
        self.assertTrue(set(output["keys"]) <= {"HOME", "PATH", "LANG", "LC_CTYPE"})

    def test_native_output_limit_and_timeout_reap_process(self):
        """Превышение stdout и deadline убивают собственный процесс до возврата."""
        with TemporaryDirectory() as directory:
            pidfile = Path(directory) / "pid"
            for body, patches, expected in (
                ("import os, time\nprint('x' * 4096, flush=True)\ntime.sleep(20)\n", {"MAX_NATIVE_BYTES": 128}, "response_too_large"),
                ("import time\ntime.sleep(20)\n", {"FETCH_TIMEOUT": 0.1}, "timeout"),
            ):
                source = f"import os\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\n" + body
                binary = self._executable(directory, source)
                with patch.multiple(catalog, **patches), self.assertRaises(catalog.CatalogFailure) as error:
                    catalog._native_output(binary)
                self.assertEqual(str(error.exception), expected)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), 0)


if __name__ == "__main__":
    unittest.main()
