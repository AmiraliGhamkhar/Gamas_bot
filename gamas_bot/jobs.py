"""The bounded job queue: fixed workers, hard back-pressure, clean shutdown.

Ported semantics from v2, extended with per-job lifecycle state isolation and
stage telemetry:

* every job receives a unique ``job_id`` and isolated per-job state/timing;
* explicit lifecycle states: ``queued``, ``downloading``, ``extracting``,
  ``transcribing``, ``generating_notes``, ``uploading``, ``completed``,
  ``failed``, ``cancelled``;
* bounded pending capacity — a full queue is rejected with an explicit
  back-pressure message instead of growing an unbounded in-memory backlog;
* a second semaphore bound on *active* jobs, so a job started outside the
  worker pool still cannot exceed the configured concurrency;
* one user's backlog never delays another's: dispatch is round-robin across
  users while each user's own jobs keep their order;
* shutdown stops intake first, transitions queued jobs cleanly via ``on_reject``,
  then cancels workers — a job is either fully recorded or cleanly terminated,
  never silently leaked.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .logging_config import log_job_id
from .metrics import METRICS

logger = logging.getLogger(__name__)

JOB_STATES = (
    "queued",
    "downloading",
    "extracting",
    "transcribing",
    "generating_notes",
    "uploading",
    "completed",
    "failed",
    "cancelled",
)

TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})

ACTIVE_STAGE_STATES = frozenset(
    {"queued", "downloading", "extracting", "transcribing", "generating_notes", "uploading"}
)

JOB_STATE_LABELS_FA: dict[str, str] = {
    "queued": "در صف انتظار",
    "downloading": "دریافت فایل از تلگرام",
    "extracting": "استخراج و بهینه‌سازی صدا",
    "transcribing": "تبدیل گفتار به متن",
    "generating_notes": "نگارش هوشمند جزوه",
    "uploading": "ساخت فایل نهایی و ارسال",
    "completed": "تکمیل شد",
    "failed": "ناموفق",
    "cancelled": "لغو شد",
}


def new_job_id(submission_id: int) -> str:
    """Return a unique, URL/log-safe identifier for one pipeline execution."""
    return f"job-{submission_id}-{uuid.uuid4().hex[:8]}"


class QueueFullError(RuntimeError):
    """The pending queue is at capacity; the upload was not accepted."""


@dataclass(slots=True)
class Job:
    """One isolated unit of work moving through the pipeline."""

    submission_id: int
    user_id: int
    kind: str  # audio | video | presentation
    filename: str | None
    message: Any = None  # the Telethon message that carried the upload
    is_admin: bool = False
    duration_hint: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    job_id: str = ""
    state: str = "queued"
    created_monotonic: float = field(default_factory=time.monotonic)
    started_monotonic: float | None = None
    finished_monotonic: float | None = None
    stage_started_monotonic: float = field(default_factory=time.monotonic)
    stage_durations: dict[str, float] = field(default_factory=dict)
    transitions: list[tuple[str, float]] = field(default_factory=list)
    _metrics_stage: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.job_id:
            self.job_id = new_job_id(self.submission_id)
        if self.state not in JOB_STATES:
            raise ValueError(f"Unknown job state: {self.state!r}")
        if not self.transitions:
            self.transitions.append((self.state, self.created_monotonic))

    def transition_to(self, new_state: str) -> float:
        """Transition this job to *new_state* and record stage elapsed time."""
        if new_state not in JOB_STATES:
            raise ValueError(f"Unknown job state: {new_state!r}")
        now = time.monotonic()
        prev_state = self.state
        elapsed = max(0.0, now - self.stage_started_monotonic)
        if prev_state in ACTIVE_STAGE_STATES:
            self.stage_durations[prev_state] = (
                self.stage_durations.get(prev_state, 0.0) + elapsed
            )
        if self._metrics_stage is not None:
            METRICS.record_stage_transition(self._metrics_stage, None, elapsed)
            self._metrics_stage = None

        self.state = new_state
        self.stage_started_monotonic = now
        self.transitions.append((new_state, now))

        if new_state in ACTIVE_STAGE_STATES and new_state != "queued":
            METRICS.record_stage_transition(None, new_state)
            self._metrics_stage = new_state
        elif new_state in TERMINAL_JOB_STATES:
            self.finished_monotonic = now

        logger.info(
            "job_state_transition job_id=%s submission=%s prev=%s state=%s prev_elapsed_s=%.3f",
            self.job_id,
            self.submission_id,
            prev_state,
            new_state,
            elapsed,
        )
        return elapsed

    def release_active_stage(self) -> None:
        """Idempotently close any open non-terminal stage metric on teardown."""
        if self._metrics_stage is not None:
            elapsed = max(0.0, time.monotonic() - self.stage_started_monotonic)
            METRICS.record_stage_transition(self._metrics_stage, None, elapsed)
            self._metrics_stage = None


class JobQueue:
    """Bounded round-robin queue of jobs with a fixed worker pool."""

    def __init__(
        self,
        *,
        max_queued: int,
        max_active: int,
        worker: Callable[[Job], Awaitable[None]],
    ):
        if max_active < 1:
            raise ValueError("max_active must be >= 1")
        if max_queued < 1:
            raise ValueError("max_queued must be >= 1")
        self._max_queued = max_queued
        self._max_active = max_active
        self._worker = worker
        self._workers: list[asyncio.Task[None]] = []
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
        #: Live in-flight jobs keyed by unique job_id; cleaned in finally blocks.
        self._in_flight: dict[str, Job] = {}
        self._arrived = asyncio.Event()

    @property
    def pending(self) -> int:
        return self._pending

    @property
    def active_count(self) -> int:
        return len(self._in_flight)

    def active_snapshot(self) -> dict[str, dict[str, Any]]:
        """Point-in-time read-only view of all active jobs."""
        now = time.monotonic()
        return {
            jid: {
                "submission_id": job.submission_id,
                "kind": job.kind,
                "state": job.state,
                "state_fa": JOB_STATE_LABELS_FA.get(job.state, job.state),
                "elapsed_seconds": round(
                    max(0.0, now - (job.started_monotonic or job.created_monotonic)),
                    3,
                ),
            }
            for jid, job in list(self._in_flight.items())
        }

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
            METRICS.record_job_rejected()
            raise QueueFullError("ربات در حال خاموش شدن است؛ لطفاً بعداً دوباره امتحان کنید.")
        if self._pending >= self._max_queued:
            METRICS.record_job_rejected()
            raise QueueFullError(
                "صف پردازش پر است؛ لطفاً چند لحظه بعد دوباره بفرستید."
            )
        now = time.monotonic()
        job.created_monotonic = now
        job.stage_started_monotonic = now
        job.state = "queued"
        queue = self._queues.get(job.user_id)
        if queue is None:
            queue = self._queues[job.user_id] = deque()
            self._rotation.append(job.user_id)
        queue.append(job)
        self._pending += 1
        METRICS.record_job_queued(self._pending)
        logger.info(
            "job_queued job_id=%s submission=%s kind=%s pending=%d/%d",
            job.job_id,
            job.submission_id,
            job.kind,
            self._pending,
            self._max_queued,
        )
        self._arrived.set()

    def pop_pending(self) -> Job:
        """Take the next job a worker would take, without a worker."""
        if not self._pending:
            raise RuntimeError("no pending job")
        return self._pop()

    def _pop(self) -> Job:
        """The next job in round-robin order (the caller ensures one exists)."""
        user_id = next(
            (other for other in self._rotation if not self._running.get(other)),
            self._rotation[0],
        )
        self._rotation.remove(user_id)
        queue = self._queues[user_id]
        job = queue.popleft()
        if queue:
            self._rotation.append(user_id)
        else:
            del self._queues[user_id]
        self._pending -= 1
        if not self._pending:
            self._arrived.clear()
        return job

    def _yield_turn(self, user_id: int) -> None:
        """Move a user to the back of the rotation after one of their jobs."""
        if user_id in self._rotation and self._queues.get(user_id):
            self._rotation.remove(user_id)
            self._rotation.append(user_id)

    async def _take(self) -> Job:
        while True:
            if self._pending:
                return self._pop()
            self._arrived.clear()
            if self._pending:
                continue
            await self._arrived.wait()

    async def _drain(self) -> None:
        while True:
            job = await self._take()
            async with self._active:
                started = time.monotonic()
                queue_wait = max(0.0, started - job.created_monotonic)
                job.started_monotonic = started
                self._running[job.user_id] = self._running.get(job.user_id, 0) + 1
                self._in_flight[job.job_id] = job
                METRICS.record_job_started(
                    queue_wait_seconds=queue_wait, queued_now=self._pending
                )
                token = log_job_id.set(job.job_id)
                outcome = "completed"
                try:
                    await self._worker(job)
                    if job.state not in TERMINAL_JOB_STATES:
                        job.transition_to("completed")
                    requested_outcome = job.meta.get("outcome")
                    outcome = (
                        requested_outcome
                        if requested_outcome in {"partial", "completed"}
                        else job.state if job.state in TERMINAL_JOB_STATES else "completed"
                    )
                except asyncio.CancelledError:
                    if job.state not in TERMINAL_JOB_STATES:
                        job.transition_to("cancelled")
                    outcome = "cancelled"
                    raise
                except Exception:
                    if job.state not in TERMINAL_JOB_STATES:
                        job.transition_to("failed")
                    outcome = "failed"
                    logger.exception(
                        "job worker failed job_id=%s submission=%s",
                        job.job_id,
                        job.submission_id,
                    )
                finally:
                    job.release_active_stage()
                    self._in_flight.pop(job.job_id, None)
                    remaining = self._running.get(job.user_id, 1) - 1
                    if remaining > 0:
                        self._running[job.user_id] = remaining
                    else:
                        self._running.pop(job.user_id, None)
                    self._yield_turn(job.user_id)
                    METRICS.record_job_finished(
                        outcome,
                        end_to_end_seconds=max(0.0, time.monotonic() - started),
                    )
                    log_job_id.reset(token)

    async def shutdown(self, on_reject: Callable[[Job], Awaitable[None]] | None = None) -> None:
        """Stop intake, reject queued jobs, cancel workers cleanly."""
        self._closed = True
        rejected = 0
        pending_rejects: list[Job] = []
        while self._pending:
            job = self._pop()
            rejected += 1
            pending_rejects.append(job)
            logger.warning(
                "rejected queued job at shutdown job_id=%s submission=%s",
                job.job_id,
                job.submission_id,
            )
        for job in pending_rejects:
            token = log_job_id.set(job.job_id)
            try:
                job.transition_to("cancelled")
                if on_reject is not None:
                    await on_reject(job)
            except Exception:
                logger.exception(
                    "on_reject failed job_id=%s submission=%s",
                    job.job_id,
                    job.submission_id,
                )
            finally:
                job.release_active_stage()
                METRICS.record_queued_cancelled(
                    queue_wait_seconds=max(0.0, time.monotonic() - job.created_monotonic),
                    queued_now=self._pending,
                )
                log_job_id.reset(token)
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers = []
        if rejected:
            logger.info("shutdown rejected %d queued jobs", rejected)
