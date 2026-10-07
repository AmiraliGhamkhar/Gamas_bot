"""Cheap, explicit, cached provider health checks.

The admin panel answers one question — *is this key usable right now?* — without
spending money or submitting work:

* every probe is a **read-only, free** endpoint (job/model listing), never a
  transcription job and never a model generation;
* probes run only when an administrator asks (panel open or explicit test) and
  results are cached for :data:`DEFAULT_CACHE_SECONDS` so redrawing the panel
  cannot hammer a provider;
* a provider whose deployment cannot be probed safely is reported as
  ``configured`` and exposes the manual per-credential test instead of a
  fabricated status;
* the outcome feeds the same rotation state as live traffic: HTTP 429 sets the
  credential cooldown (honouring ``Retry-After``), HTTP 401/403 quarantines it,
  and a success clears both;
* secrets never leave this module: results carry a label and a masked tail only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiohttp

from .config import Settings
from .database import Database
from .provider_credentials import (
    PROVIDER_CHOICES,
    CredentialStoreError,
    ProviderCredential,
    ProviderCredentialManager,
)
from .structuring import _endpoint

logger = logging.getLogger(__name__)

#: Vocabulary shared by the panel, the tests and the audit log.
HEALTH_STATUSES = frozenset(
    {
        "healthy",
        "degraded",
        "rate_limited",
        "authentication_failed",
        "unavailable",
        "disabled",
        "not_configured",
        "configured",
    }
)

STATUS_LABELS_FA = {
    "healthy": "سالم",
    "degraded": "کاهش کیفیت",
    "rate_limited": "سقف درخواست (429)",
    "authentication_failed": "خطای احراز هویت (401/403)",
    "unavailable": "در دسترس نیست",
    "disabled": "غیرفعال",
    "not_configured": "تنظیم‌نشده",
    "configured": "تنظیم‌شده (بررسی نشده)",
}

#: Statuses that mean "this credential must not be used for requests now".
UNUSABLE_STATUSES = frozenset(
    {"rate_limited", "authentication_failed", "unavailable", "disabled"}
)

#: A cheap probe is a list request with a short timeout; anything slower than
#: this is a problem the operator should see instead of a queued HTTP call.
DEFAULT_PROBE_TIMEOUT_SECONDS = 6.0

#: Panel cache lifetime. Health checks are manual by design (see module docstring).
DEFAULT_CACHE_SECONDS = 300.0

#: Read at most this many bytes of a probe response body (error detail only).
_MAX_BODY_BYTES = 2048

#: Never put a full provider message in the panel.
_MAX_DETAIL_CHARS = 240

#: Providers whose probe URL is fixed by the vendor contract.
DEEPGRAM_PROBE_URL = "https://api.deepgram.com/v1/projects"
GEMINI_PROBE_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_PROBE_BASE = "https://api.anthropic.com/v1"


@dataclass(frozen=True, slots=True)
class HealthResult:
    """One credential's health. ``detail``/``error`` are already sanitized."""

    service: str
    provider: str
    credential_id: int | None
    source: str
    label: str
    masked: str
    status: str
    http_status: int | None = None
    latency_ms: int | None = None
    checked_at: str | None = None
    cooldown_until: str | None = None
    detail: str | None = None
    cached: bool = False
    probe_supported: bool = True

    @property
    def usable(self) -> bool:
        return self.status not in UNUSABLE_STATUSES

    @property
    def status_fa(self) -> str:
        return STATUS_LABELS_FA.get(self.status, self.status)


@dataclass(frozen=True, slots=True)
class Probe:
    """A probe request template. ``headers`` may carry the key (never logged)."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    unsupported_reason: str | None = None

    @property
    def supported(self) -> bool:
        return self.unsupported_reason is None and bool(self.url)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.isoformat()


def classify_status(status_code: int) -> str:
    """Map an HTTP status to the health vocabulary (probe semantics).

    ``400``, ``404``, ``405`` and ``422`` mean *our probe* is not offered by that
    deployment — the host answered, so the credential is not unhealthy.
    """
    if 200 <= status_code < 300:
        return "healthy"
    if status_code in {401, 403}:
        return "authentication_failed"
    if status_code == 429:
        return "rate_limited"
    if status_code in {400, 404, 405, 409, 415, 422}:
        return "configured"
    if status_code >= 500:
        return "degraded"
    return "degraded"


def parse_retry_after(value: object, *, now: datetime | None = None) -> float | None:
    """Seconds until retry, bounded to seven days; supports the date form too."""
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None:
        if seconds != seconds or seconds in {float("inf"), float("-inf")}:
            return None
        return min(max(seconds, 0.0), 604_800.0)
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        reference = now or _utc_now()
        return min(max((parsed - reference).total_seconds(), 0.0), 604_800.0)
    except (TypeError, ValueError, OverflowError):
        return None


def sanitize_detail(value: object, secrets: tuple[str, ...] = ()) -> str | None:
    """Strip secrets out of provider text and bound its length."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    for secret in secrets:
        if secret and len(secret) >= 8 and secret in text:
            text = text.replace(secret, "••••")
    for secret in secrets:
        if secret and len(secret) >= 12:
            fragment_length = 12
            for index in range(len(secret) - fragment_length + 1):
                text = text.replace(secret[index:index + fragment_length], "••••")
    return text[:_MAX_DETAIL_CHARS]


def build_probe(service: str, provider: str, credential: ProviderCredential, settings: Settings) -> Probe:
    """Return the free, read-only probe for one provider.

    Providers differ: Speechmatics is probed through its job list, Deepgram
    through the project list, Gemini/Anthropic/OpenAI-compatible endpoints
    through their model list. A deployment with no usable base URL is reported
    as unsupported and falls back to the manual credential test.
    """
    key = credential.secret or ""
    base_url = credential.base_url
    if service == "stt" and provider == "speechmatics":
        base = (base_url or settings.speechmatics_base_url).rstrip("/")
        return Probe(
            "GET",
            f"{base}/jobs?limit=1",
            {"Authorization": f"Bearer {key}"} if key else {},
        )
    if service == "stt" and provider == "deepgram":
        return Probe(
            "GET",
            DEEPGRAM_PROBE_URL,
            {"Authorization": f"Token {key}"} if key else {},
        )
    if provider in {"openai_compatible", "gemini", "anthropic"}:
        if not base_url:
            base_url = {
                "gemini": settings.note_api_base_url or GEMINI_PROBE_BASE,
                "anthropic": settings.note_api_base_url or ANTHROPIC_PROBE_BASE,
                "openai_compatible": settings.note_api_base_url,
            }.get(provider if service == "notes" else "openai_compatible")
        if service == "stt" and provider == "openai_compatible":
            base_url = base_url or settings.stt_openai_base_url
        if not base_url:
            return Probe("GET", "", {}, unsupported_reason="بدون Base URL")
        headers: dict[str, str] = {}
        if provider == "gemini":
            if key:
                headers["x-goog-api-key"] = key
        elif provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            if key:
                headers["x-api-key"] = key
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        return Probe("GET", _endpoint(base_url, "models"), headers)
    return Probe("GET", "", {}, unsupported_reason="بدون بررسی سبک")


async def _fetch(method: str, url: str, headers: dict[str, str], timeout: float):
    """Perform one bounded probe request. Patchable in tests."""
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.request(method, url, headers=headers) as response:
            body = await response.content.read(_MAX_BODY_BYTES)
            return SimpleNamespace(
                status=int(response.status),
                headers=dict(response.headers),
                text=body.decode("utf-8", "replace"),
            )


class ProviderHealthChecker:
    """Manual, cached health checks for every configured provider credential."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        credentials: ProviderCredentialManager,
        *,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    ):
        self.db = db
        self.settings = settings
        self.credentials = credentials
        self.cache_seconds = max(0.0, float(cache_seconds))
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self._cache: dict[tuple[str, str, int | None], tuple[float, HealthResult]] = {}

    # -- credential discovery -------------------------------------------------
    async def _stored_candidates(self, service: str, provider: str) -> list:
        summaries = await self.credentials.list_summaries()
        candidates = []
        for row in summaries:
            if str(row["service"]) != service or str(row["provider"]) != provider:
                continue
            if not int(row["enabled"]):
                candidates.append(
                    HealthResult(
                        service=service,
                        provider=provider,
                        credential_id=int(row["id"]),
                        source="database",
                        label=str(row["label"]),
                        masked="••••••••" + str(row["secret_last4"]),
                        status="disabled",
                        cooldown_until=row.get("cooldown_until"),
                        detail="کلید توسط مدیر غیرفعال شده است.",
                    )
                )
                continue
            try:
                credential = await self.credentials.credential_for_test(int(row["id"]))
            except CredentialStoreError:
                candidates.append(
                    HealthResult(
                        service=service,
                        provider=provider,
                        credential_id=int(row["id"]),
                        source="database",
                        label=str(row["label"]),
                        masked="••••••••" + str(row["secret_last4"]),
                        status="unavailable",
                        detail="کلید ذخیره‌شده با کلید اصلی فعلی باز نمی‌شود.",
                    )
                )
                continue
            candidates.append(self._credential_result(credential))
        return candidates

    def _environment_credential(self, service: str, provider: str) -> ProviderCredential | None:
        settings = self.settings
        key: str | None
        base: str | None = None
        if service == "stt" and provider == "speechmatics":
            key, base = settings.speechmatics_api_key, settings.speechmatics_base_url
        elif service == "stt" and provider == "deepgram":
            key = settings.deepgram_api_key
        elif service == "stt" and provider == "openai_compatible":
            key, base = settings.stt_openai_api_key, settings.stt_openai_base_url
        elif service == "notes" and provider in {"gemini", "anthropic", "openai_compatible"}:
            if settings.note_api_provider != provider:
                return None
            key, base = settings.effective_note_api_key, settings.note_api_base_url
        else:
            return None
        if not key and not (provider == "openai_compatible" and base):
            return None
        return ProviderCredential(
            id=None,
            service=service,
            provider=provider,
            label="environment",
            secret=key or "",
            last4=(key or "")[-4:],
            base_url=base,
            source="environment",
        )

    @staticmethod
    def _credential_result(credential: ProviderCredential) -> HealthResult:
        return HealthResult(
            service=credential.service,
            provider=credential.provider,
            credential_id=credential.id,
            source=credential.source,
            label=credential.label,
            masked=credential.masked,
            status="configured",
            cooldown_until=None,
        )

    async def providers(self) -> list[tuple[str, str]]:
        """Every provider the deployment can talk about, disabled ones included."""
        pairs: list[tuple[str, str]] = []
        summaries = await self.credentials.list_summaries()
        stored = {(str(row["service"]), str(row["provider"])) for row in summaries}
        for service, providers in PROVIDER_CHOICES.items():
            for provider in sorted(providers):
                if self._environment_credential(service, provider) is not None or (
                    service, provider
                ) in stored:
                    pairs.append((service, provider))
        return pairs

    # -- checks ---------------------------------------------------------------
    async def check(
        self, service: str, provider: str, *, force: bool = False
    ) -> list[HealthResult]:
        """Health for one provider: one row per credential, plus a fallback row."""
        results = await self._stored_candidates(service, provider)
        environment = self._environment_credential(service, provider)
        if environment is not None:
            results.append(self._credential_result(environment))
        if not results:
            return [
                HealthResult(
                    service=service,
                    provider=provider,
                    credential_id=None,
                    source="none",
                    label="—",
                    masked="—",
                    status="not_configured",
                    detail="کلیدی برای این سرویس تنظیم نشده است.",
                )
            ]
        # Probes run concurrently so a panel with several keys answers in about
        # one timeout, not one timeout per key. ``gather`` keeps the pool order.
        pending: list = []
        checked: list[HealthResult] = []
        for result in results:
            if result.status in {"disabled", "unavailable"}:
                checked.append(result)
                continue
            pending.append(self._probe(result, force=force))
        if pending:
            checked.extend(await asyncio.gather(*pending))
        return checked

    async def check_all(self, *, force: bool = False) -> list[HealthResult]:
        pairs = await self.providers()
        if not pairs:
            return []
        groups = await asyncio.gather(
            *(self.check(service, provider, force=force) for service, provider in pairs)
        )
        return [result for group in groups for result in group]

    async def test_credential(self, credential_id: int, *, force: bool = True) -> HealthResult:
        """Manual end-to-end check for one stored credential (admin action)."""
        credential = await self.credentials.credential_for_test(credential_id)
        self.invalidate()
        return await self._probe(
            self._credential_result(credential), force=force, credential=credential
        )

    async def _probe(
        self,
        result: HealthResult,
        *,
        force: bool,
        credential: ProviderCredential | None = None,
    ) -> HealthResult:
        key = (result.service, result.provider, result.credential_id)
        if not force:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return replace(cached[1], cached=True)
        if credential is None:
            credential = await self._materialize(result)
        if credential is None:  # pragma: no cover - stored rows are re-read above
            return result
        probe = build_probe(result.service, result.provider, credential, self.settings)
        secrets = (credential.secret,) if credential.secret else ()
        if not probe.supported:
            outcome = replace(
                result,
                probe_supported=False,
                checked_at=_iso(_utc_now()),
                detail=(
                    f"بررسی خودکار ممکن نیست ({probe.unsupported_reason})؛ "
                    "از تست دستی استفاده کنید."
                ),
            )
            self._remember(key, outcome)
            return outcome
        started = time.perf_counter()
        status_code: int | None = None
        retry_after: float | None = None
        status = "unavailable"
        detail: str | None = None
        try:
            response = await _fetch(probe.method, probe.url, probe.headers, self.timeout_seconds)
            status_code = int(response.status)
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            status = classify_status(status_code)
            if status in {"degraded", "configured"}:
                detail = sanitize_detail(response.text, secrets)
        except asyncio.TimeoutError:
            detail = f"پاسخ در {self.timeout_seconds:.0f} ثانیه دریافت نشد."
        except aiohttp.ClientError as exc:
            detail = sanitize_detail(type(exc).__name__, secrets)
        except Exception as exc:  # pragma: no cover - defensive, still secret-free
            logger.warning(
                "Health probe failed provider=%s/%s error=%s",
                result.service,
                result.provider,
                type(exc).__name__,
            )
            detail = "خطای غیرمنتظره در بررسی سلامت."
        latency_ms = int((time.perf_counter() - started) * 1000)
        outcome = HealthResult(
            service=result.service,
            provider=result.provider,
            credential_id=result.credential_id,
            source=result.source,
            label=result.label,
            masked=result.masked,
            status=status,
            http_status=status_code,
            latency_ms=latency_ms,
            checked_at=_iso(_utc_now()),
            detail=detail,
        )
        await self._apply_rotation_state(credential, outcome, retry_after)
        self._remember(key, outcome)
        return outcome

    async def _materialize(self, result: HealthResult) -> ProviderCredential | None:
        if result.credential_id is None:
            return self._environment_credential(result.service, result.provider)
        try:
            return await self.credentials.credential_for_test(result.credential_id)
        except CredentialStoreError:
            return None

    async def _apply_rotation_state(
        self, credential: ProviderCredential, outcome: HealthResult, retry_after: float | None
    ) -> None:
        """Feed the probe result into the same state live requests use."""
        try:
            if outcome.status == "healthy":
                await self.credentials.record_result(
                    credential, result="success", status_code=outcome.http_status
                )
            elif outcome.status == "rate_limited":
                await self.credentials.record_result(
                    credential,
                    result="cooldown",
                    status_code=outcome.http_status,
                    retry_after_seconds=retry_after,
                    safe_error="health probe: HTTP 429",
                )
            elif outcome.status == "authentication_failed":
                await self.credentials.record_result(
                    credential,
                    result="quarantined",
                    status_code=outcome.http_status,
                    safe_error="health probe: authentication failed",
                )
            elif outcome.status in {"degraded", "unavailable"}:
                await self.credentials.record_result(
                    credential,
                    result="failure",
                    status_code=outcome.http_status,
                    safe_error=sanitize_detail(outcome.detail, (credential.secret,)),
                )
        except Exception:
            logger.warning(
                "Could not persist health state provider=%s/%s credential=%s",
                credential.service,
                credential.provider,
                credential.id,
            )

    def _remember(self, key: tuple[str, str, int | None], result: HealthResult) -> None:
        if self.cache_seconds:
            self._cache[key] = (time.monotonic() + self.cache_seconds, result)

    def cached(self) -> list[HealthResult]:
        """Last known result per credential, expired entries dropped."""
        now = time.monotonic()
        return [result for expires, result in self._cache.values() if expires > now]

    def invalidate(self) -> None:
        self._cache.clear()


def cooldown_remaining_seconds(value: object, *, now: datetime | None = None) -> int:
    """Whole seconds left in a persisted cooldown; zero when not cooling down."""
    if not value:
        return 0
    reference = now or _utc_now()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    remaining = parsed.astimezone(timezone.utc) - reference
    return max(0, int(remaining.total_seconds()))


def cooldown_until_from(seconds: float, *, now: datetime | None = None) -> str:
    reference = now or _utc_now()
    return (reference + timedelta(seconds=max(0.0, float(seconds)))).isoformat()
