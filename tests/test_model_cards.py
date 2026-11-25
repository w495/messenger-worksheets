"""Проверить точность, границы и изоляцию справки официальных моделей."""

from concurrent.futures import ThreadPoolExecutor
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from messenger_worksheets import model_cards as cards


# Форма реальных ссылок /models/all на 2026-11-25; текст намеренно тестовый.
INDEX = b'''<html><script>not a model</script><body>
<a href="/api/docs/models/gpt-future">
 <div class="flex"><img alt="gpt-future" src="/image.png">
 <div><div class="font-semibold">GPT Future</div>
 <div class="text-sm text-secondary">Useful for <span>precise</span> tasks &amp; text.</div>
 </div></div></a>
<a href="/api/docs/models/gpt-5.3-codex">
 <div class="text-sm text-secondary">Current index description.</div></a>
</body></html>'''


class ModelCardsTest(unittest.TestCase):
    """Использовать только синтетическую сеть и изолированный процессный кэш."""

    def setUp(self):
        """Сбросить кэш перед тестом без изменения окружения и чтения секретов."""
        cards._CACHE.clear()
        cards._CACHE_UNTIL = 0.0

    def test_one_fixed_credential_free_get_and_shared_cache(self):
        """Несколько ID и потоков делят один GET с ограничениями и без auth."""
        with patch.object(cards, "post_model_response", return_value=INDEX) as transport:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(cards.model_card, ["gpt-future"] * 8))
            missing = cards.model_card("gpt-unknown")
        transport.assert_called_once()
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, "https://developers.openai.com/api/docs/models/all")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(transport.call_args.kwargs,
                         {"timeout": 5.0, "max_bytes": 4 * 1024 * 1024})
        self.assertEqual(results[0], {
            "description": "Useful for precise tasks & text.", "details_source": "openai_docs",
            "details_url": "https://developers.openai.com/api/docs/models/gpt-future"})
        self.assertEqual(missing, {})
        results[0]["description"] = "changed"
        self.assertNotEqual(cards.model_card("gpt-future")["description"], "changed")

    def test_reviewed_codex_efforts_and_russian_fallback_survive_network_failure(self):
        """API default получает только четыре явно опубликованных уровня Codex."""
        with patch.object(cards, "post_model_response", side_effect=URLError("private diagnostic")) as transport:
            first = cards.model_card("gpt-5.3-codex")
            second = cards.model_card("gpt-5.4-pro")
        transport.assert_called_once()
        self.assertEqual(first["reasoning_efforts"], ["low", "medium", "high", "xhigh"])
        self.assertEqual(first["context_window"], 400_000)
        self.assertEqual(first["input_modalities"], ["text", "image"])
        self.assertNotIn("default_reasoning_effort", first)
        self.assertIn("программирование", first["description"])
        self.assertEqual(second["reasoning_efforts"], ["medium", "high", "xhigh"])
        self.assertEqual(second["default_reasoning_effort"], "medium")
        self.assertNotIn("private", str(first))

    def test_only_published_snapshot_aliases_receive_metadata(self):
        """Произвольная дата и похожий вариант не наследуют свойства семейства."""
        with patch.object(cards, "post_model_response", return_value=INDEX):
            dated = cards.model_card("gpt-5.4-2026-03-05")
            base = cards.model_card("gpt-5.4")
            self.assertEqual(dated, base)
            self.assertEqual(cards.model_card("gpt-5.4-2099-01-01"), {})
            self.assertEqual(cards.model_card("gpt-5.4-micro"), {})
            self.assertEqual(cards.model_card("gpt-5.3-codex-spark"), {})
            preview = cards.model_card("o1-preview-2024-09-12")
            self.assertEqual(preview["context_window"], 128_000)
            self.assertEqual(preview["input_modalities"], ["text"])
            self.assertEqual(cards.model_card("o1")["context_window"], 200_000)
            self.assertEqual(cards.model_card("gpt-4-turbo-preview"), {})
            self.assertEqual(cards.model_card("gpt-4o-2024-05-13"), {})

    def test_native_like_new_models_do_not_acquire_unpublished_efforts(self):
        """Запрет none в API-карточке не доказывает остальные уровни native route."""
        with patch.object(cards, "post_model_response", return_value=INDEX):
            result = cards.model_card("gpt-6.2-sol")
        self.assertEqual(result["reasoning_efforts"], [])
        self.assertNotIn("default_reasoning_effort", result)
        self.assertEqual(result["context_window"], 1_050_000)
        self.assertNotIn("max_context_window", result)

    def test_additional_explicit_guide_efforts_do_not_spread_to_other_models(self):
        """Явные списки руководств применяются только к перечисленным ID и снимкам."""
        with patch.object(cards, "post_model_response", return_value=INDEX):
            for identifier in ("gpt-5-mini", "gpt-5-nano-2025-08-07"):
                self.assertEqual(cards.model_card(identifier)["reasoning_efforts"],
                                 ["minimal", "low", "medium", "high"])
            for identifier in ("o3", "o3-mini-2025-01-31", "o4-mini"):
                result = cards.model_card(identifier)
                self.assertEqual(result["reasoning_efforts"], ["low", "medium", "high"])
                self.assertNotIn("default_reasoning_effort", result)
            self.assertEqual(cards.model_card("o3-pro")["reasoning_efforts"], [])
            self.assertEqual(cards.model_card("o1")["reasoning_efforts"], [])
            self.assertEqual(cards.model_card("gpt-4.1")["reasoning_efforts"], [])
            self.assertEqual(cards.model_card("gpt-5.5-2026-04-23")["reasoning_efforts"],
                             ["none", "low", "medium", "high", "xhigh"])
            self.assertEqual(cards.model_card("gpt-5.5")["default_reasoning_effort"], "medium")
            self.assertEqual(cards.model_card("gpt-5.5-pro-2026-04-23")["reasoning_efforts"],
                             ["medium", "high", "xhigh"])
            self.assertEqual(cards.model_card("gpt-5.5-pro")["default_reasoning_effort"], "high")
            self.assertEqual(cards.model_card("gpt-5.5-2099-01-01"), {})

    def test_invalid_identifier_never_reaches_network(self):
        """URL, обход пути, управляющие символы и неверный тип не запускают GET."""
        with patch.object(cards, "post_model_response") as transport:
            for value in (None, [], {}, "../gpt-5", "https://evil.example", "gpt-5\n", "gpt-5?x=1", "a" * 102):
                self.assertEqual(cards.model_card(value), {})
        transport.assert_not_called()

    def test_parser_ignores_external_links_scripts_metadata_and_bounded_text(self):
        """Индекс выдаёт лишь текст назначения и URL фиксированного origin."""
        fragment = '''<a href="https://evil.example/api/docs/models/gpt-evil">
          <div class="text-sm text-secondary">secret</div></a>
          <a href="/api/docs/models/gpt-query?url=bad">
          <div class="text-sm text-secondary">secret</div></a>
          <a href="/api/docs/models/gpt-safe"><div class="font-semibold">Title</div>
          <div class="text-secondary text-sm">Hello<br>world<script>secret</script>
          &lt;b&gt; text\u202e\x00''' + 'x' * 2000 + '''</div></a>'''
        with patch.object(cards, "post_model_response", return_value=fragment.encode()):
            result = cards.model_card("gpt-safe")
            self.assertEqual(cards.model_card("gpt-evil"), {})
        self.assertEqual(result.keys(), {"description", "details_source", "details_url"})
        self.assertLessEqual(len(result["description"]), 600)
        self.assertIn("Hello world", result["description"])
        self.assertNotIn("secret", result["description"])
        self.assertNotIn("<", result["description"])
        self.assertNotIn("\u202e", result["description"])
        self.assertNotIn("\x00", result["description"])
        self.assertNotIn("Title", result["description"])

    def test_failed_refresh_retains_old_index_and_waits_for_next_ttl(self):
        """После часа выполняется один refresh; ошибка сохраняет прежнюю справку."""
        with patch.object(cards.time, "monotonic", return_value=100.0), patch.object(
                cards, "post_model_response", return_value=INDEX) as transport:
            expected = cards.model_card("gpt-future")
            cards.model_card("gpt-unknown")
            transport.assert_called_once()
        with patch.object(cards.time, "monotonic", return_value=3701.0), patch.object(
                cards, "post_model_response", side_effect=HTTPError(cards.INDEX_URL, 302, "redirect", None, None)) as transport:
            self.assertEqual(cards.model_card("gpt-future"), expected)
            self.assertEqual(cards.model_card("gpt-future"), expected)
            transport.assert_called_once()

    def test_oversize_invalid_utf8_empty_and_timeout_fail_to_reviewed_facts(self):
        """Повреждение справки не мешает каталогу и не создаёт неподтверждённых полей."""
        bodies = [b"x" * (cards.MAX_DOC_BYTES + 1), b"\xff", b"<html>changed layout</html>"]
        for body in bodies:
            cards._CACHE.clear()
            cards._CACHE_UNTIL = 0.0
            with self.subTest(body_size=len(body)), patch.object(cards, "post_model_response", return_value=body):
                self.assertEqual(cards.model_card("gpt-future"), {})
                self.assertEqual(cards.model_card("gpt-5-pro")["reasoning_efforts"], ["high"])
        cards._CACHE_UNTIL = 0.0
        with patch.object(cards, "post_model_response", side_effect=subprocess.TimeoutExpired("test", 5)):
            self.assertEqual(cards.model_card("gpt-future"), {})

    def test_index_max_cards_and_returned_static_lists_are_independent(self):
        """Ограничение числа карточек и копирование списков защищают общий кэш."""
        fragment = ''.join(f'<a href="/api/docs/models/gpt-{i}"><div class="text-sm text-secondary">Purpose {i}</div></a>'
                           for i in range(cards.MAX_CARDS + 10))
        with patch.object(cards, "post_model_response", return_value=fragment.encode()):
            self.assertEqual(cards.model_card("gpt-0")["description"], "Purpose 0")
            self.assertEqual(cards.model_card(f"gpt-{cards.MAX_CARDS}"), {})
            result = cards.model_card("gpt-5.3-codex")
            result["reasoning_efforts"].append("ultra")
            self.assertNotIn("ultra", cards.model_card("gpt-5.3-codex")["reasoning_efforts"])


if __name__ == "__main__":
    unittest.main()
