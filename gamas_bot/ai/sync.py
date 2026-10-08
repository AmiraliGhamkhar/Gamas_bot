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
