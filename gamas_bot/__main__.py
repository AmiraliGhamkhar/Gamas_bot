"""``python -m gamas_bot`` — start the bot, or ``--check`` for a self-test.

The self-check is deliberately offline and side-effect free: it never connects
to Telegram, never opens or migrates the database and never takes the instance
lock, so it is safe to run while a healthy instance is serving traffic.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import signal
import sys

from .bot import GamasBot
from .config import Settings, check, is_advisory
from .instance_lock import AlreadyRunningError, InstanceLock, is_locked
from .logging_config import configure_logging, install_asyncio_exception_handler

#: Distributions whose importability ``--check`` verifies directly (the media
#: worker's own ``av``/``ppt2pptx`` check is reported separately). ``python-socks``
#: is excluded: it is only needed with a proxy, which v3 does not configure.
CHECK_REQUIRED_DISTRIBUTIONS = (
    "telethon", "aiohttp", "aiosqlite", "dotenv", "pptx", "docx", "lxml", "av", "ppt2pptx",
)


def lock_path(settings: Settings):

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

    missing = [
        name for name in CHECK_REQUIRED_DISTRIBUTIONS
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


async def main(settings: Settings) -> None:
    if hasattr(os, "umask"):
        os.umask(0o077)
    loop = asyncio.get_running_loop()
    install_asyncio_exception_handler(loop)
    main_task = asyncio.current_task()
    if main_task is not None and hasattr(loop, "add_signal_handler"):
        # cPanel's process manager, ``kill`` and ``pkill`` all send SIGTERM.
        # Turn it into the same graceful shutdown Ctrl+C gets.
        try:
            loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows event loops have no POSIX signal handlers.
    bot = GamasBot(settings)
    try:
        await bot.run()
    finally:
        await bot.shutdown()


def run() -> None:
    if hasattr(os, "umask"):
        os.umask(0o077)
    configured = False
    try:
        if "--check" in sys.argv[1:]:
            raise SystemExit(asyncio.run(run_self_check()))
        from .config import from_env, is_advisory

        settings = from_env()
        configure_logging(settings)
        configured = True
        problems = check(settings)
        # Advisory problems keep the bot running with reduced functionality;
        # they still belong in the log, where an operator debugs a job.
        for problem in problems:
            if is_advisory(problem):
                logging.getLogger(__name__).warning("configuration: %s", problem)
        fatal = [p for p in problems if not is_advisory(p)]
        if fatal:
            for problem in fatal:
                print(f"FAIL configuration: {problem}")
            raise SystemExit(2)
        # Held for the life of the process; the OS releases it on any exit.
        with InstanceLock(lock_path(settings)):
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


if __name__ == "__main__":
    run()
