# Clean-room спецификация Messenger user client

## Изоляция реализации

Реализация создаётся независимо. Разработчику запрещено читать, импортировать,
сравнивать или копировать legacy-каталог ``messenger-client``, локально
установленные прежние клиенты и любые другие реализации ``MessengerUserClient``.

Разрешённые источники: эта спецификация, официальные HTTPS endpoint, ответы
выделенного тестового аккаунта и создаваемые в clean-room тесты.

## Цель

Создать Python 3.11+ пакет без CLI, который предоставляет минимальный
user-authorized adapter для `messenger-worksheets`. Код и комментарии —
самостоятельные. Комментарии и docstring на русском обязательны для публичных
API, wire-формата, проверки identity, файловой безопасности и retry semantics.
Очевидные строки комментировать не требуется.

## Публичный интерфейс `client.py`

Экспортировать:

- `MessengerError`, `AuthenticationError`, `PermissionDenied`,
  `ConfirmationError`, `OperationUncertain`;
- `MessengerUserClient`;
- `render_message(raw) -> dict`;
- `thread_chat_id(parent_chat_id, timestamp) -> str`.

Конструктор клиента:

```text
MessengerUserClient(
    token,
    account="work" | "personal",
    environment="prod" | "alpha",
    expected_login=None,
    expected_email=None,
    expected_uid=None,
    expected_organization_id=None,
    enforce_account_class=True,
    timeout=30,
)
```

Нужные методы:

- `identity(refresh=False)`;
- `resolve_invite(invite)`;
- `fanout(path, payload)`;
- `read_history(chat_id, limit=30, before_ts=None)`;
- `send_message(text, chat_id, thread_ts=None, payload_id=None,
  confirm_fingerprint=None)`;
- `edit_message(chat_id, timestamp, text, confirm_fingerprint=None)`;
- `set_reaction(chat_id, timestamp, reaction, operation="add",
  confirm_fingerprint=None)`;
- `send_document(chat_id, path, story_id, payload_id=None,
  confirm_fingerprint=None, receipt_dir=None)`;
- `send_image_with_caption(chat_id, path, text, story_id, payload_id=None,
  confirm_fingerprint=None, receipt_dir=None)`;
- `download_media(file_id, target, max_bytes,
  confirm_fingerprint=None)`.

Все мутации двухфазны: первый вызов возвращает `preview_not_applied` и
SHA-256 fingerprint; второй выполняется только с точным fingerprint.

## Registry HTTPS

Контуры:

- prod: `https://messenger.360.yandex.ru/api/`;
- alpha: `https://api.messenger.alpha.yandex.ru/api/`.

POST form-urlencoded поле `request`, содержащее compact JSON:

```json
{"method":"<method>","params":{}}
```

Заголовки: OAuth token, `X-Application-Id: Yamb-web`, JSON accept.
Разрешить только фиксированный allowlist методов. Для текущего проекта нужны
`request_user`, `get_chats_info` и read-only вспомогательные методы,
необходимые для identity/invite. Ответ имеет `status` и `data`.

Токен, `Authorization` и свободный error body никогда не логируются.

## Fanout WebSocket

Endpoint: `wss://push.yandex.ru/v2/subscribe/websocket`.
Query содержит service (`messenger-prod:version5` или `messenger:version5`),
client id реализации, параметр `user` со значением uid подтверждённой identity
и случайный UUID session.
OAuth передаётся только заголовком. TLS verification обязательна.
WebSocket transport не отправляет browser `Origin` (`suppress_origin=true`).
Ошибки handshake классифицируются только безопасной категорией (`http_NNN`,
`timeout`, `tls`, `connection`) без response body, headers и OAuth.

Управляющее текстовое JSON-сообщение использует поле `operation`. После
`{"operation":"ping"}` или `{"operation":"subscribed"}` отправляется
бинарный DATA frame:

1. байт `0x01`;
2. msgpack-массив из трёх элементов: ноль, request id, строковый path;
3. little-endian uint32 со значением 5;
4. little-endian uint64, младшие 32 бита которого равны MurmurHash2 payload;
5. compact UTF-8 JSON payload.

Ответ DATA начинается байтом 1 и содержит JSON-объект после служебного
бинарного заголовка. Парсер ищет кандидаты `{` и принимает первый хвост,
который полностью декодируется как JSON object.

Allowlist path для проекта: `history`, `message_info`, `push`.
Read-only вызовы можно ограниченно повторять; `push` автоматически не
повторяется после неизвестного исхода.

## Сообщения

Нормализованный результат `render_message` содержит безопасные поля:

- `ts`, `seq`, `version`;
- `author`, `author_guid`;
- `text`, `payload_id`, `deleted`;
- `media` для image/gallery/document;
- `unknown_content=True`, если wire содержит нераспознанные данные.

Никогда не возвращать произвольные неизвестные wire-поля: они могут содержать
URL или credentials.

Тред группового чата `n/k/id` имеет id `(n+100)/k/id_<parent_timestamp>`.

Отправка текста использует `push` с Plain: целевой ChatId, Text.MessageText и
UUID PayloadId. Редактирование обязано перечитать сообщение, убедиться, что
оно своё, и повторно использовать исходный PayloadId вместе с Timestamp.

Реакции проекта:

- `✅` -> type `100101`;
- `👀` -> type `100110`;
- `💔` -> type `100116` (ошибка задачи или доставки);
- actions add/remove/replace -> `0/1/2`.

## Файлы и изображения

- Только абсолютные локальные пути.
- Запрет symlink во всех компонентах пути.
- Только regular file, максимум 50 MiB.
- Открытие через `O_NOFOLLOW`, сверка stat до/после чтения.
- PNG: валидная signature/IHDR/CRC, разумные размеры, без APNG.
- Upload только на фиксированный HTTPS media host, OAuth в заголовке,
  redirects запрещены.
- Перед upload и push записывается приватная receipt (`0700` directory,
  `0600` file) для reconcile после unknown outcome.
- Документ отправляется как MiscFile; PNG с caption — как Gallery.
- Download создаёт новый файл через `O_EXCL|O_NOFOLLOW`, ограничивает размер,
  удаляет partial file после ошибки. OAuth снимается перед разрешённым media
  redirect; каждый redirect остаётся HTTPS и внутри `*.yandex.ru`/`*.yandex.net`.

## Identity

`request_user` обязан вернуть uid/guid. При заданных expectations проверяются
точные email/uid/organization. `work` требует организационный аккаунт,
`personal` — consumer account. Не строить email из nickname. При mismatch или
unverified остановить чтение и запись fail-closed.

## Security acceptance criteria

- Нет секретов, OAuth client ID, реальных chat ID или PII в исходниках.
- Нет `eval`, `exec`, subprocess/shell, pickle/yaml unsafe load.
- Нет отключения TLS и автоматических redirect с OAuth.
- URL/host/method/path/header/file inputs валидируются allowlist.
- Ошибки не содержат response body и токен.
- Preview fingerprint связывает actor, account, environment и точный request.
- Сравнение fingerprints — constant-time.
- Тесты покрывают token redaction, redirect, symlink/path traversal, size
  limits, identity mismatch, unknown mutation outcome и wire frame fixtures.

## Acceptance для интеграции

Новый пакет должен пройти собственные unit-тесты, затем тестовый запуск на
выделенном OAuth без изменения production state. После этого
`messenger-worksheets` переключается импортом на новый пакет с возможностью
немедленного rollback.

## Точный compatibility contract для messenger-worksheets

Эта секция задаёт только наблюдаемое поведение и wire shape, без исходного
кода прежнего клиента.

### Registry

Invite разрешается методом `get_chats_info` с параметром `invite_hash`.
Публичный результат:

```text
{"invite_hash": <str>, "found": <bool>, "chat": <dict|null>}
```

`identity()` возвращает как минимум `uid`, `guid`, `login`,
`is_org_account`; expectations проверяются fail-closed.

Фактический `request_user` содержит вложенный объект `user`. Организации могут
быть списком в `user.organizations`; email может находиться в allowlisted
`user.contacts` (`type=email`, `value=<address>`). Email нельзя строить из
nickname. Для personal-account пустой список organizations означает
`is_org_account=false`.

Для совместимости `login` нормализуется отдельно от email: значение вида
`example-user@yandex-team.ru` в login/nickname превращается в короткий
`example-user`, тогда
как поле email сохраняет полный адрес. Другие домены самовольно не обрезаются.

Invite parser обязан поддерживать bare hash и URL вида
`https://messenger.360.yandex.ru/#/join/<hash>`: hash в таком URL находится во
fragment, а не path. Допустимые host проверяются allowlist.

### Fanout payload

Fanout `push` получает envelope:

```json
{"ClientMessage":{"Plain":{...}},"UserIp":"::1"}
```

Для Reaction вместо Plain используется `ClientMessage.Reaction`.

History request: `{"ChatId": chat_id, "Limit": limit}` и опциональный
`MaxTimestamp`. Response содержит `Chats`; нужная комната обязана иметь ровно
запрошенный `ChatId`, а сообщения находятся в `Messages`.

`message_info` request содержит `ChatId` и `Timestamp`; response может вернуть
message в `Message`, `ServerMessage` либо `MessageInfo`-совместимом envelope.

### Нормализованные результаты

`read_history(...)` возвращает dict:

```text
{
  "chat_id": <str>, "messages": <list>, "count": <int>,
  "next_before_ts": <oldest timestamp|null>
}
```

Preview мутации содержит `fingerprint` и `status=preview_not_applied`.
После confirm:

- `send_message`: `outcome`, `chat_id`, `message_info`, `payload_id`;
- `edit_message`: `outcome`, `message_info`;
- `set_reaction`: `outcome`, `chat_id`, `timestamp`, `reaction`;
- document/image: `outcome` и безопасные delivery/verification поля.

`outcome` принимает `confirmed`, `duplicate`, `rejected`, `unknown`.
Fanout `FullyCommitted`, `Duplicate`, числовые 1/2 либо успешный MessageInfo
классифицируются как confirmed; Details — rejected; неоднозначный ответ —
unknown. Неизвестная мутация не повторяется автоматически.

### Text/Edit/Reaction wire

Text Plain:

```text
ChatId, Text.MessageText, PayloadId
```

Edit Plain дополнительно содержит Timestamp и повторно использует исходный
PayloadId. Reaction wire:

```text
ChatId, Timestamp, Action (0/1/2), Type (100101/100110)
```

`Timestamp` во всех `message_info`, Edit и Reaction wire-пакетах передаётся
JSON-числом. Строковый timestamp сервер отклоняет NACK frame `0x02`.
`Reaction.Type` также передаётся JSON-числом; строковый код реакции сервер
отклоняет тем же NACK вместо применения side effect.

### Media wire

Upload endpoint:

```text
https://files.messenger.yandex.ru/media_upload/
  <quoted chat-id parts>/<quoted payload UUID>/<quoted filename>
```

Тело upload — исходные bytes, Content-Type файла, OAuth в заголовке,
redirect запрещён. Ответ JSON возвращает `file_id`.

Download endpoint:

```text
https://files.messenger.yandex.ru/file_shortterm/<quoted file_id>
```

OAuth только на первом запросе; после проверенного redirect заголовок снимается.

Document Plain использует `MiscFile.FileInfo` с Id2/Name/Size/Source.
`Source` — JSON integer `0`; story_id остаётся локальным id квитанции.
Media file_id — namespace из slash-разделённых безопасных сегментов; пустые,
``.`` и ``..`` сегменты, URL, query и percent escapes запрещены.
Download сохраняет разделители namespace в URL. PNG-gallery использует upload
Content-Type image/png. Любой document использует application/octet-stream,
чтобы server image-оптимизация не меняла bytes скачиваемого оригинала.
Image with caption использует `Gallery.Text` и один item Image с Width/Height,
Animated=false и FileInfo.

### render_message actual envelope

Основной wire message имеет `ServerMessage`, внутри `ClientMessage.Plain` и
`ServerMessageInfo`. Timestamp/SeqNo/Version/From/Deleted берутся из info;
Text/Gallery/MiscFile/PayloadId — из Plain. Неизвестные поля не выдаются.
