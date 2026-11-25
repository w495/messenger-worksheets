# Пользовательский клиент Мессенджера: API и протокол

Модуль `messenger_user_client` работает с Registry по HTTPS и Fanout по
WebSocket. В нём нет CLI, запуска команд и хранения OAuth-токена на диске.
Gateway передаёт токен при создании клиента.

Клиент входит в исходный код проекта. Этот справочник описывает
его интерфейс и поля протокола, важные для совместимости.

## Подключение

```python
from messenger_user_client import MessengerUserClient

client = MessengerUserClient(
    token,
    account="personal",
    expected_email="user@example.test",
    timeout=12,
)
identity = client.identity()
```

Конструктор также принимает `environment` (`prod` или `alpha`),
`expected_login`, `expected_uid`, `expected_organization_id` и
`enforce_account_class`.

`identity()` получает UID и GUID с сервера, затем сверяет заданные ожидания.
Email проверяется по ответу и контактам пользователя; он не строится из логина.
Если проверка не пройдена, последующие операции не выполняются.

По умолчанию `account="work"` требует организационный аккаунт, а
`account="personal"` — аккаунт без организации. Gateway использует
`enforce_account_class=False`, поскольку членство в организации само по себе
не определяет нужную учётную запись. Проверка `expected_email` при этом сохраняется.

Ошибки: `MessengerError`, `AuthenticationError`, `PermissionDenied`,
`ConfirmationError`, `OperationUncertain`. В сообщениях ошибок нет OAuth
и тела удалённого ответа.

## Чтение

| Метод | Результат |
|---|---|
| `identity(refresh=False)` | UID, GUID, логин, email и сведения об организации |
| `resolve_invite(invite)` | `invite_hash`, `found`, `chat` |
| `read_history(chat_id, limit=30, before_ts=None)` | `chat_id`, `messages`, `count`, `next_before_ts` |
| `fanout(path, payload)` | Ответ разрешённой операции `history` или `message_info` |

`resolve_invite` принимает hash либо HTTPS-ссылку. В ссылке вида
`https://messenger.360.yandex.ru/#/join/<hash>` hash находится после `#`.

История принимается только для точного запрошенного `ChatId`.
`render_message(raw)` возвращает `ts`, `seq`, `version`, автора,
`author_guid`, `text`, `payload_id`, `deleted` и метаданные вложений.
Неизвестное содержимое помечается `unknown_content`; произвольные поля
ответа не копируются.

Тред чата `n/k/id` имеет ID `(n+100)/k/id_<parent_timestamp>`.
Его строит `thread_chat_id(parent_chat_id, timestamp)`.

## Запись и подтверждение

Методы записи вызываются дважды:

1. Вызов без `confirm_fingerprint` возвращает
   `status="preview_not_applied"` и SHA-256 `fingerprint`. Запись не выполняется.
2. Повторный вызов с теми же параметрами и этим fingerprint выполняет операцию.

Fingerprint связывает пользователя, окружение, вид операции и точные параметры.
Проверка выполняется через `hmac.compare_digest`. OAuth в fingerprint не входит.

```python
preview = client.send_message("Ответ", chat_id=chat_id)
result = client.send_message(
    "Ответ",
    chat_id=chat_id,
    payload_id=preview["payload_id"],
    confirm_fingerprint=preview["fingerprint"],
)
```

Интерфейс методов:

```text
send_message(text, chat_id, thread_ts=None, payload_id=None, confirm_fingerprint=None)
edit_message(chat_id, timestamp, text, confirm_fingerprint=None)
set_reaction(chat_id, timestamp, reaction, operation="add", confirm_fingerprint=None)
send_document(chat_id, path, story_id, payload_id=None,
              confirm_fingerprint=None, receipt_dir=None)
send_image_with_caption(chat_id, path, text, story_id, payload_id=None,
                        confirm_fingerprint=None, receipt_dir=None)
download_media(file_id, target, max_bytes, confirm_fingerprint=None)
```

Edit перечитывает сообщение перед каждым вызовом, проверяет автора и использует
исходный `PayloadId`. Новый UUID для редактирования создавать нельзя.

Исход операции возвращается в `outcome`: `confirmed`, `rejected` или
`unknown`. Потребители также принимают `duplicate` для совместимости.
Числовые статусы успеха учитываются только в полях результата, а не в
произвольных вложенных данных. Непустой `Details` означает отказ.

`unknown` означает, что ответ не позволяет установить результат. Перед
повтором нужно проверить историю по `payload_id`, автору и адресату.
Транспорт сам не повторяет `push` после неизвестного исхода.

## Registry

| Окружение | URL |
|---|---|
| prod | `https://messenger.360.yandex.ru/api/` |
| alpha | `https://api.messenger.alpha.yandex.ru/api/` |

Запрос — POST form-urlencoded с полем `request`:

```json
{"method":"request_user","params":{}}
```

Разрешены методы `request_user` и `get_chats_info`.
Заголовки: `Authorization: OAuth <token>`, `X-Application-Id: Yamb-web`,
`Accept: application/json`. Автоматические перенаправления запрещены.

Ответ содержит `status` и `data`. Пользователь находится в `data.user`;
организации могут быть в `user.organizations`, email — в `user.contacts`.
Корпоративный суффикс `@yandex-team.ru` удаляется только из login/nickname;
поле email сохраняется целиком.

## Fanout

Адрес: `wss://push.yandex.ru/v2/subscribe/websocket`.

Параметры подключения: `service` (`messenger-prod:version5` для prod,
`messenger:version5` для alpha), имя клиента, `user` с UID и случайный
`session`. OAuth передаётся заголовком. TLS проверяется; `Origin` не отправляется.

После текстового события `operation=ping` или `operation=subscribed`
клиент отправляет бинарный DATA-кадр:

1. Маркер `0x01`.
2. Массив msgpack `[0, request_id, path]`.
3. Версия протокола: uint32 little-endian со значением 5.
4. Контрольная сумма: uint64 little-endian с MurmurHash2 JSON в младших 32 битах.
5. Компактный JSON в UTF-8.

JSON-ответ извлекается из DATA-кадра `0x01`. Короткий кадр `0x02`
сам по себе не считается подтверждением записи. При отсутствии DATA-ответа
ожидание завершается по timeout.

Разрешены `history`, `message_info` и внутренний `push`.
Публичный `fanout("push", ...)` запрещён: запись доступна через методы
с подтверждением fingerprint. Чтение повторяется один раз при транспортном сбое.

### Поля запросов

История: `ChatId`, `Limit`, необязательные `MinTimestamp` и
`MaxTimestamp`. Ответ содержит `Chats[].Messages`.

Одно сообщение: `ChatId`, `Timestamp`. В ответе сообщение может быть
в `Message`, `ServerMessage` или `MessageInfo`.

Текст отправляется в следующем конверте:

```json
{"ClientMessage":{"Plain":{"ChatId":"…","Text":{"MessageText":"…"},"PayloadId":"…"}},"UserIp":"::1"}
```

При редактировании в `Plain` добавляется `Timestamp`.
Для реакции вместо `Plain` передаётся `Reaction` с полями
`ChatId`, `Timestamp`, `Type` и `Action`.

`Timestamp`, `Reaction.Type` и `Action` должны быть JSON-числами.
Строковые значения этих полей сервер может отклонить кадром `0x02`.

| Реакция | Type |
|---|---|
| ✅ | 100101 |
| 👀 | 100110 |
| 💔 | 100116 |

`Action`: добавить — 0, удалить — 1, заменить — 2.

## Файлы и изображения

Upload выполняется POST-запросом на фиксированный адрес:

```text
https://files.messenger.yandex.ru/media_upload/<chat segments>/<payload UUID>/<filename>
```

Сегменты пути кодируются отдельно. Тело запроса — байты файла, OAuth — в
заголовке. Перенаправления запрещены. Ответ возвращает `file_id`.

Документы загружаются с `Content-Type: application/octet-stream`,
PNG-картинки — с `image/png`. Мессенджер может перекодировать картинку;
копия документа должна сохранять исходные байты.

Документ передаётся как `Plain.MiscFile.FileInfo`:
`Id2`, `Name`, `Size`, `Source=0`. Картинка — как
`Plain.Gallery` с подписью `Text` и одним `Items[].Image`:
`Width`, `Height`, `Animated=false`, `FileInfo`.
`story_id` используется для локального учёта, а не как значение `Source`.

Download:

```text
https://files.messenger.yandex.ru/file_shortterm/<file_id>
```

В media ID разрешены namespace-сегменты из букв ASCII, цифр, `.`, `_`
и `-`, разделённые `/`. Пустые сегменты, `.`, `..`, URL и query
отклоняются. Разделители namespace в download URL сохраняются.

Локальный файл должен иметь абсолютный путь и быть обычным файлом размером
не более 50 МиБ. Symlink запрещён во всех компонентах пути. Чтение использует
`O_NOFOLLOW` и сверяет stat до и после операции. PNG проверяется по сигнатуре,
IHDR, CRC и размерам; APNG не принимается.

Перед upload и push создаётся квитанция с правами `0600` в каталоге
`0700`. Она содержит параметры и стадию операции, но не OAuth и не байты файла.
Квитанция сама по себе не доказывает доставку; результат сверяется с историей.

Download создаёт новый файл через `O_EXCL|O_NOFOLLOW`, ограничивает объём
и удаляет незавершённый файл при ошибке. После разрешённого HTTPS-перенаправления
OAuth снимается; следующий адрес должен оставаться в зоне
`*.yandex.ru` или `*.yandex.net`.

## Проверка

`tests/cleanroom/test_client.py` проверяет протокол, подтверждения, ошибки
и файловые ограничения без сети. Тесты используют искусственные ID и токены.
`tests/test_cleanroom_integration.py` проверяет совместимость со шлюзом.

Проверка реального сервиса описана в
[руководстве эксплуатации](../how-to/operate.md). Успешный unit-тест не подтверждает
отправку сообщения или файла в Мессенджер.
