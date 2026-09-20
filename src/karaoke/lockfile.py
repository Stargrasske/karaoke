"""Cross-process lockfile utilities for platform pipeline operations.

Uses an OS-level file lock — ``fcntl.flock`` on POSIX (Linux/macOS), or
``msvcrt.locking`` on Windows (``fcntl`` has no Windows build at all) — so
operations like vector index rebuilds, audio backfills, and folder scans
cannot be run concurrently across processes, on either platform.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from .config import settings
from .logger import log

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl

# msvcrt.locking locks a byte range rather than the whole file (like flock);
# one byte is enough to use the file purely as a mutex.
_WIN_LOCK_BYTES = 1


class ProcessLock:
    """A cross-process lock backed by an OS-level lock on a lockfile."""

    def __init__(self, name: str):
        self.name = name
        self._lock_dir = Path(settings.data_dir) / "locks"
        self._lock_file = self._lock_dir / f"{name}.lock"
        self._fd: Optional[int] = None

    def acquire(self, *, blocking: bool = False) -> bool:
        """Acquire the lockfile. Returns True if acquired, False if held by another process."""
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self._lock_file), os.O_CREAT | os.O_RDWR)
            if IS_WINDOWS:
                # A locked region must be within the file, so ensure at least
                # one byte exists before locking it.
                if os.fstat(fd).st_size < _WIN_LOCK_BYTES:
                    os.write(fd, b"\0" * _WIN_LOCK_BYTES)
                os.lseek(fd, 0, os.SEEK_SET)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(fd, mode, _WIN_LOCK_BYTES)
            else:
                flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, flags)
            self._fd = fd
            return True
        except (BlockingIOError, OSError):
            log.info("ProcessLock: %s lock held by another process", self.name)
            return False

    def release(self) -> None:
        """Release the lockfile."""
        if self._fd is not None:
            try:
                if IS_WINDOWS:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, _WIN_LOCK_BYTES)
                else:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            finally:
                self._fd = None

    def is_locked(self) -> bool:
        """Check if the lock is held by another process without acquiring it."""
        if self._fd is not None:
            return True
        if not self._lock_file.exists():
            return False
        try:
            fd = os.open(str(self._lock_file), os.O_RDWR)
            try:
                if IS_WINDOWS:
                    if os.fstat(fd).st_size < _WIN_LOCK_BYTES:
                        os.write(fd, b"\0" * _WIN_LOCK_BYTES)
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, _WIN_LOCK_BYTES)
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, _WIN_LOCK_BYTES)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            except (BlockingIOError, OSError):
                return True
            finally:
                os.close(fd)
        except OSError:
            return False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
