from __future__ import annotations

import asyncio
import logging
import os
import sys

from .bot import StudyBot
from .config import Settings


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main() -> None:
    if hasattr(os, "umask"):
        os.umask(0o077)
    configure_logging()
    try:
        settings = Settings.from_env()
        settings.validate_runtime()
        await StudyBot(settings).run()
    except (ValueError, OSError) as exc:
        logging.getLogger(__name__).critical("Startup failed: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
