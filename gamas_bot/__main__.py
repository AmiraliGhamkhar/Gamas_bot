"""``python -m gamas_bot`` — start the bot, or ``--check`` for a self-test.

The self-check is deliberately offline and side-effect free: it never connects
to Telegram, never opens or migrates the database and never takes the instance
lock, so it is safe to run while a healthy instance is serving traffic.

``GamasBot`` and its heavy Office/PDF/Telethon stack are imported lazily inside
:func:`_run_bot` rather than at module import time:

1. ``python -m gamas_bot --check`` can report missing runtime packages instead
   of failing with a top-level ``ImportError`` traceback.
2. A cron/watchdog launch that hits :class:`AlreadyRunningError` exits in a few
   milliseconds without loading ~80 MB of Office/PDF/media libraries.
3. Any import failure during bot startup occurs *after* ``configure_logging``
   has opened ``LOG_FILE`` and ``stderr``, so the failure is always recorded.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import logging
import os
from pathlib import Path
import signal
import sys

from . import __version__
from .config import Settings, check, is_advisory, note_route_summary
from .instance_lock import AlreadyRunningError, InstanceLock, is_locked
from .logging_config import configure_logging, install_asyncio_exception_handler

#: Runtime modules whose importability ``--check`` verifies directly (the media
#: worker's own ``av``/``ppt2pptx`` check is reported separately as well).
CHECK_REQUIRED_DISTRIBUTIONS = (
    "telethon",
    "cryptg",
    "aiohttp",
    "orjson",
    "aiosqlite",
    "dotenv",
    "pptx",
    "docx",
    "lxml",
    "av",
    "ppt2pptx",
    "openpyxl",
    "PIL",
    "pypdf",
    "reportlab",
    "arabic_reshaper",
    "bidi",
)


def lock_path(settings: Settings) -> Path:
    return settings.session_path.with_suffix(".lock")


async def run_self_check() -> int:
    print("gamas-bot v3 self-check")
    settings = from_env_safe()
    if settings is None:
        return 2
    problems = check(settings)
    fatal = [p for p in problems if not is_advisory(p)]
    for problem in problems:
        level = "WARN" if is_advisory(problem) else "FAIL"
        print(f"{level} configuration: {problem}")
    if fatal:
        return 2
    print("PASS configuration: telegram, provider and admin settings are valid")
    # Offline and credential-free: the operator can confirm which providers the
    # *next* start will attempt without printing a key or contacting a gateway.
    print("Effective note routing (no secrets):")
    for line in note_route_summary(settings):
        print(f"  {line}")

    missing = [
        name
        for name in CHECK_REQUIRED_DISTRIBUTIONS
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        print(f"FAIL dependencies: not importable: {', '.join(missing)}")
        return 2
    print("PASS dependencies: every runtime package is importable")

    lock = lock_path(settings)
    if lock.exists() and is_locked(lock):
        print(f"NOTE instance: another process holds {lock}")

    from .media import MediaToolError, check_media_worker

    try:
        summary = await check_media_worker()
    except MediaToolError as exc:
        print(f"FAIL media worker: {exc}")
        return 2
    print(f"PASS media worker: {summary}")
    print("self-check passed")
    return 0


def from_env_safe() -> Settings | None:
    from .config import from_env

    try:
        return from_env()
    except ValueError as exc:
        print(f"FAIL configuration: {exc}")
        return None


def _flush_logging() -> None:
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


async def _run_bot(settings: Settings) -> None:
    from .bot import GamasBot

    if hasattr(os, "umask"):
        os.umask(0o077)
    loop = asyncio.get_running_loop()
    # ``asyncio.to_thread`` powers every file, document-build, and image call
    # (see ``fileio.run_file_io``). The default executor sizes itself from
    # ``os.cpu_count()``, which on shared cPanel reports the *host's* cores
    # and can spawn up to 32 threads against a 10 MB/s I/O budget.
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=8, thread_name_prefix="gamas-io")
    )
    install_asyncio_exception_handler(loop)
    main_task = asyncio.current_task()
    if main_task is not None and hasattr(loop, "add_signal_handler"):
        # Convert both SIGTERM (cPanel/systemd/kill) and SIGINT (Ctrl+C) into
        # clean asyncio task cancellation so ``await bot.shutdown()`` runs to
        # completion without mid-cleanup KeyboardInterrupt races.
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, main_task.cancel)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Windows event loops have no POSIX signal handlers.
    bot = GamasBot(settings)
    try:
        await bot.run()
    finally:
        await bot.shutdown()


def run(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else list(argv)
    if hasattr(os, "umask"):
        os.umask(0o077)
    if "--version" in args:
        print(f"gamas-bot {__version__}")
        raise SystemExit(0)
    if "--check" in args:
        raise SystemExit(asyncio.run(run_self_check()))

    configured = False
    try:
        # Bootstrap env-backed stderr/file logging *before* ``from_env()`` so
        # parse warnings and ``ValueError`` exceptions are never lost.
        configure_logging()
        from .config import from_env

        settings = from_env()
        configure_logging(settings)
        configured = True
        problems = check(settings)
        logger = logging.getLogger(__name__)
        # Advisory problems keep the bot running with reduced functionality;
        # they still belong in the log, where an operator debugs a job.
        for problem in problems:
            if is_advisory(problem):
                logger.warning("configuration: %s", problem)
        fatal = [p for p in problems if not is_advisory(p)]
        if fatal:
            for problem in fatal:
                logger.critical("configuration: %s", problem)
                if sys.stderr is not None:
                    print(f"FAIL configuration: {problem}", file=sys.stderr)
            _flush_logging()
            raise SystemExit(2)
        # Held for the life of the process; the OS releases it on any exit.
        with InstanceLock(lock_path(settings)):
            asyncio.run(_run_bot(settings))
    except AlreadyRunningError as exc:
        # Cron watchdogs start the bot every few minutes; a live instance is
        # the normal case, not a failure.
        logging.getLogger(__name__).warning("Not starting: %s", exc)
        _flush_logging()
        raise SystemExit(3) from exc
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.getLogger(__name__).info("Shutdown requested by operator or process manager")
        _flush_logging()
    except SystemExit:
        raise
    except Exception as exc:
        if not configured:
            configure_logging()
        logging.getLogger(__name__).critical("Startup or runtime failure", exc_info=True)
        _flush_logging()
        raise SystemExit(2) from exc


def main() -> None:
    """Synchronous console-script entry point (``gamas-bot``)."""
    run()


if __name__ == "__main__":
    main()
