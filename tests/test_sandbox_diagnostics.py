from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("sandbox_diagnostics", Path(__file__).resolve().parents[1] / "deployment/diagnose_sandbox.py")
diag = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diag)


class SandboxDiagnosticsTest(unittest.TestCase):
    def properties(self):
        """Вернуть только известный исходный hardening production worker для unit tests."""
        properties = {key: "yes" for key in diag.PROPERTY_VALUES}
        properties.update(User=diag.ACCOUNT, Group=diag.ACCOUNT, ProtectSystem="strict",
                          ProtectHome="yes", ProtectProc="invisible", ProcSubset="pid",
                          RestrictNamespaces="no", RestrictAddressFamilies="AF_INET AF_INET6 AF_NETLINK AF_UNIX", AppArmorProfile="")
        return properties

    def test_metadata_read_uses_fixed_service_and_never_reads_environment(self):
        """Systemctl show запрашивает только известные свойства одного фиксированного unit."""
        properties = self.properties()
        raw = "\n".join(key + "=" + value for key, value in properties.items()).encode()
        with patch.object(diag, "bounded_process", return_value=(0, raw, b"")) as run:
            self.assertEqual(diag.read_properties(), properties)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["/usr/bin/systemctl", "show", diag.SERVICE])
        self.assertEqual({item.removeprefix("--property=") for item in argv if item.startswith("--property=")}, set(diag.PROPERTY_NAMES))
        self.assertNotIn("Environment", " ".join(argv))
        for bad in ({**properties, "ExecStart": "/bin/sh"}, {**properties, "User": "root"},
                    {**properties, "ProtectSystem": "strict\nExecStart=/bin/sh"},
                    {**properties, "RestrictAddressFamilies": "~AF_UNIX"},
                    {**properties, "AppArmorProfile": "other-profile"}):
            with self.subTest(bad=bad), self.assertRaises(diag.DiagnosticFailure):
                diag.validate_properties(bad)

    def test_static_variants_change_exactly_one_property_and_have_fixed_harmless_command(self):
        """Relaxed unit запускает только printf; каждое сравнение меняет одно свойство."""
        unit = "worksheet-sandbox-diag-" + "a" * 32 + ".service"
        binary = Path("/usr/local/lib/node_modules/@openai/codex/native/bin/codex")
        baseline = None
        for variant in diag.VARIANTS:
            argv = diag.probe_command(unit, binary, self.properties(), variant)
            self.assertEqual(argv[argv.index("--") + 1:], [str(binary), "sandbox", "-P", ":read-only", "/usr/bin/printf", diag.MARKER + "\n"])
            self.assertIn("--wait", argv)
            self.assertIn("--pipe", argv)
            self.assertIn("--collect", argv)
            self.assertIn("--property=KillMode=control-group", argv)
            properties = dict(item[len("--property="):].split("=", 1) for item in argv if item.startswith("--property="))
            self.assertIn("AF_NETLINK", properties["RestrictAddressFamilies"])
            self.assertEqual(properties["User"], diag.ACCOUNT)
            if baseline is None:
                baseline = properties
                self.assertEqual(properties["InaccessiblePaths"], "-/data -/mnt -/media -/srv")
            else:
                differences = {key for key in properties if properties[key] != baseline[key]}
                self.assertEqual(differences, {variant[1]})
        for badunit in (diag.SERVICE, "worksheet-sandbox-diag-invalid.service", "a.service;reboot"):
            with self.assertRaises(diag.DiagnosticFailure):
                diag.probe_command(badunit, binary, self.properties(), diag.VARIANTS[0])
        with self.assertRaises(diag.DiagnosticFailure):
            diag.probe_command(unit, binary, {**self.properties(), "ExecStart": "/bin/sh"}, diag.VARIANTS[0])

    def test_native_phase_is_only_original_hardening_and_whitelisted_settings(self):
        """Модель запускается только baseline без разрешения relaxed unit и чужих env ключей."""
        binary = Path("/fixed/codex")
        unit = "worksheet-sandbox-diag-" + "b" * 32 + ".service"
        settings = {"WORKSHEET_CODEX_BINARY": str(binary), "WORKSHEET_CODEX_MODEL": "gpt-6.2-sol", "WORKSHEET_CODEX_REASONING_EFFORT": "high"}
        argv = diag.probe_command(unit, binary, self.properties(), diag.VARIANTS[0], settings)
        self.assertEqual(argv[argv.index("--") + 1:], [diag.INNER_PYTHON, str(diag.INNER_SCRIPT), "--inner-native"])
        self.assertIn("--property=RuntimeMaxSec=150s", argv)
        self.assertIn("--property=ProtectSystem=strict", argv)
        self.assertIn("--property=ProtectHome=yes", argv)
        self.assertIn("--setenv=WORKSHEET_CODEX_TASK_TIMEOUT=120", argv)
        for variant in diag.VARIANTS[1:]:
            with self.assertRaises(diag.DiagnosticFailure):
                diag.probe_command(unit, binary, self.properties(), variant, settings)
        for bad in ({**settings, "WORKSHEET_CODEX_API_KEY": "synthetic-secret"},
                    {**settings, "WORKSHEET_CODEX_BINARY": "/bin/sh"},
                    {**settings, "WORKSHEET_CODEX_MODEL": "model -c other"}):
            with self.assertRaises(diag.DiagnosticFailure):
                diag.validate_settings(bad, binary)

    def test_inner_guard_rejects_root_and_other_uid_before_worker_import(self):
        """Ручной вызов inner от root/постороннего пользователя не запускает модель."""
        with patch.object(sys, "argv", ["diagnose_sandbox.py", "--inner-native"]), patch.object(diag, "inner_native") as inner:
            for uid in (0, 1000):
                with patch.object(os, "geteuid", return_value=uid), patch.object(diag.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=994)), self.assertRaises(SystemExit):
                    diag.main()
            inner.assert_not_called()
            with patch.object(os, "geteuid", return_value=994), patch.object(diag.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=994)):
                diag.main()
            inner.assert_called_once()

    def test_probe_cleanup_runs_after_success_failure_and_timeout_only_for_own_unit(self):
        """Finally очищает сгенерированный unit при любом результате и не трогает worker."""
        for outcome in ((0, (diag.MARKER + "\n").encode(), b""), (1, b"", b"bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"), diag.DiagnosticFailure("timeout")):
            with patch.object(diag, "bounded_process", side_effect=outcome if isinstance(outcome, Exception) else None, return_value=outcome), patch.object(diag, "stop_probe", return_value=True) as stop:
                result = diag.probe(Path("/fixed/codex"), self.properties(), diag.VARIANTS[0])
            unit = stop.call_args.args[0]
            self.assertIsNotNone(diag.UNIT_NAME.fullmatch(unit))
            self.assertNotEqual(unit, diag.SERVICE)
            self.assertTrue(result["cleanup_ok"])
            if not isinstance(outcome, Exception) and outcome[0] == 1:
                self.assertIn("Failed RTM_NEWADDR", result["diagnostic"])
        with patch.object(diag, "bounded_process") as run, self.assertRaises(diag.DiagnosticFailure):
            diag.stop_probe(diag.SERVICE)
        run.assert_not_called()

    def test_native_path_requires_fixed_package_root_owner_and_nonwritable_ancestors(self):
        """Symlink escape, чужой владелец и writable binary/parent отклоняются до запуска."""
        package = diag.NATIVE_PACKAGE
        candidate = package / "native/bin/codex"
        safe = SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)
        with patch.object(Path, "resolve", return_value=candidate), patch.object(Path, "stat", return_value=safe):
            self.assertEqual(diag.validate_native(candidate), candidate)
        for info in (SimpleNamespace(st_mode=stat.S_IFREG | 0o777, st_uid=0), SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=1000)):
            with patch.object(Path, "resolve", return_value=candidate), patch.object(Path, "stat", return_value=info), self.assertRaises(diag.DiagnosticFailure):
                diag.validate_native(candidate)
        with patch.object(Path, "resolve", return_value=Path("/tmp/codex")), self.assertRaises(diag.DiagnosticFailure):
            diag.validate_native(candidate)

    def test_settings_reader_ignores_credentials_and_rejects_duplicate_safe_keys(self):
        """Из env возвращаются только три заданных ключа; credentials не попадают в unit."""
        with TemporaryDirectory() as directory:
            file = Path(directory) / "worker.env"
            file.write_text("WORKSHEET_CODEX_API_KEY=synthetic-secret\nWORKSHEET_CODEX_BINARY=/fixed/codex\nWORKSHEET_CODEX_MODEL=gpt-6.2-sol\nWORKSHEET_CODEX_REASONING_EFFORT=high\n")
            with patch.object(diag, "WORKER_ENV", file), patch.object(diag, "validate_owned_file"):
                result = diag.read_settings(Path("/fixed/codex"))
                self.assertEqual(set(result), diag.SETTING_KEYS)
                self.assertNotIn("synthetic-secret", json.dumps(result))
                with file.open("a") as stream:
                    stream.write("WORKSHEET_CODEX_MODEL=other\n")
                with self.assertRaises(diag.DiagnosticFailure):
                    diag.read_settings(Path("/fixed/codex"))

    def test_redaction_and_trace_projection_keep_error_without_secrets_commands_or_raw_json(self):
        """Ошибки bwrap сохраняются, токены/URL query и произвольные поля не проходят."""
        raw = b"bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted\nBearer synthetic-secret token=private https://example.test/a?key=secret#fragment"
        text = diag.safe_diagnostic(raw)
        self.assertIn("Failed RTM_NEWADDR", text)
        for secret in ("synthetic-secret", "private", "key=secret", "fragment"):
            self.assertNotIn(secret, text)
        trace = {"marker_found": False, "commands": [{"exit_code": 1, "diagnostic": raw.decode()}], "final_reply": "own synthetic reply", "error": None}
        self.assertNotIn("synthetic-secret", json.dumps(diag.validate_trace(trace)))
        trace["commands"][0]["command"] = "must not be output"
        with self.assertRaises(diag.DiagnosticFailure):
            diag.validate_trace(trace)

    def test_bounded_process_caps_output_and_deadline_reaps_own_process(self):
        """Реальный unprivileged child убирается при переполнении stdout и timeout."""
        with TemporaryDirectory() as directory:
            pidfile = Path(directory) / "pid"
            for code, timeout, expected in (("print('x' * 65536, flush=True)", 2, "output_limit"), ("", 0.15, "timeout")):
                source = f"import os,time\nopen({str(pidfile)!r},'w').write(str(os.getpid()))\n{code}\ntime.sleep(20)"
                with self.assertRaises(diag.DiagnosticFailure) as error:
                    diag.bounded_process([sys.executable, "-c", source], timeout=timeout)
                self.assertEqual(str(error.exception), expected)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), 0)
            with patch.dict(os.environ, {"UNRELATED_SECRET": "synthetic-secret"}):
                status, output, _ = diag.bounded_process([sys.executable, "-c", "import os,json; print(json.dumps(sorted(os.environ)))"])
            self.assertEqual(status, 0)
            self.assertNotIn("UNRELATED_SECRET", json.loads(output))


if __name__ == "__main__":
    unittest.main()
