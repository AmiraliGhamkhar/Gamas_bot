"""Speech-to-text: three providers, ordered failover, word timings.

The provider order in ``STT_PROVIDERS`` *is* the failover policy. A provider is
attempted when it is configured, not in cooldown, and its direct-upload ceiling
covers the file; otherwise the next provider in the list is tried. There is no
route database, no registry and no quota platform — a provider that just failed
is simply skipped for a bounded cooldown, in memory.

Every engine returns a :class:`~gamas_bot.transcript.RawTranscript` with **word
timings whenever the engine supplies them**, because the paragraphing engine
(``transcript.py``) rebuilds readable structure from the lecturer's own pacing.
Punctuation and smart formatting are requested from the engine itself — there
is no post-hoc repair pass anywhere in v3.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

import aiohttp

from .config import (
    DEEPGRAM_MAX_UPLOAD,
    OPENAI_STT_MAX_UPLOAD,
    SPEECHMATICS_MAX_UPLOAD,
    Settings,
)
from .transcript import RawTranscript, Word

logger = logging.getLogger(__name__)


class STTError(RuntimeError):
    """Base class; the message is safe to show the user."""


class STTConfigurationError(STTError):
    """No provider could accept this job (not configured / file too large)."""


class STTAuthenticationError(STTError):
    """The provider rejected our credentials — cooldown, then failover."""


class STTTransientError(STTError):
    """Network/server failure after the bounded retry budget."""


class STTRequestError(STTError):
    """A definitive 4xx rejection (bad request, unsupported media)."""


# ---------------------------------------------------------------------------
# Cooldowns (in-memory, bounded)
# ---------------------------------------------------------------------------

_COOLDOWNS: dict[str, float] = {}

#: How long a provider is skipped after an authentication failure.
AUTH_COOLDOWN_SECONDS = 900.0
#: How long a provider is skipped after repeated server errors.
TRANSIENT_COOLDOWN_SECONDS = 60.0


def _set_cooldown(slug: str, seconds: float) -> None:
    _COOLDOWNS[slug] = time.monotonic() + max(0.0, seconds)


def _cooldown_remaining(slug: str) -> float:
    deadline = _COOLDOWNS.get(slug)
    if deadline is None:
        return 0.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _COOLDOWNS.pop(slug, None)
        return 0.0
    return remaining


def reset_cooldowns() -> None:
    """Test hook: clear every provider cooldown."""
    _COOLDOWNS.clear()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _retry_after_seconds(headers) -> float | None:
    """Parse Retry-After (seconds or HTTP date), bounded to one day."""
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


def _sanitize_error(value: object, limit: int = 180) -> str:
    """Bound a provider-supplied string so it is safe to log."""
    text = " ".join(str(value).split())
    return text[:limit]


async def _error_from_response(
    label: str, response: aiohttp.ClientResponse
) -> STTError:
    body = await response.read()
    detail = None
    try:
        import json

        payload = json.loads(body.decode("utf-8", "replace"))
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                detail = _sanitize_error(error["message"])
            elif isinstance(error, str):
                detail = _sanitize_error(error)
            elif isinstance(payload.get("message"), str):
                detail = _sanitize_error(payload["message"])
    except (ValueError, TypeError):
        pass
    suffix = f": {detail}" if detail else ""
    if response.status in (401, 403):
        return STTAuthenticationError(f"{label} اعتبارنامه را نپذیرفت (HTTP {response.status}){suffix}")
    if response.status == 429:
        return STTTransientError(f"{label} محدودیت نرخ درخواست را اعلام کرد (HTTP 429){suffix}")
    if response.status >= 500:
        return STTTransientError(f"{label} خطای سرور داد (HTTP {response.status}){suffix}")
    return STTRequestError(f"{label} درخواست را رد کرد (HTTP {response.status}){suffix}")


def _normalize_language(provider: str, language: str, *, model: str = "") -> str:
    """Translate the configured language keyword for one provider.

    ``auto`` asks for detection when the provider has it, ``multi`` selects a
    multilingual model when one exists, and anything else (a language code or a
    Speechmatics bilingual pack like ``ar_en``) passes through untouched.
    """
    value = (language or "").strip()
    key = value.lower()
    if provider == "speechmatics":
        if key in ("auto", "detect"):
            # Language detection: only the enhanced/standard tier has it.
            return "auto" if model in ("enhanced", "standard") else "fa"
        if key == "multi":
            return "fa"  # multilingual exists only for melia-1/oak-1; not assumed
        return value or "fa"
    if provider == "deepgram":
        if key in ("auto", "detect", "multi"):
            return ""  # detection/multilingual is requested by omitting language
        return value
    # OpenAI-compatible: pass the code through; whisper-1 accepts "fa"/"en"/…
    if key == "multi":
        return ""
    return value


# ---------------------------------------------------------------------------
# Speechmatics (batch API, json-v2 with word timings)
# ---------------------------------------------------------------------------


def _speechmatics_words(results: object) -> tuple[str, list[Word]]:
    """Join a json-v2 ``results`` array into text plus timed word tokens.

    Punctuation carries an ``attaches_to`` marker (``previous``/``next``/
    ``both``); honouring it is what keeps ``«سلام .`` from appearing in RTL
    text. Tokens join with a single ASCII space; nothing else is inserted.
    """
    if not isinstance(results, (list, tuple)):
        return "", []
    tokens: list[Word] = []
    pending = ""
    for item in results:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        alternatives = item.get("alternatives") or []
        if (
            item_type not in ("word", "punctuation")
            or not isinstance(alternatives, (list, tuple))
            or not alternatives
            or not isinstance(alternatives[0], dict)
        ):
            continue
        alternative = alternatives[0]
        content = str(alternative.get("content", "")).strip()
        if not content:
            continue
        start = item.get("start_time", item.get("startTime"))
        end = item.get("end_time", item.get("endTime"))
        try:
            start_value = float(start) if start is not None else None
            end_value = float(end) if end is not None else None
        except (TypeError, ValueError):
            start_value = end_value = None
        if item_type == "punctuation":
            attachment = str(alternative.get("attaches_to", "")).strip().lower()
            if attachment == "next":
                pending += content
            elif attachment == "both":
                if tokens:
                    tokens[-1] = Word(
                        tokens[-1].text + content, tokens[-1].start, tokens[-1].end
                    )
                pending += content
            else:  # previous, or unknown: closing the previous token is safest
                if tokens:
                    tokens[-1] = Word(
                        tokens[-1].text + content, tokens[-1].start, tokens[-1].end
                    )
                else:
                    pending += content
            continue
        tokens.append(Word(pending + content, start_value, end_value))
        pending = ""
    if pending and tokens:
        tokens[-1] = Word(tokens[-1].text + pending, tokens[-1].start, tokens[-1].end)
    elif pending:
        tokens.append(Word(pending))
    text = " ".join(token.text for token in tokens if token.text).strip()
    return text, tokens


async def _speechmatics(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> RawTranscript:
    import json

    language = _normalize_language(
        "speechmatics", settings.stt_language, model=settings.speechmatics_model
    )
    config = {
        "type": "transcription",
        "transcription_config": {
            "language": language,
            "model": settings.speechmatics_model,
        },
    }
    form = aiohttp.FormData()
    form.add_field("config", json.dumps(config), content_type="application/json")
    with audio_path.open("rb") as audio:
        form.add_field(
            "data_file", audio, filename=audio_path.name,
            content_type="application/octet-stream",
        )
        async with session.post(
            f"{settings.speechmatics_base_url}/jobs",
            headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
            data=form,
        ) as response:
            if response.status not in (200, 201, 202):
                raise await _error_from_response("Speechmatics", response)
            payload = await response.json(content_type=None)
    job_id = payload.get("id")
    if not job_id:
        raise STTError("Speechmatics شناسهٔ پردازش را برنگرداند.")

    deadline = time.monotonic() + settings.stt_job_timeout
    while True:
        if time.monotonic() >= deadline:
            raise STTTransientError("زمان پردازش Speechmatics به پایان رسید.")
        async with session.get(
            f"{settings.speechmatics_base_url}/jobs/{quote(str(job_id), safe='')}",
            headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
        ) as response:
            if response.status != 200:
                raise await _error_from_response("Speechmatics", response)
            status_data = await response.json(content_type=None)
        job = status_data.get("job", status_data)
        status = str(job.get("status", "")).lower()
        if status == "done":
            break
        if status in {"rejected", "failed", "deleted", "expired"}:
            raise STTRequestError(f"Speechmatics کار را با وضعیت {status} پایان داد.")
        await asyncio.sleep(
            min(settings.stt_poll_interval, max(0.1, deadline - time.monotonic()))
        )

    async with session.get(
        f"{settings.speechmatics_base_url}/jobs/{quote(str(job_id), safe='')}/transcript",
        params={"format": "json-v2"},
        headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
    ) as response:
        if response.status != 200:
            raise await _error_from_response("Speechmatics", response)
        result = await response.json(content_type=None)
    if not isinstance(result, dict):
        raise STTError("Speechmatics پاسخ متن را با ساختار قابل‌خواندن برنگرداند.")
    text, words = _speechmatics_words(result.get("results"))
    if not text:
        raise STTError("Speechmatics متن قابل‌استفاده‌ای تولید نکرد.")
    confidence = None
    duration = result.get("duration")
    if isinstance(duration, (int, float)):
        logger.info(
            "Speechmatics transcript ready job_id=%s words=%s duration=%.1fs",
            job_id, len(words), float(duration),
        )
    return RawTranscript(words=words, text=text, confidence=confidence, engine="speechmatics")


# ---------------------------------------------------------------------------
# Deepgram (pre-recorded API, words with timings + smart formatting)
# ---------------------------------------------------------------------------


def _deepgram_transcript(payload: object) -> RawTranscript:
    if not isinstance(payload, dict):
        raise STTError("Deepgram پاسخ غیرمنتظره‌ای فرستاد.")
    try:
        alternative = payload["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise STTError("Deepgram پاسخ متن را با ساختار قابل‌خواندن برنگرداند.") from exc
    text = str(alternative.get("transcript", "")).strip()
    words: list[Word] = []
    raw_words = alternative.get("words")
    if isinstance(raw_words, list):
        for item in raw_words:
            if not isinstance(item, dict):
                continue
            token = str(item.get("punctuated_word") or item.get("word") or "").strip()
            if not token:
                continue
            try:
                start = float(item["start"]) if item.get("start") is not None else None
                end = float(item["end"]) if item.get("end") is not None else None
            except (TypeError, ValueError):
                start = end = None
            words.append(Word(token, start, end))
    if not text:
        text = " ".join(word.text for word in words).strip()
    if not text:
        raise STTError("Deepgram متن قابل‌استفاده‌ای تولید نکرد.")
    confidence = alternative.get("confidence")
    try:
        confidence_value = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence_value = None
    return RawTranscript(words=words, text=text, confidence=confidence_value, engine="deepgram")


async def _deepgram(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> RawTranscript:
    language = _normalize_language(
        "deepgram", settings.stt_language, model=settings.deepgram_model
    )
    params = {
        "model": settings.deepgram_model,
        "smart_format": "true",
        "punctuate": "true",
    }
    if language:
        params["language"] = language
    with audio_path.open("rb") as audio:
        async with session.post(
            f"{settings.deepgram_base_url}/listen",
            params=params,
            headers={
                "Authorization": f"Token {settings.deepgram_api_key}",
                "Content-Type": "application/octet-stream",
            },
            data=audio,
        ) as response:
            if response.status != 200:
                raise await _error_from_response("Deepgram", response)
            payload = await response.json(content_type=None)
    return _deepgram_transcript(payload)


# ---------------------------------------------------------------------------
# OpenAI-compatible /audio/transcriptions
# ---------------------------------------------------------------------------


def _openai_transcript(payload: object) -> RawTranscript:
    """Normalize an OpenAI-compatible transcription response.

    Two shapes are understood: ``{"text": "…"}`` (plain) and verbose JSON with
    ``segments`` (per-segment text and timings, which the paragraphing engine
    uses exactly like word timings).
    """
    if not isinstance(payload, dict):
        raise STTError(
            "سرویس تبدیل گفتار سازگار با OpenAI پاسخی با ساختار غیرمنتظره فرستاد."
        )
    words: list[Word] = []
    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            try:
                start = float(segment["start"]) if segment.get("start") is not None else None
                end = float(segment["end"]) if segment.get("end") is not None else None
            except (TypeError, ValueError):
                start = end = None
            words.append(Word(text, start, end))
        text = " ".join(word.text for word in words).strip()
    else:
        text = str(payload.get("text", "")).strip()
    if not text:
        raise STTError("سرویس تبدیل گفتار سازگار با OpenAI متن قابل‌استفاده‌ای تولید نکرد.")
    return RawTranscript(words=words, text=text, confidence=None, engine="openai_compatible")


async def _openai_compatible(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> RawTranscript:
    language = _normalize_language("openai", settings.stt_language)
    form = aiohttp.FormData()
    form.add_field("model", settings.stt_openai_model)
    if language:
        form.add_field("language", language)
    form.add_field("response_format", "verbose_json")
    with audio_path.open("rb") as audio:
        form.add_field(
            "file", audio, filename=audio_path.name,
            content_type="application/octet-stream",
        )
        async with session.post(
            f"{settings.stt_openai_base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.stt_openai_api_key}"},
            data=form,
        ) as response:
            if response.status != 200:
                # Some gateways reject verbose_json; retry once with plain json.
                if response.status == 400:
                    logger.info("openai_compatible rejected verbose_json; retrying plain")
                    return await _openai_plain(session, audio_path, settings, language)
                raise await _error_from_response("سرویس تبدیل گفتار", response)
            payload = await response.json(content_type=None)
    return _openai_transcript(payload)


async def _openai_plain(
    session: aiohttp.ClientSession,
    audio_path: Path,
    settings: Settings,
    language: str,
) -> RawTranscript:
    form = aiohttp.FormData()
    form.add_field("model", settings.stt_openai_model)
    if language:
        form.add_field("language", language)
    form.add_field("response_format", "json")
    with audio_path.open("rb") as audio:
        form.add_field(
            "file", audio, filename=audio_path.name,
            content_type="application/octet-stream",
        )
        async with session.post(
            f"{settings.stt_openai_base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.stt_openai_api_key}"},
            data=form,
        ) as response:
            if response.status != 200:
                raise await _error_from_response("سرویس تبدیل گفتار", response)
            payload = await response.json(content_type=None)
    return _openai_transcript(payload)


# ---------------------------------------------------------------------------
# Providers, retries and the failover driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    slug: str
    label: str
    configured: Callable[[Settings], bool]
    max_upload: int
    run: Callable[[aiohttp.ClientSession, Path, Settings], Awaitable[RawTranscript]]


PROVIDERS: dict[str, ProviderSpec] = {
    "speechmatics": ProviderSpec(
        slug="speechmatics",
        label="Speechmatics",
        configured=lambda s: s.speechmatics_configured,
        max_upload=SPEECHMATICS_MAX_UPLOAD,
        run=_speechmatics,
    ),
    "deepgram": ProviderSpec(
        slug="deepgram",
        label="Deepgram",
        configured=lambda s: s.deepgram_configured,
        max_upload=DEEPGRAM_MAX_UPLOAD,
        run=_deepgram,
    ),
    "openai_compatible": ProviderSpec(
        slug="openai_compatible",
        label="OpenAI-compatible",
        configured=lambda s: s.stt_openai_configured,
        max_upload=OPENAI_STT_MAX_UPLOAD,
        run=_openai_compatible,
    ),
}

#: Bounded per-provider retries for transient failures.
MAX_TRANSIENT_RETRIES = 2


async def _attempt_with_retries(
    spec: ProviderSpec, session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> RawTranscript:
    attempt = 0
    while True:
        try:
            return await spec.run(session, audio_path, settings)
        except STTTransientError as exc:
            if attempt >= MAX_TRANSIENT_RETRIES:
                _set_cooldown(spec.slug, TRANSIENT_COOLDOWN_SECONDS)
                raise
            attempt += 1
            retry_after = getattr(exc, "retry_after_seconds", None)
            delay = retry_after if retry_after else min(2.0**attempt, 15.0)
            logger.warning(
                "STT provider %s transient failure (attempt %d/%d); retrying in %.1fs: %s",
                spec.slug, attempt, MAX_TRANSIENT_RETRIES + 1, delay, exc,
            )
            await asyncio.sleep(delay)
        except STTAuthenticationError:
            _set_cooldown(spec.slug, AUTH_COOLDOWN_SECONDS)
            raise


async def transcribe(
    audio_path: Path,
    settings: Settings,
    *,
    session: aiohttp.ClientSession | None = None,
) -> RawTranscript:
    """Transcribe one audio file through the configured failover chain."""
    if not settings.stt_providers:
        raise STTConfigurationError("هیچ سرویس تبدیل گفتاری پیکربندی نشده است.")
    size = audio_path.stat().st_size
    owned_session = session is None
    if session is None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_read=settings.stt_job_timeout)
        )
    try:
        failures: list[str] = []
        for slug in settings.stt_providers:
            spec = PROVIDERS.get(slug)
            if spec is None or not spec.configured(settings):
                continue
            remaining = _cooldown_remaining(slug)
            if remaining > 0:
                failures.append(f"{spec.label} در حال استراحت ({int(remaining)}s)")
                continue
            if size > spec.max_upload:
                failures.append(
                    f"{spec.label} سقف حجم {spec.max_upload // (1024**2)}MB دارد"
                )
                continue
            try:
                result = await _attempt_with_retries(spec, session, audio_path, settings)
                logger.info("STT done provider=%s bytes=%s", slug, size)
                return result
            except STTAuthenticationError as exc:
                logger.warning("STT provider %s auth failure: %s", slug, exc)
                failures.append(str(exc))
            except STTRequestError as exc:
                logger.warning("STT provider %s rejected the request: %s", slug, exc)
                failures.append(str(exc))
            except STTTransientError as exc:
                logger.warning("STT provider %s transient failure exhausted: %s", slug, exc)
                failures.append(str(exc))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("STT provider %s network failure: %s", slug, exc)
                failures.append(f"{spec.label} در دسترس نبود")
        if failures:
            raise STTError("؛ ".join(failures))
        raise STTConfigurationError(
            "هیچ سرویس تبدیل گفتاری پیکربندی نشده است "
            "(SPEECHMATICS_API_KEY، DEEPGRAM_API_KEY یا STT_OPENAI_BASE_URL+STT_OPENAI_API_KEY)."
        )
    finally:
        if owned_session:
            await session.close()
