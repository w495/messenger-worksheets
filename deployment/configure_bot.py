#!/usr/bin/env python3
"""Verify and save a Team-bot token without echoing it."""

from __future__ import annotations

import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "messenger-worksheets"))

from messenger_worksheets.bot_api import BotApiClient  # noqa: E402


def main() -> None:
    """Проверить Team-bot OAuth и атомарно сохранить токен и локальный config.

    Исключения:
        Отклоняет несовпадение имени/организации, symlink-цель и ошибки API или
        файловой системы до публикации неполной конфигурации.

    Побочные эффекты:
        Скрыто читает OAuth из терминала, делает read-only identity-запрос,
        затем заменяет token/config через приватные временные файлы mode 0600.

    Инварианты безопасности:
        Токен не передаётся аргументом, не печатается и не записывается через
        symlink; итоговые файлы создаются в заранее выбранных каталогах.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-name", required=True)
    parser.add_argument("--expected-organization-id", type=int)
    parser.add_argument("--auth-scheme", choices=("OAuth", "OAuthTeam"), default="OAuth")
    parser.add_argument(
        "--base-url", default="https://botapi.messenger.yandex.net/bot/v1/",
    )
    parser.add_argument(
        "--token-file", type=Path,
        default=Path.home() / ".config/messenger-worksheets/bot-token",
    )
    parser.add_argument(
        "--config", type=Path,
        default=REPO_ROOT / "deployment/instances.local.json",
    )
    args = parser.parse_args()
    token = getpass("Team-bot OAuth: ").strip()
    identity = BotApiClient(
        token, auth_scheme=args.auth_scheme, base_url=args.base_url,
    ).identity()
    if identity.get("display_name") != args.expected_name:
        raise ValueError("Team-bot identity does not match --expected-name")
    organizations = identity.get("organizations") or []
    if (args.expected_organization_id is not None
            and args.expected_organization_id not in organizations):
        actual = ",".join(map(str, organizations)) or "none"
        raise ValueError(
            "Team-bot organization mismatch: "
            f"expected={args.expected_organization_id} actual={actual}"
        )
    target = args.token_file.expanduser().resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("token file cannot be a symlink")
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as stream:
        stream.write(token + "\n")
        temporary = Path(stream.name)
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    config["bot"] = {
        "expected_name": args.expected_name,
        "token_file": str(target),
        "auth_scheme": args.auth_scheme,
        "base_url": args.base_url,
    }
    if args.expected_organization_id is not None:
        config["bot"]["organization_id"] = args.expected_organization_id
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.config.parent, delete=False) as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        config_temporary = Path(stream.name)
    try:
        os.chmod(config_temporary, 0o600)
        config_temporary.replace(args.config)
    finally:
        config_temporary.unlink(missing_ok=True)
    print(
        f"team_bot_configured login={identity['login']} "
        f"display_name={identity.get('display_name')} token_file={target}",
    )


if __name__ == "__main__":
    main()
