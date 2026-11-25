"""Разбить текст Messenger без потерь и с лимитом 4096 единиц UTF-16."""

from __future__ import annotations

import re


MAX_MESSAGE_UNITS = 4096


def utf16_length(text: str) -> int:
    """Посчитать длину строки в единицах UTF-16 без кодирования или ввода-вывода.

    Каждый символ вне BMP занимает две единицы, остальные — одну. Принимает
    только str, иначе выбрасывает TypeError; содержимое не нормализуется.
    """
    if not isinstance(text, str):
        raise TypeError("message text must be a string")
    return sum(2 if ord(character) > 0xFFFF else 1 for character in text)


def split_messages(text: str, limit: int = MAX_MESSAGE_UNITS) -> tuple[str, ...]:
    """Разбить строку на сообщения до limit единиц UTF-16 без изменения текста.

    Предпочитает последний полный абзац, затем строку и пробельную границу.
    Длинное слово режется по целым Unicode code points; дополнительные fences,
    заголовки и многоточия не вставляются. Конкатенация результата равна text;
    пустой ввод даёт (). limit — целое 1..4096; неверный тип/предел или символ,
    не помещающийся даже один, вызывает TypeError/ValueError. Сети и ФС нет.
    """
    if not isinstance(text, str):
        raise TypeError("message text must be a string")
    if type(limit) is not int or not 1 <= limit <= MAX_MESSAGE_UNITS:
        raise ValueError("message limit must be an integer from 1 to 4096")
    parts = []
    start = 0
    while start < len(text):
        end, units = start, 0
        while end < len(text):
            width = 2 if ord(text[end]) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            end += 1
        if end == start:
            raise ValueError("a Unicode character exceeds the message limit")
        if end < len(text):
            fragment = text[start:end]
            paragraph = fragment.rfind("\n\n")
            line = fragment.rfind("\n")
            if paragraph >= 0 and fragment[:paragraph + 2].strip():
                end = start + paragraph + 2
            elif line >= 0 and fragment[:line + 1].strip():
                end = start + line + 1
            else:
                boundary = None
                for match in re.finditer(r"\s+", fragment):
                    boundary = match.end()
                if boundary and fragment[:boundary].strip():
                    end = start + boundary
        parts.append(text[start:end])
        start = end
    return tuple(parts)
