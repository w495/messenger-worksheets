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
cd /home/operator/messenger-worksheets-install
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
`operator` и использует его официальную авторизацию. Из
`/home/operator/.codex/config.toml` при provisioning копируются только `model` и
`model_reasoning_effort`; Codex стартует с `--ignore-user-config`. API key,
MCP, hooks и остальные пользовательские настройки не наследуются.

Режим менее изолирован: worker получает права `operator`, доступ к его home,
собственному TLS key и transport token процесса. Gateway по-прежнему работает
под отдельным `worksheet-gateway`.

Оба instance могут работать одновременно:

```bash
sudo -n /usr/local/sbin/worksheet-admin start-api-instance
sudo -n /usr/local/sbin/worksheet-admin start-local-instance
```

API instance использует порт `8443` и `gateway-api.env`; local-user instance —
порт `8444` и `gateway-local-user.env`. StateDirectory и RPC token раздельные.

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
cd /home/operator/messenger-worksheets-install
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
