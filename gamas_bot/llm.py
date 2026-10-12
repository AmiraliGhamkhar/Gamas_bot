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
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .config import NoteRoute, Settings, note_routes

logger = logging.getLogger(__name__)

#: Sampling hint for structured note generation. Low, because the note task is
#: extraction and organisation, not creative writing.
NOTE_TEMPERATURE = 0.2

#: A hung note-provider request must not inherit the STT job's multi-hour read timeout.
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
    "too many tokens",
    "token limit",
    "input is too long",
    "prompt is too long",
    "reduce the length",
    "max_tokens is too large",
    "maximum tokens",
    "request too large",
    "context overflow",
    "context limit",
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
    path = base_path if base_path.endswith(suffix_path) else base_path + suffix_path
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _retry_after_seconds(headers) -> float | None:
    raw = str(headers.get("Retry-After", "")).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        if math.isfinite(seconds):
            return min(max(seconds, 0.0), 86_400.0)
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return min(max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0), 86_400.0)
    except (TypeError, ValueError, OverflowError):
        return None


def _sanitize_error_text(value: object, limit: int = 180) -> str:
    return " ".join(str(value).split())[:limit]


async def _error_from_response(
    label: str, response: aiohttp.ClientResponse, api_key: str | None
) -> LLMError:
    body = await response.read()
    detail = None
    error_markers: list[str] = []
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str):
                    detail = _sanitize_error_text(message)
                for field in ("status", "code", "type"):
                    value = error.get(field)
                    if isinstance(value, (str, int)):
                        error_markers.append(str(value).upper())
                details = error.get("details")
                if isinstance(details, list):
                    for item in details:
                        if not isinstance(item, dict):
                            continue
                        reason = item.get("reason")
                        metadata = item.get("metadata")
                        if isinstance(reason, str):
                            error_markers.append(reason.upper())
                        if isinstance(metadata, dict):
                            meta_reason = metadata.get("reason")
                            if isinstance(meta_reason, str):
                                error_markers.append(meta_reason.upper())
            elif isinstance(error, str):
                detail = _sanitize_error_text(error)
    except (ValueError, TypeError):
        pass
    if error_markers:
        codes = _sanitize_error_text(", ".join(dict.fromkeys(error_markers)), limit=80)
        detail = f"{detail} [{codes}]" if detail else codes
    if detail and api_key and api_key in detail:
        detail = detail.replace(api_key, "***")
    suffix = f": {detail}" if detail else ""
    lower_detail = (detail or "").lower()
    auth_error = (
        response.status in (401, 403)
        or any(
            marker in error_markers
            for marker in ("API_KEY_INVALID", "INVALID_API_KEY", "UNAUTHENTICATED", "PERMISSION_DENIED")
        )
        or "api key not valid" in lower_detail
        or "invalid api key" in lower_detail
    )
    if auth_error:
        return LLMAuthError(f"{label} اعتبارنامه را نپذیرفت (HTTP {response.status}){suffix}")
    if response.status == 400 and is_context_overflow(f"{detail or ''} {codes if error_markers else ''}"):
        return LLMContextOverflowError(
            f"{label} متن ورودی را بزرگ‌تر از پنجرهٔ زمینهٔ خود دانست (HTTP 400){suffix}"
        )
    if response.status in (408, 425, 429) or response.status >= 500:
        return LLMTransientError(
            f"{label} موقتاً در دسترس نیست (HTTP {response.status}){suffix}",
            retry_after_seconds=_retry_after_seconds(response.headers),
        )
    return LLMRequestError(f"{label} درخواست را رد کرد (HTTP {response.status}){suffix}")


def _build_request_for(
    route: NoteRoute, settings: Settings, system: str, user: str
) -> tuple[str, dict[str, str], dict]:
    """Build one provider's request from its own route.

    Authentication is never caller-overridable: each style sets its documented
    header itself.
    """
    style = route.style
    key = route.api_key
    model = route.model
    max_tokens = settings.note_max_output_tokens

    if style == "gemini":
        if not key:
            raise LLMAuthError("کلید سرویس تولید جزوه تنظیم نشده است.")
        # Accept the documented "models/<name>" spelling without doubling it.
        model_id = quote(model.strip().removeprefix("models/"), safe="")
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
    return _endpoint(route.base_url, "chat/completions"), headers, payload


def _build_request(
    settings: Settings, system: str, user: str, *, route: NoteRoute | None = None
) -> tuple[str, dict[str, str], dict]:
    """Build the request for one route — by default the first eligible one."""
    if route is None:
        routes = note_routes(settings)
        if not routes:
            raise LLMAuthError(
                "هیچ سرویس تولید جزوه‌ای پیکربندی نشده است (NOTE_PROVIDERS / NOTE_API_KEY / "
                "NARA_API_KEY / OPENROUTER_API_KEY)."
            )
        route = routes[0]
    return _build_request_for(route, settings, system, user)


def _parse_response(payload: dict, provider: str) -> str:
    """Normalize a provider response to plain text."""
    try:
        if provider == "gemini":
            candidates = payload.get("candidates")
            if not candidates:
                feedback = payload.get("promptFeedback")
                if isinstance(feedback, dict) and isinstance(feedback.get("blockReason"), str):
                    reason = _sanitize_error_text(feedback["blockReason"])
                    raise LLMResponseError(f"پاسخ سرویس تولید جزوه مسدود شد ({reason}).")
                raise LLMResponseError("پاسخ سرویس تولید جزوه خالی یا نامعتبر است.")
            parts = candidates[0]["content"]["parts"]
            text = "".join(str(part.get("text", "")) for part in parts)
        elif provider == "anthropic":
            text = "".join(
                str(part.get("text", ""))
                for part in payload["content"]
                if part.get("type") == "text"
            )
        else:
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                text = "".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
            else:
                text = "" if content is None else str(content)
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


async def _complete_on_route(
    route: NoteRoute,
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
) -> str:
    """One completion from one provider, with its own bounded retry budget."""
    url, headers, payload = _build_request_for(route, settings, system, user)
    attempt = 0
    while True:
        try:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=LLM_REQUEST_TIMEOUT_SECONDS),
            ) as response:
                if response.status != 200:
                    raise await _error_from_response(
                        route.label, response, route.api_key or None
                    )
                try:
                    result = await response.json(content_type=None)
                except (ValueError, TypeError) as exc:
                    raise LLMResponseError(
                        "پاسخ سرویس تولید جزوه JSON معتبری نبود."
                    ) from exc
            if not isinstance(result, dict):
                raise LLMResponseError("پاسخ سرویس تولید جزوه یک شیء JSON نبود.")
            return _parse_response(result, route.style)
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
                "LLM transient failure provider=%s (attempt %d/%d; %s); retrying in %.1fs",
                route.slug, attempt, MAX_TRANSIENT_RETRIES + 1,
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
    for route in routes:
        try:
            text = await _complete_on_route(
                route, session, settings, system=system, user=user
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
    for route in routes:
        for attempt, prompt in enumerate((user, JSON_ONLY_REMINDER + user)):
            try:
                raw = await _complete_on_route(
                    route, session, settings, system=system, user=prompt
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
