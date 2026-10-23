"""The single LLM client for note generation: ordered providers, bounded retries.

``NOTE_PROVIDERS`` *is* the failover policy (``gemini,nara,openrouter,groq,
cerebras`` by default — Gemini flash-lite first, then each further provider
only when it is listed *and* has its own credential). For every route the
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
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit
import weakref

import aiohttp

from .capacity import CapacityOverloadError, pool_for, reset_pools
from .config import NoteRoute, Settings, note_routes
from .http_util import (
    read_error_body,
    retry_after_seconds as _retry_after_seconds,
    sanitize_error_text as _sanitize_error_text,
)
from .metrics import METRICS

try:
    import orjson as _orjson

    _loads = _orjson.loads
except ModuleNotFoundError:  # stdlib fallback: identical parsing contract
    _orjson = None  # type: ignore[assignment]

    def _loads(body):  # type: ignore[no-redef]
        return json.loads(body.decode("utf-8", "replace"))


MAX_LLM_RESPONSE_BYTES = 16 * 1024 * 1024


async def _read_json_body(response: Any) -> Any:
    """Parse a bounded response body from ``read()`` bytes via :data:`_loads`.

    One narrow compatibility fallback: if the bytes parse to an empty
    object, ``response.json()`` is consulted and used only when it yields
    a different value. Real aiohttp parses the same (cached) body, so
    production results are identical; this only keeps test doubles whose
    ``read()`` is a stub (but whose ``json()`` is faithful) working.
    """
    raw = await response.read()
    if len(raw) > MAX_LLM_RESPONSE_BYTES:
        raise LLMResponseError("حجم پاسخ سرویس تولید جزوه بیش از حد مجاز بود.")
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
MAX_TRANSIENT_RETRIES = 2

_T = TypeVar("_T")

#: Loop- and session-scoped route cooldown map so concurrent requests sharing
#: a client session immediately bypass a rate-limited/depleted primary without
#: colliding across separate sessions or test cases.
_LLM_COOLDOWNS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[int, str], float]
] = weakref.WeakKeyDictionary()

LLM_RATE_LIMIT_COOLDOWN_SECONDS = 15.0
LLM_AUTH_COOLDOWN_SECONDS = 60.0


def _set_route_cooldown(session: object, slug: str, seconds: float) -> None:
    if seconds <= 0:
        return
    loop = asyncio.get_running_loop()
    table = _LLM_COOLDOWNS.setdefault(loop, {})
    table[(id(session), slug)] = time.monotonic() + seconds


def _route_cooldown_remaining(session: object, slug: str) -> float:
    loop = asyncio.get_running_loop()
    table = _LLM_COOLDOWNS.get(loop)
    if not table:
        return 0.0
    key = (id(session), slug)
    deadline = table.get(key)
    if deadline is None:
        return 0.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        table.pop(key, None)
        return 0.0
    return remaining


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
    """Convert a plain JSON schema to Gemini's ``responseSchema`` dialect.

    Gemini spells ``type`` values in upper case and does not accept the parts of
    JSON Schema that a strict-mode OpenAI request needs, so the same schema
    cannot be sent verbatim to both.
    """
    if isinstance(node, list):
        return [_gemini_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    converted: dict = {}
    for key, value in node.items():
        if key == "type" and isinstance(value, str):
            converted[key] = value.upper()
        elif key == "required":
            continue  # Gemini infers required properties from the schema.
        elif key in {"properties"} and isinstance(value, dict):
            converted[key] = {name: _gemini_schema(item) for name, item in value.items()}
        else:
            converted[key] = _gemini_schema(value)
    return converted


def _openai_json_schema(schema: dict) -> dict:
    """The documented OpenAI-compatible ``json_schema`` wrapper (non-strict).

    ``strict: true`` demands that every property is listed in ``required`` and
    that ``additionalProperties`` is false, which a note document (whose
    sections legitimately omit empty fields) cannot truthfully declare. The
    non-strict form still constrains the shape and is what gateways accept.
    """
    return {"name": "gamas_notes", "schema": schema}


def _build_request_for(
    route: NoteRoute,
    settings: Settings,
    system: str,
    user: str,
    *,
    schema: dict | None = None,
) -> tuple[str, dict[str, str], dict]:
    """Build one provider's POST request from its own route.

    Authentication is never caller-overridable: each style sets its documented
    header itself. ``schema`` is attached only when a caller asks for
    structured output; a provider that rejects the parameter drops it through
    the generic rejected-parameter retry.
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
        generation_config = {
            "temperature": NOTE_TEMPERATURE,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        }
        if schema is not None:
            generation_config["responseSchema"] = _gemini_schema(schema)
        if settings.gemini_thinking_budget >= 0:
            # ``0`` (the default) turns thinking off entirely: a note request is
            # extraction/organisation, so hidden reasoning tokens buy nothing
            # and cost latency. A positive value caps them; ``-1`` sends no
            # ``thinkingConfig`` at all (the pre-flag behaviour). A model that
            # rejects the field is handled by the generic rejected-parameter
            # retry below, so no model-name sniffing is needed here.
            generation_config["thinkingConfig"] = {
                "thinkingBudget": settings.gemini_thinking_budget,
            }
        payload = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": generation_config,
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
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": _openai_json_schema(schema),
        }
    elif route.json_object_mode:
        # Opt-in, and only for providers that document ``response_format``
        # (NaraRouter does not, so it is never asked for it).
        payload["response_format"] = {"type": "json_object"}
    if getattr(settings, "note_openrouter_throughput", False) and route.slug == "openrouter":
        # OpenRouter load-balances by *price* by default, which can pick a much
        # slower upstream of the same model. Its documented throughput sort is
        # the only per-request control over that choice.
        payload["provider"] = {"sort": "throughput"}
    if settings.note_reasoning_effort and route.slug in (
        "nara", "openrouter", "groq", "cerebras"
    ):
        # Only the gateways whose contracts were verified for this field:
        # NaraRouter documents ``reasoning_effort`` and clamps unsupported
        # levels instead of erroring, OpenRouter accepts it for the models
        # that support it, and Groq and Cerebras document
        # ``reasoning_effort`` on their OpenAI-compatible gpt-oss endpoints
        # (where ``low`` keeps the model from spending output tokens on
        # hidden reasoning). A generic OpenAI-compatible gateway (whose model
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
    """Race two route attempts, starting the second only after a delay.

    A first attempt that finishes (success *or* failure) before the hedge
    delay is returned/raised unchanged via :class:`_PrimaryEarlyFailure`
    for failures, so sub-threshold behaviour is exactly the sequential
    path. Once both run, the first success wins and the loser is cancelled.
    At most one extra in-flight POST per pipeline call is added.
    """
    first_task = asyncio.ensure_future(first_factory())
    done, _pending = await asyncio.wait({first_task}, timeout=hedge_after)
    if first_task in done:
        try:
            return first_task.result()
        except LLMError as exc:
            raise _PrimaryEarlyFailure(exc) from exc
    second_task = asyncio.ensure_future(second_factory())
    pending = {first_task, second_task}
    errors: dict = {}

    async def _settle_losers() -> None:
        for loser in list(pending):
            if loser.done():
                continue
            loser.cancel()
            try:
                await loser
            except (asyncio.CancelledError, LLMError):
                pass
            except Exception:  # pragma: no cover - a loser's own error is moot
                pass

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                result = task.result()
            except asyncio.CancelledError:
                # The task was cancelled from outside: treat it as a failure of
                # this attempt rather than crashing the race.
                errors[task] = LLMError("درخواست لغو شد.")
                continue
            except LLMError as exc:
                errors[task] = exc
                continue
            except Exception:
                # A non-LLMError (a bug, a malformed local gateway) must not be
                # swallowed by the hedged path: stop the sibling and surface it
                # exactly as the sequential path would.
                await _settle_losers()
                raise
            await _settle_losers()
            return result
    raise _HedgedFailures(errors[first_task], errors[second_task])


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
) -> str:
    """OpenAI- or Gemini-style streaming completion guarded by a first-byte deadline."""
    ttfb_timeout = float(getattr(settings, "note_ttfb_timeout", 0.0) or 0.0)
    is_gemini = route.style == "gemini"
    if is_gemini:
        stream_url = _gemini_sse_url(url)
        stream_payload = payload
    else:
        stream_url = url
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        stream_payload["stream_options"] = {"include_usage": True}
    started = time.perf_counter()
    ttfb_ms = 0.0
    text_parts: list[str] = []
    finish_reason = ""
    usage: dict = {}
    saw_thought = False
    model_version = ""
    raw_fallback_body: bytes | None = None
    async with session.post(
        stream_url,
        headers=headers,
        json=stream_payload,
        timeout=aiohttp.ClientTimeout(total=timeout_seconds, sock_connect=10),
    ) as response:
        if response.status != 200:
            raise await _error_from_response(route.label, response, route.api_key or None)
        resp_headers = getattr(response, "headers", None)
        content = getattr(response, "content", None)
        iterator = content.__aiter__() if hasattr(content, "__aiter__") else None
        lines: list = []
        if iterator is not None:
            try:
                first_line = await asyncio.wait_for(iterator.__anext__(), timeout=ttfb_timeout)
            except asyncio.TimeoutError as exc:
                raise _TTFBTimeoutError(
                    f"{route.label} در مهلت نخستین بایت پاسخ نداد."
                ) from exc
            except StopAsyncIteration:
                first_line = None
            ttfb_ms = (time.perf_counter() - started) * 1000.0
            if first_line is not None:
                lines.append(first_line)
                async for line in iterator:
                    lines.append(line)
        else:  # Fallback for doubles exposing only read()
            body = await response.read()
            raw_fallback_body = body
            ttfb_ms = (time.perf_counter() - started) * 1000.0
            lines = body.splitlines()
        parsed_chunks = 0
        for line in lines:
            chunk = _parse_sse_line(line)
            if chunk is None:
                continue
            parsed_chunks += 1
            error_obj = chunk.get("error")
            if _has_error_content(error_obj):
                status = _infer_in_body_error_status(error_obj)
                raise _error_from_payload(
                    route.label, status, {"error": error_obj}, resp_headers, route.api_key or None
                )
            if is_gemini:
                if isinstance(chunk.get("usageMetadata"), dict):
                    usage = chunk["usageMetadata"]
                if isinstance(chunk.get("modelVersion"), str):
                    model_version = chunk["modelVersion"]
                candidates = chunk.get("candidates")
                if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
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
                continue
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
                delta = choice.get("delta")
                if isinstance(delta, dict) and delta.get("content"):
                    text_parts.append(str(delta["content"]))
                elif isinstance(choice.get("message"), dict) and choice["message"].get("content"):
                    text_parts.append(str(choice["message"]["content"]))
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if parsed_chunks == 0 and raw_fallback_body:
        try:
            fallback_dict = _loads(raw_fallback_body)
            if isinstance(fallback_dict, dict):
                _log_usage(route, fallback_dict, ttfb_ms=ttfb_ms, elapsed_ms=elapsed_ms)
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
        assembled = {
            "candidates": [{
                "content": {"parts": assembled_parts},
                "finishReason": finish_reason or "STOP",
            }],
            "usageMetadata": usage,
        }
        if model_version:
            assembled["modelVersion"] = model_version
    else:
        assembled = {
            "choices": [{
                "message": {"content": "".join(text_parts)},
                "finish_reason": finish_reason or "stop",
            }],
            "usage": usage,
        }
    _log_usage(route, assembled, ttfb_ms=ttfb_ms, elapsed_ms=elapsed_ms)
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
            if (
                float(getattr(settings, "note_ttfb_timeout", 0.0) or 0.0) > 0
                and route.style in ("openai", "gemini")
            ):
                return await _complete_streaming_on_route(
                    route, session, settings, url=url, headers=headers,
                    payload=payload, timeout_seconds=timeout_seconds,
                )
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


async def _dispatch_across_routes(
    routes: tuple[NoteRoute, ...],
    session: object,
    settings: Settings,
    call_route: Callable[[NoteRoute, bool], Awaitable[_T]],
    *,
    hedge_route: Callable[[NoteRoute, bool], Awaitable[_T]] | None = None,
) -> _T:
    """Execute *call_route* across *routes* with non-blocking capacity reservation."""
    pool = pool_for("llm", settings)
    failures: list[LLMError] = []
    descriptions: list[str] = []
    indexed_routes: list[tuple[int, NoteRoute]] = list(enumerate(routes))
    hedge_after = float(getattr(settings, "note_hedge_after", 0.0) or 0.0)

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
                    timeout=getattr(settings, "llm_slot_wait_timeout", 30.0),
                )
                if acquired_b is None:
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
                descriptions.append(f"{routes[0].label}: {early.exc}")
                logger.warning(
                    "note provider failed; trying the next one provider=%s error=%s detail=%s",
                    routes[0].slug, type(early.exc).__name__, early.exc,
                )
                indexed_routes = indexed_routes[1:]
            except _HedgedFailures as both:
                for route, exc in ((routes[0], both.first), (routes[1], both.second)):
                    failures.append(exc)
                    descriptions.append(f"{route.label}: {exc}")
                    logger.warning(
                        "note provider failed; trying the next one provider=%s error=%s detail=%s",
                        route.slug, type(exc).__name__, exc,
                    )
                indexed_routes = indexed_routes[2:]

    previous_failed_slug: str | None = failures and routes[0].slug or None  # type: ignore[assignment]
    wait_timeout = float(getattr(settings, "llm_slot_wait_timeout", 30.0))

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
            result = await _run_with_lease(
                route,
                lease,
                lambda r=route, last=is_last_route: call_route(r, last),  # type: ignore[misc]
            )
        except LLMError as exc:
            previous_failed_slug = route.slug
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
) -> str:
    """One completion from the first eligible provider with capacity that answers."""
    routes = note_routes(settings)
    if not routes:
        raise LLMAuthError(
            "هیچ سرویس تولید جزوه‌ای پیکربندی نشده است (NOTE_PROVIDERS / NOTE_API_KEY / "
            "NARA_API_KEY / OPENROUTER_API_KEY)."
        )

    async def _call(route: NoteRoute, is_last: bool) -> str:
        return await _complete_on_route(
            route,
            session,
            settings,
            system=system,
            user=user,
            retry_on_timeout=is_last,
        )

    return await _dispatch_across_routes(
        routes, session, settings, _call, hedge_route=_call
    )


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
    for attempt, prompt in enumerate((user, JSON_ONLY_REMINDER + user)):
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


def _ordered_routes(routes: tuple[NoteRoute, ...], prefer: str) -> tuple[NoteRoute, ...]:
    """Move ``prefer`` to the front, keeping every other route as a fallback."""
    slug = (prefer or "").strip().lower()
    if not slug:
        return routes
    preferred = tuple(route for route in routes if route.slug == slug)
    if not preferred:
        return routes
    return preferred + tuple(route for route in routes if route.slug != slug)


async def complete_json(
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
    schema: dict | None = None,
    prefer_provider: str = "",
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
    routes = _ordered_routes(note_routes(settings), prefer_provider)
    if not routes:
        raise LLMAuthError(
            "هیچ سرویس تولید جزوه‌ای پیکربندی نشده است (NOTE_PROVIDERS / NOTE_API_KEY / "
            "NARA_API_KEY / OPENROUTER_API_KEY)."
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

    return await _dispatch_across_routes(
        routes, session, settings, _call_json, hedge_route=_call_json
    )
