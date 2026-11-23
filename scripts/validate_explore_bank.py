"""Validate the Explore seed bank, and optionally seed it into a database.

The bank has one canonical definition in :mod:`gamas_bot.explore_bank`; this
script is the operator entry point, so the documented deploy command really
seeds instead of printing a validation message.

    python3 scripts/validate_explore_bank.py --validate-only
    python3 scripts/validate_explore_bank.py --db gamas_bot.db --dry-run
    python3 scripts/validate_explore_bank.py --db gamas_bot.db
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamas_bot.explore_seed import _main

if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
