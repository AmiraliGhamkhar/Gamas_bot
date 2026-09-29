"""Single-instance guard for the bot process.

Two copies of the bot sharing one Telegram session and one SQLite file would
both answer every message, corrupt the session, and delete each other's
temporary job folders at startup.  That is an easy mistake on shared hosting
where a cron watchdog, a web-triggered launcher and an operator's SSH shell can
all start the bot.

The guard is an advisory OS file lock.  The kernel drops it when the process
exits for *any* reason (including ``kill -9`` or an out-of-memory kill), so a
crash can never leave a stale lock behind, unlike a PID file.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt


class AlreadyRunningError(RuntimeError):
    """Another process already holds the instance lock."""


class InstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - Windows
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunningError(
                f"another Gamas Bot instance holds the lock {self.path}"
            ) from exc
        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass  # The PID is informational only.

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def is_locked(path: Path) -> bool:
    """True when a live process currently holds the lock (never blocks)."""
    probe = InstanceLock(path)
    try:
        probe.acquire()
    except AlreadyRunningError:
        return True
    except OSError:
        return False
    probe.release()
    return False
