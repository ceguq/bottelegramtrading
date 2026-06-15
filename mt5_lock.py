"""
Cross-process lock for serialized MT5 access.

MetaTrader5's Python bridge is sensitive to concurrent initialize/login/use
calls from separate processes. Use mt5_process_lock() around each complete
connect/use/disconnect sequence.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


LOCK_PATH = Path(__file__).resolve().parent / "active_orders.mt5.lock"
LOCK_POLL_SECONDS = 0.1


if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _open_lock_file():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("a+b")
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    return lock_file


def _try_lock(lock_file) -> bool:
    lock_file.seek(0)
    try:
        if os.name == "nt":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(lock_file) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def mt5_process_lock(timeout: float = 30):
    """Acquire the shared MT5 process lock or raise TimeoutError."""
    deadline = time.monotonic() + timeout
    lock_file = _open_lock_file()
    locked = False

    try:
        while time.monotonic() < deadline:
            if _try_lock(lock_file):
                locked = True
                break
            time.sleep(LOCK_POLL_SECONDS)

        if not locked:
            raise TimeoutError(f"Timed out waiting for MT5 lock: {LOCK_PATH}")

        yield

    finally:
        if locked:
            _unlock(lock_file)
        lock_file.close()
