# Изменить код и выполнить локальные проверки

Работайте из корня репозитория. Для локальной проверки нужны Python 3.14,
ShellCheck и shfmt. Shell-скрипты используют POSIX sh; дополнительная
оболочка для их запуска не требуется.

## Найдите нужный модуль

| Каталог или файл | За что отвечает |
|---|---|
| `messenger-worksheets/messenger_codex/bridge.py` | Polling, треды, статусы, отправка ответов и файлов |
| `messenger-worksheets/messenger_worksheets/cloud_gateway.py` | Передача запросов удалённому worker |
| `messenger-worksheets/messenger_worksheets/chat_commands.py` | Команды управления тредом |
| `messenger-worksheets/messenger_worksheets/thread_context.py` | Ограниченная история и копии файлов |
| `messenger-worksheets/messenger_worksheets/codex_rpc.py` | HTTPS RPC, отмена, кеш и идемпотентность |
| `messenger-worksheets/messenger_worksheets/codex_worker.py` | Вызовы Responses API и Codex CLI |
| `messenger-worksheets/messenger_worksheets/model_*.py` | Каталоги, карточки и представление моделей |
| `messenger-worksheets/messenger_user_client/` | Registry, Fanout и файлы Мессенджера |
| `deployment/` | Установка, диагностика, unit-файлы |
| `tests/` | Проверки с искусственными данными |

## Подготовьте зависимости

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes \
  -r messenger-worksheets/requirements.lock
```

Активируйте окружение, чтобы команда `python3` в проверочном скрипте
использовала установленные зависимости:

```sh
. .venv/bin/activate
sh deployment/verify.sh
```

`verify.sh` требует установленные ShellCheck и shfmt. Он проверяет
шесть shell-скриптов: пять файлов `deployment/*.sh` и helper
`deployment/worksheet-admin`. Проверяются первые три строки, предел
в 72 символа, ShellCheck со всеми проверками и формат shfmt. Синтаксис
проверяется в `sh` и остальных установленных оболочках: `bash`, `dash`,
`fizsh`, `ksh`, `mksh`, `posh`, `zsh`. Обязателен только `sh`;
отсутствующие дополнительные оболочки пропускаются.

Затем проверяются docstring Python-функций и компиляция исходников,
запускаются unit-тесты. Поведенческие тесты shell используют временные
каталоги и подмены системных команд. Они проверяют работу скриптов
без установки приложения в систему и обращения к боевым службам.
Для этих проверок не нужны производственные токены; работу реальных
API и доставку сообщений проверяют отдельно.

## Проверьте shell после правок

Shell-скрипты используют синтаксис POSIX и строки не длиннее
72 символов. Первые три строки каждого скрипта:

```sh
#!/usr/bin/env sh
# shellcheck enable=all
# shfmt -p -s -i 2 -kp -sr -ci -d
```

Для отдельной проверки ShellCheck и форматирования выполните:

```sh
shellcheck --enable=all --shell=sh \
  deployment/*.sh deployment/worksheet-admin
shfmt -p -s -i 2 -kp -sr -ci -d \
  deployment/*.sh deployment/worksheet-admin
```

`shfmt -d` показывает различия, не меняя файлы. Для применения формата
замените `-d` на `-w`, затем повторите проверку.

## Проверьте нужную часть Python

Для быстрого запуска одного набора укажите существующее имя файла:

```sh
PYTHONPATH=messenger-worksheets \
  python3 -m unittest discover -s tests -p 'test_chat_commands.py'
```

После изменения протокола сверяйте
[справочник пользовательского клиента](../reference/messenger-user-client.md).
После изменения маршрутов, настроек или команд обновляйте
соответствующий справочник и примеры.

Проверки установленного worker и доставки выполняются отдельно по
[руководству эксплуатации](operate.md).
