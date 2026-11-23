# Changelog

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
- добавлен opt-in local-user worker для Codex-конфига и auth пользователя `operator`;
- добавлена read-only проверка точной Messenger identity перед запуском gateway;
- один переключаемый worker заменён двумя независимыми instances на портах 8443/8444;
- оба Messenger invite разрешаются read-only при provisioning, chat state и RPC tokens разделены;
- исправлены gateway dependency, personal account enforcement, mode session fallback и dual TLS rotation;
- worker logs теперь содержат только безопасную категорию Codex failure без raw stderr;
- добавлен hidden OAuth updater со строгой personal identity-проверкой до записи;
- добавлен официальный redacted `codex doctor --network-only` для API worker;
- исправлена загрузка instance-specific chat/email в Messenger identity-check;
- статусные сообщения и модельная самореференция переведены в безличный стиль;
- добавлены документация и локальная проверка репозитория.
