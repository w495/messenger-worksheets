# Messenger Worksheets

Два независимых профиля обработки для чатов Яндекс Мессенджера. Сообщения
публикуются user-authorized аккаунтом `Lang Env`; worker, RPC tokens, порты и
каталоги состояния профилей изолированы. В репозитории есть независимый
clean-room user client; legacy backend поддержан только как опциональный откат
для отдельно полученного правомерного дистрибутива.

## Архитектура

```text
Lang Env ── gateway-api ───── API chat ── text ── HTTPS :8443 ── Responses API
          │                            └─ task ── HTTPS :8445 ── sandbox-admin
          └─ gateway-sandbox ─ Local chat ────── HTTPS :8445 ── sandbox-admin

Messenger backend: cleanroom (default) | legacy (optional rollback)
```

- Каждый gateway хранит собственный cursor/SQLite и работает ровно с одним
  chat id; OAuth Мессенджера и OpenAI API key остаются в разных root-only env.
- API worker не получает OAuth Мессенджера и работает под системным пользователем.
- Sandbox-admin worker выполняет task-запросы обоих чатов под отдельным системным
  пользователем, с полным Codex toolset и внешней сетью внутри systemd-границы.
- Резервный local-user worker не получает OAuth или API key, но работает с правами `w495`,
  использует его auth и безопасную проекцию model/effort из `~/.codex`; это
  явно менее изолированный режим.
- Локальный RPC защищён собственной TLS-цепочкой и случайным транспортным
  ключом для каждого instance.
- Unit-файлы используют разные `StateDirectory` и системных пользователей.
- Ответы, статусы и файлы отправляются в тред исходного сообщения; PNG также
  публикуются нативным изображением. Исходное сообщение получает 👀 во время
  обработки и ✅ после доставки результата.

## Структура

- `messenger-worksheets/` — шлюз, RPC-обработчик и логика Codex;
- `messenger_user_client/` — независимый clean-room user-account adapter;
- `messenger-client/` — место для опционального legacy adapter; его исходники
  не требуются и не входят в публичный дистрибутив;
- `messenger_worksheets/bot_api.py` — неактивный экспериментальный Bot API;
- `deployment/` — установщик и systemd unit-файлы;
- `docs/OPERATIONS.md` — установка, проверка и эксплуатация.

## Требования

- Ubuntu 26.04 или совместимая система с systemd;
- Python 3.14 и пакет `python3.14-venv`;
- `openssl`, `rsync` и `codex` в `/usr/local/bin/codex`;
- личный OAuth аккаунта, от имени которого отвечает gateway;
- OpenAI API key с отдельным API-биллингом.

Production-зависимости закреплены в `requirements.lock` с SHA-256 для Python
3.14 / x86_64 manylinux VM; root-installer использует `--require-hashes`.

Для получения нового пользовательского OAuth задайте ID собственного OAuth
приложения через `MESSENGER_OAUTH_CLIENT_ID`. В готовом deployment, где токен
уже сохранён в root-only environment-файле, client ID не требуется.

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

## Лицензия

Самостоятельный код проекта и clean-room клиент: **AGPL-3.0-only**.
Лицензия требует предоставления исходников изменённой версии также при работе
с ней через сеть. Полный текст — [LICENSE](LICENSE), область применения и
исключения — [LICENSE_SCOPE.md](LICENSE_SCOPE.md).
