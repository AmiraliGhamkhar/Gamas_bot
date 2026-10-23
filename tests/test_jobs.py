"""Tests for the bounded job queue."""

from __future__ import annotations

import asyncio
import unittest

from gamas_bot.jobs import Job, JobQueue, QueueFullError


def make_job(submission_id: int, user_id: int = 1) -> Job:
    return Job(
        submission_id=submission_id, user_id=user_id, kind="audio", filename="a.mp3"
    )


class JobQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_jobs_run_in_worker(self):
        done: list[int] = []

        async def worker(job: Job) -> None:
            done.append(job.submission_id)

        queue = JobQueue(max_queued=10, max_active=2, worker=worker)
        queue.ensure_workers()
        for index in range(5):
            queue.submit(make_job(index))
        await asyncio.wait_for(_drain(queue), timeout=5)
        self.assertEqual(sorted(done), [0, 1, 2, 3, 4])
        await queue.shutdown()

    async def test_back_pressure_rejects_overflow(self):
        busy = asyncio.Event()
        release = asyncio.Event()

        async def worker(job: Job) -> None:
            busy.set()
            await release.wait()

        queue = JobQueue(max_queued=2, max_active=1, worker=worker)
        queue.ensure_workers()
        # The worker picks job 0 and blocks; the queue then fills up.
        queue.submit(make_job(0))
        await asyncio.wait_for(busy.wait(), timeout=5)
        queue.submit(make_job(1))
        queue.submit(make_job(2))
        with self.assertRaises(QueueFullError):
            queue.submit(make_job(3))
        release.set()
        await queue.shutdown()

    async def test_worker_exception_does_not_kill_pool(self):
        processed: list[int] = []

        async def worker(job: Job) -> None:
            if job.submission_id == 0:
                raise RuntimeError("boom")
            processed.append(job.submission_id)

        queue = JobQueue(max_queued=10, max_active=1, worker=worker)
        queue.ensure_workers()
        queue.submit(make_job(0))
        queue.submit(make_job(1))
        await asyncio.wait_for(_drain(queue), timeout=5)
        self.assertEqual(processed, [1])
        await queue.shutdown()

    async def test_shutdown_rejects_queued_and_cancels_in_flight(self):
        started = asyncio.Event()
        release = asyncio.Event()
        handled: list[int] = []

        async def worker(job: Job) -> None:
            started.set()
            await release.wait()
            handled.append(job.submission_id)

        queue = JobQueue(max_queued=10, max_active=1, worker=worker)
        queue.ensure_workers()
        queue.submit(make_job(0))
        await asyncio.wait_for(started.wait(), timeout=5)
        queue.submit(make_job(1))  # still queued
        queue.submit(make_job(2))  # still queued

        # Shutdown while job 0 is blocked: the queued jobs are rejected and
        # the in-flight job is cancelled (the caller records it as failed).
        await asyncio.wait_for(queue.shutdown(), timeout=5)
        self.assertEqual(handled, [])
        with self.assertRaises(QueueFullError):
            queue.submit(make_job(3))
        # The worker never runs again after shutdown.
        release.set()
        await asyncio.sleep(0.02)
        self.assertEqual(handled, [])

    async def test_zero_queue_capacity_is_refused_not_unbounded(self):
        """``asyncio.Queue(maxsize=0)`` is unbounded, so 0 must never reach it."""

        async def worker(job: Job) -> None:
            return None

        with self.assertRaises(ValueError):
            JobQueue(max_queued=0, max_active=1, worker=worker)
        with self.assertRaises(ValueError):
            JobQueue(max_queued=-1, max_active=1, worker=worker)

    async def test_submit_after_close_rejected(self):
        async def worker(job: Job) -> None:
            return None

        queue = JobQueue(max_queued=1, max_active=1, worker=worker)
        await queue.shutdown()
        with self.assertRaises(QueueFullError):
            queue.submit(make_job(0))

    async def test_one_users_backlog_does_not_delay_another_users_job(self):
        """F-8: dispatch is round-robin across users, order kept per user.

        The per-user admission cap is 2 and a job runs for hours, so two
        uploads from one user used to occupy the whole pool while everyone
        else's jobs waited behind them.
        """
        order: list[tuple[int, int]] = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def worker(job: Job) -> None:
            order.append((job.user_id, job.submission_id))
            started.set()
            await release.wait()

        queue = JobQueue(max_queued=10, max_active=1, worker=worker)
        queue.ensure_workers()
        queue.submit(make_job(0, user_id=1))
        await asyncio.wait_for(started.wait(), timeout=5)
        queue.submit(make_job(1, user_id=1))
        queue.submit(make_job(2, user_id=2))
        queue.submit(make_job(3, user_id=3))
        release.set()
        await asyncio.wait_for(_drain(queue), timeout=5)
        self.assertEqual(order, [(1, 0), (2, 2), (3, 3), (1, 1)])
        await queue.shutdown()

    async def test_a_users_jobs_keep_their_own_order(self):
        order: list[tuple[int, int]] = []

        async def worker(job: Job) -> None:
            order.append((job.user_id, job.submission_id))

        queue = JobQueue(max_queued=10, max_active=1, worker=worker)
        queue.ensure_workers()
        for submission, user in ((0, 1), (1, 2), (2, 1), (3, 1), (4, 2)):
            queue.submit(make_job(submission, user_id=user))
        await asyncio.wait_for(_drain(queue), timeout=5)
        self.assertEqual([job for user, job in order if user == 1], [0, 2, 3])
        self.assertEqual([job for user, job in order if user == 2], [1, 4])
        await queue.shutdown()

    async def test_amount_kept_pending_matches_the_submitted_jobs(self):
        blocked = asyncio.Event()
        started = asyncio.Event()

        async def worker(job: Job) -> None:
            started.set()
            await blocked.wait()

        queue = JobQueue(max_queued=2, max_active=1, worker=worker)
        queue.ensure_workers()
        queue.submit(make_job(0, user_id=1))
        await asyncio.wait_for(started.wait(), timeout=5)
        queue.submit(make_job(1, user_id=1))
        queue.submit(make_job(2, user_id=2))
        self.assertEqual(queue.pending, 2)
        # ``pending`` is the back-pressure count, exactly as before.
        with self.assertRaises(QueueFullError):
            queue.submit(make_job(3, user_id=3))
        blocked.set()
        await queue.shutdown()

    async def test_active_snapshot_does_not_expose_user_ids(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def worker(_job: Job) -> None:
            started.set()
            await release.wait()

        queue = JobQueue(max_queued=2, max_active=1, worker=worker)
        queue.ensure_workers()
        with self.assertLogs("gamas_bot.jobs", level="INFO") as captured:
            queue.submit(make_job(7, user_id=987654321))
        self.assertNotIn("987654321", " ".join(captured.output))
        await asyncio.wait_for(started.wait(), timeout=5)
        snapshot = queue.active_snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertNotIn("user_id", next(iter(snapshot.values())))
        self.assertNotIn("987654321", repr(snapshot))
        release.set()
        await queue.shutdown()

    async def test_active_bound_respected(self):
        concurrent = 0
        peak = 0

        async def worker(job: Job) -> None:
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.02)
            concurrent -= 1

        queue = JobQueue(max_queued=100, max_active=2, worker=worker)
        queue.ensure_workers()
        for index in range(8):
            queue.submit(make_job(index))
        await asyncio.wait_for(_drain(queue), timeout=5)
        self.assertLessEqual(peak, 2)
        await queue.shutdown()


async def _drain(queue: JobQueue, idle_checks: int = 8, delay: float = 0.02) -> None:
    """Wait until the queue stays empty and no work is pending."""
    for _ in range(idle_checks * 10):
        if queue.pending == 0:
            idle_checks -= 1
            if idle_checks <= 0:
                return
        else:
            idle_checks = 8
        await asyncio.sleep(delay)


if __name__ == "__main__":
    unittest.main()
