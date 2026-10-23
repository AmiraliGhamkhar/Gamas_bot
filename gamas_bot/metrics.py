"""Lightweight, bounded in-process observability for pipeline concurrency and failover.

Shared hosting leaves no room for a metrics daemon or unbounded time series.
Counters are plain integers and latency samples live in fixed-capacity ring
buffers (512 floats per series, a few kilobytes total).  Only stage names,
provider slugs, status codes and durations are accepted — never prompts,
transcripts, filenames, user identifiers or API keys.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import os
import sys
import threading
from typing import Any

SAMPLE_WINDOW = 512

PIPELINE_STAGES: tuple[str, ...] = (
    "queued",
    "downloading",
    "extracting",
    "transcribing",
    "generating_notes",
    "uploading",
)

TERMINAL_OUTCOMES: tuple[str, ...] = (
    "completed",
    "partial",
    "failed",
    "rejected",
    "cancelled",
)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(
        len(sorted_values) - 1,
        max(0, math.ceil(fraction * len(sorted_values)) - 1),
    )
    return sorted_values[index]


@dataclass
class _Series:
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=SAMPLE_WINDOW))

    def observe(self, value: float) -> None:
        clean = max(0.0, float(value))
        self.count += 1
        self.total += clean
        if clean > self.maximum:
            self.maximum = clean
        self.samples.append(clean)

    def summary(self) -> dict[str, float | int]:
        if not self.count:
            return {
                "count": 0,
                "total_seconds": 0.0,
                "mean_seconds": 0.0,
                "p50_seconds": 0.0,
                "p95_seconds": 0.0,
                "max_seconds": 0.0,
            }
        ordered = sorted(self.samples)
        return {
            "count": self.count,
            "total_seconds": round(self.total, 4),
            "mean_seconds": round(self.total / self.count, 4),
            "p50_seconds": round(_percentile(ordered, 0.50), 4),
            "p95_seconds": round(_percentile(ordered, 0.95), 4),
            "max_seconds": round(self.maximum, 4),
        }


@dataclass
class _ProviderStats:
    in_flight: int = 0
    peak_in_flight: int = 0
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    rate_limits: int = 0
    quota_exhaustions: int = 0
    server_errors: int = 0
    timeouts: int = 0
    connection_errors: int = 0
    auth_errors: int = 0
    invalid_inputs: int = 0
    retries: int = 0
    cooldown_waits: int = 0
    cancellations: int = 0
    overflow_skips: int = 0
    failovers_out: int = 0
    failovers_in: int = 0
    latency: _Series = field(default_factory=_Series)

    def summary(self) -> dict[str, Any]:
        completed_attempts = self.successes + self.failures
        success_rate = (
            round(self.successes / completed_attempts, 4)
            if completed_attempts > 0
            else 1.0
        )
        return {
            "in_flight": self.in_flight,
            "peak_in_flight": self.peak_in_flight,
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": success_rate,
            "rate_limits": self.rate_limits,
            "quota_exhaustions": self.quota_exhaustions,
            "server_errors": self.server_errors,
            "timeouts": self.timeouts,
            "connection_errors": self.connection_errors,
            "auth_errors": self.auth_errors,
            "invalid_inputs": self.invalid_inputs,
            "retries": self.retries,
            "cooldown_waits": self.cooldown_waits,
            "cancellations": self.cancellations,
            "overflow_skips": self.overflow_skips,
            "failovers_out": self.failovers_out,
            "failovers_in": self.failovers_in,
            "latency": self.latency.summary(),
        }


def peak_rss_bytes() -> int:
    """Return peak resident memory in bytes on POSIX/Linux (0 where unavailable)."""
    if os.name == "posix" and PathLikeStatuses.available():
        hwm = PathLikeStatuses.vm_hwm_bytes()
        if hwm > 0:
            return hwm
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; Linux/BSD report KiB.
        return int(usage) if sys.platform == "darwin" else int(usage) * 1024
    except Exception:  # pragma: no cover - non-POSIX fallback
        return 0


class PathLikeStatuses:
    """Read Linux ``/proc/self/status`` without importing heavy process libraries."""

    _STATUS_PATH = "/proc/self/status"

    @classmethod
    def available(cls) -> bool:
        return os.path.exists(cls._STATUS_PATH)

    @classmethod
    def vm_hwm_bytes(cls) -> int:
        try:
            with open(cls._STATUS_PATH, encoding="ascii", errors="ignore") as handle:
                for line in handle:
                    if line.startswith("VmHWM:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1]) * 1024
        except OSError:
            return 0
        return 0


class MetricsCollector:
    """Process-wide, bounded telemetry collector safe across threads and tasks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._active_jobs = 0
            self._peak_active_jobs = 0
            self._queued_jobs = 0
            self._peak_queued_jobs = 0
            self._submitted_jobs = 0
            self._deduplicated_updates = 0
            self._outcomes: dict[str, int] = dict.fromkeys(TERMINAL_OUTCOMES, 0)
            self._stage_active: dict[str, int] = dict.fromkeys(PIPELINE_STAGES, 0)
            self._stage_latency: dict[str, _Series] = {
                stage: _Series() for stage in PIPELINE_STAGES
            }
            self._queue_wait = _Series()
            self._end_to_end = _Series()
            self._providers: dict[tuple[str, str], _ProviderStats] = {}

    def _provider(self, layer: str, slug: str) -> _ProviderStats:
        key = (layer, slug)
        stats = self._providers.get(key)
        if stats is None:
            stats = _ProviderStats()
            self._providers[key] = stats
        return stats

    def record_job_queued(self, queued_now: int) -> None:
        with self._lock:
            self._submitted_jobs += 1
            self._queued_jobs = max(0, queued_now)
            if self._queued_jobs > self._peak_queued_jobs:
                self._peak_queued_jobs = self._queued_jobs

    def update_queue_depth(self, queued_now: int) -> None:
        with self._lock:
            self._queued_jobs = max(0, queued_now)
            if self._queued_jobs > self._peak_queued_jobs:
                self._peak_queued_jobs = self._queued_jobs

    def record_deduplicated(self) -> None:
        with self._lock:
            self._deduplicated_updates += 1

    def record_job_started(self, *, queue_wait_seconds: float, queued_now: int) -> None:
        with self._lock:
            self._queued_jobs = max(0, queued_now)
            self._active_jobs += 1
            if self._active_jobs > self._peak_active_jobs:
                self._peak_active_jobs = self._active_jobs
            self._queue_wait.observe(queue_wait_seconds)

    def record_stage_transition(
        self,
        previous_stage: str | None,
        new_stage: str | None,
        elapsed_in_previous: float | None = None,
    ) -> None:
        with self._lock:
            if previous_stage and previous_stage in self._stage_active:
                self._stage_active[previous_stage] = max(
                    0, self._stage_active[previous_stage] - 1
                )
                if elapsed_in_previous is not None:
                    self._stage_latency[previous_stage].observe(elapsed_in_previous)
            if new_stage and new_stage in self._stage_active:
                self._stage_active[new_stage] += 1

    def record_job_finished(
        self,
        outcome: str,
        *,
        end_to_end_seconds: float | None = None,
    ) -> None:
        with self._lock:
            self._active_jobs = max(0, self._active_jobs - 1)
            bucket = outcome if outcome in self._outcomes else "failed"
            self._outcomes[bucket] += 1
            if end_to_end_seconds is not None:
                self._end_to_end.observe(end_to_end_seconds)

    def record_job_rejected(self) -> None:
        with self._lock:
            self._outcomes["rejected"] += 1

    def record_queued_cancelled(
        self, *, queue_wait_seconds: float = 0.0, queued_now: int = 0
    ) -> None:
        with self._lock:
            self._queued_jobs = max(0, queued_now)
            self._outcomes["cancelled"] += 1
            self._queue_wait.observe(max(0.0, queue_wait_seconds))

    def record_provider_acquire(self, layer: str, slug: str) -> None:
        with self._lock:
            stats = self._provider(layer, slug)
            stats.in_flight += 1
            stats.attempts += 1
            if stats.in_flight > stats.peak_in_flight:
                stats.peak_in_flight = stats.in_flight

    def record_provider_release(
        self,
        layer: str,
        slug: str,
        *,
        elapsed_seconds: float,
        ok: bool,
        cancelled: bool = False,
    ) -> None:
        with self._lock:
            stats = self._provider(layer, slug)
            stats.in_flight = max(0, stats.in_flight - 1)
            stats.latency.observe(elapsed_seconds)
            if cancelled:
                stats.cancellations += 1
            elif ok:
                stats.successes += 1
            else:
                stats.failures += 1

    def record_provider_event(
        self,
        layer: str,
        slug: str,
        event: str,
    ) -> None:
        with self._lock:
            stats = self._provider(layer, slug)
            if event == "rate_limit":
                stats.rate_limits += 1
            elif event == "quota_exhausted":
                stats.quota_exhaustions += 1
            elif event == "server_error":
                stats.server_errors += 1
            elif event == "timeout":
                stats.timeouts += 1
            elif event == "connection_error":
                stats.connection_errors += 1
            elif event == "auth_error":
                stats.auth_errors += 1
            elif event == "invalid_input":
                stats.invalid_inputs += 1
            elif event == "retry":
                stats.retries += 1
            elif event == "cooldown_wait":
                stats.cooldown_waits += 1
            elif event == "overflow_skip":
                stats.overflow_skips += 1

    def record_failover(self, layer: str, from_slug: str, to_slug: str) -> None:
        with self._lock:
            self._provider(layer, from_slug).failovers_out += 1
            self._provider(layer, to_slug).failovers_in += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            providers_by_layer: dict[str, dict[str, Any]] = {}
            for (layer, slug), stats in sorted(self._providers.items()):
                providers_by_layer.setdefault(layer, {})[slug] = stats.summary()
            return {
                "active_jobs": self._active_jobs,
                "peak_active_jobs": self._peak_active_jobs,
                "queued_jobs": self._queued_jobs,
                "peak_queued_jobs": self._peak_queued_jobs,
                "submitted_jobs": self._submitted_jobs,
                "deduplicated_updates": self._deduplicated_updates,
                "outcomes": dict(self._outcomes),
                "stage_active": dict(self._stage_active),
                "queue_wait": self._queue_wait.summary(),
                "end_to_end": self._end_to_end.summary(),
                "stages": {
                    stage: series.summary()
                    for stage, series in self._stage_latency.items()
                },
                "providers": providers_by_layer,
                "peak_rss_mb": round(peak_rss_bytes() / (1024 * 1024), 2),
            }


METRICS = MetricsCollector()
