"""
Run the Telegram listener and breakeven monitor from one terminal.

This runner starts both long-running bot processes with the active Python
interpreter, prefixes their output, and shuts both down together.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
STOP_TIMEOUT_SECONDS = 10


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
