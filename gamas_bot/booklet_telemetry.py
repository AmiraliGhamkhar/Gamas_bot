"""Failure-safe, job-local counters for the booklet generation pipeline.

Telemetry is operational metadata only: it is carried in a context variable,
never persisted in the StructuredNotes contract, and every accessor swallows
its own errors. A broken metric must not be able to reject or shorten a booklet.
"""
from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypeVar, cast

from .llm_quota import estimate_tokens

logger = logging.getLogger(__name__)

_TASK_FIELDS = {
    "BOOKLET_PLAN": "planner_requests",
    "BOOKLET_GENERATION": "map_request_count",
    "BOOKLET_COMPILE": "synthesis_requests",
    "BOOKLET_VERIFY": "semantic_verification_requests",
    "BOOKLET_REPAIR": "repair_requests",
}

_F = TypeVar("_F", bound=Callable[..., Any])


def _failure_safe(fn: _F) -> _F:
    """Never let observability change a booklet's outcome."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - telemetry is strictly best-effort
            logger.debug("booklet telemetry ignored error=%s", type(exc).__name__)
            return None

    return cast(_F, wrapper)


@dataclass(slots=True)
class BookletTelemetry:
    """One booklet job's request and output counters (no source text)."""

    job_id: str = ""
    source_chars: int = 0
    estimated_source_tokens: int = 0
    semantic_unit_count: int = 0
    packed_unit_count: int = 0
    map_request_count: int = 0
    map_retry_count: int = 0
    planner_requests: int = 0
    synthesis_requests: int = 0
    semantic_verification_requests: int = 0
    repair_requests: int = 0
    total_llm_requests: int = 0
    wire_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_input_tokens: int = 0
    final_booklet_chars: int = 0
    final_booklet_sections: int = 0
    quality_state: str = ""
    failure_reason: str = ""
    recovery_reason: str = ""
    notes: dict[str, str] = field(default_factory=dict)

    @_failure_safe
    def record_source(self, text: str, *, chars_per_token: float = 1.5) -> None:
        self.source_chars = len(text or "")
        self.estimated_source_tokens = estimate_tokens(text or "", chars_per_token=chars_per_token)

    @_failure_safe
    def record_units(self, semantic: int, packed: int) -> None:
        self.semantic_unit_count = int(semantic)
        self.packed_unit_count = int(packed)

    @_failure_safe
    def record_call(self, task: str, *, system: str = "", user: str = "", chars_per_token: float = 1.5) -> None:
        name = str(task or "").strip().upper()
        field_name = _TASK_FIELDS.get(name)
        if field_name is not None:
            setattr(self, field_name, getattr(self, field_name) + 1)
        self.total_llm_requests += 1
        self.estimated_input_tokens += estimate_tokens(
            f"{system}\n{user}", chars_per_token=chars_per_token
        )

    @_failure_safe
    def record_wire(self, stage: str, attempts: int) -> None:
        count = max(0, int(attempts))
        self.wire_attempts += count
        if str(stage or "").strip().upper() == "BOOKLET_GENERATION":
            self.map_retry_count += max(0, count - 1)

    @_failure_safe
    def record_provider_tokens(self, input_tokens: int | None, output_tokens: int | None) -> None:
        if isinstance(input_tokens, int) and input_tokens >= 0:
            self.input_tokens += input_tokens
        if isinstance(output_tokens, int) and output_tokens >= 0:
            self.output_tokens += output_tokens

    @_failure_safe
    def record_result(self, notes: Any) -> None:
        payload = notes.to_json() if notes is not None and hasattr(notes, "to_json") else ""
        self.final_booklet_chars = len(payload or "")
        self.final_booklet_sections = len(getattr(notes, "sections", ()) or ())
        self.quality_state = str(getattr(notes, "quality_state", "") or "")
        if not self.recovery_reason:
            recoveries = getattr(notes, "semantic_recoveries", ()) or ()
            failures = getattr(notes, "failed_sections", ()) or ()
            if recoveries:
                self.recovery_reason = str(getattr(recoveries[0], "reason", "source_split"))
            elif failures and getattr(failures[0], "recovered", False):
                self.recovery_reason = "source_node_fallback"
            elif self.quality_state == "REPAIRED":
                self.recovery_reason = "targeted_repair"

    @_failure_safe
    def fail(self, reason: str) -> None:
        if not self.failure_reason:
            self.failure_reason = str(reason or "unknown")[:120]

    @_failure_safe
    def note(self, key: str, value: str) -> None:
        self.notes[str(key)[:40]] = str(value)[:120]

    @_failure_safe
    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_chars": self.source_chars,
            "estimated_source_tokens": self.estimated_source_tokens,
            "semantic_unit_count": self.semantic_unit_count,
            "packed_unit_count": self.packed_unit_count,
            "map_requests": self.map_request_count,
            "map_retries": self.map_retry_count,
            "planner_requests": self.planner_requests,
            "synthesis_requests": self.synthesis_requests,
            "semantic_verification_requests": self.semantic_verification_requests,
            "repair_requests": self.repair_requests,
            "total_llm_requests": self.total_llm_requests,
            "wire_attempts": self.wire_attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "final_booklet_chars": self.final_booklet_chars,
            "final_booklet_sections": self.final_booklet_sections,
            "quality_state": self.quality_state,
            "failure_reason": self.failure_reason,
            "recovery_reason": self.recovery_reason,
            "notes": dict(self.notes),
        }

    @_failure_safe
    def log_summary(self) -> None:
        row = self.snapshot()
        reasons = dict(row.pop("notes"))
        if self.failure_reason:
            reasons["failure"] = self.failure_reason
        if self.recovery_reason:
            reasons["recovery"] = self.recovery_reason
        logger.info(
            "booklet telemetry job=%s source_chars=%d source_tokens_est=%d units=%d packed=%d "
            "map=%d map_retries=%d planner=%d synthesis=%d verify=%d repair=%d llm_total=%d "
            "wire_attempts=%d input_tokens=%d output_tokens=%d input_tokens_est=%d "
            "booklet_chars=%d booklet_sections=%d quality=%s reasons=%s",
            row["job_id"], row["source_chars"], row["estimated_source_tokens"],
            row["semantic_unit_count"], row["packed_unit_count"], row["map_requests"],
            row["map_retries"], row["planner_requests"], row["synthesis_requests"],
            row["semantic_verification_requests"], row["repair_requests"], row["total_llm_requests"],
            row["wire_attempts"], row["input_tokens"], row["output_tokens"],
            row["estimated_input_tokens"], row["final_booklet_chars"], row["final_booklet_sections"],
            row["quality_state"], ",".join(f"{key}:{value}" for key, value in sorted(reasons.items())) or "-",
        )


_CURRENT: contextvars.ContextVar[BookletTelemetry | None] = contextvars.ContextVar(
    "gamas_booklet_telemetry", default=None
)
_JOB_ID: contextvars.ContextVar[str] = contextvars.ContextVar("gamas_booklet_job_id", default="")


@contextmanager
def telemetry_scope(telemetry: BookletTelemetry) -> Iterator[BookletTelemetry]:
    token = _CURRENT.set(telemetry)
    try:
        yield telemetry
    finally:
        _CURRENT.reset(token)


@_failure_safe
def enter_scope(telemetry: BookletTelemetry) -> Any:
    """Set the job-local telemetry without requiring a ``with`` block."""
    return _CURRENT.set(telemetry)


@_failure_safe
def exit_scope(token: Any) -> None:
    """Restore the previous scope; a failed reset never fails a booklet."""
    if token is not None:
        _CURRENT.reset(token)


@_failure_safe
def enter_job_scope(job_id: int | str) -> Any:
    """Bind a job reference to the current booklet telemetry scope."""
    return _JOB_ID.set(str(job_id))


@_failure_safe
def exit_job_scope(token: Any) -> None:
    if token is not None:
        _JOB_ID.reset(token)


@contextmanager
def job_id_scope(job_id: str) -> Iterator[str]:
    token = _JOB_ID.set(str(job_id))
    try:
        yield str(job_id)
    finally:
        _JOB_ID.reset(token)


@_failure_safe
def current_telemetry() -> BookletTelemetry | None:
    return _CURRENT.get()


@_failure_safe
def current_job_id() -> str:
    return _JOB_ID.get()


@_failure_safe
def record_call(task: str, *, system: str = "", user: str = "", chars_per_token: float = 1.5) -> None:
    telemetry = current_telemetry()
    if telemetry is not None:
        telemetry.record_call(task, system=system, user=user, chars_per_token=chars_per_token)


@_failure_safe
def record_wire(stage: str, attempts: int) -> None:
    telemetry = current_telemetry()
    if telemetry is not None:
        telemetry.record_wire(stage, attempts)


@_failure_safe
def record_provider_tokens(input_tokens: int | None, output_tokens: int | None) -> None:
    telemetry = current_telemetry()
    if telemetry is not None:
        telemetry.record_provider_tokens(input_tokens, output_tokens)
