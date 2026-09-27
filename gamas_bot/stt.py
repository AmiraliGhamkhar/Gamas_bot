from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .config import Settings

logger = logging.getLogger(__name__)


class STTError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Transcript:
    engine: str
    text: str
    confidence: float | None = None


def _provider_key(settings: Settings, engine: str) -> str | None:
    """API key configured for the given STT engine, if any."""
    return settings.speechmatics_api_key if engine == "speechmatics" else settings.deepgram_api_key


def _speechmatics_confidence(payload: dict) -> float | None:
    values = []
    for item in payload.get("results", []):
        alternatives = item.get("alternatives") or []
        if alternatives and alternatives[0].get("confidence") is not None:
            try:
                values.append(float(alternatives[0]["confidence"]))
            except (TypeError, ValueError):
                pass
    return sum(values) / len(values) if values else None


def _deepgram_transcript(payload: dict) -> Transcript:
    try:
        alternative = payload["results"]["channels"][0]["alternatives"][0]
        text = alternative.get("transcript", "").strip()
        confidence = alternative.get("confidence")
        return Transcript("deepgram", text, float(confidence) if confidence is not None else None)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise STTError("ساختار پاسخ دیپ‌گرام قابل‌خواندن نیست.") from exc


async def _error_text(response: aiohttp.ClientResponse) -> str:
    try:
        return (await response.text())[:700]
    except Exception:
        return f"HTTP {response.status}"


def speechmatics_config(settings: Settings) -> dict:
    """Job configuration for the Speechmatics batch API."""
    return {
        "type": "transcription",
        "transcription_config": {
            "language": settings.stt_language,
            "model": "standard",
        },
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
                raise STTError(f"Speechmatics job submission failed ({response.status}): {await _error_text(response)}")
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
                raise STTError(f"Speechmatics status failed ({response.status}): {await _error_text(response)}")
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
            raise STTError(f"Speechmatics transcript retrieval failed ({response.status}): {await _error_text(response)}")
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
                raise STTError(f"Deepgram request failed ({response.status}): {await _error_text(response)}")
            payload = await response.json(content_type=None)
    transcript = _deepgram_transcript(payload)
    if not transcript.text:
        raise STTError("Deepgram متن قابل‌استفاده‌ای تولید نکرد.")
    return transcript


async def transcribe(audio_path: Path, settings: Settings) -> Transcript:
    """Transcribe with the configured primary provider and optional fallback."""
    providers = {
        "speechmatics": _speechmatics,
        "deepgram": _deepgram,
    }
    primary = settings.stt_primary
    secondary = "deepgram" if primary == "speechmatics" else "speechmatics"
    order = [primary]
    if settings.stt_fallback_enabled:
        order.append(secondary)
    elif not _provider_key(settings, primary):
        # Fallback is off, but refusing every job because the *primary* engine
        # has no key while the other one does would be pointless.
        logger.warning(
            "STT primary provider=%s has no API key; using %s instead", primary, secondary
        )
        order = [secondary]
    if audio_path.stat().st_size >= 1_000_000_000:
        # Speechmatics Batch SaaS rejects direct multipart uploads at 1 GB.
        # Deepgram supports direct pre-recorded uploads up to 2 GB.
        order = ["deepgram"] if settings.deepgram_api_key else []
        if not order:
            raise STTError("برای فایل‌های یک گیگابایت یا بزرگ‌تر، کلید Deepgram لازم است.")
    available = [name for name in order if _provider_key(settings, name)]
    if not available:
        raise STTError("هیچ کلید API برای سرویس تبدیل گفتار تنظیم نشده است.")

    timeout = aiohttp.ClientTimeout(
        total=None, connect=45, sock_read=min(max(settings.stt_job_timeout, 120), 660)
    )
    failures: list[str] = []
    outcomes: list[Transcript] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index, engine in enumerate(available):
            started = time.monotonic()
            logger.info(
                "STT attempt started provider=%s file_bytes=%s attempt=%s/%s",
                engine,
                audio_path.stat().st_size,
                index + 1,
                len(available),
            )
            try:
                transcript = await providers[engine](session, audio_path, settings)
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
                if not is_low or index == len(available) - 1:
                    break
                logger.warning(
                    "Low STT confidence from %s (%.3f); trying fallback",
                    engine,
                    transcript.confidence,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "STT provider failed provider=%s elapsed_seconds=%.3f",
                    engine,
                    time.monotonic() - started,
                )
                failures.append(f"{engine}: {exc}")
                if index == len(available) - 1 and not outcomes:
                    raise STTError("؛ ".join(failures)) from exc
        if not outcomes:
            raise STTError("؛ ".join(failures) or "تبدیل گفتار ناموفق بود.")
        # When both providers work, prefer the more confident result. If either
        # omits confidence, the later (fallback) result is preferred after a low score.
        if len(outcomes) == 1:
            return outcomes[0]
        if outcomes[0].confidence is not None and outcomes[1].confidence is not None:
            return max(outcomes, key=lambda item: item.confidence)
        return outcomes[-1]
