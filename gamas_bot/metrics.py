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
import time
from typing import Any

SAMPLE_WINDOW = 512

#: Provider counter fields persisted across restarts. Gauges (``in_flight``),
#: per-process peaks and latency ring buffers are intentionally excluded.
PROVIDER_COUNTER_FIELDS: tuple[str, ...] = (
    "attempts",
    "successes",
    "failures",
    "rate_limits",
    "quota_exhaustions",
    "server_errors",
    "timeouts",
    "connection_errors",
    "auth_errors",
    "invalid_inputs",
    "retries",
    "cooldown_waits",
    "cancellations",
    "overflow_skips",
    "failovers_in",
    "failovers_out",
)

#: Flat counters persisted across restarts (provider counters are added by
#: the collector itself). Units are plain integer events, never prices or text.
FLAT_COUNTER_FIELDS: tuple[str, ...] = (
    "submitted_jobs",
    "deduplicated_updates",
    "db_errors",
    "curation_delivery_errors",
    "curation_source_missing",
    "support_ticket_denials",
    "support_reply_failures",
    "learning_validation_failures",
    "retention_errors",
    "broadcast_failures",
    "balance_repairs",
    "learning_capacity_denials",
    "document_unpack_rejections",
    "receipt_hash_errors",
)

#: Provider events that mean "something needs an operator"; the health screen
#: shows when the most recent one happened (wall clock).
PROVIDER_ERROR_EVENTS: frozenset[str] = frozenset(
    {
        "timeout",
        "rate_limit",
        "server_error",
        "connection_error",
        "auth_error",
        "quota_exhausted",
        "invalid_input",
    }
)

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
            else None
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
    """Return peak resident memory in bytes (0 where unavailable)."""
    if os.name == "posix" and PathLikeStatuses.available():
        hwm = PathLikeStatuses.vm_hwm_bytes()
        if hwm > 0:
            return hwm
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            if ok:
                return int(counters.PeakWorkingSetSize)
            return 0
        except Exception:  # pragma: no cover - Windows fallback
            return 0
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
            self._db_errors = 0
            self._curation_delivery_errors = 0
            self._curation_source_missing = 0
            self._support_ticket_denials = 0
            self._support_reply_failures = 0
            self._learning_validation_failures = 0
            self._retention_deletions = {"documents": 0, "learning": 0, "audit": 0, "holds": 0}
            self._retention_errors = 0
            self._broadcast_failures = 0
            self._balance_repairs = 0
            self._learning_capacity_denials = 0
            self._document_unpack_rejections = 0
            self._receipt_hash_errors = 0
            self._outcomes: dict[str, int] = dict.fromkeys(TERMINAL_OUTCOMES, 0)
            self._stage_active: dict[str, int] = dict.fromkeys(PIPELINE_STAGES, 0)
            self._stage_latency: dict[str, _Series] = {
                stage: _Series() for stage in PIPELINE_STAGES
            }
            self._queue_wait = _Series()
            self._end_to_end = _Series()
            self._providers: dict[tuple[str, str], _ProviderStats] = {}
            self._llm_operations: dict[tuple[str, str, str], dict[str, Any]] = {}
            # Cumulative totals restored at startup (empty until the first
            # orderly flush of a previous run completes).
            self._base_totals: dict[str, int] = {}
            self._last_db_error_at: float | None = None
            self._last_provider_error: dict[str, Any] | None = None

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

    def record_deduplicated(self) -> None:
        with self._lock:
            self._deduplicated_updates += 1

    def record_db_error(self) -> None:
        """Count one failed/strained SQLite operation (admin health panel)."""
        with self._lock:
            self._db_errors += 1
            self._last_db_error_at = time.time()

    def record_curation_delivery_error(self) -> None:
        with self._lock:
            self._curation_delivery_errors += 1

    def record_curation_source_missing(self) -> None:
        with self._lock:
            self._curation_source_missing += 1

    def record_learning_capacity_denial(self) -> None:
        with self._lock:
            self._learning_capacity_denials += 1

    def record_balance_repair(self) -> None:
        with self._lock:
            self._balance_repairs += 1

    def record_broadcast_failure(self) -> None:
        with self._lock:
            self._broadcast_failures += 1

    def record_retention_deleted(self, kind: str, count: int) -> None:
        with self._lock:
            if kind in self._retention_deletions:
                self._retention_deletions[kind] += max(0, int(count))

    def record_retention_error(self) -> None:
        with self._lock:
            self._retention_errors += 1

    def record_learning_validation_failure(self) -> None:
        with self._lock:
            self._learning_validation_failures += 1

    def record_support_reply_failure(self) -> None:
        with self._lock:
            self._support_reply_failures += 1

    def record_support_ticket_denial(self) -> None:
        with self._lock:
            self._support_ticket_denials += 1

    def record_document_unpack_rejection(self) -> None:
        with self._lock:
            self._document_unpack_rejections += 1

    def record_receipt_hash_error(self) -> None:
        with self._lock:
            self._receipt_hash_errors += 1

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
            if event in PROVIDER_ERROR_EVENTS:
                self._last_provider_error = {
                    "layer": str(layer)[:16],
                    "slug": str(slug)[:64],
                    "event": event,
                    "at": time.time(),
                }

    def record_llm_operation(
        self,
        *,
        task: str,
        provider: str,
        model: str,
        success: bool,
        failure_category: str = "",
        estimated_input_tokens: int = 0,
        reserved_output_tokens: int = 0,
        actual_total_tokens: int | None = None,
        duration_seconds: float = 0.0,
    ) -> None:
        """Aggregate privacy-safe operation counts by task/provider/model."""
        key = (
            (task or "UNSPECIFIED")[:48],
            (provider or "unknown")[:24],
            (model or "unknown")[:100],
        )
        with self._lock:
            # Keep the collector bounded even if a deployment rotates model ids.
            if key not in self._llm_operations and len(self._llm_operations) >= 128:
                self._llm_operations.pop(next(iter(self._llm_operations)))
            row = self._llm_operations.setdefault(key, {
                "requests": 0, "successes": 0, "failures": 0,
                "estimated_input_tokens": 0, "reserved_output_tokens": 0,
                "actual_total_tokens": 0, "actual_usage_count": 0,
                "failure_categories": {}, "latency": _Series(),
            })
            row["requests"] += 1
            row["successes" if success else "failures"] += 1
            row["estimated_input_tokens"] += max(0, int(estimated_input_tokens))
            row["reserved_output_tokens"] += max(0, int(reserved_output_tokens))
            if actual_total_tokens is not None:
                row["actual_total_tokens"] += max(0, int(actual_total_tokens))
                row["actual_usage_count"] += 1
            if failure_category:
                categories = row["failure_categories"]
                categories[failure_category[:64]] = categories.get(failure_category[:64], 0) + 1
            row["latency"].observe(duration_seconds)

    def record_failover(self, layer: str, from_slug: str, to_slug: str) -> None:
        with self._lock:
            self._provider(layer, from_slug).failovers_out += 1
            self._provider(layer, to_slug).failovers_in += 1

    def _flat_totals_unlocked(self) -> dict[str, int]:
        """Flatten every cumulative counter into persistable integer totals."""
        flat = {field: int(getattr(self, f"_{field}")) for field in FLAT_COUNTER_FIELDS}
        flat.update(
            {f"outcome.{name}": int(value) for name, value in self._outcomes.items()}
        )
        flat.update(
            {
                f"retention.{name}": int(value)
                for name, value in self._retention_deletions.items()
            }
        )
        for (layer, slug), stats in self._providers.items():
            for field_name in PROVIDER_COUNTER_FIELDS:
                flat[f"provider.{layer}.{slug}.{field_name}"] = int(
                    getattr(stats, field_name)
                )
        return flat

    @property
    def restored(self) -> bool:
        """True when totals from an earlier run were loaded at startup."""
        with self._lock:
            return any(self._base_totals.values())

    def export_totals(self) -> dict[str, int]:
        """Cumulative counters (restored base plus this run) for persistence.

        The store is absolute — rows are overwritten, never incremented — so a
        crash between flushes can lose at most the current run's delta, never
        double-count it.
        """
        with self._lock:
            return self._flat_totals_unlocked()

    def load_base(self, totals: dict[str, Any]) -> None:
        """Merge persisted cumulative counters into the live counters."""
        with self._lock:
            for key, raw in totals.items():
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    continue
                if value <= 0:
                    continue
                self._base_totals[key] = int(self._base_totals.get(key, 0)) + value
                if key in FLAT_COUNTER_FIELDS:
                    setattr(self, f"_{key}", int(getattr(self, f"_{key}")) + value)
                elif key.startswith("outcome."):
                    name = key.partition(".")[2]
                    if name in self._outcomes:
                        self._outcomes[name] += value
                elif key.startswith("retention."):
                    name = key.partition(".")[2]
                    if name in self._retention_deletions:
                        self._retention_deletions[name] += value
                elif key.startswith("provider."):
                    parts = key.split(".", 3)
                    if len(parts) != 4:
                        continue
                    _, layer, slug, field_name = parts
                    if field_name not in PROVIDER_COUNTER_FIELDS:
                        continue
                    stats = self._provider(layer, slug)
                    setattr(stats, field_name, int(getattr(stats, field_name)) + value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            providers_by_layer: dict[str, dict[str, Any]] = {}
            for (layer, slug), stats in sorted(self._providers.items()):
                providers_by_layer.setdefault(layer, {})[slug] = stats.summary()
            llm_operations = {
                "|".join(key): {
                    **{name: value for name, value in row.items() if name != "latency"},
                    "latency": row["latency"].summary(),
                }
                for key, row in self._llm_operations.items()
            }
            return {
                "llm_operations": llm_operations,
                "active_jobs": self._active_jobs,
                "peak_active_jobs": self._peak_active_jobs,
                "queued_jobs": self._queued_jobs,
                "peak_queued_jobs": self._peak_queued_jobs,
                "submitted_jobs": self._submitted_jobs,
                "deduplicated_updates": self._deduplicated_updates,
                "db_errors": self._db_errors,
                "curation_delivery_errors": self._curation_delivery_errors,
                "curation_source_missing": self._curation_source_missing,
                "support_ticket_denials": self._support_ticket_denials,
                "support_reply_failures": self._support_reply_failures,
                "learning_validation_failures": self._learning_validation_failures,
                "retention_deletions": dict(self._retention_deletions),
                "retention_errors": self._retention_errors,
                "broadcast_failures": self._broadcast_failures,
                "balance_repairs": self._balance_repairs,
                "learning_capacity_denials": self._learning_capacity_denials,
                "document_unpack_rejections": self._document_unpack_rejections,
                "receipt_hash_errors": self._receipt_hash_errors,
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
                "metrics_restored": any(self._base_totals.values()),
                "last_db_error_at": self._last_db_error_at,
                "last_provider_error": (
                    dict(self._last_provider_error)
                    if self._last_provider_error is not None
                    else None
                ),
            }


METRICS = MetricsCollector()
