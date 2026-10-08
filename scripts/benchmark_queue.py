"""Offline load benchmark for the bounded job queue.

Measures what happens when ``N`` uploads arrive at once against the real
``StudyBot`` queue machinery (bounded ``asyncio.Queue`` + fixed worker pool +
job semaphore + SQLite status writes) with a synthetic job body: no Telegram,
no providers, no network.

For every scenario it reports acceptance vs back-pressure rejection, queue
wait, per-job latency and throughput percentiles, plus process memory — the
numbers the capacity model (``MAX_CONCURRENT_JOBS`` / ``MAX_PENDING_JOBS``)
is chosen with. Deterministic offline; safe to run in CI with ``--json``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from gamas_bot.bot import QueuedJob, StudyBot
from gamas_bot.billing import canonical_plan_records
from gamas_bot.config import Settings

DEFAULT_SCENARIOS = (1, 3, 5, 10, 25, 50)


def _base_settings(root: Path, *, workers: int, pending: int) -> Settings:
    base = Settings(
        telegram_bot_token="benchmark-token",
        telegram_api_id=1,
        telegram_api_hash="benchmark-hash",
        admin_ids=frozenset(),
        database_path=root / "bot.sqlite3",
        session_path=root / "session",
        temp_dir=root / "tmp",
        max_file_size=2_000_000_000,
        stt_primary="speechmatics",
        stt_language="fa",
        stt_fallback_enabled=True,
        stt_min_confidence=0.65,
        speechmatics_api_key="benchmark-key",
        speechmatics_base_url="https://example.test/v2",
        deepgram_api_key=None,
        deepgram_model="nova-3",
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash-lite",
        max_concurrent_jobs=workers,
        stt_poll_interval=1,
        stt_job_timeout=100,
    )
    return replace(base, max_pending_jobs=pending)


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile (deterministic, no interpolation)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


async def _scenario(total: int, *, workers: int, pending: int, job_seconds: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="queue-bench-") as folder:
        root = Path(folder)
        settings = _base_settings(root, workers=workers, pending=pending)
        bot = StudyBot(settings)
        await bot.db.open()
        await bot.db.sync_plan_catalog(list(canonical_plan_records()))
        try:
            user = await bot.db.upsert_user(100_001, "bench")
            finished: dict[int, tuple[float, float, float]] = {}
            rejected: dict[int, float] = {}
            failed: list[int] = []

            def make_job(submission_id: int, enqueued_at: float):
                async def run():
                    started_at = time.perf_counter()
                    try:
                        # Mirror the production job shape: semaphore-bounded
                        # work plus two status writes against the real DB.
                        async with bot._job_semaphore:
                            await bot.db.set_submission_status(
                                submission_id, "processing"
                            )
                            await asyncio.sleep(job_seconds)
                            await bot.db.set_submission_status(submission_id, "done")
                    except BaseException:
                        failed.append(submission_id)
                        raise
                    finally:
                        # A crash must still end the clock, or the harness
                        # would wait for a job that never reports back.
                        finished[submission_id] = (
                            enqueued_at, started_at, time.perf_counter()
                        )

                return QueuedJob(submission_id=submission_id, kind="audio", run=run)

            wall_start = time.perf_counter()
            for _ in range(total):
                submission_id = await bot.db.create_submission(
                    user["id"], "file", None, "lecture.mp3", None, source_type="audio"
                )
                enqueued_at = time.perf_counter()
                job = make_job(submission_id, enqueued_at)
                if not bot._enqueue(job):
                    rejected[submission_id] = time.perf_counter()
                    await bot._reject_job(job, "ظرفیت صف پر است")

            deadline = time.perf_counter() + 120.0
            while len(finished) + len(rejected) < total:
                if time.perf_counter() > deadline:
                    raise RuntimeError(f"scenario of {total} jobs did not finish in time")
                await asyncio.sleep(0.01)
            wall = time.perf_counter() - wall_start
            if failed:
                raise RuntimeError(f"jobs failed during the run: {failed}")

            waits = [start - enqueued for enqueued, start, _ in finished.values()]
            latencies = [end - start for _, start, end in finished.values()]
            end_to_end = [end - enqueued for enqueued, _, end in finished.values()]
            accepted = len(finished)
            return {
                "jobs": total,
                "workers": workers,
                "pending_capacity": pending,
                "accepted": accepted,
                "backpressure_rejected": total - accepted,
                "wall_seconds": round(wall, 3),
                "throughput_jobs_per_s": round(accepted / wall, 2) if wall else 0.0,
                "queue_wait_p50_ms": round(_percentile(waits, 0.50) * 1000, 1),
                "queue_wait_p95_ms": round(_percentile(waits, 0.95) * 1000, 1),
                "queue_wait_p99_ms": round(_percentile(waits, 0.99) * 1000, 1),
                "latency_p50_ms": round(_percentile(latencies, 0.50) * 1000, 1),
                "latency_p95_ms": round(_percentile(latencies, 0.95) * 1000, 1),
                "latency_p99_ms": round(_percentile(latencies, 0.99) * 1000, 1),
                "end_to_end_p95_ms": round(_percentile(end_to_end, 0.95) * 1000, 1),
                "rss_kib": _rss_kib(),
            }
        finally:
            await bot.shutdown()


async def run(args: argparse.Namespace) -> list[dict]:
    # Back-pressure rejections are *expected* at high load and would drown
    # the report; a scenario failure still surfaces through the raised error.
    logging.getLogger("gamas_bot.bot").setLevel(logging.ERROR)
    results = []
    for total in args.jobs:
        result = await _scenario(
            total, workers=args.workers, pending=args.pending,
            job_seconds=args.job_seconds,
        )
        results.append(result)
        if not args.json:
            print(
                f"jobs={result['jobs']:>3} accepted={result['accepted']:>3} "
                f"rejected={result['backpressure_rejected']:>3} "
                f"wall={result['wall_seconds']:>6.3f}s "
                f"throughput={result['throughput_jobs_per_s']:>6.2f}/s "
                f"wait p50/p95/p99={result['queue_wait_p50_ms']:.0f}/"
                f"{result['queue_wait_p95_ms']:.0f}/{result['queue_wait_p99_ms']:.0f}ms "
                f"latency p50/p95/p99={result['latency_p50_ms']:.0f}/"
                f"{result['latency_p95_ms']:.0f}/{result['latency_p99_ms']:.0f}ms "
                f"rss={result['rss_kib']}KiB"
            )
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="bounded-queue load benchmark (offline)")
    parser.add_argument("--jobs", nargs="+", type=int, default=list(DEFAULT_SCENARIOS))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--pending", type=int, default=8)
    parser.add_argument("--job-seconds", type=float, default=0.15)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
