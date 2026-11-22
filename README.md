# Messenger Worksheets

Автоответчик для отдельного чата Яндекс Мессенджера. Шлюз читает сообщения от
личного аккаунта и передаёт задания изолированному обработчику Codex. Компоненты
работают на одной Linux-ВМ под разными системными пользователями.

## Архитектура

```text
Яндекс Мессенджер
        │ OAuth: только worksheet-gateway
        ▼
worksheet-gateway.service
        │ HTTPS на 127.0.0.1 + transport token
        ▼
worksheet-worker.service
        │ OpenAI API key: только worksheet-worker
        ▼
codex exec
```

- `worksheet-gateway` хранит состояние чата и не получает ключ OpenAI.
- `worksheet-worker` запускает `codex exec` и не получает OAuth Мессенджера.
- Локальный RPC защищён собственной TLS-цепочкой и случайным транспортным
  ключом.
- Unit-файлы используют разные `StateDirectory` и системных пользователей.

## Структура

- `messenger-worksheets/` — шлюз, RPC-обработчик и логика Codex;
- `messenger-client/` — авторизованный клиент Яндекс Мессенджера;
- `deployment/` — установщик и systemd unit-файлы;
- `docs/OPERATIONS.md` — установка, проверка и эксплуатация.

## Требования

- Ubuntu 26.04 или совместимая система с systemd;
- Python 3.14 и пакет `python3.14-venv`;
- `openssl`, `rsync` и `codex` в `/usr/local/bin/codex`;
- личный OAuth Яндекс Мессенджера со scope `yamb:all`;
- OpenAI API key с отдельным API-биллингом.

## Быстрый старт

На целевой ВМ:

```bash
cd ~/messenger-worksheets-install
sudo bash deployment/install.sh
```

Оба секрета вводятся скрыто. Установщик создаёт пользователей, виртуальное
окружение, сертификаты, файлы окружения и systemd units, но не запускает и не
включает службы.

До запуска шлюза оператор обязан проверить привязку OAuth к ожидаемому email и
работоспособность обработчика. Подробности — в [инструкции эксплуатации](docs/OPERATIONS.md).

## Локальная проверка

```bash
./deployment/verify.sh
```

Секреты, сертификаты, runtime-state, SSH `known_hosts` и исходный handoff-архив
исключены из Git.
