from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .config import Settings
from .structuring import _endpoint

logger = logging.getLogger(__name__)


class STTError(RuntimeError):
    pass


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


def _speechmatics_confidence(payload: dict) -> float | None:
    values = []
    for item in payload.get("results", []):
        alternatives = item.get("alternatives") or []
        if alternatives and alternatives[0].get("confidence") is not None:
            try:
                value = float(alternatives[0]["confidence"])
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


def _http_error(stage: str, status: int) -> STTError:
    # Provider/gateway error bodies may echo lecture content or credentials.
    return STTError(f"{stage} failed (HTTP {status})")


def speechmatics_config(settings: Settings) -> dict:
    """Job configuration for the Speechmatics batch API.

    ``model`` defaults to ``enhanced`` — Speechmatics documents it as the
    highest-accuracy tier (``standard`` only prioritises throughput), and this
    pipeline is accuracy-first. ``additional_vocab`` is the provider's native
    custom-dictionary feature: exact terms for drug names and English
    technical vocabulary, with no LLM or post-processing layer involved.
    """
    transcription_config: dict[str, object] = {
        "language": settings.stt_language,
        "model": settings.speechmatics_model,
    }
    if settings.speechmatics_additional_vocab:
        transcription_config["additional_vocab"] = [
            {"content": term} for term in settings.speechmatics_additional_vocab
        ]
    return {
        "type": "transcription",
        "transcription_config": transcription_config,
    }


def deepgram_params(settings: Settings) -> dict[str, str]:
    """Query parameters for the Deepgram pre-recorded API."""
    return {
        "model": settings.deepgram_model,
        "language": settings.stt_language,
        "smart_format": "true",
        "punctuate": "true",
    }


async def _speechmatics(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> Transcript:
    assert settings.speechmatics_api_key
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
                raise _http_error("Speechmatics job submission", response.status)
            payload = await response.json(content_type=None)
    job_id = payload.get("id")
    if not job_id:
        raise STTError("Speechmatics شناسهٔ پردازش را برنگرداند.")

    deadline = time.monotonic() + settings.stt_job_timeout
    while True:
        if time.monotonic() >= deadline:
            raise STTError("زمان پردازش Speechmatics به پایان رسید.")
        async with session.get(
            f"{settings.speechmatics_base_url}/jobs/{quote(str(job_id), safe='')}",
            headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
        ) as response:
            if response.status != 200:
                raise _http_error("Speechmatics status", response.status)
            status_data = await response.json(content_type=None)
        job = status_data.get("job", status_data)
        status = str(job.get("status", "")).lower()
        if status == "done":
            break
        if status in {"rejected", "failed", "deleted", "expired"}:
            raise STTError(f"Speechmatics کار را با وضعیت {status} پایان داد.")
        await asyncio.sleep(min(settings.stt_poll_interval, max(0.1, deadline - time.monotonic())))

    async with session.get(
        f"{settings.speechmatics_base_url}/jobs/{quote(str(job_id), safe='')}/transcript",
        params={"format": "json-v2"},
        headers={"Authorization": f"Bearer {settings.speechmatics_api_key}"},
    ) as response:
        if response.status != 200:
            raise _http_error("Speechmatics transcript retrieval", response.status)
        result = await response.json(content_type=None)
    parts: list[str] = []
    for item in result.get("results", []):
        item_type = item.get("type")
        alternatives = item.get("alternatives") or []
        if item_type not in {"word", "punctuation"} or not alternatives:
            continue
        content = str(alternatives[0].get("content", ""))
        if item_type == "punctuation" and parts:
            parts[-1] += content
        elif content:
            parts.append(content)
    text = " ".join(parts).strip()
    if not text:
        # Some API response versions expose the text in results without a type.
        text = " ".join(
            str(item.get("alternatives", [{}])[0].get("content", ""))
            for item in result.get("results", [])
            if item.get("alternatives")
        ).strip()
    if not text:
        raise STTError("Speechmatics متن قابل‌استفاده‌ای تولید نکرد.")
    return Transcript("speechmatics", text, _speechmatics_confidence(result))


async def _deepgram(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> Transcript:
    assert settings.deepgram_api_key
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
                raise _http_error("Deepgram request", response.status)
            payload = await response.json(content_type=None)
    transcript = _deepgram_transcript(payload)
    if not transcript.text:
        raise STTError("Deepgram متن قابل‌استفاده‌ای تولید نکرد.")
    return transcript


def _openai_transcript(payload: object) -> Transcript:
    """Normalize an OpenAI-compatible /audio/transcriptions response."""
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise STTError("سرویس تبدیل گفتار سازگار با OpenAI متن قابل‌استفاده‌ای تولید نکرد.")
    # Whisper-style APIs return no per-word confidence, so the router treats
    # this engine like any other un-scored provider.
    return Transcript("openai_compatible", text.strip(), None)


async def _openai_compatible_stt(
    session: aiohttp.ClientSession, audio_path: Path, settings: Settings
) -> Transcript:
    """POST to an OpenAI-compatible STT endpoint (OpenAI, Groq, vLLM, ...)."""
    assert settings.stt_openai_base_url
    form = aiohttp.FormData()
    form.add_field("model", settings.stt_openai_model)
    form.add_field("language", settings.stt_language)
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
                raise _http_error("OpenAI-compatible STT request", response.status)
            payload = await response.json(content_type=None)
    return _openai_transcript(payload)


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


async def transcribe(audio_path: Path, settings: Settings) -> Transcript:
    """Transcribe with the configured primary provider and optional fallback."""
    primary = settings.stt_primary
    order = [primary]
    if settings.stt_fallback_enabled:
        order += [name for name in STT_PROVIDERS if name != primary]
    elif not STT_PROVIDERS[primary].availability(settings):
        # Fallback is off, but refusing every job because the *primary* engine
        # is not configured while another one is would be pointless.
        others = [name for name in STT_PROVIDERS if name != primary]
        logger.warning(
            "STT primary provider=%s is not configured; using another configured engine",
            primary,
        )
        order = others
    configured = [name for name in order if STT_PROVIDERS[name].availability(settings)]
    if not configured:
        raise STTError("هیچ کلید یا نشانی API برای سرویس تبدیل گفتار تنظیم نشده است.")

    # Whole-file transcription only: chunking audio would cost word context at
    # every boundary. Files at/above a provider's direct-upload limit are
    # routed to another configured engine that accepts them.
    file_size = audio_path.stat().st_size
    usable: list[str] = []
    oversized: list[str] = []
    for name in configured:
        (oversized if file_size >= STT_PROVIDERS[name].max_upload(settings) else usable).append(
            name
        )
    if not usable:
        limits = "، ".join(
            f"{STT_PROVIDERS[name].label}:تا حد {STT_PROVIDERS[name].max_upload(settings) / 1_000_000_000:g} گیگابایت"
            for name in configured
        )
        raise STTError(
            "حجم فایل از سقف آپلود مستقیم همهٔ سرویس‌های پیکربندی‌شده بیشتر است "
            f"({limits}). فایل کوچک‌تری بفرستید یا سرویس دیگری را فعال کنید."
        )
    if oversized:
        logger.info(
            "STT providers skipped for size file_bytes=%s skipped=%s remaining=%s",
            file_size,
            oversized,
            usable,
        )

    timeout = aiohttp.ClientTimeout(
        total=None, connect=45, sock_read=min(max(settings.stt_job_timeout, 120), 660)
    )
    failures: list[str] = []
    outcomes: list[Transcript] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index, engine in enumerate(usable):
            started = time.monotonic()
            logger.info(
                "STT attempt started provider=%s file_bytes=%s attempt=%s/%s",
                engine,
                audio_path.stat().st_size,
                index + 1,
                len(usable),
            )
            try:
                # Bound the whole provider attempt, including upload, polling
                # and transcript download (socket read timeouts are not totals).
                transcript = await asyncio.wait_for(
                    STT_PROVIDERS[engine].attempt(session, audio_path, settings),
                    timeout=settings.stt_job_timeout,
                )
                logger.info(
                    "STT attempt completed provider=%s elapsed_seconds=%.3f confidence=%s text_chars=%s",
                    engine,
                    time.monotonic() - started,
                    transcript.confidence,
                    len(transcript.text),
                )
                outcomes.append(transcript)
                is_low = (
                    transcript.confidence is not None
                    and transcript.confidence < settings.stt_min_confidence
                )
                if not is_low or index == len(usable) - 1:
                    break
                logger.warning(
                    "Low STT confidence from %s (%.3f); trying fallback",
                    engine,
                    transcript.confidence,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "STT provider failed provider=%s elapsed_seconds=%.3f error_type=%s",
                    engine,
                    time.monotonic() - started,
                    type(exc).__name__,
                )
                # Unexpected client errors can contain URLs, keys or response
                # fragments. Keep only our own sanitized errors and error types.
                detail = str(exc) if isinstance(exc, STTError) else type(exc).__name__
                failures.append(f"{engine}: {detail}")
                if index == len(usable) - 1 and not outcomes:
                    raise STTError("؛ ".join(failures)) from None
        if not outcomes:
            raise STTError("؛ ".join(failures) or "تبدیل گفتار ناموفق بود.")
        # When both providers work, prefer the more confident result. If either
        # omits confidence, the later (fallback) result is preferred after a low score.
        if len(outcomes) == 1:
            return outcomes[0]
        if outcomes[0].confidence is not None and outcomes[1].confidence is not None:
            return max(outcomes, key=lambda item: item.confidence)
        return outcomes[-1]
