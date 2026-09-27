from __future__ import annotations

import asyncio
import logging
import os

from .bot import StudyBot
from .config import Settings
from .logging_config import configure_logging, install_asyncio_exception_handler


async def main(settings: Settings) -> None:
    if hasattr(os, "umask"):
        os.umask(0o077)
    install_asyncio_exception_handler(asyncio.get_running_loop())
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
        asyncio.run(main(settings))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Shutdown requested by operator")
    except Exception as exc:
        if not configured:
            configure_logging()
        logging.getLogger(__name__).critical("Startup or runtime failure", exc_info=True)
        raise SystemExit(2) from exc
