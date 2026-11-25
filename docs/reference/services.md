# Службы, каталоги и административные команды

## Основные службы

| Служба | Назначение | Пользователь | Порт |
|---|---|---|---|
| `worksheet-worker.service` | Responses API для текста API-чата | `worksheet-worker` | 8443 |
| `worksheet-worker-sandbox-admin.service` | Codex CLI для задач обоих чатов и текста Local-чата | `worksheet-chat-admin` | 8445 |
| `worksheet-gateway-api.service` | Приём и доставка API-чата | `worksheet-gateway` | — |
| `worksheet-gateway-sandbox-admin.service` | Приём и доставка Local-чата | `worksheet-gateway` | — |

Worker слушают loopback. RPC использует HTTPS, отдельный доверенный
CA и транспортный токен каждого профиля.

`worksheet-worker-local-user.service` на порту 8444 — резервный профиль
Codex под оператором `w495`, с отдельными CA, ключом и токеном.
`worksheet-gateway-local-user.service` относится к этому профилю
и конфликтует с основным Local-шлюзом. Он не обслуживает Local-чат
в схеме из [руководства установки](../how-to/install.md).

`worksheet-gateway-bot.service` — экспериментальный Team Bot API.
`worksheet-gateway.service` — агрегированный шлюз, который установщик
отключает при настройке раздельных экземпляров. Один чат должен
обслуживаться одним polling writer.

## Конфигурации и состояние

| Путь | Содержимое |
|---|---|
| `/opt/messenger-worksheets/app` | Установленный код; владелец root |
| `/opt/messenger-worksheets/venv` | Python-зависимости |
| `/opt/messenger-worksheets/admin` | Python-скрипты административных проверок |
| `/etc/messenger-worksheets/worker.env` | Настройки API-worker |
| `/etc/messenger-worksheets/worker-sandbox-admin.env` | Настройки общего Codex worker |
| `/etc/messenger-worksheets/gateway-api.env` | Настройки API-чата |
| `/etc/messenger-worksheets/gateway-sandbox-admin.env` | Настройки Local-чата |
| `/etc/messenger-worksheets/gateway.env` | Начальная конфигурация первого установщика |
| `/etc/messenger-worksheets/worker/` | TLS API-worker |
| `/etc/messenger-worksheets/sandbox-admin/` | TLS общего Codex worker |
| `/etc/messenger-worksheets/local-user/` | TLS резервного профиля |
| `/var/lib/messenger-worksheets-worker` | Состояние API-worker |
| `/var/lib/messenger-worksheets-chat-admin` | Codex-авторизация и задачи общего worker |
| `/var/lib/messenger-worksheets-gateway-api` | SQLite и копии файлов API-шлюза |
| `/var/lib/messenger-worksheets-gateway-sandbox-admin` | SQLite и копии файлов Local-шлюза |

Файлы окружения имеют права `0600`. Приватные TLS-ключи принадлежат
root и группе соответствующего worker, имеют права `0640`. Каталоги
состояния служб создаются с правами `0700`.

## Административный helper

Вызов: `sudo -n /usr/local/sbin/worksheet-admin операция`.
Helper принимает ровно одну операцию из фиксированного набора.
Произвольные команды, пути и дополнительные аргументы не принимаются.

Таблица охватывает операции основного развёртывания. Здесь
`{api,sandbox}` обозначает два отдельных имени операции, а не аргумент.

| Операция | Действие |
|---|---|
| `status` | Состояние всех установленных профилей |
| `start-worker-{api,sandbox}` | Запуск worker |
| `restart-worker-{api,sandbox}` | Перезапуск worker |
| `stop-worker-{api,sandbox}` | Остановка worker |
| `start-gateway-{api,sandbox}` | Запуск шлюза |
| `stop-gateway-{api,sandbox}` | Остановка шлюза |
| `logs-worker-{api,sandbox}` | Журнал worker |
| `logs-gateway-{api,sandbox}` | Журнал шлюза |
| `login-sandbox-admin` | Device authorization Codex под `worksheet-chat-admin` |
| `check-messenger-api-cleanroom` | Проверка аккаунта и API-чата чтением |
| `check-messenger-local-cleanroom` | Проверка аккаунта и основного Local-чата чтением |
| `check-tls-isolation` | Проверка трёх TLS-профилей и отсутствия общего доверия |
| `smoke-api` | Тестовый текстовый запрос к API-worker |
| `smoke-sandbox-task` | Создание тестового файла общим worker |
| `smoke-sandbox-link` | Скачивание example.com и сравнение результата |
| `smoke-models` | Чтение каталогов моделей обоих маршрутов |
| `smoke-controls` | Проверка plan/review и отмены тестовой задачи через RPC |
| `diagnose-openai` | Диагностика API и Codex с очищенным выводом |
| `diagnose-sandbox` | Сравнение запуска инструментов во временных unit |
| `revoke` | Удаление helper и его sudoers-правила |

Например, `start-worker-sandbox` — полное имя операции; фигурные
скобки из таблицы в команду не вводятся. Все операции определены в
[deployment/worksheet-admin](../../deployment/worksheet-admin).

Порядок запуска и проверки — в
[установке](../how-to/install.md), обновление и остановка — в
[эксплуатации](../how-to/operate.md).
