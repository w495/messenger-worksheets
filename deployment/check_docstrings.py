#!/usr/bin/env python3
"""Проверить наличие русских docstring у каждой функции проекта."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys


ROOTS = ("messenger-client", "messenger-worksheets", "deployment", "tests")
RUSSIAN = re.compile(r"[А-Яа-яЁё]")


def python_files(repository: Path) -> tuple[Path, ...]:
    """Вернуть детерминированный список Python-файлов из явных корней проекта.

    Параметры:
        repository: Абсолютный или относительный корень Git-репозитория.

    Результат:
        Кортеж путей ``*.py`` без кэшей и скрытых служебных каталогов.

    Побочные эффекты:
        Функция только читает метаданные каталогов и не открывает секретные
        env-файлы: область обхода ограничена четырьмя публичными корнями.

    Инварианты безопасности:
        Обход не выходит за пределы ``ROOTS`` и никогда не начинается от HOME.
    """

    files: list[Path] = []
    for root_name in ROOTS:
        root = repository / root_name
        if not root.is_dir():
            continue
        files.extend(
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        )
    return tuple(sorted(files))


def validate_file(path: Path, repository: Path) -> list[str]:
    """Найти функции без содержательного русского docstring в одном файле.

    Параметры:
        path: Проверяемый Python-файл внутри репозитория.
        repository: Корень, относительно которого строится безопасное имя.

    Результат:
        Список диагностик с относительным путём, строкой и именем функции.

    Исключения:
        ``SyntaxError`` пробрасывается вызывающему коду, чтобы повреждённый
        исходник не маскировался как обычное отсутствие документации.

    Побочные эффекты:
        Читает только текст указанного исходника в UTF-8.

    Инварианты безопасности:
        В диагностику не включается тело функции или содержимое docstring;
        поэтому случайный литерал секрета не копируется в CI-лог.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []
    relative = path.relative_to(repository)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        docstring = ast.get_docstring(node, clean=True)
        if docstring and RUSSIAN.search(docstring):
            continue
        problems.append(f"{relative}:{node.lineno}: {node.name}: требуется русский docstring")
    return problems


def main() -> int:
    """Проверить весь разрешённый Python-код и вернуть CI-совместимый статус.

    Результат:
        Ноль, если каждая функция документирована; единицу при нарушениях.

    Исключения:
        Ошибки чтения и синтаксиса намеренно не подавляются: verify должен
        завершиться до тестов, если исходники невозможно надёжно разобрать.

    Побочные эффекты:
        Печатает только координаты нарушений либо краткое подтверждение успеха.

    Инварианты безопасности:
        Не исполняет проверяемый код, не импортирует модули и не читает env.
    """

    repository = Path(__file__).resolve().parents[1]
    problems = [
        problem
        for path in python_files(repository)
        for problem in validate_file(path, repository)
    ]
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("Docstring check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
