"""Measure the admin dashboard's database cost on a synthetic dataset.

The dashboard replaced a lifetime-only overview that ran a handful of
full-table aggregates with a period report over indexed queries. This script
puts both on the same database and reports what each costs, plus what the new
report costs with the migration-009 indexes dropped (i.e. the "before" schema).

It is a database benchmark, not a capacity test: it measures SQL time on one
process with no concurrent writers. Rerun it on the deployment host with the
real row counts before drawing sizing conclusions.

Usage:
    python -m scripts.admin_report_bench                 # 20k jobs, 5k users
    python -m scripts.admin_report_bench --jobs 50000 --users 10000 --repeat 5
"""

from __future__ import annotations

import argparse
import asyncio
import random
import shutil
import statistics
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gamas_bot.db import Database

DAY = timedelta(days=1)
ISO = "%Y-%m-%dT%H:%M:%S+00:00"
NEW_SCHEMA_INDEXES = (
    "idx_submissions_created",
    "idx_submissions_finished",
    "idx_submissions_kind_created",
    "idx_users_joined",
    "idx_payments_created",
    "idx_ledger_kind_expiry",
)


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime(ISO)


async def _seed(db: Database, *, users: int, jobs: int, payments: int, days: int) -> None:
    """Insert a deterministic, plausible dataset in one write transaction."""
    rng = random.Random(20261023)
    statuses = ["done"] * 80 + ["partial"] * 6 + ["failed"] * 8 + ["rejected"] * 2 + ["processing"] * 2
    kinds = ["audio"] * 70 + ["video"] * 20 + ["presentation"] * 7 + ["office"] * 3
    errors = ["stt", "stt", "stt", "delivery", "notes", "input", "download", "internal", None]
    engines = ["speechmatics"] * 80 + ["deepgram"] * 15 + ["openai_compatible"] * 3 + [None] * 2
    async with db._transaction():
        await db.db.executemany(
            "INSERT OR IGNORE INTO users(telegram_id, joined_at) VALUES (?, ?)",
            [(uid, _iso(-timedelta(days=rng.uniform(0, days * 2)))) for uid in range(1, users + 1)],
        )
        await db.db.executemany(
            "INSERT INTO plans(code, hours, price, validity_days) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(code) DO NOTHING",
            [("paid_5h_30d", 5, 50000, 30)],
        )
        now = datetime.now(timezone.utc)
        rows = []
        for _ in range(jobs):
            created = now - timedelta(days=rng.uniform(0, days), seconds=rng.uniform(0, 86400))
            wait = rng.uniform(0, 300)
            status = rng.choice(statuses)
            finished = None
            if status in ("done", "partial", "failed", "rejected"):
                finished = created + timedelta(seconds=wait + rng.uniform(30, 3600))
            rows.append(
                (
                    rng.randint(1, users),
                    rng.choice(kinds),
                    "f.wav",
                    status,
                    "x" if status in ("failed", "rejected", "partial") else None,
                    rng.uniform(60, 7200),
                    created.strftime(ISO),
                    (created + timedelta(seconds=wait)).strftime(ISO) if status != "rejected" else None,
                    finished.strftime(ISO) if finished else None,
                    rng.choice(errors) if status in ("failed", "rejected", "partial") else None,
                    rng.choice(engines),
                )
            )
        await db.db.executemany(
            "INSERT INTO submissions(user_id, kind, filename, status, error, duration_seconds, "
            "created_at, started_at, finished_at, error_kind, stt_engine) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        payment_rows = []
        open_users: set[int] = set()
        for index in range(payments):
            status = rng.choice(["approved"] * 6 + ["rejected"] * 2 + ["pending_review"])
            user_id = rng.randint(1, users)
            if status == "pending_review":
                # idx_payments_one_open allows one open order per user.
                user_id = (index % users) + 1
                while user_id in open_users:
                    user_id = (user_id % users) + 1
                open_users.add(user_id)
            payment_rows.append(
                (
                    user_id,
                    status,
                    "receipt-1.jpg",
                    _iso(-timedelta(days=rng.uniform(0, days))),
                )
            )
        await db.db.executemany(
            "INSERT INTO payments(user_id, plan_code, status, receipt_path, created_at, "
            "purchased_seconds, purchased_price, purchased_validity_days) "
            "VALUES (?, 'paid_5h_30d', ?, ?, ?, 18000, 50000, 30)",
            payment_rows,
        )
        await db.db.executemany(
            "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
            "VALUES (?, ?, 'credit', 'bench', ?, ?)",
            [
                (
                    rng.randint(1, users),
                    18000,
                    _iso(timedelta(days=rng.uniform(0, 60))) if rng.random() < 0.8 else None,
                    _iso(-timedelta(days=rng.uniform(0, days))),
                )
                for _ in range(max(1, users // 2))
            ],
        )
        await db.db.commit()


async def _time(call, repeats: int) -> tuple[float, float]:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        await call()
        samples.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(samples), max(samples)


async def run(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "bench.db"
        db = Database(path)
        await db.connect()
        seed_started = time.perf_counter()
        await _seed(db, users=args.users, jobs=args.jobs, payments=args.payments, days=args.days)
        seed_seconds = time.perf_counter() - seed_started
        counts = {
            table: (await db.read_db.execute_fetchall(f"SELECT COUNT(*) FROM {table}"))[0][0]
            for table in ("users", "submissions", "payments", "ledger")
        }
        print(
            f"dataset: {counts['users']} users, {counts['submissions']} submissions, "
            f"{counts['payments']} payments, {counts['ledger']} ledger rows "
            f"(seeded in {seed_seconds:.1f}s, {args.days}-day window)"
        )

        async def legacy_overview():
            await db.stats()
            await db.count_pending_review_payments()

        legacy_median, legacy_max = await _time(legacy_overview, args.repeat)
        print(f"previous overview (lifetime aggregates): median {legacy_median:.2f} ms, "
              f"max {legacy_max:.2f} ms")

        seven_days_ago = _iso(-7 * DAY)
        thirty_days_ago = _iso(-30 * DAY)
        results: dict[str, tuple[float, float]] = {}

        async def report(since):
            await db.admin_report(period_start=since)

        for label, since in (
            ("new report, 7-day window", seven_days_ago),
            ("new report, 30-day window", thirty_days_ago),
            ("new report, all history", None),
        ):
            results[label] = await _time(lambda s=since: report(s), args.repeat)
            median, worst = results[label]
            print(f"{label}: median {median:.2f} ms, max {worst:.2f} ms")

        for label, since in (
            ("users/payments screens, 7-day", seven_days_ago),
            ("users/payments screens, 30-day", thirty_days_ago),
            ("users/payments screens, all history", None),
        ):
            async def report_no_timings(s=since):
                await db.admin_report(period_start=s, with_timings=False)

            median, worst = await _time(report_no_timings, args.repeat)
            print(f"{label}: median {median:.2f} ms, max {worst:.2f} ms")

        engines_median, _ = await _time(
            lambda: db.admin_stt_engines(period_start=seven_days_ago), args.repeat
        )
        health_median, _ = await _time(db.admin_health, args.repeat)
        print(f"STT engine usage (7-day): median {engines_median:.2f} ms")
        print(f"system health: median {health_median:.2f} ms")
        await db.close()

        # Same report on the pre-009 schema: the indexes are dropped, the data
        # is byte-identical.
        legacy_path = Path(temp) / "no_indexes.db"
        shutil.copy2(path, legacy_path)
        connection = sqlite3.connect(legacy_path)
        for index in NEW_SCHEMA_INDEXES:
            connection.execute(f"DROP INDEX IF EXISTS {index}")
        connection.commit()
        connection.close()
        legacy_db = Database(legacy_path)
        await legacy_db.connect()
        try:
            for label, since in (
                ("7-day", seven_days_ago),
                ("30-day", thirty_days_ago),
                ("all history", None),
            ):
                async def report_legacy(s=since):
                    await legacy_db.admin_report(period_start=s)

                median, worst = await _time(report_legacy, args.repeat)
                print(f"new report WITHOUT 009 indexes, {label}: "
                      f"median {median:.2f} ms, max {worst:.2f} ms")
        finally:
            await legacy_db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=5_000)
    parser.add_argument("--jobs", type=int, default=20_000)
    parser.add_argument("--payments", type=int, default=2_000)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--repeat", type=int, default=5)
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
