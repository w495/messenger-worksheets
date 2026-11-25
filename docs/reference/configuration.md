# Конфигурация и лимиты

## Файл установки

`deployment/instances.local.json` — локальная конфигурация для
`install_admin_helper.sh`. Образец находится в
[instances.example.json](../../deployment/instances.example.json).

```json
{
  "expected_email": "user@example.test",
  "messenger_backend": "cleanroom",
  "token_file": "/absolute/private/path/to/messenger-token",
  "instances": {
    "api": {
      "invite": "https://messenger.360.yandex.ru/#/join/API-INVITE"
    },
    "local_user": {
      "invite": "https://messenger.360.yandex.ru/#/join/LOCAL-INVITE"
    }
  }
}
```

| Поле | Требование |
|---|---|
| `expected_email` | Точный email отвечающего аккаунта; проверяется сервером |
| `messenger_backend` | Значение `cleanroom`; используется встроенный `messenger_user_client` |
| `token_file` | Необязательный абсолютный путь к OAuth-файлу |
| `instances.api.invite` | Ссылка-приглашение в API-чат |
| `instances.local_user.invite` | Ссылка-приглашение в Local-чат |

Набор `instances` содержит ровно `api` и `local_user`. Ссылки должны
разрешаться в разные чаты. Аккаунт уже должен состоять в этих чатах.

OAuth-файл должен быть обычным файлом без symlink, размером до 4096
байт, принадлежать root или оператору `w495` и не иметь прав для группы
и остальных пользователей. Рекомендуемые права — `0600`.
Значение OAuth не должно содержать пробелов. Если `token_file` отсутствует,
используется OAuth, записанный первым установщиком в `gateway.env`.

Необязательный объект `bot` относится к экспериментальному Team Bot API:
`expected_name`, `token_file`, `auth_scheme` (`OAuth` или `OAuthTeam`),
`base_url`, `organization_id`. Он не нужен для двух пользовательских
чатов из руководства установки. В `configure_bot.py` ожидаемое имя
задаётся параметром `--expected-name`; новый токен по умолчанию хранится
в `~/.config/messenger-worksheets/bot-token`.

## Окружение шлюза

Файлы находятся в `/etc/messenger-worksheets/`, принадлежат root
и имеют права `0600`. Разделение по службам описано в
[справочнике служб](services.md).

| Переменная | Назначение |
|---|---|
| `WORKSHEET_MESSENGER_BACKEND` | Единственное поддерживаемое значение — `cleanroom` |
| `WORKSHEET_MESSENGER_TOKEN` | OAuth отвечающего аккаунта |
| `WORKSHEET_MESSENGER_EMAIL` | Ожидаемый email этого аккаунта |
| `WORKSHEET_MESSENGER_CHAT_ID` | ID одного обслуживаемого чата |
| `WORKSHEET_REPLY_IN_THREADS` | Ответы в тредах; установщик задаёт `true` |
| `WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT` | Задачи всех участников; установщик задаёт `true` |
| `WORKSHEET_CODEX_WORKER_URL` | URL worker обычных ответов |
| `WORKSHEET_CODEX_WORKER_CA` | CA для проверки этого worker |
| `WORKSHEET_CODEX_TRANSPORT_TOKEN` | RPC-токен этого worker |
| `WORKSHEET_TASK_WORKER_URL` | Отдельный URL задач API-чата |
| `WORKSHEET_TASK_WORKER_CA` | CA worker задач API-чата |
| `WORKSHEET_TASK_WORKER_TRANSPORT_TOKEN` | RPC-токен задач API-чата |
| `WORKSHEET_CODEX_CHAT_RPC_TIMEOUT` | Ожидание обычного ответа; 1860 секунд |
| `WORKSHEET_CODEX_TASK_RPC_TIMEOUT` | Ожидание задачи; 1860 секунд |

URL API-worker: `https://127.0.0.1:8443/v1/codex-worksheet-jobs`.
URL общего Codex worker: `https://127.0.0.1:8445/v1/codex-worksheet-jobs`.
В API-шлюзе он задан отдельным маршрутом задач. В Local-шлюзе это
основной маршрут и для текста, и для задач.

## Окружение worker

| Переменная | Назначение |
|---|---|
| `WORKSHEET_CODEX_PROVIDER` | `responses_direct` для API-текста; `sandbox_admin` для общего Codex worker |
| `WORKSHEET_CODEX_MODEL` | Модель по умолчанию |
| `WORKSHEET_CODEX_REASONING_EFFORT` | Уровень размышлений по умолчанию |
| `WORKSHEET_CODEX_API_KEY` | OpenAI API key; нужен API-worker |
| `WORKSHEET_CODEX_BINARY` | Путь к Codex; установщик находит нативный бинарный файл |
| `WORKSHEET_CODEX_LISTEN` | Адрес прослушивания; `127.0.0.1` |
| `WORKSHEET_CODEX_PORT` | `8443` для API; `8445` для общего Codex worker |
| `WORKSHEET_CODEX_TLS_CERT` | Сертификат соответствующего профиля |
| `WORKSHEET_CODEX_TLS_KEY` | Приватный TLS-ключ профиля |
| `WORKSHEET_CODEX_TRANSPORT_TOKEN` | RPC-токен профиля |
| `WORKSHEET_CODEX_CHAT_TIMEOUT` | Бюджет обычного запроса; 1800 секунд |
| `WORKSHEET_CODEX_TASK_TIMEOUT` | Бюджет задачи; 1800 секунд |
| `WORKSHEET_CODEX_MAX_CONCURRENT_JOBS` | Число запросов worker; установщик задаёт 4 |
| `WORKSHEET_ENABLE_IMAGE_GENERATION` | Разрешение возможностей генерации изображений Codex |

Таймауты worker принимают целое число от 1 до 3600 секунд.
Число параллельных запросов — от 1 до 16, по умолчанию 4.
Превышение числа свободных мест возвращает HTTP 429. Шлюз каждого
чата обрабатывает обычные сообщения последовательно.

API-worker получает начальные модель и уровень из provisioning-кода;
Codex worker — из `model` и `model_reasoning_effort` конфигурации
оператора. Фактические настройки показывает `/help` или `/model`.
Переопределения [командами чата](commands.md) действуют внутри треда.

## Ограничения запроса и доставки

| Объект | Предел |
|---|---|
| Время обычного запроса или задачи | 1800 секунд в установленном профиле |
| Ожидание RPC | 1860 секунд в установленном профиле |
| Поле `text` запроса worker | 8000 символов, включая обёртку шлюза |
| Входящие изображения вместе | 20 МиБ; PNG, JPEG, GIF, WebP |
| Результирующие файлы вместе | 50 МиБ |
| Исходящее текстовое сообщение | 4096 единиц UTF-16 |
| Карточки моделей в сообщении | До 3 |
| Попытки доставки одного вложения | До 5 |
| Кеш результатов RPC | До 256 записей и 1 ГиБ |

Входящие документы пока не передаются worker. Текст задания нужно
вставить в сообщение или передать HTTP(S)-ссылкой. Изображения
передаются модели. Поддержка типа входа моделью не снимает эти ограничения.

Бюджет 1800 секунд общий для повторных подключений Codex. HTTP-вызов
модели ограничен общим временем дочернего процесса. Ожидание RPC
оставляет дополнительное время для возврата результата или ошибки.

## История и файлы

Пределы относятся к одному шлюзу, если явно не указан тред.

| Объект | Предел |
|---|---|
| Текстовая история треда | Исходный запрос и последние 20 сообщений |
| Общий объём текста треда | 64 КиБ |
| Одна запись истории | 16 КиБ |
| Копии файлов треда | До 32 файлов и 50 МиБ |
| Число хранимых тредов | До 128 |
| Копии файлов всех тредов | До 512 МиБ |
| Первичное чтение истории Мессенджера | Корень и последние 40 сообщений |

При достижении лимитов старая часть контекста удаляется. Комментарии
участников и служебные статусы в модельную историю не включаются.
`/reset` удаляет сохранённый контекст конкретного треда. Устройство
памяти описано в [объяснении архитектуры](../explanation/architecture.md).
