"""Start the bot if — and only if — it is not already running.

cPanel shared hosting has no systemd.  The bot must therefore be (re)started by
something that *is* available: a once-a-minute cron job
(``python -m gamas_bot.launcher``) and/or a Passenger web request
(``passenger_wsgi.py``).  Both call :func:`ensure_running`.

The check imports only the configuration and the lock helper — not Telethon,
PyAV or python-docx — so a per-minute cron run costs a few tens of
milliseconds instead of a full application start-up.  The bot itself still
enforces single-instance with the same lock, so a race between two launchers
can never produce two bots.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from .config import PROJECT_DIR, from_env
from .instance_lock import AlreadyRunningError, InstanceLock, is_locked

RUNNING = "running"
STARTED = "started"
THROTTLED = "throttled"
MISCONFIGURED = "misconfigured"

#: Minimum seconds between two spawn attempts. A bot that dies during start-up
#: (bad token, no network) must not be relaunched on every web hit.
MIN_SPAWN_INTERVAL_SECONDS = 45


def ensure_running(*, min_interval: float = MIN_SPAWN_INTERVAL_SECONDS) -> str:
    """Return ``running``, ``started``, ``throttled`` or ``misconfigured``.

    A separate launcher lock serializes watchdogs from cron and Passenger. The
    bot's own session lock remains authoritative after spawn, but this lock
    avoids starting redundant Python interpreters while the winner is booting.
    """
    try:
        settings = from_env()
    except ValueError as exc:
        print(f"gamas-bot launcher: configuration error: {exc}", file=sys.stderr)
        return MISCONFIGURED
    if not settings.bot_token or settings.api_id <= 0 or not settings.api_hash:
        print("gamas-bot launcher: TELEGRAM_BOT_TOKEN/API_ID/API_HASH missing", file=sys.stderr)
        return MISCONFIGURED

    data_dir = settings.session_path.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    instance_lock_path = settings.session_path.with_suffix(".lock")
    launcher_lock = InstanceLock(data_dir / "launcher.lock")
    try:
        launcher_lock.acquire()
    except AlreadyRunningError:
        return RUNNING if is_locked(instance_lock_path) else THROTTLED
    try:
        # The process lock is checked only while this launcher's lock is held,
        # so no competing watchdog can pass the same check and spawn alongside.
        if is_locked(instance_lock_path):
            return RUNNING

        stamp = data_dir / "launcher.stamp"
        try:
            if time.time() - stamp.stat().st_mtime < min_interval:
                return THROTTLED
        except FileNotFoundError:
            pass

        # Recheck immediately before updating the throttle stamp and spawning.
        if is_locked(instance_lock_path):
            return RUNNING
        try:
            tmp_stamp = data_dir / f".launcher.stamp.{os.getpid()}.tmp"
            tmp_stamp.write_text(str(time.time()), encoding="utf-8")
            os.chmod(tmp_stamp, 0o600)
            tmp_stamp.replace(stamp)
            os.chmod(stamp, 0o600)
        except OSError:
            try:
                stamp.touch()
            except OSError:
                pass
        # A manually started bot does not use launcher.lock; make one final
        # session-lock check after the stamp update before creating a child.
        if is_locked(instance_lock_path):
            return RUNNING

        env = os.environ.copy()
        if settings.log_file is None:
            # stdout is discarded below, so without a log file a start-up
            # failure would leave no trace at all.
            env["LOG_FILE"] = str(log_dir / "bot.log")
        error_log = log_dir / "launcher.err"
        with open(error_log, "wb") as err:
            os.chmod(error_log, 0o600)
            subprocess.Popen(  # noqa: S603 - fixed argument vector, no shell
                [sys.executable, "-m", "gamas_bot"],
                cwd=PROJECT_DIR,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err,
                start_new_session=(os.name == "posix"),
                close_fds=True,
            )
        return STARTED
    finally:
        launcher_lock.release()


def main() -> int:
    if hasattr(os, "umask"):
        os.umask(0o077)  # logs, lock and stamp may sit next to a chat transcript database
    status = ensure_running()
    print(status)
    return 2 if status == MISCONFIGURED else 0


if __name__ == "__main__":
    raise SystemExit(main())
