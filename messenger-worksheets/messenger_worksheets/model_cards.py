"""Справка об API-моделях из публичных карточек OpenAI, без вывода по имени.

Индекс /models/all обновляется одним GET на час. Проверенная 2026-11-25
выжимка отдельных карточек сохраняет полезную справку при недоступной сети.
Возможности модели не означают доступность инструментов в данном приложении.
"""

from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
import re
import threading
import time
import unicodedata
from urllib.error import URLError
from urllib.request import Request
import subprocess

from .model_http import post_model_response


DOCS_ROOT = "https://developers.openai.com/api/docs/models/"
INDEX_URL = DOCS_ROOT + "all"
MAX_DOC_BYTES = 4 * 1024 * 1024
DOC_TIMEOUT = 5.0
CACHE_SECONDS = 60 * 60
MAX_CARDS = 1024
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,100}\Z")
_CACHE: dict[str, dict] = {}
_CACHE_UNTIL = 0.0
_LOCK = threading.Lock()

# Каждая строка проверена по DOCS_ROOT + точный ключ, 2026-11-25.
# Поля: русский пересказ назначения, context_window, входные модальности,
# только явно перечисленные карточкой reasoning.effort, явно указанный default,
# опубликованные снимки с теми же приведёнными свойствами. Даты не обрезаются.
# Пустые efforts означают отсутствие опубликованного списка, а не поддержку none.
# Дополнительные явные источники усилий (проверены 2026-11-25):
# gpt-5-mini/nano: https://developers.openai.com/api/docs/guides/gpt-5
# Раздел Model, API, and feature updates прямо перечисляет три точных ID и 4 уровня.
# o3/o4-mini: https://developers.openai.com/cookbook/examples/partners/model_selection_guide/model_selection_guide
# Раздел o-series Models (o3, o4-mini) прямо перечисляет low/medium/high.
# o3-mini: https://openai.com/index/openai-o3-mini/ (3 уровня API, не default ChatGPT).
_REVIEWED = {
    "gpt-6.2-sol": ("Повседневная работа, программирование и масштабируемые агенты; принимает текст и изображения, отвечает текстом.", 1_050_000, ("text", "image"), (), None, ()),
    "gpt-6.2-astra": ("Сложные проекты: разработка и изменение ПО, работа с документами и компьютером; принимает текст и изображения.", 1_050_000, ("text", "image"), (), None, ()),
    "gpt-5.3-codex": ("Агентное программирование в Codex и сходных средах; принимает текст и изображения, отвечает текстом.", 400_000, ("text", "image"), ("low", "medium", "high", "xhigh"), None, ()),
    "gpt-5.5": ("Сложная профессиональная работа и программирование с настраиваемой глубиной рассуждения.", 1_050_000, ("text", "image"), ("none", "low", "medium", "high", "xhigh"), "medium", ("gpt-5.5-2026-04-23",)),
    "gpt-5.5-pro": ("Сложные задачи с дополнительными вычислениями для более точных ответов; Responses API.", 1_050_000, ("text", "image"), ("medium", "high", "xhigh"), "high", ("gpt-5.5-pro-2026-04-23",)),
    "gpt-5.4": ("Программирование и сложная профессиональная работа с настраиваемой глубиной рассуждения.", 1_050_000, ("text", "image"), ("none", "low", "medium", "high", "xhigh"), "none", ("gpt-5.4-2026-03-05",)),
    "gpt-5.4-mini": ("Быстрые массовые задачи: программирование, управление компьютером и работа субагентов.", 400_000, ("text", "image"), ("none", "low", "medium", "high", "xhigh"), "none", ("gpt-5.4-mini-2026-03-17",)),
    "gpt-5.4-nano": ("Простые массовые задачи с низкой стоимостью: классификация, извлечение данных, ранжирование и субагенты.", 400_000, ("text", "image"), ("none", "low", "medium", "high", "xhigh"), "none", ("gpt-5.4-nano-2026-03-17",)),
    "gpt-5.4-pro": ("Сложные профессиональные задачи с дополнительными вычислениями для более точных ответов; Responses API.", 1_050_000, ("text", "image"), ("medium", "high", "xhigh"), "medium", ("gpt-5.4-pro-2026-03-05",)),
    "gpt-5.2": ("Профессиональная работа и сложные задачи с настраиваемой глубиной рассуждения.", 400_000, ("text", "image"), ("none", "low", "medium", "high", "xhigh"), "none", ("gpt-5.2-2025-12-11",)),
    "gpt-5.2-pro": ("Сложная профессиональная работа с дополнительными вычислениями для точных ответов; Responses API.", 400_000, ("text", "image"), ("medium", "high", "xhigh"), None, ("gpt-5.2-pro-2025-12-11",)),
    "gpt-5.2-codex": ("Длительные задачи агентного программирования в Codex и сходных средах.", 400_000, ("text", "image"), ("low", "medium", "high", "xhigh"), None, ()),
    "gpt-5.1": ("Программирование и агентные задачи с настраиваемым рассуждением и режимом без рассуждения.", 400_000, ("text", "image"), ("none", "low", "medium", "high"), "none", ("gpt-5.1-2025-11-13",)),
    "gpt-5.1-codex": ("Агентное программирование в Codex и сходных средах; Responses API.", 400_000, ("text", "image"), (), None, ()),
    "gpt-5.1-codex-max": ("Агентное программирование и длительные задачи; Responses API.", 400_000, ("text", "image"), (), None, ()),
    "gpt-5.1-codex-mini": ("Компактная, более экономичная версия GPT-5.1-Codex для программирования.", 400_000, ("text", "image"), (), None, ()),
    "gpt-5": ("Программирование, рассуждение и агентные задачи в разных предметных областях.", 400_000, ("text", "image"), ("minimal", "low", "medium", "high"), None, ("gpt-5-2025-08-07",)),
    "gpt-5-mini": ("Чётко заданные задачи с точными инструкциями при требованиях к скорости и стоимости.", 400_000, ("text", "image"), ("minimal", "low", "medium", "high"), None, ("gpt-5-mini-2025-08-07",)),
    "gpt-5-nano": ("Быстрые недорогие задачи, включая суммаризацию и классификацию.", 400_000, ("text", "image"), ("minimal", "low", "medium", "high"), None, ("gpt-5-nano-2025-08-07",)),
    "gpt-5-pro": ("Сложные задачи с дополнительными вычислениями для более точных ответов; Responses API.", 400_000, ("text", "image"), ("high",), "high", ("gpt-5-pro-2025-10-06",)),
    "gpt-5-codex": ("Агентное программирование в Codex и сходных средах; Responses API.", 400_000, ("text", "image"), (), None, ()),
    "gpt-4.1": ("Следование инструкциям и вызов инструментов; большой контекст и ответы без отдельного этапа рассуждения.", 1_047_576, ("text", "image"), (), None, ("gpt-4.1-2025-04-14",)),
    "gpt-4.1-mini": ("Быстрая компактная модель для следования инструкциям и вызова инструментов с большим контекстом.", 1_047_576, ("text", "image"), (), None, ("gpt-4.1-mini-2025-04-14",)),
    "gpt-4.1-nano": ("Быстрая экономичная модель для следования инструкциям и вызова инструментов с большим контекстом.", 1_047_576, ("text", "image"), (), None, ("gpt-4.1-nano-2025-04-14",)),
    "gpt-4o": ("Универсальные задачи с текстом и изображениями; текстовые и структурированные ответы.", 128_000, ("text", "image"), (), None, ("gpt-4o-2024-08-06", "gpt-4o-2024-11-20")),
    "gpt-4o-mini": ("Быстрые недорогие специализированные задачи, включая классификацию, извлечение ключевых слов и перевод.", 128_000, ("text", "image"), (), None, ("gpt-4o-mini-2024-07-18",)),
    "o3": ("Многошаговые задачи по математике, науке, программированию и анализу текста и изображений.", 200_000, ("text", "image"), ("low", "medium", "high"), None, ("o3-2025-04-16",)),
    "o3-mini": ("Компактная модель рассуждения с поддержкой структурированных ответов и вызова функций.", 200_000, ("text",), ("low", "medium", "high"), None, ("o3-mini-2025-01-31",)),
    "o3-pro": ("Сложное рассуждение с дополнительными вычислениями для более точных ответов; Responses API.", 200_000, ("text", "image"), (), None, ("o3-pro-2025-06-10",)),
    "o4-mini": ("Быстрое экономичное рассуждение, программирование и визуальные задачи.", 200_000, ("text", "image"), ("low", "medium", "high"), None, ("o4-mini-2025-04-16",)),
    "o1": ("Сложные задачи рассуждения с предварительным обдумыванием ответа.", 200_000, ("text", "image"), (), None, ("o1-2024-12-17",)),
    "o1-mini": ("Компактная более быстрая и экономичная модель для задач рассуждения.", 128_000, ("text",), (), None, ("o1-mini-2024-09-12",)),
    "o1-preview": ("Исследовательская предварительная версия модели o1 для сложного рассуждения.", 128_000, ("text",), (), None, ("o1-preview-2024-09-12",)),
    "gpt-4-turbo": ("Предыдущее поколение GPT-4 для работы с текстом и изображениями.", 128_000, ("text", "image"), (), None, ("gpt-4-turbo-2024-04-09",)),
    "gpt-3.5-turbo": ("Предыдущее поколение модели для диалогов, текста и кода через Chat Completions API.", 16_385, ("text",), (), None, ("gpt-3.5-turbo-0125", "gpt-3.5-turbo-1106")),
}


def _plain(value: str) -> str:
    """Очистить текст карточки для одной строки интерфейса, не выполняя разметку.

    Принимает строку из HTML, убирает управляющие символы, угловые скобки и
    лишние пробелы, ограничивает результат 600 символами. Состояние не меняет.
    """
    cleaned = "".join(" " if unicodedata.category(char).startswith("C") else char
                      for char in value)
    return " ".join(cleaned.replace("<", " ").replace(">", " ").split())[:600]


class _IndexParser(HTMLParser):
    """Извлечь только подпись назначения из точных ссылок модельного индекса."""

    def __init__(self) -> None:
        """Создать пустую ограниченную проекцию без сети и доступа к файлам."""
        super().__init__(convert_charrefs=True)
        self.cards: dict[str, dict] = {}
        self.identifier: str | None = None
        self.description_depth = 0
        self.depth = 0
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        """Принять HTML-тег и открыть только allowlisted модельную подпись.

        attrs — недоверенные атрибуты; URL никогда не используется для GET.
        Скрипты и стили пропускаются. Число сохраняемых карточек ограничено.
        """
        if tag in {"script", "style"}:
            self.skip += 1
        if self.skip:
            return
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            if tag in {"br", "hr"} and self.description_depth:
                self.parts.append(" ")
            return
        self.depth += 1
        attributes = dict(attrs)
        if tag == "a":
            self.identifier = None
            self.parts = []
            self.description_depth = 0
            href = attributes.get("href", "")
            prefix = "/api/docs/models/"
            candidate = href[len(prefix):] if isinstance(href, str) and href.startswith(prefix) else ""
            if _MODEL_ID.fullmatch(candidate) and candidate != "all":
                self.identifier = candidate
        classes = set((attributes.get("class") or "").split())
        if self.identifier and {"text-sm", "text-secondary"} <= classes:
            self.description_depth = self.depth

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        """Игнорировать одиночные HTML-теги без текста и состояния вложенности."""
        return None

    def handle_endtag(self, tag: str) -> None:
        """Закрыть подпись и сохранить лишь очищенный текст с фиксированным URL.

        Не читает связанные страницы. Ссылки вне фиксированного пути, пустые
        подписи и карточки сверх MAX_CARDS не попадают в публичную проекцию.
        """
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a":
            text = _plain("".join(self.parts))
            if self.identifier and text and len(self.cards) < MAX_CARDS:
                self.cards[self.identifier] = {"description": text, "details_source": "openai_docs",
                                               "details_url": DOCS_ROOT + self.identifier}
            self.identifier = None
            self.description_depth = 0
            self.parts = []
        if self.description_depth == self.depth:
            self.description_depth = 0
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        """Накопить до 1200 символов подписи; инструкции и атрибуты не извлекаются."""
        if self.description_depth and not self.skip and sum(map(len, self.parts)) < 1200:
            self.parts.append(data[:1200])


def _index_cards() -> dict[str, dict]:
    """Вернуть общий часовой индекс, обновив его максимум одним публичным GET.

    Нет параметров: адрес, GET, пять секунд общего deadline и 4 MiB заданы
    кодом. Authorization отсутствует; общий transport запрещает redirects.
    При ожидаемой сетевой/форматной ошибке остаётся прежний кэш, повторная
    попытка разрешена через час. Отмена job не подавляется. ФС не используется.
    """
    global _CACHE, _CACHE_UNTIL
    with _LOCK:
        if time.monotonic() < _CACHE_UNTIL:
            return _CACHE
        try:
            request = Request(INDEX_URL, method="GET", headers={"Accept": "text/html"})
            body = post_model_response(request, timeout=DOC_TIMEOUT, max_bytes=MAX_DOC_BYTES)
            if len(body) > MAX_DOC_BYTES:
                raise ValueError("documentation index exceeds limit")
            parser = _IndexParser()
            parser.feed(body.decode("utf-8"))
            parser.close()
            if parser.cards:
                _CACHE = parser.cards
        except (URLError, OSError, ValueError, subprocess.TimeoutExpired):
            pass
        _CACHE_UNTIL = time.monotonic() + CACHE_SECONDS
        return _CACHE


def model_card(identifier: object) -> dict:
    """Вернуть независимую безопасную справку для точного API model ID.

    Принимает ID, а не URL. Назначение берётся из фиксированного публичного
    индекса; проверенные поля и русские описания — из перечисленных выше
    точных карточек и опубликованных снимков. Имена семейств, произвольные
    даты и похожие варианты не нормализуются. Непроверенные поля отсутствуют,
    неизвестная модель возвращает {}. Один общий GET/час, без вызова модели,
    чтения credentials, файлов или передачи пользовательского текста в сеть.
    """
    if not isinstance(identifier, str) or _MODEL_ID.fullmatch(identifier) is None:
        return {}
    result = deepcopy(_index_cards().get(identifier, {}))
    for canonical, row in _REVIEWED.items():
        description, context, modalities, efforts, default, snapshots = row
        if identifier != canonical and identifier not in snapshots:
            continue
        result.update(description=description, context_window=context,
                      input_modalities=list(modalities), reasoning_efforts=list(efforts),
                      details_source="openai_docs", details_url=DOCS_ROOT + canonical)
        if default is not None:
            result["default_reasoning_effort"] = default
        break
    return result
