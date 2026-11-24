#!/usr/bin/env python3
"""Resolve configured Messenger invites and write two isolated instance envs."""

from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import pwd
import secrets
import stat
import sys
from tempfile import NamedTemporaryFile
import tomllib
import uuid


ETC = Path("/etc/messenger-worksheets")
LEGACY_GATEWAY_ENV = ETC / "gateway.env"
API_WORKER_ENV = ETC / "worker.env"


def read_env(path: Path) -> dict[str, str]:
    """Прочитать env как данные без shell-интерпретации.

    Параметры:
        path: Root-owned конфигурация существующего instance.

    Результат:
        Словарь всех строк ``KEY=VALUE`` для последующей контролируемой записи.

    Инварианты безопасности:
        Значения не печатаются и не исполняются.
    """
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def read_token_file(path: Path, owner_name: str) -> str:
    """Безопасно прочитать небольшой OAuth-файл с проверкой владельца и mode.

    Параметры:
        path: Абсолютный путь к regular-файлу без symlink.
        owner_name: Единственный допустимый непривилегированный владелец.

    Результат:
        Непустой OAuth без пробельных символов.

    Исключения:
        Отклоняет неверный путь, тип, размер, владельца, права или содержимое.

    Инварианты безопасности:
        Принимается только root либо заданный owner и отсутствуют group/other bits.
    """
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("token_file must be an absolute regular file, not a symlink")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 4096:
        raise ValueError("token_file has an invalid type or size")
    allowed_owners = {0, pwd.getpwnam(owner_name).pw_uid}
    if metadata.st_uid not in allowed_owners or metadata.st_mode & 0o077:
        raise ValueError("token_file must be owned by root/operator and have mode 0600")
    token = path.read_text(encoding="utf-8").strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("token_file contains an invalid OAuth value")
    return token


def write_env(path: Path, values: dict[str, str]) -> None:
    """Атомарно записать root-owned env с mode 0600.

    Параметры:
        path: Целевой файл конфигурации.
        values: Проверенные ключи и значения без переводов строк.

    Исключения:
        Отклоняет пустой ключ и CR/LF-инъекцию в значениях.

    Побочные эффекты:
        Создаёт временный файл рядом с целью, меняет owner/mode и делает rename.

    Инварианты безопасности:
        Частично записанный env никогда не публикуется под целевым именем.
    """
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        for key, value in values.items():
            if not key or "\n" in value or "\r" in value:
                raise ValueError("invalid environment value")
            stream.write(f"{key}={value}\n")
        temporary = Path(stream.name)
    try:
        os.chmod(temporary, 0o600)
        os.chown(temporary, 0, 0)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def canonical_chat(value: object) -> str:
    """Проверить внешний chat id как канонический ``0/0/<UUID>``.

    Параметры:
        value: Недоверенное значение из invite response.

    Результат:
        Исходная строка после строгой UUID-проверки.

    Исключения:
        ``ValueError`` для любого иного формата.
    """
    if not isinstance(value, str) or not value.startswith("0/0/"):
        raise ValueError("invite did not resolve to a canonical group chat")
    if str(uuid.UUID(value[4:])) != value[4:]:
        raise ValueError("invite returned an invalid group chat identifier")
    return value


def chat_id_from_result(result: dict) -> str:
    """Извлечь канонический chat id из нормализованного invite result.

    Параметры:
        result: Ответ ``resolve_invite`` выбранного backend.

    Результат:
        Проверенный id найденного группового чата.

    Исключения:
        Отклоняет ненайденный invite, неизвестный shape и некорректный UUID.
    """
    chat = result.get("chat")
    if not result.get("found") or not isinstance(chat, dict):
        raise ValueError("Messenger invite was not found")
    for key in ("chat_id", "ChatId", "id"):
        if key in chat:
            return canonical_chat(chat[key])
    raise ValueError("Messenger invite did not return chat_id")


def native_codex() -> str:
    """Найти единственный executable нативного Codex в установленном package.

    Результат:
        Абсолютный путь к исполняемому vendor binary.

    Исключения:
        ``RuntimeError`` при отсутствии либо неоднозначности кандидатов.

    Инварианты безопасности:
        Поиск ограничен фиксированным системным каталогом ``@openai/codex``.
    """
    package = Path("/usr/local/lib/node_modules/@openai/codex")
    candidates = sorted(package.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
    executable = [path for path in candidates if path.is_file() and os.access(path, os.X_OK)]
    if len(executable) != 1:
        raise RuntimeError("expected exactly one native Codex binary")
    return str(executable[0])


def local_user_profile() -> tuple[str, str]:
    """Прочитать безопасную проекцию model/effort пользовательского Codex config.

    Результат:
        Пара ``(model, reasoning_effort)`` после allowlist-валидации.

    Исключения:
        Отклоняет неизвестный model id или effort вместо передачи свободного TOML.

    Инварианты безопасности:
        Из config не извлекаются provider credentials и произвольные команды.
    """
    config_path = Path("/home/w495/.codex/config.toml")
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    model = config.get("model")
    effort = config.get("model_reasoning_effort")
    if not isinstance(model, str) or not model.startswith("gpt-") \
            or not all(char.isalnum() or char in "._-" for char in model):
        raise ValueError("local Codex config has an invalid model")
    if effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError("local Codex config has an invalid reasoning effort")
    return model, effort


def main() -> None:
    """Разрешить два invite и атомарно сформировать изолированные instance env.

    Исключения:
        Отклоняет запуск без root, неверный config/identity/invite, совпадающие
        чаты, несогласованные transport tokens и недопустимый backend.

    Побочные эффекты:
        Выполняет read-only Messenger identity/invite запросы, может ротировать
        новые transport tokens и заменяет root-owned worker/gateway env-файлы.

    Инварианты безопасности:
        OAuth читается только из защищённого файла; секреты не печатаются;
        backend выбирается явно без автоматического fallback.
    """
    if os.geteuid() != 0:
        raise SystemExit("Run instance provisioning with sudo.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    expected_email = config.get("expected_email")
    token_file = config.get("token_file")
    messenger_backend = config.get("messenger_backend", "cleanroom")
    bot = config.get("bot")
    instances = config.get("instances")
    if not isinstance(expected_email, str) or not isinstance(instances, dict):
        raise ValueError("invalid instance config")
    if messenger_backend not in {"legacy", "cleanroom"}:
        raise ValueError("messenger_backend must be legacy or cleanroom")
    if set(instances) != {"api", "local_user"}:
        raise ValueError("instance config must contain api and local_user")
    if bot is not None and (
        not isinstance(bot, dict)
        or not isinstance(bot.get("expected_name"), str)
        or not isinstance(bot.get("token_file"), str)
        or bot.get("auth_scheme", "OAuth") not in {"OAuth", "OAuthTeam"}
        or not isinstance(bot.get("base_url", ""), str)
        or (bot.get("organization_id") is not None
            and type(bot.get("organization_id")) is not int)
    ):
        raise ValueError("bot config must contain expected_name and token_file")

    legacy = read_env(LEGACY_GATEWAY_ENV)
    worker = read_env(API_WORKER_ENV)
    if token_file is not None:
        if not isinstance(token_file, str):
            raise ValueError("token_file must be a path string")
        messenger_token = read_token_file(Path(token_file), "w495")
    else:
        messenger_token = legacy["WORKSHEET_MESSENGER_TOKEN"]
    client_dir = Path(legacy.get(
        "WORKSHEET_MESSENGER_CLIENT_DIR", "/opt/messenger-worksheets/messenger-client",
    ))
    if messenger_backend == "legacy":
        client_dir = client_dir.resolve(strict=True)
        sys.path.insert(0, str(client_dir))
        from client import MessengerUserClient
        client_options = {
            "account": "personal",
            "expected_email": expected_email,
            "timeout": 12,
        }
    else:
        from messenger_user_client import MessengerUserClient
        client_options = {
            "account": "personal",
            "expected_email": expected_email,
            "enforce_account_class": False,
            "timeout": 12,
        }

    messenger = MessengerUserClient(messenger_token, **client_options)
    messenger.identity()
    resolved: dict[str, str] = {}
    for name in ("api", "local_user"):
        spec = instances[name]
        if not isinstance(spec, dict) or not isinstance(spec.get("invite"), str):
            raise ValueError(f"invalid {name} instance")
        resolved[name] = chat_id_from_result(messenger.resolve_invite(spec["invite"]))
    if resolved["api"] == resolved["local_user"]:
        raise ValueError("api and local_user invites resolved to the same chat")

    api_token = worker["WORKSHEET_CODEX_TRANSPORT_TOKEN"]
    if api_token != legacy["WORKSHEET_CODEX_TRANSPORT_TOKEN"] or len(api_token) < 32:
        raise ValueError("legacy API transport token is inconsistent")
    worker["WORKSHEET_CODEX_PROVIDER"] = "responses_direct"
    worker["WORKSHEET_CODEX_MODEL"] = "gpt-5.3-codex"
    worker["WORKSHEET_CODEX_REASONING_EFFORT"] = "high"
    worker["WORKSHEET_CODEX_TASK_TIMEOUT"] = "1800"
    worker["WORKSHEET_CODEX_CHAT_TIMEOUT"] = "1800"
    worker["WORKSHEET_CODEX_MAX_CONCURRENT_JOBS"] = "4"
    write_env(API_WORKER_ENV, worker)
    local_gateway_path = ETC / "gateway-local-user.env"
    if local_gateway_path.exists():
        local_token = read_env(local_gateway_path).get("WORKSHEET_CODEX_TRANSPORT_TOKEN", "")
    else:
        local_token = ""
    if len(local_token) < 32 or hmac.compare_digest(local_token, api_token):
        local_token = secrets.token_hex(32)
    sandbox_gateway_path = ETC / "gateway-sandbox-admin.env"
    if sandbox_gateway_path.exists():
        sandbox_token = read_env(sandbox_gateway_path).get("WORKSHEET_CODEX_TRANSPORT_TOKEN", "")
    else:
        sandbox_token = ""
    if len(sandbox_token) < 32 \
            or hmac.compare_digest(sandbox_token, api_token) \
            or hmac.compare_digest(sandbox_token, local_token):
        sandbox_token = secrets.token_hex(32)

    common_gateway = {
        "WORKSHEET_MESSENGER_BACKEND": messenger_backend,
        "WORKSHEET_MESSENGER_CLIENT_DIR": str(client_dir),
        "WORKSHEET_MESSENGER_TOKEN": messenger_token,
        "WORKSHEET_MESSENGER_EMAIL": expected_email,
        "WORKSHEET_REPLY_IN_THREADS": "true",
        "WORKSHEET_CODEX_TASK_RPC_TIMEOUT": "1860",
        "WORKSHEET_CODEX_CHAT_RPC_TIMEOUT": "1860",
    }
    write_env(ETC / "gateway-api.env", {
        **common_gateway,
        "WORKSHEET_MESSENGER_CHAT_ID": resolved["api"],
        "WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true",
        "WORKSHEET_CODEX_WORKER_CA": str(ETC / "ca-api.crt"),
        "WORKSHEET_CODEX_WORKER_URL": "https://127.0.0.1:8443/v1/codex-worksheet-jobs",
        "WORKSHEET_CODEX_TRANSPORT_TOKEN": api_token,
        "WORKSHEET_TASK_WORKER_URL": "https://127.0.0.1:8445/v1/codex-worksheet-jobs",
        "WORKSHEET_TASK_WORKER_TRANSPORT_TOKEN": sandbox_token,
        "WORKSHEET_TASK_WORKER_CA": str(ETC / "ca-sandbox-admin.crt"),
    })
    write_env(local_gateway_path, {
        **common_gateway,
        "WORKSHEET_MESSENGER_CHAT_ID": resolved["local_user"],
        "WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true",
        "WORKSHEET_CODEX_WORKER_CA": str(ETC / "ca-local-user.crt"),
        "WORKSHEET_CODEX_WORKER_URL": "https://127.0.0.1:8444/v1/codex-worksheet-jobs",
        "WORKSHEET_CODEX_TRANSPORT_TOKEN": local_token,
    })
    write_env(sandbox_gateway_path, {
        **common_gateway,
        "WORKSHEET_MESSENGER_CHAT_ID": resolved["local_user"],
        "WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true",
        "WORKSHEET_CODEX_WORKER_CA": str(ETC / "ca-sandbox-admin.crt"),
        "WORKSHEET_CODEX_WORKER_URL": "https://127.0.0.1:8445/v1/codex-worksheet-jobs",
        "WORKSHEET_CODEX_TRANSPORT_TOKEN": sandbox_token,
    })
    if bot is not None:
        bot_name = bot["expected_name"].strip()
        if not bot_name or len(bot_name) > 100 or "\n" in bot_name or "\r" in bot_name:
            raise ValueError("invalid Team bot display name")
        bot_token = read_token_file(Path(bot["token_file"]), "w495")
        write_env(ETC / "gateway-bot.env", {
            "WORKSHEET_CODEX_TASK_RPC_TIMEOUT": "1860",
            "WORKSHEET_CODEX_CHAT_RPC_TIMEOUT": "1860",
            "WORKSHEET_BOT_TOKEN": bot_token,
            "WORKSHEET_BOT_AUTH_SCHEME": bot.get("auth_scheme", "OAuth"),
            "WORKSHEET_BOT_BASE_URL": bot.get(
                "base_url", "https://botapi.messenger.yandex.net/bot/v1/",
            ),
            "WORKSHEET_BOT_EXPECTED_NAME": bot_name,
            "WORKSHEET_BOT_API_CHAT_ID": resolved["api"],
            "WORKSHEET_BOT_SANDBOX_CHAT_ID": resolved["local_user"],
            "WORKSHEET_BOT_API_WORKER_URL": "https://127.0.0.1:8443/v1/codex-worksheet-jobs",
            "WORKSHEET_BOT_API_WORKER_TOKEN": api_token,
            "WORKSHEET_BOT_API_WORKER_CA": str(ETC / "ca-api.crt"),
            "WORKSHEET_BOT_API_TASK_URL": "https://127.0.0.1:8445/v1/codex-worksheet-jobs",
            "WORKSHEET_BOT_API_TASK_TOKEN": sandbox_token,
            "WORKSHEET_BOT_API_TASK_CA": str(ETC / "ca-sandbox-admin.crt"),
            "WORKSHEET_BOT_SANDBOX_WORKER_URL": "https://127.0.0.1:8445/v1/codex-worksheet-jobs",
            "WORKSHEET_BOT_SANDBOX_WORKER_TOKEN": sandbox_token,
            "WORKSHEET_BOT_SANDBOX_WORKER_CA": str(ETC / "ca-sandbox-admin.crt"),
            "WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true",
            "WORKSHEET_REPLY_IN_THREADS": "true",
            **(
                {"WORKSHEET_BOT_EXPECTED_ORGANIZATION_ID": str(bot["organization_id"])}
                if bot.get("organization_id") is not None else {}
            ),
        })
    local_model, local_effort = local_user_profile()
    write_env(ETC / "worker-local-user.env", {
        "WORKSHEET_CODEX_PROVIDER": "local_user",
        "WORKSHEET_CODEX_BINARY": native_codex(),
        "WORKSHEET_CODEX_MODEL": local_model,
        "WORKSHEET_CODEX_REASONING_EFFORT": local_effort,
        "WORKSHEET_CODEX_TASK_TIMEOUT": "1800",
        "WORKSHEET_CODEX_CHAT_TIMEOUT": "1800",
        "WORKSHEET_CODEX_MAX_CONCURRENT_JOBS": "4",
        "WORKSHEET_ENABLE_IMAGE_GENERATION": "true",
        "WORKSHEET_CODEX_LISTEN": "127.0.0.1",
        "WORKSHEET_CODEX_PORT": "8444",
        "WORKSHEET_CODEX_TLS_CERT": str(ETC / "local-user/server.crt"),
        "WORKSHEET_CODEX_TLS_KEY": str(ETC / "local-user/server.key"),
        "WORKSHEET_CODEX_TRANSPORT_TOKEN": local_token,
    })
    write_env(ETC / "worker-sandbox-admin.env", {
        "WORKSHEET_CODEX_PROVIDER": "sandbox_admin",
        "WORKSHEET_CODEX_BINARY": native_codex(),
        "WORKSHEET_CODEX_MODEL": local_model,
        "WORKSHEET_CODEX_REASONING_EFFORT": local_effort,
        "WORKSHEET_CODEX_TASK_TIMEOUT": "1800",
        "WORKSHEET_CODEX_CHAT_TIMEOUT": "1800",
        "WORKSHEET_CODEX_MAX_CONCURRENT_JOBS": "4",
        "WORKSHEET_ENABLE_IMAGE_GENERATION": "true",
        "WORKSHEET_CODEX_LISTEN": "127.0.0.1",
        "WORKSHEET_CODEX_PORT": "8445",
        "WORKSHEET_CODEX_TLS_CERT": str(ETC / "sandbox-admin/server.crt"),
        "WORKSHEET_CODEX_TLS_KEY": str(ETC / "sandbox-admin/server.key"),
        "WORKSHEET_CODEX_TRANSPORT_TOKEN": sandbox_token,
    })
    for name, chat_id in resolved.items():
        print(f"resolved_instance={name} chat_id={chat_id}")
    if bot is not None:
        print(f"team_bot_configured expected_name={bot_name}")
    print(f"local_user_profile model={local_model} reasoning_effort={local_effort}")


if __name__ == "__main__":
    main()
