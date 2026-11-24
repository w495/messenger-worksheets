# Changelog

## 2026-11-24

- внешние HTTP(S)-ссылки направляются в sandbox task с сетью и инструментами;
  добавлены отдельные сетевые инструкции, routing tests и live link smoke;
- clean-room backend используется по умолчанию; legacy adapter и аватарка
  необязательны и не входят в публичный дистрибутив;
- самостоятельный код опубликован под AGPL-3.0-only;
- при ошибке задачи/доставки реакция 👀 на исходном сообщении заменяется на 💔;
- media file_id принимает реальные namespace-сегменты; FileInfo.Source
  отправляется integer 0 для документов и изображений;
- download media сохраняет namespace-разделители в URL и проверяет сегменты
  до запроса, вместо ошибочного кодирования slash как %2F;
- task timeout увеличен до 1800 секунд, RPC timeout — до 1860 секунд;
- worker переведён на bounded concurrency 4 и покрыт тестом двух параллельных jobs;
- artifact delivery ограничена пятью попытками с безопасным stage-кодом ошибки;
- worker-профили получили разные CA/leaf/private key и profile-pinned RPC trust;
- повтор одного job UUID сериализован; result cache и gateway artifacts получили
  bounded retention/cleanup;
- sandbox-admin ограничен CPU/memory/tasks и скрывает внешние data mounts;
- Python 3.14 dependency tree закреплён exact versions и SHA-256 hashes;
- добавлен независимый clean-room Messenger user client и backend selector с
  read-only smoke и атомарным rollback при сохранении production SQLite state;
- clean-room Edit/Reaction используют integer `Timestamp`, Reaction — integer
  `Type`; строковые поля, вызывавшие NACK и зависший progress, устранены;
- mutation classifier больше не принимает произвольные nested dict/числа за
  подтверждённый MessageInfo или успешный commit;
- добавлен обязательный AST-gate русских docstring для каждой Python-функции;
- Team Bot API оставлен неактивным экспериментом; production использует `Lang Env`.

## 2026-11-23

- подготовлен раздельный gateway/worker deployment для systemd;
- добавлены локальные TLS и transport-token для RPC;
- добавлена изоляция OAuth Мессенджера и OpenAI API key;
- установщик научился восстанавливать `pip` в частично созданном venv;
- ввод секретов перенесён после проверки и установки зависимостей;
- добавлен безопасный end-to-end smoke-test локального RPC и OpenAI;
- CA-профиль дополнен `keyCertSign`; добавлено восстановление TLS без ротации секретов;
- TLS repair ожидает готовность HTTPS endpoint, а не только ранний статус systemd;
- добавлена безопасная классификация OpenAI API и `codex exec` без вывода секретов;
- диагностика сбрасывает привилегии через `setpriv` и жёстко завершает process group по таймауту;
- диагностика обходит Node-launcher, использует нативный Codex binary и не ждёт унаследованные pipe;
- вывод диагностического Codex направлен в `/dev/null`, жёсткий лимит снижен до 45 секунд;
- диагностический stderr собирается через временный fd и выводится только как безопасная категория;
- диагностика различает Responses API quota/rate-limit по безопасным `error.type` и `error.code`;
- основной API-key профиль закреплён на проверенной `gpt-5.3-codex`, reasoning `high`;
- API-key mode использует явный Responses provider `https://api.openai.com/v1` вместо встроенного Codex route;
- диагностика сверяет reasoning `high` и выводит только структурированные code/type/status Codex;
- диагностика выводит безопасный список текстовых GPT model ID и проверяет target даже вне `/v1/models`;
- добавлен официальный `codex login --with-api-key` под изолированным worker через stdin;
- добавлен временный root-owned `worksheet-admin` с фиксированными операциями и self-revoke;
- добавлен opt-in local-user worker для Codex-конфига и auth пользователя `w495`;
- добавлена read-only проверка точной Messenger identity перед запуском gateway;
- один переключаемый worker заменён двумя независимыми instances на портах 8443/8444;
- оба Messenger invite разрешаются read-only при provisioning, chat state и RPC tokens разделены;
- исправлены gateway dependency, personal account enforcement, mode session fallback и dual TLS rotation;
- worker logs теперь содержат только безопасную категорию Codex failure без raw stderr;
- добавлен hidden OAuth updater со строгой personal identity-проверкой до записи;
- добавлен официальный redacted `codex doctor --network-only` для API worker;
- исправлена загрузка instance-specific chat/email в Messenger identity-check;
- статусные сообщения и модельная самореференция переведены в безличный стиль;
- local-user instance разрешает task/image-generation любому участнику по явной настройке;
- добавлен отдельный sandbox-admin instance для task-запросов всех участников без доступа к `w495` home;
- worker units разрешают `AF_NETLINK`, необходимый bwrap для локального network namespace;
- sandbox-admin task-mode включает явно разрешённый внешний network access;
- sandbox-admin task-mode использует явно разрешённый `danger-full-access` внутри systemd isolation;
- PNG artifacts отправляются как нативные Messenger images, остальные — как documents;
- invalid PNG preview безопасно откатывается к document и не блокирует очередь gateway;
- валидный PNG отправляется двумя сообщениями: native image и downloadable document;
- оба gateway отправляют статусы, ответы и artifacts в тред исходного сообщения;
- task progress редактирует одно сообщение с BSD spinner и завершает его `[+] Готово.`;
- исходное сообщение получает реакцию 👀 в работе и ✅ после завершения;
- интервал обновления progress spinner сокращён до одной секунды;
- интервал обновления spinner установлен в 200 ms с best-effort edit delivery;
- интервал обновления spinner дополнительно ускорен до 100 ms;
- spinner заполнения сокращён до кадров `▁ ▂ ▃ ▅ ▆ ▇` без `▄` и `█`;
- spinner случайно выбирается из набора стилей и сохраняется для task в SQLite;
- финальный progress-status изменён на `✅ Готово.`;
- анимированные spinner-кадры выводятся без квадратных скобок;
- вариант spinner с движущимся квадратом удалён;
- добавлен spinner из девяти цветных кругов;
- добавлен spinner из девяти цветных квадратов;
- добавлен spinner из восьми монохромных квадратов;
- добавлен spinner из восьми emoji-стрелок;
- добавлен spinner из двенадцати emoji-часов;
- progress timestamp при отсутствии в push-response сверяется по payload_id через history;
- progress timestamp reconciliation учитывает eventual consistency с bounded backoff;
- progress timestamp извлекается из nested `ServerMessageInfo` push-response;
- progress start логирует только безопасный delivery failure code и response key shape;
- progress payload IDs участвуют в self-message filter и не запускают reply loop;
- `send_outcome_unknown` подтверждается через history по payload/author вместо retry-loop;
- inclusive history cursor/MaxTimestamp boundaries пропускаются как дубли вместо retry-loop;
- worker HTTP/connection failures завершают task один раз и продвигают thread cursor;
- API диагностика проверяет совместимые fallback-модели текущего Codex CLI;
- API-key production возвращён на штатный Codex provider `openai`; custom provider оставлен диагностическим;
- старый API-chat переведён на text-only direct Responses adapter с локальными UUID-сессиями;
- task-запросы API-чата маршрутизируются в sandbox-admin для команд и файлов;
- task prompt запрещает объявлять изображение готовым без реального файла в `out/`;
- удалённые родительские сообщения исключаются из thread polling, а сохранённые
  feeds таких тредов очищаются без бесконечного retry-loop;
- любые сообщения собственного Messenger-аккаунта исключаются из polling до
  разбора payload, поэтому document/image payload не запускает self-reply loop;
- `SetReaction` зарегистрирован в confirmation-preview registry, поэтому реакции
  👀 и ✅ проходят двухфазный вызов вместо `ValueError`;
- task timeout и другие ошибки завершают progress как `❌ Ошибка.` и снимают 👀
  вместо ложного `✅ Готово.` и реакции успешного завершения;
- добавлен публичный официальный Bot API client и единый `getUpdates` gateway,
  маршрутизирующий API- и sandbox-чаты к разным worker-профилям;
- добавлены безопасная локальная настройка OAuth бота, read-only identity/chat
  check, атомарное переключение на Team-бота и rollback к legacy userbot;
- подготовлена квадратная публичная аватарка `Lang Env Bot`;
- конфигуратор и gateway проверяют ожидаемую organization бота до запуска,
  чтобы токен из другой организации не приводил к `user_not_in_organization`;
- ошибка конфигуратора показывает безопасные expected/actual organization IDs;
- пользовательский OAuth client ID удалён из исходников и принимается только
  через `MESSENGER_OAUTH_CLIENT_ID` или явный аргумент URL-builder;
- `X-Ya-Organization-Id` проходит allowlist-проверку как положительное число
  до добавления в HTTP-заголовки;
- task execution timeout увеличен до 1800 секунд, task RPC timeout — до 1860;
- worker переведён на bounded threaded HTTP server с concurrency 4 и тестом
  реального пересечения двух одновременных task-запросов;
- добавлены документация и локальная проверка репозитория.
