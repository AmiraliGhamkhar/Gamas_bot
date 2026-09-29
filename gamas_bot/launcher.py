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

from .config import PROJECT_ROOT, Settings
from .instance_lock import is_locked

RUNNING = "running"
STARTED = "started"
THROTTLED = "throttled"
MISCONFIGURED = "misconfigured"

#: Minimum seconds between two spawn attempts. A bot that dies during start-up
#: (bad token, no network) must not be relaunched on every web hit.
MIN_SPAWN_INTERVAL_SECONDS = 45


def ensure_running(*, min_interval: float = MIN_SPAWN_INTERVAL_SECONDS) -> str:
    """Return ``running``, ``started``, ``throttled`` or ``misconfigured``."""
    try:
        settings = Settings.from_env()
        settings.validate_runtime()
    except ValueError as exc:
        print(f"gamas-bot launcher: configuration error: {exc}", file=sys.stderr)
        return MISCONFIGURED
    if is_locked(settings.lock_path):
        return RUNNING

    data_dir = settings.session_path.parent
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = data_dir / "launcher.stamp"
    try:
        if time.time() - stamp.stat().st_mtime < min_interval:
            return THROTTLED
    except FileNotFoundError:
        pass
    stamp.touch()

    env = os.environ.copy()
    if settings.log_file is None:
        # stdout is discarded below, so without a log file a start-up failure
        # would leave no trace at all. The file is size-rotated by the bot.
        env["LOG_FILE"] = str(log_dir / "bot.log")
    # stderr is truncated on every launch: it only ever needs to explain why
    # the *latest* start failed before logging was configured.
    with open(log_dir / "launcher.err", "wb") as err:
        subprocess.Popen(  # noqa: S603 - fixed argument vector, no shell
            [sys.executable, "-m", "gamas_bot"],
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=(os.name == "posix"),
            close_fds=True,
        )
    return STARTED


def main() -> int:
    if hasattr(os, "umask"):
        os.umask(0o077)  # logs, lock and stamp may sit next to a chat transcript database
    status = ensure_running()
    print(status)
    return 2 if status == MISCONFIGURED else 0


if __name__ == "__main__":
    raise SystemExit(main())
