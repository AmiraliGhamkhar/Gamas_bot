"""Cheap, manual, cached provider health checks for the admin panel.

Every check is a single read-only *list* request (jobs, projects or models)
that costs nothing and never submits audio or a prompt: no paid STT job and no
LLM generation is ever started from here. Results are cached per credential
for :data:`CACHE_TTL_SECONDS` so repeated taps in Telegram do not hammer an
API, and only sanitized metadata (state, HTTP code, latency, a short provider
reason) is returned — never a key, a response body, or request headers.

States
    healthy                2xx within the latency budget
    degraded               2xx but slow, or an unexpected non-auth 4xx
    rate_limited           HTTP 429 (cooldown honours Retry-After)
    authentication_failed  401/403, or Gemini's 400 API_KEY_INVALID
    unavailable            5xx, timeout or network failure
    disabled               the stored key is switched off by an admin
    not_configured         no key for this provider
    configured             a key exists but has not been checked yet
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

from .provider_credentials import PROVIDER_CHOICES, ProviderCredential
from .structuring import is_invalid_key_error

CACHE_TTL_SECONDS = 60
CHECK_TIMEOUT_SECONDS = 10
DEGRADED_LATENCY_MS = 4_000

STATES = (
    "configured", "healthy", "degraded", "rate_limited", "authentication_failed",
    "unavailable", "disabled", "not_configured",
)
STATE_LABELS = {
    "configured": "⚪️ پیکربندی‌شده (بررسی نشده)",
    "healthy": "🟢 سالم",
    "degraded": "🟡 کند/ناپایدار",
    "rate_limited": "🟠 محدودیت نرخ (429)",
    "authentication_failed": "🔴 خطای احراز هویت",
    "unavailable": "🔴 در دسترس نیست",
    "disabled": "⚫️ غیرفعال",
    "not_configured": "⚪️ پیکربندی نشده",
}
PROVIDER_LABELS = {
    ("stt", "speechmatics"): "Speechmatics",
    ("stt", "deepgram"): "Deepgram",
    ("stt", "openai_compatible"): "STT سازگار با OpenAI",
    ("notes", "gemini"): "Gemini",
    ("notes", "anthropic"): "Anthropic",
    ("notes", "openai_compatible"): "LLM سازگار با OpenAI",
}


@dataclass(frozen=True, slots=True)
class HealthResult:
    service: str
    provider: str
    state: str
    status_code: int | None = None
    latency_ms: int | None = None
    detail: str | None = None
    retry_after_seconds: int | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    masked_key: str = ""
    cached: bool = False


_CACHE: dict[tuple, tuple[float, HealthResult]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def mask_secret(secret: str | None) -> str:
    """The one display form for keys: eight bullets plus the last four."""
    return "••••••••" + (secret or "")[-4:] if secret else "—"


def sanitize_detail(text: str | None, secret: str | None = None, limit: int = 160) -> str | None:
    """Short, single-line, secret-free provider reason for display."""
    if not text:
        return None
    value = str(text)
    if secret:
        value = value.replace(secret, mask_secret(secret))
    # Bearer tokens / long opaque strings that could be credentials.
    value = re.sub(r"(?i)(bearer|token|key)[=: ]+\S+", r"\1=••••", value)
    value = re.sub(r"[A-Za-z0-9_\-]{32,}", "••••", value)
    value = " ".join(value.split())
    return value[:limit] or None


def _request_for(credential: ProviderCredential, settings) -> tuple[str, dict[str, str]]:
    """Return (url, headers) of the provider's free read-only listing endpoint."""
    key = credential.secret or ""
    service, provider = credential.service, credential.provider
    if service == "stt" and provider == "speechmatics":
        base = (credential.base_url or settings.speechmatics_base_url).rstrip("/")
        return f"{base}/jobs?limit=1", {"Authorization": f"Bearer {key}"}
    if service == "stt" and provider == "deepgram":
        return "https://api.deepgram.com/v1/projects", {"Authorization": f"Token {key}"}
    if provider == "openai_compatible":
        default = settings.stt_openai_base_url if service == "stt" else (
            settings.note_api_base_url if settings.note_api_provider == "openai_compatible" else None
        )
        base = (credential.base_url or default or "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return f"{base}/models", headers
    if service == "notes" and provider == "gemini":
        default = settings.note_api_base_url if settings.note_api_provider == "gemini" else None
        base = (credential.base_url or default or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        return f"{base}/models?pageSize=1", {"x-goog-api-key": key}
    if service == "notes" and provider == "anthropic":
        default = settings.note_api_base_url if settings.note_api_provider == "anthropic" else None
        base = (credential.base_url or default or "https://api.anthropic.com/v1").rstrip("/")
        return f"{base}/models?limit=1", {"x-api-key": key, "anthropic-version": "2023-06-01"}
    raise ValueError("Unsupported provider")


def _provider_reason(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        for item in error.get("details") or []:
            if isinstance(item, dict) and item.get("reason"):
                return str(item["reason"])
        for name in ("status", "type", "code"):
            if isinstance(error.get(name), str):
                return error[name]
    if isinstance(error, str):
        return error
    return None


def classify(status: int | None, latency_ms: int | None, body: bytes = b"") -> str:
    if status is None:
        return "unavailable"
    if 200 <= status < 300:
        return "degraded" if (latency_ms or 0) > DEGRADED_LATENCY_MS else "healthy"
    if status == 429:
        return "rate_limited"
    if status in (401, 403) or is_invalid_key_error(status, body):
        return "authentication_failed"
    if status >= 500 or status in (408, 425):
        return "unavailable"
    return "degraded"


async def health_check(
    provider: str,
    credential: ProviderCredential,
    *,
    settings,
    session: aiohttp.ClientSession | None = None,
    use_cache: bool = True,
) -> HealthResult:
    """Check one credential of ``provider`` with a single free request."""
    service = credential.service
    if service not in PROVIDER_CHOICES or provider not in PROVIDER_CHOICES[service]:
        raise ValueError("Unsupported provider")
    if credential.provider != provider:
        raise ValueError("Credential does not belong to this provider")
    cache_key = (service, provider, credential.id, credential.last4, credential.base_url)
    now = time.monotonic()
    if use_cache and cache_key in _CACHE:
        stamp, cached = _CACHE[cache_key]
        if now - stamp < CACHE_TTL_SECONDS:
            return HealthResult(**{**_as_dict(cached), "cached": True})
    if not credential.secret and not (provider == "openai_compatible" and credential.base_url):
        return HealthResult(service, provider, "not_configured")
    url, headers = _request_for(credential, settings)
    owned = session is None
    client = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT_SECONDS)
    )
    started = time.monotonic()
    status: int | None = None
    body = b""
    retry_after: int | None = None
    detail: str | None = None
    try:
        async with client.get(url, headers=headers, allow_redirects=False) as response:
            status = response.status
            body = await response.content.read(16_384)
            raw_retry = response.headers.get("Retry-After", "")
            if raw_retry.strip().isdigit():
                retry_after = int(raw_retry.strip())
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        detail = "timeout"
    except aiohttp.ClientError as exc:
        detail = type(exc).__name__
    finally:
        if owned:
            await client.close()
    latency = int((time.monotonic() - started) * 1000)
    state = classify(status, latency, body)
    if status is not None and not 200 <= status < 300:
        detail = _provider_reason(body) or f"HTTP {status}"
    result = HealthResult(
        service=service,
        provider=provider,
        state=state,
        status_code=status,
        latency_ms=latency,
        detail=sanitize_detail(detail, credential.secret),
        retry_after_seconds=retry_after,
        masked_key=mask_secret(credential.secret) if credential.secret else "بدون کلید",
    )
    _CACHE[cache_key] = (now, result)
    return result


def _as_dict(result: HealthResult) -> dict:
    return {name: getattr(result, name) for name in HealthResult.__slots__}


def environment_credentials(settings) -> list[ProviderCredential]:
    """Static env-configured keys, so the panel covers non-vault setups too."""
    pairs = [
        ("stt", "speechmatics", settings.speechmatics_api_key, settings.speechmatics_base_url, None),
        ("stt", "deepgram", settings.deepgram_api_key, None, None),
        ("stt", "openai_compatible", settings.stt_openai_api_key, settings.stt_openai_base_url, None),
    ]
    if settings.note_api_provider in PROVIDER_CHOICES["notes"]:
        pairs.append((
            "notes", settings.note_api_provider, settings.effective_note_api_key,
            settings.note_api_base_url, settings.note_api_model,
        ))
    result = []
    for service, provider, secret, base_url, model in pairs:
        if not secret and not (provider == "openai_compatible" and base_url):
            continue
        result.append(ProviderCredential(
            id=None, service=service, provider=provider, label="environment",
            secret=secret or "", last4=(secret or "")[-4:], base_url=base_url, model=model,
            source="environment",
        ))
    return result
