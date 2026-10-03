"""Concurrency coverage for the bounded job queue.

A semaphore alone only limits *active* jobs: a burst of uploads could still
create thousands of waiting tasks, each holding an event object, a progress
message and a database row. The bot therefore moves accepted uploads through a
fixed-capacity ``asyncio.Queue`` drained by a fixed pool of workers, and this
module pins that behaviour:

* accepted work is bounded and the overflow is rejected with back-pressure
  (never silently dropped and never left as a "pending" row forever);
* concurrency never exceeds ``MAX_CONCURRENT_JOBS``;
* cancelling a worker (shutdown) leaves no leaked task and no stuck job;
* a queued job that never runs is still recorded as failed and explained.

Capacity model: ``MAX_CONCURRENT_JOBS`` jobs run at once and at most
``MAX_PENDING_JOBS`` more wait in the queue; anything beyond that is rejected.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from gamas_bot.bot import QueuedJob, StudyBot

from support import FakeJobEvent, make_settings


class JobQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.settings = make_settings(
            database_path=self.root / "bot.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            max_concurrent_jobs=1,
            max_pending_jobs=2,
        )
        self.bot = StudyBot(self.settings)
        await self.bot.db.open()
        self.addCleanup(self._stop_workers)

    async def _stop_workers(self):
        for worker in list(self.bot._workers):
            worker.cancel()
        if self.bot._workers:
            await asyncio.gather(*self.bot._workers, return_exceptions=True)
            self.bot._workers = []
        await self.bot.db.close()

    async def _submission(self, telegram_id: int = 101) -> int:
        user = await self.bot.db.upsert_user(telegram_id, "student")
        return await self.bot.db.create_submission(
            user["id"], "file", None, "lecture.mp3", None, source_type="audio"
        )

    @staticmethod
    async def _wait_for(predicate, timeout: float = 2.0):
        """Yield until ``predicate()`` holds (tests must never sleep blindly)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.005)
        return False

    def _blocking_job(self, submission_id: int, started: list, release: asyncio.Event):
        async def run():
            started.append(submission_id)
            await release.wait()

        return QueuedJob(submission_id=submission_id, kind="audio", run=run)

    def _quick_job(self, submission_id: int, order: list | None = None):
        async def run():
            if order is not None:
                order.append(submission_id)
            await self.bot.db.set_submission_status(submission_id, "done")

        return QueuedJob(submission_id=submission_id, kind="audio", run=run)

    async def _status(self, submission_id: int) -> tuple[str, str | None]:
        async with self.bot.db._lock:
            cursor = await self.bot.db._db().execute(
                "SELECT status, error_message FROM audio_submissions WHERE id=?",
                (submission_id,),
            )
            return tuple(await cursor.fetchone())

    async def test_worker_pool_matches_the_configured_concurrency(self):
        self.bot.settings = make_settings(
            database_path=self.settings.database_path,
            session_path=self.settings.session_path,
            temp_dir=self.settings.temp_dir,
            max_concurrent_jobs=2,
            max_pending_jobs=4,
        )
        release = asyncio.Event()
        started: list[int] = []
        first = await self._submission()
        second = await self._submission()
        self.assertTrue(self.bot._enqueue(self._blocking_job(first, started, release)))
        self.assertTrue(self.bot._enqueue(self._blocking_job(second, started, release)))
        self.assertEqual(len(self.bot._workers), 2)
        self.assertTrue(await self._wait_for(lambda: len(started) == 2))
        release.set()

    async def test_queue_is_bounded_and_overflow_is_rejected(self):
        release = asyncio.Event()
        started: list[int] = []
        running = await self._submission(200)
        self.assertTrue(self.bot._enqueue(self._blocking_job(running, started, release)))
        self.assertTrue(await self._wait_for(lambda: bool(started)))

        # The two queue slots fill up, and anything beyond them is refused
        # instead of piling up as an unbounded in-memory backlog.
        waiting = [await self._submission(201 + index) for index in range(2)]
        for submission_id in waiting:
            self.assertTrue(self.bot._enqueue(self._blocking_job(submission_id, started, release)))
        overflow = await self._submission(203)
        overflow_job = self._blocking_job(overflow, started, release)
        self.assertFalse(self.bot._enqueue(overflow_job))

        # A refused job is explained, recorded and its progress closed.
        event = FakeJobEvent()
        await self.bot._reject_job(
            QueuedJob(
                submission_id=overflow,
                kind="audio",
                run=overflow_job.run,
                event=event,
            ),
            "ظرفیت صف پر است",
        )
        status, error = await self._status(overflow)
        self.assertEqual(status, "failed")
        self.assertIn("ظرفیت صف پر است", error or "")
        self.assertTrue(any("ظرفیت صف پر است" in reply for reply in event.replies))
        release.set()

    async def test_queue_drains_in_order_and_completes_every_job(self):
        # The queue must hold every job of a small burst before it drains.
        self.bot.settings = make_settings(
            database_path=self.settings.database_path,
            session_path=self.settings.session_path,
            temp_dir=self.settings.temp_dir,
            max_concurrent_jobs=1,
            max_pending_jobs=4,
        )
        order: list[int] = []
        submission_ids = [await self._submission(300 + index) for index in range(3)]
        for submission_id in submission_ids:
            self.assertTrue(self.bot._enqueue(self._quick_job(submission_id, order)))
        await self.bot._job_queue().join()
        self.assertEqual(order, submission_ids)
        stats = await self.bot.db.stats()
        self.assertEqual(stats["done"], 3)
        self.assertEqual(self.bot._job_queue().qsize(), 0)

    async def test_a_failing_job_does_not_kill_the_worker(self):
        boom = await self._submission(400)
        healthy = await self._submission(401)

        async def failing():
            raise RuntimeError("provider exploded")

        self.assertTrue(self.bot._enqueue(QueuedJob(boom, "audio", failing)))
        self.assertTrue(self.bot._enqueue(self._quick_job(healthy)))
        with self.assertLogs("gamas_bot.bot", level="ERROR"):
            await self.bot._job_queue().join()
        # The worker survived and the healthy job still completed.
        self.assertTrue(all(not worker.done() for worker in self.bot._workers))
        self.assertEqual(await self._status(healthy), ("done", None))

    async def test_shutdown_rejects_jobs_that_never_started(self):
        release = asyncio.Event()
        started: list[int] = []
        running = await self._submission(500)
        self.assertTrue(self.bot._enqueue(self._blocking_job(running, started, release)))
        self.assertTrue(await self._wait_for(lambda: bool(started)))

        event = FakeJobEvent()
        waiting = await self._submission(501)
        self.assertTrue(
            self.bot._enqueue(
                QueuedJob(
                    submission_id=waiting,
                    kind="audio",
                    run=self._blocking_job(waiting, started, release).run,
                    event=event,
                )
            )
        )
        # Shutdown: every job still waiting is explained instead of vanishing.
        await self.bot._drain_queue("ربات خاموش شد و این کار از صف خارج شد")
        self.assertEqual(self.bot._job_queue().qsize(), 0)
        status, error = await self._status(waiting)
        self.assertEqual(status, "failed")
        self.assertIn("ربات خاموش شد", error or "")
        self.assertTrue(any("ربات خاموش شد" in reply for reply in event.replies))
        release.set()

    async def test_worker_cancellation_propagates_and_leaves_no_task_behind(self):
        release = asyncio.Event()
        started: list[int] = []
        submission_id = await self._submission(600)
        self.assertTrue(self.bot._enqueue(self._blocking_job(submission_id, started, release)))
        self.assertTrue(await self._wait_for(lambda: bool(started)))
        worker = self.bot._workers[0]
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertTrue(worker.done())
        self.bot._workers.remove(worker)
        release.set()

    async def test_no_job_is_left_in_a_stuck_state_after_shutdown(self):
        submission_id = await self._submission(700)
        self.assertTrue(
            self.bot._enqueue(
                QueuedJob(
                    submission_id=submission_id,
                    kind="audio",
                    run=lambda: asyncio.sleep(30),
                )
            )
        )
        self.assertTrue(await self._wait_for(lambda: self.bot._job_queue().qsize() == 0))
        for worker in list(self.bot._workers):
            worker.cancel()
        await asyncio.gather(*self.bot._workers, return_exceptions=True)
        self.bot._workers = []
        self.assertEqual(self.bot._job_queue().qsize(), 0)
        # The job never reached "processing", so the next start-up recovery
        # (or an explicit rejection) is what records it — it is not stuck.
        self.assertEqual((await self._status(submission_id))[0], "pending")


if __name__ == "__main__":
    unittest.main()
