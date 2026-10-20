"""The single LLM client for note generation: ordered providers, bounded retries.

``NOTE_PROVIDERS`` *is* the failover policy (``nara,openrouter`` by default,
i.e. Nara first, OpenRouter second, any other provider only when it is listed
and configured). For every route the client builds that provider's own request
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
import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .config import NoteRoute, Settings, note_routes

try:
    import orjson

    _loads = orjson.loads
except ModuleNotFoundError:  # stdlib fallback: identical parsing contract
    def _loads(body):  # type: ignore[no-redef]
        return json.loads(body.decode("utf-8", "replace"))


async def _read_json_body(response: Any) -> Any:
    """Parse a response body from ``read()`` bytes via :data:`_loads`.

    One narrow compatibility fallback: if the bytes parse to an empty
    object, ``response.json()`` is consulted and used only when it yields
    a different value. Real aiohttp parses the same (cached) body, so
    production results are identical; this only keeps test doubles whose
    ``read()`` is a stub (but whose ``json()`` is faithful) working.
    """
    result = _loads(await response.read())
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
MAX_TRANSIENT_RETRIES = 2

#: Authentication header names a caller may never override.
RESERVED_HEADER_NAMES = frozenset(
    {"authorization", "x-api-key", "x-goog-api-key", "anthropic-version"}
)


class LLMError(RuntimeError):
    """Base class; the message is safe to show the user."""


class LLMTransientError(LLMError):
    """Network/rate-limit/server failure after the bounded retry budget."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


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

    NaraRouter documents this for reasoning models: thinking tokens are spent
    out of the same ``max_tokens`` budget, so a small budget can return an
    empty or half-finished answer (https://router.bynara.id/docs, "Output
    budget"). Re-sending the same request with the same budget produces the
    same truncation, so this is treated as permanent for the provider — the
    fix is a larger ``NOTE_MAX_OUTPUT_TOKENS``, or the next provider in the
    chain, never another identical attempt.
    """


#: Structured error codes/types that providers use to report an input/context
#: token limit failure (OpenRouter ``error.metadata.error_type``, OpenAI/Nara
#: ``error.code``/``error.type``).
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
    # NaraRouter answers an oversized body with HTTP 413 / ``bad_request`` and
    # the generic wording "The request body or input is too large."
    # (https://router.bynara.id/docs, "Status codes").
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


def _parse_retry_value(raw: object) -> float | None:
    """Parse one Retry-After / RetryInfo / RateLimit-Reset value to seconds."""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    # Google RPC RetryInfo format: "42s" or "12.5s".
    if text.endswith("s") and text[:-1].replace(".", "", 1).isdigit():
        text = text[:-1]
    try:
        value = float(text)
        if math.isfinite(value):
            now = datetime.now(timezone.utc).timestamp()
            if value > 1_000_000_000_000:
                # OpenRouter X-RateLimit-Reset is Unix epoch in milliseconds.
                value = (value / 1000.0) - now
            elif value > 1_000_000_000:
                # Unix epoch in seconds.
                value = value - now
            return min(max(value, 0.0), 86_400.0)
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(text)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return min(
            max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0),
            86_400.0,
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _retry_after_seconds(headers, payload: object = None) -> float | None:
    if headers:
        for header_name in ("Retry-After", "retry-after", "X-RateLimit-Reset", "x-ratelimit-reset"):
            parsed = _parse_retry_value(headers.get(header_name, ""))
            if parsed is not None:
                return parsed
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            metadata = error.get("metadata")
            if isinstance(metadata, dict):
                meta_headers = metadata.get("headers")
                if isinstance(meta_headers, dict):
                    for key in ("Retry-After", "retry-after", "X-RateLimit-Reset", "x-ratelimit-reset"):
                        parsed = _parse_retry_value(meta_headers.get(key, ""))
                        if parsed is not None:
                            return parsed
            details = error.get("details")
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict) and "retryDelay" in item:
                        parsed = _parse_retry_value(item.get("retryDelay"))
                        if parsed is not None:
                            return parsed
    return None


def _sanitize_error_text(value: object, limit: int = 180) -> str:
    return " ".join(str(value).split())[:limit]


def _redact_secrets(text: str, api_key: str | None) -> str:
    if not text:
        return text
    if api_key:
        clean_key = re.sub(r"^bearer\s+", "", api_key.strip(), flags=re.IGNORECASE).strip()
        for candidate in (api_key, clean_key):
            if candidate and candidate in text:
                text = text.replace(candidate, "***")
    return _SECRET_TOKEN_RE.sub("***", text)


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
    # The provider's own correlation key. NaraRouter's error envelope carries
    # ``request_id`` precisely so a failure can be matched against its logs
    # (https://router.bynara.id/docs, "Errors"), so it belongs in our message.
    request_id = ""
    error_markers: list[str] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        candidate = None
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                detail = _sanitize_error_text(message)
            for field in ("status", "code", "type"):
                value = error.get(field)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    error_markers.append(str(value).upper())
            candidate = error.get("request_id")
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
        if not candidate:
            candidate = payload.get("request_id")
        if isinstance(candidate, str) and candidate.strip():
            request_id = _sanitize_error_text(candidate, limit=80)
    if not request_id and headers:
        header_req_id = headers.get("x-request-id") or headers.get("X-Request-Id")
        if isinstance(header_req_id, str) and header_req_id.strip():
            request_id = _sanitize_error_text(header_req_id, limit=80)
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
    # NaraRouter reserves 413 for an oversized body ("The request body or input
    # is too large"), and OpenRouter/OpenAI report typed context/token limit
    # codes in error.metadata.error_type / error.code.
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
        return LLMTransientError(
            f"{label} موقتاً در دسترس نیست (HTTP {status}){suffix}",
            retry_after_seconds=_retry_after_seconds(headers, payload),
        )
    return LLMRequestError(f"{label} درخواست را رد کرد (HTTP {status}){suffix}")


async def _error_from_response(
    label: str, response: aiohttp.ClientResponse, api_key: str | None
) -> LLMError:
    body = await response.read()
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


def _build_request_for(
    route: NoteRoute, settings: Settings, system: str, user: str
) -> tuple[str, dict[str, str], dict]:
    """Build one provider's POST request from its own route.

    Authentication is never caller-overridable: each style sets its documented
    header itself.
    """
    style = route.style
    key = _clean_api_key(route.api_key)
    model = route.model.strip()
    max_tokens = settings.note_max_output_tokens

    if style == "gemini":
        if not key:
            raise LLMAuthError("کلید سرویس تولید جزوه تنظیم نشده است.")
        # Accept the documented "models/<name>" spelling without doubling it.
        model_id = _gemini_model_id(model)
        url = _endpoint(route.base_url, f"models/{model_id}:generateContent")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "temperature": NOTE_TEMPERATURE,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        return url, {"x-goog-api-key": key}, payload

    if style == "anthropic":
        if not key:
            raise LLMAuthError("برای Anthropic باید NOTE_API_KEY تنظیم شود.")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": NOTE_TEMPERATURE,
        }
        return _endpoint(route.base_url, "messages"), headers, payload

    # openai: NaraRouter, OpenRouter and any OpenAI-compatible gateway.
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": NOTE_TEMPERATURE,
        "max_tokens": max_tokens,
    }
    if route.json_object_mode:
        # Opt-in, and only for providers that document ``response_format``
        # (NaraRouter does not, so it is never asked for it).
        payload["response_format"] = {"type": "json_object"}
    if settings.note_reasoning_effort and route.slug in ("nara", "openrouter"):
        # Only the two gateways whose contracts were verified for this field:
        # NaraRouter documents ``reasoning_effort`` and clamps unsupported
        # levels instead of erroring, and OpenRouter accepts it for the models
        # that support it. A generic OpenAI-compatible gateway (whose model
        # may reject the parameter outright) and the Gemini/Anthropic styles
        # above never receive it. Unset (the default) sends nothing, so the
        # provider's own reasoning default applies.
        payload["reasoning_effort"] = settings.note_reasoning_effort
    return _endpoint(route.base_url, "chat/completions"), headers, payload


def _build_request(
    settings: Settings, system: str, user: str, *, route: NoteRoute | None = None
) -> tuple[str, dict[str, str], dict]:
    """Build the POST request for one route — by default the first eligible one."""
    if route is None:
        routes = note_routes(settings)
        if not routes:
            raise LLMAuthError(
                "هیچ سرویس تولید جزوه‌ای پیکربندی نشده است (NOTE_PROVIDERS / NOTE_API_KEY / "
                "NARA_API_KEY / OPENROUTER_API_KEY)."
            )
        route = routes[0]
    return _build_request_for(route, settings, system, user)


def _build_get_request_for(
    route: NoteRoute, target: str = "models"
) -> tuple[str, dict[str, str]]:
    """Build one provider's documented GET metadata request ``(url, headers)``.

    Supported ``target`` values across providers:
    * ``"models"``: ``GET /v1/models`` (NaraRouter / OpenAI-compatible),
      ``GET /api/v1/models`` (OpenRouter), ``GET /v1beta/models`` (Gemini).
    * ``"model"``: ``GET /v1beta/models/{model}`` (Gemini single-model resource)
      or ``GET {base}/models/{model}`` (OpenAI-compatible).
    * ``"model_endpoints"``: ``GET /api/v1/models/{author}/{slug}/endpoints``
      (OpenRouter per-model endpoint capacities and parameters).
    * ``"key"``: ``GET /api/v1/key`` (OpenRouter API key limits and free-tier
      daily request counter).
    * ``"plans"``: ``GET https://router.bynara.id/api/plans`` (NaraRouter public
      plan & model catalogue; no authentication header is sent).
    """
    style = route.style
    key = _clean_api_key(route.api_key)
    clean_target = target.strip().strip("/")

    if style == "gemini":
        if not key:
            raise LLMAuthError("کلید سرویس تولید جزوه تنظیم نشده است.")
        headers = {"x-goog-api-key": key}
        if clean_target == "model":
            model_id = _gemini_model_id(route.model)
            return _endpoint(route.base_url, f"models/{model_id}"), headers
        return _endpoint(route.base_url, clean_target or "models"), headers

    if style == "anthropic":
        if not key:
            raise LLMAuthError("برای Anthropic باید NOTE_API_KEY تنظیم شود.")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        return _endpoint(route.base_url, clean_target or "models"), headers

    if clean_target in ("plans", "api/plans"):
        parsed = urlsplit(route.base_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "/api/plans", "", "")), {}

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if clean_target == "model_endpoints":
        model_path = _openrouter_model_path(route.model)
        return _endpoint(route.base_url, f"models/{model_path}/endpoints"), headers
    if clean_target == "model":
        model_path = _openrouter_model_path(route.model)
        return _endpoint(route.base_url, f"models/{model_path}"), headers
    return _endpoint(route.base_url, clean_target or "models"), headers


def _build_get_request(
    settings: Settings, target: str = "models", *, route: NoteRoute | None = None
) -> tuple[str, dict[str, str]]:
    """Build the GET request for one route — by default the first eligible one."""
    if route is None:
        routes = note_routes(settings)
        if not routes:
            raise LLMAuthError(
                "هیچ سرویس تولید جزوه‌ای پیکربندی نشده است (NOTE_PROVIDERS / NOTE_API_KEY / "
                "NARA_API_KEY / OPENROUTER_API_KEY)."
            )
        route = routes[0]
    return _build_get_request_for(route, target)


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


def _output_truncated(payload: dict, provider: str) -> bool:
    """True when the provider says the answer hit its output budget.

    Each request style spells this differently: ``finish_reason: "length"``
    (OpenAI-compatible, which is what NaraRouter and OpenRouter speak;
    OpenRouter also exposes the raw upstream reason in ``native_finish_reason``),
    ``finishReason: "MAX_TOKENS"`` or ``"CONTINUATION"`` (Gemini), and
    ``stop_reason: "max_tokens"`` (Anthropic).
    """
    try:
        if provider == "gemini":
            reason = str(payload["candidates"][0].get("finishReason") or "").strip().upper()
            return reason in ("MAX_TOKENS", "CONTINUATION", "LENGTH")
        if provider == "anthropic":
            return payload.get("stop_reason") == "max_tokens"
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


def _reasoning_exhausted_output(payload: dict, provider: str) -> bool:
    """True when a thinking/reasoning model spent its output tokens on thoughts
    and left zero visible answer text."""
    if provider == "gemini":
        usage = payload.get("usageMetadata")
        if isinstance(usage, dict):
            thoughts = _usage_count(usage.get("thoughtsTokenCount"))
            if thoughts is not None and thoughts > 0:
                return True
        return False
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


def _parse_response(payload: dict, provider: str) -> str:
    """Normalize a provider response to plain text."""
    # Checked before indexing into candidate/message content: when Gemini or a
    # reasoning gateway hits MAX_TOKENS / length while still thinking, the
    # response candidate often omits ``content.parts`` altogether.
    if _output_truncated(payload, provider):
        raise LLMOutputBudgetError(
            "پاسخ سرویس تولید جزوه پیش از پایان به سقف توکن خروجی رسید "
            "(NOTE_MAX_OUTPUT_TOKENS را افزایش دهید)."
        )
    try:
        if provider == "gemini":
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
                if _reasoning_exhausted_output(payload, provider):
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
            # Gemini thinking models mark thought-summary parts with
            # ``"thought": true`` (https://ai.google.dev/gemini-api/docs/thinking);
            # never concatenate internal thought summaries into the answer text.
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
            if not text.strip() and (saw_thought or _reasoning_exhausted_output(payload, provider)):
                raise LLMOutputBudgetError(
                    "پاسخ سرویس تولید جزوه پیش از پایان به سقف توکن خروجی رسید "
                    "(NOTE_MAX_OUTPUT_TOKENS را افزایش دهید)."
                )
        elif provider == "anthropic":
            text = "".join(
                str(part.get("text", ""))
                for part in payload["content"]
                if part.get("type") == "text"
            )
        else:
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
            content = message.get("content") if isinstance(message, dict) else choice["message"]["content"]
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
                    raise LLMResponseError(
                        "پاسخ سرویس تولید جزوه مسدود شد (refusal)."
                    )
                if _reasoning_exhausted_output(payload, provider):
                    raise LLMOutputBudgetError(
                        "پاسخ سرویس تولید جزوه پیش از پایان به سقف توکن خروجی رسید "
                        "(NOTE_MAX_OUTPUT_TOKENS را افزایش دهید)."
                    )
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


def _usage_count(value: object) -> int | None:
    """One token count from a provider usage block, or ``None`` if unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _usage_summary(payload: dict, style: str) -> tuple[str, int | None, int | None, int | None]:
    """``(served_model, input_tokens, output_tokens, total_tokens)`` of a reply.

    Each request style reports usage under its own names: OpenAI-compatible
    ``usage.prompt_tokens``/``completion_tokens``, Gemini ``usageMetadata``
    (served model arrives as ``modelVersion``) and Anthropic ``usage``
    ``input_tokens``/``output_tokens`` with no total of its own. Anything a
    provider omits stays ``None``: the summary is for calibrating the
    character-to-token estimate against real Persian traffic, never for
    billing, so a partial report is logged as-is instead of being guessed.
    """
    model = payload.get("model") or payload.get("modelVersion") or ""
    input_tokens = output_tokens = total_tokens = None
    if style == "gemini":
        usage = payload.get("usageMetadata")
        if isinstance(usage, dict):
            input_tokens = _usage_count(usage.get("promptTokenCount"))
            output_tokens = _usage_count(usage.get("candidatesTokenCount"))
            total_tokens = _usage_count(usage.get("totalTokenCount"))
    elif style == "anthropic":
        usage = payload.get("usage")
        if isinstance(usage, dict):
            input_tokens = _usage_count(usage.get("input_tokens"))
            output_tokens = _usage_count(usage.get("output_tokens"))
    else:
        usage = payload.get("usage")
        if isinstance(usage, dict):
            input_tokens = _usage_count(usage.get("prompt_tokens"))
            output_tokens = _usage_count(usage.get("completion_tokens"))
            total_tokens = _usage_count(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return str(model), input_tokens, output_tokens, total_tokens


def _log_usage(
    route: NoteRoute, payload: dict, *, ttfb_ms: float = 0.0, elapsed_ms: float = 0.0
) -> None:
    """Log one completed request's token usage (never its content or key)."""
    model, input_tokens, output_tokens, total_tokens = _usage_summary(payload, route.style)

    def shown(value: int | None) -> str:
        return str(value) if value is not None else "-"

    logger.info(
        "note provider usage provider=%s model=%s input_tokens=%s "
        "output_tokens=%s total_tokens=%s ttfb_ms=%.0f elapsed_ms=%.0f",
        route.slug,
        # The served-model string is provider-controlled: collapse any
        # whitespace (including newlines) so it can never forge a log line.
        _sanitize_error_text(model or route.model, limit=80),
        shown(input_tokens),
        shown(output_tokens),
        shown(total_tokens),
        ttfb_ms,
        elapsed_ms,
    )


def _adjust_rejected_request_param(payload: dict, error_text: str) -> str | None:
    """Adjust a request field after an explicit 400/422 parameter rejection."""
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
    )
    if "response_format" in payload and any(
        marker in lower for marker in ("response_format", "json_object", "structured_output")
    ):
        payload.pop("response_format", None)
        return "response_format"
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
    generation_config = payload.get("generationConfig")
    if isinstance(generation_config, dict):
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
        "temperature" in payload
        and "temperature" in lower
        and any(marker in lower for marker in rejected_markers)
    ):
        payload.pop("temperature", None)
        return "temperature"
    return None


async def _complete_on_route(
    route: NoteRoute,
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
    retry_on_timeout: bool = True,
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
    url, headers, payload = _build_request_for(route, settings, system, user)
    timeout_seconds = float(settings.note_request_timeout or 0) or (
        LLM_REQUEST_TIMEOUT_SECONDS
    )
    attempt = 0
    request_number = 0
    while True:
        request_number += 1
        logger.info(
            "LLM request sending provider=%s model=%s style=%s attempt=%d system_chars=%d "
            "user_chars=%d output_tokens=%d fields=%s",
            route.slug,
            route.model,
            route.style,
            request_number,
            len(system),
            len(user),
            settings.note_max_output_tokens,
            ",".join(sorted(payload)),
        )
        started = time.perf_counter()
        ttfb_ms = 0.0
        elapsed_ms = 0.0
        try:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
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
            _log_usage(route, result, ttfb_ms=ttfb_ms, elapsed_ms=elapsed_ms)
            _raise_if_in_body_error(
                route.label, result, resp_headers, route.api_key or None
            )
            return _parse_response(result, route.style)
        except LLMRequestError as exc:
            if not isinstance(exc, (LLMContextOverflowError, LLMOutputBudgetError)):
                adjustment = _adjust_rejected_request_param(payload, str(exc))
                if adjustment is not None:
                    logger.info(
                        "Adjusting rejected request parameter and retrying "
                        "provider=%s adjustment=%s",
                        route.slug,
                        adjustment,
                    )
                    continue
            raise
        except (LLMTransientError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            timed_out = isinstance(exc, asyncio.TimeoutError)
            if timed_out and not retry_on_timeout:
                # The provider did not answer inside the deadline. Asking the
                # same (expensive) request again only repeats the wait, so the
                # provider is left behind and the chain moves on.
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
            retry_after = (
                exc.retry_after_seconds if isinstance(exc, LLMTransientError) else None
            )
            delay = retry_after if retry_after is not None else min(2.0**attempt, 15.0)
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
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
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
            delay = retry_after if retry_after is not None else min(2.0**attempt, 15.0)
            logger.warning(
                "LLM GET transient failure provider=%s target=%s (attempt %d/%d; %s); retrying in %.1fs",
                route.slug, target, attempt, MAX_TRANSIENT_RETRIES + 1,
                type(exc).__name__, delay,
            )
            await asyncio.sleep(delay)


async def complete(
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
) -> str:
    """One completion from the first eligible provider that answers.

    Providers are tried in the configured order. Each one gets its own bounded
    retry budget for transient failures only; an authentication problem, a
    rejected request or an unusable answer moves on to the next provider
    immediately. The failure class of the *first* provider is preserved, so a
    context overflow still reaches the caller as one.
    """
    routes = note_routes(settings)
    if not routes:
        raise LLMAuthError(
            "هیچ سرویس تولید جزوه‌ای پیکربندی نشده است (NOTE_PROVIDERS / NOTE_API_KEY / "
            "NARA_API_KEY / OPENROUTER_API_KEY)."
        )
    failures: list[LLMError] = []
    descriptions: list[str] = []
    for index, route in enumerate(routes):
        try:
            text = await _complete_on_route(
                route, session, settings, system=system, user=user,
                # A provider that ran out of time is left behind when a
                # successor is still available: re-sending the same request
                # would only wait for the same deadline again.
                retry_on_timeout=index == len(routes) - 1,
            )
        except LLMError as exc:
            failures.append(exc)
            descriptions.append(f"{route.label}: {exc}")
            logger.warning(
                "note provider failed; trying the next one provider=%s error=%s detail=%s",
                route.slug, type(exc).__name__, exc,
            )
            continue
        if failures:
            logger.info(
                "note generation succeeded after %d provider failure(s) provider=%s",
                len(failures), route.slug,
            )
        return text
    if len(descriptions) > 1:
        logger.error("every configured note provider failed: %s", " | ".join(descriptions))
    raise type(failures[0])("؛ ".join(descriptions))


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: str) -> str:
    """Extract the outermost JSON object from a model answer.

    Models wrap JSON in prose or code fences despite instructions; the
    extraction is bracket-balanced from the first ``{`` to its matching ``}``.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start < 0:
        raise LLMResponseError("پاسخ مدل شامل هیچ شیء JSON نبود.")
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
                return text[start : index + 1]
    raise LLMResponseError("شیء JSON در پاسخ مدل کامل نبود.")


def _parse_json_object(text: str) -> dict:
    """Parse strict JSON and enforce the object shape used by note prompts."""
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


async def complete_json(
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
) -> dict:
    """A completion parsed as strict JSON, with bounded per-provider recovery.

    Each provider gets at most two requests: the original one and a single retry
    whose prompt is a minimal "valid JSON only" reminder. A provider that still
    cannot produce JSON is left behind for the next provider in the chain — its
    answer is a fresh chance rather than a repeat of the same request — and when
    the chain runs out the caller degrades to the raw transcript, which the user
    already holds. A rejected request (4xx) is permanent for that provider and is
    never re-sent to it with the same content.
    """
    routes = note_routes(settings)
    if not routes:
        raise LLMAuthError(
            "هیچ سرویس تولید جزوه‌ای پیکربندی نشده است (NOTE_PROVIDERS / NOTE_API_KEY / "
            "NARA_API_KEY / OPENROUTER_API_KEY)."
        )
    failures: list[LLMError] = []
    descriptions: list[str] = []
    for index, route in enumerate(routes):
        for attempt, prompt in enumerate((user, JSON_ONLY_REMINDER + user)):
            try:
                raw = await _complete_on_route(
                    route, session, settings, system=system, user=prompt,
                    retry_on_timeout=index == len(routes) - 1,
                )
                return _parse_json_object(raw)
            except LLMRequestError as exc:
                # Permanent for this provider: another provider's model or
                # parameters may still accept the request, this one will not.
                failures.append(exc)
                descriptions.append(f"{route.label}: {exc}")
                logger.warning(
                    "note provider rejected the request provider=%s error=%s",
                    route.slug, exc,
                )
                break
            except LLMError as exc:
                failures.append(exc)
                descriptions.append(f"{route.label}: {exc}")
                logger.warning(
                    "note request failed provider=%s attempt=%d error=%s detail=%s",
                    route.slug, attempt + 1, type(exc).__name__, exc,
                )
                if not isinstance(exc, LLMResponseError) or attempt:
                    # Transient and authentication failures already exhausted
                    # their own budget; a second unusable answer ends this
                    # provider's turn.
                    break
    raise type(failures[0])("؛ ".join(descriptions))
