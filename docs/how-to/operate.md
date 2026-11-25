# Обновить, проверить или остановить сервис

Команды выполняются на машине с установленным сервисом из корня
репозитория. В примерах используется helper:

```sh
admin=/usr/local/sbin/worksheet-admin
```

Список служб и расположение состояния — в
[справочнике](../reference/services.md).

## Обновить код

Дождитесь завершения задач в обоих чатах. Затем остановите шлюзы,
обновите исходники и установите их:

```sh
sudo -n "$admin" stop-gateway-api
sudo -n "$admin" stop-gateway-sandbox
git pull --ff-only
sudo sh deployment/install_admin_helper.sh
sudo -n "$admin" restart-worker-api
sudo -n "$admin" restart-worker-sandbox
```

Перед возвратом чатов проверьте worker и соединение с Мессенджером:

```sh
sudo -n "$admin" check-messenger-api-cleanroom
sudo -n "$admin" check-messenger-local-cleanroom
sudo -n "$admin" smoke-api
sudo -n "$admin" smoke-sandbox-task
sudo -n "$admin" start-gateway-api
sudo -n "$admin" start-gateway-sandbox
sudo -n "$admin" status
```

Проверьте доставку файла в каждом чате по
[упражнению первого задания](../tutorials/first-task.md).

Для обновления используется `install_admin_helper.sh`. Первый
установщик `install.sh` заново запрашивает и записывает секреты.
SQLite-файлы состояния сохраняйте при обновлении и откате: их удаление
может вызвать повторную обработку сообщений.

## Проверить работу без отправки в чат

```sh
sudo -n "$admin" check-messenger-api-cleanroom
sudo -n "$admin" check-messenger-local-cleanroom
sudo -n "$admin" smoke-api
sudo -n "$admin" smoke-sandbox-task
sudo -n "$admin" smoke-sandbox-link
sudo -n "$admin" smoke-models
sudo -n "$admin" smoke-controls
```

Первые две команды проверяют аккаунт и доступ к чату чтением.
`smoke-models` читает каталоги моделей обоих маршрутов без генерации
ответа. Остальные проверки вызывают модель и расходуют её квоту.
`smoke-controls` проверяет чтение синтетического файла в `/plan`
и `/review`, а затем запускает и отменяет отдельную тестовую задачу.
Сообщения в Мессенджер эти проверки не отправляют.

Успешный RPC-тест подтверждает работу worker. Для проверки полной
доставки всё равно нужно отправить задание в чат и скачать результат.

## Найти причину ошибки

Начните со состояния и журналов нужного маршрута:

```sh
sudo -n "$admin" status
sudo -n "$admin" logs-gateway-api
sudo -n "$admin" logs-gateway-sandbox
sudo -n "$admin" logs-worker-api
sudo -n "$admin" logs-worker-sandbox
```

При HTTP 503 смотрите журнал соответствующего worker. При проблемах
с OpenAI API или Codex CLI выполните:

```sh
sudo -n "$admin" diagnose-openai
```

Команда выводит категории ошибок без ключей и сырых ответов API.

Если команда работает из SSH, но не из Codex worker:

```sh
sudo -n "$admin" diagnose-sandbox
```

Диагностика создаёт временные unit с настройками worker и сравнивает
безопасный запуск при изменении одного ограничения. Последняя проба
проверяет чтение синтетического файла с исходными ограничениями.
Работающий worker, AppArmor и sysctl команда не меняет. Причины
требований к `PATH` и `/proc` объяснены в
[описании границ доступа](../explanation/auth-and-permissions.md).

При ошибке TLS восстановите цепочки и проверьте их разделение:

```sh
sudo sh deployment/repair_tls.sh
sudo -n "$admin" check-tls-isolation
```

Для передачи диагностики используйте очищенный вывод проверок.
Файлы окружения, OAuth-файлы и приватные TLS-ключи содержат секреты.

## Остановить сервис

Сначала остановите приём сообщений, затем worker:

```sh
sudo -n "$admin" stop-gateway-api
sudo -n "$admin" stop-gateway-sandbox
sudo -n "$admin" stop-worker-api
sudo -n "$admin" stop-worker-sandbox
```

Состояние и секреты сохранятся. После следующего запуска шлюзы
продолжат использовать свои SQLite-файлы.

## Отозвать доступ оператора

```sh
sudo -n "$admin" revoke
```

Операция удалит sudoers-правило и административный helper.
Для восстановления выполните от root или через обычный sudo:

```sh
sudo sh deployment/install_admin_helper.sh
```
