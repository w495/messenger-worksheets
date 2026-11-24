"""Остановить потомков одного native job, включая PTY и double-fork процессы."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


CLEANUP_FAILED = 125
CANCELLED = 143


def enable_subreaper() -> None:
    """Сделать текущий Linux-процесс приёмником осиротевших потомков job.

    Не принимает параметров. PR_SET_CHILD_SUBREAPER не меняет другие jobs и
    не требует root. При недоступности Linux prctl выбрасывает OSError до
    запуска native процесса: без контроля orphan-потомков job не выполняется.
    """
    if not sys.platform.startswith("linux"):
        raise OSError("job supervision requires Linux")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise OSError("job supervision requires pidfd")
    descriptor = os.pidfd_open(os.getpid())
    os.close(descriptor)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enable job subreaper")


def process_identity(pid: int) -> tuple[int, int, str] | None:
    """Прочитать PPID, стартовые ticks и состояние процесса без argv или env.

    Параметр pid — числовой PID. Возвращает None для исчезнувшего либо
    недоступного процесса. Стартовые ticks используются перед каждым signal,
    чтобы повторное использование PID не затронуло чужую задачу. Не читает
    командную строку, credentials или пользовательские файлы.
    """
    try:
        parts = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return int(parts[1]), int(parts[19]), parts[0]
    except (OSError, ValueError, IndexError):
        return None


def descendants() -> dict[int, int]:
    """Получить PID/start-ticks только живого дерева данного supervisor.

    Сканирует ограниченный снимок /proc, читая только stat-метаданные, и
    вычисляет транзитивных потомков собственного PID. Возвращает словарь
    идентичностей; orphan-потомки после double-fork усыновляются subreaper.
    Слишком большой снимок вызывает RuntimeError вместо частичной очистки.
    """
    children: dict[int, list[tuple[int, int]]] = {}
    with os.scandir("/proc") as entries:
        count = 0
        for entry in entries:
            if not entry.name.isascii() or not entry.name.isdecimal():
                continue
            count += 1
            if count > 131072:
                raise RuntimeError("process snapshot limit exceeded")
            pid = int(entry.name)
            info = process_identity(pid)
            if info is not None:
                parent, ticks, _ = info
                children.setdefault(parent, []).append((pid, ticks))
    result = {}
    pending = [os.getpid()]
    while pending:
        for pid, ticks in children.get(pending.pop(), ()):
            if pid not in result:
                result[pid] = ticks
                pending.append(pid)
    return result


def signal_owned(pid: int, ticks: int, number: int) -> None:
    """Послать signal ранее обнаруженному потомку после проверки PID reuse.

    Параметры: pid/start-ticks принадлежат снимку descendants, number — номер
    сигнала. Исчезнувшие процессы пропускаются. Ошибки прав передаются, чтобы
    supervisor не выдал успешную остановку при невозможности очистки. Общие
    process groups и service cgroups метод не затрагивает.
    """
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        current = process_identity(pid)
        if current is not None and current[1] == ticks:
            # pidfd фиксирует процесс до повторной проверки start-ticks:
            # исчезновение PID и его reuse после проверки не меняют адресата.
            signal.pidfd_send_signal(descriptor, number)
    except ProcessLookupError:
        pass
    finally:
        os.close(descriptor)


def reap_children() -> dict[int, int]:
    """Собрать уже завершившихся непосредственных и усыновлённых потомков.

    Возвращает словарь PID/exit-code, не блокируя живые процессы. Усыновлённые
    orphan-процессы также waitpid-ятся этим supervisor, поэтому zombie не
    остаются worker. Отсутствие детей является нормальным пустым результатом.
    """
    result = {}
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return result
        if pid == 0:
            return result
        result[pid] = os.waitstatus_to_exitcode(status)


def children_remaining() -> bool:
    """Проверить kernel-список детей, собирая zombie и не доверяя одному /proc снимку.

    Возвращает True, если waitpid видит хотя бы одного живого ребёнка; False
    возможен только после ECHILD. Это закрывает гонку /proc между double-fork,
    reparent и чтением метаданных: пустое дерево снимка не доказывает, что
    supervisor уже усыновил и остановил всех потомков.
    """
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return False
        if pid == 0:
            return True


def cleanup_descendants() -> bool:
    """Ограниченно завершить только потомков job с TERM, затем STOP/KILL.

    Сначала даёт 150 мс на обычное завершение. Затем замораживает обнаруженное
    дерево, повторяя снимки для потомков, созданных до SIGSTOP, и уничтожает
    его с проверкой PID/start-ticks. Новые orphan-потомки подбираются subreaper.
    За два секунды должен остаться пустой набор детей; иначе возвращает False,
    не позволяя объявить отмену подтверждённой. Не убивает общий worker/cgroup.
    """
    deadline = time.monotonic() + 2.0
    graceful = time.monotonic() + 0.15
    owned = descendants()
    for pid, ticks in owned.items():
        signal_owned(pid, ticks, signal.SIGTERM)
    while time.monotonic() < graceful:
        reap_children()
        if not descendants() and not children_remaining():
            return True
        time.sleep(0.01)
    while time.monotonic() < deadline:
        reap_children()
        current = descendants()
        if not current and not children_remaining():
            return True
        # STOP не даёт живому потомку продолжать fork, пока строится следующий
        # снимок. KILL затем действует и на остановленные процессы.
        for pid, ticks in current.items():
            signal_owned(pid, ticks, signal.SIGSTOP)
        for pid, ticks in descendants().items():
            signal_owned(pid, ticks, signal.SIGSTOP)
            current[pid] = ticks
        for pid, ticks in current.items():
            signal_owned(pid, ticks, signal.SIGKILL)
        time.sleep(0.01)
    reap_children()
    return not descendants() and not children_remaining()


def supervise(argv: list[str]) -> int:
    """Исполнить один native argv и очистить всех потомков до возврата exit code.

    Параметр argv передаётся Popen без shell; stdin/stdout/stderr наследуются
    без изменения JSON-потока Codex. SIGTERM/SIGINT только запрашивают cleanup
    supervisor. Успешно очищенная отмена возвращает 143, невозможность очистки
    — 125. При нормальном завершении возвращает код native процесса после
    уборки его оставшихся потомков, включая отдельные sessions и double-fork.
    """
    enable_subreaper()
    stopping = False

    def request_stop(number, frame):
        """Отметить остановку без выхода из signal handler и потери orphan-детей."""
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    process = None
    outcome = CLEANUP_FAILED
    try:
        if not stopping:
            process = subprocess.Popen(argv)
        while process is not None and not stopping:
            outcomes = reap_children()
            if process.pid in outcomes:
                outcome = outcomes[process.pid]
                process.returncode = outcome
                break
            time.sleep(0.02)
        if stopping:
            outcome = CANCELLED
    finally:
        if not cleanup_descendants():
            outcome = CLEANUP_FAILED
    return outcome if outcome >= 0 else 128 - outcome


def main() -> None:
    """Обслужить внутренний wrapper ``-- native argv`` без вывода диагностики.

    Некорректный вызов, недоступный subreaper или ошибка очистки дают exit125.
    Native argv и его env не печатаются; stdout принадлежит исключительно
    выполняемому Codex, что сохраняет прежний JSON-протокол worker.
    """
    try:
        if len(sys.argv) < 3 or sys.argv[1] != "--":
            raise ValueError("invalid supervisor invocation")
        outcome = supervise(sys.argv[2:])
    except BaseException:
        outcome = CLEANUP_FAILED
    sys.exit(outcome)


if __name__ == "__main__":
    main()
