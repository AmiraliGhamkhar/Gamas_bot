"""Single-instance guard for the bot process.

Two copies of the bot sharing one Telegram session and one SQLite file would
both answer every message, corrupt the session, and delete each other's
temporary job folders at startup. That is an easy mistake on shared hosting
where a cron watchdog, a web-triggered launcher and an operator's SSH shell can
all start the bot.

The guard is an OS file lock. The kernel drops it when the process exits for
*any* reason (including ``kill -9`` or an out-of-memory kill), so a crash can
never leave a stale lock behind, unlike a PID file.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

#: On Windows, ``msvcrt.locking`` places a *mandatory* byte-range lock starting
#: at the current file descriptor offset. Locking a high offset past the short
#: informational PID text (bytes 0..31) both keeps ``acquire``/``release``
#: symmetric across ``os.write`` and allows status/stop tools to read the PID
#: without hitting ``ERROR_LOCK_VIOLATION``.
_WINDOWS_LOCK_OFFSET = 0x10000


class AlreadyRunningError(RuntimeError):
    """Another process already holds the instance lock."""


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "posix":
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - Windows
                os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunningError(
                f"another Gamas Bot instance holds the lock {self.path}"
            ) from exc
        self._fd = fd
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
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
                os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc_info: object) -> None:
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


def read_lock_pid(path: Path) -> int | None:
    """Return the informational PID recorded in ``path``, or ``None``."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw:
        return None
    first_token = raw.splitlines()[0].strip()
    try:
        pid = int(first_token)
    except ValueError:
        return None
    return pid if pid > 0 else None
