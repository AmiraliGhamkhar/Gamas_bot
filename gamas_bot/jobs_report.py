"""Daily jobs dashboard aggregates over the submissions ledger (read-only).

The bot already records every job in ``submissions`` (kind, status, timings,
terminal ``finished_at``). This module turns those rows into a small daily
operations report — counts, success rate, and duration percentiles — so
capacity decisions (``MAX_ACTIVE_JOBS``, host sizing) are made from data
instead of guesswork. It never writes to the database and never raises for
malformed rows: a broken metric must not break the dashboard.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = ["fetch_daily_job_report", "summarize_daily"]

TERMINAL_STATUSES = ("done", "partial", "failed", "rejected")


def _parse_day(stamp: str | None) -> str | None:
    """The UTC calendar day (``YYYY-MM-DD``) of one ISO timestamp."""
    if not stamp:
        return None
    try:
        return str(stamp)[:10]
    except Exception:  # noqa: BLE001 - dashboard is strictly best-effort
        return None


def _percentile(values: Sequence[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(pct / 100.0 * (len(ordered) - 1)))
    return round(ordered[max(0, min(len(ordered) - 1, index))], 1)


def summarize_daily(
    rows: Iterable[dict[str, Any]], *, days: int = 7, now: datetime | None = None
) -> dict[str, Any]:
    """Aggregate submission rows into per-day statistics.

    Each row needs ``created_at``, ``status``, ``kind`` and
    ``duration_seconds`` (the shape :meth:`gamas_bot.db.Database.db` returns).
    ``days`` keeps only the newest N calendar days including today (UTC).
    """
    days = max(1, int(days))
    today = (now or datetime.now(timezone.utc)).date()
    cutoff = (today - timedelta(days=days - 1)).isoformat()
    end = (today + timedelta(days=1)).isoformat()

    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            day = _parse_day(row.get("created_at"))
            if day is None or day < cutoff or day >= end:
                continue
            status = str(row.get("status") or "unknown")
            kind = str(row.get("kind") or "unknown")
            entry = by_day.setdefault(
                day,
                {
                    "day": day,
                    "total": 0,
                    "by_status": {},
                    "by_kind": {},
                    "durations": [],
                    "processing_hours": 0.0,
                },
            )
            entry["total"] += 1
            entry["by_status"][status] = entry["by_status"].get(status, 0) + 1
            entry["by_kind"][kind] = entry["by_kind"].get(kind, 0) + 1
            duration = float(row.get("duration_seconds") or 0.0)
            if status in TERMINAL_STATUSES and duration > 0:
                entry["durations"].append(duration)
                entry["processing_hours"] += duration / 3600.0
        except Exception:  # noqa: BLE001 - one bad row never kills the report
            continue

    days_out = []
    totals = {
        "days": days,
        "total": 0,
        "terminal": 0,
        "succeeded": 0,
        "partial": 0,
        "failed": 0,
        "delivered": 0,
        "success_rate": 0.0,
        "processing_hours": 0.0,
        "p50_duration_s": None,
        "p95_duration_s": None,
    }
    all_durations: list[float] = []
    for day in sorted(by_day, reverse=True):
        entry = by_day[day]
        durations = entry.pop("durations")
        terminal = sum(
            count
            for status, count in entry["by_status"].items()
            if status in TERMINAL_STATUSES
        )
        succeeded = entry["by_status"].get("done", 0)
        partial = entry["by_status"].get("partial", 0)
        failed = entry["by_status"].get("failed", 0)
        days_out.append(
            {
                **entry,
                "terminal": terminal,
                "succeeded": succeeded,
                "partial": partial,
                "failed": failed,
                "delivered": succeeded + partial,
                "success_rate": (
                    round(succeeded / terminal, 3) if terminal else 0.0
                ),
                "p50_duration_s": _percentile(durations, 50),
                "p95_duration_s": _percentile(durations, 95),
                "processing_hours": round(entry["processing_hours"], 2),
            }
        )
        totals["total"] += entry["total"]
        totals["terminal"] += terminal
        totals["succeeded"] += succeeded
        totals["partial"] += partial
        totals["failed"] += failed
        totals["delivered"] += succeeded + partial
        totals["processing_hours"] += entry["processing_hours"]
        all_durations.extend(durations)

    totals["processing_hours"] = round(totals["processing_hours"], 2)
    totals["success_rate"] = (
        round(totals["succeeded"] / totals["terminal"], 3) if totals["terminal"] else 0.0
    )
    totals["p50_duration_s"] = _percentile(all_durations, 50)
    totals["p95_duration_s"] = _percentile(all_durations, 95)
    return {"days": days_out, "totals": totals}


async def fetch_daily_job_report(
    db: Any, *, days: int = 7, now: datetime | None = None
) -> dict[str, Any]:
    """Read only rows in the requested UTC date window (before aggregation)."""
    days = max(1, int(days))
    today = (now or datetime.now(timezone.utc)).date()
    cutoff = (today - timedelta(days=days - 1)).isoformat()
    end = (today + timedelta(days=1)).isoformat()
    rows = await db.db.execute_fetchall(
        """
        SELECT created_at, status, kind, duration_seconds
        FROM submissions
        WHERE julianday(created_at) >= julianday(?)
          AND julianday(created_at) < julianday(?)
        ORDER BY created_at DESC
        """,
        (cutoff, end),
    )
    return summarize_daily([dict(r) for r in rows], days=days, now=now)
