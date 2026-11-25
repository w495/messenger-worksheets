#!/usr/bin/env python3
"""Сравнить sandbox Codex в одноразовых unit без изменения production-службы."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlsplit, urlunsplit
import uuid


SERVICE = "worksheet-worker-sandbox-admin.service"
ACCOUNT = "worksheet-chat-admin"
STATE = "/var/lib/messenger-worksheets-chat-admin"
NATIVE_PACKAGE = Path("/usr/local/lib/node_modules/@openai/codex")
MARKER = "WORKSHEET_SANDBOX_DIAG_OK"
UNIT_NAME = re.compile(r"worksheet-sandbox-diag-[0-9a-f]{32}\.service\Z", re.ASCII)
TIMEOUT = 15.0
MAX_OUTPUT = 32 * 1024
WORKER_ENV = Path("/etc/messenger-worksheets/worker-sandbox-admin.env")
INNER_SCRIPT = Path("/opt/messenger-worksheets/admin/diagnose_sandbox.py")
INNER_PYTHON = "/opt/messenger-worksheets/venv/bin/python"
SETTING_KEYS = frozenset({"WORKSHEET_CODEX_BINARY", "WORKSHEET_CODEX_MODEL", "WORKSHEET_CODEX_REASONING_EFFORT"})
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z", re.ASCII)
EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
BOOL = frozenset({"yes", "no"})
PROPERTY_VALUES = {
    "User": frozenset({ACCOUNT}), "Group": frozenset({ACCOUNT}),
    "NoNewPrivileges": BOOL, "PrivateDevices": BOOL,
    "PrivateTmp": frozenset({"yes", "no", "disconnected"}), "PrivateMounts": BOOL,
    "ProtectSystem": frozenset({"no", "yes", "full", "strict"}),
    "ProtectHome": frozenset({"no", "yes", "read-only", "tmpfs"}),
    "ProtectProc": frozenset({"default", "noaccess", "invisible", "ptraceable"}),
    "ProcSubset": frozenset({"all", "pid"}), "ProtectControlGroups": BOOL,
    "ProtectKernelLogs": BOOL, "ProtectKernelModules": BOOL, "ProtectKernelTunables": BOOL,
    "RestrictNamespaces": BOOL, "AppArmorProfile": frozenset({"", "unconfined"}),
}
FAMILIES = frozenset({"AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"})
PROPERTY_NAMES = (*PROPERTY_VALUES, "RestrictAddressFamilies")
VARIANTS = (
    ("baseline", None, None), ("private_devices_no", "PrivateDevices", "no"),
    ("proc_subset_all", "ProcSubset", "all"), ("protect_proc_default", "ProtectProc", "default"),
    ("protect_kernel_tunables_no", "ProtectKernelTunables", "no"),
    ("protect_system_no", "ProtectSystem", "no"),
    ("protect_home_no", "ProtectHome", "no"),
    ("inaccessible_paths_clear", "InaccessiblePaths", ""),
)


class DiagnosticFailure(RuntimeError):
    """Передать фиксированную категорию сбоя без исходных argv, env и диагностики."""


def bounded_process(argv: list[str], *, timeout: float = TIMEOUT) -> tuple[int, bytes, bytes]:
    """Прочитать stdout/stderr собственного процесса до лимита и общего deadline.

    Параметр argv создаётся только фиксированными внутренними builders, shell
    не используется. Возвращает exit code и два ограниченных byte-потока. При
    timeout/переполнении выбрасывает DiagnosticFailure и всегда убивает только
    собственную process group, закрывает pipes и собирает процесс. Наследуемое
    окружение заменяется фиксированными PATH/LANG; секреты root env не уходят
    в systemctl, systemd-run или их дочерние процессы.
    """
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True,
                               env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"})
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for name in outputs:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DiagnosticFailure("timeout")
                if not selector.get_map():
                    try:
                        process.wait(timeout=min(0.2, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                    continue
                for key, _ in selector.select(min(0.2, remaining)):
                    piece = os.read(key.fd, 4096)
                    if not piece:
                        selector.unregister(key.fileobj)
                        continue
                    output = outputs[key.data]
                    output.extend(piece)
                    if sum(len(value) for value in outputs.values()) > MAX_OUTPUT:
                        raise DiagnosticFailure("output_limit")
        return process.returncode, bytes(outputs["stdout"]), bytes(outputs["stderr"])
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for name in outputs:
            getattr(process, name).close()
        process.wait(timeout=1)


def read_properties() -> dict[str, str]:
    """Прочитать только allowlisted свойства одного фиксированного production-unit.

    Не читает Environment, EnvironmentFiles, auth HOME и содержимое конфигов.
    Возвращает dict после проверки точного набора имён, enum значений и списка
    address families. Незнакомые свойства/значения или сбой systemctl вызывают
    DiagnosticFailure; непроверенные строки никогда не становятся root-аргументом.
    Production-службу и её настройки функция не меняет.
    """
    argv = ["/usr/bin/systemctl", "show", SERVICE, "--no-pager", *["--property=" + key for key in PROPERTY_NAMES]]
    status, output, _ = bounded_process(argv, timeout=5)
    if status:
        raise DiagnosticFailure("service_properties_failed")
    properties = {}
    for line in output.decode("utf-8", errors="strict").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in PROPERTY_NAMES or key in properties:
            raise DiagnosticFailure("invalid_service_properties")
        properties[key] = value
    return validate_properties(properties)


def validate_properties(properties: object) -> dict[str, str]:
    """Проверить точный набор hardening metadata перед преобразованием в root argv.

    Принимает только dict с полным allowlist имён и строковыми enum значениями.
    Возвращает независимую копию; неизвестные поля, дубли и семейства адресов
    отклоняются DiagnosticFailure. Произвольные systemd свойства и ExecStart
    через этот builder не проходят. Сеть, ФС и service состояние не меняются.
    """
    if not isinstance(properties, dict) or any(not isinstance(value, str) for value in properties.values()):
        raise DiagnosticFailure("invalid_service_properties")
    if properties.keys() != set(PROPERTY_NAMES):
        raise DiagnosticFailure("incomplete_service_properties")
    for key, allowed in PROPERTY_VALUES.items():
        if properties[key] not in allowed:
            raise DiagnosticFailure("unsupported_service_properties")
    families = properties["RestrictAddressFamilies"].split()
    if not families or len(set(families)) != len(families) or not set(families) <= FAMILIES:
        raise DiagnosticFailure("unsupported_address_families")
    return dict(properties)


def validate_native(candidate: Path, package: Path = NATIVE_PACKAGE) -> Path:
    """Проверить executable и все его каталоги до корня перед root-запуском unit.

    Принимает путь из фиксированного npm glob и фиксированный package root.
    Возвращает canonical путь только внутри package, с обычным executable-файлом,
    root ownership и без group/world write для файла и всех его родителей.
    Выход symlink за package, чужой владелец или изменяемый каталог отклоняется
    DiagnosticFailure. Читает только metadata файлов; credentials не открывает.
    """
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(package)
    except ValueError:
        raise DiagnosticFailure("unsafe_native_path") from None
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        raise DiagnosticFailure("invalid_native_binary")
    for path in (resolved, *resolved.parents):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise DiagnosticFailure("unsafe_native_permissions")
    return resolved


def native_binary() -> Path:
    """Найти ровно один установленный native Codex в фиксированном системном package.

    Параметров нет. Ограниченный glob не обходит HOME или всю файловую систему.
    При нуле/нескольких кандидатах вызывает DiagnosticFailure; найденный путь
    проверяет validate_native. Содержимое бинарника и пользовательских конфигов
    не читает и системное состояние не изменяет.
    """
    candidates = list(NATIVE_PACKAGE.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
    if len(candidates) != 1:
        raise DiagnosticFailure("native_binary_not_unique")
    return validate_native(candidates[0])


def probe_command(unit: str, binary: Path, properties: dict[str, str], variant: tuple,
                  native_settings: dict[str, str] | None = None) -> list[str]:
    """Построить один фиксированный harmless transient-unit и одну разрешённую вариацию.

    Unit должен иметь собственный UUID-префикс; variant выбирается из VARIANTS,
    properties получены только read_properties. Возвращает argv systemd-run без
    shell и пользовательских аргументов. В harmless unit переносится исходный
    hardening и меняется максимум одно свойство; команда только printf через
    read-only sandbox. native_settings разрешает один synthetic worker review
    только при baseline hardening: он использует штатный auth HOME, фиксированные
    provider/timeout и три проверенных env ключа. Production-служба не меняется;
    никаких root credentials, чатов или произвольных команд в unit не передаётся.
    """
    if UNIT_NAME.fullmatch(unit) is None or variant not in VARIANTS or (native_settings is not None and variant != VARIANTS[0]):
        raise DiagnosticFailure("invalid_probe_scope")
    if native_settings is not None:
        native_settings = validate_settings(native_settings, binary)
    current = validate_properties(properties)
    current["InaccessiblePaths"] = "-/data -/mnt -/media -/srv"
    _, key, value = variant
    if key is not None:
        current[key] = value
    argv = ["/usr/bin/systemd-run", "--unit=" + unit, "--wait", "--pipe", "--collect", "--quiet", "--service-type=exec"]
    argv.extend("--property=" + key + "=" + value for key, value in current.items())
    argv.extend("--property=" + value for value in (
        "WorkingDirectory=" + STATE, "ReadWritePaths=" + STATE, "UMask=0077",
        "LockPersonality=yes",
        "RestrictRealtime=yes", "SystemCallArchitectures=native", "TasksMax=512",
        "RuntimeMaxSec=150s" if native_settings is not None else "RuntimeMaxSec=15s",
        "TimeoutStopSec=1s", "KillMode=control-group",
    ))
    argv.extend(("--setenv=HOME=" + STATE, "--setenv=PATH=/usr/local/bin:/usr/bin:/bin", "--setenv=LANG=C.UTF-8"))
    if native_settings is None:
        argv.extend(("--", str(binary), "sandbox", "-P", ":read-only", "/usr/bin/printf", MARKER + "\n"))
    else:
        argv.extend(("--setenv=PYTHONPATH=/opt/messenger-worksheets/app", "--setenv=WORKSHEET_CODEX_PROVIDER=sandbox_admin",
                     "--setenv=WORKSHEET_CODEX_TASK_TIMEOUT=120"))
        argv.extend("--setenv=" + key + "=" + value for key, value in native_settings.items())
        argv.extend(("--", INNER_PYTHON, str(INNER_SCRIPT), "--inner-native"))
    return argv


def stop_probe(unit: str) -> bool:
    """Остановить только собственный transient-unit после каждой диагностической попытки.

    Принимает UUID-имя с жёстким префиксом; production-unit и другие имена
    отклоняет до вызова systemctl. Возвращает признак успешного bounded stop;
    уже собранный --collect unit также безопасен. Ошибку cleanup не маскирует
    как доказательство успешной остановки; действующие worker не затрагивает.
    """
    if UNIT_NAME.fullmatch(unit) is None:
        raise DiagnosticFailure("invalid_cleanup_scope")
    try:
        status, _, stderr = bounded_process(["/usr/bin/systemctl", "stop", unit], timeout=3)
        return status == 0 or (status == 5 and b"not loaded" in stderr)
    except (DiagnosticFailure, OSError, subprocess.TimeoutExpired):
        return False


def safe_diagnostic(text: bytes) -> str:
    """Очистить только собственный synthetic stdout/stderr перед ограниченным выводом.

    Принимает byte-поток одной harmless попытки, не пользовательские ответы.
    Маскирует bearer, типовые ключи/JWT, значения secret-полей, URL userinfo,
    query и fragment. Возвращает максимум 1200 исходных символов с экранированными
    управляющими символами; сеть/ФС не используются. Bwrap error без секретов
    сохраняется буквально, чтобы диагноз опирался на точный kernel отказ.
    """
    redacted = text.decode("utf-8", errors="replace")
    redacted = re.sub(r"(?i)\bBearer\s+\S+|\b(?:sk-|gh[pousr]_|y[01]_|t[01]_|AQAD-)[A-Za-z0-9_.-]{12,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<masked>", redacted)
    redacted = re.sub(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+", r"\1<masked>", redacted)
    def clean_url(match):
        """Убрать credentials/query/fragment одной diagnostic URL без обращения к ней."""
        try:
            parsed = urlsplit(match[0])
            if parsed.username is not None or parsed.password is not None:
                return "<url-masked>"
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<query-masked>" if parsed.query else "", ""))
        except ValueError:
            return "<url-masked>"
    redacted = re.sub(r"https?://[^\s`<>]+", clean_url, redacted)
    return "".join(char if ord(char) >= 32 and ord(char) != 127 else f"\\x{ord(char):02x}" for char in redacted[:1200])


def probe(binary: Path, properties: dict[str, str], variant: tuple,
          native_settings: dict[str, str] | None = None) -> dict:
    """Запустить одну проверку и гарантировать stop только созданного transient-unit.

    Входы подготовлены фиксированными builders. Возвращает безопасные поля probe,
    status, marker_found, cleanup_ok и masked diagnostic. Timeout/переполнение
    превращаются в фиксированные категории. Вывод разрешён только собственной
    синтетической команды; ошибки root argv/metadata сюда не публикуются.
    """
    unit = "worksheet-sandbox-diag-" + uuid.uuid4().hex + ".service"
    result = {"probe": "native_worker_baseline" if native_settings is not None else variant[0], "status": "failed", "marker_found": False, "cleanup_ok": False, "diagnostic": ""}
    try:
        status, stdout, stderr = bounded_process(probe_command(unit, binary, properties, variant, native_settings),
                                                 timeout=140 if native_settings is not None else TIMEOUT)
        result.update(status="ok" if status == 0 else "exit_" + str(status),
                      marker_found=MARKER.encode() in stdout, diagnostic=safe_diagnostic(stderr or stdout.replace(MARKER.encode(), b"")))
        if native_settings is not None and status == 0:
            result["trace"] = validate_trace(json.loads(stdout))
            result["marker_found"] = result["trace"]["marker_found"]
            result["diagnostic"] = safe_diagnostic(stderr)
    except (ValueError, TypeError, RecursionError):
        result["status"] = "invalid_trace"
    except DiagnosticFailure as error:
        result["status"] = str(error)
    except (OSError, subprocess.TimeoutExpired):
        result["status"] = "execution_failed"
    finally:
        result["cleanup_ok"] = stop_probe(unit)
    return result



def validate_owned_file(path: Path) -> None:
    """Проверить root ownership и отсутствие group/world write системного helper-файла.

    Принимает только фиксированный путь Python/helper/env. Проверяет resolved
    файл и все его каталоги до корня; небезопасная metadata вызывает фиксированный
    DiagnosticFailure. Содержимое не открывает и системное состояние не меняет.
    """
    resolved = path.resolve(strict=True)
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise DiagnosticFailure("unsafe_helper_file")
    for current in (resolved, *resolved.parents):
        info = current.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise DiagnosticFailure("unsafe_helper_file")


def validate_settings(settings: object, binary: Path) -> dict[str, str]:
    """Проверить только три нечувствительных env ключа перед transient native phase.

    Параметры: settings — операторские binary/model/effort; binary уже проверен.
    Возвращает независимый dict только с этим executable и allowlisted строками.
    Секреты, provider URL, произвольные ключи/пути и config syntax отклоняются.
    ФС и сеть не читает; ошибка передаётся фиксированным DiagnosticFailure.
    """
    if (not isinstance(settings, dict) or not settings.keys() <= SETTING_KEYS
            or settings.get("WORKSHEET_CODEX_BINARY") != str(binary)
            or any(not isinstance(value, str) for value in settings.values())):
        raise DiagnosticFailure("invalid_native_settings")
    if "WORKSHEET_CODEX_MODEL" in settings and MODEL_NAME.fullmatch(settings["WORKSHEET_CODEX_MODEL"]) is None:
        raise DiagnosticFailure("invalid_native_settings")
    if "WORKSHEET_CODEX_REASONING_EFFORT" in settings and settings["WORKSHEET_CODEX_REASONING_EFFORT"] not in EFFORTS:
        raise DiagnosticFailure("invalid_native_settings")
    return dict(settings)


def read_settings(binary: Path) -> dict[str, str]:
    """Прочитать из фиксированного root env только binary, model и reasoning effort.

    Не исполняет файл и не загружает его целиком. Строки чужих ключей пропускает
    до разбора значений; API keys, transport/Messenger credentials не сохраняет
    и не передаёт ребёнку. Возвращает только validate_settings проекцию. Metadata,
    неполные значения и дубли ключей отклоняются без вывода содержимого env.
    """
    validate_owned_file(WORKER_ENV)
    settings = {}
    with WORKER_ENV.open(encoding="utf-8") as stream:
        for line in stream:
            if not any(line.startswith(key + "=") for key in SETTING_KEYS):
                continue
            key, _, value = line.rstrip("\n").partition("=")
            if key in settings:
                raise DiagnosticFailure("duplicate_native_setting")
            settings[key] = value
    return validate_settings(settings, binary)


def validate_trace(value: object) -> dict:
    """Очистить JSON собственной inner trace до фиксированных полей и лимитов.

    Принимает только synthetic trace, не пользовательские worker ответы.
    Возвращает marker bool, до восьми exit code/diagnostic, masked final text
    и фиксированную ошибку. Не пропускает команды, пути конфигов, raw event JSON,
    неизвестные поля и произвольные enum. Сеть и ФС не используются.
    """
    if (not isinstance(value, dict) or value.keys() != {"marker_found", "commands", "final_reply", "error"}
            or type(value["marker_found"]) is not bool or not isinstance(value["commands"], list)
            or len(value["commands"]) > 8 or not isinstance(value["final_reply"], str)
            or value["error"] not in (None, "execution_failed")):
        raise DiagnosticFailure("invalid_trace")
    commands = []
    for item in value["commands"]:
        if (not isinstance(item, dict) or item.keys() != {"exit_code", "diagnostic"}
                or (item["exit_code"] is not None and type(item["exit_code"]) is not int)
                or not isinstance(item["diagnostic"], str)):
            raise DiagnosticFailure("invalid_trace")
        commands.append({"exit_code": item["exit_code"], "diagnostic": safe_diagnostic(item["diagnostic"].encode())})
    return {"marker_found": value["marker_found"], "commands": commands,
            "final_reply": safe_diagnostic(value["final_reply"].encode()), "error": value["error"]}


def inner_native() -> None:
    """Выполнить один synthetic /review под UID worker и исходным hardening.

    Вызывается только после UID guard в main. Импортирует штатный worker,
    создаёт приватную временную fixture в его state, запускает настоящий native
    execute с task timeout 120 секунд. Перехватывает только собственные completed
    command_execution события: сохраняет exit code и masked output, не команду
    или raw JSON. В finally восстанавливает функцию worker. Печатает только
    ограниченную validate_trace проекцию; model/runtime ошибки скрыты категорией.
    """
    from messenger_worksheets import codex_worker as worker

    os.umask(0o077)
    os.environ["WORKSHEET_CODEX_PROVIDER"] = "sandbox_admin"
    os.environ["WORKSHEET_CODEX_TASK_TIMEOUT"] = "120"
    marker = "READONLY_OK_" + uuid.uuid4().hex
    trace = {"marker_found": False, "commands": [], "final_reply": "", "error": None}
    original = worker.run_codex_process
    def capture(*args, **kwargs):
        """Собрать только exit code и masked output собственной synthetic command event."""
        result = original(*args, **kwargs)
        for line in result.stdout.splitlines():
            if len(trace["commands"]) >= 8 or len(line) > 2 * 1024 * 1024:
                continue
            try:
                event = json.loads(line)
                item = event.get("item") if isinstance(event, dict) else None
                if (event.get("type") != "item.completed" or not isinstance(item, dict)
                        or item.get("type") != "command_execution"):
                    continue
                output = item.get("aggregated_output", "")
                code = item.get("exit_code")
                trace["commands"].append({"exit_code": code if type(code) is int else None,
                                          "diagnostic": safe_diagnostic(output.encode()) if isinstance(output, str) else ""})
            except (ValueError, TypeError, AttributeError, RecursionError):
                continue
        return result
    worker.run_codex_process = capture
    try:
        source = ("# " + marker + "\ndef ratio(x):\n    return x / 0\n").encode()
        request = worker.Request.decode({
            "text": "Инструментом команд прочитай input/history/check.py. Укажи дефект и точное слово READONLY_OK с полным суффиксом первой строки. Ничего не исправляй.",
            "task": True, "session": None, "images": [], "context": [], "options": {"mode": "review"},
            "history_files": [{"name": "check.py", "original_name": "check.py", "data": base64.b64encode(source).decode("ascii")}],
        })
        with TemporaryDirectory(prefix="sandbox-diag-", dir=STATE) as directory:
            result = worker.execute(request, Path(directory))
        trace["marker_found"] = marker in result["text"]
        trace["final_reply"] = safe_diagnostic(result["text"].encode())
    except Exception:
        trace["error"] = "execution_failed"
    finally:
        worker.run_codex_process = original
    print(json.dumps(validate_trace(trace), ensure_ascii=False), flush=True)

def main() -> None:
    """Выполнить восемь фиксированных read-only sandbox проб только из root-helper.

    Root phase не принимает аргументов; non-root не может читать service metadata.
    Единственный --inner-native допускается только для точного UID worker до его
    импорта/auth и выполняет собственный synthetic review с исходным hardening.
    Каждая строка stdout — безопасный JSON своей попытки, не env или чаты.
    Production-unit, AppArmor, sysctl и конфиги не изменяются; собственные fixture
    временные, transient-unit ограничены временем и очищаются отдельно. Общая
    ошибка даёт фиксированную категорию без traceback, paths или credentials.
    """
    if sys.argv[1:] == ["--inner-native"]:
        if os.geteuid() == 0 or os.geteuid() != pwd.getpwnam(ACCOUNT).pw_uid:
            raise SystemExit("sandbox_diagnostics=invalid_inner_uid")
        inner_native()
        return
    if os.geteuid() != 0 or len(sys.argv) != 1:
        raise SystemExit("sandbox_diagnostics=requires_fixed_root_helper")
    try:
        binary, properties = native_binary(), read_properties()
        for variant in VARIANTS:
            result = probe(binary, properties, variant)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if not result["cleanup_ok"]:
                raise DiagnosticFailure("cleanup_unconfirmed")
        settings = read_settings(binary)
        validate_owned_file(INNER_SCRIPT)
        validate_owned_file(Path(INNER_PYTHON))
        result = probe(binary, properties, VARIANTS[0], settings)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if not result["cleanup_ok"]:
            raise DiagnosticFailure("cleanup_unconfirmed")
    except DiagnosticFailure as error:
        raise SystemExit("sandbox_diagnostics=" + str(error)) from None
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise SystemExit("sandbox_diagnostics=execution_failed") from None


if __name__ == "__main__":
    main()
