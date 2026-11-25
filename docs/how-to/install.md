# Установить и запустить сервис

Руководство рассчитано на администратора отдельной Linux-машины.
В результате будут работать два чата: API и Local.

## Подготовьте машину и аккаунты

Нужны systemd, Python 3.14 с `venv` и `ensurepip`, OpenSSL, rsync, curl,
Git и Codex CLI в `/usr/local/bin/codex`. Lock-файл зависимостей рассчитан
на Linux x86_64 с поддержкой manylinux_2_28.

Проверьте доступность программ:

```sh
python3 --version
python3 -m ensurepip --version
/usr/local/bin/codex --version
systemctl --version
openssl version
```

`python3` должен запускать Python 3.14. Если в Debian или Ubuntu
недоступен `ensurepip`, установите пакет `python3.14-venv`.

Установщики и unit-файлы используют оператора `w495`, его группу
и модель из `/home/w495/.codex/config.toml`. Проверьте там `model`
и `model_reasoning_effort`. Для другого оператора сначала адаптируйте
эти пути и учётную запись в `deployment/`; автоматического параметра
для такой замены нет.

Подготовьте OAuth пользовательского аккаунта Мессенджера и OpenAI API
key. Аккаунт должен состоять в обоих чатах и иметь права отправлять
сообщения, вложения и реакции. Codex авторизуется отдельно под
пользователем worker на следующем этапе.

## Установите приложение

Получите исходники и выполните установщик из корня репозитория:

```sh
git clone https://github.com/w495/messenger-worksheets.git
cd messenger-worksheets
sudo sh deployment/install.sh
```

Установщик скрыто запросит OAuth и OpenAI API key, создаст системных
пользователей, venv, TLS и файлы окружения. Он проверит unit-файлы,
но не запустит службы.

Создайте локальную конфигурацию:

```sh
cp deployment/instances.example.json deployment/instances.local.json
chmod 600 deployment/instances.local.json
```

В редакторе заполните:

- `expected_email` — email отвечающего аккаунта;
- `messenger_backend` — `cleanroom`;
- `instances.api.invite` — ссылку на API-чат;
- `instances.local_user.invite` — ссылку на Local-чат;
- `token_file` — абсолютный путь к OAuth-файлу с правами `0600`.
  Удалите это поле, если нужен OAuth из первого установщика.

Секреты в JSON вставлять не нужно. Точный формат — в
[справочнике конфигурации](../reference/configuration.md).

```sh
sudo sh deployment/install_admin_helper.sh
```

Скрипт сверит аккаунт и ссылки на чаты, создаст конфигурации экземпляров
и установит `/usr/local/sbin/worksheet-admin`. Оператор `w495` получит
право выполнять фиксированные операции helper без пароля sudo.
Это право сохраняется до [явного отзыва](operate.md#отозвать-доступ-оператора).

## Авторизуйте Codex и проверьте worker

В этой и следующих командах `admin` — путь к установленному helper:

```sh
admin=/usr/local/sbin/worksheet-admin
sudo -n "$admin" login-sandbox-admin
```

Завершите показанный процесс device authorization. Авторизация будет
принадлежать системному пользователю `worksheet-chat-admin`.

Запустите worker и выполните проверки:

```sh
sudo -n "$admin" start-worker-api
sudo -n "$admin" start-worker-sandbox
sudo -n "$admin" check-messenger-api-cleanroom
sudo -n "$admin" check-messenger-local-cleanroom
sudo -n "$admin" smoke-api
sudo -n "$admin" smoke-sandbox-task
sudo -n "$admin" smoke-sandbox-link
```

Проверки Мессенджера читают аккаунт и чат. Три `smoke`-команды обращаются
к модели: проверяют текстовый API-ответ, создание `hello.txt`
и скачивание example.com. Последняя сравнивает файл с независимо
полученным HTTP-ответом. Они ещё не подтверждают доставку в чат.

## Запустите шлюзы и проверьте доставку

```sh
sudo -n "$admin" start-gateway-api
sudo -n "$admin" start-gateway-sandbox
sudo -n "$admin" status
```

Выполните [первое задание](../tutorials/first-task.md) в каждом чате.
Проверьте документ, статус `✅ Готово.` и реакцию ✅. Для проверки
изображений попросите создать PNG: он должен прийти картинкой
и отдельным документом.

После успешной проверки включите автозапуск:

```sh
sudo systemctl enable \
  worksheet-worker.service \
  worksheet-worker-sandbox-admin.service \
  worksheet-gateway-api.service \
  worksheet-gateway-sandbox-admin.service
```

Дальнейшие обновления и диагностика описаны в
[руководстве эксплуатации](operate.md).
