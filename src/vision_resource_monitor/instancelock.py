"""A best-effort single-instance guard via an OS-released file lock.

Mirrors the resource monitor's ``LogWriterLock``: the OS drops ``flock`` /
``msvcrt`` locks when the holder dies, so a crash never leaves a stale claim.
Environments where no lock can be taken fail *open* (the app still runs) — a
second tray icon is a cosmetic annoyance, never a reason to refuse to start.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl

    def _lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
except ImportError:  # Windows
    try:
        import msvcrt

        def _lock(handle) -> bool:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
    except ImportError:
        _lock = None


class SingleInstance:
    def __init__(self, lock_path):
        self.lock_path = Path(lock_path)
        self._handle = None

    def acquire(self) -> bool:
        """True if we may run (lock taken, or locking impossible here)."""
        if _lock is None:
            return True
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.lock_path, "a+")
        except OSError:
            return True
        if not _lock(handle):
            handle.close()
            return False
        self._handle = handle
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except OSError:
            pass
        return True

    def release(self):
        if self._handle is not None:
            self._handle.close()
            self._handle = None
