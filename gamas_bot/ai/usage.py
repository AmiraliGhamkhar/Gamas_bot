"""Usage accounting + structured request-lifecycle events.

Every LLM request produces:

1. a row in ``ai_usage_records`` (+ the ``ai_usage_daily`` rollup), and
2. structured events (``note_request_started`` … ``credential_quarantined``)
   logged through the module logger *and* persisted to ``ai_events`` for the
   admin Logs panel.

Non-negotiable rules:

* **never** log the API key, Authorization headers, prompts, transcripts or
  model answers — only the metadata fields enumerated here;
* provider-supplied text (error detail) passes through :func:`sanitize_text`
  with the credential's secret as the redaction needle;
* the database is append-only metadata: aggregate panels read from
  ``ai_usage_daily``; per-request rows exist for diagnostics.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger("gamas_bot.ai")

#: Event vocabulary (spec §30).
EVENTS = (
    "note_request_started",
    "note_request_retry",
    "note_request_succeeded",
    "note_request_failed",
    "note_request_fallback",
    "note_json_validation_failed",
    "note_repair_started",
    "note_repair_accepted",
    "note_repair_rejected",
    "note_compile_started",
    "note_compile_accepted",
    "note_compile_rejected",
    "provider_quota_warning",
    "provider_rate_limited",
    "provider_billing_blocked",
    "provider_model_unavailable",
    "provider_health_changed",
    "provider_route_changed",
    "provider_sync",
    "credential_rotated",
    "credential_quarantined",
    "credential_cooldown",
    "generation_test",
)

_EVENT_LEVELS = {
    "note_request_succeeded": "info",
    "provider_route_changed": "info",
    "provider_sync": "info",
}

_MAX_DETAIL = 240


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_text(value: object, secrets: Iterable[str] = (), *, limit: int = _MAX_DETAIL) -> str:
    """Bound provider-supplied text and redact any echoed secret material."""
    if value is None:
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    for secret in secrets:
        if not secret:
            continue
        needle = str(secret)
        text = text.replace(needle, "***")
        if len(needle) >= 12:
            fragment = needle[:12]
            text = text.replace(fragment, "***")
            fragment = needle[-12:]
            text = text.replace(fragment, "***")
    return text[:limit]


class AIUsageTracker:
    """Writes usage rows/rollups and emits lifecycle events safely."""

    def __init__(self, db, *, enabled: bool = True):
        self.db = db
        self.enabled = enabled and db is not None
        self._lock = asyncio.Lock()

    # -- events --------------------------------------------------------------

    async def event(self, name: str, *, level: str | None = None, **fields) -> None:
        """Emit one structured event (logger + ai_events table)."""
        if name not in EVENTS:
            name = "provider_route_changed" if name.endswith("_changed") else "note_request_failed"
        level = level or _EVENT_LEVELS.get(name, "warning")
        detail = sanitize_text(fields.pop("detail", "") or "", fields.pop("_secrets", ()))
        # Render a logfmt-style one-liner; every field is metadata by contract.
        pairs = [f"event={name}"]
        for key in sorted(fields):
            value = fields[key]
            if value is None or isinstance(value, (list, tuple, dict)):
                continue
            pairs.append(f"{key}={value}")
        message = " ".join(pairs)
        if detail:
            message += f" detail={detail}"
        log_fn = logger.info if level == "info" else logger.warning
        log_fn("%s", message)
        if not self.enabled:
            return
        try:
            await self.db.ai_event_insert(
                {
                    "level": level,
                    "event": name,
                    "service": fields.get("service", "notes"),
                    "provider": fields.get("provider"),
                    "canonical": fields.get("canonical"),
                    "model": fields.get("model"),
                    "request_type": fields.get("request_type"),
                    "route_position": fields.get("route_position"),
                    "http_status": fields.get("http_status"),
                    "latency_ms": fields.get("latency_ms"),
                    "error_class": fields.get("error_class"),
                    "detail": detail,
                    "job_id": fields.get("job_id"),
                    "check_type": fields.get("check_type"),
                    "created_at": fields.get("created_at") or _utc_now(),
                }
            )
        except Exception:  # observability must never break a job
            logger.warning("Could not persist AI event name=%s", name)

    # -- usage records ---------------------------------------------------------

    async def record(self, row: dict) -> None:
        if not self.enabled:
            return
        try:
            await self.db.ai_usage_insert(row)
        except Exception:
            logger.warning(
                "Could not persist AI usage provider=%s model=%s",
                row.get("provider"),
                row.get("model"),
            )

    async def quota_snapshot(
        self,
        provider: str,
        credential_id: int | None,
        model: str,
        headers: dict[str, str],
        *,
        source: str = "headers",
    ) -> None:
        """Persist allowlisted rate-limit headers as the latest quota hint."""
        if not self.enabled or not headers:
            return
        remaining = None
        reset_at = None
        window = "day" if "x-ratelimit-limit-requests-day" in headers else "minute"
        for key in ("x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens"):
            if key in headers:
                remaining = f"{key}={headers[key]}"
                break
        for key in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
            if key in headers:
                reset_at = f"{key}={headers[key]}"
                break
        if remaining is None and reset_at is None:
            return
        try:
            await self.db.ai_quota_upsert(
                provider, credential_id, model, window, remaining, reset_at, source=source
            )
        except Exception:
            logger.warning("Could not persist quota snapshot provider=%s", provider)

    # -- panels -----------------------------------------------------------------

    async def today_request_count(self, provider: str) -> int:
        if not self.enabled:
            return 0
        try:
            return await self.db.ai_usage_today_count(provider)
        except Exception:
            return 0


class NullTracker(AIUsageTracker):
    def __init__(self):
        super().__init__(None, enabled=False)
