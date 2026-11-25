#!/usr/bin/env python3
"""Проверить формат, POSIX-синтаксис и ограничения shell-скриптов."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile


HEADER = (
    "#!/usr/bin/env sh",
    "# shellcheck enable=all",
    "# shfmt -p -s -i 2 -kp -sr -ci -d",
)
SHELLS = ("sh", "bash", "dash", "fizsh", "ksh", "mksh", "posh", "zsh")


def shell_scripts(repository: Path) -> tuple[Path, ...]:
    """Вернуть скрипты развёртывания из фиксированного каталога проекта.

    Учитывает файлы ``*.sh`` и административный helper без расширения.
    Каталоги пользователя и локальная конфигурация не обходятся.
    """
    deployment = repository / "deployment"
    return tuple(sorted((*deployment.glob("*.sh"), deployment / "worksheet-admin")))


def check_source(path: Path) -> list[str]:
    """Проверить обязательные первые строки и предел в 72 символа.

    Возвращает только координаты нарушений. Табуляция считается по
    восьмиколоночным позициям; форматирование отдельно проверяет shfmt.
    Исходник не исполняется, его содержимое не копируется в диагностику.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    problems = []
    if tuple(lines[:3]) != HEADER:
        problems.append(f"{path.name}: неверные первые три строки")
    for number, line in enumerate(lines, 1):
        if len(line.expandtabs(8)) > 72:
            problems.append(f"{path.name}:{number}: строка длиннее 72 символов")
    return problems


def check_tools(paths: tuple[Path, ...]) -> None:
    """Выполнить обязательные ShellCheck и shfmt с заданными флагами.

    Отсутствующий инструмент или ошибка проверки прерывает процесс.
    Системные скрипты не запускаются: инструменты только читают исходники.
    """
    for tool in ("shellcheck", "shfmt"):
        if shutil.which(tool) is None:
            raise SystemExit(f"Для проверки требуется {tool}.")
    arguments = [str(path) for path in paths]
    subprocess.run(["shellcheck", "--enable=all", "--shell=sh", *arguments], check=True)
    subprocess.run(
        ["shfmt", "-p", "-s", "-i", "2", "-kp", "-sr", "-ci", "-d", *arguments],
        check=True,
    )


def check_syntax(paths: tuple[Path, ...]) -> tuple[str, ...]:
    """Проверить синтаксис в установленных оболочках без исполнения команд.

    Режим ``-n`` проверяет каждый файл отдельно. Для дочерних процессов
    выделяется временный каталог настроек: запуск Fizsh не меняет файлы
    текущего пользователя. Окружение дочернего процесса не наследует
    секреты и shell startup hooks. Родительское окружение не изменяется.

    Возвращает имена реально проверенных оболочек; ``sh`` обязателен.
    """
    verified = []
    with tempfile.TemporaryDirectory(prefix="worksheet-shell-syntax-") as directory:
        environment = {
            "PATH": os.defpath,
            "HOME": directory,
            "ZDOTDIR": directory,
            "LC_ALL": "C.UTF-8",
        }
        for shell in SHELLS:
            executable = shutil.which(shell, path=os.defpath)
            if executable is None:
                if shell == "sh":
                    raise SystemExit("Для проверки требуется sh.")
                continue
            for path in paths:
                subprocess.run(
                    [executable, "-n", str(path)], env=environment,
                    check=True, capture_output=True, text=True, timeout=30,
                )
            verified.append(shell)
    return tuple(verified)


def main() -> int:
    """Проверить все shell-скрипты и сообщить фактический набор оболочек.

    Возвращает ноль после успешных проверок. Нарушение заголовка или ширины
    завершает процесс до инструментальных проверок; ошибки инструментов
    и синтаксиса также считаются ошибкой всей проверки.
    """
    repository = Path(__file__).resolve().parents[1]
    paths = shell_scripts(repository)
    problems = [problem for path in paths for problem in check_source(path)]
    if problems:
        raise SystemExit("\n".join(problems))
    check_tools(paths)
    verified = check_syntax(paths)
    print(f"Shell: {len(paths)} скриптов; синтаксис: {', '.join(verified)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
