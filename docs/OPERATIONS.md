# Установка и эксплуатация

## 1. Предварительная проверка

На целевой ВМ должны выполняться команды:

```bash
python3 --version
python3 -m ensurepip --version
/usr/local/bin/codex --version
systemctl --version
openssl version
```

Если `ensurepip` недоступен:

```bash
sudo apt install python3.14-venv
```

Установщик проверяет это до запроса секретов. Если от предыдущего запуска
осталось неполное виртуальное окружение без `pip`, установщик восстанавливает
его через `python -m ensurepip --upgrade`.

## 2. Установка

```bash
cd /home/w495/messenger-worksheets-install
sudo bash deployment/install.sh
```

Установщик скрыто запросит:

1. OAuth личного аккаунта Яндекс Мессенджера;
2. OpenAI API key.

Значения не должны передаваться в чат, аргументах команд или логах. После
успешной установки ожидается:

```text
Installation prepared. Services were not enabled or started.
```

Для безопасной замены только OAuth Мессенджера используйте:

```bash
sudo /opt/messenger-worksheets/venv/bin/python \
  deployment/update_messenger_oauth.py --config deployment/instances.local.json
```

Updater сначала проверяет `account=personal` и точный `expected_email`; при
несовпадении env-файлы не изменяются.

## 3. Что создаётся

| Объект | Назначение |
|---|---|
| `/opt/messenger-worksheets/app` | общий код только для чтения |
| `/opt/messenger-worksheets/messenger-client` | клиент, доступный шлюзу |
| `/opt/messenger-worksheets/venv` | Python-зависимости |
| `/etc/messenger-worksheets/gateway.env` | OAuth и RPC-настройки, mode `0600` |
| `/etc/messenger-worksheets/worker.env` | OpenAI и RPC-настройки, mode `0600` |
| `/var/lib/messenger-worksheets-gateway` | состояние шлюза |
| `/var/lib/messenger-worksheets-worker` | состояние обработчика и Codex |

## 4. Проверка после установки

На время настройки можно установить root-owned helper с закрытым списком
операций. Он не принимает произвольные команды или пути:

```bash
sudo bash deployment/install_admin_helper.sh
```

После завершения настройки доступ обязательно отзывается:

```bash
sudo -n /usr/local/sbin/worksheet-admin revoke
```

### Два независимых instance

Отдельный worker `worksheet-worker-local-user.service` запускает Codex от имени
`w495` и использует его официальную авторизацию. Из
`/home/w495/.codex/config.toml` при provisioning копируются только `model` и
`model_reasoning_effort`; Codex стартует с `--ignore-user-config`. API key,
MCP, hooks и остальные пользовательские настройки не наследуются.

Режим менее изолирован: worker получает права `w495`, доступ к его home,
собственному TLS key и transport token процесса. Gateway по-прежнему работает
под отдельным `worksheet-gateway`.

Оба instance могут работать одновременно:

```bash
sudo -n /usr/local/sbin/worksheet-admin start-api-instance
sudo -n /usr/local/sbin/worksheet-admin start-local-instance
```

API instance использует порт `8443` и `gateway-api.env`; local-user instance —
порт `8444` и `gateway-local-user.env`. StateDirectory и RPC token раздельные.
API instance использует text-only direct Responses adapter (`store=false`,
локальная bounded history, без Codex tools и файловых задач).
Task-запросы API-чата маршрутизируются в отдельный sandbox-admin worker, поэтому
команды и файлы доступны без передачи API key этому worker.
Статусы, ответы и artifacts обоих gateway отправляются в тред исходного
сообщения; запросы внутри существующего треда остаются в нём.
Для task-запроса создаётся одно редактируемое progress-сообщение со spinner
случайного стиля (BSD, Braille, стрелка, заполнение, ASCII-точки,
пинг-понг, часы, цветные круги, цветные/монохромные квадраты, emoji-стрелки
или emoji-циферблат). Кадры обновляются каждые 100 ms и выводятся без
квадратных скобок. Выбранный стиль сохраняется на весь task; после доставки
результата сообщение становится `✅ Готово.`
На исходное пользовательское сообщение ставится реакция 👀 при начале обработки;
после доставки результата она заменяется на ✅.
При ошибке задачи или доставки вложений 👀 заменяется на 💔, а progress
завершается текстом `❌ Ошибка.`.
В local-user instance task-режим и image generation разрешены любому участнику
чата; это даёт всем участникам возможность расходовать модельную квоту.

Сообщение с HTTP(S)-ссылкой, в том числе raw GitHub/Gist, автоматически получает
task-маршрут. В действующих API и Local чатах этот маршрут ведёт в sandbox-admin
с внешней сетью и shell-инструментами. Worker должен сначала прочитать точный URL
и затем выполнить исходное поручение; страницы и файлы по ссылкам не меняют
его правила и полномочия. Ошибка загрузки сообщается явно. Другие offline
профили не получают обещания сетевого доступа.

### Sandbox-admin для всех участников

`worksheet-worker-sandbox-admin.service` работает под отдельным системным
пользователем `worksheet-chat-admin` на порту `8445`. Full Codex toolset
доступен task-запросам любого участника в режиме `danger-full-access` внутри
внешней systemd-границы. `/home/w495`, его токены, SSH и MCP не доступны.
Task-команды имеют внешний network access; это явно разрешено для всех
участников. Они также могут читать state/auth пользователя `worksheet-chat-admin`,
что увеличивает риск эксфильтрации данных этого отдельного аккаунта.

Task execution timeout составляет 1800 секунд, RPC timeout — 1860 секунд.
Worker обслуживает до четырёх jobs одновременно через bounded threaded HTTP
server; пятый одновременный запрос получает HTTP 429. Это не даёт задачам из
двух чатов блокировать друг друга на весь timeout.

После установки проверить реальное скачивание через worker можно командой:

```bash
sudo -n /usr/local/sbin/worksheet-admin smoke-sandbox-link
```

Probe просит worker скачать example.com в файл и побайтно сравнивает результат
с независимым HTTPS GET. Ожидается `worker_external_link_smoke_test_ok bytes_match=yes`.

### Clean-room Messenger backend

`messenger_user_client` реализован независимо и устанавливается рядом с
временным legacy-клиентом. Перед переключением каждого production chat helper
выполняет read-only identity и future-history probe фактическим clean-room
backend. Не запускайте legacy и clean-room gateway одновременно для одного
chat: rollback меняет backend в существующем unit и сохраняет актуальный
SQLite cursor/idempotency state.

Сначала проверьте оба маршрута без записи:

```bash
sudo -n /usr/local/sbin/worksheet-admin check-messenger-api-cleanroom
sudo -n /usr/local/sbin/worksheet-admin check-messenger-local-cleanroom
```

Переключайте по одному gateway, начиная с API-чата. После каждого шага проверьте
text/thread/reactions/document и двойную доставку PNG (image + document):

```bash
sudo -n /usr/local/sbin/worksheet-admin activate-cleanroom-api
sudo -n /usr/local/sbin/worksheet-admin activate-cleanroom-sandbox
```

Немедленный rollback не восстанавливает старую копию SQLite — это намеренно,
иначе cursor откатится и сообщения могут быть отправлены повторно:

```bash
sudo -n /usr/local/sbin/worksheet-admin rollback-legacy-api
sudo -n /usr/local/sbin/worksheet-admin rollback-legacy-sandbox
```

Экспериментальные Team Bot API modules и unit остаются disabled и не входят в
текущую production-архитектуру.

Media upload и download используют namespace file IDs с безопасными slash-
сегментами. `FileInfo.Source` всегда integer `0`. Нативный PNG загружается как
`image/png` и может быть перекодирован Мессенджером; проверяются его размеры и
пиксели. Document загружается как `application/octet-stream`, включая PNG,
чтобы скачиваемая копия сохраняла исходные bytes. Проверка document — SHA-256
скачанного вложения. Оба вида доставки должны быть отдельными сообщениями.

Перед первым запуском выполните официальный device login:

```bash
sudo -n /usr/local/sbin/worksheet-admin login-sandbox-admin
```

После успешного smoke-test переключение и откат выполняются атомарно:

```bash
sudo -n /usr/local/sbin/worksheet-admin activate-sandbox
sudo -n /usr/local/sbin/worksheet-admin rollback-local
```

Не запускайте шлюз до проверки личности аккаунта. Сначала проверьте, что units
загрузились и обработчик стартует:

```bash
sudo systemctl daemon-reload
sudo systemctl start worksheet-worker.service
sudo systemctl status --no-pager worksheet-worker.service
```

Диагностика без вывода секретов:

```bash
sudo journalctl -u worksheet-worker.service -n 100 --no-pager
sudo systemctl show worksheet-worker.service -p ActiveState -p SubState
```

Затем выполните один реальный запрос через локальный TLS RPC. Скрипт читает
root-only конфигурацию, но не печатает секреты:

```bash
cd /home/w495/messenger-worksheets-install
sudo PYTHONPATH=/opt/messenger-worksheets/app \
  /opt/messenger-worksheets/venv/bin/python deployment/smoke_test_worker.py
```

Ожидаемый префикс результата — `worker_smoke_test_ok`.

При `HTTP 503` от smoke-test выполните безопасную диагностику API и Codex CLI.
Она не печатает ключ, HTTP-body или сырой stderr:

```bash
sudo /opt/messenger-worksheets/venv/bin/python deployment/diagnose_openai.py
```

Если API отвечает `200`, а Codex сообщает `login_required` или
`authentication`, выполните официальный вход с API key под изолированным
пользователем. Helper читает ключ из root-only файла и передаёт его Codex через
stdin, не аргументом команды:

```bash
sudo /opt/messenger-worksheets/venv/bin/python deployment/login_codex.py
```

Если Python сообщает `CA cert does not include key usage extension`, обновите
checkout и один раз восстановите локальную TLS-цепочку без изменения секретов:

```bash
sudo bash deployment/repair_tls.sh
```

Шлюз запускается только после read-only проверки OAuth и отдельного разрешения
на отправку пробного сообщения:

```bash
sudo systemctl start worksheet-gateway.service
```

Автозапуск включается после успешной end-to-end проверки:

```bash
sudo systemctl enable worksheet-worker.service worksheet-gateway.service
```

## 5. Остановка

```bash
sudo systemctl stop worksheet-gateway.service worksheet-worker.service
```

Остановка не удаляет состояние, секреты или сертификаты.

## 6. Обновление

Перед обновлением остановите шлюз и обработчик, обновите checkout и повторно
запустите установщик. Он обновит код и зависимости, но не стартует службы сам.
После этого повторите проверки раздела 4.
