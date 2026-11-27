"""Daily jobs dashboard CLI (cron-friendly, read-only).

Prints per-day job counts, success rate, and duration percentiles from the
submissions ledger, so operators can size ``MAX_ACTIVE_JOBS`` and the host
from real numbers::

    python scripts/job_report.py                  # last 7 days, text
    python scripts/job_report.py --days 14 --json # machine-readable
    python scripts/job_report.py --db /path/to/gamas.sqlite3

It never writes to the database and never needs network access.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamas_bot.config import from_env  # noqa: E402
from gamas_bot.db import Database  # noqa: E402
from gamas_bot.jobs_report import fetch_daily_job_report  # noqa: E402


async def _report(db_path: Path, days: int) -> dict[str, Any]:
    db = Database(db_path)
    await db.connect()
    try:
        return await fetch_daily_job_report(db, days=days)
    finally:
        await db.close()


def _format_text(report: dict[str, Any]) -> str:
    lines = ["Gamas daily jobs report", "=" * 24, ""]
    header = (
        f"{'day':<12} {'total':>6} {'done':>6} {'partial':>8} "
        f"{'failed':>7} {'reject':>7} {'succ%':>6} {'p50s':>7} {'p95s':>7} {'hours':>7}"
    )
    lines.append(header)
    for entry in report["days"]:
        status = entry["by_status"]
        lines.append(
            f"{entry['day']:<12} {entry['total']:>6} "
            f"{status.get('done', 0):>6} {status.get('partial', 0):>8} "
            f"{status.get('failed', 0):>7} {status.get('rejected', 0):>7} "
            f"{round(entry['success_rate'] * 100):>6} "
            f"{entry['p50_duration_s'] or 0:>7} {entry['p95_duration_s'] or 0:>7} "
            f"{entry['processing_hours']:>7}"
        )
    totals = report["totals"]
    lines += [
        "",
        (
            f"totals: {totals['total']} jobs, success {totals['success_rate'] * 100:.1f}%, "
            f"p50 {totals['p50_duration_s'] or 0}s / p95 {totals['p95_duration_s'] or 0}s, "
            f"{totals['processing_hours']} processing hours over {totals['days']} day(s)"
        ),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="calendar days to include (default 7)")
    parser.add_argument("--db", type=Path, default=None, help="database file (default: from .env)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    db_path = args.db if args.db is not None else Path(from_env().db_path)
    report = asyncio.run(_report(db_path, max(1, args.days)))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
