"""Show the confirmed Gamas tariff and plan catalogue.

The confirmed commercial tariff is:

    100,000 credits = 3 hours (10,800 seconds) = 65,000 Toman.

This script is read-only: it prints the canonical constants, the full plan
catalogue, and (optionally) the existing plan rows from a read-only database
so an operator can verify that prices match before/after deployment.

Usage::

    python -m scripts.credit_price_preview
    python -m scripts.credit_price_preview --candidate 65000
    python -m scripts.credit_price_preview --db /path/to/bot.sqlite3

Exit status 0 for a valid price; 2 for a nonsensical candidate (e.g. 65).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from gamas_bot.billing import (
    CREDITS_PER_UNIT,
    HOURS_PER_PACKAGE,
    PRICE_TOMAN_PER_PACKAGE,
    SECONDS_PER_UNIT,
    plan_catalog,
    price_change_preview,
    seconds_to_credits,
)

#: Confirmed canonical price (3 h = 65,000 T).
CONFIRMED_PRICE = PRICE_TOMAN_PER_PACKAGE


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
        default=CONFIRMED_PRICE,
        help="price per 100,000 credits in toman to preview (default 65000)",
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
        "hours_per_package": HOURS_PER_PACKAGE,
        "confirmed_price_toman_per_package": CONFIRMED_PRICE,
        "confirmed_price_toman_per_hour": CONFIRMED_PRICE // HOURS_PER_PACKAGE,
        "ceiling_check": {
            "1_second": seconds_to_credits(1),
            "10_800_seconds": seconds_to_credits(10_800),
            "10_801_seconds": seconds_to_credits(10_801),
        },
    }
    catalogue = [
        {
            "code": plan.code,
            "name": plan.name,
            "hours": plan.included_hours,
            "credits": plan.included_credits,
            "price_toman": plan.price_toman,
            "validity_days": plan.validity_days,
            "is_free": plan.is_free,
        }
        for plan in plan_catalog()
    ]
    output: dict[str, object] = {
        "confirmed_tariff": {
            "credits_per_package": CREDITS_PER_UNIT,
            "hours_per_package": HOURS_PER_PACKAGE,
            "price_toman_per_package": CONFIRMED_PRICE,
        },
        "unit": unit,
        "preview": preview,
        "catalogue": catalogue,
        "live_plans": live_plans(args.db),
        "writes_performed": False,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    # Sanity check: the literal "65" (from an early brief misreading) is
    # roughly 1000x too cheap. Surface it clearly so nobody ships it by accident.
    if args.candidate < 1000:
        print(
            f"\nWARNING: candidate price {args.candidate} is implausibly low "
            f"(canonical is {CONFIRMED_PRICE}). Did you mean {args.candidate * 1000}?",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
