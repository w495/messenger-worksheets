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
        self.original_card_lookup = catalog._model_card
        self.cards = patch.object(catalog, "_model_card", return_value={})
        self.card_lookup = self.cards.start()
        self.addCleanup(self.cards.stop)

    def test_direct_api_projects_text_models_without_secret_or_raw_metadata(self):
        """GET использует фиксированный origin и публикует только безопасные текстовые ID."""
        os.environ.update(WORKSHEET_CODEX_PROVIDER="responses_direct", WORKSHEET_CODEX_API_KEY="synthetic-secret", WORKSHEET_CODEX_MODEL="gpt-6.2")
        payload = {"data": [{"id": value, "instructions": "private instructions"} for value in
                            ("gpt-6.2", "o3", "gpt-audio", "gpt-6-search-preview", "text-embedding-3-large", "gpt-image-1", "gpt-realtime", "gpt-x\nsecret", "unknown", "o1-mini")]}
        with patch.object(catalog, "post_model_response", return_value=json.dumps(payload).encode()) as transport:
            result = catalog.effective_models()
        self.assertEqual([item["id"] for item in result["models"]], ["gpt-6.2", "o3", "o1-mini"])
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
                               "supported_reasoning_levels": [{"effort": "high", "description": "More deliberate reasoning"}, {"effort": "medium"}, {"effort": "invented"}],
                               "default_reasoning_level": "medium", "instructions": "secret"},
                              {"slug": "gpt-hidden", "visibility": "hide"}, {"slug": "gpt-bad\n", "visibility": "list"}]}
        with patch.object(catalog, "_native_output", return_value=json.dumps(payload).encode()) as native:
            result = catalog.effective_models()
        native.assert_called_once_with("/synthetic/codex")
        self.assertEqual(result["models"], [{"id": "gpt-6.2-sol", "is_default": False,
                                            "reasoning_efforts": ["medium", "high"], "default_reasoning_effort": "medium",
                                            **{**catalog._empty_details(), "reasoning_source": "codex_catalog", "reasoning_descriptions": {"high": "More deliberate reasoning"}}}])
        self.assertEqual(result["source"], "codex_active_catalog")
        self.assertNotIn("secret", json.dumps(result))

    def test_response_and_projection_limits_fail_closed_or_truncate(self):
        """Большое тело отклоняется, публичный каталог ограничен числом и размером."""
        os.environ.update(WORKSHEET_CODEX_PROVIDER="responses_direct", WORKSHEET_CODEX_API_KEY="synthetic")
        with patch.object(catalog, "post_model_response", return_value=b"x" * (catalog.MAX_API_BYTES + 1)):
            self.assertEqual(catalog.effective_models()["error"], "response_too_large")
        entries = [{"id": f"gpt-{index:04d}"} for index in range(300)]
        models, truncated = catalog._project(entries, native=False, configured=None)
        self.assertGreater(len(models), 0)
        self.assertLessEqual(len(models), 256)
        self.assertTrue(truncated)
        with patch.object(catalog, "MAX_PUBLIC_BYTES", 200):
            models, truncated = catalog._project(entries, native=False, configured=None)
        self.assertLessEqual(len(json.dumps(models, ensure_ascii=False).encode()), 200)
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

    def test_native_capabilities_keep_catalog_values_and_strip_markup_controls(self):
        """Native context и модальности берутся из CLI, API карточки не подменяют их."""
        entries = [{"slug": "gpt-6.2-sol", "visibility": "list",
                    "description": "**Fast** [coding](https://example.test) <b>model</b>\n\u202e!",
                    "context_window": 500000, "max_context_window": 872000,
                    "input_modalities": ["text", "image", "text", "unknown"],
                    "supported_reasoning_levels": [{"effort": "high", "description": "**Increased** reasoning"},
                                                   {"effort": "ultra", "description": "Maximum reasoning with automatic task delegation"}], "default_reasoning_level": "high",
                    "base_instructions": "must never appear"}]
        models, truncated = catalog._project(entries, native=True, configured="gpt-6.2-sol")
        model = models[0]
        self.assertFalse(truncated)
        self.assertEqual(model["description"], "Fast coding model !")
        self.assertEqual(model["context_window"], 500000)
        self.assertEqual(model["max_context_window"], 872000)
        self.assertEqual(model["input_modalities"], ["text", "image"])
        self.assertEqual(model["details_source"], "codex_catalog")
        self.assertEqual(model["reasoning_source"], "codex_catalog")
        self.assertEqual(model["reasoning_descriptions"], {"high": "Increased reasoning", "ultra": "Maximum reasoning with automatic task delegation"})
        self.assertIsNone(model["details_url"])
        self.assertNotIn("must never appear", json.dumps(model))
        self.card_lookup.assert_not_called()

    def test_api_exact_card_enrichment_preserves_unknowns_and_does_not_infer_family(self):
        """Только точная карточка добавляет свойства и подтверждённые четыре уровня."""
        known = {"description": "Программирование и работа с кодом.", "context_window": 400000,
                 "input_modalities": ["text", "image"], "reasoning_efforts": ["low", "medium", "high", "xhigh"],
                 "details_source": "openai_docs", "details_url": "https://developers.openai.com/api/docs/models/gpt-5.3-codex"}
        self.card_lookup.side_effect = lambda identifier: known if identifier == "gpt-5.3-codex" else {}
        models, _ = catalog._project([{"id": "gpt-5.3-codex"}, {"id": "gpt-5.3-codex-unpublished"}], native=False, configured=None)
        first, unknown = models
        self.assertEqual(first["description"], known["description"])
        self.assertEqual(first["reasoning_efforts"], ["low", "medium", "high", "xhigh"])
        self.assertEqual(first["reasoning_source"], "openai_docs")
        self.assertEqual(first["context_window"], 400000)
        self.assertEqual(first["details_source"], "openai_docs")
        self.assertEqual(unknown["description"], "")
        self.assertEqual(unknown["reasoning_efforts"], [])
        self.assertEqual(unknown["reasoning_source"], "unavailable")
        self.assertIsNone(unknown["context_window"])
        self.assertEqual([call.args[0] for call in self.card_lookup.call_args_list], ["gpt-5.3-codex", "gpt-5.3-codex-unpublished"])

    def test_api_reported_fields_and_invalid_metadata_are_safely_projected(self):
        """API capabilities допустимы только в типах/лимитах; bool/float и неизвестные модальности отбрасываются."""
        models, _ = catalog._project([{"id": "gpt-reported", "description": "Описание" * 200,
                                       "context_window": True, "max_context_window": 1.5,
                                       "input_modalities": ["audio", "video", "bogus"],
                                       "reasoning_efforts": ["none", "high", "unknown"], "default_reasoning_effort": "none"}], native=False, configured=None)
        model = models[0]
        self.assertEqual(len(model["description"]), 600)
        self.assertIsNone(model["context_window"])
        self.assertIsNone(model["max_context_window"])
        self.assertEqual(model["input_modalities"], ["audio", "video"])
        self.assertEqual(model["details_source"], "api_metadata")
        self.assertEqual(model["reasoning_source"], "api_metadata")
        self.assertEqual(model["reasoning_efforts"], ["none", "high"])

    def test_rich_schema_and_legacy_normalization_enforce_metadata_boundaries(self):
        """Старые четыре поля дополняются unknown; новая schema блокирует markup, сторонние URL и raw инструкции."""
        value = catalog._empty("sandbox_admin")
        old = {"id": "gpt-test", "is_default": False, "reasoning_efforts": ["high"], "default_reasoning_effort": None}
        value.update(available=True, source="codex_active_catalog", models=[old])
        normalized = catalog.validate_catalog(value)
        model = normalized["models"][0]
        self.assertEqual(set(model), catalog.BASE_MODEL_FIELDS | catalog.DETAIL_MODEL_FIELDS)
        self.assertEqual(model["description"], "")
        self.assertEqual(model["reasoning_source"], "codex_catalog")
        self.assertEqual(set(old), catalog.BASE_MODEL_FIELDS)
        valid = {**model, "description": "Русское описание", "context_window": 1050000,
                 "details_source": "openai_docs", "details_url": "https://developers.openai.com/api/docs/models/gpt-test"}
        value["models"] = [valid]
        self.assertEqual(catalog.validate_catalog(value)["models"][0], valid)
        for update in ({"description": "**markup**"}, {"description": "bad\u202evalue"}, {"description": "x" * 601},
                       {"context_window": True}, {"context_window": 1.5}, {"context_window": 100000001},
                       {"input_modalities": ["text", "unknown"]}, {"details_source": "untrusted"},
                       {"reasoning_source": "invented"}, {"reasoning_descriptions": {"ultra": "not supported"}},
                       {"reasoning_descriptions": {"high": "**unsafe markup**"}}, {"reasoning_descriptions": {"high": "x" * 301}},
                       {"base_instructions": "raw private"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                catalog.validate_catalog({**value, "models": [{**valid, **update}]})
        for url in ("http://developers.openai.com/a", "https://evil.test/a", "https://openai.com.evil.test/a",
                    "https://user:password@openai.com/a", "https://openai.com/a?token=secret", "https://openai.com/a#secret",
                    "https://openai.com/a)bad", "https://openai.com:444/a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                catalog.validate_catalog({**value, "models": [{**valid, "details_url": url}]})

    def test_russian_descriptions_use_utf8_budget_and_full_projection_is_bounded(self):
        """Лимит соответствует UTF-8 wire JSON, а не шестикратному ASCII escaping русского текста."""
        self.card_lookup.return_value = {"description": "Описание назначения модели. " * 8,
                                         "details_source": "openai_docs", "details_url": "https://developers.openai.com/api/docs/models/gpt-test"}
        models, truncated = catalog._project([{"id": f"gpt-{index}"} for index in range(54)], native=False, configured=None)
        self.assertEqual(len(models), 54)
        self.assertFalse(truncated)
        self.assertLessEqual(len(json.dumps(models, ensure_ascii=False).encode()), catalog.MAX_PUBLIC_BYTES)
        old = [{"id": "gpt-" + str(index), "is_default": False, "reasoning_efforts": [], "default_reasoning_effort": None} for index in range(256)]
        value = catalog._empty("responses_direct")
        value.update(available=True, source="openai_models_api", models=old)
        normalized = catalog.validate_catalog(value)
        self.assertLessEqual(len(json.dumps(normalized["models"], ensure_ascii=False).encode()), catalog.MAX_PUBLIC_BYTES)
        self.assertTrue(normalized["truncated"])

    def test_previous_eleven_fields_default_empty_reasoning_descriptions(self):
        """Worker прежней версии с 11 полями остаётся совместимым без выдуманных описаний."""
        models, _ = catalog._project([{"slug": "gpt-legacy", "visibility": "list"}], native=True, configured=None)
        models[0].pop("reasoning_descriptions")
        value = catalog._empty("sandbox_admin")
        value.update(available=True, source="codex_active_catalog", models=models)
        normalized = catalog.validate_catalog(value)
        self.assertEqual(normalized["models"][0]["reasoning_descriptions"], {})
        self.assertNotIn("reasoning_descriptions", value["models"][0])

    def test_real_card_helper_enriches_default_and_failure_keeps_api_model(self):
        """Интеграция использует точную реальную карточку offline; сбой справки не скрывает ID."""
        from messenger_worksheets import model_cards
        with patch.object(catalog, "_model_card", self.original_card_lookup), patch.object(model_cards, "_index_cards", return_value={}):
            models, _ = catalog._project([{"id": "gpt-5.3-codex"}], native=False, configured=None)
        self.assertEqual(models[0]["reasoning_efforts"], ["low", "medium", "high", "xhigh"])
        self.assertEqual(models[0]["context_window"], 400000)
        self.assertEqual(models[0]["details_source"], "openai_docs")
        with patch.object(catalog, "_model_card", self.original_card_lookup), patch.object(model_cards, "model_card", side_effect=RuntimeError("private failure")):
            models, _ = catalog._project([{"id": "gpt-unknown"}], native=False, configured=None)
        self.assertEqual(models[0]["id"], "gpt-unknown")
        self.assertEqual(models[0]["description"], "")
        self.assertEqual(models[0]["reasoning_efforts"], [])
        self.assertNotIn("private failure", json.dumps(models))

    def test_api_created_sorts_newest_across_families_without_changing_schema(self):
        """Реальный created старше имени поколения: o4 и GPT сравниваются только по дате."""
        entries = [{"id": "gpt-9", "created": 100}, {"id": "o4-mini", "created": 300},
                   {"id": "o3", "created": 200}, {"id": "gpt-4.1", "created": 400},
                   {"id": "gpt-6.2-sol", "created": 300}]
        models, truncated = catalog._project(entries, native=False, configured=None)
        self.assertFalse(truncated)
        self.assertEqual([model["id"] for model in models], ["gpt-4.1", "o4-mini", "gpt-6.2-sol", "o3", "gpt-9"])
        self.assertTrue(all(set(model) == catalog.BASE_MODEL_FIELDS | catalog.DETAIL_MODEL_FIELDS for model in models))
        self.assertNotIn("created", json.dumps(models))
        self.assertEqual(entries[0]["id"], "gpt-9")

    def test_api_snapshot_date_is_fallback_and_real_created_remains_authoritative(self):
        """Валидный ISO suffix задаёт дату только без created; неверная дата не угадывается."""
        entries = [{"id": "gpt-9-2026-12-31", "created": 123},
                   {"id": "gpt-4.1-2025-04-14"}, {"id": "o3-2026-06-01"},
                   {"id": "gpt-6.2-2026-02-30"}, {"id": "gpt-5.10"}]
        models, _ = catalog._project(entries, native=False, configured=None)
        self.assertEqual([model["id"] for model in models],
                         ["o3-2026-06-01", "gpt-4.1-2025-04-14", "gpt-9-2026-12-31", "gpt-6.2-2026-02-30", "gpt-5.10"])
        self.assertIsNone(catalog._entry_timestamp({"id": "gpt-6.2-2026-02-30"}))
        self.assertIsNone(catalog._entry_timestamp({"id": "gpt-6.2-20260101"}))

    def test_invalid_created_does_not_override_natural_numeric_generation(self):
        """Bool/отрицательные/NaN/строки не выдаются за дату, 5.10 располагается выше 5.9."""
        entries = [{"id": "gpt-5.9", "created": True}, {"id": "gpt-5.10", "created": -1},
                   {"id": "gpt-6.1", "created": "999999"}, {"id": "o3", "created": False},
                   {"id": "gpt-6", "created": float("nan")}]
        models, _ = catalog._project(entries, native=False, configured=None)
        self.assertEqual([model["id"] for model in models], ["gpt-6.1", "gpt-6", "gpt-5.10", "o3", "gpt-5.9"])
        for invalid in (True, False, -1, 0, "123", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(value=repr(invalid)):
                self.assertIsNone(catalog._created_timestamp(invalid))
        self.assertEqual(catalog._created_timestamp(123.5), 123.5)

    def test_native_generations_preserve_provider_priority_and_unknown_family_positions(self):
        """Native GPT поколения идут численно вниз, sol/astra одного поколения сохраняют priority."""
        identifiers = ["vendor-other", "gpt-5.6-sol", "gpt-6.2-sol", "gpt-5.9-sol", "gpt-6.2-astra",
                       "gpt-6.1-terra", "gpt-6-sol", "gpt-5.10-sol"]
        entries = [{"slug": identifier, "visibility": "list", "created": 10000 - index} for index, identifier in enumerate(identifiers)]
        models, _ = catalog._project(entries, native=True, configured=None)
        self.assertEqual([model["id"] for model in models], ["vendor-other", "gpt-6.2-sol", "gpt-6.2-astra",
                                                             "gpt-6.1-terra", "gpt-6-sol", "gpt-5.10-sol", "gpt-5.9-sol", "gpt-5.6-sol"])
        self.assertEqual([entry["slug"] for entry in entries], identifiers)

    def test_duplicate_api_id_uses_latest_reliable_metadata_and_ties_are_stable(self):
        """Дубли не размножают записи; новый created выбирает metadata, равный сохраняет первую."""
        entries = [{"id": "gpt-5", "created": 10, "description": "old"},
                   {"id": "gpt-4", "created": 30, "description": "other"},
                   {"id": "gpt-5", "created": 40, "description": "latest"},
                   {"id": "gpt-5", "created": 40, "description": "same date later in provider"},
                   {"id": "gpt-5", "created": True, "description": "invalid date"}]
        models, _ = catalog._project(entries, native=False, configured=None)
        self.assertEqual([model["id"] for model in models], ["gpt-5", "gpt-4"])
        self.assertEqual(models[0]["description"], "latest")
        models, _ = catalog._project([{"slug": "gpt-6.2-sol", "visibility": "list", "description": "provider first"},
                                      {"slug": "gpt-6.2-sol", "visibility": "list", "description": "later duplicate", "created": 999}],
                                     native=True, configured=None)
        self.assertEqual(models[0]["description"], "provider first")

    def test_sort_precedes_both_count_and_public_size_caps(self):
        """Поздно пришедшая новая модель сохраняется первой при обоих видах усечения."""
        entries = [{"id": "gpt-" + str(index), "created": index} for index in range(1, 301)]
        with patch.object(catalog, "MAX_MODELS", 2):
            models, truncated = catalog._project(entries, native=False, configured=None)
        self.assertTrue(truncated)
        self.assertEqual([model["id"] for model in models], ["gpt-300", "gpt-299"])
        with patch.object(catalog, "MAX_PUBLIC_BYTES", 550):
            models, truncated = catalog._project(entries, native=False, configured=None)
        self.assertTrue(truncated)
        self.assertGreater(len(models), 0)
        self.assertEqual(models[0]["id"], "gpt-300")
        self.assertLessEqual(len(json.dumps(models, ensure_ascii=False).encode()), 550)

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
