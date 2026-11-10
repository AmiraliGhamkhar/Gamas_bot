"""Read-only preview of the Gamas 2.0 tariff change.

The brief quotes 75 in a heading but writes ``CREDIT_PRICE_PER_100K_TOMAN=65``
literally. The live catalogue sells 100,000 credits (3 h = 10,800 s) for
60,000 toman. This script never writes to the database and never reinterprets
65 as 65,000: it shows both numbers, the ratio, the plan rows that *would*
change and the SQL an operator would have to run by hand after review.

Usage::

    python -m scripts.credit_price_preview
    python -m scripts.credit_price_preview --candidate 65
    python -m scripts.credit_price_preview --db /path/to/bot.sqlite3

Exit status is always 0 for a preview; a bad candidate exits 2.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from gamas_bot.billing import (
    CREDITS_PER_UNIT,
    PRICE_TOMAN_PER_PACKAGE,
    PROPOSED_PRICE_TOMAN_PER_PACKAGE,
    SECONDS_PER_UNIT,
    plan_catalog,
    price_change_preview,
    seconds_to_credits,
)


def live_plans(db_path: str | None) -> list[dict[str, object]] | None:
    """Read the *existing* plan snapshots, without modifying them."""
    if not db_path:
        return None
    path = Path(db_path)
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT code, hours, price, is_custom, active FROM plans ORDER BY code"
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "code": str(row[0]),
            "hours": float(row[1]),
            "price_toman": int(row[2]),
            "is_custom": bool(row[3]),
            "active": bool(row[4]),
        }
        for row in rows
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate",
        type=int,
        default=PROPOSED_PRICE_TOMAN_PER_PACKAGE,
        help="proposed price per 100,000 credits in toman (literal, default 65)",
    )
    parser.add_argument(
        "--db", default="", help="optional read-only database to show live plan prices"
    )
    args = parser.parse_args(argv)
    if args.candidate <= 0:
        print("candidate price must be positive", file=sys.stderr)
        return 2
    preview = price_change_preview(args.candidate)
    unit = {
        "credits_per_unit": CREDITS_PER_UNIT,
        "seconds_per_unit": SECONDS_PER_UNIT,
        "hours_per_unit": SECONDS_PER_UNIT / 3600,
        "ceiling_check": {
            "1_second": seconds_to_credits(1),
            "10_800_seconds": seconds_to_credits(10_800),
            "10_801_seconds": seconds_to_credits(10_801),
        },
        "live_price_toman_per_100k": PRICE_TOMAN_PER_PACKAGE,
    }
    catalogue = [
        {
            "code": plan.code,
            "hours": plan.included_hours,
            "credits": plan.included_credits,
            "live_price_toman": plan.price_toman,
        }
        for plan in plan_catalog()
    ]
    output: dict[str, object] = {
        "unit": unit,
        "preview": preview,
        "catalogue_unchanged": catalogue,
        "live_plans": live_plans(args.db),
        "activation_flag": "V2_TARIFF_ACTIVATED",
        "activation_default": False,
        "writes_performed": False,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print("\n-- Dry-run migration SQL (NOT executed; review before any activation) --")
    print("BEGIN IMMEDIATE;")
    print("-- Keep historical payment snapshots untouched: they are frozen prices.")
    print("-- Only *new* plan rows may use the candidate tariff after approval:")
    print(f"-- UPDATE plans SET price = {args.candidate} * hours / 3 WHERE is_custom = 0;")
    print("-- ROLLBACK;  -- preview only; COMMIT only after an explicit operator decision")
    ratio = preview.get("ratio_live_over_proposed")
    print("\nWARNING: the candidate is ~%.0fx away from the live tariff."
          % (float(ratio) if isinstance(ratio, (int, float)) else 0.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
