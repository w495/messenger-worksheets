"""Проверить читаемые страницы каталога без сети и Messenger-учётных данных."""
from __future__ import annotations

import re
import unittest

from messenger_worksheets.model_presentation import catalogue_pages
from messenger_worksheets.text_messages import utf16_length


class ModelPresentationTest(unittest.TestCase):
    def model(self, identifier, **fields):
        """Создать модель с безопасными подтверждёнными характеристиками для pure renderer."""
        return {"id": identifier, "is_default": False, "description": "Код и повседневные задачи.",
                "context_window": 500000, "max_context_window": 872000,
                "input_modalities": ["text", "image"], "reasoning_efforts": ["low", "high"],
                "default_reasoning_effort": "low", "reasoning_descriptions": {},
                "details_source": "codex_catalog", "details_url": None, **fields}

    def catalog(self, models):
        """Обернуть уже отсортированные записи в schema каталога без side effects."""
        return {"models": models, "source": "codex_active_catalog", "truncated": False}

    def test_cards_have_paragraphs_headers_and_at_most_three_models(self):
        """Десять моделей дают читаемые страницы с шапками, а не одну простыню."""
        records = [self.model(f"gpt-6.{index}") for index in range(9, -1, -1)]
        pages = catalogue_pages(self.catalog(records), task=True, current="gpt-6.5", plain=str)
        self.assertGreaterEqual(len(pages), 4)
        for index, page in enumerate(pages, 1):
            self.assertIn(f"{index}/{len(pages)}", page)
            self.assertIn("Выбрана: `gpt-6.5`", page)
            self.assertLessEqual(len(re.findall(r"(?:^|\n\n)`gpt-[^`]+`(?: — выбрана)?\n\n", page)), 3)
            self.assertLessEqual(utf16_length(page), 4096)
        text = "\n".join(pages)
        self.assertLess(text.index("`gpt-6.9`\n\n"), text.index("`gpt-6.0`\n\n"))
        self.assertIn("Для чего: Код", text)
        self.assertIn("Вход: текст, изображения", text)
        self.assertIn("Контекст: 500 000 токенов", text)

    def test_equivalent_published_versions_share_description_but_not_different_capabilities(self):
        """Один verified card URL объединяет равные снимки; отличающийся контекст остаётся отдельной карточкой."""
        url = "https://developers.openai.com/api/docs/models/gpt-5.4"
        fields = {"details_source": "openai_docs", "details_url": url}
        models = [self.model("gpt-5.4-2026-03-05", **fields), self.model("gpt-5.4", **fields),
                  self.model("gpt-5.4-other", **fields, context_window=400000)]
        pages = catalogue_pages(self.catalog(models), task=False, current="gpt-5.4-2026-03-05", plain=str)
        text = "\n".join(pages)
        self.assertEqual(text.count("Для чего:"), 2)
        self.assertIn("Версии с теми же характеристиками:", text)
        self.assertIn("`gpt-5.4-2026-03-05` — выбрана", text)
        self.assertIn("`gpt-5.4-other`", text)

    def test_long_unicode_cards_and_version_groups_preserve_every_id_within_limit(self):
        """Длинные emoji-описания и многочисленные версии не теряют ID и учитывают UTF-16."""
        url = "https://developers.openai.com/api/docs/models/gpt-6.2-sol"
        records = [self.model("gpt-6.2-sol-" + str(index) + "x" * 70,
                              description="🧠" * 600, details_source="openai_docs", details_url=url)
                   for index in range(80)]
        pages = catalogue_pages(self.catalog(records), task=False, current=None, plain=str)
        text = "\n".join(pages)
        self.assertTrue(all(utf16_length(page) <= 4096 for page in pages))
        for record in records:
            self.assertIn(record["id"], text)
        self.assertIn("продолжение версий", text)

    def test_unknown_details_and_missing_selected_model_are_explicit(self):
        """Нет источника — нет выдуманных параметров; неизвестный selected ID остаётся виден в шапке."""
        item = self.model("gpt-unknown", description="", context_window=None,
                          max_context_window=None, input_modalities=[], reasoning_efforts=[])
        pages = catalogue_pages(self.catalog([item]), task=False, current="chosen-outside-catalog", plain=str)
        text = "\n".join(pages)
        self.assertIn("описание пока не опубликовано", text)
        self.assertIn("источник пока не сообщил", text)
        self.assertIn("пока отсутствует", text)
        self.assertNotIn("500 000", text)

    def test_all_messages_count_emoji_in_utf16_and_skip_blank_only_boundary(self):
        """Разделитель абзаца сразу после большого блока не превращается в отдельное пустое сообщение."""
        from messenger_worksheets.text_messages import split_messages
        text = "🧠" * 2048 + "\n\n" + "🧠" * 3000
        parts = split_messages(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(part.strip() for part in parts))
        self.assertTrue(all(utf16_length(part) <= 4096 for part in parts))


if __name__ == "__main__":
    unittest.main()
