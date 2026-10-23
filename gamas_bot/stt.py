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
import json
import logging
import math
import mimetypes
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - optional fast-path
    _orjson = None  # type: ignore[assignment]

from .capacity import CapacityOverloadError, pool_for, reset_pools
from .config import (
    DEEPGRAM_MAX_UPLOAD,
    OPENAI_STT_MAX_UPLOAD,
    SPEECHMATICS_MAX_UPLOAD,
    Settings,
    first_configured_stt_provider,
)
from .http_util import (
    read_error_body,
    retry_after_seconds as _retry_after_seconds,
    sanitize_error_text as _sanitize_error,
)
from .metrics import METRICS
from .transcript import RawTranscript, Word

logger = logging.getLogger(__name__)


class STTError(RuntimeError):
    """Base class; the message is safe to show the user."""


class STTConfigurationError(STTError):
    """No provider could accept this job (not configured / file too large)."""


class STTAuthenticationError(STTError):
    """The provider rejected our credentials — cooldown, then failover."""


class STTQuotaError(STTAuthenticationError):
    """The provider reported quota or credit exhaustion — cooldown, then failover."""


class STTTransientError(STTError):
    """Network/server failure that can be retried within the bounded budget."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class STTRequestError(STTError):
    """A definitive 4xx rejection (bad request, unsupported media)."""


class STTInvalidInputError(STTRequestError):
    """Permanent input problem (empty/corrupt audio) no STT provider can salvage."""


# ---------------------------------------------------------------------------
# Cooldowns (in-memory, bounded)
# ---------------------------------------------------------------------------

_COOLDOWNS: dict[str, float] = {}

#: How long a provider is skipped after an authentication failure.
AUTH_COOLDOWN_SECONDS = 900.0
#: How long a provider is skipped after quota/credit exhaustion.
QUOTA_COOLDOWN_SECONDS = 300.0
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
    """Test hook: clear every provider cooldown and local capacity state."""
    _COOLDOWNS.clear()
    reset_pools()


_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient credits",
    "out of credits",
    "quota exceeded",
    "quota exhausted",
    "credit balance",
    "payment required",
    "billing hard limit",
)

_PERMANENT_BAD_AUDIO_MARKERS = (
    "invalid audio",
    "corrupt",
    "malformed audio",
    "unable to decode",
    "could not decode",
    "damaged",
    "empty audio",
    "no audio stream",
)


def _contains_any(text: str | None, markers: tuple[str, ...]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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
    body = await read_error_body(response) if body is None else body
    detail = None
    try:
        payload = _json_loads(body)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                detail = _sanitize_error(error["message"])
            elif isinstance(error, str):
                detail = _sanitize_error(error)
            elif isinstance(payload.get("message"), str):
                detail = _sanitize_error(payload["message"])
            elif isinstance(payload.get("err_msg"), str):
                detail = _sanitize_error(payload["err_msg"])
    except (ValueError, TypeError):
        pass
    if detail and api_key:
        detail = detail.replace(api_key, "***")
    suffix = f": {detail}" if detail else ""
    if response.status == 402 or (
        response.status in (401, 403, 429) and _contains_any(detail, _QUOTA_MARKERS)
    ):
        return STTQuotaError(
            f"سهمیه یا اعتبار {label} به پایان رسیده است (HTTP {response.status}){suffix}"
        )
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
    if response.status in (400, 415, 422) and _contains_any(
        detail, _PERMANENT_BAD_AUDIO_MARKERS
    ):
        return STTInvalidInputError(
            f"{label} فایل صوتی را نامعتبر یا آسیب‌دیده تشخیص داد (HTTP {response.status}){suffix}"
        )
    return STTRequestError(f"{label} درخواست را رد کرد (HTTP {response.status}){suffix}")


def _transfer_timeout(settings: Settings) -> aiohttp.ClientTimeout:
    """The deadline of one provider transfer, never of the job.

    ``STT_JOB_TIMEOUT`` bounds how long a transcription may take; it was also
    being used as the socket read timeout, so one stalled upload could hold a
    worker for hours. The transfer deadline is its own, much smaller knob: a
    stalled socket now fails (and the chain moves on) instead of waiting.
    """
    return aiohttp.ClientTimeout(total=settings.stt_transfer_timeout)


#: Default ceiling on one provider response body read into memory. It matches
#: the shipped ``MAX_STT_RESPONSE_BYTES``; every call site passes the configured
#: value explicitly.
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

#: Read/upload buffer size for large audio and JSON streams (matches aiohttp's
#: 256 KiB payload chunk size while reducing OS read syscalls).
AUDIO_UPLOAD_BUFFER_BYTES = 1024 * 1024
RESPONSE_READ_CHUNK_BYTES = 256 * 1024


def _json_loads(raw: bytes | str) -> object:
    """Parse JSON bytes/str with ``orjson`` when available, stdlib otherwise."""
    if _orjson is not None:
        try:
            return _orjson.loads(raw)
        except ValueError:
            if isinstance(raw, (bytes, bytearray)):
                return _orjson.loads(raw.decode("utf-8", errors="replace"))
            raise
    if isinstance(raw, (bytes, bytearray)):
        return json.loads(raw.decode("utf-8", errors="replace"))
    return json.loads(raw)


def _json_dumps_str(value: object) -> str:
    """Serialize a small config dictionary to a JSON string."""
    if _orjson is not None:
        return _orjson.dumps(value).decode("utf-8")
    return json.dumps(value)


async def _read_bounded(label: str, response, max_bytes: int) -> bytes:
    """Read a response body up to ``max_bytes``, never more (audit F-7).

    A provider answer was decoded straight into memory, so a corrupted stream,
    an error page or a hostile gateway could hand a worker an arbitrarily large
    document. The bytes are read in chunks and the read stops at the ceiling.
    """
    content = getattr(response, "content", None)
    reader = getattr(content, "read", None)
    if reader is None:
        # A stub that only exposes ``read()``: the whole body, then the check.
        body = await response.read()
        if len(body) > max_bytes:
            raise STTError(f"{label} پاسخ بزرگ‌تری از حد مجاز برگرداند.")
        return body
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await reader(min(RESPONSE_READ_CHUNK_BYTES, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise STTError(f"{label} پاسخ بزرگ‌تری از حد مجاز برگرداند.")
    return b"".join(chunks)


async def _json_response(
    label: str,
    response: aiohttp.ClientResponse,
    *,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> object:
    """Decode a bounded provider response without leaking a raw body."""
    headers = getattr(response, "headers", None) or {}
    try:
        declared = int(headers.get("Content-Length", ""))
    except (TypeError, ValueError):
        declared = None
    if declared is not None and declared > max_bytes:
        raise STTError(f"{label} پاسخ بزرگ‌تری از حد مجاز برگرداند.")
    body = await _read_bounded(label, response, max_bytes)
    try:
        return _json_loads(body)
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
    # Stage mutable [text, start, end] entries so attaching punctuation mutates
    # in place rather than discarding and reallocating frozen Word dataclasses.
    staged: list[list[object]] = []
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
        if item_type == "punctuation":
            attachment = str(alternative.get("attaches_to", "")).strip().lower()
            if attachment == "next":
                pending.append((content, False))
            elif attachment == "both":
                attached_to_previous = bool(staged)
                if staged:
                    staged[-1][0] = str(staged[-1][0]) + content
                pending.append((content, attached_to_previous))
            else:  # previous, or unknown: closing the previous token is safest
                if staged:
                    staged[-1][0] = str(staged[-1][0]) + content
                else:
                    pending.append((content, False))
            continue
        start = item.get("start_time", item.get("startTime"))
        end = item.get("end_time", item.get("endTime"))
        start_value = _optional_float(start)
        end_value = _optional_float(end)
        prefix = "".join(part for part, _already_attached in pending) if pending else ""
        staged.append([prefix + content, start_value, end_value])
        if pending:
            pending.clear()
    if pending and staged:
        # A dangling "next" mark has nowhere to go, so attach it to the last
        # word; a dangling "both" mark is already present there and must not
        # be duplicated.
        suffix = "".join(content for content, already_attached in pending if not already_attached)
        if suffix:
            staged[-1][0] = str(staged[-1][0]) + suffix
    elif pending:
        staged.append(["".join(content for content, _already_attached in pending), None, None])
    tokens = [
        Word(str(entry[0]), entry[1], entry[2])  # type: ignore[arg-type]
        for entry in staged
    ]
    text = " ".join(token.text for token in tokens if token.text).strip()
    return text, tokens


#: Terminal Speechmatics job states that are a failure, not a wait.
SPEECHMATICS_FAILURE_STATES = frozenset({"rejected", "failed", "deleted", "expired"})


def _speechmatics_wait_params(
    settings: Settings, *, output_format: str | None = None
) -> dict[str, str]:
    """Query parameters that ask Speechmatics to hold the request open.

    ``wait`` makes the endpoint long-poll until the job changes state (the API
    caps the value server-side), and on ``POST /jobs`` it additionally returns
    the finished transcript in the submit response, which removes both the
    status poll and the transcript download for a job that finishes inside the
    window. ``STT_STATUS_LONG_POLL=0`` sends nothing, restoring the plain
    request this bot sent before.
    """
    params: dict[str, str] = {}
    try:
        wait_seconds = float(getattr(settings, "stt_status_long_poll", 0.0) or 0.0)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        wait_seconds = 0.0
    if math.isfinite(wait_seconds) and wait_seconds > 0:
        params["wait"] = str(int(wait_seconds))
    if output_format:
        params["format"] = output_format
    return params


def _speechmatics_inline_transcript(payload: object) -> dict | None:
    """The transcript a Speechmatics response carries inline, when it has one.

    ``POST /jobs?format=json-v2`` embeds the finished transcript under the
    format key (the same shape as the transcript endpoint answers). The
    embedding is best-effort: a ``done`` job without the key is a normal
    outcome, and the caller falls back to the transcript ``GET``.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("json-v2", "json_v2", "transcript_document", "transcript"):
        value = payload.get(key)
        if isinstance(value, dict) and isinstance(value.get("results"), list):
            return value
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                parsed = _json_loads(value)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
                return parsed
    if isinstance(payload.get("results"), list):
        return payload
    return None


def _speechmatics_result(result: dict, job_id: str) -> RawTranscript:
    """Turn one json-v2 transcript document into a ``RawTranscript``."""
    text, words = _speechmatics_words(result.get("results"))
    if not _has_meaningful_text(text):
        raise STTError("Speechmatics متن قابل‌استفاده‌ای تولید نکرد.")
    duration = result.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        logger.info(
            "Speechmatics transcript ready job_id=%s words=%s duration=%.1fs",
            job_id, len(words), float(duration),
        )
    return RawTranscript(words=words, text=text, confidence=None, engine="speechmatics")


async def _speechmatics(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> RawTranscript:
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
    form.add_field("config", _json_dumps_str(config), content_type="application/json")
    with audio_path.open("rb", buffering=AUDIO_UPLOAD_BUFFER_BYTES) as audio:
        form.add_field(
            "data_file", audio, filename=audio_path.name,
            content_type="application/octet-stream",
        )
        async with session.post(
            f"{settings.speechmatics_base_url}/jobs",
            headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
            params=_speechmatics_wait_params(settings, output_format="json-v2"),
            data=form,
            timeout=_transfer_timeout(settings),
        ) as response:
            if response.status not in (200, 201, 202):
                raise await _error_from_response(
                    "Speechmatics", response, api_key=settings.speechmatics_api_key
                )
            payload = await _json_response(
                "Speechmatics", response, max_bytes=settings.max_stt_response_bytes
            )
    if not isinstance(payload, dict):
        raise STTError("Speechmatics پاسخ ایجاد پردازش را با ساختار قابل‌خواندن برنگرداند.")
    job_id = payload.get("id")
    if not isinstance(job_id, (str, int)) or isinstance(job_id, bool) or not str(job_id):
        raise STTError("Speechmatics شناسهٔ پردازش را برنگرداند.")

    status = str(payload.get("status", "")).strip().lower()
    if status in SPEECHMATICS_FAILURE_STATES:
        raise STTRequestError(f"Speechmatics کار را با وضعیت {status} پایان داد.")
    inline = _speechmatics_inline_transcript(payload)
    if status == "done" and inline is not None:
        # The submit call carried the whole transcript: no status poll, no
        # transcript download, one round trip for the entire job.
        logger.info("Speechmatics transcript came back on submit job_id=%s", job_id)
        return _speechmatics_result(inline, str(job_id))

    try:
        return await _speechmatics_poll(
            session,
            settings,
            str(job_id),
            submitted_at=time.monotonic(),
            first_status=status,
            inline_transcript=inline,
        )
    except BaseException:
        # A rejected, timed-out or cancelled job keeps running (and billing)
        # on the provider side; deleting it is the only way to stop that.
        await _cancel_speechmatics_job(session, settings, str(job_id))
        raise


async def _cancel_speechmatics_job(
    session: aiohttp.ClientSession, settings: Settings, job_id: str
) -> None:
    """Best-effort DELETE of an abandoned Speechmatics job.

    The client session is not always an ``aiohttp.ClientSession`` (tests and
    local gateways use a stub), so a missing ``delete`` is not an error.
    """
    delete = getattr(session, "delete", None)
    if delete is None:
        return
    try:
        async with delete(
            f"{settings.speechmatics_base_url}/jobs/{quote(job_id, safe='')}",
            headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
        ) as response:
            logger.info(
                "Speechmatics job cancelled job_id=%s status=%s", job_id, response.status
            )
    except Exception:
        logger.warning("could not cancel Speechmatics job_id=%s", job_id, exc_info=True)


async def _get_json(
    label: str,
    session: aiohttp.ClientSession,
    url: str,
    *,
    settings: Settings,
    headers: dict[str, str],
    deadline: float,
    params: dict[str, str] | None = None,
) -> object:
    """One idempotent GET with a bounded transient retry.

    A GET re-reads state; it never re-uploads the audio, so a throttled or
    dropped status read is retried in place. The submit call is *not* retried
    anywhere in the chain: repeating it means re-sending gigabytes, which is
    the failure this retry policy exists to prevent (audit F-3). Retries are
    bounded and never extend past the chain deadline.
    """
    attempt = 0
    while True:
        try:
            async with session.get(
                url,
                params=params,
                headers=headers,
                timeout=_transfer_timeout(settings),
            ) as response:
                if response.status != 200:
                    raise await _error_from_response(
                        label, response, api_key=settings.speechmatics_api_key
                    )
                return await _json_response(
                    label, response, max_bytes=settings.max_stt_response_bytes
                )
        except (STTTransientError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_TRANSIENT_RETRIES:
                raise
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            retry_after = getattr(exc, "retry_after_seconds", None)
            delay = (
                min(2.0**attempt, 15.0)
                if retry_after is None
                else min(retry_after, MAX_RETRY_AFTER_SECONDS)
            )
            delay = min(delay, remaining)
            METRICS.record_provider_event("stt", label.lower(), "retry")
            logger.warning(
                "%s read failed (attempt %d/%d); retrying in %.1fs (%s)",
                label, attempt, MAX_TRANSIENT_RETRIES + 1, delay, type(exc).__name__,
            )
            await asyncio.sleep(delay)


def _adaptive_poll_interval(base_interval: float, poll_count: int) -> float:
    """Progressive polling interval: faster initial checks, capped at *base_interval*."""
    if base_interval <= 0.5 or poll_count >= 4:
        return base_interval
    scale = 0.25 * poll_count
    return min(base_interval, max(0.5, base_interval * scale))


async def _speechmatics_poll(
    session: aiohttp.ClientSession,
    settings: Settings,
    job_id: str,
    *,
    submitted_at: float,
    first_status: str = "",
    inline_transcript: dict | None = None,
) -> RawTranscript:
    deadline = submitted_at + min(
        settings.stt_job_timeout, settings.stt_provider_timeout
    )
    poll_count = 0
    if inline_transcript is not None:
        return _speechmatics_result(inline_transcript, job_id)
    # A submit that already reported ``done`` does not need a status read: the
    # only thing left is the transcript document, which was not embedded.
    polling = first_status != "done"
    while polling:
        if time.monotonic() >= deadline:
            raise STTTransientError("زمان پردازش Speechmatics به پایان رسید.")
        status_data = await _get_json(
            "Speechmatics",
            session,
            f"{settings.speechmatics_base_url}/jobs/{quote(str(job_id), safe='')}",
            settings=settings,
            headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
            params=_speechmatics_wait_params(settings),
            deadline=deadline,
        )
        if not isinstance(status_data, dict):
            raise STTError("Speechmatics پاسخ وضعیت را با ساختار قابل‌خواندن برنگرداند.")
        job = status_data.get("job", status_data)
        if not isinstance(job, dict):
            raise STTError("Speechmatics پاسخ وضعیت را با ساختار قابل‌خواندن برنگرداند.")
        status = str(job.get("status", "")).lower()
        if not status:
            raise STTError("Speechmatics وضعیت پردازش را برنگرداند.")
        if status == "done":
            # ``first_status`` already told us the job is done; the loop only
            # runs when it did not, so an inline document here is a bonus.
            inline = _speechmatics_inline_transcript(status_data)
            if inline is not None:
                return _speechmatics_result(inline, job_id)
            break  # status GET + transcript GET is the fallback
        if status in {"rejected", "failed", "deleted", "expired"}:
            raise STTRequestError(f"Speechmatics کار را با وضعیت {status} پایان داد.")
        poll_count += 1
        await asyncio.sleep(
            min(
                _adaptive_poll_interval(settings.stt_poll_interval, poll_count),
                max(0.1, deadline - time.monotonic()),
            )
        )

    result = await _get_json(
        "Speechmatics",
        session,
        f"{settings.speechmatics_base_url}/jobs/{quote(str(job_id), safe='')}/transcript",
        settings=settings,
        headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
        params={"format": "json-v2"},
        deadline=deadline,
    )
    if not isinstance(result, dict):
        raise STTError("Speechmatics پاسخ متن را با ساختار قابل‌خواندن برنگرداند.")
    return _speechmatics_result(result, job_id)


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
    with audio_path.open("rb", buffering=AUDIO_UPLOAD_BUFFER_BYTES) as audio:
        async with session.post(
            _endpoint(settings.deepgram_base_url, "listen"),
            params=params,
            headers={
                "Authorization": f"Token {settings.deepgram_api_key}",
                "Content-Type": _audio_content_type(audio_path),
            },
            data=audio,
            timeout=_transfer_timeout(settings),
        ) as response:
            if response.status != 200:
                raise await _error_from_response(
                    "Deepgram", response, api_key=settings.deepgram_api_key
                )
            payload = await _json_response(
                "Deepgram", response, max_bytes=settings.max_stt_response_bytes
            )
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
    with audio_path.open("rb", buffering=AUDIO_UPLOAD_BUFFER_BYTES) as audio:
        form.add_field(
            "file", audio, filename=audio_path.name,
            content_type=_audio_content_type(audio_path),
        )
        async with session.post(
            _endpoint(settings.stt_openai_base_url, "audio/transcriptions"),
            headers={"Authorization": f"Bearer {settings.stt_openai_api_key}"},
            data=form,
            timeout=_transfer_timeout(settings),
        ) as response:
            if response.status != 200:
                body = await read_error_body(response)
                retry_plain = response.status == 400 and _rejects_verbose_json(body)
                if not retry_plain:
                    raise await _error_from_response(
                        "سرویس تبدیل گفتار", response,
                        api_key=settings.stt_openai_api_key, body=body,
                    )
            else:
                payload = await _json_response(
                    "سرویس تبدیل گفتار",
                    response,
                    max_bytes=settings.max_stt_response_bytes,
                )
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
    with audio_path.open("rb", buffering=AUDIO_UPLOAD_BUFFER_BYTES) as audio:
        form.add_field(
            "file", audio, filename=audio_path.name,
            content_type=_audio_content_type(audio_path),
        )
        async with session.post(
            _endpoint(settings.stt_openai_base_url, "audio/transcriptions"),
            headers={"Authorization": f"Bearer {settings.stt_openai_api_key}"},
            data=form,
            timeout=_transfer_timeout(settings),
        ) as response:
            if response.status != 200:
                raise await _error_from_response(
                    "سرویس تبدیل گفتار", response, api_key=settings.stt_openai_api_key
                )
            payload = await _json_response(
                "سرویس تبدیل گفتار", response, max_bytes=settings.max_stt_response_bytes
            )
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

def provider_upload_limit(slug: str, settings: Settings) -> int:
    """The upload ceiling of one provider, honouring the configurable one."""
    if slug == "openai_compatible":
        return settings.stt_openai_max_upload
    spec = PROVIDERS.get(slug)
    return spec.max_upload if spec is not None else settings.max_file_size


def largest_configured_upload(settings: Settings) -> int | None:
    """The biggest file any *configured* provider accepts, or ``None``.

    Media preparation runs before the failover chain: a merged WAV that no
    configured provider accepts fails the job after the download, the decode
    and the merge. Targeting the largest reachable ceiling (Opus when WAV would
    not fit) turns that late failure into a format choice that can succeed.
    """
    limits = [
        provider_upload_limit(slug, settings)
        for slug in settings.stt_providers
        if (PROVIDERS.get(slug) is not None
            and PROVIDERS[slug].configured(settings))
    ]
    return max(limits) if limits else None


def first_configured_stt_url(settings: Settings) -> str | None:
    """Base URL of the first configured STT provider, for connection pre-warming.

    Warm the host that will actually serve the job: the chosen slug comes from
    the same helper ``config.check`` uses to warn about an inverted order, so
    the pre-warm and the failover chain can never disagree.
    """
    slug = first_configured_stt_provider(settings)
    if slug == "speechmatics":
        return settings.speechmatics_base_url
    if slug == "deepgram":
        return settings.deepgram_base_url
    if slug == "openai_compatible":
        return settings.stt_openai_base_url
    return None


#: Bounded per-provider retries for transient failures. They apply to
#: *idempotent reads only* (``GET`` status/transcript calls): a transient
#: submit failure fails over to the next provider instead of re-uploading the
#: file, because repeating a multi-gigabyte POST inside one 6-hour budget is the
#: failure mode this policy exists to prevent (audit F-3).
MAX_TRANSIENT_RETRIES = 2


def provider_max_in_flight(slug: str, settings: Settings) -> int:
    """Configured local concurrency limit for *slug*."""
    if slug == "speechmatics":
        return settings.speechmatics_max_in_flight
    if slug == "deepgram":
        return settings.deepgram_max_in_flight
    if slug == "openai_compatible":
        return settings.stt_openai_max_in_flight
    return 1


async def _attempt_provider(
    spec: ProviderSpec, session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> RawTranscript:
    """One provider attempt: submit once, never re-upload on a transient error.

    The provider reads its own deadline from ``settings.stt_job_timeout``; the
    chain passes a copy narrowed to the time it has left, so every attempt in
    the chain shares one budget instead of each getting a fresh one.
    """
    attempt_deadline = min(
        float(settings.stt_job_timeout),
        float(getattr(settings, "stt_provider_timeout", settings.stt_job_timeout)),
    )
    try:
        return await asyncio.wait_for(
            spec.run(session, audio_path, settings),
            timeout=max(0.01, attempt_deadline),
        )
    except STTQuotaError:
        METRICS.record_provider_event("stt", spec.slug, "quota_exhausted")
        _set_cooldown(spec.slug, QUOTA_COOLDOWN_SECONDS)
        raise
    except STTAuthenticationError:
        METRICS.record_provider_event("stt", spec.slug, "auth_error")
        _set_cooldown(spec.slug, AUTH_COOLDOWN_SECONDS)
        raise
    except STTInvalidInputError:
        METRICS.record_provider_event("stt", spec.slug, "invalid_input")
        raise
    except STTTransientError as exc:
        if exc.retry_after_seconds is not None:
            METRICS.record_provider_event("stt", spec.slug, "rate_limit")
            cooldown = min(
                MAX_RETRY_AFTER_SECONDS,
                max(1.0, exc.retry_after_seconds),
            )
        elif "زمان" in str(exc):
            METRICS.record_provider_event("stt", spec.slug, "timeout")
            cooldown = TRANSIENT_COOLDOWN_SECONDS
        else:
            METRICS.record_provider_event("stt", spec.slug, "server_error")
            cooldown = TRANSIENT_COOLDOWN_SECONDS
        _set_cooldown(spec.slug, cooldown)
        raise
    except asyncio.TimeoutError as exc:
        METRICS.record_provider_event("stt", spec.slug, "timeout")
        _set_cooldown(spec.slug, TRANSIENT_COOLDOWN_SECONDS)
        raise STTTransientError(
            f"مهلت پاسخ سرویس {spec.label} ({int(attempt_deadline)}s) به پایان رسید."
        ) from exc
    except aiohttp.ClientError as exc:
        METRICS.record_provider_event("stt", spec.slug, "connection_error")
        _set_cooldown(spec.slug, TRANSIENT_COOLDOWN_SECONDS)
        raise STTTransientError(
            f"ارتباط شبکه‌ای با سرویس {spec.label} قطع شد: {exc}"
        ) from exc


async def transcribe(
    audio_path: Path,
    settings: Settings,
    *,
    session: aiohttp.ClientSession | None = None,
) -> RawTranscript:
    """Transcribe one audio file with non-blocking capacity reservation & failover."""
    if not settings.stt_providers:
        raise STTConfigurationError("هیچ سرویس تبدیل گفتاری پیکربندی نشده است.")
    try:
        size = audio_path.stat().st_size
    except OSError as exc:
        raise STTInvalidInputError("فایل صوتی برای تبدیل گفتار قابل‌خواندن نیست.") from exc
    if size <= 0:
        raise STTInvalidInputError("فایل صوتی خالی است و قابل تبدیل به متن نیست.")

    owned_session = session is None
    if session is None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=None,
                sock_read=settings.stt_transfer_timeout,
                sock_connect=min(60.0, settings.stt_transfer_timeout),
            )
        )
    pool = pool_for("stt", settings)
    try:
        failures: list[str] = []
        deadline = time.monotonic() + settings.stt_job_timeout
        skipped: list[str] = []
        logger.info(
            "STT failover chain providers=%s fallback_enabled=%s bytes=%s language=%s",
            ",".join(settings.stt_providers),
            settings.stt_fallback_enabled,
            size,
            settings.stt_language or "auto",
        )
        configured_specs: list[ProviderSpec] = []
        for slug in settings.stt_providers:
            spec = PROVIDERS.get(slug)
            if spec is None or not spec.configured(settings):
                logger.info("STT provider %s has no credentials; skipped", slug)
                continue
            configured_specs.append(spec)
            if not settings.stt_fallback_enabled:
                break

        if not configured_specs:
            raise STTConfigurationError(
                "هیچ سرویس تبدیل گفتاری پیکربندی نشده است "
                "(SPEECHMATICS_API_KEY، DEEPGRAM_API_KEY یا STT_OPENAI_BASE_URL+STT_OPENAI_API_KEY)."
            )

        pending_specs: list[ProviderSpec] = []
        for spec in configured_specs:
            slug = spec.slug
            remaining = _cooldown_remaining(slug)
            if remaining > 0:
                logger.warning(
                    "STT provider %s in cooldown for %.0fs more; skipped", slug, remaining
                )
                skipped.append(f"{spec.label} در حال استراحت ({int(remaining)}s)")
                continue
            max_upload = provider_upload_limit(slug, settings)
            if size > max_upload:
                limit_mb = max(1, math.ceil(max_upload / 1_000_000))
                logger.warning(
                    "STT provider %s skipped: file is %s bytes, over its %s byte ceiling",
                    slug, size, max_upload,
                )
                skipped.append(f"{spec.label} سقف حجم {limit_mb}MB دارد")
                continue
            pending_specs.append(spec)

        previous_failed_slug: str | None = None
        while pending_specs:
            remaining_budget = deadline - time.monotonic()
            if remaining_budget <= 0:
                logger.warning(
                    "STT chain deadline exhausted before provider %s (budget=%.0fs)",
                    pending_specs[0].slug, settings.stt_job_timeout,
                )
                failures.append(
                    f"مهلت زنجیرهٔ تبدیل گفتار ({int(settings.stt_job_timeout)}s) به پایان رسید"
                )
                break

            candidates = [
                (spec.slug, provider_max_in_flight(spec.slug, settings))
                for spec in pending_specs
            ]
            wait_budget = min(remaining_budget, settings.stt_slot_wait_timeout)
            try:
                acquired = await pool.acquire_any(
                    candidates,
                    timeout=wait_budget,
                    max_waiters=max(8, sum(limit for _, limit in candidates) * 8),
                )
            except CapacityOverloadError:
                acquired = None

            if acquired is None:
                labels = "، ".join(spec.label for spec in pending_specs)
                logger.warning(
                    "STT local concurrency saturated across all eligible providers: %s",
                    labels,
                )
                skipped.append(f"تکمیل ظرفیت هم‌زمان ({labels})")
                break

            chosen_slug, lease, _waited = acquired
            chosen_index = next(
                idx for idx, s in enumerate(pending_specs) if s.slug == chosen_slug
            )
            bypassed = list(pending_specs[:chosen_index])
            spec = pending_specs.pop(chosen_index)
            if bypassed:
                for saturated in bypassed:
                    METRICS.record_provider_event("stt", saturated.slug, "overflow_skip")
                    METRICS.record_failover("stt", saturated.slug, chosen_slug)
                    logger.info(
                        "STT primary %s saturated (%d/%d in flight); overflowing "
                        "immediately without wait to %s",
                        saturated.slug,
                        pool.in_flight(saturated.slug),
                        provider_max_in_flight(saturated.slug, settings),
                        chosen_slug,
                    )
            elif previous_failed_slug is not None:
                METRICS.record_failover("stt", previous_failed_slug, chosen_slug)

            remaining_budget = deadline - time.monotonic()
            attempt_settings = (
                settings
                if remaining_budget >= settings.stt_job_timeout
                else replace(settings, stt_job_timeout=max(1.0, remaining_budget))
            )
            logger.info(
                "STT attempt provider=%s bytes=%s model=%s language=%s in_flight=%d/%d",
                chosen_slug,
                size,
                _provider_model(chosen_slug, settings),
                settings.stt_language or "auto",
                pool.in_flight(chosen_slug),
                provider_max_in_flight(chosen_slug, settings),
            )
            METRICS.record_provider_acquire("stt", chosen_slug)
            started = time.monotonic()
            ok = False
            cancelled = False
            try:
                result = await _attempt_provider(
                    spec, session, audio_path, attempt_settings
                )
                ok = True
                logger.info(
                    "STT done provider=%s bytes=%s elapsed_seconds=%.1f",
                    result.engine or chosen_slug, size, time.monotonic() - started,
                )
                return result
            except asyncio.CancelledError:
                cancelled = True
                raise
            except STTInvalidInputError as exc:
                # Permanent job-specific input error: do not waste fallback
                # uploads on an unreadable or corrupted audio stream.
                logger.warning(
                    "STT permanent invalid input on %s after %.1fs (no fallback): %s",
                    chosen_slug, time.monotonic() - started, exc,
                )
                raise
            except STTAuthenticationError as exc:
                previous_failed_slug = chosen_slug
                logger.warning(
                    "STT provider %s auth/quota failure after %.1fs: %s",
                    chosen_slug, time.monotonic() - started, exc,
                )
                failures.append(str(exc))
            except STTRequestError as exc:
                previous_failed_slug = chosen_slug
                logger.warning(
                    "STT provider %s rejected the request after %.1fs: %s",
                    chosen_slug, time.monotonic() - started, exc,
                )
                failures.append(str(exc))
            except STTTransientError as exc:
                previous_failed_slug = chosen_slug
                logger.warning(
                    "STT provider %s transient failure after %.1fs; failing over "
                    "without re-uploading: %s",
                    chosen_slug, time.monotonic() - started, exc,
                )
                failures.append(str(exc))
            except STTError as exc:
                previous_failed_slug = chosen_slug
                logger.warning(
                    "STT provider %s returned an unusable response after %.1fs: %s",
                    chosen_slug, time.monotonic() - started, exc,
                )
                failures.append(str(exc))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                previous_failed_slug = chosen_slug
                logger.warning(
                    "STT provider %s network failure after %.1fs: %s",
                    chosen_slug, time.monotonic() - started, exc,
                )
                failures.append(f"{spec.label} در دسترس نبود")
            finally:
                held = lease.release()
                METRICS.record_provider_release(
                    "stt",
                    chosen_slug,
                    elapsed_seconds=held,
                    ok=ok,
                    cancelled=cancelled,
                )

        if failures:
            logger.error(
                "STT exhausted every eligible provider (%d attempted, %d skipped): %s",
                len(failures), len(skipped), " | ".join(failures + skipped),
            )
            raise STTError("؛ ".join(failures))
        if skipped:
            logger.warning("STT skipped every eligible provider: %s", " | ".join(skipped))
            raise STTTransientError(
                "سرویس تبدیل گفتار همین حالا در دسترس نیست؛ چند دقیقهٔ دیگر "
                "دوباره بفرستید. " + "؛ ".join(skipped)
            )
        raise STTConfigurationError(
            "هیچ سرویس تبدیل گفتاری پیکربندی نشده است "
            "(SPEECHMATICS_API_KEY، DEEPGRAM_API_KEY یا STT_OPENAI_BASE_URL+STT_OPENAI_API_KEY)."
        )
    finally:
        if owned_session:
            await session.close()
