"""The single LLM client for note generation: ordered providers, bounded retries.

``NOTE_PROVIDERS`` *is* the failover policy (``avalai,gemini,openrouter`` is
the shipped project default — AvalAI first, then each further approved
provider only when it is listed *and* has its own credential; a deployment can
narrow it, e.g. ``gemini,openrouter``). For every route the
client builds that provider's own request
(base URL, authentication, model, supported parameters), parses its own response
shape, retries only transient failures — a bounded budget per provider — and
then moves to the next eligible provider. Nothing is retried forever and a
permanent 4xx is never re-sent unchanged.

Everything the note pipeline needs is :func:`complete` (plain text) and
:func:`complete_json` (strict JSON, one deterministic retry, then failover).

Two size failures are told apart because only one of them is fixed by asking
again with less input: :class:`LLMContextOverflowError` (the *input* was too
big — HTTP 413, or a 400 that says so) makes the caller split the transcript,
while :class:`LLMOutputBudgetError` (the *answer* was cut off by
``max_tokens``) makes the caller raise ``NOTE_MAX_OUTPUT_TOKENS`` instead.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import replace
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .capacity import (
    CapacityOverloadError,
    LoopCooldownRegistry,
    pool_for,
    reset_pools,
)
from .config import APPROVED_LLM_PROVIDERS, NoteRoute, Settings, note_routes
from .http_util import (
    read_error_body,
    retry_after_seconds as _retry_after_seconds,
    sanitize_error_text as _sanitize_error_text,
)
from .llm_profiles import (
    get_provider_profile,
    to_gemini_schema,
)
from .metrics import METRICS
from .llm_quota import ProviderBudget, QuotaUnavailable, finish as finish_quota, reserve as reserve_quota, estimate_tokens
from .protocols import LLMProviderDialect

try:
    import orjson as _orjson

    _loads = _orjson.loads
except ModuleNotFoundError:  # stdlib fallback: identical parsing contract
    _orjson = None  # type: ignore[assignment]

    def _loads(body):
        return json.loads(body.decode("utf-8", "replace"))


MAX_LLM_RESPONSE_BYTES = 16 * 1024 * 1024


async def _read_bounded_body(response: Any, max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` bytes, using the response stream if present."""
    headers = getattr(response, "headers", None) or {}
    try:
        declared = int(headers.get("Content-Length", ""))
    except (TypeError, ValueError):
        declared = None
    if declared is not None and declared > max_bytes:
        raise LLMResponseError("حجم پاسخ سرویس تولید جزوه بیش از حد مجاز بود.")

    content = getattr(response, "content", None)
    reader = getattr(content, "read", None)
    if not callable(reader):
        # Compatibility for tiny test doubles; a real aiohttp response always
        # exposes ``content.read`` and follows the bounded streaming branch.
        body = await response.read()
        if len(body) > max_bytes:
            raise LLMResponseError("حجم پاسخ سرویس تولید جزوه بیش از حد مجاز بود.")
        return body

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await reader(min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise LLMResponseError("حجم پاسخ سرویس تولید جزوه بیش از حد مجاز بود.")
    return b"".join(chunks)


async def _read_json_body(response: Any) -> Any:
    """Parse a bounded response body via :data:`_loads`.

    One narrow compatibility fallback: if the bytes parse to an empty object,
    ``response.json()`` is consulted for test doubles whose ``read()`` is a
    stub. Real aiohttp parses the same cached bytes.
    """
    raw = await _read_bounded_body(response, MAX_LLM_RESPONSE_BYTES)
    result = _loads(raw)
    if isinstance(result, dict) and not result:
        json_method = getattr(response, "json", None)
        if json_method is not None:
            try:
                alternative = await json_method(content_type=None)
            except (ValueError, TypeError):
                alternative = None
            if alternative:
                return alternative
    return result

logger = logging.getLogger(__name__)

#: Sampling hint for structured note generation. Low, because the note task is
#: extraction and organisation, not creative writing.
NOTE_TEMPERATURE = 0.2

#: A hung note-provider request must not inherit the STT job's multi-hour read
#: timeout. This is the hard fallback for a request whose settings carry no
#: deadline; the operator's ``NOTE_REQUEST_TIMEOUT_SECONDS`` is the one in use
#: (``Settings.note_request_timeout``), because the right deadline depends on
#: how long the configured output budget takes to produce on the gateway.
LLM_REQUEST_TIMEOUT_SECONDS = 300.0

#: Bounded retries for transient LLM failures (5xx, 429, network).
MAX_TRANSIENT_RETRIES = 1

#: A provider can return an arbitrary ``Retry-After`` (including an HTTP-date
#: days ahead). Never let that remote hint keep a scarce note-provider lease
#: occupied indefinitely; the chain either retries within this cap or fails
#: over. This mirrors the bounded STT retry policy.
MAX_LLM_RETRY_AFTER_SECONDS = 60.0

#: Connection establishment has a much shorter budget than a valid long-form
#: completion. A dead route must not consume the whole note request deadline
#: before the provider chain can try its next eligible leg.
LLM_CONNECT_TIMEOUT_SECONDS = 10.0

#: Maximum number of request-parameter compatibility adjustments per attempt
#: (Gemini ``responseSchema``/``thinkingConfig`` removal, ``response_format``
#: downgrades, ``max_completion_tokens`` renames, …). Each adjustment costs one
#: extra real request, so the count is bounded even if a gateway keeps
#: rejecting a field — or rejects two spellings alternately.
MAX_COMPATIBILITY_ADJUSTMENTS = 4

_T = TypeVar("_T")
_ACTUAL_USAGE_TOKENS: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "gamas_llm_actual_usage_tokens", default=None
)

#: Loop- and session-scoped route cooldown map so concurrent requests sharing
#: a client session immediately bypass a rate-limited/depleted primary without
#: colliding across separate sessions or test cases.
_LLM_COOLDOWNS = LoopCooldownRegistry(clock=lambda: time.monotonic())

LLM_RATE_LIMIT_COOLDOWN_SECONDS = 15.0
LLM_AUTH_COOLDOWN_SECONDS = 60.0


def _set_route_cooldown(session: object, slug: str, seconds: float) -> None:
    _LLM_COOLDOWNS.set(session, slug, seconds)


def _route_cooldown_remaining(session: object, slug: str) -> float:
    return _LLM_COOLDOWNS.remaining(session, slug)


def reset_cooldowns() -> None:
    """Clear LLM route cooldowns and local capacity pools."""
    _LLM_COOLDOWNS.clear()
    reset_pools()


def route_max_in_flight(index: int, settings: Settings) -> int:
    """Local concurrency limit for route position *index* (0 = primary)."""
    if index <= 0:
        return max(1, int(getattr(settings, "llm_primary_max_in_flight", 4)))
    return max(1, int(getattr(settings, "llm_fallback_max_in_flight", 4)))


class LLMError(RuntimeError):
    """Base class; the message is safe to show the user."""


class LLMTransientError(LLMError):
    """Network/rate-limit/server failure after the bounded retry budget."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        rate_limited: bool = False,
    ):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.rate_limited = rate_limited


class LLMAuthError(LLMError):
    """The provider rejected our credentials (no retry)."""


class LLMResponseError(LLMError):
    """The answer was malformed, blocked or empty."""


class LLMRequestError(LLMResponseError):
    """The provider rejected the request (retrying it with new content cannot help)."""


class LLMContextOverflowError(LLMRequestError):
    """The request was larger than the model's context window.

    Recoverable — but not by asking the same provider again with the same
    content: the caller splits the input at a safe boundary (or lets the next
    provider in the chain answer).
    """


class LLMOutputBudgetError(LLMRequestError):
    """The answer was cut off by the *output* budget, not by a broken model.

    Reasoning models spend thinking tokens out of the same ``max_tokens``
    budget, so a small budget can return an empty or half-finished answer.
    Re-sending the same request with the same budget produces the
    same truncation, so this is treated as permanent for the provider — the
    fix is a larger ``NOTE_MAX_OUTPUT_TOKENS``, or the next provider in the
    chain, never another identical attempt.
    """


#: Structured error codes/types that providers use to report an input/context
#: token limit failure (OpenRouter ``error.metadata.error_type``,
#: OpenAI-compatible gateways' ``error.code``/``error.type``).
CONTEXT_OVERFLOW_CODES = frozenset(
    {
        "CONTEXT_LENGTH_EXCEEDED",
        "TOKEN_LIMIT_EXCEEDED",
        "MAX_TOKENS_EXCEEDED",
        "STRING_TOO_LONG",
        "STRING_ABOVE_MAX_LENGTH",
        "PAYLOAD_TOO_LARGE",
        "PROMPT_TOO_LONG",
        "INPUT_TOO_LONG",
    }
)

#: Structured error codes/types that mark a failure as transient even when an
#: upstream gateway wraps it in an HTTP 200 in-body error envelope.
TRANSIENT_ERROR_CODES = frozenset(
    {
        "RATE_LIMIT_EXCEEDED",
        "RATE_LIMITED",
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_OVERLOADED",
        "TIMEOUT",
        "SERVER",
        "SERVICE_UNAVAILABLE",
        "INTERNAL_ERROR",
        "DEADLINE_EXCEEDED",
        "UNAVAILABLE",
        "IN_FLIGHT_BUDGET_EXHAUSTED",
        "OPENROUTER_IN_FLIGHT_BUDGET",
    }
)

#: Provider wording for "the input is larger than my context window". Providers
#: are inconsistent here, so the classification is deliberately generous: it is
#: only used to choose between splitting the input and giving up on it.
CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "max context",
    "input exceeds",
    "exceeds the maximum",
    "exceed the maximum",
    "exceeds the limit",
    "payload size exceeds",
    "too many tokens",
    "token limit",
    "token_limit_exceeded",
    "input token limit",
    "input_token_limit",
    "input is too long",
    "prompt is too long",
    "string_too_long",
    "reduce the length",
    "max_tokens is too large",
    "max_tokens_exceeded",
    "maximum tokens",
    "request too large",
    "context overflow",
    "context limit",
    # Gateways may answer an oversized body with HTTP 413 and the generic
    # wording "The request body or input is too large."
    "is too large",
    "input too large",
    "payload too large",
    "body too large",
)

_SECRET_TOKEN_RE = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9._\-]+|sk-nry-[A-Za-z0-9_\-]+|sk-or-v1-[A-Za-z0-9_\-]+|sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{20,})",
    re.IGNORECASE,
)


def is_context_overflow(detail: str) -> bool:
    """True when a provider 400 describes an oversized input."""
    haystack = detail.lower()
    return any(marker in haystack for marker in CONTEXT_OVERFLOW_MARKERS)


def _endpoint(base_url: str, suffix: str) -> str:
    """Append an API path without breaking an exact endpoint's query string."""
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    suffix_path = "/" + suffix.strip("/")
    if base_path.endswith("/chat/completions") and suffix_path != "/chat/completions":
        base_path = base_path[: -len("/chat/completions")].rstrip("/")
    if base_path.endswith("/models") and suffix_path.startswith("/models/"):
        base_path = base_path[: -len("/models")].rstrip("/")
    path = base_path if base_path.endswith(suffix_path) else base_path + suffix_path
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _redact_secrets(text: str, api_key: str | None) -> str:
    if not text:
        return text
    if api_key:
        clean_key = re.sub(r"^bearer\s+", "", api_key.strip(), flags=re.IGNORECASE).strip()
        for candidate in (api_key, clean_key):
            if candidate and candidate in text:
                text = text.replace(candidate, "***")
    return _SECRET_TOKEN_RE.sub("***", text)


#: Correlation headers emitted by the gateways we support. AvalAI's exact
#: ``avalai-request-id`` is needed when an operator reconciles one completion's
#: cost in its User API; ``x-request-id`` remains the portable fallback used by
#: other OpenAI-compatible providers.
_REQUEST_ID_HEADERS = ("avalai-request-id", "x-request-id")


def _header_value(headers: Any, *names: str) -> str:
    """Get a non-blank HTTP header from real or tiny-test-double mappings.

    ``aiohttp.CIMultiDictProxy`` is case-insensitive, while the deliberately
    simple dictionaries in offline tests are not. Scan their items only after
    direct lookups so production stays on the efficient normal path.
    """
    if not headers:
        return ""
    getter = getattr(headers, "get", None)
    if callable(getter):
        for name in names:
            for spelling in (name, name.title(), name.upper()):
                try:
                    value = getter(spelling)
                except (AttributeError, TypeError):
                    value = None
                if isinstance(value, str) and value.strip():
                    return value
    items = getattr(headers, "items", None)
    if callable(items):
        wanted = {name.lower() for name in names}
        try:
            for key, value in items():
                if str(key).lower() in wanted and isinstance(value, str) and value.strip():
                    return value
        except (AttributeError, TypeError, ValueError):
            # Header diagnostics must never hide the response's real error.
            pass
    return ""


def _payload_request_id(payload: object) -> str:
    """Read the conventional request id in a gateway JSON envelope."""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        candidate = error.get("request_id")
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    candidate = payload.get("request_id")
    return candidate if isinstance(candidate, str) and candidate.strip() else ""


def _response_request_id(
    headers: Any,
    *,
    payload: object = None,
    api_key: str | None = None,
) -> str:
    """Return a safe provider correlation id, preferring AvalAI's cost id.

    AvalAI documents ``avalai-request-id`` on every response and accepts that
    exact value for precise cost lookup. Prefer it even if an upstream model
    also puts its own id in the JSON body; otherwise retain the established
    body-id then ``x-request-id`` fallback for generic gateways.
    """
    request_id = _header_value(headers, _REQUEST_ID_HEADERS[0])
    if not request_id:
        request_id = _payload_request_id(payload)
    if not request_id:
        request_id = _header_value(headers, _REQUEST_ID_HEADERS[1])
    if not request_id:
        return ""
    return _redact_secrets(_sanitize_error_text(request_id, limit=80), api_key)


def _extract_raw_upstream_message(raw_value: object) -> str | None:
    """Extract a human-readable message from OpenRouter's ``error.metadata.raw``."""
    if isinstance(raw_value, dict):
        err = raw_value.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return _sanitize_error_text(err["message"])
        if isinstance(raw_value.get("message"), str):
            return _sanitize_error_text(raw_value["message"])
        return None
    if isinstance(raw_value, str) and raw_value.strip():
        text = raw_value.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                return _sanitize_error_text(text)
            return _extract_raw_upstream_message(parsed) or _sanitize_error_text(text)
        return _sanitize_error_text(text)
    return None


def _error_from_payload(
    label: str,
    status: int,
    payload: object,
    headers: Any,
    api_key: str | None,
) -> LLMError:
    detail: str | None = None
    # The provider's own correlation key. AvalAI's documented
    # ``avalai-request-id`` header takes precedence, because it is the id its
    # User API uses for exact cost reconciliation; a generic ``request_id``
    # field in an error envelope is used otherwise.
    request_id = _response_request_id(headers, payload=payload, api_key=api_key)
    error_markers: list[str] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                detail = _sanitize_error_text(message)
            for field in ("status", "code", "type"):
                value = error.get(field)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    error_markers.append(str(value).upper())
            metadata = error.get("metadata")
            if isinstance(metadata, dict):
                for meta_field in ("error_type", "reason", "limit_source"):
                    meta_val = metadata.get(meta_field)
                    if isinstance(meta_val, str) and meta_val.strip():
                        error_markers.append(meta_val.strip().upper())
                raw_msg = _extract_raw_upstream_message(metadata.get("raw"))
                if raw_msg and (
                    not detail
                    or detail.lower() in ("provider returned error", "error", "unknown error")
                ):
                    detail = f"{detail}: {raw_msg}" if detail else raw_msg
            details = error.get("details")
            if isinstance(details, list):
                for item in details:
                    if not isinstance(item, dict):
                        continue
                    reason = item.get("reason")
                    item_meta = item.get("metadata")
                    if isinstance(reason, str) and reason.strip():
                        error_markers.append(reason.strip().upper())
                    if isinstance(item_meta, dict):
                        meta_reason = item_meta.get("reason")
                        if isinstance(meta_reason, str) and meta_reason.strip():
                            error_markers.append(meta_reason.strip().upper())
                    if not detail and isinstance(item.get("message"), str):
                        detail = _sanitize_error_text(item["message"])
        elif isinstance(error, str) and error.strip():
            detail = _sanitize_error_text(error)
    codes = ""
    if error_markers:
        codes = _sanitize_error_text(", ".join(dict.fromkeys(error_markers)), limit=80)
        detail = f"{detail} [{codes}]" if detail else codes
    if detail:
        detail = _redact_secrets(detail, api_key)
    if request_id:
        request_id = _redact_secrets(request_id, api_key)
    suffix = f": {detail}" if detail else ""
    if request_id:
        suffix = f"{suffix} [request_id={request_id}]"
    lower_detail = (detail or "").lower()
    marker_set = frozenset(error_markers)

    # OpenRouter documents HTTP 402 with limit_source="openrouter_in_flight_budget"
    # (reason="in_flight_budget_exhausted") as a transient concurrent reservation
    # cap accompanied by Retry-After, distinct from a depleted credit balance.
    if status == 402 and marker_set & {
        "IN_FLIGHT_BUDGET_EXHAUSTED",
        "OPENROUTER_IN_FLIGHT_BUDGET",
    }:
        return LLMTransientError(
            f"{label} موقتاً در دسترس نیست (HTTP {status}){suffix}",
            retry_after_seconds=_retry_after_seconds(headers, payload),
            rate_limited=True,
        )

    # OpenRouter uses HTTP 403 with error_type="content_policy_violation" or
    # "refusal" for prompt moderation blocks; those are request rejections, not
    # broken credentials.
    is_moderation_403 = status == 403 and bool(
        marker_set & {"CONTENT_POLICY_VIOLATION", "REFUSAL"}
    )
    auth_error = (
        (status in (401, 402, 403) and not is_moderation_403)
        or bool(
            marker_set
            & {
                "API_KEY_INVALID",
                "INVALID_API_KEY",
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "AUTHENTICATION",
                "PAYMENT_REQUIRED",
                "BILLING_DISABLED",
            }
        )
        or (
            "FAILED_PRECONDITION" in marker_set
            and any(w in lower_detail for w in ("billing", "free tier", "country", "key"))
        )
        or "api key not valid" in lower_detail
        or "invalid api key" in lower_detail
    )
    if auth_error:
        return LLMAuthError(f"{label} اعتبارنامه را نپذیرفت (HTTP {status}){suffix}")
    # HTTP 413 is an oversized body ("The request body or input is too large")
    # on every gateway, and OpenRouter/OpenAI-compatible gateways report typed
    # context/token limit codes in error.metadata.error_type / error.code.
    if (
        status == 413
        or bool(marker_set & CONTEXT_OVERFLOW_CODES)
        or (status in (400, 422) and is_context_overflow(f"{detail or ''} {codes}"))
    ):
        return LLMContextOverflowError(
            f"{label} متن ورودی را بزرگ‌تر از پنجرهٔ زمینهٔ خود دانست "
            f"(HTTP {status}){suffix}"
        )
    if (
        status in (408, 409, 425, 429, 499)
        or status >= 500
        or bool(marker_set & TRANSIENT_ERROR_CODES)
    ):
        is_rl = status == 429 or bool(
            marker_set & {"RATE_LIMITED", "RESOURCE_EXHAUSTED", "TOO_MANY_REQUESTS"}
        )
        return LLMTransientError(
            f"{label} موقتاً در دسترس نیست (HTTP {status}){suffix}",
            retry_after_seconds=_retry_after_seconds(headers, payload),
            rate_limited=is_rl,
        )
    return LLMRequestError(f"{label} درخواست را رد کرد (HTTP {status}){suffix}")


async def _error_from_response(
    label: str, response: aiohttp.ClientResponse, api_key: str | None
) -> LLMError:
    body = await read_error_body(response)
    payload: object = None
    try:
        payload = _loads(body)
    except (ValueError, TypeError):
        payload = None
    return _error_from_payload(
        label, response.status, payload, getattr(response, "headers", None), api_key
    )


def _infer_in_body_error_status(error_obj: object) -> int:
    """Infer an HTTP status from an OpenRouter/gateway HTTP-200 in-body error."""
    if isinstance(error_obj, dict):
        for field in ("code", "status"):
            raw = error_obj.get(field)
            if isinstance(raw, int) and not isinstance(raw, bool) and 400 <= raw <= 599:
                return raw
            if isinstance(raw, str) and raw.strip().isdigit():
                val = int(raw.strip())
                if 400 <= val <= 599:
                    return val
        metadata = error_obj.get("metadata")
        error_type = ""
        if isinstance(metadata, dict) and isinstance(metadata.get("error_type"), str):
            error_type = metadata["error_type"].strip().lower()
        elif isinstance(error_obj.get("type"), str):
            error_type = error_obj["type"].strip().lower()
        status_by_type = {
            "context_length_exceeded": 400,
            "max_tokens_exceeded": 400,
            "token_limit_exceeded": 400,
            "string_too_long": 400,
            "invalid_request": 400,
            "invalid_prompt": 400,
            "validation_error": 400,
            "authentication": 401,
            "unauthorized": 401,
            "payment_required": 402,
            "permission_denied": 403,
            "forbidden": 403,
            "content_policy_violation": 403,
            "refusal": 403,
            "not_found": 404,
            "timeout": 504,
            "precondition_failed": 412,
            "payload_too_large": 413,
            "unprocessable": 422,
            "rate_limit_exceeded": 429,
            "rate_limited": 429,
            "server": 500,
            "internal_error": 500,
            "unmapped": 500,
            "provider_unavailable": 502,
            "provider_overloaded": 503,
            "service_unavailable": 503,
        }
        if error_type in status_by_type:
            return status_by_type[error_type]
    return 502


def _has_error_content(error_obj: object) -> bool:
    if isinstance(error_obj, str):
        return bool(error_obj.strip())
    if isinstance(error_obj, dict):
        return any(val is not None and val != "" for val in error_obj.values())
    return False


def _raise_if_in_body_error(
    label: str,
    payload: dict,
    headers: Any,
    api_key: str | None,
) -> None:
    """Detect upstream errors delivered inside an HTTP 200 JSON body.

    OpenRouter documents two non-streaming HTTP 200 failure shapes when an
    upstream provider fails after OpenRouter has already sent ``200 OK``
    headers (https://openrouter.ai/docs/api_reference/errors-and-debugging):
    1. A top-level ``{"error": {...}}`` object instead of ``choices``.
    2. ``choices[0]`` carrying ``"finish_reason": "error"`` and/or a nested
       ``choices[0]["error"]`` object alongside partial ``message.content``.
    """
    top_error = payload.get("error")
    if _has_error_content(top_error):
        status = _infer_in_body_error_status(top_error)
        raise _error_from_payload(label, status, payload, headers, api_key)

    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        first = choices[0]
        choice_error = first.get("error")
        finish_reason = str(first.get("finish_reason") or "").strip().lower()
        native_reason = str(first.get("native_finish_reason") or "").strip().lower()
        if _has_error_content(choice_error):
            wrapped: dict[str, object] = {"error": choice_error}
            if "request_id" in payload:
                wrapped["request_id"] = payload["request_id"]
            status = _infer_in_body_error_status(choice_error)
            raise _error_from_payload(label, status, wrapped, headers, api_key)
        if finish_reason == "error" or native_reason == "error":
            raise LLMTransientError(
                f"{label} تولید پاسخ را به دلیل خطای بالادستی ناتمام گذاشت "
                "(finish_reason=error)."
            )


def _clean_api_key(key: str | None) -> str:
    raw = (key or "").strip()
    if not raw:
        return ""
    return re.sub(r"^bearer\s+", "", raw, flags=re.IGNORECASE).strip()


def _gemini_model_id(model: str) -> str:
    """Normalize a Gemini model name for ``/v1beta/models/<id>`` URL paths."""
    raw = model.strip().removeprefix("models/")
    for suffix in (":generateContent", ":streamGenerateContent"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
    return quote(raw.strip(), safe="")


def _openrouter_model_path(model: str) -> str:
    """Encode an OpenRouter ``author/slug[:variant]`` model id for URL paths."""
    parts = [part for part in model.strip().split("/") if part]
    return "/".join(quote(part, safe=":-._~") for part in parts)


def _gemini_schema(node: object) -> object:
    """Convert a JSON Schema to Gemini's OpenAPI 3.0 ``responseSchema`` dialect."""
    return to_gemini_schema(node)


#: Gemini FinishReason values that report safety, policy or recitation blocks
#: (https://ai.google.dev/api/generate-content#FinishReason).
GEMINI_BLOCKED_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "PROHIBITED_CONTENT",
        "BLOCKLIST",
        "SPII",
        "LANGUAGE",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
        "ESCALATION",
        "PUP_LIMITED_DISABLED",
    }
)

#: Gemini FinishReason values that report a malformed generation or signature error.
GEMINI_MALFORMED_FINISH_REASONS = frozenset(
    {
        "MALFORMED_FUNCTION_CALL",
        "MALFORMED_RESPONSE",
        "UNEXPECTED_TOOL_CALL",
        "TOO_MANY_TOOL_CALLS",
        "MISSING_THOUGHT_SIGNATURE",
    }
)

#: OpenAI/OpenRouter finish_reason / native_finish_reason values for content filters.
OPENAI_BLOCKED_FINISH_REASONS = frozenset(
    {
        "content_filter",
        "safety",
        "recitation",
        "prohibited_content",
        "blocklist",
        "spii",
    }
)


def _usage_count(value: object) -> int | None:
    """One token count from a provider usage block, or ``None`` if unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


class OpenAIDialect:
    """OpenAI-compatible wire-format dialect (AvalAI, OpenRouter, generic)."""

    style = "openai"

    def build_post_request(
        self,
        route: NoteRoute,
        settings: Settings,
        system: str,
        user: str,
        *,
        schema: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        key = _clean_api_key(route.api_key)
        model = route.model.strip()
        profile = get_provider_profile(route.slug)
        streaming = float(getattr(settings, "note_ttfb_timeout", 0.0) or 0.0) > 0
        max_tokens = profile.clamp_output_tokens(route, settings.note_max_output_tokens)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": NOTE_TEMPERATURE,
            "max_tokens": max_tokens,
        }
        response_format = profile.build_response_format(
            route, settings, schema, streaming=streaming
        )
        if response_format is not None:
            payload["response_format"] = response_format
        profile.apply_openai_extras(payload, route, settings)
        return _endpoint(route.base_url, "chat/completions"), headers, payload

    def build_get_request(
        self,
        route: NoteRoute,
        target: str = "models",
    ) -> tuple[str, dict[str, str]]:
        key = _clean_api_key(route.api_key)
        clean_target = target.strip().strip("/")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if clean_target == "model_endpoints":
            model_path = _openrouter_model_path(route.model)
            return _endpoint(route.base_url, f"models/{model_path}/endpoints"), headers
        if clean_target == "model":
            model_path = _openrouter_model_path(route.model)
            return _endpoint(route.base_url, f"models/{model_path}"), headers
        return _endpoint(route.base_url, clean_target or "models"), headers

    def is_output_truncated(self, payload: dict[str, Any]) -> bool:
        try:
            choice = payload["choices"][0]
            finish = str(choice.get("finish_reason") or "").strip().lower()
            native = str(choice.get("native_finish_reason") or "").strip().lower()
            return finish in ("length", "max_tokens", "continuation") or native in (
                "length",
                "max_tokens",
                "continuation",
            )
        except (AttributeError, KeyError, IndexError, TypeError):
            return False

    def is_reasoning_exhausted(self, payload: dict[str, Any]) -> bool:
        usage = payload.get("usage")
        if isinstance(usage, dict):
            details = usage.get("completion_tokens_details")
            if isinstance(details, dict):
                reasoning = _usage_count(details.get("reasoning_tokens"))
                completion = _usage_count(usage.get("completion_tokens"))
                if reasoning is not None and reasoning > 0 and (
                    completion is None or reasoning >= completion
                ):
                    return True
        try:
            message = payload["choices"][0]["message"]
            if isinstance(message, dict):
                for key in ("reasoning", "reasoning_content"):
                    val = message.get(key)
                    if isinstance(val, str) and val.strip():
                        return True
        except (AttributeError, KeyError, IndexError, TypeError):
            pass
        return False

    def parse_response(self, payload: dict[str, Any]) -> str:
        choice = payload["choices"][0]
        if not isinstance(choice, dict):
            raise TypeError("choice is not a dict")
        finish_reason_lower = str(choice.get("finish_reason") or "").strip().lower()
        native_reason_lower = str(choice.get("native_finish_reason") or "").strip().lower()
        blocked_reason = (
            finish_reason_lower
            if finish_reason_lower in OPENAI_BLOCKED_FINISH_REASONS
            else (
                native_reason_lower
                if native_reason_lower in OPENAI_BLOCKED_FINISH_REASONS
                else ""
            )
        )
        message = choice.get("message")
        if not isinstance(message, dict):
            if blocked_reason:
                raise LLMResponseError(
                    f"پاسخ سرویس تولید جزوه مسدود شد ({_sanitize_error_text(blocked_reason)})."
                )
            message = choice["message"]
        content = (
            message.get("content")
            if isinstance(message, dict)
            else choice["message"]["content"]
        )
        if isinstance(content, list):
            text = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
                and part.get("type") not in ("thinking", "reasoning")
                and part.get("thought") is not True
            )
        else:
            text = "" if content is None else str(content)
        if not text.strip():
            if blocked_reason:
                raise LLMResponseError(
                    f"پاسخ سرویس تولید جزوه مسدود شد ({_sanitize_error_text(blocked_reason)})."
                )
            refusal = message.get("refusal") if isinstance(message, dict) else None
            if isinstance(refusal, str) and refusal.strip():
                raise LLMResponseError("پاسخ سرویس تولید جزوه مسدود شد (refusal).")
            if self.is_reasoning_exhausted(payload):
                raise LLMOutputBudgetError(
                    "پاسخ سرویس تولید جزوه پیش از پایان به سقف توکن خروجی رسید "
                    "(NOTE_MAX_OUTPUT_TOKENS را افزایش دهید)."
                )
        return text

    def extract_usage(
        self, payload: dict[str, Any]
    ) -> tuple[str, int | None, int | None, int | None]:
        model = payload.get("model") or payload.get("modelVersion") or ""
        input_tokens = output_tokens = total_tokens = None
        usage = payload.get("usage")
        if isinstance(usage, dict):
            input_tokens = _usage_count(usage.get("prompt_tokens"))
            output_tokens = _usage_count(usage.get("completion_tokens"))
            total_tokens = _usage_count(usage.get("total_tokens"))
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        return str(model), input_tokens, output_tokens, total_tokens


class GeminiDialect:
    """Google Gemini ``generateContent`` wire-format dialect."""

    style = "gemini"

    def build_post_request(
        self,
        route: NoteRoute,
        settings: Settings,
        system: str,
        user: str,
        *,
        schema: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        key = _clean_api_key(route.api_key)
        if not key:
            raise LLMAuthError("کلید سرویس تولید جزوه تنظیم نشده است.")
        model_id = _gemini_model_id(route.model.strip())
        url = _endpoint(route.base_url, f"models/{model_id}:generateContent")
        generation_config: dict[str, Any] = {
            "temperature": NOTE_TEMPERATURE,
            "maxOutputTokens": settings.note_max_output_tokens,
            "responseMimeType": "application/json",
        }
        if schema is not None and bool(
            getattr(settings, "note_structured_output", True)
        ):
            generation_config["responseSchema"] = _gemini_schema(schema)
        if settings.gemini_thinking_budget >= 0:
            generation_config["thinkingConfig"] = {
                "thinkingBudget": settings.gemini_thinking_budget,
            }
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": generation_config,
        }
        return url, {"x-goog-api-key": key}, payload

    def build_get_request(
        self,
        route: NoteRoute,
        target: str = "models",
    ) -> tuple[str, dict[str, str]]:
        key = _clean_api_key(route.api_key)
        if not key:
            raise LLMAuthError("کلید سرویس تولید جزوه تنظیم نشده است.")
        clean_target = target.strip().strip("/")
        headers = {"x-goog-api-key": key}
        if clean_target == "model":
            model_id = _gemini_model_id(route.model)
            return _endpoint(route.base_url, f"models/{model_id}"), headers
        return _endpoint(route.base_url, clean_target or "models"), headers

    def is_output_truncated(self, payload: dict[str, Any]) -> bool:
        try:
            reason = str(payload["candidates"][0].get("finishReason") or "").strip().upper()
            return reason in ("MAX_TOKENS", "CONTINUATION", "LENGTH")
        except (AttributeError, KeyError, IndexError, TypeError):
            return False

    def is_reasoning_exhausted(self, payload: dict[str, Any]) -> bool:
        usage = payload.get("usageMetadata")
        if isinstance(usage, dict):
            thoughts = _usage_count(usage.get("thoughtsTokenCount"))
            if thoughts is not None and thoughts > 0:
                return True
        return False

    def parse_response(self, payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates")
        if not candidates:
            feedback = payload.get("promptFeedback")
            if isinstance(feedback, dict) and isinstance(feedback.get("blockReason"), str):
                reason = _sanitize_error_text(feedback["blockReason"])
                raise LLMResponseError(f"پاسخ سرویس تولید جزوه مسدود شد ({reason}).")
            raise LLMResponseError("پاسخ سرویس تولید جزوه خالی یا نامعتبر است.")
        first_candidate = candidates[0]
        if not isinstance(first_candidate, dict):
            raise TypeError("candidate is not a dict")
        finish_reason = str(first_candidate.get("finishReason") or "").strip().upper()
        if finish_reason in GEMINI_BLOCKED_FINISH_REASONS:
            raise LLMResponseError(
                f"پاسخ سرویس تولید جزوه مسدود شد ({_sanitize_error_text(finish_reason)})."
            )
        if finish_reason in GEMINI_MALFORMED_FINISH_REASONS:
            raise LLMResponseError(
                f"پاسخ سرویس تولید جزوه ناقص یا نامعتبر بود ({_sanitize_error_text(finish_reason)})."
            )
        content_obj = first_candidate.get("content")
        if not isinstance(content_obj, dict) or not isinstance(
            content_obj.get("parts"), list
        ):
            feedback = payload.get("promptFeedback")
            if isinstance(feedback, dict) and isinstance(feedback.get("blockReason"), str):
                reason = _sanitize_error_text(feedback["blockReason"])
                raise LLMResponseError(f"پاسخ سرویس تولید جزوه مسدود شد ({reason}).")
            if self.is_reasoning_exhausted(payload):
                raise LLMOutputBudgetError(
                    "پاسخ سرویس تولید جزوه پیش از پایان به سقف توکن خروجی رسید "
                    "(NOTE_MAX_OUTPUT_TOKENS را افزایش دهید)."
                )
            if content_obj is None or (
                isinstance(content_obj, dict) and "parts" not in content_obj
            ):
                raise LLMResponseError("پاسخ سرویس تولید جزوه خالی بود.")
            raise KeyError("parts")
        parts = content_obj["parts"]
        answer_chunks: list[str] = []
        saw_thought = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("thought") is True:
                saw_thought = True
                continue
            if "text" in part and part.get("text") is not None:
                answer_chunks.append(str(part.get("text", "")))
        text = "".join(answer_chunks)
        if not text.strip() and (saw_thought or self.is_reasoning_exhausted(payload)):
            raise LLMOutputBudgetError(
                "پاسخ سرویس تولید جزوه پیش از پایان به سقف توکن خروجی رسید "
                "(NOTE_MAX_OUTPUT_TOKENS را افزایش دهید)."
            )
        return text

    def extract_usage(
        self, payload: dict[str, Any]
    ) -> tuple[str, int | None, int | None, int | None]:
        model = payload.get("model") or payload.get("modelVersion") or ""
        input_tokens = output_tokens = total_tokens = None
        usage = payload.get("usageMetadata")
        if isinstance(usage, dict):
            input_tokens = _usage_count(usage.get("promptTokenCount"))
            output_tokens = _usage_count(usage.get("candidatesTokenCount"))
            total_tokens = _usage_count(usage.get("totalTokenCount"))
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        return str(model), input_tokens, output_tokens, total_tokens


_DEFAULT_DIALECT: LLMProviderDialect = OpenAIDialect()

#: Wire-format dialects by request style. The approved providers map to these
#: styles in ``config.NOTE_REQUEST_STYLE`` (``avalai``/``openrouter`` ->
#: ``openai``, ``gemini`` -> ``gemini``).
DIALECTS: dict[str, LLMProviderDialect] = {
    "openai": _DEFAULT_DIALECT,
    "gemini": GeminiDialect(),
}


def register_llm_dialect(dialect: LLMProviderDialect) -> None:
    """Register or override a wire-format dialect strategy (OCP extension point)."""
    DIALECTS[dialect.style] = dialect


def get_dialect(style: str) -> LLMProviderDialect:
    """Return the wire-format dialect strategy for *style* (defaults to OpenAI)."""
    return DIALECTS.get(style, _DEFAULT_DIALECT)


def _build_request_for(
    route: NoteRoute,
    settings: Settings,
    system: str,
    user: str,
    *,
    schema: dict | None = None,
) -> tuple[str, dict[str, str], dict]:
    """Build one provider's POST request from its own route dialect."""
    return get_dialect(route.style).build_post_request(
        route, settings, system, user, schema=schema
    )


def _build_request(
    settings: Settings, system: str, user: str, *, route: NoteRoute | None = None
) -> tuple[str, dict[str, str], dict]:
    """Build the POST request for one route — by default the first eligible one."""
    if route is None:
        routes = note_routes(settings)
        if not routes:
            raise LLMAuthError(
                "هیچ سرویس تولید جزوه‌ای پیکربندی نشده است (NOTE_PROVIDERS / NOTE_API_KEY / "
                "GEMINI_API_KEY / AVALAI_API_KEY / OPENROUTER_API_KEY)."
            )
        route = routes[0]
    return _build_request_for(route, settings, system, user)


def _build_get_request_for(
    route: NoteRoute, target: str = "models"
) -> tuple[str, dict[str, str]]:
    """Build one provider's documented GET metadata request ``(url, headers)``."""
    return get_dialect(route.style).build_get_request(route, target)


def _parse_response(payload: dict, provider: str) -> str:
    """Normalize a provider response to plain text."""
    dialect = get_dialect(provider)
    if dialect.is_output_truncated(payload):
        raise LLMOutputBudgetError(
            "پاسخ سرویس تولید جزوه پیش از پایان به سقف توکن خروجی رسید "
            "(NOTE_MAX_OUTPUT_TOKENS را افزایش دهید)."
        )
    try:
        text = dialect.parse_response(payload)
    except (AttributeError, KeyError, IndexError, TypeError) as exc:
        logger.warning(
            "Note provider returned an unexpected response schema provider=%s error=%s",
            provider,
            type(exc).__name__,
        )
        raise LLMResponseError(
            "پاسخ سرویس تولید جزوه با قرارداد انتظار ما سازگار نبود."
        ) from exc
    text = text.strip()
    if not text:
        raise LLMResponseError("پاسخ سرویس تولید جزوه خالی بود.")
    return text


def _usage_summary(payload: dict, style: str) -> tuple[str, int | None, int | None, int | None]:
    """``(served_model, input_tokens, output_tokens, total_tokens)`` of a reply."""
    return get_dialect(style).extract_usage(payload)


def request_payload_output_tokens(payload: dict) -> int | None:
    """The output budget this *request* actually sends, after provider clamping.

    Read from the built payload rather than ``Settings`` so logs and operator
    probes report the effective value (including per-request overrides and any
    provider-specific cap), not merely what the environment declared.
    """
    for key in ("max_tokens", "max_completion_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    generation_config = payload.get("generationConfig")
    if isinstance(generation_config, dict):
        value = generation_config.get("maxOutputTokens")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _retry_delay_seconds(retry_after: float | None, attempt: int) -> float:
    """Return a finite, bounded delay for one transient LLM retry.

    The HTTP utility already parses recognised headers, but this final guard
    also protects direct test doubles and future provider adapters from a NaN,
    a negative value, or a very large retry hint. A missing/invalid hint uses
    capped exponential backoff.
    """
    fallback = min(2.0**attempt, MAX_LLM_RETRY_AFTER_SECONDS)
    if retry_after is None:
        return fallback
    try:
        candidate = float(retry_after)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(candidate):
        return fallback
    return min(MAX_LLM_RETRY_AFTER_SECONDS, max(0.0, candidate))


def _log_usage(
    route: NoteRoute,
    payload: dict,
    *,
    headers: Any = None,
    ttfb_ms: float = 0.0,
    elapsed_ms: float = 0.0,
    requested_output_tokens: int | None = None,
) -> None:
    """Log one completed request's token usage and safe correlation id.

    ``output_tokens`` is the provider's *actual* completion usage; the optional
    ``requested_output_tokens`` is the budget we sent. Keeping them separate is
    what makes a truncated answer diagnosable without guessing from ``.env``.
    AvalAI's ``avalai-request-id`` response header is logged as ``request_id``
    so the operator can ask its User API for that request's exact cost. Neither
    the completion content nor a credential is logged.
    """
    model, input_tokens, output_tokens, total_tokens = _usage_summary(payload, route.style)
    _ACTUAL_USAGE_TOKENS.set(total_tokens)
    request_id = _response_request_id(headers, payload=payload, api_key=route.api_key)

    def shown(value: int | None) -> str:
        return str(value) if value is not None else "-"

    logger.info(
        "note provider usage provider=%s request_id=%s model=%s requested_output_tokens=%s "
        "input_tokens=%s output_tokens=%s total_tokens=%s ttfb_ms=%.0f elapsed_ms=%.0f",
        route.slug,
        request_id or "-",
        # The served-model string is provider-controlled: collapse any
        # whitespace (including newlines) so it can never forge a log line.
        _sanitize_error_text(model or route.model, limit=80),
        shown(requested_output_tokens),
        shown(input_tokens),
        shown(output_tokens),
        shown(total_tokens),
        ttfb_ms,
        elapsed_ms,
    )


_OUTPUT_CAP_ERROR_RE = re.compile(
    r"(?:max_tokens|max_completion_tokens)[^0-9]{1,80}?"
    r"(?:<=|less than or equal to|at most|cannot exceed|must not exceed|maximum[^\d]{0,15}is)"
    r"\s*(\d{3,6})\b",
    re.IGNORECASE,
)


def _is_gemini_invalid_argument(error_text: str) -> bool:
    """Gemini's canonical HTTP 400 ``INVALID_ARGUMENT`` request rejection.

    Auth/quota/permission failures are classified as :class:`LLMAuthError` and
    never reach this path; 429/5xx are transient and context/budget failures
    have their own classes. What remains is a 400-level request rejection, and
    only Google's ``INVALID_ARGUMENT`` marker is considered a possible
    structured-output incompatibility.
    """
    lower = error_text.lower()
    return "http 400" in lower and (
        "invalid_argument" in lower or "invalid argument" in lower
    )


def _adjust_rejected_request_param(
    payload: dict,
    error_text: str,
    *,
    allow_json_object_fallback: bool = True,
    provider_slug: str = "",
) -> str | None:
    """Adjust a request field after an explicit 400/404/422 parameter rejection.

    ``provider_slug`` is used only for one narrow Gemini compatibility case: a
    structured request that gets a 400 ``INVALID_ARGUMENT`` drops *only*
    ``generationConfig.responseSchema`` and is retried once. The JSON mime type
    and every other field stay, so the answer still goes through the existing
    strict JSON parser and note validation.
    """
    lower = error_text.lower()
    rejected_markers = (
        "unsupported parameter",
        "unknown parameter",
        "unrecognized parameter",
        "unknown field",
        "unrecognized field",
        "unknown name",
        "cannot find field",
        "not supported",
        "not allowed",
        "only supports",
        "must be 1",
        "must equal 1",
        "invalid_request",
        "invalid json schema",
    )
    if "no endpoints found" in lower:
        resp_fmt = payload.get("response_format")
        if isinstance(resp_fmt, dict) and resp_fmt.get("type") == "json_schema":
            if allow_json_object_fallback:
                payload["response_format"] = {"type": "json_object"}
                return "response_format.json_schema_to_json_object"
            payload.pop("response_format", None)
            return "response_format"
        if "provider" in payload:
            payload.pop("provider", None)
            return "provider"
        if "reasoning_effort" in payload:
            payload.pop("reasoning_effort", None)
            return "reasoning_effort"
        if "response_format" in payload:
            payload.pop("response_format", None)
            return "response_format"

    if "response_format" in payload:
        resp_fmt = payload.get("response_format")
        is_schema_fmt = isinstance(resp_fmt, dict) and resp_fmt.get("type") == "json_schema"
        schema_specific_markers = (
            "json_schema",
            "structured_output",
            "structured output",
            "additionalproperties",
            "additional_properties",
            "minitems",
            "maxitems",
            "generated json does not match",
            "invalid json schema",
            "response_schema",
        )
        if (
            is_schema_fmt
            and allow_json_object_fallback
            and "json_object" not in lower
            and (
                any(marker in lower for marker in schema_specific_markers)
                or ("strict" in lower and "schema" in lower)
            )
        ):
            payload["response_format"] = {"type": "json_object"}
            return "response_format.json_schema_to_json_object"
        if any(
            marker in lower
            for marker in (
                "response_format",
                "json_object",
                "structured_output",
                "structured output",
                "json_schema",
                "json mode",
                "json_validate_failed",
                "failed to generate json",
                "generated json does not match",
            )
        ):
            payload.pop("response_format", None)
            return "response_format"

    if "reasoning_format" in payload and any(
        marker in lower for marker in ("reasoning_format", "reasoning format")
    ):
        payload.pop("reasoning_format", None)
        return "reasoning_format"

    if "reasoning_effort" in payload and any(
        marker in lower
        for marker in (
            "reasoning_effort",
            "reasoning effort",
            "thinking_level",
            "thinking_budget",
            "reasoning is mandatory",
            "reasoning is not supported",
        )
    ):
        payload.pop("reasoning_effort", None)
        return "reasoning_effort"

    if (
        "provider" in payload
        and "provider" in lower
        and any(marker in lower for marker in rejected_markers)
    ):
        payload.pop("provider", None)
        return "provider"

    generation_config = payload.get("generationConfig")
    if isinstance(generation_config, dict):
        # The 2026-11-07 Gemini test failed with HTTP 400 INVALID_ARGUMENT when
        # structured output was enabled and succeeded with it disabled. The
        # exact rejected field was never isolated from the failing payload, so
        # this is a deliberately narrow, single-shot compatibility retry: it is
        # Gemini-only, needs a 400 INVALID_ARGUMENT, needs the original request
        # to actually contain responseSchema, and removes only that field.
        if (
            provider_slug == "gemini"
            and "responseSchema" in generation_config
            and _is_gemini_invalid_argument(error_text)
        ):
            generation_config.pop("responseSchema", None)
            return "generationConfig.responseSchema_invalid_argument"
        if (
            "responseSchema" in generation_config
            and any(
                marker in lower
                for marker in ("responseschema", "response_schema", "schema")
            )
            and any(marker in lower for marker in rejected_markers)
        ):
            # A model (or a gateway in front of it) that does not implement
            # structured output drops the schema; the JSON mime type and the
            # prompt's own rules still apply, so the answer stays parseable.
            generation_config.pop("responseSchema", None)
            return "generationConfig.responseSchema"
        if (
            "responseMimeType" in generation_config
            and any(
                marker in lower
                for marker in ("responsemimetype", "response_mime_type", "application/json")
            )
            and any(marker in lower for marker in rejected_markers)
        ):
            generation_config.pop("responseMimeType", None)
            return "generationConfig.responseMimeType"
        if (
            "thinkingConfig" in generation_config
            and any(
                marker in lower
                for marker in (
                    "thinkingconfig",
                    "thinking_config",
                    "thinkingbudget",
                    "thinking_budget",
                )
            )
            and any(marker in lower for marker in rejected_markers)
        ):
            # A model that predates ``thinkingConfig`` rejects the field
            # instead of ignoring it; drop it and retry the same request once.
            generation_config.pop("thinkingConfig", None)
            return "generationConfig.thinkingConfig"
        if (
            "temperature" in generation_config
            and "temperature" in lower
            and any(marker in lower for marker in rejected_markers)
        ):
            generation_config.pop("temperature", None)
            return "generationConfig.temperature"
    if (
        "max_tokens" in payload
        and "max_tokens" in lower
        and "max_completion_tokens" in lower
        and any(
            marker in lower
            for marker in (
                "unsupported",
                "not supported",
                "unknown parameter",
                "unrecognized",
                "not allowed",
            )
        )
    ):
        payload["max_completion_tokens"] = payload.pop("max_tokens")
        return "max_tokens_to_max_completion_tokens"
    if (
        "max_completion_tokens" in payload
        and "max_completion_tokens" in lower
        and any(marker in lower for marker in rejected_markers)
        and "max_tokens" not in payload
    ):
        payload["max_tokens"] = payload.pop("max_completion_tokens")
        return "max_completion_tokens_to_max_tokens"
    for budget_key in ("max_tokens", "max_completion_tokens"):
        current_budget = payload.get(budget_key)
        if isinstance(current_budget, int) and not isinstance(current_budget, bool):
            cap_match = _OUTPUT_CAP_ERROR_RE.search(error_text)
            if cap_match is not None:
                cap_val = int(cap_match.group(1))
                if 0 < cap_val < current_budget:
                    payload[budget_key] = cap_val
                    return f"{budget_key}_clamped"
    if (
        "temperature" in payload
        and "temperature" in lower
        and any(marker in lower for marker in rejected_markers)
    ):
        payload.pop("temperature", None)
        return "temperature"
    return None


class _TTFBTimeoutError(LLMTransientError):
    """First streaming bytes did not arrive inside NOTE_TTFB_TIMEOUT_SECONDS."""


class _PrimaryEarlyFailure(Exception):
    def __init__(self, exc: LLMError):
        self.exc = exc


class _HedgedFailures(Exception):
    def __init__(self, first: LLMError, second: LLMError):
        self.first = first
        self.second = second


async def _hedge_two(first_factory, second_factory, hedge_after: float):
    """Race two route attempts and always cancel/join unfinished siblings."""
    first_task = asyncio.ensure_future(first_factory())
    tasks = {first_task}
    second_task = None
    try:
        done, _pending = await asyncio.wait({first_task}, timeout=hedge_after)
        if first_task in done:
            try:
                return first_task.result()
            except LLMError as exc:
                raise _PrimaryEarlyFailure(exc) from exc

        second_task = asyncio.ensure_future(second_factory())
        tasks.add(second_task)
        pending = {first_task, second_task}
        errors: dict[asyncio.Task, LLMError] = {}
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    # The task was cancelled from outside: treat it as a failure
                    # of this attempt rather than crashing the race.
                    errors[task] = LLMError("درخواست لغو شد.")
                    continue
                except LLMError as exc:
                    errors[task] = exc
                    continue
                # Returning from the try still runs the finally block below,
                # which cancels and joins the loser before releasing this call.
                return result
        raise _HedgedFailures(errors[first_task], errors[second_task])
    finally:
        unfinished = [task for task in tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)


def _parse_sse_line(line: bytes | str) -> dict | None:
    if isinstance(line, (bytes, bytearray)):
        raw = bytes(line).strip()
        if not raw or raw.startswith(b":"):
            return None
        if raw.startswith(b"data:"):
            raw = raw[5:].strip()
        if not raw or raw == b"[DONE]":
            return None
        try:
            parsed = _loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    text = str(line).strip()
    if not text or text.startswith(":"):
        return None
    if text.startswith("data:"):
        text = text[5:].strip()
    if not text or text == "[DONE]":
        return None
    try:
        parsed = _loads(text.encode("utf-8"))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _gemini_sse_url(url: str) -> str:
    """Convert a Gemini ``:generateContent`` URL into ``:streamGenerateContent?alt=sse``."""
    stream_url = url.replace(":generateContent", ":streamGenerateContent")
    if "alt=sse" in stream_url:
        return stream_url
    sep = "&" if "?" in stream_url else "?"
    return f"{stream_url}{sep}alt=sse"


async def _complete_streaming_on_route(
    route: NoteRoute,
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict,
    timeout_seconds: float,
    include_stream_options: bool | None = None,
) -> str:
    """Parse an SSE response incrementally under a hard body-size ceiling."""
    ttfb_timeout = float(getattr(settings, "note_ttfb_timeout", 0.0) or 0.0)
    profile = get_provider_profile(route.slug)
    is_gemini = route.style == "gemini"
    if is_gemini:
        stream_url = _gemini_sse_url(url)
        stream_payload = payload
    else:
        stream_url = url
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        if (
            not profile.supports_streaming_with_json_schema
            and isinstance(stream_payload.get("response_format"), dict)
            and stream_payload["response_format"].get("type") == "json_schema"
        ):
            stream_payload["response_format"] = {"type": "json_object"}
        use_stream_opts = (
            profile.supports_stream_options(route)
            if include_stream_options is None
            else include_stream_options
        )
        if use_stream_opts:
            stream_payload["stream_options"] = {"include_usage": True}

    requested_output_tokens = request_payload_output_tokens(stream_payload)
    started = time.perf_counter()
    ttfb_ms = 0.0
    text_parts: list[str] = []
    refusal_parts: list[str] = []
    finish_reason = ""
    native_finish_reason = ""
    usage: dict[str, Any] = {}
    saw_thought = False
    saw_reasoning = False
    saw_candidate = False
    prompt_feedback: dict[str, Any] | None = None
    model_version = ""
    served_model = ""
    raw_fallback_body: bytes | None = None
    non_sse_buffer = bytearray()
    saw_sse_prefix = False
    parsed_chunks = 0
    response_bytes = 0

    async with session.post(
        stream_url,
        headers=headers,
        json=stream_payload,
        timeout=aiohttp.ClientTimeout(
            total=timeout_seconds,
            sock_connect=LLM_CONNECT_TIMEOUT_SECONDS,
        ),
        allow_redirects=False,
    ) as response:
        if response.status != 200:
            raise await _error_from_response(route.label, response, route.api_key or None)
        resp_headers = getattr(response, "headers", None)

        def consume_line(line: bytes | str, *, count_bytes: bool = True) -> None:
            nonlocal parsed_chunks, response_bytes, finish_reason, native_finish_reason
            nonlocal usage, saw_thought, saw_reasoning, saw_candidate, prompt_feedback
            nonlocal model_version, served_model, saw_sse_prefix
            line_bytes_obj = (
                bytes(line)
                if isinstance(line, (bytes, bytearray))
                else str(line).encode("utf-8")
            )
            if count_bytes:
                response_bytes += len(line_bytes_obj)
                if response_bytes > MAX_LLM_RESPONSE_BYTES:
                    raise LLMResponseError(
                        "حجم پاسخ سرویس تولید جزوه بیش از حد مجاز بود."
                    )
            stripped_raw = line_bytes_obj.lstrip()
            if stripped_raw.startswith((b"data:", b"event:", b":")):
                saw_sse_prefix = True
            elif parsed_chunks == 0 and not saw_sse_prefix and len(non_sse_buffer) < 2 * 1024 * 1024:
                non_sse_buffer.extend(line_bytes_obj)
                non_sse_buffer.append(ord("\n"))

            chunk = _parse_sse_line(line)
            if chunk is None:
                return
            parsed_chunks += 1
            error_obj = chunk.get("error")
            if _has_error_content(error_obj):
                status = _infer_in_body_error_status(error_obj)
                raise _error_from_payload(
                    route.label, status, {"error": error_obj}, resp_headers,
                    route.api_key or None,
                )
            if is_gemini:
                if isinstance(chunk.get("usageMetadata"), dict):
                    usage = chunk["usageMetadata"]
                if isinstance(chunk.get("modelVersion"), str):
                    model_version = chunk["modelVersion"]
                if isinstance(chunk.get("promptFeedback"), dict):
                    prompt_feedback = chunk["promptFeedback"]
                candidates = chunk.get("candidates")
                if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
                    saw_candidate = True
                    first_cand = candidates[0]
                    if first_cand.get("finishReason"):
                        finish_reason = str(first_cand["finishReason"])
                    content_obj = first_cand.get("content")
                    parts = content_obj.get("parts") if isinstance(content_obj, dict) else None
                    if isinstance(parts, list):
                        for part in parts:
                            if not isinstance(part, dict):
                                continue
                            if part.get("thought") is True:
                                saw_thought = True
                                continue
                            if "text" in part and part.get("text") is not None:
                                text_parts.append(str(part.get("text", "")))
                return

            if isinstance(chunk.get("model"), str) and chunk["model"]:
                served_model = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
                if _has_error_content(choice.get("error")):
                    status = _infer_in_body_error_status(choice["error"])
                    raise _error_from_payload(
                        route.label, status, {"error": choice["error"]},
                        resp_headers, route.api_key or None,
                    )
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                if choice.get("native_finish_reason"):
                    native_finish_reason = str(choice["native_finish_reason"])
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    if delta.get("content"):
                        text_parts.append(str(delta["content"]))
                    for r_key in ("reasoning", "reasoning_content"):
                        r_val = delta.get(r_key)
                        if isinstance(r_val, str) and r_val.strip():
                            saw_reasoning = True
                    refusal_val = delta.get("refusal")
                    if isinstance(refusal_val, str) and refusal_val:
                        refusal_parts.append(refusal_val)
                elif isinstance(choice.get("message"), dict):
                    msg_dict = choice["message"]
                    if msg_dict.get("content"):
                        text_parts.append(str(msg_dict["content"]))
                    for r_key in ("reasoning", "reasoning_content"):
                        r_val = msg_dict.get(r_key)
                        if isinstance(r_val, str) and r_val.strip():
                            saw_reasoning = True
                    refusal_val = msg_dict.get("refusal")
                    if isinstance(refusal_val, str) and refusal_val:
                        refusal_parts.append(refusal_val)

        content = getattr(response, "content", None)
        iter_chunked = getattr(content, "iter_chunked", None) if content is not None else None
        if callable(iter_chunked):
            chunk_iter = iter_chunked(16384).__aiter__()
            try:
                if ttfb_timeout > 0:
                    first_raw = await asyncio.wait_for(
                        chunk_iter.__anext__(), timeout=ttfb_timeout
                    )
                else:
                    first_raw = await chunk_iter.__anext__()
            except asyncio.TimeoutError as exc:
                raise _TTFBTimeoutError(
                    f"{route.label} در مهلت نخستین بایت پاسخ نداد."
                ) from exc
            except StopAsyncIteration:
                first_raw = None
            ttfb_ms = (time.perf_counter() - started) * 1000.0
            line_buf = bytearray()

            def _feed_chunk(raw_bytes: bytes) -> None:
                nonlocal response_bytes
                response_bytes += len(raw_bytes)
                if response_bytes > MAX_LLM_RESPONSE_BYTES:
                    raise LLMResponseError(
                        "حجم پاسخ سرویس تولید جزوه بیش از حد مجاز بود."
                    )
                line_buf.extend(raw_bytes)
                while True:
                    nl = line_buf.find(b"\n")
                    if nl < 0:
                        break
                    one_line = bytes(line_buf[:nl])
                    del line_buf[: nl + 1]
                    consume_line(one_line, count_bytes=False)

            if first_raw is not None:
                _feed_chunk(first_raw)
                async for raw_chunk in chunk_iter:
                    _feed_chunk(raw_chunk)
                if line_buf:
                    consume_line(bytes(line_buf), count_bytes=False)
        elif content is not None and hasattr(content, "__aiter__"):
            iterator = content.__aiter__()
            try:
                if ttfb_timeout > 0:
                    first_line = await asyncio.wait_for(
                        iterator.__anext__(), timeout=ttfb_timeout
                    )
                else:
                    first_line = await iterator.__anext__()
            except asyncio.TimeoutError as exc:
                raise _TTFBTimeoutError(
                    f"{route.label} در مهلت نخستین بایت پاسخ نداد."
                ) from exc
            except StopAsyncIteration:
                first_line = None
            ttfb_ms = (time.perf_counter() - started) * 1000.0
            if first_line is not None:
                consume_line(first_line)
                async for line in iterator:
                    consume_line(line)
        else:  # Compatibility for doubles exposing only read().
            raw_fallback_body = await _read_bounded_body(
                response, MAX_LLM_RESPONSE_BYTES
            )
            response_bytes = len(raw_fallback_body)
            ttfb_ms = (time.perf_counter() - started) * 1000.0
            for line in raw_fallback_body.splitlines():
                consume_line(line, count_bytes=False)

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    fallback_bytes = raw_fallback_body or (bytes(non_sse_buffer) if non_sse_buffer else None)
    if parsed_chunks == 0 and fallback_bytes:
        try:
            fallback_dict = _loads(fallback_bytes)
            if isinstance(fallback_dict, dict):
                _log_usage(
                    route, fallback_dict, headers=resp_headers,
                    ttfb_ms=ttfb_ms, elapsed_ms=elapsed_ms,
                    requested_output_tokens=requested_output_tokens,
                )
                _raise_if_in_body_error(route.label, fallback_dict, resp_headers, route.api_key or None)
                return _parse_response(fallback_dict, route.style)
        except (ValueError, TypeError):
            pass

    if is_gemini:
        assembled_parts: list[dict[str, object]] = []
        if saw_thought:
            assembled_parts.append({"thought": True, "text": ""})
        if text_parts:
            assembled_parts.append({"text": "".join(text_parts)})
        assembled: dict[str, Any] = {"usageMetadata": usage}
        if saw_candidate or not prompt_feedback:
            assembled["candidates"] = [{
                "content": {"parts": assembled_parts},
                "finishReason": finish_reason or "STOP",
            }]
        if prompt_feedback:
            assembled["promptFeedback"] = prompt_feedback
        if model_version:
            assembled["modelVersion"] = model_version
        if served_model:
            assembled["model"] = served_model
    else:
        assembled_msg: dict[str, Any] = {"content": "".join(text_parts)}
        if saw_reasoning:
            assembled_msg["reasoning"] = "[present]"
        if refusal_parts:
            assembled_msg["refusal"] = "".join(refusal_parts)
        assembled_choice: dict[str, Any] = {
            "message": assembled_msg,
            "finish_reason": finish_reason or "stop",
        }
        if native_finish_reason:
            assembled_choice["native_finish_reason"] = native_finish_reason
        assembled = {
            "choices": [assembled_choice],
            "usage": usage,
        }
        if served_model:
            assembled["model"] = served_model
    _log_usage(
        route, assembled, headers=resp_headers,
        ttfb_ms=ttfb_ms, elapsed_ms=elapsed_ms,
        requested_output_tokens=requested_output_tokens,
    )
    _raise_if_in_body_error(route.label, assembled, resp_headers, route.api_key or None)
    return _parse_response(assembled, route.style)


async def _complete_on_route(
    route: NoteRoute,
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
    retry_on_timeout: bool = True,
    schema: dict | None = None,
) -> str:
    """One completion from one provider, with its own bounded retry budget.

    ``retry_on_timeout`` decides what a deadline miss means for the *same*
    provider: with it (the default) the request is re-sent within the bounded
    budget, without it the provider is left behind immediately. The chain uses
    the second form for every provider that still has a successor, because a
    provider that needed the whole deadline once will need it again — the
    identical request is simply re-waiting — while the next provider may
    answer in seconds.
    """
    url, headers, payload = _build_request_for(route, settings, system, user, schema=schema)
    profile = get_provider_profile(route.slug)
    allow_streaming = True
    include_stream_options = profile.supports_stream_options(route)
    timeout_seconds = float(settings.note_request_timeout or 0) or (
        LLM_REQUEST_TIMEOUT_SECONDS
    )
    attempt = 0
    request_number = 0
    compatibility_adjustments = 0
    while True:
        request_number += 1
        # Read the budget from the payload: this is the value that really goes
        # on the wire after provider-specific clamping and per-call overrides.
        requested_output_tokens = request_payload_output_tokens(payload)
        logger.info(
            "LLM request sending provider=%s model=%s style=%s attempt=%d system_chars=%d "
            "user_chars=%d requested_output_tokens=%s fields=%s",
            route.slug,
            route.model,
            route.style,
            request_number,
            len(system),
            len(user),
            requested_output_tokens if requested_output_tokens is not None else "-",
            ",".join(sorted(payload)),
        )
        started = time.perf_counter()
        ttfb_ms = 0.0
        elapsed_ms = 0.0
        streaming_active = (
            allow_streaming
            and float(getattr(settings, "note_ttfb_timeout", 0.0) or 0.0) > 0
            and route.style in ("openai", "gemini")
        )
        try:
            if streaming_active:
                return await _complete_streaming_on_route(
                    route, session, settings, url=url, headers=headers,
                    payload=payload, timeout_seconds=timeout_seconds,
                    include_stream_options=include_stream_options,
                )
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(
                    total=timeout_seconds,
                    sock_connect=LLM_CONNECT_TIMEOUT_SECONDS,
                ),
                allow_redirects=False,
            ) as response:
                ttfb_ms = (time.perf_counter() - started) * 1000.0
                if response.status != 200:
                    raise await _error_from_response(
                        route.label, response, route.api_key or None
                    )
                resp_headers = getattr(response, "headers", None)
                try:
                    result = await _read_json_body(response)
                except (ValueError, TypeError) as exc:
                    raise LLMResponseError(
                        "پاسخ سرویس تولید جزوه JSON معتبری نبود."
                    ) from exc
                elapsed_ms = (time.perf_counter() - started) * 1000.0
            if not isinstance(result, dict):
                raise LLMResponseError("پاسخ سرویس تولید جزوه یک شیء JSON نبود.")
            # Usage is logged even for an answer the parser then rejects or
            # reports as truncated: those are exactly the requests whose real
            # token counts calibrate the chunk budget.
            _log_usage(
                route, result, headers=resp_headers,
                ttfb_ms=ttfb_ms, elapsed_ms=elapsed_ms,
                requested_output_tokens=requested_output_tokens,
            )
            _raise_if_in_body_error(
                route.label, result, resp_headers, route.api_key or None
            )
            return _parse_response(result, route.style)
        except LLMRequestError as exc:
            if not isinstance(exc, (LLMContextOverflowError, LLMOutputBudgetError)):
                err_lower = str(exc).lower()
                if streaming_active and include_stream_options and any(
                    marker in err_lower for marker in ("stream_options", "include_usage")
                ):
                    include_stream_options = False
                    logger.info(
                        "Adjusting rejected stream_options and retrying provider=%s",
                        route.slug,
                    )
                    continue
                if streaming_active and any(
                    marker in err_lower
                    for marker in (
                        "streaming is not supported",
                        "stream is not supported",
                        "streaming not supported",
                        "cannot stream",
                    )
                ):
                    allow_streaming = False
                    logger.info(
                        "Disabling unsupported streaming and retrying provider=%s",
                        route.slug,
                    )
                    continue
                adjustment = None
                if compatibility_adjustments < MAX_COMPATIBILITY_ADJUSTMENTS:
                    adjustment = _adjust_rejected_request_param(
                        payload,
                        str(exc),
                        allow_json_object_fallback=profile.supports_response_format(route),
                        provider_slug=route.slug,
                    )
                if adjustment is not None:
                    compatibility_adjustments += 1
                    logger.info(
                        "Adjusting rejected request parameter and retrying "
                        "provider=%s adjustment=%s (%d/%d)",
                        route.slug,
                        adjustment,
                        compatibility_adjustments,
                        MAX_COMPATIBILITY_ADJUSTMENTS,
                    )
                    continue
                if compatibility_adjustments >= MAX_COMPATIBILITY_ADJUSTMENTS:
                    logger.warning(
                        "Compatibility adjustment budget exhausted provider=%s; "
                        "not retrying this request again",
                        route.slug,
                    )
            raise
        except LLMAuthError as exc:
            event = "quota_exhausted" if "HTTP 402" in str(exc) else "auth_error"
            METRICS.record_provider_event("llm", route.slug, event)
            _set_route_cooldown(session, route.slug, LLM_AUTH_COOLDOWN_SECONDS)
            raise
        except (LLMTransientError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            timed_out = isinstance(exc, (asyncio.TimeoutError, _TTFBTimeoutError))
            is_rate_limit = isinstance(exc, LLMTransientError) and exc.rate_limited
            retry_after = (
                exc.retry_after_seconds if isinstance(exc, LLMTransientError) else None
            )
            if timed_out:
                METRICS.record_provider_event("llm", route.slug, "timeout")
            elif is_rate_limit:
                METRICS.record_provider_event("llm", route.slug, "rate_limit")
            elif isinstance(exc, aiohttp.ClientError):
                METRICS.record_provider_event("llm", route.slug, "connection_error")
            else:
                METRICS.record_provider_event("llm", route.slug, "server_error")

            if is_rate_limit and not retry_on_timeout and retry_after != 0.0:
                cooldown = min(
                    LLM_RATE_LIMIT_COOLDOWN_SECONDS,
                    max(1.0, retry_after if retry_after is not None else 5.0),
                )
                _set_route_cooldown(session, route.slug, cooldown)
                logger.warning(
                    "LLM provider rate-limited provider=%s cooldown_seconds=%.1f; "
                    "failing over immediately without sleeping",
                    route.slug,
                    cooldown,
                )
                raise

            if timed_out and not retry_on_timeout:
                logger.warning(
                    "LLM request timed out provider=%s; trying the next provider "
                    "instead of repeating the request (timeout_seconds=%.0f)",
                    route.slug, timeout_seconds,
                )
                raise LLMTransientError(
                    f"سرویس تولید جزوه در مهلت {timeout_seconds:.0f} ثانیه‌ای پاسخ نداد "
                    "(NOTE_REQUEST_TIMEOUT_SECONDS را افزایش دهید یا "
                    "NOTE_MAX_OUTPUT_TOKENS را کم کنید)."
                ) from exc
            if attempt >= MAX_TRANSIENT_RETRIES:
                if is_rate_limit:
                    _set_route_cooldown(
                        session,
                        route.slug,
                        min(LLM_RATE_LIMIT_COOLDOWN_SECONDS, max(1.0, retry_after or 5.0)),
                    )
                if isinstance(exc, LLMTransientError):
                    raise
                if timed_out:
                    raise LLMTransientError(
                        f"سرویس تولید جزوه در مهلت {timeout_seconds:.0f} ثانیه‌ای پاسخ نداد "
                        "(NOTE_REQUEST_TIMEOUT_SECONDS را افزایش دهید یا "
                        "NOTE_MAX_OUTPUT_TOKENS را کم کنید)."
                    ) from exc
                raise LLMTransientError(
                    "سرویس تولید جزوه پس از چند تلاش در دسترس نبود."
                ) from exc
            attempt += 1
            METRICS.record_provider_event("llm", route.slug, "retry")
            delay = _retry_delay_seconds(retry_after, attempt)
            logger.warning(
                "LLM transient failure provider=%s (attempt %d/%d; %s); retrying in %.1fs",
                route.slug, attempt, MAX_TRANSIENT_RETRIES + 1,
                type(exc).__name__, delay,
            )
            await asyncio.sleep(delay)


async def get_json(
    session: aiohttp.ClientSession,
    route: NoteRoute,
    target: str = "models",
    *,
    timeout_seconds: float = LLM_REQUEST_TIMEOUT_SECONDS,
) -> dict:
    """Execute one provider GET metadata request with bounded transient retries.

    Uses :func:`_build_get_request_for` for provider-specific URL/auth construction
    and :func:`_error_from_response` / :func:`_raise_if_in_body_error` for error
    classification and key redaction.
    """
    url, headers = _build_get_request_for(route, target)
    attempt = 0
    while True:
        started = time.perf_counter()
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=timeout_seconds,
                    sock_connect=LLM_CONNECT_TIMEOUT_SECONDS,
                ),
                allow_redirects=False,
            ) as response:
                ttfb_ms = (time.perf_counter() - started) * 1000.0
                if response.status != 200:
                    raise await _error_from_response(
                        route.label, response, route.api_key or None
                    )
                resp_headers = getattr(response, "headers", None)
                try:
                    result = await _read_json_body(response)
                except (ValueError, TypeError) as exc:
                    raise LLMResponseError(
                        "پاسخ سرویس تولید جزوه JSON معتبری نبود."
                    ) from exc
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                logger.debug(
                    "note provider GET timing provider=%s ttfb_ms=%.0f elapsed_ms=%.0f",
                    route.slug, ttfb_ms, elapsed_ms,
                )
            if not isinstance(result, dict):
                raise LLMResponseError("پاسخ سرویس تولید جزوه یک شیء JSON نبود.")
            _raise_if_in_body_error(
                route.label, result, resp_headers, route.api_key or None
            )
            return result
        except (LLMTransientError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_TRANSIENT_RETRIES:
                if isinstance(exc, LLMTransientError):
                    raise
                raise LLMTransientError(
                    "سرویس تولید جزوه پس از چند تلاش در دسترس نبود."
                ) from exc
            attempt += 1
            retry_after = (
                exc.retry_after_seconds if isinstance(exc, LLMTransientError) else None
            )
            delay = _retry_delay_seconds(retry_after, attempt)
            logger.warning(
                "LLM GET transient failure provider=%s target=%s (attempt %d/%d; %s); retrying in %.1fs",
                route.slug, target, attempt, MAX_TRANSIENT_RETRIES + 1,
                type(exc).__name__, delay,
            )
            await asyncio.sleep(delay)


async def _run_with_lease(
    route: NoteRoute,
    lease: Any,
    fn: Callable[[], Awaitable[_T]],
) -> _T:
    METRICS.record_provider_acquire("llm", route.slug)
    ok = False
    cancelled = False
    try:
        result = await fn()
        ok = True
        return result
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        held = lease.release()
        METRICS.record_provider_release(
            "llm", route.slug, elapsed_seconds=held, ok=ok, cancelled=cancelled
        )


def _provider_budget(settings: Settings, route: NoteRoute, route_index: int = 0) -> ProviderBudget:
    slug = route.slug
    provider_concurrency = max(
        1, int(getattr(settings, f"{slug}_max_in_flight", route_max_in_flight(route_index, settings)) or 1)
    )
    return ProviderBudget(
        rpm=max(0, int(getattr(settings, f"{slug}_rpm_limit", 0) or 0)),
        tpm=max(0, int(getattr(settings, f"{slug}_tpm_limit", 0) or 0)),
        rpd=max(0, int(getattr(settings, f"{slug}_rpd_limit", 0) or 0)),
        max_in_flight=min(provider_concurrency, route_max_in_flight(route_index, settings)),
    )


async def _dispatch_across_routes(
    routes: tuple[NoteRoute, ...],
    session: object,
    settings: Settings,
    call_route: Callable[[NoteRoute, bool], Awaitable[_T]],
    *,
    hedge_route: Callable[[NoteRoute, bool], Awaitable[_T]] | None = None,
    pool_owner: object | None = None,
    wait_for_capacity: bool = True,
    reservation_tokens: int = 0,
    task: str = "",
    estimated_input_tokens: int = 0,
    reserved_output_tokens: int = 0,
) -> _T:
    """Execute *call_route* across *routes* with non-blocking capacity reservation."""
    pool = pool_for("llm", pool_owner if pool_owner is not None else settings)
    failures: list[LLMError] = []
    descriptions: list[str] = []
    indexed_routes: list[tuple[int, NoteRoute]] = list(enumerate(routes))
    # Duplicate hedged completions spend scarce provider quota; disabled for the
    # approved routes even if a stale deployment sets the legacy knob nonzero.
    hedge_after = 0.0

    if (
        hedge_after > 0
        and len(indexed_routes) >= 2
        and hedge_route is not None
        and _route_cooldown_remaining(session, routes[0].slug) <= 0
    ):
        primary_lease = pool.try_acquire(
            routes[0].slug, route_max_in_flight(0, settings)
        )
        if primary_lease is not None:
            def _make_primary() -> Awaitable[_T]:
                return _run_with_lease(
                    routes[0],
                    primary_lease,
                    lambda: hedge_route(routes[0], len(routes) == 1),
                )

            async def _run_backup() -> _T:
                backup_limit = route_max_in_flight(1, settings)
                acquired_b = await pool.acquire_any(
                    [(routes[1].slug, backup_limit)],
                    timeout=getattr(settings, "llm_slot_wait_timeout", 30.0) if wait_for_capacity else 0.0,
                )
                if acquired_b is None:
                    if not wait_for_capacity:
                        METRICS.record_learning_capacity_denial()
                    raise LLMTransientError(
                        "ظرفیت هم‌زمان سرویس پشتیبان تولید جزوه تکمیل است."
                    )
                _, backup_lease, _ = acquired_b
                METRICS.record_failover("llm", routes[0].slug, routes[1].slug)
                return await _run_with_lease(
                    routes[1],
                    backup_lease,
                    lambda: hedge_route(routes[1], len(routes) == 2),
                )

            try:
                return await _hedge_two(_make_primary, _run_backup, hedge_after)
            except _PrimaryEarlyFailure as early:
                failures.append(early.exc)
                descriptions.append(f"{routes[0].label}: {type(early.exc).__name__}")
                logger.warning(
                    "note provider failed; trying the next one provider=%s error=%s",
                    routes[0].slug, type(early.exc).__name__,
                )
                indexed_routes = indexed_routes[1:]
            except _HedgedFailures as both:
                for route, exc in ((routes[0], both.first), (routes[1], both.second)):
                    failures.append(exc)
                    descriptions.append(f"{route.label}: {type(exc).__name__}")
                    logger.warning(
                        "note provider failed; trying the next one provider=%s error=%s",
                        route.slug, type(exc).__name__,
                    )
                indexed_routes = indexed_routes[2:]

    previous_failed_slug: str | None = routes[0].slug if failures else None
    wait_timeout = float(getattr(settings, "llm_slot_wait_timeout", 30.0)) if wait_for_capacity else 0.0

    while indexed_routes:
        # Uncooled routes lead; cooled routes stay as last-resort fallbacks so
        # single-provider deployments still get their bounded attempt.
        ordered = sorted(
            indexed_routes,
            key=lambda item: (
                1 if _route_cooldown_remaining(session, item[1].slug) > 0 else 0,
                item[0],
            ),
        )
        candidates = [
            (route.slug, route_max_in_flight(orig_idx, settings))
            for orig_idx, route in ordered
        ]
        try:
            acquired = await pool.acquire_any(
                candidates,
                timeout=wait_timeout,
                max_waiters=max(16, sum(lim for _, lim in candidates) * 8),
            )
        except CapacityOverloadError as exc:
            raise LLMTransientError(str(exc)) from exc

        if acquired is None:
            if not wait_for_capacity:
                METRICS.record_learning_capacity_denial()
            raise LLMTransientError(
                "ظرفیت هم‌زمان سرویس تولید جزوه تکمیل است؛ لطفاً چند لحظه بعد دوباره تلاش کنید."
            )

        chosen_slug, lease, _waited = acquired
        picked_pos = next(
            pos for pos, (_idx, r) in enumerate(ordered) if r.slug == chosen_slug
        )
        orig_idx, route = ordered[picked_pos]
        indexed_routes = [
            item for item in indexed_routes if item[1].slug != chosen_slug
        ]
        if picked_pos > 0:
            for _b_idx, bypassed_route in ordered[:picked_pos]:
                METRICS.record_provider_event("llm", bypassed_route.slug, "overflow_skip")
                METRICS.record_failover("llm", bypassed_route.slug, chosen_slug)
                logger.info(
                    "LLM route %s saturated (%d/%d in flight); overflowing "
                    "immediately without wait to %s",
                    bypassed_route.slug,
                    pool.in_flight(bypassed_route.slug),
                    route_max_in_flight(_b_idx, settings),
                    chosen_slug,
                )
        elif previous_failed_slug is not None:
            METRICS.record_failover("llm", previous_failed_slug, chosen_slug)

        is_last_route = len(indexed_routes) == 0
        try:
            reservation = await asyncio.to_thread(
                reserve_quota,
                str(settings.db_path),
                route.slug,
                _provider_budget(settings, route, orig_idx),
                max(1, int(reservation_tokens)),
                2,  # reserve one bounded retry as well as the initial dispatch
            )
        except QuotaUnavailable as exc:
            lease.release()
            quota_error = LLMTransientError(
                "پیکربندی سهمیهٔ سرویس تکمیل است؛ مسیر پشتیبان بررسی می‌شود."
            )
            failures.append(quota_error)
            descriptions.append(f"{route.label}: quota unavailable")
            METRICS.record_provider_event("llm", route.slug, "quota_exhausted")
            logger.info("LLM quota denied provider=%s category=%s", route.slug, str(exc).split()[1] if len(str(exc).split()) > 1 else "budget")
            previous_failed_slug = route.slug
            continue
        except Exception as exc:
            lease.release()
            raise LLMTransientError("امکان رزرو امن سهمیهٔ LLM وجود نداشت.") from exc

        async def _reserved_call(r=route, last=is_last_route, hold=reservation):
            succeeded = False
            failure_category = ""
            circuit_failure: bool | None = None
            started = time.perf_counter()
            usage_token = _ACTUAL_USAGE_TOKENS.set(None)
            try:
                result = await call_route(r, last)
                succeeded = True
                return result
            except BaseException as exc:
                failure_category = type(exc).__name__
                circuit_failure = isinstance(exc, (LLMTransientError, LLMAuthError))
                raise
            finally:
                actual_tokens = _ACTUAL_USAGE_TOKENS.get()
                _ACTUAL_USAGE_TOKENS.reset(usage_token)
                await asyncio.to_thread(
                    finish_quota, hold, actual_tokens=actual_tokens, succeeded=succeeded,
                    circuit_failure=circuit_failure,
                )
                METRICS.record_llm_operation(
                    task=task,
                    provider=r.slug,
                    model=r.model,
                    success=succeeded,
                    failure_category=failure_category,
                    estimated_input_tokens=estimated_input_tokens,
                    reserved_output_tokens=reserved_output_tokens,
                    actual_total_tokens=actual_tokens,
                    duration_seconds=time.perf_counter() - started,
                )

        try:
            result = await _run_with_lease(route, lease, _reserved_call)
        except LLMError as exc:
            previous_failed_slug = route.slug
            failures.append(exc)
            descriptions.append(f"{route.label}: {type(exc).__name__}")
            logger.warning(
                "note provider failed; trying the next one provider=%s error=%s",
                route.slug, type(exc).__name__,
            )
            continue
        if failures:
            logger.info(
                "note generation succeeded after %d provider failure(s) provider=%s",
                len(failures), route.slug,
            )
        return result

    if len(descriptions) > 1:
        logger.error("every configured note provider failed: %s", " | ".join(descriptions))
    raise type(failures[0])("؛ ".join(descriptions))


async def complete(
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
    task: str = "",
    data_sensitivity: str = "UNKNOWN",
) -> str:
    """Text completion through the same allowlist, task policy and quota ledger."""
    configured_routes = note_routes(settings)
    routes = _task_ordered_routes(
        _sensitivity_eligible_routes(configured_routes, settings, data_sensitivity), task
    )
    if not routes:
        if configured_routes and (data_sensitivity or "UNKNOWN").strip().upper() in {
            "INTERNAL", "SENSITIVE", "PHI_OR_PII",
        }:
            raise LLMAuthError(
                "هیچ ارائه‌دهندهٔ مجازی برای سطح حساسیت این داده تأیید نشده است."
            )
        reason = (
            "هیچ ارائه‌دهندهٔ پیکربندی‌شده‌ای برای سطح حساسیت این داده مجاز نیست."
            if configured_routes else
            "هیچ ارائه‌دهندهٔ مجاز و دارای کلید پیکربندی نشده است."
        )
        raise LLMAuthError(reason)

    async def _call(route: NoteRoute, is_last: bool) -> str:
        return await _complete_on_route(
            route,
            session,
            settings,
            system=system,
            user=user,
            retry_on_timeout=is_last,
        )

    estimated_input = estimate_tokens(
        system + "\\n" + user,
        chars_per_token=getattr(settings, "llm_token_estimation_chars_per_token", 2.0),
    )
    return await _dispatch_across_routes(
        routes, session, settings, _call, hedge_route=_call,
        reservation_tokens=estimated_input + max(1, int(settings.note_max_output_tokens)),
        task=task,
        estimated_input_tokens=estimated_input,
        reserved_output_tokens=max(1, int(settings.note_max_output_tokens)),
    )


_THINK_TAG_RE = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>",
    re.IGNORECASE | re.DOTALL,
)
_ORPHAN_THINK_END_RE = re.compile(
    r"^.*?</think(?:ing)?>\s*",
    re.IGNORECASE | re.DOTALL,
)
_JSON_FENCE_RE = re.compile(
    r"```(?:json|JSON)?\s*(.*?)```",
    re.DOTALL,
)


def _balanced_object_end(text: str, start: int) -> int | None:
    """Return the index past the matching ``}`` for ``text[start] == '{'``."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def extract_json_object(text: str) -> str:
    """Extract the outermost valid JSON object from a model answer.

    Strips inline ``<think>...</think>`` reasoning blocks, prioritizes fenced
    JSON blocks, and skips non-JSON ``{...}`` prose fragments (e.g. set notation
    before the real payload) in favor of the first balanced JSON ``dict``.
    """
    cleaned = _THINK_TAG_RE.sub("", text or "").strip()
    if "</think" in cleaned.lower():
        cleaned = _ORPHAN_THINK_END_RE.sub("", cleaned).strip()

    regions: list[str] = [
        m.group(1).strip() for m in _JSON_FENCE_RE.finditer(cleaned) if m.group(1).strip()
    ]
    if cleaned.startswith("```"):
        unfenced = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        unfenced = re.sub(r"\s*```$", "", unfenced).strip()
        regions.append(unfenced)
    regions.append(cleaned)

    first_balanced: str | None = None
    saw_opening_brace = False
    for region in regions:
        search_from = 0
        while True:
            brace_idx = region.find("{", search_from)
            if brace_idx < 0:
                break
            saw_opening_brace = True
            end_idx = _balanced_object_end(region, brace_idx)
            if end_idx is None:
                search_from = brace_idx + 1
                continue
            candidate = region[brace_idx:end_idx]
            if first_balanced is None:
                first_balanced = candidate
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return candidate
            except ValueError:
                pass
            search_from = brace_idx + 1

    if first_balanced is not None:
        return first_balanced
    if saw_opening_brace:
        raise LLMResponseError("شیء JSON در پاسخ مدل کامل نبود.")
    raise LLMResponseError("پاسخ مدل شامل هیچ شیء JSON نبود.")


def _parse_json_object(text: str) -> dict:
    """Parse strict JSON and enforce the object shape used by note prompts."""
    if _orjson is not None and isinstance(text, str):
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                fast_payload = _orjson.loads(stripped)
                if isinstance(fast_payload, dict):
                    return fast_payload
            except ValueError:
                pass

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        payload = json.loads(
            extract_json_object(text),
            parse_constant=reject_constant,
        )
    except (LLMResponseError, ValueError, TypeError) as exc:
        raise LLMResponseError("پاسخ مدل شامل JSON معتبر نبود.") from exc
    if not isinstance(payload, dict):
        raise LLMResponseError("پاسخ مدل باید یک شیء JSON باشد.")
    return payload


#: Prepended to the retry request when a model answered with unparsable text.
JSON_ONLY_REMINDER = (
    "پاسخ قبلی قابل تجزیه نبود. فقط و فقط یک شیء JSON معتبر و کامل "
    "بدون هیچ متن یا توضیح اضافه برگردانید.\n\n"
)


async def _complete_json_on_route(
    route: NoteRoute,
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
    retry_on_timeout: bool,
    schema: dict | None = None,
) -> dict:
    """One provider's two-attempt JSON turn (used by the hedged path)."""
    last_exc: LLMError | None = None
    # Prefer failover over a second near-identical full-prompt request. The
    # schema remains on the request; another provider gets the recovery attempt.
    for attempt, prompt in enumerate((user,)):
        try:
            raw = await _complete_on_route(
                route, session, settings, system=system, user=prompt,
                retry_on_timeout=retry_on_timeout, schema=schema,
            )
            return _parse_json_object(raw)
        except LLMRequestError:
            raise
        except LLMError as exc:
            last_exc = exc
            if not isinstance(exc, LLMResponseError) or attempt:
                raise
    assert last_exc is not None
    raise last_exc


TASK_PROVIDER_ORDER = {
    "BOOKLET_GENERATION": ("avalai", "gemini", "openrouter"),
    "BOOKLET_REPAIR": ("avalai", "gemini", "openrouter"),
    "BOOKLET_COMPILE": ("avalai", "gemini", "openrouter"),
    "KEY_POINTS": ("gemini", "avalai", "openrouter"),
    "COMPACT_NOTES": ("gemini", "avalai", "openrouter"),
    "QUIZ_GENERATION": ("gemini", "avalai", "openrouter"),
    "QUIZ_20": ("avalai", "gemini", "openrouter"),
    "QUIZ_DIFFICULT": ("avalai", "gemini", "openrouter"),
    "FLASHCARD_GENERATION": ("gemini", "avalai", "openrouter"),
    "OFFICE_TABLE_GENERATION": ("gemini", "avalai", "openrouter"),
    "OFFICE_SLIDE_PLANNING": ("gemini", "avalai", "openrouter"),
    "OFFICE_DOCUMENT_GENERATION": ("gemini", "avalai", "openrouter"),
}


def _sensitivity_eligible_routes(
    routes: tuple[NoteRoute, ...], settings: Settings, sensitivity: str
) -> tuple[NoteRoute, ...]:
    """Apply a fail-closed provider policy for explicitly sensitive material.

    Unclassified data is never sent to OpenRouter Free. Gemini/AvalAI remain
    available for ordinary educational jobs; sensitive classes require explicit
    operator approval for each provider.
    """
    label = (sensitivity or "UNKNOWN").strip().upper()
    if label not in {"STANDARD_EDUCATIONAL", "INTERNAL", "SENSITIVE", "PHI_OR_PII"}:
        label = "UNKNOWN"
    eligible: list[NoteRoute] = []
    for route in routes:
        if route.slug not in APPROVED_LLM_PROVIDERS:
            continue
        if label == "STANDARD_EDUCATIONAL":
            eligible.append(route)
        elif label == "UNKNOWN":
            if route.slug != "openrouter":
                eligible.append(route)
        elif route.slug == "openrouter":
            if getattr(settings, "openrouter_free_allow_sensitive_data", False):
                eligible.append(route)
        elif route.slug == "gemini":
            if getattr(settings, "gemini_free_allow_sensitive_data", False):
                eligible.append(route)
        elif route.slug == "avalai":
            if getattr(settings, "avalai_allow_sensitive_data", False):
                eligible.append(route)
    return tuple(eligible)


def _task_ordered_routes(routes: tuple[NoteRoute, ...], task: str) -> tuple[NoteRoute, ...]:
    """Enforce the allowlist and task priorities; OpenRouter is always last."""
    by_slug = {route.slug: route for route in routes if route.slug in APPROVED_LLM_PROVIDERS}
    order = TASK_PROVIDER_ORDER.get((task or "").strip().upper())
    if order:
        return tuple(by_slug[slug] for slug in order if slug in by_slug)
    return tuple(route for slug, route in by_slug.items() if slug != "openrouter") + (
        (by_slug["openrouter"],) if "openrouter" in by_slug else ()
    )


def _ordered_routes(
    routes: tuple[NoteRoute, ...], prefer: str, *, task: str = ""
) -> tuple[NoteRoute, ...]:
    """A preference may reorder ordinary primaries, never task policy/fallback."""
    routes = _task_ordered_routes(routes, task)
    if (task or "").strip().upper() in TASK_PROVIDER_ORDER:
        return routes
    slug = (prefer or "").strip().lower()
    if slug not in APPROVED_LLM_PROVIDERS or slug == "openrouter":
        return routes
    return tuple(route for route in routes if route.slug == slug) + tuple(
        route for route in routes if route.slug != slug
    )


async def complete_json(
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
    schema: dict | None = None,
    prefer_provider: str = "",
    max_output_tokens: int | None = None,
    wait_for_capacity: bool = True,
    task: str = "",
    data_sensitivity: str = "UNKNOWN",
) -> dict:
    """A completion parsed as strict JSON, with bounded per-provider recovery.

    Each route gets one JSON attempt; malformed/truncated output moves to the
    next eligible provider instead of repeating a costly generation. Transport
    retries and request-parameter compatibility adjustments remain bounded.
    Task policy overrides preferences, and OpenRouter is always the last route.
    Callers decide how to degrade after the eligible chain is exhausted.
    """
    pool_owner = settings
    if max_output_tokens is not None:
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        settings = replace(settings, note_max_output_tokens=int(max_output_tokens))
    configured_routes = note_routes(settings)
    eligible_routes = _sensitivity_eligible_routes(
        configured_routes, settings, data_sensitivity
    )
    routes = _ordered_routes(eligible_routes, prefer_provider, task=task)
    if not routes:
        if configured_routes:
            raise LLMAuthError(
                "هیچ ارائه‌دهندهٔ پیکربندی‌شده‌ای برای سطح حساسیت این داده مجاز نیست."
            )
        if (data_sensitivity or "UNKNOWN").strip().upper() in {"INTERNAL", "SENSITIVE", "PHI_OR_PII"}:
            raise LLMAuthError(
                "هیچ ارائه‌دهندهٔ مجازی برای سطح حساسیت این داده تأیید نشده است."
            )
        raise LLMAuthError(
            "هیچ ارائه‌دهندهٔ مجاز و دارای کلید پیکربندی نشده است "
            "(GEMINI_API_KEY / AVALAI_API_KEY / OPENROUTER_API_KEY)."
        )

    async def _call_json(route: NoteRoute, is_last: bool) -> dict:
        return await _complete_json_on_route(
            route,
            session,
            settings,
            system=system,
            user=user,
            retry_on_timeout=is_last,
            schema=schema,
        )

    estimated_input = estimate_tokens(
        system + "\\n" + user,
        chars_per_token=getattr(settings, "llm_token_estimation_chars_per_token", 2.0),
    )
    output_reserve = min(
        int(settings.note_max_output_tokens),
        max(1, int(getattr(settings, "note_context_tokens", settings.note_max_output_tokens))),
    )
    return await _dispatch_across_routes(
        routes, session, settings, _call_json, hedge_route=_call_json,
        pool_owner=pool_owner, wait_for_capacity=wait_for_capacity,
        reservation_tokens=estimated_input + output_reserve,
        task=task or "UNSPECIFIED",
        estimated_input_tokens=estimated_input,
        reserved_output_tokens=output_reserve,
    )
