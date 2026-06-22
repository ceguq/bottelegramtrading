"""
Run the Telegram listener and breakeven monitor from one terminal.

This runner starts both long-running bot processes with the active Python
interpreter, prefixes their output, and shuts both down together.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
STOP_TIMEOUT_SECONDS = 10
STACK_LOCK_PATH = BASE_DIR / "run_bot.stack.lock"
STACK_RUNNING_MESSAGE = (
    "Another BOT_TRADING_TELEGRAM bot stack is already running. "
    "Stop it before starting a new one."
)


if os.name == "nt":
    import msvcrt
else:
    import fcntl


class _SingleInstanceGuard:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.lock_file = None
        self.locked = False

    def __enter__(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = self.lock_path.open("a+b")
        self.lock_file.seek(0, os.SEEK_END)
        if self.lock_file.tell() == 0:
            self.lock_file.write(b"\0")
            self.lock_file.flush()

        self.lock_file.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locked = True
        except OSError:
            self.lock_file.close()
            self.lock_file = None
            self.locked = False

        return self.locked

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.lock_file is None:
            return

        try:
            self.lock_file.seek(0)
            if self.locked:
                if os.name == "nt":
                    msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self.lock_file.close()
            self.lock_file = None
            self.locked = False


def _json_process_rows(output: str) -> list[dict]:
    output = output.strip()
    if not output:
        return []

    data = json.loads(output)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _other_project_run_bot_processes() -> list[dict]:
    if os.name != "nt":
        return []

    command = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'python' -and "
        "$_.CommandLine -match 'BOT_TRADING_TELEGRAM|run_bot.py|telegram_listener.py|be_monitor.py' } | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
    )

    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except Exception:
        return []

    if completed.returncode != 0:
        return []

    try:
        rows = _json_process_rows(completed.stdout)
    except json.JSONDecodeError:
        return []

    current_pid = os.getpid()
    current_parent_pid = os.getppid()
    project_marker = str(BASE_DIR).lower()
    project_marker_alt = project_marker.replace("\\", "/")

    def _pid(row, key):
        try:
            return int(row.get(key))
        except (TypeError, ValueError):
            return None

    def _cmd(row) -> str:
        return str(row.get("CommandLine") or "").lower()

    project_parent_pids = set()
    for row in rows:
        cmdline = _cmd(row)
        if project_marker in cmdline or project_marker_alt in cmdline or "bot_trading_telegram" in cmdline:
            parent_pid = _pid(row, "ParentProcessId")
            if parent_pid is not None:
                project_parent_pids.add(parent_pid)

    matches = []
    for row in rows:
        pid = _pid(row, "ProcessId")
        if pid is None or pid in {current_pid, current_parent_pid}:
            continue

        cmdline = _cmd(row)
        if "run_bot.py" not in cmdline:
            continue

        is_project_process = (
            project_marker in cmdline
            or project_marker_alt in cmdline
            or "bot_trading_telegram" in cmdline
            or pid in project_parent_pids
        )
        if is_project_process:
            matches.append(row)

    return matches


def _creationflags() -> int:
    if os.name == "nt":
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def _start_process(script_name: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    return subprocess.Popen(
        [sys.executable, "-u", str(BASE_DIR / script_name)],
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=_creationflags(),
    )


def _pump_output(prefix: str, process: subprocess.Popen, print_lock: threading.Lock) -> None:
    if process.stdout is None:
        return

    try:
        for line in process.stdout:
            with print_lock:
                print(f"{prefix} {line}", end="", flush=True)
    finally:
        process.stdout.close()


def _request_stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            os.kill(process.pid, signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()


def _force_stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


def _stop_all(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        _request_stop(process)

    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if all(process.poll() is not None for process in processes):
            return
        time.sleep(0.2)

    for process in processes:
        _force_stop(process)


def main() -> int:
    if _other_project_run_bot_processes():
        print(STACK_RUNNING_MESSAGE, flush=True)
        return 1

    with _SingleInstanceGuard(STACK_LOCK_PATH) as lock_acquired:
        if not lock_acquired:
            print(STACK_RUNNING_MESSAGE, flush=True)
            return 1

        print_lock = threading.Lock()
        telegram = _start_process("telegram_listener.py")
        be_monitor = _start_process("be_monitor.py")
        processes = [telegram, be_monitor]

        threads = [
            threading.Thread(
                target=_pump_output,
                args=("[TELEGRAM]", telegram, print_lock),
                daemon=True,
            ),
            threading.Thread(
                target=_pump_output,
                args=("[BE]", be_monitor, print_lock),
                daemon=True,
            ),
        ]

        for thread in threads:
            thread.start()

        try:
            while True:
                for name, process in (("telegram_listener.py", telegram), ("be_monitor.py", be_monitor)):
                    return_code = process.poll()
                    if return_code is not None:
                        with print_lock:
                            print(
                                f"[RUNNER] {name} exited with code {return_code}; stopping the other process.",
                                flush=True,
                            )
                        _stop_all(processes)
                        return return_code if return_code != 0 else 1

                time.sleep(0.5)

        except KeyboardInterrupt:
            with print_lock:
                print("\n[RUNNER] Ctrl+C received; stopping child processes.", flush=True)
            _stop_all(processes)
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
