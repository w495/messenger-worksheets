# Messenger Worksheets

Два независимых автоответчика для чатов Яндекс Мессенджера. Оба gateway читают
сообщения одного личного аккаунта, но используют разные worker, RPC tokens,
порты и каталоги состояния.

## Архитектура

```text
API chat ── gateway-api ── HTTPS :8443 ── worker-api ── direct OpenAI API key

Local chat ─ gateway-local ─ HTTPS :8444 ─ worker-local (operator) ─ local Codex auth/config
```

- Gateway хранят раздельное состояние чатов и не получают ключ OpenAI.
- API worker не получает OAuth Мессенджера и работает под системным пользователем.
- Local worker не получает OAuth или API key, но работает с правами `operator`,
  использует его auth и безопасную проекцию model/effort из `~/.codex`; это
  явно менее изолированный режим.
- Локальный RPC защищён собственной TLS-цепочкой и случайным транспортным
  ключом для каждого instance.
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
