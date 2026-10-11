"""The bounded job queue: fixed workers, hard back-pressure, clean shutdown.

Ported semantics from v2 (they were right):

* ``asyncio.Queue(maxsize=MAX_QUEUED_JOBS)`` drained by ``MAX_ACTIVE_JOBS``
  workers — a full queue is rejected with an explicit back-pressure message
  instead of growing an unbounded in-memory backlog;
* a second semaphore bound on *active* jobs, so a job started outside the
  worker pool still cannot exceed the configured concurrency;
* shutdown stops intake first, rejects whatever is still queued, then cancels
  workers — a job is either fully recorded or fully failed, never silently
  dropped with a ``processing`` row left behind.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class QueueFullError(RuntimeError):
    """The pending queue is at capacity; the upload was not accepted."""


@dataclass(slots=True)
class Job:
    """One unit of work moving through the pipeline."""

    submission_id: int
    user_id: int
    kind: str                      # audio | video | presentation
    filename: str | None
    message: Any = None            # the Telethon message that carried the upload
    is_admin: bool = False
    duration_hint: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class JobQueue:
    """Bounded FIFO of jobs with a fixed worker pool."""

    def __init__(self, *, max_queued: int, max_active: int, worker: Callable[[Job], Awaitable[None]]):
        if max_active < 1:
            raise ValueError("max_active must be >= 1")
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=max(0, max_queued))
        self._max_active = max_active
        self._worker = worker
        self._workers: list[asyncio.Task] = []
        self._active = asyncio.Semaphore(max_active)
        self._closed = False

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def active_possible(self) -> int:
        return self._max_active

    def ensure_workers(self) -> None:
        """Start the fixed worker pool once (idempotent)."""
        if self._workers or self._closed:
            return
        for index in range(self._max_active):
            self._workers.append(
                asyncio.create_task(self._drain(), name=f"job-worker-{index + 1}")
            )

    def submit(self, job: Job) -> None:
        """Accept a job or raise :class:`QueueFullError` (never block, never grow)."""
        if self._closed:
            raise QueueFullError("ربات در حال خاموش شدن است؛ لطفاً بعداً دوباره امتحان کنید.")
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            raise QueueFullError(
                "صف پردازش پر است؛ لطفاً چند لحظه بعد دوباره بفرستید."
            ) from None

    async def _drain(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                async with self._active:
                    try:
                        await self._worker(job)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "job worker failed submission=%s", job.submission_id
                        )
            finally:
                self._queue.task_done()

    async def shutdown(self) -> None:
        """Stop intake, reject queued jobs, cancel workers."""
        self._closed = True
        rejected = 0
        while not self._queue.empty():
            try:
                job = self._queue.get_nowait()
                rejected += 1
                self._queue.task_done()
                logger.warning("rejected queued job at shutdown submission=%s", job.submission_id)
            except asyncio.QueueEmpty:
                break
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers = []
        if rejected:
            logger.info("shutdown rejected %d queued jobs", rejected)
