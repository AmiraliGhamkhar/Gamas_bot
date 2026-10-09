"""Live model-catalog synchronisation.

Discovery is **explicit** (admin button, panel open with stale cache, or the
optional start-up kick-off) and cached with a TTL — never per-panel-refresh
and never per-note-request (spec §38).

The sync reads the provider's official model listing exactly once with the
best available credential (stored key first, environment second), normalizes
the catalog through the adapter and persists it into ``ai_models``. Models
that vanished from the catalog become ``available=0``; history is kept.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from ..structuring import _retry_after_seconds
from .adapters import adapter_for
from .models import ModelRegistry
from .registry import registry_info
from .usage import AIUsageTracker, sanitize_text

logger = logging.getLogger(__name__)

#: A sync probe is cheap; never queue behind a slow provider.
DEFAULT_SYNC_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class SyncResult:
    provider: str
    ok: bool
    synced: int = 0
    deactivated: int = 0
    http_status: int | None = None
    latency_ms: int = 0
    cached: bool = False
    detail: str = ""

    @property
    def label(self) -> str:
        if self.cached:
            return "کش معتبر"
        if self.ok:
            return f"موفق ({self.synced} مدل)"
        return "ناموفق"


async def _fetch(url: str, headers: dict[str, str], timeout: float):
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        async with session.get(url, headers=headers) as response:
            body = await response.read()
            return int(response.status), dict(response.headers), body


async def sync_provider_catalog(
    router,
    canonical: str,
    *,
    adapter=None,
    session_get=None,
    force: bool = False,
    check_type: str = "read_only",
) -> SyncResult:
    """Sync one provider's model catalog (idempotent, TTL-guarded)."""
    models: ModelRegistry = router.models
    tracker: AIUsageTracker = router.tracker
    adapter = adapter or adapter_for(canonical, router.settings)
    info = registry_info(canonical)
    if not info.model_discovery:
        return SyncResult(provider=canonical, ok=False, detail="discovery unsupported")

    base_url = router._base_url_for_slug(canonical)
    if not base_url and not getattr(router.settings, "cloudflare_account_id", None):
        return SyncResult(provider=canonical, ok=False, detail="no base URL")
    if not base_url:
        base_url = adapter.default_base_url()
    if not base_url:
        return SyncResult(provider=canonical, ok=False, detail="no base URL")

    synced_at = None
    try:
        synced_at = await router.db.ai_model_latest_sync(canonical)
    except Exception:
        synced_at = None
    if not force and not models.stale(canonical, synced_at):
        rows = await models.cached(canonical)
        return SyncResult(provider=canonical, ok=True, synced=len(rows), cached=True)

    # Best credential: first usable stored key, else the environment fallback.
    credential = None
    try:
        from .routing import RouteLeg

        pool = await router.credentials_for(RouteLeg(provider=canonical, canonical=canonical))
        credential = pool[0] if pool else None
    except Exception:
        credential = None
    secret = credential.secret if credential else ""
    secret = secret or ""
    try:
        request = adapter.discovery_request(base_url, secret)
    except Exception:
        request = None
    if request is None:
        return SyncResult(provider=canonical, ok=False, detail="discovery unsupported")

    method, url, headers = request
    started = time.perf_counter()
    try:
        fetcher = session_get or _fetch
        status, resp_headers, body = await fetcher(url, dict(headers), DEFAULT_SYNC_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return SyncResult(provider=canonical, ok=False, detail="timeout")
    except aiohttp.ClientError as exc:
        return SyncResult(
            provider=canonical, ok=False, detail=sanitize_text(type(exc).__name__, (secret,))
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    if status in {401, 403}:
        result = SyncResult(
            provider=canonical, ok=False, http_status=status, latency_ms=latency_ms,
            detail="authentication failed",
        )
    elif status == 429:
        result = SyncResult(
            provider=canonical, ok=False, http_status=status, latency_ms=latency_ms,
            detail=f"rate limited (retry-after={_retry_after_seconds(resp_headers)})",
        )
    elif status != 200:
        result = SyncResult(
            provider=canonical, ok=False, http_status=status, latency_ms=latency_ms,
            detail=f"HTTP {status}",
        )
    else:
        import json

        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            result = SyncResult(
                provider=canonical, ok=False, http_status=status, latency_ms=latency_ms,
                detail="invalid catalog payload",
            )
        else:
            discovered = adapter.parse_discovery(payload)
            stats = await models.apply_discovery(canonical, discovered)
            result = SyncResult(
                provider=canonical,
                ok=True,
                synced=stats.get("synced", 0),
                deactivated=stats.get("deactivated", 0),
                http_status=status,
                latency_ms=latency_ms,
            )
    await tracker.event(
        "provider_sync",
        service="notes",
        provider=canonical,
        http_status=result.http_status,
        latency_ms=result.latency_ms,
        check_type=check_type,
        detail=result.detail,
    )
    return result


# ---------------------------------------------------------------------------
# Live quota/entitlement probes (read-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuotaProbeResult:
    """One provider's live quota state (spec §12/§17: never guessed)."""

    provider: str
    ok: bool
    supported: bool = True
    window: str = "day"
    remaining: int | None = None
    limit: int | None = None
    used: int | None = None
    reset_at: str | None = None
    is_free_tier: bool | None = None
    http_status: int | None = None
    latency_ms: int = 0
    detail: str = ""

    def summary(self) -> str:
        if not self.supported:
            return "این ارائه‌دهنده کاوش سهمیهٔ زنده ندارد (از هدرهای پاسخ استفاده می‌شود)."
        if not self.ok:
            return f"ناموفق: {self.detail or '—'}"
        if self.remaining is None and self.used is None:
            return "سهمیهٔ زنده‌ای گزارش نشده است."
        parts = []
        if self.used is not None and self.limit is not None:
            parts.append(f"مصرف امروز {self.used}/{self.limit}")
        elif self.remaining is not None:
            parts.append(f"باقی‌مانده {self.remaining}")
        if self.reset_at:
            parts.append(f"بازنشانی {self.reset_at}")
        if self.is_free_tier is False:
            parts.append("حساب پولی")
        return " | ".join(parts) if parts else "—"


def _next_utc_midnight() -> str:
    """OpenRouter free daily counters reset at the start of the UTC day."""
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat()


async def probe_provider_quota(
    router,
    canonical: str,
    *,
    session_get=None,
    check_type: str = "read_only",
) -> QuotaProbeResult | None:
    """Read one provider's live quota state (never a generation).

    Only providers with a documented account/entitlement endpoint are
    probed (currently OpenRouter ``GET /api/v1/key``); everyone else returns
    ``supported=False`` and keeps relying on response-header snapshots. The
    probe result is persisted as a quota snapshot (``source='key_endpoint'``)
    so the router's FREE_ONLY gate can honour the *live* daily limit instead
    of only the conservative static default.
    """
    adapter = adapter_for(canonical, router.settings)
    if not hasattr(adapter, "key_info_request"):
        return QuotaProbeResult(provider=canonical, ok=False, supported=False)
    base_url = router._base_url_for_slug(canonical)
    if not base_url:
        base_url = adapter.default_base_url()
    if not base_url:
        return QuotaProbeResult(provider=canonical, ok=False, detail="no base URL")
    credential = None
    try:
        from .routing import RouteLeg

        pool = await router.credentials_for(RouteLeg(provider=canonical, canonical=canonical))
        credential = pool[0] if pool else None
    except Exception:
        credential = None
    secret = credential.secret if credential else ""
    request = adapter.key_info_request(base_url, secret)
    if request is None:
        return QuotaProbeResult(provider=canonical, ok=False, supported=False)
    method, url, headers = request
    started = time.perf_counter()
    try:
        fetcher = session_get or _fetch
        status, resp_headers, body = await fetcher(url, dict(headers), DEFAULT_SYNC_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return QuotaProbeResult(provider=canonical, ok=False, detail="timeout")
    except aiohttp.ClientError as exc:
        return QuotaProbeResult(
            provider=canonical, ok=False, detail=sanitize_text(type(exc).__name__, (secret,))
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    if status != 200:
        return QuotaProbeResult(
            provider=canonical, ok=False, http_status=status, latency_ms=latency_ms,
            detail=f"HTTP {status}",
        )
    import json

    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        return QuotaProbeResult(
            provider=canonical, ok=False, http_status=status, latency_ms=latency_ms,
            detail="invalid key-info payload",
        )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return QuotaProbeResult(
            provider=canonical, ok=False, http_status=status, latency_ms=latency_ms,
            detail="invalid key-info payload",
        )
    free_daily = data.get("free_model_daily_requests")
    result = QuotaProbeResult(
        provider=canonical,
        ok=True,
        http_status=status,
        latency_ms=latency_ms,
        is_free_tier=data.get("is_free_tier"),
    )
    if isinstance(free_daily, dict):
        try:
            result = QuotaProbeResult(
                provider=canonical,
                ok=True,
                window="day",
                remaining=int(free_daily.get("remaining")),
                limit=int(free_daily.get("limit")),
                used=int(free_daily.get("used")),
                reset_at=_next_utc_midnight(),
                is_free_tier=data.get("is_free_tier"),
                http_status=status,
                latency_ms=latency_ms,
            )
        except (TypeError, ValueError):
            pass
    # Persist as the authoritative quota snapshot for the router's gate.
    try:
        remaining_text = None
        if result.limit is not None:
            remaining_text = f"free_daily remaining={result.remaining}/{result.limit}"
        elif result.remaining is not None:
            remaining_text = f"free_daily remaining={result.remaining}"
        if remaining_text:
            await router.db.ai_quota_upsert(
                canonical,
                credential.id if credential else None,
                "",
                "day",
                remaining_text,
                result.reset_at,
                source="key_endpoint",
            )
    except Exception:
        logger.warning("Could not persist quota probe provider=%s", canonical)
    await router.tracker.event(
        "provider_sync",
        service="notes",
        provider=canonical,
        request_type="quota_probe",
        http_status=status,
        latency_ms=latency_ms,
        check_type=check_type,
        detail=(
            f"free_daily used={result.used} limit={result.limit} "
            f"remaining={result.remaining}"
        ),
    )
    return result
