from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .config import Settings

logger = logging.getLogger(__name__)


class StructuringError(RuntimeError):
    pass


ERROR_DETAIL_LIMIT = 180


PROMPT = """شما دستیار آموزشی فارسی هستید. متن پیاده‌سازی‌شده از یک کلاس یا فایل صوتی را به جزوه‌ای دقیق، خوانا و مناسب مرور تبدیل کنید.

قواعد:
- فقط بر پایهٔ متن داده‌شده بنویسید؛ اطلاعات، فرمول، تعریف یا نتیجهٔ تازه نسازید. اگر بخشی نامفهوم است، آن را حدس نزنید.
- نکته‌های کلیدی، یادآوری‌ها و هشدارهای مهم را جداگانه برجسته کنید.
- مطالب را با عنوان‌ها، زیرعنوان‌ها، فهرست و تأکید مناسب مرتب کنید.
- اگر چند مورد قابل مقایسه یا دسته‌بندی وجود دارد، از جدول سادهٔ Markdown استفاده کنید؛ جدول را بی‌دلیل به کار نبرید.
- برای فرمول‌ها و اصطلاح‌های تخصصی، صورت اصلی را حفظ کنید و متن را به فارسی روان بنویسید.
- اگر متن ناقص یا تکراری است، مفهوم موجود را مرتب کنید و چیزی به آن نیفزایید.
- پاسخ را فقط به زبان فارسی و به شکل جزوه ارائه کنید؛ مقدمهٔ گفت‌وگویی ننویسید.

متن پیاده‌سازی‌شده:
"""


def split_transcript(text: str, max_chars: int = 22000) -> list[str]:
    """Split long transcripts near sentence boundaries to fit LLM context limits."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    while len(text) > max_chars:
        boundary = max(
            text.rfind(mark, 0, max_chars)
            for mark in (". ", "؟", "!", "؟ ", "؛", "\n")
        )
        if boundary < max_chars // 2:
            boundary = text.rfind(" ", 0, max_chars)
        if boundary < max_chars // 2:
            boundary = max_chars
        else:
            boundary += 1
        pieces.append(text[:boundary].strip())
        text = text[boundary:].strip()
    if text:
        pieces.append(text)
    return pieces


PRESENTATION_PROMPT = """شما دستیار آموزشی فارسی هستید. از روی محتوای یک فایل ارائهٔ درسی (PowerPoint) — شامل متن اسلایدها، یادداشت‌های گوینده و متن پیاده‌سازی‌شدهٔ صدای ضبط‌شدهٔ همان ارائه — یک جزوهٔ دقیق، خوانا و مناسب مرور بسازید.

قواعد:
- فقط بر پایهٔ مطالب داده‌شده بنویسید؛ اطلاعات، فرمول، تعریف یا نتیجهٔ تازه نسازید. اگر بخشی نامفهوم است، آن را حدس نزنید.
- ترتیب جزوه را از ترتیب اسلایدها بگیرید و توضیح‌های صوتی را زیر همان موضوع اسلاید ادغام کنید.
- اگر صدا مطلبی فراتر از متن اسلاید دارد، آن را به‌عنوان توضیح کامل‌کننده بیاورید؛ مطالب تکراری را یک بار بنویسید.
- نکته‌های کلیدی، یادآوری‌ها و هشدارهای مهم گوینده را جداگانه برجسته کنید.
- اگر چند مورد قابل مقایسه یا دسته‌بندی وجود دارد، از جدول سادهٔ Markdown استفاده کنید؛ جدول را بی‌دلیل به کار نبرید.
- برای فرمول‌ها و اصطلاح‌های تخصصی، صورت اصلی را حفظ کنید و متن را به فارسی روان بنویسید.
- پاسخ را فقط به زبان فارسی و به شکل جزوه ارائه کنید؛ مقدمهٔ گفت‌وگویی ننویسید.

محتوای ارائه:
"""


RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


def _endpoint(base_url: str, suffix: str) -> str:
    """Append an API path without breaking an exact endpoint's query string."""
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    suffix_path = "/" + suffix.strip("/")
    path = base_path if base_path.endswith(suffix_path) else base_path + suffix_path
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _retry_delay(response: aiohttp.ClientResponse, attempt: int) -> float:
    """Respect Retry-After while keeping a bounded exponential fallback."""
    raw = response.headers.get("Retry-After", "").strip()
    if raw:
        try:
            return min(max(float(raw), 0.25), 30.0)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                return min(max(delay, 0.25), 30.0)
            except (TypeError, ValueError, OverflowError):
                pass
    return min(2 ** attempt, 15.0)


def _sanitize_error_text(value: object, key: str | None) -> str:
    """Bound and redact a provider-supplied string so it is safe to log.

    Only printable text survives, lengths are capped, and the configured API
    key is blanked if a gateway ever echoes it back.
    """
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    if key and key in text:
        text = text.replace(key, "***")
    return text[:ERROR_DETAIL_LIMIT]


def _extract_error_detail(raw_body: bytes | None, key: str | None) -> str | None:
    """Return the provider's structured diagnosis from an error body, or None.

    Only well-known metadata fields are extracted (status/type/code, the
    provider's own message, and per-detail ``reason`` codes), because all of
    the supported APIs shape errors as ``{"error": {...}}`` (Gemini's
    google.rpc shape, OpenAI-compatible, Anthropic). Raw bodies are never
    returned: gateways may answer with HTML pages or echo request fragments,
    so anything the parser does not recognise simply stays out of the logs.
    """
    if not raw_body:
        return None
    try:
        payload = json.loads(raw_body.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return None
    error = payload["error"]
    parts: list[str] = []
    for field in ("status", "type", "code", "message"):
        value = error.get(field)
        if isinstance(value, str) and value.strip():
            sanitized = _sanitize_error_text(value, key)
            if sanitized not in parts:
                parts.append(sanitized)
    details = error.get("details")
    if isinstance(details, list):
        for item in details[:3]:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reason = _sanitize_error_text(item["reason"], key)
                if reason and reason not in parts:
                    parts.append(reason)
    return "; ".join(parts) or None


def _provider_request(
    chunk: str, settings: Settings, prompt: str
) -> tuple[str, dict[str, str], dict, dict[str, str]]:
    """Build a request for Gemini, Anthropic, or an OpenAI-compatible endpoint."""
    provider = settings.note_api_provider
    key = settings.effective_note_api_key
    model = settings.effective_note_model
    extra_headers = dict(settings.note_api_extra_headers)
    full_prompt = prompt + chunk

    if provider == "gemini":
        if not key:
            raise StructuringError("کلید NOTE_API_KEY یا GEMINI_API_KEY تنظیم نشده است.")
        base = settings.note_api_base_url or "https://generativelanguage.googleapis.com/v1beta"
        # Accepting the documented "models/<name>" spelling prevents the
        # .../models/models%2F... URL that Google rejects with HTTP 400.
        model_id = quote(model.strip().removeprefix("models/"), safe="")
        url = _endpoint(base, f"models/{model_id}:generateContent")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": settings.note_api_max_output_tokens,
            },
        }
        headers = {"x-goog-api-key": key}
        headers.update(extra_headers)
        return url, headers, payload, {}

    if provider == "openai_compatible":
        base = settings.note_api_base_url or "https://api.openai.com/v1"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        headers.update(extra_headers)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "پاسخ را دقیقاً طبق دستور کاربر تولید کن."},
                {"role": "user", "content": full_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": settings.note_api_max_output_tokens,
        }
        return _endpoint(base, "chat/completions"), headers, payload, {}

    if provider == "anthropic":
        if not key:
            raise StructuringError("برای Anthropic باید NOTE_API_KEY تنظیم شود.")
        base = settings.note_api_base_url or "https://api.anthropic.com/v1"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        headers.update(extra_headers)
        payload = {
            "model": model,
            "max_tokens": settings.note_api_max_output_tokens,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": full_prompt}],
        }
        return _endpoint(base, "messages"), headers, payload, {}

    raise StructuringError("سرویس تولید جزوه غیرفعال است.")


def _provider_response(payload: dict, provider: str) -> str:
    """Normalize supported provider responses to plain text."""
    try:
        if provider == "gemini":
            candidates = payload.get("candidates")
            if not candidates:
                # Safety-blocked or empty answers return no candidates; the
                # blockReason enum makes such failures diagnosable.
                feedback = payload.get("promptFeedback")
                if isinstance(feedback, dict) and isinstance(feedback.get("blockReason"), str):
                    reason = _sanitize_error_text(feedback["blockReason"], None)
                    raise StructuringError(f"پاسخ سرویس تولید جزوه مسدود شد ({reason}).")
                raise StructuringError("پاسخ سرویس تولید جزوه خالی یا نامعتبر است.")
            if candidates[0].get("finishReason") == "MAX_TOKENS":
                raise StructuringError("خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود.")
            parts = candidates[0]["content"]["parts"]
            text = "".join(str(part.get("text", "")) for part in parts)
        elif provider == "anthropic":
            if payload.get("stop_reason") == "max_tokens":
                raise StructuringError("خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود.")
            text = "".join(
                str(part.get("text", ""))
                for part in payload["content"]
                if part.get("type") == "text"
            )
        else:
            if payload["choices"][0].get("finish_reason") == "length":
                raise StructuringError("خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود.")
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                text = "".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict)
                )
            else:
                text = "" if content is None else str(content)
    except (AttributeError, KeyError, IndexError, TypeError) as exc:
        raise StructuringError("پاسخ سرویس تولید جزوه خالی یا نامعتبر است.") from exc
    text = text.strip()
    if not text:
        raise StructuringError("پاسخ سرویس تولید جزوه خالی بود.")
    return text


async def _structure_chunk(
    chunk: str, settings: Settings, session: aiohttp.ClientSession, prompt: str = PROMPT
) -> str:
    url, headers, payload, params = _provider_request(chunk, settings, prompt)
    provider = settings.note_api_provider
    attempts = settings.note_api_retries + 1
    for attempt in range(attempts):
        try:
            async with session.post(
                url, headers=headers, params=params, json=payload
            ) as response:
                if (
                    response.status in RETRYABLE_HTTP_STATUSES
                    or 500 <= response.status < 600
                ) and attempt + 1 < attempts:
                    delay = _retry_delay(response, attempt)
                    await response.read()
                    logger.warning(
                        "Note API transient failure provider=%s status=%s retry=%s/%s delay=%.1fs",
                        provider,
                        response.status,
                        attempt + 1,
                        settings.note_api_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status < 200 or response.status >= 300:
                    # Never log raw response bodies: gateways may echo fragments
                    # of the prompt or lecture text. Only the provider's
                    # structured error metadata is extracted and logged — enough
                    # to diagnose a Gemini HTTP 400 (e.g. API_KEY_INVALID)
                    # without exposing content or the API key.
                    body = await response.read()
                    detail = _extract_error_detail(body, settings.effective_note_api_key)
                    request_id = (
                        response.headers.get("x-request-id")
                        or response.headers.get("request-id")
                        or "unknown"
                    )
                    logger.error(
                        "Note API request failed provider=%s status=%s request_id=%s detail=%s",
                        provider,
                        response.status,
                        request_id,
                        detail or "not provided",
                    )
                    message = f"سرویس {provider} خطای HTTP {response.status} داد."
                    if detail:
                        message += f" ({detail})"
                    raise StructuringError(message)
                data = await response.json(content_type=None)
                return _provider_response(data, provider)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt + 1 >= attempts:
                raise StructuringError(
                    f"ارتباط با سرویس تولید جزوه ({provider}) برقرار نشد."
                ) from None
            delay = min(2 ** attempt, 15.0)
            logger.warning(
                "Note API network failure provider=%s retry=%s/%s delay=%.1fs: %s",
                provider,
                attempt + 1,
                settings.note_api_retries,
                delay,
                type(exc).__name__,
            )
            await asyncio.sleep(delay)
    raise StructuringError("سرویس تولید جزوه پاسخی برنگرداند.")


async def structure_transcript(text: str, settings: Settings) -> str:
    """Turn a transcript into notes with the configured provider."""
    if not text.strip():
        raise StructuringError("متن پیاده‌سازی‌شده خالی است.")
    if settings.note_api_provider == "disabled":
        raise StructuringError("سرویس تولید جزوه غیرفعال است.")
    chunks = split_transcript(text)
    timeout = aiohttp.ClientTimeout(
        total=settings.note_api_timeout,
        connect=min(30, settings.note_api_timeout),
        sock_read=settings.note_api_timeout,
    )
    outputs: list[str] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index, chunk in enumerate(chunks, start=1):
            try:
                result = await _structure_chunk(chunk, settings, session)
            except Exception:
                logger.exception("Transcript structuring failed for chunk %s/%s", index, len(chunks))
                raise
            if len(chunks) > 1:
                result = f"## بخش {index}\n\n{result}"
            outputs.append(result)
    return "\n\n---\n\n".join(outputs)


def build_presentation_document(outline: str, transcript: str) -> str:
    """Combine slide text and narration transcript into one prompt payload."""
    sections: list[str] = []
    if outline.strip():
        sections.append("## متن اسلایدها\n\n" + outline.strip())
    if transcript.strip():
        sections.append("## متن پیاده‌سازی‌شدهٔ صدای ارائه\n\n" + transcript.strip())
    return "\n\n".join(sections)


async def structure_presentation(
    outline: str, transcript: str, settings: Settings, max_chars: int = 22000
) -> str:
    """Build a slide-ordered booklet from slide text plus narration transcript."""
    if not outline.strip() and not transcript.strip():
        raise StructuringError("محتوای قابل‌استفاده‌ای از فایل ارائه به دست نیامد.")
    if settings.note_api_provider == "disabled":
        raise StructuringError("سرویس تولید جزوه غیرفعال است.")

    if max_chars < 1000:
        raise ValueError("max_chars must be at least 1000 for presentation context")
    outline = outline.strip()
    if transcript.strip() and len(outline) <= max_chars // 2:
        # Repeat a short outline as context for each narration chunk.
        transcript_budget = max_chars - len(build_presentation_document(outline, "")) - 100
        documents = [
            build_presentation_document(outline, chunk)
            for chunk in split_transcript(transcript, transcript_budget)
        ]
    else:
        # Long decks (including slides-only decks) must not silently lose their
        # final slides. Chunk the complete material instead of truncating it.
        documents = split_transcript(build_presentation_document(outline, transcript), max_chars)

    timeout = aiohttp.ClientTimeout(
        total=settings.note_api_timeout,
        connect=min(30, settings.note_api_timeout),
        sock_read=settings.note_api_timeout,
    )
    outputs: list[str] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index, document in enumerate(documents, start=1):
            try:
                result = await _structure_chunk(
                    document, settings, session, PRESENTATION_PROMPT
                )
            except Exception:
                logger.exception(
                    "Presentation structuring failed for chunk %s/%s", index, len(documents)
                )
                raise
            if len(documents) > 1:
                result = f"## بخش {index}\n\n{result}"
            outputs.append(result)
    return "\n\n---\n\n".join(outputs)
