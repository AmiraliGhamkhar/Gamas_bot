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
    """Sync one provider catalog without letting a bad response erase good data.

    Read-only discovery credentials deliberately bypass generation billing
    eligibility. They can inspect an account catalog before an administrator
    has attested the key for generation.
    """
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

    try:
        synced_at = await router.db.ai_model_latest_sync(canonical)
    except Exception:
        synced_at = None
    if not force and not models.stale(canonical, synced_at):
        rows = await models.cached(canonical)
        return SyncResult(provider=canonical, ok=True, synced=len(rows), cached=True)

    # Catalog probes need read-only credentials even when the key is not yet
    # marked free/paid. This path never authorizes a generation request.
    credential = None
    try:
        from .routing import RouteLeg

        pool = await router.credentials_for(
            RouteLeg(provider=canonical, canonical=canonical), for_generation=False
        )
        credential = pool[0] if pool else None
    except Exception:
        credential = None
    secret = (credential.secret if credential else "") or ""
    fetcher = session_get or _fetch

    # Nara's account model endpoint does not itself encode the public Free plan.
    # Fetch the documented public plans endpoint independently; failure means
    # the catalog can still refresh, but every model's free status remains
    # unknown until the plan evidence is available.
    plan_model_statuses = None
    plan_warning = ""
    plan_request = getattr(adapter, "public_plan_request", None)
    plan_parser = getattr(adapter, "plan_model_statuses", None)
    if callable(plan_request) and callable(plan_parser):
        try:
            method, plan_url, plan_headers = plan_request()
            if method != "GET":
                raise ValueError("read-only plan discovery must use GET")
            plan_status, _plan_headers, plan_body = await fetcher(
                plan_url, dict(plan_headers), DEFAULT_SYNC_TIMEOUT_SECONDS
            )
            if plan_status == 200:
                import json

                plan_payload = json.loads(plan_body.decode("utf-8", "replace"))
                plan_model_statuses = plan_parser(plan_payload)
            if plan_status != 200 or plan_model_statuses is None:
                plan_warning = "public plan unavailable; free eligibility unknown"
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError, TypeError):
            plan_warning = "public plan unavailable; free eligibility unknown"
        except Exception as exc:
            plan_warning = f"public plan unavailable ({type(exc).__name__}); free eligibility unknown"

    discovered_by_id: dict[str, object] = {}
    page_count = 1
    page_size = 0
    if hasattr(adapter, "discovery_page_count"):
        page_count = max(1, int(getattr(adapter, "max_discovery_pages", 20)))
        page_size = max(1, int(getattr(adapter, "discovery_page_size", 100)))

    final_status = None
    total_latency_ms = 0
    completed = False
    result: SyncResult | None = None
    for page in range(1, page_count + 1):
        try:
            request = adapter.discovery_request(base_url, secret, page=page)
        except Exception:
            request = None
        if request is None:
            return SyncResult(provider=canonical, ok=False, detail="discovery unsupported")
        method, url, headers = request
        if method != "GET":
            return SyncResult(provider=canonical, ok=False, detail="discovery method is not read-only")
        started = time.perf_counter()
        try:
            status, resp_headers, body = await fetcher(
                url, dict(headers), DEFAULT_SYNC_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            result = SyncResult(provider=canonical, ok=False, detail="timeout")
            break
        except aiohttp.ClientError as exc:
            result = SyncResult(
                provider=canonical, ok=False,
                detail=sanitize_text(type(exc).__name__, (secret,)),
            )
            break
        except Exception as exc:
            result = SyncResult(
                provider=canonical, ok=False,
                detail=f"discovery transport failed ({type(exc).__name__})",
            )
            break

        latency_ms = int((time.perf_counter() - started) * 1000)
        total_latency_ms += latency_ms
        final_status = status
        if status in {401, 403}:
            result = SyncResult(
                provider=canonical, ok=False, http_status=status,
                latency_ms=total_latency_ms, detail="authentication failed",
            )
            break
        if status == 429:
            result = SyncResult(
                provider=canonical, ok=False, http_status=status,
                latency_ms=total_latency_ms,
                detail=f"rate limited (retry-after={_retry_after_seconds(resp_headers)})",
            )
            break
        if status != 200:
            result = SyncResult(
                provider=canonical, ok=False, http_status=status,
                latency_ms=total_latency_ms, detail=f"HTTP {status}",
            )
            break

        import json

        try:
            payload = json.loads(body.decode("utf-8", "replace"))
            if page_count > 1:
                count = adapter.discovery_page_count(payload)
                if count is None:
                    raise ValueError("malformed paginated catalog")
                # An empty first page is never accepted as a fresh catalog.
                # An empty later page is a valid terminator after a full page.
                if count == 0:
                    if page == 1:
                        raise ValueError("empty catalog")
                    completed = True
                    break
            parsed_page = adapter.parse_discovery(
                payload, plan_model_statuses=plan_model_statuses
            ) if plan_model_statuses is not None else adapter.parse_discovery(payload)
            if not isinstance(parsed_page, list):
                raise ValueError("catalog parser returned a non-list")
            if page_count > 1 and count and not parsed_page:
                raise ValueError("catalog page contains no valid model IDs")
            for model_info in parsed_page:
                model_id = getattr(model_info, "model_id", None)
                if not isinstance(model_id, str) or not model_id.strip():
                    raise ValueError("catalog parser returned an invalid model")
                discovered_by_id[model_id] = model_info
            if page_count == 1 or count < page_size:
                completed = True
                break
            if page == page_count:
                # The response may be truncated; do not deactivate any prior
                # model unless every page has been fetched successfully.
                raise ValueError("catalog pagination limit reached")
        except (ValueError, TypeError, UnicodeError):
            result = SyncResult(
                provider=canonical, ok=False, http_status=status,
                latency_ms=total_latency_ms, detail="invalid, empty, or incomplete catalog",
            )
            break
        except Exception as exc:
            # A buggy/changed upstream shape is a failed sync, never an empty
            # snapshot that marks every known model unavailable.
            result = SyncResult(
                provider=canonical, ok=False, http_status=status,
                latency_ms=total_latency_ms,
                detail=f"catalog parser failed ({type(exc).__name__})",
            )
            break
    else:
        result = SyncResult(
            provider=canonical, ok=False, http_status=final_status,
            latency_ms=total_latency_ms, detail="catalog pagination limit reached",
        )

    if not completed and result is None:
        result = SyncResult(
            provider=canonical, ok=False, http_status=final_status,
            latency_ms=total_latency_ms, detail="incomplete catalog",
        )
    if completed:
        discovered = list(discovered_by_id.values())
        if not discovered:
            result = SyncResult(
                provider=canonical, ok=False, http_status=final_status,
                latency_ms=total_latency_ms, detail="empty catalog; existing catalog preserved",
            )
        else:
            try:
                stats = await models.apply_discovery(canonical, discovered)
            except Exception as exc:
                result = SyncResult(
                    provider=canonical, ok=False, http_status=final_status,
                    latency_ms=total_latency_ms,
                    detail=f"catalog persistence failed ({type(exc).__name__})",
                )
            else:
                result = SyncResult(
                    provider=canonical,
                    ok=True,
                    synced=stats.get("synced", 0),
                    deactivated=stats.get("deactivated", 0),
                    http_status=final_status,
                    latency_ms=total_latency_ms,
                    detail=plan_warning,
                )
    assert result is not None
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

        pool = await router.credentials_for(
            RouteLeg(provider=canonical, canonical=canonical), for_generation=False
        )
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
