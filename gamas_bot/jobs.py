"""The bounded job queue: fixed workers, hard back-pressure, clean shutdown.

Ported semantics from v2 (they were right):

* bounded pending capacity — a full queue is rejected with an explicit
  back-pressure message instead of growing an unbounded in-memory backlog;
* a second semaphore bound on *active* jobs, so a job started outside the
  worker pool still cannot exceed the configured concurrency;
* one user's backlog never delays another's: dispatch is round-robin across
  users while each user's own jobs keep their order. A per-user admission cap
  of 2 and three workers meant two long uploads from one user could occupy the
  whole pool for hours with everyone else's jobs waiting behind them (audit F-8);
* shutdown stops intake first, rejects whatever is still pending, then cancels
  workers — a job is either fully recorded or fully failed, never silently
  dropped with a ``processing`` row left behind.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
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
    """Bounded round-robin queue of jobs with a fixed worker pool."""

    def __init__(self, *, max_queued: int, max_active: int, worker: Callable[[Job], Awaitable[None]]):
        if max_active < 1:
            raise ValueError("max_active must be >= 1")
        if max_queued < 1:
            # A pending capacity of 0 would silently remove the back-pressure
            # this class exists to provide. Refuse it like ``max_active``.
            raise ValueError("max_queued must be >= 1")
        self._max_queued = max_queued
        self._max_active = max_active
        self._worker = worker
        self._workers: list[asyncio.Task] = []
        self._active = asyncio.Semaphore(max_active)
        self._closed = False
        #: Per-user pending jobs, oldest first: a user's own order is kept.
        self._queues: dict[int, deque[Job]] = {}
        #: Users with pending jobs, rotated on every dispatch.
        self._rotation: deque[int] = deque()
        self._pending = 0
        #: Jobs each user currently has in flight (a user with none is served
        #: first, which is what keeps a second upload from jumping the queue).
        self._running: dict[int, int] = {}
        self._arrived = asyncio.Event()

    @property
    def pending(self) -> int:
        return self._pending

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
        if self._pending >= self._max_queued:
            raise QueueFullError(
                "صف پردازش پر است؛ لطفاً چند لحظه بعد دوباره بفرستید."
            )
        queue = self._queues.get(job.user_id)
        if queue is None:
            queue = self._queues[job.user_id] = deque()
            self._rotation.append(job.user_id)
        queue.append(job)
        self._pending += 1
        self._arrived.set()

    def pop_pending(self) -> Job:
        """Take the next job a worker would take, without a worker.

        Used by tests and by tooling that drains the queue in-process; it is
        the same rotation a worker follows, so no ordering rule is bypassed.
        """
        if not self._pending:
            raise RuntimeError("no pending job")
        return self._pop()

    def _pop(self) -> Job:
        """The next job in round-robin order (the caller ensures one exists).

        A user with nothing in flight is served before a user who already has
        a job running, then the rotation decides: that is what keeps one
        person's backlog from holding the workers (audit F-8).
        """
        user_id = next(
            (other for other in self._rotation if not self._running.get(other)),
            self._rotation[0],
        )
        self._rotation.remove(user_id)
        queue = self._queues[user_id]
        job = queue.popleft()
        if queue:
            # This user still has work: go to the back of the rotation so the
            # other users' jobs run first next time.
            self._rotation.append(user_id)
        else:
            del self._queues[user_id]
        self._pending -= 1
        if not self._pending:
            self._arrived.clear()
        return job

    def _yield_turn(self, user_id: int) -> None:
        """Move a user to the back of the rotation after one of their jobs.

        Without this a user whose job has just finished would be served again
        immediately (they are at the front of the rotation), which is exactly
        the monopolisation the round robin exists to prevent.
        """
        if user_id in self._rotation and self._queues.get(user_id):
            self._rotation.remove(user_id)
            self._rotation.append(user_id)

    async def _take(self) -> Job:
        while True:
            if self._pending:
                return self._pop()
            self._arrived.clear()
            # There is no ``await`` between the clear and this second check, so
            # a job submitted in between cannot be lost behind a cleared event.
            if self._pending:
                continue
            await self._arrived.wait()

    async def _drain(self) -> None:
        while True:
            job = await self._take()
            async with self._active:
                self._running[job.user_id] = self._running.get(job.user_id, 0) + 1
                try:
                    await self._worker(job)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "job worker failed submission=%s", job.submission_id
                    )
                finally:
                    remaining = self._running.get(job.user_id, 1) - 1
                    if remaining > 0:
                        self._running[job.user_id] = remaining
                    else:
                        self._running.pop(job.user_id, None)
                    self._yield_turn(job.user_id)

    async def shutdown(self, on_reject: Callable[[Job], Awaitable[None]] | None = None) -> None:
        """Stop intake, reject queued jobs, cancel workers.

        If *on_reject* is provided, it is awaited for each queued job so the
        caller can transition DB rows / notify users (audit F-H04). The queue
        promise — 'a job is either fully recorded or fully failed' — is thus
        kept even for jobs that never reached a worker.
        """
        self._closed = True
        rejected = 0
        pending_rejects: list[Job] = []
        while self._pending:
            job = self._pop()
            rejected += 1
            pending_rejects.append(job)
            logger.warning("rejected queued job at shutdown submission=%s", job.submission_id)
        if on_reject is not None and pending_rejects:
            for job in pending_rejects:
                try:
                    await on_reject(job)
                except Exception:
                    logger.exception("on_reject failed for submission=%s", job.submission_id)
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers = []
        if rejected:
            logger.info("shutdown rejected %d queued jobs", rejected)
