"""Cron entry point: start the bot when it is not running.

Works from any working directory, so a cPanel cron job can call it with
absolute paths only::

    * * * * * /home/USER/virtualenv/gamas_bot/3.11/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamas_bot.launcher import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
