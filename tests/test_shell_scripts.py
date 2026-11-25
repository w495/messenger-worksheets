"""Проверить существенное поведение shell без root и боевых служб."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from deployment.check_shell_scripts import SHELLS


ROOT = Path(__file__).resolve().parents[1]


class ShellScriptsTest(unittest.TestCase):
    def setUp(self):
        """Создать временные команды и чистое окружение без секретов.

        Только тестовая копия генератора пишет сертификаты во временный
        каталог. Подмена install убирает смену владельца, сохраняя режимы.
        Системные каталоги и настоящие службы не изменяются.
        """
        self.storage = tempfile.TemporaryDirectory(prefix="worksheet-shell-test-")
        self.addCleanup(self.storage.cleanup)
        self.base = Path(self.storage.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.environment = {
            "PATH": f"{self.bin}{os.pathsep}{os.defpath}",
            "HOME": str(self.base), "ZDOTDIR": str(self.base),
            "TMPDIR": str(self.base), "LC_ALL": "C.UTF-8",
        }
        self.shells = [shutil.which(name) for name in SHELLS if shutil.which(name)]
        self.assertTrue(self.shells)
        self.command("id", "printf '0\\n'\n")
        self.command("getent", "exit 0\n")
        install = shutil.which("install")
        self.assertIsNotNone(install)
        wrapper = self.bin / "install"
        wrapper.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            "arguments = []\nindex = 1\n"
            "while index < len(sys.argv):\n"
            "    if sys.argv[index] in ('-o', '-g'):\n"
            "        index += 2\n"
            "    else:\n"
            "        arguments.append(sys.argv[index])\n"
            "        index += 1\n"
            f"os.execv({install!r}, ['install', *arguments])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)

    def command(self, name, body):
        """Установить тестовый sh-двойник команды в приватный PATH."""
        path = self.bin / name
        path.write_text("#!/usr/bin/env sh\n" + body, encoding="utf-8")
        path.chmod(0o700)

    def run_shell(self, shell, path, *arguments, expected=0):
        """Запустить временный сценарий и проверить статус без боевого env."""
        result = subprocess.run(
            [shell, str(path), *arguments], env=self.environment, cwd=self.base,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_tls_generation_and_admin_validation_in_available_shells(self):
        """Выпустить настоящие цепочки и проверить права, CA и отказ OpenSSL.

        Проверяется работа скриптов в каждой установленной оболочке.
        Группы и root-проверка подменены; OpenSSL и режимы файлов настоящие.
        """
        openssl = shutil.which("openssl")
        self.assertIsNotNone(openssl)
        generator_source = (ROOT / "deployment/generate_tls.sh").read_text()
        helper_source = (ROOT / "deployment/worksheet-admin").read_text()
        for index, shell in enumerate(self.shells):
            with self.subTest(shell=shell):
                target = self.base / f"tls-{index}"
                generator = self.base / "generate_tls.sh"
                generator.write_text(generator_source.replace(
                    "destination=/etc/messenger-worksheets", f"destination={target}",
                ))
                self.run_shell(shell, generator)
                self.assertEqual((target / "ca.crt").read_bytes(), (target / "ca-api.crt").read_bytes())
                self.assertFalse(list(target.rglob("ca.key")))
                for profile in ("worker", "local-user", "sandbox-admin"):
                    key = target / profile / "server.key"
                    self.assertEqual(key.stat().st_mode & 0o777, 0o640)
                helper = self.base / "worksheet-admin"
                helper.write_text(helper_source.replace(
                    "export PATH=/usr/local/bin:/usr/bin:/bin",
                    f"export PATH={self.environment['PATH']}",
                ).replace("config=/etc/messenger-worksheets", f"config={target}"))
                result = self.run_shell(shell, helper, "check-tls-isolation")
                self.assertIn("tls_profile_isolation_ok profiles=3", result.stdout)
                self.assertNotEqual(subprocess.run(
                    [openssl, "verify", "-CAfile", str(target / "ca-api.crt"),
                     str(target / "sandbox-admin/server.crt")], capture_output=True,
                ).returncode, 0)
                # Успешный sha256sum не должен скрывать сбой x509 -pubkey.
                self.command("openssl", 'case " $* " in\n'
                             '  *" -pubkey "*) exit 37 ;;\nesac\n'
                             f'exec "{openssl}" "$@"\n')
                failed = self.run_shell(shell, helper, "check-tls-isolation", expected=37)
                self.assertNotIn("tls_profile_isolation_ok", failed.stdout)
                (self.bin / "openssl").unlink()

    def test_tls_repair_restores_only_previously_active_workers(self):
        """Проверить выбор служб и отказ при неготовом TLS без systemd.

        curl возвращает сначала 401, затем ошибку транспорта. Неактивный
        local-user не должен получить start или stop ни в одной оболочке.
        """
        self.command("systemctl", 'printf "%s\\n" "$*" >> "$CHECK_LOG"\n'
                     'case "$*" in\n'
                     '  "is-active --quiet worksheet-worker-local-user.service") exit 1 ;;\n'
                     'esac\n')
        self.command("sleep", "exit 0\n")
        generator = self.base / "generate_tls.sh"
        generator.write_text("#!/usr/bin/env sh\nexit 0\n")
        generator.chmod(0o700)
        repair = self.base / "repair_tls.sh"
        repair.write_text((ROOT / "deployment/repair_tls.sh").read_text().replace(
            '"${check_attempt}" -lt 40', '"${check_attempt}" -lt 2',
        ))
        log = self.base / "systemctl.log"
        self.environment["CHECK_LOG"] = str(log)
        for shell in self.shells:
            with self.subTest(shell=shell):
                self.command("curl", "printf 401\n")
                log.write_text("")
                self.run_shell(shell, repair)
                operations = log.read_text().splitlines()
                self.assertIn("start worksheet-worker.service", operations)
                self.assertIn("start worksheet-worker-sandbox-admin.service", operations)
                self.assertFalse(any(
                    line.startswith(("start ", "stop ")) and "local-user" in line
                    for line in operations
                ))
                self.command("curl", "exit 7\n")
                failed = self.run_shell(shell, repair, expected=1)
                self.assertNotIn("HTTP 401", failed.stdout)

    def test_cleanup_preserves_failure_and_signal_status(self):
        """Проверить cleanup всех секретных temp, включая семантику mksh.

        Извлекается только функция очистки из текущего исходника, а не
        системные операции установщика. Исходный статус не должен исчезать
        при успешном удалении; сигнал также обязан дать ненулевой статус.
        """
        for filename in ("install.sh", "generate_tls.sh", "install_admin_helper.sh", "worksheet-admin"):
            source = (ROOT / "deployment" / filename).read_text()
            cleanup = re.search(r"^cleanup\(\) \{\n.*?^\}", source, re.M | re.S)
            self.assertIsNotNone(cleanup)
            for shell in self.shells:
                for action, expected in (("exit 0", 0), ("exit 37", 37), ("kill -TERM $$", 143)):
                    with self.subTest(script=filename, shell=shell, action=action):
                        payload = self.base / "temporary-secret"
                        payload.mkdir(exist_ok=True)
                        (payload / "fixture").write_text("TEST_ONLY")
                        script = self.base / "cleanup.sh"
                        script.write_text(
                            "set -eu\nterminal_state=\n"
                            f"secret_directory={payload}\ncertificate_work={payload}\n"
                            f"temporary={payload / 'fixture'}\nsudoers_tmp={payload / 'fixture'}\n"
                            + cleanup.group() + "\ntrap cleanup 0\n"
                            "trap 'exit 143' TERM\n" + action + "\n",
                        )
                        self.run_shell(shell, script, expected=expected)
                        self.assertFalse((payload / "fixture").exists())
                        if payload.exists():
                            payload.rmdir()


if __name__ == "__main__":
    unittest.main()
