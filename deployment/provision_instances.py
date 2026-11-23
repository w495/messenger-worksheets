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
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def read_token_file(path: Path, owner_name: str) -> str:
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
    if not isinstance(value, str) or not value.startswith("0/0/"):
        raise ValueError("invite did not resolve to a canonical group chat")
    if str(uuid.UUID(value[4:])) != value[4:]:
        raise ValueError("invite returned an invalid group chat identifier")
    return value


def chat_id_from_result(result: dict) -> str:
    chat = result.get("chat")
    if not result.get("found") or not isinstance(chat, dict):
        raise ValueError("Messenger invite was not found")
    for key in ("chat_id", "ChatId", "id"):
        if key in chat:
            return canonical_chat(chat[key])
    raise ValueError("Messenger invite did not return chat_id")


def native_codex() -> str:
    package = Path("/usr/local/lib/node_modules/@openai/codex")
    candidates = sorted(package.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
    executable = [path for path in candidates if path.is_file() and os.access(path, os.X_OK)]
    if len(executable) != 1:
        raise RuntimeError("expected exactly one native Codex binary")
    return str(executable[0])


def local_user_profile() -> tuple[str, str]:
    config_path = Path("/home/operator/.codex/config.toml")
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
    if os.geteuid() != 0:
        raise SystemExit("Run instance provisioning with sudo.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    expected_email = config.get("expected_email")
    token_file = config.get("token_file")
    instances = config.get("instances")
    if not isinstance(expected_email, str) or not isinstance(instances, dict):
        raise ValueError("invalid instance config")
    if set(instances) != {"api", "local_user"}:
        raise ValueError("instance config must contain api and local_user")

    legacy = read_env(LEGACY_GATEWAY_ENV)
    worker = read_env(API_WORKER_ENV)
    if token_file is not None:
        if not isinstance(token_file, str):
            raise ValueError("token_file must be a path string")
        messenger_token = read_token_file(Path(token_file), "operator")
    else:
        messenger_token = legacy["WORKSHEET_MESSENGER_TOKEN"]
    client_dir = Path(legacy["WORKSHEET_MESSENGER_CLIENT_DIR"]).resolve(strict=True)
    sys.path.insert(0, str(client_dir))
    from client import MessengerUserClient

    messenger = MessengerUserClient(
        messenger_token,
        account="personal",
        expected_email=expected_email,
        timeout=12,
    )
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
        "WORKSHEET_MESSENGER_CLIENT_DIR": str(client_dir),
        "WORKSHEET_MESSENGER_TOKEN": messenger_token,
        "WORKSHEET_MESSENGER_EMAIL": expected_email,
        "WORKSHEET_CODEX_WORKER_CA": str(ETC / "ca.crt"),
    }
    write_env(ETC / "gateway-api.env", {
        **common_gateway,
        "WORKSHEET_MESSENGER_CHAT_ID": resolved["api"],
        "WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "false",
        "WORKSHEET_CODEX_WORKER_URL": "https://127.0.0.1:8443/v1/codex-worksheet-jobs",
        "WORKSHEET_CODEX_TRANSPORT_TOKEN": api_token,
    })
    write_env(local_gateway_path, {
        **common_gateway,
        "WORKSHEET_MESSENGER_CHAT_ID": resolved["local_user"],
        "WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true",
        "WORKSHEET_CODEX_WORKER_URL": "https://127.0.0.1:8444/v1/codex-worksheet-jobs",
        "WORKSHEET_CODEX_TRANSPORT_TOKEN": local_token,
    })
    write_env(sandbox_gateway_path, {
        **common_gateway,
        "WORKSHEET_MESSENGER_CHAT_ID": resolved["local_user"],
        "WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true",
        "WORKSHEET_CODEX_WORKER_URL": "https://127.0.0.1:8445/v1/codex-worksheet-jobs",
        "WORKSHEET_CODEX_TRANSPORT_TOKEN": sandbox_token,
    })
    local_model, local_effort = local_user_profile()
    write_env(ETC / "worker-local-user.env", {
        "WORKSHEET_CODEX_PROVIDER": "local_user",
        "WORKSHEET_CODEX_BINARY": native_codex(),
        "WORKSHEET_CODEX_MODEL": local_model,
        "WORKSHEET_CODEX_REASONING_EFFORT": local_effort,
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
        "WORKSHEET_ENABLE_IMAGE_GENERATION": "true",
        "WORKSHEET_CODEX_LISTEN": "127.0.0.1",
        "WORKSHEET_CODEX_PORT": "8445",
        "WORKSHEET_CODEX_TLS_CERT": str(ETC / "sandbox-admin/server.crt"),
        "WORKSHEET_CODEX_TLS_KEY": str(ETC / "sandbox-admin/server.key"),
        "WORKSHEET_CODEX_TRANSPORT_TOKEN": sandbox_token,
    })
    for name, chat_id in resolved.items():
        print(f"resolved_instance={name} chat_id={chat_id}")
    print(f"local_user_profile model={local_model} reasoning_effort={local_effort}")


if __name__ == "__main__":
    main()
