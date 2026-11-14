"""Print the active plan catalog and optionally inspect a database read-only.

The credit conversion is a usage-unit relation, not a price rule. Original/list
and final/sale prices are explicit values from the catalog. This command makes
no writes.

Usage::

    python -m scripts.plan_catalog_preview
    python -m scripts.plan_catalog_preview --db /path/to/bot.sqlite3
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from gamas_bot.billing import (
    CREDITS_PER_UNIT,
    HOURS_PER_UNIT,
    SECONDS_PER_UNIT,
    plan_catalog,
)


def live_plans(db_path: str | None) -> list[dict[str, Any]] | None:
    """Read existing plan rows without modifying the database."""
    if not db_path:
        return None
    path = Path(db_path)
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(plans)").fetchall()
        }
        if not columns:
            return None
        original_expr = "original_price" if "original_price" in columns else "price"
        rows = connection.execute(
            "SELECT code, hours, price, "
            f"{original_expr} AS original_price, is_custom, active "
            "FROM plans ORDER BY code"
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "code": str(row[0]),
            "hours": float(row[1]),
            "sale_price_toman": int(row[2]),
            "original_price_toman": int(row[3]),
            "is_custom": bool(row[4]),
            "active": bool(row[5]),
        }
        for row in rows
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default="", help="optional read-only database to inspect"
    )
    args = parser.parse_args(argv)

    catalogue = [
        {
            "code": plan.code,
            "name": plan.name,
            "hours": plan.included_hours,
            "credits": plan.included_credits,
            "original_price_toman": plan.original_price_toman,
            "sale_price_toman": plan.price_toman,
            "validity_days": plan.validity_days,
            "is_free": plan.is_free,
            "purchase_available": plan.purchase_available,
        }
        for plan in plan_catalog()
    ]
    output = {
        "usage_unit": {
            "credits": CREDITS_PER_UNIT,
            "seconds": SECONDS_PER_UNIT,
            "hours": HOURS_PER_UNIT,
            "price_rule": None,
        },
        "catalogue": catalogue,
        "live_plans": live_plans(args.db),
        "writes_performed": False,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
