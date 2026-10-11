"""Tests for the bounded job queue."""

from __future__ import annotations

import asyncio
import unittest

from gamas_bot.jobs import Job, JobQueue, QueueFullError


def make_job(submission_id: int) -> Job:
    return Job(submission_id=submission_id, user_id=1, kind="audio", filename="a.mp3")


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

    async def test_submit_after_close_rejected(self):
        async def worker(job: Job) -> None:
            return None

        queue = JobQueue(max_queued=1, max_active=1, worker=worker)
        await queue.shutdown()
        with self.assertRaises(QueueFullError):
            queue.submit(make_job(0))

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
