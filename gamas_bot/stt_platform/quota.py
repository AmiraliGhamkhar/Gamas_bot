"""STT quota tracking (spec §40/§41).

Quota is tracked in the database (``stt_quota_snapshots``). Provider API
headers are the preferred source; Gamas never replaces observed data with
guessed constants — when a provider does not expose quota, the panel shows
``Unknown``.

The safety margin (``STT_QUOTA_SAFETY_MARGIN``, default 10%) is applied per
provider: a provider reporting 100 remaining minutes is treated as having 90
*safe* minutes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Quota types understood by the tracker (spec §40).
QUOTA_TYPES = frozenset(
    {
        "rpm",
        "rpd",
        "audio_seconds_hour",
        "audio_seconds_day",
        "minutes_month",
        "credits",
        "concurrent_requests",
        "monthly_tokens",
        "tokens",
        "requests",
        "requests_per_minute",
        "audio_seconds",
    }
)


@dataclass(frozen=True, slots=True)
class QuotaObservation:
    """One quota fact observed from a provider response."""

    provider: str
    quota_type: str
    limit: int | None = None
    used: int | None = None
    remaining: int | None = None
    reset_at: str | None = None
    source: str = "response_headers"   # response_headers | response_body | account_api | local_estimate
    observed_at: str | None = None
    credential_id: int | None = None
    account_scope: str = "provider"
    model: str = ""

    def to_row(self) -> dict:
        return {
            "provider": self.provider,
            "credential_id": self.credential_id,
            "account_scope": self.account_scope,
            "model": self.model,
            "quota_type": self.quota_type,
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
            "source": self.source,
            "observed_at": self.observed_at,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_rate_limit_reset(value: object) -> str | None:
    """Parse a reset header (epoch seconds, ms, or ISO date) into an ISO time."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        number = None
    if number is not None:
        # Heuristic: values beyond year 2001 in seconds are ms epochs.
        if number > 10_000_000_000:
            number = number / 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).replace(microsecond=0).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except (ValueError, TypeError):
        return None


def parse_groq_headers(headers) -> list[QuotaObservation]:
    """Read Groq's documented ``x-ratelimit-*`` headers (spec §16).

    Header names: ``x-ratelimit-limit-requests``, ``x-ratelimit-limit-tokens``,
    ``x-ratelimit-remaining-requests``, ``x-ratelimit-remaining-tokens``,
    ``x-ratelimit-reset-requests``, ``x-ratelimit-reset-tokens``. Audio-specific
    quota headers (``x-ratelimit-limit-audio-seconds`` etc.) are read too when
    the provider introduces them.
    """
    observations: list[QuotaObservation] = []
    get = headers.get if hasattr(headers, "get") else lambda name, default=None: default

    def _int(name: str) -> int | None:
        raw = get(name)
        if raw is None:
            return None
        try:
            return int(float(str(raw).strip()))
        except (TypeError, ValueError):
            return None

    pairs = (
        ("requests", "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests"),
        ("tokens", "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"),
        ("audio_seconds", "x-ratelimit-limit-audio-seconds", "x-ratelimit-remaining-audio-seconds", "x-ratelimit-reset-audio-seconds"),
        ("requests_per_minute", "x-ratelimit-limit-requests-per-minute", "x-ratelimit-remaining-requests-per-minute", "x-ratelimit-reset-requests-per-minute"),
    )
    for quota_type, limit_name, remaining_name, reset_name in pairs:
        limit = _int(limit_name)
        remaining = _int(remaining_name)
        if limit is None and remaining is None:
            continue
        used = None
        if limit is not None and remaining is not None:
            used = max(0, limit - remaining)
        observations.append(
            QuotaObservation(
                provider="groq",
                quota_type=quota_type,
                limit=limit,
                used=used,
                remaining=remaining,
                reset_at=parse_rate_limit_reset(get(reset_name)),
                source="response_headers",
                observed_at=_utc_now(),
            )
        )
    return observations


def parse_retry_after_header(headers) -> float | None:
    """``Retry-After`` in seconds (bounded), used by the retry policy (spec §43)."""
    get = headers.get if hasattr(headers, "get") else lambda name, default=None: default
    raw = get("Retry-After") or get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds in {float("inf"), float("-inf")}:
        return None
    return min(max(seconds, 0.0), 604_800.0)


class STTQuotaTracker:
    """Persists quota observations and answers "is there safe capacity?"."""

    def __init__(self, db=None, settings=None):
        self.db = db
        self.settings = settings

    @property
    def safety_margin(self) -> float:
        return min(max(float(getattr(self.settings, "stt_quota_safety_margin", 0.10)), 0.0), 0.5)

    async def record(self, observation: QuotaObservation) -> None:
        if self.db is None:
            return
        try:
            await self.db.stt_quota_upsert(observation.to_row())
        except Exception:
            logger.debug("Could not persist quota observation provider=%s", observation.provider)

    async def record_many(self, observations) -> None:
        for observation in observations:
            await self.record(observation)

    async def latest(
        self,
        provider: str,
        quota_type: str | None = None,
        *,
        account_scope: str | None = None,
    ) -> list[dict]:
        if self.db is None:
            return []
        return await self.db.stt_quota_latest(
            provider, quota_type, account_scope=account_scope
        )

    async def safe_remaining(
        self,
        provider: str,
        quota_type: str,
        *,
        needed: float,
    ) -> tuple[float | None, str]:
        """``(safe_remaining, state)`` for one quota.

        ``state`` is one of ``available``, ``unknown`` (provider did not expose
        it — never fabricated), ``exhausted``, ``insufficient``.
        """
        rows = await self.latest(provider, quota_type)
        if not rows:
            return None, "unknown"
        row = rows[0]
        remaining = row.get("remaining")
        if remaining is None:
            return None, "unknown"
        try:
            remaining_value = float(remaining)
        except (TypeError, ValueError):
            return None, "unknown"
        safe = remaining_value * (1.0 - self.safety_margin)
        if safe <= 0:
            return 0.0, "exhausted"
        if safe < needed:
            return safe, "insufficient"
        return safe, "available"

    async def quota_warning_threshold_reached(
        self, provider: str, quota_type: str, *, fraction: float = 0.9
    ) -> bool:
        """True when observed usage passed ``fraction`` of the limit."""
        rows = await self.latest(provider, quota_type)
        if not rows:
            return False
        row = rows[0]
        limit = row.get("limit")
        used = row.get("used")
        if limit is None or used is None:
            remaining = row.get("remaining")
            if limit is None or remaining is None:
                return False
            used = max(0.0, float(limit) - float(remaining))
        try:
            return float(used) >= fraction * float(limit)
        except (TypeError, ValueError):
            return False
