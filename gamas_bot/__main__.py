from __future__ import annotations

import asyncio
import logging
import os
import signal

from .bot import StudyBot
from .config import Settings
from .instance_lock import AlreadyRunningError, InstanceLock
from .logging_config import configure_logging, install_asyncio_exception_handler


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
