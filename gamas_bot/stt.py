"""Speech-to-text routing for Speechmatics, Deepgram and OpenAI-compatible APIs.

The module keeps three concerns strictly apart, because the providers do not
agree on any of them:

* **request shape** — each provider gets its own serializer (``model`` for
  Speechmatics, query parameters for Deepgram, multipart form fields for
  OpenAI-compatible gateways);
* **language semantics** — ``fa``, ``en-US``, ``auto`` and ``multi`` mean
  different things to different engines, so the requested language is
  translated per provider by :func:`normalize_language_for_provider`;
* **failure semantics** — :class:`STTTransientError` marks a failure that is
  worth retrying (429/5xx/network), while a plain :class:`STTError` is
  permanent and moves straight on to the next configured engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from .config import SPEECHMATICS_MULTILINGUAL_MODELS, Settings
from .provider_credentials import ProviderCredentialManager
from .stt_platform.adapters import (
    AdapterOptions,
    NativeSTTAdapter,
    ProviderSTTError,
    STTErrorCategory,
    get_audio_duration_seconds,
    resolve_language,
    vocabulary_hints,
)
from .stt_platform.events import STTEventLogger
from .stt_platform.models import Transcript as PlatformTranscript
from .stt_platform.models import VocabularyHints
from .stt_platform.policy import SttPolicy
from .stt_platform.profiles import ProviderConcurrencyRegistry
from .stt_platform.quality import TranscriptQualityGate, hard_quality_failures
from .stt_platform.quota import STTQuotaTracker
from .stt_platform.registry import LEGACY_STT_PROVIDERS, STT_PROVIDER_REGISTRY
from .stt_platform.router import (
    CandidateFacts,
    RoutePlan,
    SttRequirements,
    plan_route,
    resolve_route,
)
from .structuring import _endpoint

logger = logging.getLogger(__name__)


class STTError(RuntimeError):
    """A transcription failure that is safe to report and not worth retrying."""


class STTConfigurationError(STTError):
    """The requested combination is invalid for the provider's API.

    Raised before any HTTP request is made: retrying or falling back to
    another engine cannot fix an unsupported language/model pair, and the same
    configuration error would simply be reproduced.
    """


class STTTransientError(STTError):
    """A temporary provider failure (429/5xx/timeouts) worth another attempt."""

    def __init__(
        self, message: str, retry_after: float | None = None, *, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


class STTAuthenticationError(STTError):
    """401/403 response; the affected credential is quarantined and rotated."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class STTRequestError(STTError):
    """Permanent invalid-request response; it must not rotate credentials."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


#: HTTP statuses a well-behaved provider uses for *temporary* trouble.
#: Everything else in the 4xx range is a request we must not repeat as-is.
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Speechmatics result types the transcript builder understands. Unknown types
#: (e.g. ``entity`` objects) are skipped rather than silently mis-rendered.
_SPEECHMATICS_TOKEN_TYPES = frozenset({"word", "punctuation"})

#: Punctuation attachment: Speechmatics reports which side a mark belongs to.
#: A missing or unrecognised value degrades to ``previous`` — the historical
#: behaviour, and the only safe default for RTL text.
_PUNCTUATION_ATTACH_PREVIOUS = "previous"
_PUNCTUATION_ATTACH_NEXT = "next"
_PUNCTUATION_ATTACH_BOTH = "both"

#: Deepgram models that accept ``language=multi``. The multilingual set is
#: *smaller* than each model's monolingual coverage: for Nova-3 it is
#: en, es, fr, de, hi, ru, pt, ja, it and nl — Persian is not part of it.
#: https://developers.deepgram.com/docs/models-languages-overview
DEEPGRAM_MULTILINGUAL_MODELS = frozenset(
    {"nova-2", "nova-3", "nova-2-general", "nova-3-general", "flux-general-multi"}
)
DEEPGRAM_MULTILINGUAL_LANGUAGES = (
    "en",
    "es",
    "fr",
    "de",
    "hi",
    "ru",
    "pt",
    "ja",
    "it",
    "nl",
)


@dataclass(frozen=True, slots=True)
class Transcript:
    engine: str
    text: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class STTProvider:
    """One speech-to-text engine and the metadata the router needs.

    ``availability`` returns a truthy marker only when the engine is actually
    configured (an API key, or a base URL for keyless local gateways).
    ``attempt`` runs one bounded provider attempt. ``max_upload`` is the
    largest file the engine accepts in a single request: larger files are
    routed to another configured engine instead of being split, because
    chunked audio loses word context at every boundary.
    """

    availability: Callable[[Settings], str | None]
    attempt: Callable[[aiohttp.ClientSession, Path, Settings], Awaitable[Transcript]]
    label: str
    max_upload: Callable[[Settings], int]


def _is_keyword(language: str, keyword: str) -> bool:
    return language.strip().lower() == keyword


def _is_speechmatics_pack(language: str) -> bool:
    """True for a Speechmatics bilingual/multilingual pack (``ar_en``, ...).

    Packs are a Speechmatics concept: every other provider expects one language
    (or its own multilingual mode), so a pack must not be forwarded to them.
    https://docs.speechmatics.com/speech-to-text/languages
    """
    return bool(re.fullmatch(r"[a-z]{2,3}(?:_[a-z]{2,3}){1,3}", language.strip().lower()))


def normalize_language_for_provider(
    provider: str, language: str, *, model: str | None = None
) -> str | None:
    """Translate the requested ``STT_LANGUAGE`` into one provider's parameter.

    Returns the value to send, or ``None`` when the provider should be left to
    detect the language itself (the parameter is then omitted).

    The four concepts the configuration can express are deliberately kept
    apart: an explicit language, ``auto`` (automatic language *detection*),
    ``multi`` (a multilingual model that switches language by itself) and the
    provider's own parameter. They are not interchangeable, so an unsupported
    combination raises :class:`STTConfigurationError` instead of being sent and
    silently mis-transcribed.
    """
    requested = (language or "").strip()
    if not requested:
        raise STTConfigurationError("زبان تبدیل گفتار (STT_LANGUAGE) تنظیم نشده است.")
    selected_model = (model or "").strip().lower()

    if provider == "speechmatics":
        multilingual = selected_model in SPEECHMATICS_MULTILINGUAL_MODELS
        if _is_keyword(requested, "auto"):
            # Language Identification: Batch SaaS, enhanced/standard only.
            # melia-1/oak-1 reject `auto` and use `multi` instead.
            if multilingual:
                raise STTConfigurationError(
                    "مدل چندزبانهٔ Speechmatics مقدار auto را نمی‌پذیرد؛ "
                    "STT_LANGUAGE را روی multi بگذارید."
                )
            return "auto"
        if _is_keyword(requested, "multi"):
            if not multilingual:
                raise STTConfigurationError(
                    "مقدار multi فقط برای مدل‌های چندزبانهٔ Speechmatics "
                    "(melia-1 یا oak-1) معتبر است."
                )
            return "multi"
        if _is_speechmatics_pack(requested) and multilingual:
            # Multilingual models do not take a pack: they use `multi` and,
            # optionally, language_hints.
            raise STTConfigurationError(
                "مدل‌های چندزبانهٔ Speechmatics بستهٔ زبانی نمی‌پذیرند؛ "
                "STT_LANGUAGE را روی multi بگذارید."
            )
        return requested

    if provider == "deepgram":
        # Deepgram has no `language=auto`: detection is the separate
        # `detect_language` flag, and its supported set does not include
        # Persian, so `auto` is refused rather than silently mis-detected.
        if _is_keyword(requested, "auto"):
            raise STTConfigurationError(
                "دیپ‌گرام مقدار auto برای زبان ندارد؛ یک کد مشخص (مثل fa یا en) "
                "یا multi برای مدل‌های چندزبانه انتخاب کنید."
            )
        if _is_keyword(requested, "multi"):
            if selected_model and selected_model not in DEEPGRAM_MULTILINGUAL_MODELS:
                raise STTConfigurationError(
                    f"مدل {selected_model} دیپ‌گرام حالت چندزبانه (multi) ندارد."
                )
            logger.warning(
                "Deepgram multilingual mode language=multi model=%s covers only %s; "
                "languages outside that set (including Persian/fa) are not transcribed "
                "in this mode",
                selected_model or "default",
                ", ".join(DEEPGRAM_MULTILINGUAL_LANGUAGES),
            )
            return "multi"
        if _is_speechmatics_pack(requested):
            raise STTConfigurationError(
                f"بستهٔ زبانی {requested} فقط برای Speechmatics تعریف شده است؛ "
                "برای دیپ‌گرام یک کد زبان مشخص (مثل fa یا en) انتخاب کنید."
            )
        # A specific language restricts recognition to it: speech in any other
        # language is not transcribed (Deepgram "language" documentation).
        return requested

    if provider == "openai_compatible":
        if _is_keyword(requested, "auto"):
            # Whisper-style APIs auto-detect when the field is absent; there is
            # no `auto` value in the schema.
            return None
        if _is_keyword(requested, "multi"):
            raise STTConfigurationError(
                "مقدار multi برای سرویس‌های سازگار با OpenAI تعریف نشده است؛ "
                "برای تشخیص خودکار زبان از auto استفاده کنید."
            )
        if _is_speechmatics_pack(requested):
            raise STTConfigurationError(
                f"بستهٔ زبانی {requested} فقط برای Speechmatics تعریف شده است؛ "
                "برای این سرویس یک کد زبان مشخص (مثل fa یا en) انتخاب کنید."
            )
        return requested

    raise STTConfigurationError(f"سرویس تبدیل گفتار ناشناخته است: {provider}")


def speechmatics_vocab(settings: Settings) -> list[dict[str, str]]:
    """The ``additional_vocab`` payload for one job, bounded by the provider.

    The provider documents 1000 words/phrases per job as the recommended
    maximum and *rejects* a job above 20000 entries. The configured order is
    the priority order, so an oversized dictionary keeps its highest-value
    prefix (drug names and English medical terms first) instead of an
    arbitrary subset. Nothing is re-ranked here: choosing the order is the
    operator's job, silently inventing one is not.
    """
    terms = settings.speechmatics_additional_vocab
    if not terms or not settings.speechmatics_vocab_supported:
        return []
    limit = max(1, settings.speechmatics_vocab_max_items)
    selected = list(terms[:limit])
    if len(terms) > len(selected):
        logger.warning(
            "Speechmatics custom dictionary truncated terms=%s sent=%s limit=%s",
            len(terms),
            len(selected),
            limit,
        )
    return [{"content": term} for term in selected]


def speechmatics_config(settings: Settings) -> dict:
    """Job configuration for the Speechmatics batch API.

    The provider's documented field for the model selection is ``model``
    (``enhanced`` — the highest-accuracy tier this accuracy-first pipeline
    defaults to — ``standard``, ``melia-1`` or ``oak-1``). ``operating_point``
    is the *deprecated* spelling of the same field, kept only for older
    self-hosted containers; ``SPEECHMATICS_MODEL_FIELD`` selects it when a
    deployment needs it, and ``both`` sends the two spellings together.

    ``additional_vocab`` is the provider's native custom dictionary: exact
    terms for drug names and English technical vocabulary, with no LLM or
    post-processing layer involved. It is only sent to models that support it.
    """
    model = settings.speechmatics_operating_point
    language = normalize_language_for_provider(
        "speechmatics", settings.stt_language, model=model
    )
    transcription_config: dict[str, object] = {"language": language}
    # One canonical field by default; the deprecated alias only on request.
    field = settings.speechmatics_model_field
    if field in {"model", "both"}:
        transcription_config["model"] = model
    if field in {"operating_point", "both"}:
        transcription_config["operating_point"] = model
    vocab = speechmatics_vocab(settings)
    if vocab:
        transcription_config["additional_vocab"] = vocab
    return {
        "type": "transcription",
        "transcription_config": transcription_config,
    }


def deepgram_params(settings: Settings) -> dict[str, str]:
    """Query parameters for the Deepgram pre-recorded API.

    ``language=fa`` (the default) asks for *Persian only*: Deepgram does not
    transcribe speech in another language while a specific language is set, so
    this is monolingual Persian recognition, not Persian+English
    code-switching. ``language=multi`` is accepted only for models that
    document a multilingual mode, and that mode does not cover Persian.
    """
    language = normalize_language_for_provider(
        "deepgram", settings.stt_language, model=settings.deepgram_model
    )
    params = {
        "model": settings.deepgram_model,
        "smart_format": "true",
        "punctuate": "true",
    }
    if language:
        params["language"] = language
    return params


def _speechmatics_confidence(payload: dict) -> float | None:
    values = []
    results = payload.get("results", [])
    if not isinstance(results, (list, tuple)):
        return None
    for item in results:
        if not isinstance(item, dict):
            continue
        alternatives = item.get("alternatives") or []
        if not isinstance(alternatives, (list, tuple)) or not alternatives:
            continue
        alternative = alternatives[0]
        if not isinstance(alternative, dict) or alternative.get("confidence") is None:
            continue
        try:
            value = float(alternative["confidence"])
            if math.isfinite(value) and 0 <= value <= 1:
                values.append(value)
        except (TypeError, ValueError):
            pass
    return sum(values) / len(values) if values else None


def _deepgram_transcript(payload: dict) -> Transcript:
    try:
        alternative = payload["results"]["channels"][0]["alternatives"][0]
        text = alternative.get("transcript", "").strip()
        confidence = alternative.get("confidence")
        confidence = float(confidence) if confidence is not None else None
        if confidence is not None and (not math.isfinite(confidence) or not 0 <= confidence <= 1):
            confidence = None
        return Transcript("deepgram", text, confidence)
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise STTError("ساختار پاسخ دیپ‌گرام قابل‌خواندن نیست.") from exc


def _retry_after_seconds(response: aiohttp.ClientResponse | None) -> float | None:
    """Parse ``Retry-After`` (seconds or HTTP date) when the provider sends it."""
    if response is None:
        return None
    raw = (response.headers or {}).get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        if math.isfinite(seconds):
            return min(max(seconds, 0.0), 604_800.0)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return min(max(delay, 0.0), 604_800.0)


def _http_error(
    stage: str,
    status: int,
    response: aiohttp.ClientResponse | None = None,
) -> STTError:
    """Turn an HTTP status into a retryable or permanent STT error.

    Provider/gateway error bodies may echo lecture content or credentials, so
    only the status (and a sanitized stage name) is reported.
    """
    message = f"{stage} failed (HTTP {status})"
    if status in RETRYABLE_HTTP_STATUSES:
        return STTTransientError(
            message, retry_after=_retry_after_seconds(response), status=status
        )
    if status in {401, 403}:
        return STTAuthenticationError(message, status=status)
    return STTRequestError(message, status=status)


def _transcript_metrics(text: str) -> tuple[int, int]:
    """(characters, words) for logging — never the transcript content itself."""
    return len(text), len(text.split())


def _log_transcript_stats(engine: str, text: str, confidence: float | None, **extra: object) -> None:
    """Log transcript size/quality metrics without any transcript content."""
    chars, words = _transcript_metrics(text)
    logger.info(
        "STT transcript stats engine=%s text_chars=%s text_words=%s confidence=%s%s",
        engine,
        chars,
        words,
        f"{confidence:.3f}" if confidence is not None else "unavailable",
        "".join(f" {key}={value}" for key, value in extra.items()),
    )


def _speechmatics_text(results: object) -> str:
    """Join a json-v2 ``results`` array into readable text.

    Punctuation carries an ``attaches_to`` marker (``previous``, ``next`` or
    ``both``) that says *which* token the mark belongs to. Appending every
    mark to the previous word — the historical behaviour — produced
    ``« سلام .`` in RTL text whenever Speechmatics reported ``next``, so the
    marker is honoured:

    * ``previous`` (and any missing/unknown value) closes the previous token;
    * ``next`` is buffered and prefixed to the following token;
    * ``both`` closes the previous token *and* opens the next one.

    Tokens are joined with a single ASCII space and nothing else is inserted,
    so no bidi control characters or zero-width marks are introduced.
    """
    if not isinstance(results, (list, tuple)):
        return ""
    tokens: list[str] = []
    pending: list[str] = []  # marks that open the next token
    for item in results:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        alternatives = item.get("alternatives") or []
        if (
            item_type not in _SPEECHMATICS_TOKEN_TYPES
            or not isinstance(alternatives, (list, tuple))
            or not alternatives
        ):
            continue
        alternative = alternatives[0]
        if not isinstance(alternative, dict):
            continue
        content = str(alternative.get("content", ""))
        if not content:
            continue
        if item_type == "punctuation":
            attachment = str(alternative.get("attaches_to") or "").strip().lower()
            if attachment == _PUNCTUATION_ATTACH_NEXT:
                pending.append(content)
            elif attachment == _PUNCTUATION_ATTACH_BOTH:
                if tokens:
                    tokens[-1] += content
                pending.append(content)
            else:
                # ``previous`` plus the unknown/missing case: closing the
                # previous token is the only safe default for RTL text.
                if tokens:
                    tokens[-1] += content
                else:
                    pending.append(content)
            continue
        tokens.append("".join(pending) + content)
        pending.clear()
    if pending:
        # Trailing marks with no token left to open.
        trailing = "".join(pending)
        if tokens:
            tokens[-1] += trailing
        else:
            tokens.append(trailing)
    return " ".join(token for token in tokens if token).strip()


async def _speechmatics(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> Transcript:
    assert settings.speechmatics_api_key
    started_at = time.monotonic()
    config = speechmatics_config(settings)
    form = aiohttp.FormData()
    form.add_field("config", json.dumps(config), content_type="application/json")
    with audio_path.open("rb") as audio:
        form.add_field(
            "data_file",
            audio,
            filename=audio_path.name,
            content_type="application/octet-stream",
        )
        async with session.post(
            f"{settings.speechmatics_base_url}/jobs",
            headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
            data=form,
        ) as response:
            if response.status not in (200, 201, 202):
                raise _http_error("Speechmatics job submission", response.status, response)
            payload = await response.json(content_type=None)
    job_id = payload.get("id")
    if not job_id:
        raise STTError("Speechmatics شناسهٔ پردازش را برنگرداند.")
    upload_elapsed = time.monotonic() - started_at
    logger.info(
        "Speechmatics job submitted job_id=%s upload_elapsed_seconds=%.3f",
        job_id,
        upload_elapsed,
    )

    deadline = time.monotonic() + settings.stt_job_timeout
    polls = 0
    while True:
        if time.monotonic() >= deadline:
            logger.warning(
                "Speechmatics job timed out job_id=%s polls=%s", job_id, polls
            )
            raise STTError("زمان پردازش Speechmatics به پایان رسید.")
        polls += 1
        async with session.get(
            f"{settings.speechmatics_base_url}/jobs/{quote(str(job_id), safe='')}",
            headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
        ) as response:
            if response.status != 200:
                raise _http_error("Speechmatics status", response.status, response)
            status_data = await response.json(content_type=None)
        job = status_data.get("job", status_data)
        status = str(job.get("status", "")).lower()
        logger.debug(
            "Speechmatics job poll job_id=%s poll=%s status=%s elapsed_seconds=%.1f",
            job_id,
            polls,
            status,
            time.monotonic() - started_at,
        )
        if status == "done":
            break
        if status in {"rejected", "failed", "deleted", "expired"}:
            logger.warning(
                "Speechmatics job ended unsuccessfully job_id=%s polls=%s final_status=%s elapsed_seconds=%.1f",
                job_id,
                polls,
                status,
                time.monotonic() - started_at,
            )
            raise STTError(f"Speechmatics کار را با وضعیت {status} پایان داد.")
        await asyncio.sleep(min(settings.stt_poll_interval, max(0.1, deadline - time.monotonic())))
    logger.info(
        "Speechmatics job finished job_id=%s polls=%s elapsed_seconds=%.1f",
        job_id,
        polls,
        time.monotonic() - started_at,
    )

    transcript_started = time.monotonic()
    async with session.get(
        f"{settings.speechmatics_base_url}/jobs/{quote(str(job_id), safe='')}/transcript",
        params={"format": "json-v2"},
        headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
    ) as response:
        if response.status != 200:
            raise _http_error(
                "Speechmatics transcript retrieval", response.status, response
            )
        result = await response.json(content_type=None)
    if not isinstance(result, dict):
        raise STTError("Speechmatics پاسخ متن را با ساختار قابل‌خواندن برنگرداند.")
    results = result.get("results", [])
    if not isinstance(results, (list, tuple)):
        raise STTError("Speechmatics پاسخ متن را با ساختار قابل‌خواندن برنگرداند.")
    logger.info(
        "Speechmatics transcript downloaded job_id=%s download_elapsed_seconds=%.3f result_items=%s",
        job_id,
        time.monotonic() - transcript_started,
        len(results),
    )

    text = _speechmatics_text(results)
    if not text:
        # Some API response versions expose the text in results without a type.
        fallback_parts = []
        for item in results:
            if not isinstance(item, dict):
                continue
            alternatives = item.get("alternatives") or []
            if not isinstance(alternatives, (list, tuple)) or not alternatives:
                continue
            alternative = alternatives[0]
            if isinstance(alternative, dict) and alternative.get("content"):
                fallback_parts.append(str(alternative["content"]))
        text = " ".join(fallback_parts).strip()
    if not text:
        raise STTError("Speechmatics متن قابل‌استفاده‌ای تولید نکرد.")
    confidence = _speechmatics_confidence(result)
    _log_transcript_stats("speechmatics", text, confidence)
    return Transcript("speechmatics", text, confidence)


async def _deepgram(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> Transcript:
    assert settings.deepgram_api_key
    started_at = time.monotonic()
    params = deepgram_params(settings)
    with audio_path.open("rb") as audio:
        async with session.post(
            "https://api.deepgram.com/v1/listen",
            params=params,
            headers={
                "Authorization": f"Token {settings.deepgram_api_key}",
                "Content-Type": "application/octet-stream",
            },
            data=audio,
        ) as response:
            if response.status != 200:
                raise _http_error("Deepgram request", response.status, response)
            payload = await response.json(content_type=None)
    logger.info(
        "Deepgram request completed model=%s upload_bytes=%s elapsed_seconds=%.3f",
        settings.deepgram_model,
        audio_path.stat().st_size,
        time.monotonic() - started_at,
    )
    transcript = _deepgram_transcript(payload)
    if not transcript.text:
        raise STTError("Deepgram متن قابل‌استفاده‌ای تولید نکرد.")
    _log_transcript_stats("deepgram", transcript.text, transcript.confidence)
    return transcript


def _openai_transcript(payload: object) -> Transcript:
    """Normalize an OpenAI-compatible /audio/transcriptions response.

    "OpenAI-compatible" here means exactly one documented shape —
    ``{"text": "..."}`` — not every server that borrows the route. A response
    without a usable ``text`` field is a schema mismatch and is reported as
    one instead of surfacing a ``KeyError`` from deep inside the parser.
    """
    if not isinstance(payload, dict):
        raise STTError(
            "سرویس تبدیل گفتار سازگار با OpenAI پاسخی با ساختار غیرمنتظره فرستاد "
            "(یک JSON object با فیلد text لازم است)."
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise STTError(
            "سرویس تبدیل گفتار سازگار با OpenAI متن قابل‌استفاده‌ای تولید نکرد "
            "(فیلد text خالی یا از نوع نامعتبر بود)."
        )
    # Whisper-style APIs return no per-word confidence, so the router treats
    # this engine like any other un-scored provider.
    return Transcript("openai_compatible", text.strip(), None)


async def _openai_compatible_stt(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> Transcript:
    """POST to an OpenAI-compatible STT endpoint (OpenAI, Groq, vLLM, ...)."""
    assert settings.stt_openai_base_url
    started_at = time.monotonic()
    # ``auto`` is not an OpenAI schema value: the field is omitted so the
    # gateway detects the language itself (see normalize_language_for_provider).
    language = normalize_language_for_provider("openai_compatible", settings.stt_language)
    form = aiohttp.FormData()
    form.add_field("model", settings.stt_openai_model)
    if language:
        form.add_field("language", language)
    with audio_path.open("rb") as audio:
        form.add_field(
            "file",
            audio,
            filename=audio_path.name,
            content_type="application/octet-stream",
        )
        headers = (
            {"Authorization": f"Bearer {settings.stt_openai_api_key}"}
            if settings.stt_openai_api_key
            else {}
        )
        async with session.post(
            _endpoint(settings.stt_openai_base_url, "audio/transcriptions"),
            headers=headers,
            data=form,
        ) as response:
            if response.status != 200:
                raise _http_error("OpenAI-compatible STT request", response.status, response)
            payload = await response.json(content_type=None)
    logger.info(
        "OpenAI-compatible STT request completed model=%s upload_bytes=%s elapsed_seconds=%.3f",
        settings.stt_openai_model,
        audio_path.stat().st_size,
        time.monotonic() - started_at,
    )
    transcript = _openai_transcript(payload)
    _log_transcript_stats("openai_compatible", transcript.text, transcript.confidence)
    return transcript


def _openai_stt_availability(settings: Settings) -> str | None:
    """A base URL (key optional, e.g. a local vLLM gateway) makes it usable."""
    return "configured" if settings.stt_openai_base_url else None


# Provider registry. Order matters: it defines both the fallback chain and
# the benchmark order. The lambdas resolve the module-level callables lazily
# so tests can keep patching gamas_bot.stt._<provider>.
STT_PROVIDERS: dict[str, STTProvider] = {
    "speechmatics": STTProvider(
        availability=lambda settings: settings.speechmatics_api_key,
        attempt=lambda session, audio, settings: _speechmatics(session, audio, settings),
        label="Speechmatics",
        # Speechmatics Batch SaaS rejects direct multipart uploads at 1 GB.
        max_upload=lambda settings: 1_000_000_000,
    ),
    "deepgram": STTProvider(
        availability=lambda settings: settings.deepgram_api_key,
        attempt=lambda session, audio, settings: _deepgram(session, audio, settings),
        label="Deepgram",
        # Deepgram supports direct pre-recorded uploads up to 2 GB.
        max_upload=lambda settings: 2_000_000_000,
    ),
    "openai_compatible": STTProvider(
        availability=_openai_stt_availability,
        attempt=lambda session, audio, settings: _openai_compatible_stt(
            session, audio, settings
        ),
        label="OpenAI-compatible STT",
        # Whisper-style gateways reject large requests (e.g. 25 MB), so the cap
        # is configurable; local servers can raise it.
        max_upload=lambda settings: settings.stt_openai_max_upload,
    ),
}


def _retry_delay_seconds(attempt: int, settings: Settings, retry_after: float | None) -> float:
    """Bounded exponential backoff, honouring a provider's ``Retry-After``."""
    if retry_after is not None:
        return min(max(retry_after, 0.0), settings.stt_retry_max_delay)
    return min(
        settings.stt_retry_base_delay * (2 ** max(0, attempt - 1)),
        settings.stt_retry_max_delay,
    )


async def _attempt_with_retries(
    engine: str,
    session: aiohttp.ClientSession,
    audio_path: Path,
    settings: Settings,
    *,
    retry_rate_limit: bool = True,
) -> Transcript:
    """Run one provider attempt, retrying only *transient* failures.

    Retryable: HTTP 408/425/429/5xx, connection resets, socket and total
    timeouts. Not retryable: authentication (401/403), malformed or
    unsupported requests (400/415/422), unsupported configuration, and any
    provider answer we cannot parse — repeating those only burns quota. The
    attempt count and the delay are both bounded, so a dead provider cannot
    turn into an infinite loop.
    """
    attempts = max(1, settings.stt_max_attempts)
    provider = STT_PROVIDERS[engine]
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(
                provider.attempt(session, audio_path, settings),
                timeout=settings.stt_job_timeout,
            )
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Network-level failure: transient unless this was the last try.
            if attempt >= attempts:
                raise STTTransientError(
                    f"{engine} failed after {attempts} attempt(s): {type(exc).__name__}"
                ) from exc
            detail, retry_after = type(exc).__name__, None
        except STTTransientError as exc:
            # In managed multi-key mode, a 429 cools down this credential and
            # ``transcribe`` immediately rotates to the next one. With legacy
            # environment settings (no credential manager), bounded retry is
            # the only recovery path and still observes Retry-After.
            if (exc.status == 429 and not retry_rate_limit) or attempt >= attempts:
                raise
            detail, retry_after = str(exc), exc.retry_after
        except STTError:
            # Permanent provider/request/configuration error: never repeated.
            raise
        delay = _retry_delay_seconds(attempt, settings, retry_after)
        logger.warning(
            "STT transient failure provider=%s attempt=%s/%s reason=%s retrying_in=%.1fs",
            engine,
            attempt,
            attempts,
            detail,
            delay,
        )
        await asyncio.sleep(delay)
    raise STTError(f"{engine} produced no transcript after {attempts} attempt(s)")


# ---------------------------------------------------------------------------
# Gamas Speech Platform integration.
#
# Everything above is the legacy request code, kept intact. Below: native
# adapters are registered in STT_PROVIDERS, routing is planned by the pure
# router, each candidate runs under per-provider concurrency and quota
# reservations, and the quality gate can reject a transcript. Events and usage
# are best-effort and can never break a transcription.
# ---------------------------------------------------------------------------

#: The request-scoped quota tracker is passed to native adapters through a
#: context variable, so the legacy ``_attempt_with_retries`` signature is kept.
_QUOTA_TRACKER: ContextVar[STTQuotaTracker | None] = ContextVar(
    "gamas_stt_quota_tracker", default=None
)
#: Per-provider semaphores, recreated when the running event loop changes.
_PROVIDER_SLOTS: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}


@dataclass(frozen=True, slots=True)
class _AdminState:
    enabled: bool = True
    billing_state: str = ""


async def _db_route_plan(db: Any) -> tuple[tuple[str, ...], dict[str, str]] | None:
    """Admin-edited ``stt_routes`` order plus per-leg model overrides.

    The STT Routes panel writes this table (spec §28/§61), so provider order
    and per-leg models are data-driven and need no code change. Returns
    ``None`` when the table holds no enabled rows: deployments without
    panel-managed routes keep the settings-derived order unchanged (spec §66).
    """
    if db is None:
        return None
    try:
        rows = await db.stt_routes_list(enabled_only=True)
    except Exception:
        logger.warning("Could not read stt_routes; falling back to the settings route")
        return None
    route = tuple(str(row.get("provider") or "").strip().lower() for row in rows)
    route = tuple(name for name in route if name)
    if not route:
        return None
    overrides = {
        str(row.get("provider") or "").strip().lower(): str(row.get("model_override") or "").strip()
        for row in rows
        if row.get("provider") and row.get("model_override")
    }
    return route, {name: model for name, model in overrides.items() if model}


def _job_policy(settings: Settings, plan_tier: str | None) -> SttPolicy:
    """Free/trial/paid policy for one job (spec §52).

    A free user's job is always free-only, even when the deployment enabled
    paid fallback for paying users. The tier never *widens* access: a paid job
    still runs under the deployment's configured ``STT_*`` policy, and an admin
    override happens through the admin panel, not through user input.
    """
    policy = SttPolicy.from_settings(settings)
    if str(plan_tier or "").strip().lower() == "free" and not policy.free_only:
        return replace(policy, free_only=True)
    return policy


def _settings_with_model(settings: Settings, engine: str, override: str | None) -> Settings:
    """Apply one route-leg ``model_override`` to the settings of that attempt.

    Each provider family stores its model in its own field; the override lands
    in exactly one of them so every request keeps its historical shape.
    """
    if not override:
        return settings
    if engine == "speechmatics":
        return replace(settings, speechmatics_operating_point=override)
    if engine == "deepgram":
        return replace(settings, deepgram_model=override)
    if engine == "openai_compatible":
        return replace(settings, stt_openai_model=override)
    pairs = dict(settings.stt_provider_models)
    pairs[engine] = override
    return replace(settings, stt_provider_models=tuple(pairs.items()))


def _native_model_label(provider: str, settings: Settings) -> str:
    configured = settings.stt_model(provider)
    if configured:
        return str(configured)
    info = STT_PROVIDER_REGISTRY.get(provider)
    return info.default_model if info else ""


def _native_options(settings: Settings, provider: str) -> AdapterOptions:
    info = STT_PROVIDER_REGISTRY[provider]
    diarization = bool(settings.stt_request_diarization) and info.supports_diarization
    word_timestamps = bool(settings.stt_request_word_timestamps) and info.supports_word_timestamps
    # Gemini cannot combine custom vocabulary with diarization or timestamps.
    vocabulary_allowed = info.vocabulary_supported and not (
        provider == "gemini_transcribe" and (diarization or word_timestamps)
    )
    return AdapterOptions(
        diarization=diarization,
        word_timestamps=word_timestamps,
        smart_transcription=bool(getattr(settings, "stt_smart_transcription", False))
        and info.supports_smart_formatting,
        vocabulary=vocabulary_hints(settings) if vocabulary_allowed else VocabularyHints(),
    )


def _legacy_error(exc: ProviderSTTError) -> STTError:
    """Map a normalized adapter error onto the legacy rotation/failover classes."""
    category = exc.category
    status = exc.http_status
    message = str(exc)
    if category in {
        STTErrorCategory.AUTHENTICATION,
        STTErrorCategory.PERMISSION,
        STTErrorCategory.BILLING_REQUIRED,
    }:
        return STTAuthenticationError(message, status=status or 401)
    if category in {STTErrorCategory.RATE_LIMITED, STTErrorCategory.QUOTA_EXHAUSTED}:
        return STTTransientError(message, exc.retry_after, status=status or 429)
    if category in {
        STTErrorCategory.SERVER_ERROR,
        STTErrorCategory.PROVIDER_TIMEOUT,
        STTErrorCategory.NETWORK_FAILURE,
    }:
        return STTTransientError(message, exc.retry_after, status=status)
    if category in {
        STTErrorCategory.FILE_TOO_LARGE,
        STTErrorCategory.DURATION_TOO_LONG,
        STTErrorCategory.FORMAT_UNSUPPORTED,
        STTErrorCategory.LANGUAGE_UNSUPPORTED,
        STTErrorCategory.INVALID_REQUEST,
    }:
        return STTRequestError(message, status=status or 400)
    # Schema errors, unknown failures and unimplemented models: permanent for
    # this request, never rotated, and the next provider is tried.
    return STTError(message)


def _legacy_transcript(platform: Any) -> Transcript:
    """Convert the rich platform transcript to the shape ``bot.py`` consumes.

    A provider confidence is passed on only when the platform marked it
    comparable across providers; otherwise it is unavailable, never guessed.
    """
    normalized = platform.normalized_confidence
    normalized = normalized() if callable(normalized) else normalized
    comparable = bool(getattr(normalized, "comparable", False))
    confidence = platform.confidence if comparable else None
    return Transcript(
        engine=str(platform.provider or platform.engine),
        text=str(platform.text or ""),
        confidence=confidence,
    )


async def _native_attempt(
    provider: str,
    session: aiohttp.ClientSession,
    audio_path: Path,
    settings: Settings,
) -> Transcript:
    adapter = NativeSTTAdapter(provider)
    try:
        platform = await adapter.transcribe(
            session,
            audio_path,
            settings,
            options=_native_options(settings, provider),
            quota_tracker=_QUOTA_TRACKER.get(),
        )
    except ProviderSTTError as exc:
        raise _legacy_error(exc) from None
    return _legacy_transcript(platform)


def _native_provider(slug: str) -> STTProvider:
    info = STT_PROVIDER_REGISTRY[slug]
    return STTProvider(
        availability=lambda settings, _slug=slug: settings.stt_api_key(_slug),
        attempt=lambda session, audio, settings, _slug=slug: _native_attempt(
            _slug, session, audio, settings
        ),
        label=info.display_name,
        # The registry records the provider's direct-upload limit.
        max_upload=lambda settings, _slug=slug: STT_PROVIDER_REGISTRY[_slug].max_file_size,
    )


for _slug in STT_PROVIDER_REGISTRY:
    STT_PROVIDERS.setdefault(_slug, _native_provider(_slug))


def _quota_units(quota_type: str, duration_seconds: float | None) -> float | None:
    """Units one job consumes for a budget row, or ``None`` when unknown."""
    if quota_type in {"rpm", "rpd", "requests", "requests_per_minute"}:
        return 1.0
    if duration_seconds is None or duration_seconds <= 0:
        return None
    if quota_type.startswith("audio_seconds"):
        return float(duration_seconds)
    if quota_type.startswith("minutes"):
        return float(duration_seconds) / 60.0
    return None


async def _reserve_quota(
    db: Any,
    provider: str,
    credential_id: int | None,
    duration_seconds: float | None,
    settings: Settings,
    submission_id: int | None,
) -> tuple[list[int], str | None]:
    """Reserve every applicable budget row. Returns (reservation ids, denial).

    No budget rows means no admin-maintained budget exists, so nothing is
    reserved and the candidate stays eligible. Budget rows that cannot be
    reserved (exhausted, stale, unknown units) deny the candidate.
    """
    if db is None:
        return [], None
    try:
        budgets = await db.stt_quota_budgets(provider)
    except Exception:
        return [], "store_unavailable"
    rows = [row for row in budgets if row.get("credential_id") in (None, credential_id)]
    if not rows:
        return [], None
    reserved: list[int] = []
    for row in rows:
        units = _quota_units(str(row.get("quota_type") or ""), duration_seconds)
        if units is None:
            await _finalize_reservations(db, reserved, commit=False)
            return [], "units_unknown"
        reservation_id, _remaining, status = await db.stt_quota_reserve(
            provider,
            str(row.get("account_scope") or ""),
            str(row.get("quota_type") or ""),
            needed=units,
            safety_margin=float(getattr(settings, "stt_quota_safety_margin", 0.10)),
            submission_id=submission_id,
            credential_id=credential_id,
        )
        if status != "available" or reservation_id is None:
            await _finalize_reservations(db, reserved, commit=False)
            return [], status
        reserved.append(reservation_id)
    return reserved, None


async def _finalize_reservations(db: Any, reservation_ids: list[int], *, commit: bool) -> None:
    if db is None:
        return
    for reservation_id in reservation_ids:
        try:
            await db.stt_quota_reservation_finalize(reservation_id, commit=commit)
        except Exception:
            logger.warning("STT quota reservation finalize failed reservation=%s", reservation_id)


async def _record_usage(db: Any, **record: Any) -> None:
    """Best-effort usage row; accounting must never break transcription."""
    if db is None:
        return
    try:
        await db.stt_usage_record(
            {"created_at": datetime.now(timezone.utc).isoformat(), **record}
        )
    except Exception:
        logger.debug("STT usage record skipped provider=%s", record.get("provider"))


def _error_category(exc: BaseException) -> str:
    if isinstance(exc, STTAuthenticationError):
        return "authentication"
    if isinstance(exc, STTTransientError):
        if exc.status == 429:
            return "rate_limited"
        return "network_failure" if exc.status is None else "server_error"
    if isinstance(exc, (STTRequestError, STTConfigurationError)):
        return "invalid_request"
    if isinstance(exc, asyncio.TimeoutError):
        return "provider_timeout"
    if isinstance(exc, aiohttp.ClientError):
        return "network_failure"
    return "unknown"


def _quality_rejections(transcript: Transcript, settings: Settings, duration: float | None) -> tuple[str, ...]:
    """Hard quality failures only; soft signals stay warnings in the gate."""
    gate = TranscriptQualityGate(settings)
    if not gate.enabled:
        return ()
    platform_view = PlatformTranscript(
        engine=transcript.engine,
        text=transcript.text,
        confidence=transcript.confidence,
        provider=transcript.engine,
    )
    verdict = gate.evaluate(
        platform_view,
        # Ratio checks are meaningful only for longer audio.
        audio_duration_seconds=duration if duration is not None and duration >= 30 else None,
        expected_language=settings.stt_language,
    )
    return hard_quality_failures(verdict.reasons, text_length=len(transcript.text or ""))


def _language_error(provider: str, settings: Settings) -> str | None:
    requested = settings.stt_language
    try:
        if provider in LEGACY_STT_PROVIDERS:
            normalize_language_for_provider(provider, requested)
        else:
            resolve_language(provider, requested, model=_native_model_label(provider, settings))
    except (STTError, ProviderSTTError, ValueError):
        return "language_unsupported"
    return None


async def _credential_pool(
    provider: str,
    settings: Settings,
    credentials: ProviderCredentialManager | None,
) -> list:
    if credentials is None:
        return [None] if STT_PROVIDERS[provider].availability(settings) else []
    if provider == "speechmatics":
        fallback = (settings.speechmatics_api_key, settings.speechmatics_base_url, None)
    elif provider == "deepgram":
        fallback = (settings.deepgram_api_key, None, None)
    elif provider == "openai_compatible":
        fallback = (settings.stt_openai_api_key, settings.stt_openai_base_url, settings.stt_openai_model)
    else:
        fallback = (settings.stt_api_key(provider), settings.stt_base_url(provider), settings.stt_model(provider))
    return await credentials.candidates(
        "stt",
        provider,
        fallback_secret=fallback[0],
        fallback_base_url=fallback[1],
        fallback_model=fallback[2],
    )


async def _provider_admin_state(db: Any, provider: str) -> _AdminState:
    if db is None:
        return _AdminState()
    try:
        row = await db.stt_provider_settings_get(provider)
    except Exception:
        logger.debug("STT provider settings unavailable provider=%s", provider)
        return _AdminState()
    if not row:
        return _AdminState()
    return _AdminState(
        enabled=bool(row.get("enabled", 1)),
        billing_state=str(row.get("billing_state") or ""),
    )


def _provider_slot(provider: str, settings: Settings) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    entry = _PROVIDER_SLOTS.get(provider)
    if entry is None or entry[0] is not loop:
        limit = ProviderConcurrencyRegistry(settings).limit(provider)
        entry = (loop, asyncio.Semaphore(max(1, int(limit))))
        _PROVIDER_SLOTS[provider] = entry
    return entry[1]


def _no_route_error(plan: RoutePlan, settings: Settings, file_size: int) -> STTError:
    configured = [item for item in plan.decisions if "not_configured" not in item.reasons]
    if not configured:
        return STTError("هیچ کلید یا نشانی API برای سرویس تبدیل گفتار تنظیم نشده است.")
    if all("file_too_large" in item.reasons for item in configured):
        limits = "، ".join(
            f"{STT_PROVIDERS[item.provider].label}:تا حد "
            f"{STT_PROVIDERS[item.provider].max_upload(settings) / 1_000_000_000:g} گیگابایت"
            for item in configured
        )
        return STTError(
            "حجم فایل از سقف آپلود مستقیم همهٔ سرویس‌های پیکربندی‌شده بیشتر است "
            f"({limits}). فایل کوچک‌تری بفرستید یا سرویس دیگری را فعال کنید."
        )
    summary = "؛ ".join(
        f"{item.provider}: {', '.join(item.reasons)}" for item in plan.decisions if item.reasons
    )
    logger.warning("STT route has no eligible provider file_bytes=%s summary=%s", file_size, summary)
    return STTError(
        "هیچ سرویس تبدیل گفتار مجاز و مناسبی برای این فایل در مسیر فعلی پیدا نشد "
        f"({summary})."
    )


async def _attempt_provider(
    provider: str,
    session: aiohttp.ClientSession,
    audio_path: Path,
    settings: Settings,
    pool: list,
    *,
    credentials: ProviderCredentialManager | None,
    db: Any,
    events: STTEventLogger,
    submission_id: int | None,
    job_ref: str,
    duration: float | None,
    file_size: int,
    route_position: int,
    attempt: int,
) -> Transcript:
    """Run the credential pool for one provider, rotating on key-level errors."""
    transcript: Transcript | None = None
    last_key_error: Exception | None = None
    model = _native_model_label(provider, settings)
    for credential in pool:
        request_settings = (
            credentials.apply_to_settings(settings, credential)
            if credentials is not None and credential is not None
            else settings
        )
        credential_id = credential.id if credential is not None else None
        reservation_ids, denial = await _reserve_quota(
            db, provider, credential_id, duration, settings, submission_id
        )
        if denial is not None:
            last_key_error = STTError(f"{provider} quota budget unavailable ({denial})")
            await events.emit(
                "stt_quota_exhausted" if denial in {"exhausted", "insufficient"} else "stt_quota_warning",
                provider=provider,
                model=model,
                job_id=job_ref,
                submission_id=submission_id,
                credential_id=credential_id,
                route_position=route_position,
                attempt=attempt,
                error_category="quota_exhausted",
                detail=denial,
            )
            continue
        committed = False
        call_started = time.monotonic()
        try:
            # Bound the whole provider attempt, including upload, polling, and
            # transcript download.
            transcript = await _attempt_with_retries(
                provider,
                session,
                audio_path,
                request_settings,
                retry_rate_limit=credentials is None,
            )
            committed = True
            if credentials is not None and credential is not None:
                await credentials.record_result(credential, result="success")
            await _record_usage(
                db,
                submission_id=submission_id,
                job_id=job_ref,
                provider=provider,
                model=model,
                credential_id=credential_id,
                route_position=route_position,
                attempt=attempt,
                result="success",
                audio_bytes=file_size,
                audio_duration_seconds=duration,
                latency_ms=int((time.monotonic() - call_started) * 1000),
                quota_type=None,
                quota_units=None,
            )
            break
        except asyncio.CancelledError:
            raise
        except STTAuthenticationError as exc:
            last_key_error = exc
            if credentials is not None and credential is not None:
                await credentials.record_result(
                    credential, result="quarantined", status_code=exc.status, safe_error=f"HTTP {exc.status}"
                )
            continue
        except STTTransientError as exc:
            last_key_error = exc
            if credentials is not None and credential is not None:
                await credentials.record_result(
                    credential,
                    result="cooldown" if exc.status == 429 else "error",
                    status_code=exc.status,
                    retry_after_seconds=exc.retry_after,
                    safe_error=f"HTTP {exc.status}" if exc.status else "transient provider failure",
                )
            continue
        except STTRequestError as exc:
            if credentials is not None and credential is not None:
                await credentials.record_result(
                    credential, result="invalid_request", status_code=exc.status, safe_error=f"HTTP {exc.status}"
                )
            # 400/415/422 are properties of the request, not of the key.
            raise
        except STTError as exc:
            if credentials is not None and credential is not None:
                await credentials.record_result(credential, result="error", safe_error=type(exc).__name__)
            raise
        except Exception as exc:
            if credentials is not None and credential is not None:
                await credentials.record_result(credential, result="error", safe_error=type(exc).__name__)
            raise
        finally:
            await _finalize_reservations(db, reservation_ids, commit=committed)
    if transcript is None:
        if last_key_error is not None:
            raise last_key_error
        raise STTError(f"{provider} has no available credential")
    return transcript


async def transcribe(
    audio_path: Path,
    settings: Settings,
    *,
    credentials: ProviderCredentialManager | None = None,
    submission_id: int | None = None,
    job_id: str = "",
    plan_tier: str | None = None,
) -> Transcript:
    """Transcribe with planned provider routing, fallback and key rotation.

    Whole-file only: files at/above a provider's direct-upload limit are routed
    to another eligible engine instead of being split. The route, free/trial
    policy, quota reservations and quality gate are applied per candidate, and
    the legacy confidence rule picks between results exactly as before.

    ``plan_tier`` (``"free"`` / ``"paid"``) narrows the policy for the caller's
    subscription (spec §52): a free user's job is always routed free-only, and
    the value can never widen access beyond the deployment's configured policy.
    """
    db = getattr(credentials, "db", None)
    events = STTEventLogger(db)
    job_ref = str(job_id or submission_id or "")[:120]
    file_size = audio_path.stat().st_size
    duration = await asyncio.to_thread(get_audio_duration_seconds, audio_path)
    token = _QUOTA_TRACKER.set(STTQuotaTracker(db, settings))
    try:
        return await _transcribe_routed(
            audio_path,
            settings,
            credentials=credentials,
            db=db,
            events=events,
            job_ref=job_ref,
            submission_id=submission_id,
            file_size=file_size,
            duration=duration,
            plan_tier=plan_tier,
        )
    finally:
        _QUOTA_TRACKER.reset(token)


async def _transcribe_routed(
    audio_path: Path,
    settings: Settings,
    *,
    credentials: ProviderCredentialManager | None,
    db: Any,
    events: STTEventLogger,
    job_ref: str,
    submission_id: int | None,
    file_size: int,
    duration: float | None,
    plan_tier: str | None = None,
) -> Transcript:
    job_started = time.monotonic()
    req = SttRequirements.for_job(
        language=settings.stt_language,
        file_bytes=file_size,
        duration_seconds=duration,
        diarization=bool(settings.stt_request_diarization),
        word_timestamps=bool(settings.stt_request_word_timestamps),
        vocabulary_terms=len(vocabulary_hints(settings).terms),
    )
    db_route = await _db_route_plan(db)
    if db_route is not None:
        effective_route, model_overrides = db_route
    else:
        effective_route, model_overrides = resolve_route(settings), {}
    facts: dict[str, CandidateFacts] = {}
    key_pools: dict[str, list] = {}
    for name in effective_route:
        if name not in STT_PROVIDERS:
            continue
        key_pools[name] = await _credential_pool(name, settings, credentials)
        admin = await _provider_admin_state(db, name)
        facts[name] = CandidateFacts(
            has_credential=bool(key_pools[name]),
            admin_enabled=admin.enabled,
            billing_state=admin.billing_state,
            max_upload=STT_PROVIDERS[name].max_upload(settings),
            language_error=_language_error(name, settings),
        )
    plan = plan_route(
        settings,
        req,
        facts,
        policy=_job_policy(settings, plan_tier),
        route=effective_route,
    )
    for decision in plan.decisions:
        logger.info(
            "STT route decision %s",
            " ".join(f"{key}={value}" for key, value in decision.as_log_fields().items()),
        )
    if not plan.execution:
        raise _no_route_error(plan, settings, file_size)

    timeout = aiohttp.ClientTimeout(
        total=None, connect=45, sock_read=min(max(settings.stt_job_timeout, 120), 660)
    )
    logger.info(
        "STT job started file_bytes=%s primary=%s fallback_enabled=%s candidate_engines=%s",
        file_size,
        settings.stt_primary,
        settings.stt_fallback_enabled,
        list(plan.execution),
    )
    failures: list[str] = []
    outcomes: list[Transcript] = []
    last = len(plan.execution) - 1
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index, engine in enumerate(plan.execution):
            started = time.monotonic()
            position = plan.route.index(engine) if engine in plan.route else 0
            engine_settings = _settings_with_model(settings, engine, model_overrides.get(engine))
            model = _native_model_label(engine, engine_settings)
            logger.info(
                "STT attempt started provider=%s file_bytes=%s attempt=%s/%s",
                engine,
                file_size,
                index + 1,
                len(plan.execution),
            )
            await events.emit(
                "stt_request_started",
                provider=engine,
                model=model,
                job_id=job_ref,
                submission_id=submission_id,
                route_position=position,
                attempt=index + 1,
                audio_bytes=file_size,
                audio_duration_seconds=duration,
            )
            try:
                async with _provider_slot(engine, settings):
                    transcript = await _attempt_provider(
                        engine,
                        session,
                        audio_path,
                        engine_settings,
                        key_pools.get(engine) or [],
                        credentials=credentials,
                        db=db,
                        events=events,
                        submission_id=submission_id,
                        job_ref=job_ref,
                        duration=duration,
                        file_size=file_size,
                        route_position=position,
                        attempt=index + 1,
                    )
                chars, words = _transcript_metrics(transcript.text)
                logger.info(
                    "STT attempt completed provider=%s elapsed_seconds=%.3f confidence=%s text_chars=%s text_words=%s attempt=%s/%s",
                    engine,
                    time.monotonic() - started,
                    f"{transcript.confidence:.3f}" if transcript.confidence is not None else "unavailable",
                    chars,
                    words,
                    index + 1,
                    len(plan.execution),
                )
                rejected = _quality_rejections(transcript, settings, duration)
                if rejected:
                    codes = ",".join(reason.split(":", 1)[0] for reason in rejected)
                    logger.warning("STT quality gate rejected provider=%s reasons=%s", engine, codes)
                    await events.emit(
                        "stt_quality_rejected",
                        provider=engine,
                        model=model,
                        job_id=job_ref,
                        submission_id=submission_id,
                        route_position=position,
                        attempt=index + 1,
                        error_category="quality_failure",
                        detail=codes[:200],
                    )
                    failures.append(f"{engine}: quality check rejected the transcript ({codes})")
                    if index < last:
                        await events.emit(
                            "stt_provider_fallback",
                            provider=engine,
                            next_provider=plan.execution[index + 1],
                            fallback_reason="quality_failure",
                            job_id=job_ref,
                        )
                    continue
                await events.emit(
                    "stt_request_succeeded",
                    provider=engine,
                    model=model,
                    job_id=job_ref,
                    submission_id=submission_id,
                    route_position=position,
                    attempt=index + 1,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    confidence=transcript.confidence,
                    word_count=words,
                    character_count=chars,
                )
                outcomes.append(transcript)
                is_low = transcript.confidence is not None and transcript.confidence < settings.stt_min_confidence
                if not is_low or index == last:
                    break
                logger.warning(
                    "Low STT confidence from %s (%.3f < threshold %.3f); trying fallback",
                    engine,
                    transcript.confidence,
                    settings.stt_min_confidence,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Unexpected client errors can contain URLs, keys or response
                # fragments. Keep only our own sanitized errors and error types.
                detail = str(exc) if isinstance(exc, STTError) else type(exc).__name__
                if isinstance(exc, STTConfigurationError):
                    logger.warning("STT provider rejected the configuration provider=%s detail=%s", engine, detail)
                else:
                    logger.warning(
                        "STT provider failed provider=%s elapsed_seconds=%.3f error_type=%s detail=%s attempt=%s/%s",
                        engine,
                        time.monotonic() - started,
                        type(exc).__name__,
                        detail,
                        index + 1,
                        len(plan.execution),
                    )
                category = _error_category(exc)
                failures.append(f"{engine}: {detail}")
                await events.emit(
                    "stt_request_failed",
                    provider=engine,
                    model=model,
                    job_id=job_ref,
                    submission_id=submission_id,
                    route_position=position,
                    attempt=index + 1,
                    error_category=category,
                    http_status=getattr(exc, "status", None),
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                await _record_usage(
                    db,
                    submission_id=submission_id,
                    job_id=job_ref,
                    provider=engine,
                    model=model,
                    credential_id=None,
                    route_position=position,
                    attempt=index + 1,
                    result="failure",
                    audio_bytes=file_size,
                    audio_duration_seconds=duration,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    http_status=getattr(exc, "status", None),
                    error_category=category,
                )
                if index < last:
                    await events.emit(
                        "stt_provider_fallback",
                        provider=engine,
                        next_provider=plan.execution[index + 1],
                        fallback_reason=category,
                        job_id=job_ref,
                    )
                if index == last and not outcomes:
                    raise STTError("؛ ".join(failures)) from None
        if not outcomes:
            raise STTError("؛ ".join(failures) or "تبدیل گفتار ناموفق بود.")
        # When both providers work, prefer the more confident result. If either
        # omits confidence, the later fallback result is preferred after a low score.
        if len(outcomes) == 1:
            selected = outcomes[0]
        elif outcomes[0].confidence is not None and outcomes[1].confidence is not None:
            selected = max(outcomes, key=lambda item: item.confidence)
        else:
            selected = outcomes[-1]
        chars, words = _transcript_metrics(selected.text)
        logger.info(
            "STT job finished engine=%s confidence=%s text_chars=%s text_words=%s outcomes=%s total_elapsed_seconds=%.3f",
            selected.engine,
            f"{selected.confidence:.3f}" if selected.confidence is not None else "unavailable",
            chars,
            words,
            [outcome.engine for outcome in outcomes],
            time.monotonic() - job_started,
        )
        return selected
