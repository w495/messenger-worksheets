"""Читаемые карточки каталога и ограниченные страницы сообщений Messenger."""
from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from .text_messages import MAX_MESSAGE_UNITS, split_messages, utf16_length


CATALOG_PAGE_BREAK = "\n\n---\n\n"
MODELS_PER_PAGE = 3
PAGE_BODY_UNITS = MAX_MESSAGE_UNITS - 320


def _group_key(item: dict) -> tuple:
    """Сгруппировать только версии одной официальной карточки с равными характеристиками.

    Принимает проверенную модель. При отсутствии официального источника ключ
    уникален по ID. Даже общий URL не объединяет записи с разным контекстом,
    входами или reasoning. Метки default и даты сортировки в равенство не входят;
    исходный порядок сохраняет более новые группы первыми. Ввода-вывода нет.
    """
    url = item.get("details_url")
    if item.get("details_source") != "openai_docs" or not url:
        return ("id", item["id"])
    return ("card", url, item.get("description", ""), item.get("context_window"),
            item.get("max_context_window"), tuple(item.get("input_modalities", ())),
            tuple(item.get("reasoning_efforts", ())), item.get("default_reasoning_effort"),
            tuple(sorted(item.get("reasoning_descriptions", {}).items())))


def _model_card(items: list[dict], selected: str | None, plain: Callable[[str], str]) -> str:
    """Оформить одну модель и подтверждённые одинаковые версии компактным блоком.

    items — записи одной группы в порядке каталога; selected — точный выбранный
    ID; plain очищает подпись и при наличии переводит штатные описания. Возвращает
    название, назначение, входы, контекст и уровни на отдельных строках. Версии
    перечисляются без повторения общего описания. Нет данных — нет догадок.
    Ссылки на источники сведены к общему footer, чтобы URL не загромождали список.
    """
    first = items[0]
    url = first.get("details_url")
    canonical = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] if url else None
    primary = next((item for item in items if item["id"] == canonical), first)
    chosen = selected == primary["id"]
    lines = ["`" + primary["id"] + "`" + (" — выбрана" if chosen else "")]
    description = primary.get("description")
    lines.append("Для чего: " + plain(description) if description else "Для чего: описание пока не опубликовано.")
    modes = {"text": "текст", "image": "изображения", "audio": "аудио", "video": "видео"}
    modalities = primary.get("input_modalities", ())
    if modalities:
        lines.append("Вход: " + ", ".join(modes[mode] for mode in modalities))
    context, maximum = primary.get("context_window"), primary.get("max_context_window")
    if context:
        value = f"{context:,}".replace(",", " ")
        line = "Контекст: " + value + " токенов"
        if maximum and maximum != context:
            line += " · максимум " + f"{maximum:,}".replace(",", " ")
        lines.append(line)
    elif maximum:
        lines.append("Контекст: до " + f"{maximum:,}".replace(",", " ") + " токенов")
    efforts = primary.get("reasoning_efforts", ())
    if efforts:
        lines.append("Размышления: " + " · ".join(efforts))
    if not any((modalities, context, maximum, efforts)):
        lines.append("Характеристики: источник пока не сообщил подробностей.")
    variants = [item for item in items if item is not primary]
    if variants:
        lines.append("Версии с теми же характеристиками:")
        lines.extend("  `" + item["id"] + "`" + (" — выбрана" if selected == item["id"] else "") for item in variants)
    return lines[0] + "\n\n" + "  \n".join(lines[1:])


def _header(task: bool, selected: str | None, page: int, total: int, source: str) -> str:
    """Создать короткую повторяемую шапку страницы с режимом и выбранным ID.

    Аргументы формируются gateway; номера страниц положительны, ID предварительно
    проверен. Шапка не вычисляет доступность модели и не меняет настройки.
    Возвращает обычный текст с inline code, без таблицы и без сетевых операций.
    """
    engine = "OpenAI API" if source == "openai_models_api" else "Codex CLI"
    label = ("Задачи /task, /plan, /review" if task else "Обычные ответы") + " · " + engine
    lines = [f"Модели · {label} · {page}/{total}", "Новые модели — сначала."]
    if selected:
        lines.append("Выбрана: `" + selected + "`")
    return "  \n".join(lines)


def catalogue_pages(catalog: dict, *, task: bool, current: str | None,
                    plain: Callable[[str], str]) -> tuple[str, ...]:
    """Построить все страницы каталога не длиннее 4096 единиц UTF-16.

    Принимает проверенный каталог, режим, override модели и formatter описаний.
    Порядок провайдера уже отсортирован backend по новизне; группы сохраняют его.
    На странице не более трёх карточек. Официальные одинаковые снимки объединены,
    очень длинная группа делится без обрезания ID. Каждая страница имеет шапку;
    справка и источники даются один раз в конце. Никаких БД, сети и мутаций.
    """
    models = catalog["models"]
    selected = current or next((item["id"] for item in models if item["is_default"]), None)
    grouped: dict[tuple, list[dict]] = {}
    for item in models:
        grouped.setdefault(_group_key(item), []).append(item)
    blocks = []
    for items in grouped.values():
        card = _model_card(items, selected, plain)
        fragments = split_messages(card, PAGE_BODY_UNITS - 160)
        for index, fragment in enumerate(fragments):
            prefix = "" if index == 0 else "`" + items[0]["id"] + "` — продолжение версий\n"
            blocks.append((prefix + fragment.strip(), 1))
    if not blocks:
        blocks.append(("Провайдер не вернул текстовых моделей.", 0))
    footer = []
    if current and not any(item["id"] == current for item in models):
        footer.append("Выбранная модель пока отсутствует в этом каталоге.")
    if catalog["truncated"]:
        footer.append("Каталог показан частично из-за лимита размера источника.")
    route = "task" if task else "chat"
    footer.extend((f"Выбор: /model {route} имя [уровень]", f"Уровни выбранной модели: /efforts {route}"))
    if catalog["source"] == "openai_models_api":
        footer.append("Источники: Models API и официальные карточки OpenAI.")
        footer.append("https://developers.openai.com/api/docs/models/")
    else:
        footer.append("Источник: каталог Codex CLI.")
    footer.append("Данные из каталога; доступ проверяется при запуске. Предел истории и инструменты задаются настройками чата.")
    if len(grouped) < len(models):
        footer.insert(0, "Версии с одинаковыми характеристиками объединены в карточки.")
    blocks.append(("  \n".join(footer), 0))
    bodies, body, count = [], "", 0
    for block, card_count in blocks:
        candidate = body + ("\n\n" if body else "") + block
        if body and (count + card_count > MODELS_PER_PAGE or utf16_length(candidate) > PAGE_BODY_UNITS):
            bodies.append(body)
            body, count = "", 0
            candidate = block
        body = candidate
        count += card_count
    if body:
        bodies.append(body)
    result = tuple(_header(task, selected, index, len(bodies), catalog["source"]) + "\n\n" + value
                   for index, value in enumerate(bodies, 1))
    if any(utf16_length(page) > MAX_MESSAGE_UNITS for page in result):
        raise ValueError("model catalogue page exceeds message limit")
    return result
