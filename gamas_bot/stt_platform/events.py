"""Structured STT lifecycle event logging (spec §44/§54).

Every STT lifecycle emits structured events to the logger AND to the
``stt_provider_events`` table (admin "STT Logs" panel). Events carry
operational metadata only — never API keys, authorization headers, raw audio,
transcript content, prompts or provider credentials.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Event vocabulary (spec §44).
STT_EVENTS = frozenset(
    {
        "stt_request_started",
        "stt_request_uploaded",
        "stt_request_succeeded",
        "stt_request_failed",
        "stt_request_retry",
        "stt_provider_fallback",
        "stt_credential_rotated",
        "stt_credential_quarantined",
        "stt_credential_cooldown",
        "stt_quota_warning",
        "stt_quota_exhausted",
        "stt_quality_warning",
        "stt_quality_rejected",
        "stt_health_checked",
        "stt_model_unavailable",
    }
)

#: Fields every event may carry (spec §44). Anything not listed here is
#: dropped before logging so a future caller cannot leak a secret by accident.
_ALLOWED_FIELDS = frozenset(
    {
        "timestamp", "job_id", "submission_id", "request_id", "provider", "model",
        "credential_id", "credential_label", "route_position", "attempt",
        "audio_bytes", "audio_duration_seconds", "format", "sample_rate",
        "channels", "language", "feature_set", "latency_ms", "http_status",
        "retry_after", "quota_remaining", "quota_type", "confidence",
        "word_count", "character_count", "detected_language", "error_category",
        "fallback_reason", "next_provider", "action", "check_type", "detail",
    }
)

#: Values that must never appear even inside allowed fields.
_SECRET_MARKERS = ("authorization", "bearer ", "api_key", "apikey", "secret")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_event_fields(fields: dict) -> dict:
    """Keep only whitelisted, non-secret fields with JSON-safe values."""
    clean: dict = {}
    for key, value in fields.items():
        if key not in _ALLOWED_FIELDS:
            continue
        if value is None:
            continue
        if isinstance(value, bool):
            clean[key] = int(value)
        elif isinstance(value, (int, float, str)):
            text = str(value)
            lowered = text.lower()
            if any(marker in lowered for marker in _SECRET_MARKERS):
                continue
            clean[key] = value if isinstance(value, (int, float)) else text[:300]
        elif isinstance(value, (list, tuple)):
            clean[key] = [str(item)[:120] for item in value][:20]
        else:
            clean[key] = type(value).__name__
    return clean


class STTEventLogger:
    """Emits structured events to the log and (optionally) the database."""

    def __init__(self, db=None, *, enabled: bool = True):
        self.db = db
        self.enabled = enabled

    async def emit(self, event: str, **fields) -> None:
        """Record one lifecycle event. ``event`` must be in :data:`STT_EVENTS`."""
        if not self.enabled:
            return
        clean = sanitize_event_fields(fields)
        clean["timestamp"] = _utc_now()
        if event not in STT_EVENTS:
            logger.warning("Unknown STT event dropped event=%s", event)
            return
        level = logging.WARNING if any(
            token in event for token in ("failed", "quarantined", "exhausted", "rejected", "warning")
        ) else logging.INFO
        logger.log(
            level,
            "STT event=%s %s",
            event,
            " ".join(f"{key}={value}" for key, value in sorted(clean.items())),
        )
        if self.db is not None:
            try:
                await self.db.stt_provider_event_insert(
                    {
                        "event": event,
                        "provider": str(clean.get("provider") or ""),
                        "model": str(clean.get("model") or ""),
                        "credential_id": clean.get("credential_id"),
                        "job_id": str(clean.get("job_id") or clean.get("submission_id") or ""),
                        "http_status": clean.get("http_status"),
                        "latency_ms": clean.get("latency_ms"),
                        "error_category": str(clean.get("error_category") or ""),
                        "level": "warning" if level >= logging.WARNING else "info",
                        "fields_json": json.dumps(clean, sort_keys=True, default=str),
                        "created_at": clean["timestamp"],
                    }
                )
            except Exception:
                # Logging must never break transcription.
                logger.debug("Could not persist STT event event=%s", event)


class NullEventLogger(STTEventLogger):
    def __init__(self):
        super().__init__(None, enabled=False)

    async def emit(self, event: str, **fields) -> None:  # pragma: no cover - trivial
        return None
