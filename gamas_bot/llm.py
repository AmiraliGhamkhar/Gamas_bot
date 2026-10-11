"""The single LLM client for note generation.

Three providers — ``openai_compatible`` (OpenAI, OpenRouter, Groq, local
gateways), ``gemini`` and ``anthropic`` — one request builder, one response
parser, one bounded retry loop. There is no model registry, no routing table
and no usage platform: the model comes from ``NOTE_MODEL`` and that is all.

Everything the note pipeline needs is :func:`complete` (plain text) and
:func:`complete_json` (strict JSON with exactly one deterministic retry).
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

from .config import Settings

logger = logging.getLogger(__name__)

#: Sampling hint for structured note generation. Low, because the note task is
#: extraction and organisation, not creative writing.
NOTE_TEMPERATURE = 0.2

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
    """The answer was malformed, blocked or empty (no retry)."""


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
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            message = payload["error"].get("message")
            if isinstance(message, str):
                detail = _sanitize_error_text(message)
    except (ValueError, TypeError):
        pass
    if detail and api_key and api_key in detail:
        detail = detail.replace(api_key, "***")
    suffix = f": {detail}" if detail else ""
    if response.status in (401, 403):
        return LLMAuthError(f"{label} اعتبارنامه را نپذیرفت (HTTP {response.status}){suffix}")
    if response.status == 429 or response.status >= 500:
        return LLMTransientError(
            f"{label} موقتاً در دسترس نیست (HTTP {response.status}){suffix}",
            retry_after_seconds=_retry_after_seconds(response.headers),
        )
    return LLMResponseError(f"{label} درخواست را رد کرد (HTTP {response.status}){suffix}")


def _build_request(
    settings: Settings, system: str, user: str
) -> tuple[str, dict[str, str], dict]:
    """Build the provider request. Authentication is never caller-overridable."""
    provider = settings.note_provider
    key = settings.note_api_key
    model = settings.note_model
    max_tokens = settings.note_max_output_tokens

    if provider == "gemini":
        if not key:
            raise LLMAuthError("کلید NOTE_API_KEY (یا GEMINI_API_KEY) تنظیم نشده است.")
        base = settings.note_api_base_url or "https://generativelanguage.googleapis.com/v1beta"
        # Accept the documented "models/<name>" spelling without doubling it.
        model_id = quote(model.strip().removeprefix("models/"), safe="")
        url = _endpoint(base, f"models/{model_id}:generateContent")
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

    if provider == "anthropic":
        if not key:
            raise LLMAuthError("برای Anthropic باید NOTE_API_KEY تنظیم شود.")
        base = settings.note_api_base_url or "https://api.anthropic.com/v1"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": NOTE_TEMPERATURE,
        }
        return _endpoint(base, "messages"), headers, payload

    # openai_compatible
    base = settings.note_api_base_url or "https://api.openai.com/v1"
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
    if settings.note_json_mode:
        # Opt-in: not every gateway implements response_format.
        payload["response_format"] = {"type": "json_object"}
    return _endpoint(base, "chat/completions"), headers, payload


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


async def complete(
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
) -> str:
    """One completion with bounded retries; returns plain text."""
    url, headers, payload = _build_request(settings, system, user)
    provider = settings.note_provider
    attempt = 0
    while True:
        try:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status != 200:
                    raise await _error_from_response(
                        "سرویس تولید جزوه", response, settings.note_api_key or None
                    )
                result = await response.json(content_type=None)
            if not isinstance(result, dict):
                raise LLMResponseError("پاسخ سرویس تولید جزوه یک شیء JSON نبود.")
            return _parse_response(result, provider)
        except LLMTransientError as exc:
            if attempt >= MAX_TRANSIENT_RETRIES:
                raise
            attempt += 1
            delay = exc.retry_after_seconds or min(2.0**attempt, 15.0)
            logger.warning(
                "LLM transient failure (attempt %d/%d); retrying in %.1fs",
                attempt, MAX_TRANSIENT_RETRIES + 1, delay,
            )
            await asyncio.sleep(delay)


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


async def complete_json(
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    system: str,
    user: str,
) -> dict:
    """One completion parsed as strict JSON, with exactly one retry.

    The retry prepends a minimal "valid JSON only" reminder; anything still
    unparsable raises :class:`LLMResponseError` and the caller degrades to the
    raw transcript (which the user has already received).
    """
    try:
        raw = await complete(session, settings, system=system, user=user)
        return json.loads(extract_json_object(raw))
    except (LLMResponseError, ValueError) as first_failure:
        logger.warning("JSON parse failed (%s); retrying once", type(first_failure).__name__)
    raw = await complete(
        session,
        settings,
        system=system,
        user=(
            "پاسخ قبلی قابل تجزیه نبود. فقط و فقط یک شیء JSON معتبر و کامل "
            "بدون هیچ متن یا توضیح اضافه برگردانید.\n\n" + user
        ),
    )
    return json.loads(extract_json_object(raw))
