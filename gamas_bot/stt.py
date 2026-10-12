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
import mimetypes
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote, urlsplit, urlunsplit

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
    """Network/server failure that can be retried within the bounded budget."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


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

#: Longest ``Retry-After`` that is actually waited out. Providers may ask for
#: minutes (or a date hours away) before the next attempt; a user's job must
#: fail over to the next engine long before that.
MAX_RETRY_AFTER_SECONDS = 60.0

#: Sentinel returned by :func:`_normalize_language` when the engine detects the
#: language with its own request parameter instead of a language code.
_AUTO_LANGUAGE = "auto"


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


def _has_meaningful_text(text: str) -> bool:
    """True when a transcript contains at least one letter or digit.

    A provider that answers with punctuation only (silence, music, a stream it
    could not make sense of) has not transcribed anything. Treating that as a
    success would deliver an empty ``.txt`` and settle the credit, so it is an
    error for this engine and the next provider gets the file. ``isalnum``
    covers Persian letters and Persian/Arabic-Indic digits.
    """
    return any(character.isalnum() for character in text)


async def _error_from_response(
    label: str,
    response: aiohttp.ClientResponse,
    *,
    api_key: str | None = None,
    body: bytes | None = None,
) -> STTError:
    body = await response.read() if body is None else body
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
    if detail and api_key:
        detail = detail.replace(api_key, "***")
    suffix = f": {detail}" if detail else ""
    if response.status in (401, 403):
        return STTAuthenticationError(f"{label} اعتبارنامه را نپذیرفت (HTTP {response.status}){suffix}")
    if response.status in (408, 425, 429) or response.status >= 500:
        if response.status == 429:
            message = f"{label} محدودیت نرخ درخواست را اعلام کرد (HTTP 429){suffix}"
        else:
            message = f"{label} موقتاً در دسترس نیست (HTTP {response.status}){suffix}"
        return STTTransientError(
            message,
            retry_after_seconds=_retry_after_seconds(response.headers),
        )
    return STTRequestError(f"{label} درخواست را رد کرد (HTTP {response.status}){suffix}")


async def _json_response(label: str, response: aiohttp.ClientResponse) -> object:
    """Decode a provider response without leaking a raw body on schema errors."""
    try:
        return await response.json(content_type=None)
    except (ValueError, TypeError) as exc:
        raise STTError(f"{label} پاسخ JSON معتبری برنگرداند.") from exc


def _optional_float(value: object) -> float | None:
    """Convert finite numeric timing/confidence values; ignore invalid values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _endpoint(base_url: str, suffix: str) -> str:
    """Append an API path while accepting either a base URL or full endpoint."""
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    suffix_path = "/" + suffix.strip("/")
    path = base_path if base_path.endswith(suffix_path) else base_path + suffix_path
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _audio_content_type(audio_path: Path) -> str:
    """Return an audio MIME type for raw-body providers and multipart uploads."""
    known = {
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".mp4": "audio/mp4",
        ".oga": "audio/ogg",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
    }
    extension = audio_path.suffix.lower()
    return known.get(extension) or mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"


def _normalize_language(provider: str, language: str, *, model: str = "") -> str:
    """Translate the configured language keyword for one provider.

    ``auto`` asks for detection when the provider has it, ``multi`` selects a
    multilingual model when one exists, and anything else (a language code or a
    Speechmatics bilingual pack like ``ar_en``) passes through untouched.

    Deepgram is the one engine whose detection is a separate parameter: it
    returns :data:`_AUTO_LANGUAGE` for ``auto``/``detect``, which ``_deepgram``
    turns into ``detect_language=true``. Omitting ``language`` would not detect
    anything — Deepgram defaults to English — and ``multi`` is only documented
    for its multilingual models (a set that does not include Persian, so
    ``STT_LANGUAGE=fa`` remains the correct setting for Persian audio).
    """
    value = (language or "").strip()
    key = value.lower()
    if provider == "speechmatics":
        model_key = model.strip().lower()
        if model_key in {"melia-1", "oak-1"}:
            # These models' batch contract uses the multilingual language pack.
            return "multi"
        if key in ("auto", "detect"):
            # Language detection: only the enhanced/standard tier has it.
            return "auto" if model_key in ("enhanced", "standard") else "fa"
        if key == "multi":
            return "fa"
        return value or "fa"
    if provider == "deepgram":
        if key in ("auto", "detect"):
            return _AUTO_LANGUAGE
        if key == "multi":
            return "multi"
        # Language codes keep the configured spelling: region subtags are
        # documented with uppercase regions (``en-US``, ``fa-IR``).
        return value
    # Whisper detects the language when the parameter is omitted; "auto" is
    # not a valid ISO-639-1 language code for the OpenAI transcription API.
    if key in ("auto", "detect", "multi"):
        return ""
    return value


def _provider_model(slug: str, settings: Settings) -> str:
    """The model configured for one provider (for logs; never a secret)."""
    if slug == "speechmatics":
        return settings.speechmatics_model
    if slug == "deepgram":
        return settings.deepgram_model
    return settings.stt_openai_model


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
    # The bool marks punctuation already attached to the previous token (for
    # attaches_to="both") but still needed as a prefix on the next token.
    pending: list[tuple[str, bool]] = []
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
        start_value = _optional_float(start)
        end_value = _optional_float(end)
        if item_type == "punctuation":
            attachment = str(alternative.get("attaches_to", "")).strip().lower()
            if attachment == "next":
                pending.append((content, False))
            elif attachment == "both":
                attached_to_previous = bool(tokens)
                if tokens:
                    tokens[-1] = Word(
                        tokens[-1].text + content, tokens[-1].start, tokens[-1].end
                    )
                pending.append((content, attached_to_previous))
            else:  # previous, or unknown: closing the previous token is safest
                if tokens:
                    tokens[-1] = Word(
                        tokens[-1].text + content, tokens[-1].start, tokens[-1].end
                    )
                else:
                    pending.append((content, False))
            continue
        prefix = "".join(content for content, _already_attached in pending)
        tokens.append(Word(prefix + content, start_value, end_value))
        pending.clear()
    if pending and tokens:
        # A dangling "next" mark has nowhere to go, so attach it to the last
        # word; a dangling "both" mark is already present there and must not
        # be duplicated.
        suffix = "".join(content for content, already_attached in pending if not already_attached)
        if suffix:
            tokens[-1] = Word(tokens[-1].text + suffix, tokens[-1].start, tokens[-1].end)
    elif pending:
        tokens.append(Word("".join(content for content, _already_attached in pending)))
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
                raise await _error_from_response(
                    "Speechmatics", response, api_key=settings.speechmatics_api_key
                )
            payload = await _json_response("Speechmatics", response)
    if not isinstance(payload, dict):
        raise STTError("Speechmatics پاسخ ایجاد پردازش را با ساختار قابل‌خواندن برنگرداند.")
    job_id = payload.get("id")
    if not isinstance(job_id, (str, int)) or isinstance(job_id, bool) or not str(job_id):
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
                raise await _error_from_response(
                    "Speechmatics", response, api_key=settings.speechmatics_api_key
                )
            status_data = await _json_response("Speechmatics", response)
        if not isinstance(status_data, dict):
            raise STTError("Speechmatics پاسخ وضعیت را با ساختار قابل‌خواندن برنگرداند.")
        job = status_data.get("job", status_data)
        if not isinstance(job, dict):
            raise STTError("Speechmatics پاسخ وضعیت را با ساختار قابل‌خواندن برنگرداند.")
        status = str(job.get("status", "")).lower()
        if not status:
            raise STTError("Speechmatics وضعیت پردازش را برنگرداند.")
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
            raise await _error_from_response(
                "Speechmatics", response, api_key=settings.speechmatics_api_key
            )
        result = await _json_response("Speechmatics", response)
    if not isinstance(result, dict):
        raise STTError("Speechmatics پاسخ متن را با ساختار قابل‌خواندن برنگرداند.")
    text, words = _speechmatics_words(result.get("results"))
    if not _has_meaningful_text(text):
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
        results = payload["results"]
        channels = results["channels"]
        alternatives = channels[0]["alternatives"]
        alternative = alternatives[0]
    except (KeyError, IndexError, TypeError) as exc:
        raise STTError("Deepgram پاسخ متن را با ساختار قابل‌خواندن برنگرداند.") from exc
    if not isinstance(alternative, dict):
        raise STTError("Deepgram پاسخ متن را با ساختار قابل‌خواندن برنگرداند.")
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
            start = _optional_float(item.get("start"))
            end = _optional_float(item.get("end"))
            words.append(Word(token, start, end))
    if not text:
        text = " ".join(word.text for word in words).strip()
    if not _has_meaningful_text(text):
        raise STTError("Deepgram متن قابل‌استفاده‌ای تولید نکرد.")
    confidence_value = _optional_float(alternative.get("confidence"))
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
    if language == _AUTO_LANGUAGE:
        # Deepgram detects the dominant language with its own parameter; the
        # docs state that it also picks the best model available for it.
        params["detect_language"] = "true"
    elif language:
        params["language"] = language
    with audio_path.open("rb") as audio:
        async with session.post(
            _endpoint(settings.deepgram_base_url, "listen"),
            params=params,
            headers={
                "Authorization": f"Token {settings.deepgram_api_key}",
                "Content-Type": _audio_content_type(audio_path),
            },
            data=audio,
        ) as response:
            if response.status != 200:
                raise await _error_from_response(
                    "Deepgram", response, api_key=settings.deepgram_api_key
                )
            payload = await _json_response("Deepgram", response)
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
            start = _optional_float(segment.get("start"))
            end = _optional_float(segment.get("end"))
            words.append(Word(text, start, end))
        text = " ".join(word.text for word in words).strip()
    else:
        text = str(payload.get("text", "")).strip()
    if not _has_meaningful_text(text):
        raise STTError("سرویس تبدیل گفتار سازگار با OpenAI متن قابل‌استفاده‌ای تولید نکرد.")
    return RawTranscript(words=words, text=text, confidence=None, engine="openai_compatible")


def _rejects_verbose_json(body: bytes) -> bool:
    """Identify a gateway's explicit unsupported-verbose-JSON response."""
    message = body.decode("utf-8", "replace").lower()
    mentions_format = "verbose_json" in message or "response_format" in message
    explicitly_rejected = any(
        marker in message
        for marker in ("unsupported", "not supported", "invalid", "unknown", "only")
    )
    return mentions_format and explicitly_rejected


async def _openai_compatible(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> RawTranscript:
    language = _normalize_language("openai_compatible", settings.stt_language)
    form = aiohttp.FormData()
    form.add_field("model", settings.stt_openai_model)
    if language:
        form.add_field("language", language)
    form.add_field("response_format", "verbose_json")
    retry_plain = False
    payload: object = None
    with audio_path.open("rb") as audio:
        form.add_field(
            "file", audio, filename=audio_path.name,
            content_type=_audio_content_type(audio_path),
        )
        async with session.post(
            _endpoint(settings.stt_openai_base_url, "audio/transcriptions"),
            headers={"Authorization": f"Bearer {settings.stt_openai_api_key}"},
            data=form,
        ) as response:
            if response.status != 200:
                body = await response.read()
                retry_plain = response.status == 400 and _rejects_verbose_json(body)
                if not retry_plain:
                    raise await _error_from_response(
                        "سرویس تبدیل گفتار", response,
                        api_key=settings.stt_openai_api_key, body=body,
                    )
            else:
                payload = await _json_response("سرویس تبدیل گفتار", response)
    if retry_plain:
        logger.info("openai_compatible rejected verbose_json; retrying plain")
        return await _openai_plain(session, audio_path, settings, language)
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
            content_type=_audio_content_type(audio_path),
        )
        async with session.post(
            _endpoint(settings.stt_openai_base_url, "audio/transcriptions"),
            headers={"Authorization": f"Bearer {settings.stt_openai_api_key}"},
            data=form,
        ) as response:
            if response.status != 200:
                raise await _error_from_response(
                    "سرویس تبدیل گفتار", response, api_key=settings.stt_openai_api_key
                )
            payload = await _json_response("سرویس تبدیل گفتار", response)
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
            retry_after = exc.retry_after_seconds
            if retry_after is None:
                delay = min(2.0**attempt, 15.0)
            else:
                # Respect Retry-After, but never sleep for minutes: the budget
                # is bounded and the next provider must stay reachable.
                delay = min(retry_after, MAX_RETRY_AFTER_SECONDS)
            logger.warning(
                "STT provider %s transient failure (attempt %d/%d); retrying in %.1fs: %s",
                spec.slug, attempt, MAX_TRANSIENT_RETRIES + 1, delay, exc,
            )
            await asyncio.sleep(delay)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_TRANSIENT_RETRIES:
                _set_cooldown(spec.slug, TRANSIENT_COOLDOWN_SECONDS)
                raise STTTransientError(
                    f"{spec.label} پس از چند تلاش در دسترس نبود."
                ) from exc
            attempt += 1
            delay = min(2.0**attempt, 15.0)
            logger.warning(
                "STT provider %s network failure (attempt %d/%d); retrying in %.1fs (%s)",
                spec.slug, attempt, MAX_TRANSIENT_RETRIES + 1, delay, type(exc).__name__,
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
        logger.info(
            "STT failover chain providers=%s bytes=%s language=%s",
            ",".join(settings.stt_providers), size, settings.stt_language or "auto",
        )
        for slug in settings.stt_providers:
            spec = PROVIDERS.get(slug)
            if spec is None or not spec.configured(settings):
                logger.info("STT provider %s has no credentials; skipped", slug)
                continue
            remaining = _cooldown_remaining(slug)
            if remaining > 0:
                logger.warning(
                    "STT provider %s in cooldown for %.0fs more; skipped", slug, remaining
                )
                failures.append(f"{spec.label} در حال استراحت ({int(remaining)}s)")
                continue
            max_upload = (
                settings.stt_openai_max_upload
                if slug == "openai_compatible"
                else spec.max_upload
            )
            if size > max_upload:
                limit_mb = max(1, math.ceil(max_upload / 1_000_000))
                logger.warning(
                    "STT provider %s skipped: file is %s bytes, over its %s byte ceiling",
                    slug, size, max_upload,
                )
                failures.append(f"{spec.label} سقف حجم {limit_mb}MB دارد")
                continue
            logger.info(
                "STT attempt provider=%s bytes=%s model=%s language=%s",
                slug, size, _provider_model(slug, settings), settings.stt_language or "auto",
            )
            started = time.monotonic()
            try:
                result = await _attempt_with_retries(spec, session, audio_path, settings)
                logger.info(
                    "STT done provider=%s bytes=%s elapsed_seconds=%.1f",
                    result.engine or slug, size, time.monotonic() - started,
                )
                return result
            except STTAuthenticationError as exc:
                logger.warning(
                    "STT provider %s auth failure after %.1fs: %s",
                    slug, time.monotonic() - started, exc,
                )
                failures.append(str(exc))
            except STTRequestError as exc:
                logger.warning(
                    "STT provider %s rejected the request after %.1fs: %s",
                    slug, time.monotonic() - started, exc,
                )
                failures.append(str(exc))
            except STTTransientError as exc:
                logger.warning(
                    "STT provider %s transient failure exhausted after %.1fs: %s",
                    slug, time.monotonic() - started, exc,
                )
                failures.append(str(exc))
            except STTError as exc:
                # Provider payload/schema errors are definitive for this engine,
                # but another configured engine may still transcribe the audio.
                logger.warning(
                    "STT provider %s returned an unusable response after %.1fs: %s",
                    slug, time.monotonic() - started, exc,
                )
                failures.append(str(exc))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "STT provider %s network failure after %.1fs: %s",
                    slug, time.monotonic() - started, exc,
                )
                failures.append(f"{spec.label} در دسترس نبود")
        if failures:
            logger.error(
                "STT exhausted every eligible provider (%d attempted/skipped): %s",
                len(failures), " | ".join(failures),
            )
            raise STTError("؛ ".join(failures))
        raise STTConfigurationError(
            "هیچ سرویس تبدیل گفتاری پیکربندی نشده است "
            "(SPEECHMATICS_API_KEY، DEEPGRAM_API_KEY یا STT_OPENAI_BASE_URL+STT_OPENAI_API_KEY)."
        )
    finally:
        if owned_session:
            await session.close()
