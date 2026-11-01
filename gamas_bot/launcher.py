"""Bot process lifecycle manager: idempotent watchdog start, status, stop, and restart.

cPanel shared hosting has no systemd. The bot is therefore started and kept
alive by a once-a-minute cron job (``python scripts/ensure_running.py`` or
``python -m gamas_bot.launcher``) and/or a Passenger web request
(``passenger_wsgi.py``). Operators on Windows or Linux can also invoke
``start``, ``status``, ``stop``, or ``restart`` directly.

Design guarantees:

* Lightweight imports only (config + file lock + secret redactor — never
  Telethon, PyAV, or python-docx), keeping a per-minute cron probe to a few
  tens of milliseconds.
* Full configuration validation (:func:`gamas_bot.config.check`) runs *before*
  spawning a child, and every diagnostic is written with a UTC timestamp to
  ``data/logs/launcher.err`` (bounded at 1 MiB, ``0600``) as well as
  ``stderr`` so ``>/dev/null 2>&1`` cron jobs never swallow startup failures.
* Real child processes are briefly polled until they acquire the session lock
  or exit, preventing false ``started`` reports on immediate startup crashes.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .config import PROJECT_DIR, Settings, check, from_env, is_advisory
from .instance_lock import AlreadyRunningError, InstanceLock, is_locked, read_lock_pid
from .logging_config import collect_configured_secrets, redact_log_secrets

RUNNING = "running"
STARTED = "started"
THROTTLED = "throttled"
MISCONFIGURED = "misconfigured"
FAILED = "failed"
STOPPED = "stopped"

#: Minimum seconds between two spawn attempts. A bot that dies during start-up
#: (bad network, transient upstream failure) must not be relaunched on every hit.
MIN_SPAWN_INTERVAL_SECONDS = 45.0

#: Bounded size for ``data/logs/launcher.err`` before single-file rollover (R6).
MAX_LAUNCHER_ERR_BYTES = 1 * 1024 * 1024

#: How long ``ensure_running`` waits to observe either session-lock acquisition
#: or an immediate startup exit from a freshly spawned child process.
DEFAULT_STARTUP_VERIFY_SECONDS = 2.0
_STARTUP_POLL_STEP_SECONDS = 0.05

#: Graceful stop wait ceiling before reporting a timeout.
DEFAULT_STOP_TIMEOUT_SECONDS = 30.0


def _ensure_private_dir(path: Path) -> None:
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created and os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def _rotate_launcher_err_if_needed(error_log: Path) -> None:
    try:
        if error_log.is_file() and error_log.stat().st_size >= MAX_LAUNCHER_ERR_BYTES:
            backup = error_log.with_name(f"{error_log.name}.1")
            error_log.replace(backup)
            if os.name == "posix":
                os.chmod(backup, 0o600)
    except OSError:
        pass


def _record_launcher_diagnostic(
    log_dir: Path,
    message: str,
    *,
    settings: Settings | None = None,
    also_stderr: bool = True,
) -> None:
    """Persist a redacted, timestamped launcher message to ``launcher.err``."""
    secrets = collect_configured_secrets(settings)
    clean = redact_log_secrets(message.strip(), secrets)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{stamp}] gamas-bot launcher: {clean}\n"
    if also_stderr and sys.stderr is not None:
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except OSError:
            pass
    try:
        _ensure_private_dir(log_dir)
        error_log = log_dir / "launcher.err"
        _rotate_launcher_err_if_needed(error_log)
        fd = os.open(error_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.name == "posix":
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            os.write(fd, line.encode("utf-8", errors="replace"))
        finally:
            os.close(fd)
    except OSError:
        pass


def _default_log_dir(settings: Settings | None = None) -> Path:
    if settings is not None:
        return settings.session_path.parent / "logs"
    raw_session = os.environ.get("TELEGRAM_SESSION", "").strip() or "data/gamas"
    session_path = Path(raw_session).expanduser()
    if not session_path.is_absolute():
        session_path = PROJECT_DIR / session_path
    return session_path.parent / "logs"


def _windows_detach_flags() -> int:
    if os.name != "nt":
        return 0
    return int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def _verify_child_startup(
    proc: object,
    instance_lock_path: Path,
    *,
    timeout: float,
) -> tuple[str, int | None]:
    """Wait briefly for ``proc`` to take ``instance_lock_path`` or exit early."""
    poll = getattr(proc, "poll", None)
    if not callable(poll):
        return STARTED, None
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        rc = poll()
        if rc is not None:
            if rc == 3 or is_locked(instance_lock_path):
                return RUNNING, int(rc)
            return FAILED, int(rc)
        if is_locked(instance_lock_path):
            return STARTED, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_STARTUP_POLL_STEP_SECONDS, remaining))
    rc = poll()
    if rc is not None:
        if rc == 3 or is_locked(instance_lock_path):
            return RUNNING, int(rc)
        return FAILED, int(rc)
    return STARTED, None


def ensure_running(
    *,
    min_interval: float = MIN_SPAWN_INTERVAL_SECONDS,
    startup_verify_seconds: float = DEFAULT_STARTUP_VERIFY_SECONDS,
) -> str:
    """Return ``running``, ``started``, ``throttled``, ``misconfigured``, or ``failed``.

    A separate launcher lock serializes watchdogs from cron and Passenger. The
    bot's own session lock remains authoritative after spawn, while this lock
    avoids starting redundant Python interpreters while the winner is booting.
    """
    prev_umask = os.umask(0o077) if hasattr(os, "umask") else None
    try:
        try:
            settings = from_env()
        except ValueError as exc:
            _record_launcher_diagnostic(
                _default_log_dir(), f"configuration error: {exc}"
            )
            return MISCONFIGURED

        data_dir = settings.session_path.parent
        log_dir = data_dir / "logs"
        _ensure_private_dir(data_dir)
        _ensure_private_dir(log_dir)

        instance_lock_path = settings.session_path.with_suffix(".lock")
        # Fast path when an instance is already serving traffic.
        if is_locked(instance_lock_path):
            return RUNNING

        problems = check(settings)
        fatal = [problem for problem in problems if not is_advisory(problem)]
        if fatal:
            for problem in fatal:
                _record_launcher_diagnostic(
                    log_dir,
                    f"FAIL configuration: {problem}",
                    settings=settings,
                )
            return MISCONFIGURED

        launcher_lock = InstanceLock(data_dir / "launcher.lock")
        try:
            launcher_lock.acquire()
        except AlreadyRunningError:
            return RUNNING if is_locked(instance_lock_path) else THROTTLED

        try:
            if is_locked(instance_lock_path):
                return RUNNING

            stamp = data_dir / "launcher.stamp"
            if min_interval > 0:
                try:
                    if time.time() - stamp.stat().st_mtime < min_interval:
                        return THROTTLED
                except FileNotFoundError:
                    pass

            if is_locked(instance_lock_path):
                return RUNNING
            try:
                tmp_stamp = data_dir / f".launcher.stamp.{os.getpid()}.tmp"
                tmp_stamp.write_text(str(time.time()), encoding="utf-8")
                if os.name == "posix":
                    os.chmod(tmp_stamp, 0o600)
                tmp_stamp.replace(stamp)
                if os.name == "posix":
                    os.chmod(stamp, 0o600)
            except OSError:
                try:
                    stamp.touch()
                except OSError:
                    pass

            if is_locked(instance_lock_path):
                return RUNNING

            env = os.environ.copy()
            env.setdefault("PYTHONUNBUFFERED", "1")
            if settings.log_file is None:
                # stdout is discarded below, so ensure ``bot.log`` receives all
                # structured runtime logs when ``LOG_FILE`` was left unset.
                env["LOG_FILE"] = str(log_dir / "bot.log")

            error_log = log_dir / "launcher.err"
            _rotate_launcher_err_if_needed(error_log)
            spawn_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            err_fd = os.open(error_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(err_fd, "ab") as err:
                if os.name == "posix":
                    try:
                        os.chmod(error_log, 0o600)
                    except OSError:
                        pass
                err.write(
                    f"[{spawn_stamp}] gamas-bot launcher: spawning bot process\n".encode(
                        "utf-8"
                    )
                )
                err.flush()
                proc = subprocess.Popen(  # noqa: S603 - fixed argument vector, no shell
                    [sys.executable, "-m", "gamas_bot"],
                    cwd=PROJECT_DIR,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=err,
                    start_new_session=(os.name == "posix"),
                    creationflags=_windows_detach_flags(),
                    close_fds=True,
                )

            outcome, rc = _verify_child_startup(
                proc,
                instance_lock_path,
                timeout=startup_verify_seconds,
            )
            if outcome == FAILED:
                _record_launcher_diagnostic(
                    log_dir,
                    f"bot process exited immediately during startup (exit code {rc}); "
                    f"inspect {env.get('LOG_FILE', log_dir / 'bot.log')} and {error_log}",
                    settings=settings,
                )
            return outcome
        finally:
            launcher_lock.release()
    finally:
        if prev_umask is not None and hasattr(os, "umask"):
            os.umask(prev_umask)


def get_status() -> tuple[str, int | None]:
    """Return ``(status, pid)`` where status is ``running``, ``stopped``, or ``misconfigured``."""
    try:
        settings = from_env()
    except ValueError as exc:
        _record_launcher_diagnostic(_default_log_dir(), f"configuration error: {exc}")
        return MISCONFIGURED, None
    lock_file = settings.session_path.with_suffix(".lock")
    if not is_locked(lock_file):
        return STOPPED, None
    return RUNNING, read_lock_pid(lock_file)


def stop_running(*, timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> str:
    """Gracefully stop the running bot instance holding the session lock."""
    try:
        settings = from_env()
    except ValueError as exc:
        _record_launcher_diagnostic(_default_log_dir(), f"configuration error: {exc}")
        return MISCONFIGURED

    lock_file = settings.session_path.with_suffix(".lock")
    if not is_locked(lock_file):
        return STOPPED

    pid = read_lock_pid(lock_file)
    if pid is None or pid == os.getpid():
        _record_launcher_diagnostic(
            settings.session_path.parent / "logs",
            f"session lock {lock_file} is held but contains no valid target PID",
            settings=settings,
        )
        return FAILED

    sig = signal.SIGTERM if hasattr(signal, "SIGTERM") else signal.SIGINT
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return STOPPED
    except OSError as exc:
        _record_launcher_diagnostic(
            settings.session_path.parent / "logs",
            f"could not signal PID {pid}: {exc}",
            settings=settings,
        )
        return FAILED

    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if not is_locked(lock_file):
            return STOPPED
        time.sleep(0.05)
    return FAILED if is_locked(lock_file) else STOPPED


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gamas_bot.launcher",
        description="Start, inspect, stop, or restart the single Gamas Bot instance.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=("start", "ensure", "status", "stop", "restart"),
        help="Lifecycle action to run (default: start / ensure running).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the spawn throttle interval when starting.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(os, "umask"):
        os.umask(0o077)
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    if args.action in ("start", "ensure"):
        interval = 0.0 if args.force else MIN_SPAWN_INTERVAL_SECONDS
        status = ensure_running(min_interval=interval)
        print(status)
        return 2 if status in {MISCONFIGURED, FAILED} else 0

    if args.action == "status":
        status, pid = get_status()
        if status == RUNNING and pid is not None:
            print(f"{RUNNING} (pid={pid})")
        else:
            print(status)
        if status == RUNNING:
            return 0
        return 2 if status == MISCONFIGURED else 1

    if args.action == "stop":
        status = stop_running()
        print(status)
        return 0 if status == STOPPED else 2

    # restart
    stop_status = stop_running()
    if stop_status != STOPPED:
        print(stop_status)
        return 2
    start_status = ensure_running(min_interval=0.0)
    print(start_status)
    return 2 if start_status in {MISCONFIGURED, FAILED} else 0


if __name__ == "__main__":
    raise SystemExit(main())
