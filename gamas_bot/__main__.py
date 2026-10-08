from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import signal
import sys

from .bot import StudyBot
from .config import Settings
from .instance_lock import AlreadyRunningError, InstanceLock, is_locked
from .logging_config import configure_logging, install_asyncio_exception_handler

#: Distributions whose importability ``--check`` verifies directly (the media
#: worker's own ``av``/``ppt2pptx`` check is reported separately). Deliberately
#: excluded: ``python-socks`` (only needed with ``TELEGRAM_PROXY``) and
#: ``Pillow``/``arabic-reshaper``/``python-bidi`` (only the offline page
#: renderer uses them). ``tests/test_dependency_consistency.py`` fails when a
#: runtime dependency exists that is neither checked nor explicitly excluded.
CHECK_REQUIRED_DISTRIBUTIONS = (
    "telethon", "aiohttp", "aiosqlite", "dotenv", "pptx", "docx",
    "lxml", "av", "ppt2pptx", "cryptography", "pypdf",
)


async def run_self_check() -> int:
    """``python -m gamas_bot --check``: configuration + dependency self-check.

    Deliberately offline and side-effect free: it never connects to Telegram,
    never opens or migrates the database and never takes the instance lock, so
    it is safe to run while a healthy instance is serving traffic (the lock is
    only *reported*). Printed lines are PASS/FAIL, exit code 0 or 2 — the same
    contract ``scripts/cpanel_preflight.py`` documents.
    """
    print("gamas-bot self-check")
    try:
        settings = Settings.from_env()
        settings.validate_runtime()
    except ValueError as exc:
        print(f"FAIL configuration: {exc}")
        return 2
    print("PASS configuration: telegram, provider and admin settings are valid")

    missing = [
        name for name in CHECK_REQUIRED_DISTRIBUTIONS
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        print(f"FAIL dependencies: not importable: {', '.join(missing)}")
        return 2
    print("PASS dependencies: every runtime package is importable")

    # Informational: a live instance is the normal state of a healthy bot, not
    # an error, but an operator debugging "no answer" needs to see it here.
    # ``is_locked`` creates the file when absent, so the existence check keeps
    # the whole self-check free of writes.
    if settings.lock_path.exists() and is_locked(settings.lock_path):
        print(f"NOTE instance: another process holds {settings.lock_path}")

    from .media import MediaToolError, check_media_worker

    try:
        summary = await check_media_worker()
    except MediaToolError as exc:
        print(f"FAIL media worker: {exc}")
        return 2
    print(f"PASS media worker: {summary}")
    print("self-check passed")
    return 0


async def main(settings: Settings) -> None:
    if hasattr(os, "umask"):
        os.umask(0o077)
    loop = asyncio.get_running_loop()
    install_asyncio_exception_handler(loop)
    main_task = asyncio.current_task()
    if main_task is not None and hasattr(loop, "add_signal_handler"):
        # cPanel's process manager, ``kill`` and ``pkill`` all send SIGTERM.
        # Turn it into the same graceful shutdown Ctrl+C gets, so running jobs
        # are marked interrupted and the Telegram session is closed cleanly.
        try:
            loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows event loops have no POSIX signal handlers.
    await StudyBot(settings).run()


if __name__ == "__main__":
    # Apply before file logging and Telethon can create sensitive files.
    if hasattr(os, "umask"):
        os.umask(0o077)
    configured = False
    try:
        if "--check" in sys.argv[1:]:
            # Before logging, lock and database: the check must be safe to run
            # at any time, including while the bot is running.
            raise SystemExit(asyncio.run(run_self_check()))
        settings = Settings.from_env()
        configure_logging(settings)
        configured = True
        settings.validate_runtime()
        # Held for the life of the process; the OS releases it on any exit.
        with InstanceLock(settings.lock_path):
            asyncio.run(main(settings))
    except AlreadyRunningError as exc:
        # Cron watchdogs start the bot every few minutes; a live instance is
        # the normal case, not a failure.
        logging.getLogger(__name__).warning("Not starting: %s", exc)
        raise SystemExit(3) from exc
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.getLogger(__name__).info("Shutdown requested by operator or process manager")
    except Exception as exc:
        if not configured:
            configure_logging()
        logging.getLogger(__name__).critical("Startup or runtime failure", exc_info=True)
        raise SystemExit(2) from exc
