#!/usr/bin/env sh
# shellcheck enable=all
# shfmt -p -s -i 2 -kp -sr -ci -d

# Все проверки выполняются от корня проекта, независимо от cwd.
# CDPATH сброшен, чтобы cd не печатал путь в подстановку команды.
set -eu
script_directory=$(dirname "$0")
repo_root=$(CDPATH='' cd "${script_directory}/.." && pwd -P)
cd "${repo_root}"

# Проверка shell требует установленных ShellCheck и shfmt. Она также
# проверяет заголовки, ширину строк и синтаксис доступных оболочек.
python3 deployment/check_shell_scripts.py

# AST-проверка не исполняет исходники и не читает конфигурацию с OAuth.
# У каждой Python-функции должен быть русский docstring.
python3 deployment/check_docstrings.py
python3 -m compileall -q messenger-worksheets deployment tests

# Тесты используют временные файлы и подмены внешних клиентов.
# Боевые службы, чаты и root-конфигурация в проверке не участвуют.
PYTHONPATH=messenger-worksheets \
  python3 -m unittest discover -s tests -p 'test_*.py'

printf '%s\n' 'Проверки репозитория пройдены.'
