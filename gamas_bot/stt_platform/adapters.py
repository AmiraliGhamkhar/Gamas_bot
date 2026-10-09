"""Provider-native STT adapters, request builders and response normalizers.

This is the transport layer for the Gamas Speech Platform. It deliberately
keeps the protocols separate:

* Speechmatics JSON job + poll (existing implementation remains in ``stt.py``)
* Deepgram pre-recorded REST (existing implementation remains in ``stt.py``)
* OpenAI audio transcriptions multipart (generic and Groq have distinct models,
  limits, quota and request settings)
* Gemini Interactions API with a native Files API upload
* AssemblyAI upload + async transcript job
* Gladia upload + pre-recorded async job
* Google Cloud Speech-to-Text v1 long-running job
* IBM Watson synchronous batch REST
* Azure Speech Fast Transcription multipart
* Soniox async job
* ElevenLabs Scribe multipart

AWS Transcribe is deliberately not implemented as an adapter yet: its official
batch API requires an S3 object and a SigV4-signed S3 upload in addition to the
Transcribe job, so a key-only integration would fail deterministically. It is
registered as EXPERIMENTAL / disabled until an S3 bucket/role workflow is
configured. The registry and router will never send Persian batch audio to it
(fa-IR is streaming-only in the current AWS language table).

No adapter logs raw responses, transcripts, audio, URLs with credentials,
request headers or secrets. A ``ProviderSTTError`` carries only a normalized
category, status, retry advice, and a safe generic message (spec §71).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import mimetypes
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from .models import Transcript, TranscriptSegment, TranscriptWord, VocabularyHints
from .quota import QuotaObservation, parse_groq_headers
from .registry import STT_PROVIDER_REGISTRY, STTProtocol

logger = logging.getLogger(__name__)


class STTErrorCategory:
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    BILLING_REQUIRED = "billing_required"
    MODEL_UNAVAILABLE = "model_unavailable"
    LANGUAGE_UNSUPPORTED = "language_unsupported"
    FORMAT_UNSUPPORTED = "format_unsupported"
    FILE_TOO_LARGE = "file_too_large"
    DURATION_TOO_LONG = "duration_too_long"
    PROVIDER_TIMEOUT = "provider_timeout"
    NETWORK_FAILURE = "network_failure"
    SERVER_ERROR = "server_error"
    INVALID_REQUEST = "invalid_request"
    RESPONSE_SCHEMA_ERROR = "response_schema_error"
    QUALITY_FAILURE = "quality_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderSTTError(RuntimeError):
    """Normalized provider error (safe to surface to routing/admin logic)."""

    category: str
    retryable: bool
    http_status: int | None
    retry_after: float | None
    request_id: str | None
    provider: str
    model: str
    safe_message: str

    def __str__(self) -> str:
        return self.safe_message


@dataclass(frozen=True, slots=True)
class RequestPreview:
    """Secret-/audio-free, provider-native request preview for the admin UI."""

    provider: str
    model: str
    endpoint: str
    method: str
    protocol: str
    headers_redacted: tuple[tuple[str, str], ...] = ()
    audio_bytes: int = 0
    audio_duration_seconds: float | None = None
    language: str | None = None
    requested_features: tuple[str, ...] = ()
    form_fields: tuple[tuple[str, str], ...] = ()
    payload_shape: str = ""


@dataclass(frozen=True, slots=True)
class AdapterOptions:
    """Per-request optional features. All default to off (spec §12/§13)."""

    diarization: bool = False
    word_timestamps: bool = False
    smart_transcription: bool = False
    vocabulary: VocabularyHints = field(default_factory=VocabularyHints)
    max_speakers: int | None = None


@dataclass(frozen=True, slots=True)
class AdapterResponse:
    """Normalized HTTP response data used by adapters and contract tests."""

    status: int
    headers: Any
    payload: Any
    request_id: str | None = None


_HTTP_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})


def _request_id(headers) -> str | None:
    if headers is None:
        return None
    for name in ("x-request-id", "request-id", "x-goog-request-id", "x-amzn-requestid", "x-ms-request-id"):
        value = headers.get(name)
        if value:
            return str(value)[:128]
    return None


def _error_category(status: int, body: Any = None) -> str:
    if status == 401:
        return STTErrorCategory.AUTHENTICATION
    if status == 403:
        lowered = json.dumps(body, default=str).lower() if body is not None else ""
        if any(token in lowered for token in ("billing", "payment", "insufficient_quota", "free quota")):
            return STTErrorCategory.BILLING_REQUIRED
        return STTErrorCategory.PERMISSION
    if status == 429:
        return STTErrorCategory.RATE_LIMITED
    if status == 413:
        return STTErrorCategory.FILE_TOO_LARGE
    if status == 415:
        return STTErrorCategory.FORMAT_UNSUPPORTED
    if status in {408, 425}:
        return STTErrorCategory.PROVIDER_TIMEOUT if status == 408 else STTErrorCategory.RATE_LIMITED
    if status in {500, 502, 503, 504} or status >= 500:
        return STTErrorCategory.SERVER_ERROR
    if status in {400, 422}:
        lowered = json.dumps(body, default=str).lower() if body is not None else ""
        if any(token in lowered for token in ("language", "locale", "unsupported language")):
            return STTErrorCategory.LANGUAGE_UNSUPPORTED
        if any(token in lowered for token in ("model", "retired", "deprecated", "not available")):
            return STTErrorCategory.MODEL_UNAVAILABLE
        if any(token in lowered for token in ("duration", "too long", "maximum duration")):
            return STTErrorCategory.DURATION_TOO_LONG
        if any(token in lowered for token in ("file size", "too large", "maximum file")):
            return STTErrorCategory.FILE_TOO_LARGE
        return STTErrorCategory.INVALID_REQUEST
    return STTErrorCategory.UNKNOWN


def _retry_after(headers) -> float | None:
    if headers is None:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw:
        try:
            seconds = float(str(raw).strip())
            if math.isfinite(seconds):
                return min(max(seconds, 0.0), 604_800.0)
        except (ValueError, TypeError):
            pass
    return None


def _raise_http(provider: str, model: str, status: int, headers=None, body=None) -> None:
    category = _error_category(status, body)
    retryable = status in _HTTP_RETRYABLE
    safe = {
        STTErrorCategory.AUTHENTICATION: "Provider rejected the credential.",
        STTErrorCategory.PERMISSION: "Provider denied this operation.",
        STTErrorCategory.QUOTA_EXHAUSTED: "Provider quota is exhausted.",
        STTErrorCategory.RATE_LIMITED: "Provider rate limit reached.",
        STTErrorCategory.BILLING_REQUIRED: "Provider requires billing for this request.",
        STTErrorCategory.MODEL_UNAVAILABLE: "Requested provider model is unavailable.",
        STTErrorCategory.LANGUAGE_UNSUPPORTED: "Provider does not support the requested language.",
        STTErrorCategory.FORMAT_UNSUPPORTED: "Provider does not support this audio format.",
        STTErrorCategory.FILE_TOO_LARGE: "Audio file exceeds the provider upload limit.",
        STTErrorCategory.DURATION_TOO_LONG: "Audio duration exceeds the provider request limit.",
        STTErrorCategory.PROVIDER_TIMEOUT: "Provider request timed out.",
        STTErrorCategory.SERVER_ERROR: "Provider returned a server error.",
        STTErrorCategory.INVALID_REQUEST: "Provider rejected the transcription request.",
        STTErrorCategory.UNKNOWN: "Provider request failed.",
    }.get(category, "Provider request failed.")
    raise ProviderSTTError(
        category=category,
        retryable=retryable,
        http_status=status,
        retry_after=_retry_after(headers),
        request_id=_request_id(headers),
        provider=provider,
        model=model,
        safe_message=f"{safe} (HTTP {status})",
    )


def _mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    known = {
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        ".mp4": "audio/mp4", ".mpeg": "audio/mpeg", ".mpga": "audio/mpeg",
        ".ogg": "audio/ogg", ".opus": "audio/opus", ".flac": "audio/flac",
        ".webm": "audio/webm", ".aiff": "audio/aiff", ".aac": "audio/aac",
    }
    return known.get(ext) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def get_audio_duration_seconds(path: Path) -> float | None:
    """Read container metadata cheaply (no decoding, no temporary re-encode)."""
    try:
        import av

        with av.open(str(path)) as container:
            if container.duration is not None:
                seconds = float(container.duration) / float(av.time_base)
                if math.isfinite(seconds) and seconds > 0:
                    return seconds
            for stream in container.streams.audio:
                if stream.duration is not None and stream.time_base is not None:
                    seconds = float(stream.duration * stream.time_base)
                    if math.isfinite(seconds) and seconds > 0:
                        return seconds
    except Exception:
        pass
    # WAV fallback for test doubles/minimal deployments.
    try:
        import wave

        with wave.open(str(path), "rb") as wav:
            duration = wav.getnframes() / float(wav.getframerate())
            return duration if math.isfinite(duration) and duration > 0 else None
    except Exception:
        return None


def resolve_language(provider: str, requested: str, *, model: str = "") -> str | None:
    """Provider-specific language mapping; ``auto``/``multi`` are never sent verbatim.

    ``None`` means omit the provider field and use its documented language
    identification behaviour.
    """
    value = (requested or "").strip()
    if not value:
        raise ProviderSTTError(
            STTErrorCategory.INVALID_REQUEST, False, None, None, None, provider, model,
            "STT_LANGUAGE is not configured.",
        )
    lowered = value.lower()
    if lowered in {"auto", "multi"}:
        # Speechmatics has its own semantics, handled in stt.py.
        if provider == "gemini_transcribe":
            return None  # omitting language_codes enables language detection + code switching
        if provider in {"groq", "openai_compatible", "elevenlabs_scribe"}:
            return None  # documented automatic detection is omission, not language=auto
        if provider == "assemblyai":
            return None  # language_detection=true and/or omission
        if provider == "gladia":
            return None  # language_config.languages=[]
        if provider == "soniox":
            return None  # omit hints
        if provider == "google_cloud_stt":
            return None  # v1 autoDetectDecodingConfig, no languageCode
        if provider == "ibm_watson_stt":
            raise ProviderSTTError(
                STTErrorCategory.LANGUAGE_UNSUPPORTED, False, None, None, None, provider, model,
                "IBM STT requires a language-specific model; auto-detection is not configured.",
            )
        if provider == "azure_speech":
            return None  # request builder uses continuous language identification
    if re.fullmatch(r"[a-z]{2,3}(?:_[a-z]{2,3}){1,3}", lowered):
        raise ProviderSTTError(
            STTErrorCategory.LANGUAGE_UNSUPPORTED, False, None, None, None, provider, model,
            "Speechmatics multilingual packs are not valid for this provider.",
        )
    base = lowered.split("-")[0]
    if provider == "gemini_transcribe":
        if base == "fa":
            return "fa-IR"
        if base == "en":
            return "en-US" if "us" in lowered or lowered == "en" else value
        return value
    if provider == "google_cloud_stt":
        if base == "fa":
            return "fa-IR"
        return value if "-" in value else {"en": "en-US"}.get(base, value)
    if provider == "ibm_watson_stt":
        if base == "fa":
            return "fa-IR_BroadbandModel"
        if base == "en":
            return "en-US_BroadbandModel" if "us" in lowered or lowered == "en" else f"{value}_BroadbandModel"
        return f"{value}_BroadbandModel"
    if provider == "aws_transcribe":
        if base == "fa":
            return "fa-IR"
        if base == "en":
            return "en-US" if lowered in {"en", "en-us"} else value
        return value
    if provider in {"assemblyai", "gladia", "soniox"}:
        return base  # these APIs use ISO-639-1 codes
    if provider == "azure_speech":
        if base == "fa":
            return "fa-IR"
        return value if "-" in value else {"en": "en-US"}.get(base, value)
    if provider in {"groq", "openai_compatible"}:
        return base  # Whisper documents ISO-639-1, e.g. fa
    if provider == "elevenlabs_scribe":
        return base  # Scribe accepts ISO-639-1/3
    return value


def vocabulary_hints(settings) -> VocabularyHints:
    """Provider-neutral hints, preserving the Speechmatics vocabulary config."""
    explicit = getattr(settings, "stt_vocabulary_terms", ()) or ()
    if not explicit:
        explicit = getattr(settings, "speechmatics_additional_vocab", ()) or ()
    cleaned = tuple(dict.fromkeys(str(term).strip() for term in explicit if str(term).strip()))
    return VocabularyHints(terms=cleaned)


def extract_groq_quota(headers, *, model: str = "") -> list[QuotaObservation]:
    """Parse documented Groq rate-limit headers and bind them to Groq."""
    return [
        QuotaObservation(
            provider="groq", model=model, quota_type=row.quota_type,
            limit=row.limit, used=row.used, remaining=row.remaining,
            reset_at=row.reset_at, source=row.source,
            observed_at=row.observed_at,
        )
        for row in parse_groq_headers(headers)
    ]


def _mean(values: list[float]) -> float | None:
    valid = [value for value in values if math.isfinite(value) and 0 <= value <= 1]
    return sum(valid) / len(valid) if valid else None


def _duration_ms(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip().lower().removesuffix("s")
        try:
            return float(raw) * 1000
        except ValueError:
            return None
    try:
        number = float(value)
        # Providers use ms integers (AssemblyAI) and seconds floats (most others).
        return number if number > 10_000 else number * 1000
    except (TypeError, ValueError):
        return None


def _safe_text(value: Any, provider: str, model: str, request_id: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderSTTError(
            STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, None, None, request_id,
            provider, model, "Provider response did not contain a usable transcript.",
        )
    return value.strip()


def normalize_groq(payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
    if isinstance(payload, str):
        return Transcript("groq", _safe_text(payload, "groq", model, request_id), None,
                          provider="groq", model=model, request_id=request_id)
    if not isinstance(payload, dict):
        raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, None, None,
                               request_id, "groq", model, "Provider response had an invalid shape.")
    text = _safe_text(payload.get("text"), "groq", model, request_id)
    words: list[TranscriptWord] = []
    segments: list[TranscriptSegment] = []
    for item in payload.get("words") or []:
        if not isinstance(item, dict):
            continue
        try:
            words.append(TranscriptWord(
                text=str(item.get("word") or ""), start=float(item.get("start") or 0),
                end=float(item.get("end") or 0),
                confidence=_float_confidence(item.get("confidence")),
            ))
        except (TypeError, ValueError):
            continue
    for item in payload.get("segments") or []:
        if not isinstance(item, dict):
            continue
        try:
            segments.append(TranscriptSegment(
                start=float(item.get("start") or 0), end=float(item.get("end") or 0),
                text=str(item.get("text") or ""),
            ))
        except (TypeError, ValueError):
            continue
    language = payload.get("language")
    duration = payload.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return Transcript(
        "groq", text, None, provider="groq", model=model,
        language=str(language) if language else None,
        segments=tuple(segments), words=tuple(words), duration_seconds=duration,
        request_id=request_id, usage=_usage_dict(payload),
        metadata={"confidence_comparable": False, "response_format": "verbose_json" if segments or words else "json"},
    )


def normalize_gemini(payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
    if not isinstance(payload, dict):
        raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, None, None,
                               request_id, "gemini_transcribe", model,
                               "Gemini Interactions response had an invalid shape.")
    if str(payload.get("status", "")).lower() in {"failed", "cancelled", "error"}:
        raise ProviderSTTError(STTErrorCategory.SERVER_ERROR, False, None, None,
                               request_id, "gemini_transcribe", model,
                               "Gemini transcription interaction did not complete.")
    text = payload.get("output_text")
    if not isinstance(text, str):
        parts = []
        for output in payload.get("outputs") or payload.get("output") or []:
            if isinstance(output, dict) and output.get("type") == "text" and isinstance(output.get("text"), str):
                parts.append(output["text"])
        if not parts:
            for step in payload.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                for item in step.get("content") or []:
                    if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                        parts.append(item["text"])
        text = "\n".join(parts)
    text = _safe_text(text, "gemini_transcribe", model, request_id)
    words: list[TranscriptWord] = []
    speakers: set[str] = set()
    for step in payload.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for content in step.get("content") or []:
            if not isinstance(content, dict):
                continue
            for annotation in content.get("annotations") or []:
                if not isinstance(annotation, dict) or annotation.get("type") != "word_info":
                    continue
                speaker = annotation.get("speaker")
                if speaker:
                    speakers.add(str(speaker))
                start = _duration_ms(annotation.get("start_offset"))
                end = _duration_ms(annotation.get("end_offset"))
                words.append(TranscriptWord(
                    text=str(annotation.get("text") or ""),
                    start=(start or 0) / 1000,
                    end=(end or 0) / 1000,
                    speaker=str(speaker) if speaker else None,
                ))
    usage = _usage_dict(payload)
    return Transcript(
        "gemini_transcribe", text, None, provider="gemini_transcribe", model=model,
        words=tuple(words), speakers=tuple(sorted(speakers)), request_id=request_id,
        usage=usage,
        metadata={"confidence_comparable": False, "confidence_method": "not_provided"},
    )


def _float_confidence(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 1 else None
    except (TypeError, ValueError):
        return None


def _usage_dict(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    for name in ("usage", "usage_metadata", "usageMetadata"):
        value = payload.get(name)
        if isinstance(value, dict):
            # Keep only numeric usage counters, never raw objects/headers.
            return {str(key): val for key, val in value.items() if isinstance(val, (int, float))}
    return {}


def normalize_assemblyai(payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
    if not isinstance(payload, dict):
        raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, None, None,
                               request_id, "assemblyai", model, "AssemblyAI response had an invalid shape.")
    if payload.get("status") == "error":
        raise ProviderSTTError(STTErrorCategory.QUALITY_FAILURE, False, None, None,
                               request_id, "assemblyai", model, "AssemblyAI could not transcribe this audio.")
    text = _safe_text(payload.get("text"), "assemblyai", model, request_id)
    words: list[TranscriptWord] = []
    for word in payload.get("words") or []:
        if not isinstance(word, dict):
            continue
        try:
            words.append(TranscriptWord(
                text=str(word.get("text") or ""),
                start=float(word.get("start") or 0) / 1000,
                end=float(word.get("end") or 0) / 1000,
                confidence=_float_confidence(word.get("confidence")),
                speaker=str(word["speaker"]) if word.get("speaker") is not None else None,
            ))
        except (TypeError, ValueError):
            continue
    segments: list[TranscriptSegment] = []
    for utterance in payload.get("utterances") or []:
        if not isinstance(utterance, dict):
            continue
        try:
            segments.append(TranscriptSegment(
                start=float(utterance.get("start") or 0) / 1000,
                end=float(utterance.get("end") or 0) / 1000,
                text=str(utterance.get("text") or ""),
                speaker=str(utterance["speaker"]) if utterance.get("speaker") is not None else None,
                confidence=_float_confidence(utterance.get("confidence")),
            ))
        except (TypeError, ValueError):
            continue
    confidence = _float_confidence(payload.get("confidence"))
    language = payload.get("language_code")
    duration = payload.get("audio_duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return Transcript(
        "assemblyai", text, confidence, provider="assemblyai", model=model,
        language=str(language) if language else None,
        language_confidence=_float_confidence(payload.get("language_confidence")),
        segments=tuple(segments), words=tuple(words),
        speakers=tuple(sorted({w.speaker for w in words if w.speaker})),
        duration_seconds=duration, request_id=request_id,
        metadata={"confidence_method": "assemblyai_transcript_confidence", "confidence_comparable": False},
    )


def normalize_gladia(payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
    if not isinstance(payload, dict):
        raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, None, None,
                               request_id, "gladia", model, "Gladia response had an invalid shape.")
    if payload.get("status") in {"error", "failed"} or payload.get("error_code"):
        raise ProviderSTTError(STTErrorCategory.QUALITY_FAILURE, False, None, None,
                               request_id, "gladia", model, "Gladia could not transcribe this audio.")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    transcription = result.get("transcription") if isinstance(result, dict) else None
    transcription = transcription if isinstance(transcription, dict) else {}
    text = transcription.get("full_transcript") or payload.get("text")
    text = _safe_text(text, "gladia", model, request_id)
    segments: list[TranscriptSegment] = []
    words: list[TranscriptWord] = []
    speakers: set[str] = set()
    for utterance in transcription.get("utterances") or []:
        if not isinstance(utterance, dict):
            continue
        speaker = utterance.get("speaker")
        if speaker is not None:
            speakers.add(str(speaker))
        try:
            segments.append(TranscriptSegment(
                start=float(utterance.get("start") or 0) / 1000,
                end=float(utterance.get("end") or 0) / 1000,
                text=str(utterance.get("text") or ""),
                speaker=str(speaker) if speaker is not None else None,
                confidence=_float_confidence(utterance.get("confidence")),
            ))
        except (TypeError, ValueError):
            continue
        for word in utterance.get("words") or []:
            if not isinstance(word, dict):
                continue
            try:
                words.append(TranscriptWord(
                    text=str(word.get("word") or ""),
                    start=float(word.get("start") or 0) / 1000,
                    end=float(word.get("end") or 0) / 1000,
                    confidence=_float_confidence(word.get("confidence")),
                    speaker=str(speaker) if speaker is not None else None,
                ))
            except (TypeError, ValueError):
                continue
    meta = result.get("metadata") if isinstance(result, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    duration = meta.get("audio_duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    confidences = [segment.confidence for segment in segments if segment.confidence is not None]
    return Transcript(
        "gladia", text, _mean(confidences), provider="gladia", model=model,
        language=(transcription.get("languages") or [None])[0],
        segments=tuple(segments), words=tuple(words), speakers=tuple(sorted(speakers)),
        duration_seconds=duration, request_id=request_id,
        metadata={"confidence_method": "gladia_mean_utterance", "confidence_comparable": False},
    )


def normalize_google_cloud(payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
    response = payload.get("response", payload) if isinstance(payload, dict) else None
    if not isinstance(response, dict):
        raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, None, None,
                               request_id, "google_cloud_stt", model, "Google Cloud response had an invalid shape.")
    results = response.get("results") or []
    parts: list[str] = []
    words: list[TranscriptWord] = []
    confidences: list[float] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        alternatives = item.get("alternatives") or []
        if not alternatives or not isinstance(alternatives[0], dict):
            continue
        alternative = alternatives[0]
        if alternative.get("transcript"):
            parts.append(str(alternative["transcript"]))
        confidence = _float_confidence(alternative.get("confidence"))
        if confidence is not None:
            confidences.append(confidence)
        for word in alternative.get("words") or []:
            if not isinstance(word, dict):
                continue
            start = _duration_ms(word.get("startTime"))
            end = _duration_ms(word.get("endTime"))
            words.append(TranscriptWord(
                text=str(word.get("word") or ""), start=(start or 0) / 1000,
                end=(end or 0) / 1000,
                speaker=str(word["speakerTag"]) if word.get("speakerTag") else None,
                confidence=_float_confidence(word.get("confidence")),
            ))
    text = _safe_text(" ".join(parts), "google_cloud_stt", model, request_id)
    return Transcript(
        "google_cloud_stt", text, _mean(confidences), provider="google_cloud_stt", model=model,
        words=tuple(words),
        speakers=tuple(sorted({word.speaker for word in words if word.speaker})),
        request_id=request_id,
        metadata={"confidence_method": "google_cloud_mean_alternative", "confidence_comparable": False},
    )


def normalize_ibm(payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, None, None,
                               request_id, "ibm_watson_stt", model, "IBM response had an invalid shape.")
    text_parts: list[str] = []
    words: list[TranscriptWord] = []
    confidence_values: list[float] = []
    for result in payload["results"]:
        if not isinstance(result, dict):
            continue
        alternatives = result.get("alternatives") or []
        if not alternatives or not isinstance(alternatives[0], dict):
            continue
        alternative = alternatives[0]
        if alternative.get("transcript"):
            text_parts.append(str(alternative["transcript"]))
        conf = _float_confidence(alternative.get("confidence"))
        if conf is not None:
            confidence_values.append(conf)
        for item in alternative.get("timestamps") or []:
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                try:
                    words.append(TranscriptWord(str(item[0]), float(item[1]), float(item[2])))
                except (TypeError, ValueError):
                    pass
    text = _safe_text(" ".join(text_parts), "ibm_watson_stt", model, request_id)
    return Transcript(
        "ibm_watson_stt", text, _mean(confidence_values), provider="ibm_watson_stt", model=model,
        words=tuple(words), request_id=request_id,
        metadata={"confidence_method": "ibm_mean_alternative", "confidence_comparable": False},
    )


def normalize_azure(payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
    if not isinstance(payload, dict):
        raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, None, None,
                               request_id, "azure_speech", model, "Azure response had an invalid shape.")
    phrases = payload.get("phrases") or []
    text = " ".join(
        str(item.get("text") or "") for item in phrases if isinstance(item, dict) and item.get("text")
    )
    if not text:
        combined = payload.get("combinedPhrases") or []
        text = " ".join(
            str(item.get("text") or "") for item in combined if isinstance(item, dict) and item.get("text")
        )
    text = _safe_text(text, "azure_speech", model, request_id)
    words: list[TranscriptWord] = []
    segments: list[TranscriptSegment] = []
    for phrase in phrases:
        if not isinstance(phrase, dict):
            continue
        offset = _duration_ms(phrase.get("offsetInTicks"))
        duration = _duration_ms(phrase.get("durationInTicks"))
        start = (offset or 0) / 1000
        end = start + (duration or 0) / 1000
        speaker = phrase.get("speaker")
        segments.append(TranscriptSegment(start, end, str(phrase.get("text") or ""),
                                          str(speaker) if speaker is not None else None))
        for word in phrase.get("words") or []:
            if not isinstance(word, dict):
                continue
            start_ms = _duration_ms(word.get("offsetInTicks"))
            dur_ms = _duration_ms(word.get("durationInTicks"))
            words.append(TranscriptWord(
                str(word.get("text") or ""), (start_ms or 0) / 1000,
                ((start_ms or 0) + (dur_ms or 0)) / 1000,
                speaker=str(speaker) if speaker is not None else None,
            ))
    language = payload.get("locale")
    return Transcript(
        "azure_speech", text, None, provider="azure_speech", model=model,
        language=str(language) if language else None, segments=tuple(segments), words=tuple(words),
        speakers=tuple(sorted({word.speaker for word in words if word.speaker})),
        request_id=request_id, metadata={"confidence_method": "not_provided", "confidence_comparable": False},
    )


def normalize_soniox(payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
    if not isinstance(payload, dict):
        raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, None, None,
                               request_id, "soniox", model, "Soniox response had an invalid shape.")
    tokens = payload.get("tokens") or []
    if not isinstance(tokens, list):
        tokens = []
    text_parts: list[str] = []
    words: list[TranscriptWord] = []
    speakers: set[str] = set()
    confidences: list[float] = []
    language = None
    for token in tokens:
        if not isinstance(token, dict):
            continue
        text = str(token.get("text") or "")
        text_parts.append(text)
        if token.get("language"):
            language = str(token["language"])
        speaker = token.get("speaker")
        if speaker is not None:
            speakers.add(str(speaker))
        confidence = _float_confidence(token.get("confidence"))
        if confidence is not None:
            confidences.append(confidence)
        start = token.get("start_ms")
        end = token.get("end_ms")
        if text and (start is not None or end is not None):
            try:
                words.append(TranscriptWord(text, float(start or 0) / 1000,
                                           float(end or 0) / 1000, confidence,
                                           str(speaker) if speaker is not None else None))
            except (TypeError, ValueError):
                pass
    text = _safe_text("".join(text_parts), "soniox", model, request_id)
    return Transcript(
        "soniox", text, _mean(confidences), provider="soniox", model=model,
        language=language, words=tuple(words), speakers=tuple(sorted(speakers)), request_id=request_id,
        metadata={"confidence_method": "soniox_mean_token", "confidence_comparable": False},
    )


def normalize_elevenlabs(payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
    if not isinstance(payload, dict):
        raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, None, None,
                               request_id, "elevenlabs_scribe", model, "ElevenLabs response had an invalid shape.")
    text = _safe_text(payload.get("text"), "elevenlabs_scribe", model, request_id)
    raw_words = payload.get("words") or []
    words: list[TranscriptWord] = []
    speakers: set[str] = set()
    for item in raw_words:
        if not isinstance(item, dict):
            continue
        speaker = item.get("speaker_id")
        if speaker:
            speakers.add(str(speaker))
        start, end = item.get("start", 0), item.get("end", 0)
        try:
            words.append(TranscriptWord(
                text=str(item.get("text") or ""), start=float(start or 0), end=float(end or 0),
                speaker=str(speaker) if speaker else None,
            ))
        except (TypeError, ValueError):
            continue
    language = payload.get("language_code")
    language_conf = _float_confidence(payload.get("language_probability"))
    return Transcript(
        "elevenlabs_scribe", text, None, provider="elevenlabs_scribe", model=model,
        language=str(language) if language else None, language_confidence=language_conf,
        words=tuple(words), speakers=tuple(sorted(speakers)), request_id=request_id,
        metadata={"confidence_method": "not_provided", "confidence_comparable": False},
    )


class STTProviderAdapter:
    """Provider-specific request builder/response normalizer.

    Concrete adapters share status/error normalization but never share a
    provider-specific payload shape. ``transcribe`` is implemented by the
    concrete protocol adapter or by :class:`NativeSTTAdapter` dispatch.
    """

    provider: str
    default_model: str

    def build_preview(self, settings, audio_path: Path, *, options: AdapterOptions | None = None) -> RequestPreview:
        raise NotImplementedError

    def normalize_response(self, payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
        raise NotImplementedError

    def extract_quota(self, headers, *, model: str) -> list[QuotaObservation]:
        return []


class NativeSTTAdapter(STTProviderAdapter):
    """Native REST adapters for STT providers added to the platform."""

    def __init__(self, provider: str):
        if provider not in STT_PROVIDER_REGISTRY:
            raise KeyError(provider)
        self.provider = provider
        info = STT_PROVIDER_REGISTRY[provider]
        self.default_model = info.default_model

    def model_for(self, settings) -> str:
        return str(getattr(settings, "stt_model", lambda _p: None)(self.provider)
                   or self.default_model)

    def key_for(self, settings) -> str:
        return str(getattr(settings, "stt_api_key", lambda _p: None)(self.provider) or "")

    def base_for(self, settings) -> str:
        return str(getattr(settings, "stt_base_url", lambda _p: None)(self.provider)
                   or STT_PROVIDER_REGISTRY[self.provider].base_url or "").rstrip("/")

    def get_url(self, settings, suffix: str) -> str:
        return f"{self.base_for(settings)}/{suffix.lstrip('/')}"

    def build_preview(self, settings, audio_path: Path, *, options: AdapterOptions | None = None) -> RequestPreview:
        options = options or AdapterOptions()
        model = self.model_for(settings)
        language = resolve_language(self.provider, settings.stt_language, model=model)
        size = audio_path.stat().st_size
        features = tuple(
            name for name, enabled in (
                ("diarization", options.diarization),
                ("word_timestamps", options.word_timestamps),
                ("smart_transcription", options.smart_transcription),
                ("vocabulary", bool(options.vocabulary.terms)),
            ) if enabled
        )
        info = STT_PROVIDER_REGISTRY[self.provider]
        endpoint = self.base_for(settings)
        method = "POST"
        fields: list[tuple[str, str]] = []
        payload = ""
        if self.provider == "groq":
            endpoint = self.get_url(settings, "audio/transcriptions")
            fields.extend((("model", model), ("temperature", "0.0"), ("response_format", "json")))
            if language:
                fields.append(("language", language))
            if options.vocabulary.terms:
                fields.append(("prompt", ", ".join(options.vocabulary.terms[:100])))
            payload = "multipart: file=<audio file>, model, language?, prompt?, response_format=json, temperature=0.0"
        elif self.provider == "gemini_transcribe":
            endpoint = self.get_url(settings, "interactions")
            fields.append(("model", model))
            if language:
                fields.append(("language_codes", language))
            payload = "Interactions JSON; audio URI references a Files API upload; transcription_config"
        elif self.provider == "assemblyai":
            endpoint = self.get_url(settings, "transcript")
            if language:
                fields.append(("language_code", language))
            fields.extend((("punctuate", "true"), ("format_text", "true"), ("speaker_labels", str(options.diarization).lower())))
            payload = "JSON: audio_url, speech_models, language_code?, keyterms_prompt?, punctuate, format_text, speaker_labels"
        elif self.provider == "gladia":
            endpoint = self.get_url(settings, "pre-recorded")
            if language:
                fields.append(("language_config.languages", language))
            payload = "Upload then JSON: audio_url, model, language_config, diarization=false, custom_vocabulary?"
        elif self.provider == "google_cloud_stt":
            endpoint = self.get_url(settings, "speech:longrunningrecognize")
            if language:
                fields.append(("languageCode", language))
            fields.append(("model", model))
            payload = "Google Cloud v1 LongRunningRecognize JSON: config + audio.content(base64)"
        elif self.provider == "ibm_watson_stt":
            endpoint = self.base_for(settings).replace("{region}", str(getattr(settings, "stt_region", lambda _p: "us-south")(self.provider))) + "/v1/recognize"
            if language:
                fields.append(("model", language))
            fields.extend((("timestamps", "true" if options.word_timestamps else "false"), ("word_confidence", "true")))
            payload = "audio bytes; query model, timestamps, word_confidence"
        elif self.provider == "azure_speech":
            region = str(getattr(settings, "stt_region", lambda _p: "")(self.provider) or "").strip()
            base = self.base_for(settings).replace("{region}", region)
            endpoint = base.rstrip("/") + "/speechtotext/transcriptions:transcribe?api-version=2024-11-15"
            fields.append(("definition.locale", language or "auto"))
            payload = "multipart: audio file + definition JSON (locale, diarization/phraseList options)"
        elif self.provider == "soniox":
            endpoint = self.get_url(settings, "v1/transcriptions")
            if language:
                fields.append(("language_hints", language))
            payload = "Upload to Files API, then async JSON: model, file_id, language_hints?, context?"
        elif self.provider == "elevenlabs_scribe":
            endpoint = self.get_url(settings, "speech-to-text")
            if language:
                fields.append(("language_code", language))
            fields.extend((("timestamps_granularity", "word" if options.word_timestamps else "none"),
                           ("diarize", str(options.diarization).lower()),
                           ("model_id", model)))
            payload = "multipart: file, model_id, language_code?, timestamps_granularity=none, diarize=false"
        else:
            payload = "provider-native request"
        return RequestPreview(
            provider=self.provider, model=model, endpoint=endpoint, method=method,
            protocol=info.protocol, headers_redacted=(("Authorization", "<redacted>"),),
            audio_bytes=size, audio_duration_seconds=get_audio_duration_seconds(audio_path),
            language=language, requested_features=features, form_fields=tuple(fields),
            payload_shape=payload,
        )

    def normalize_response(self, payload: Any, *, model: str, request_id: str | None = None) -> Transcript:
        normalizers = {
            "groq": normalize_groq,
            "gemini_transcribe": normalize_gemini,
            "assemblyai": normalize_assemblyai,
            "gladia": normalize_gladia,
            "google_cloud_stt": normalize_google_cloud,
            "ibm_watson_stt": normalize_ibm,
            "azure_speech": normalize_azure,
            "soniox": normalize_soniox,
            "elevenlabs_scribe": normalize_elevenlabs,
        }
        normalizer = normalizers.get(self.provider)
        if not normalizer:
            raise ProviderSTTError(STTErrorCategory.MODEL_UNAVAILABLE, False, None, None,
                                   request_id, self.provider, model, "Adapter is not implemented for this provider.")
        return normalizer(payload, model=model, request_id=request_id)

    def extract_quota(self, headers, *, model: str) -> list[QuotaObservation]:
        if self.provider == "groq":
            return extract_groq_quota(headers, model=model)
        # Do not manufacture remaining quotas when a provider does not expose them.
        return []

    async def transcribe(
        self,
        session: aiohttp.ClientSession,
        audio_path: Path,
        settings,
        *,
        model: str | None = None,
        options: AdapterOptions | None = None,
        quota_tracker=None,
    ) -> Transcript:
        options = options or AdapterOptions(vocabulary=vocabulary_hints(settings))
        model = model or self.model_for(settings)
        key = self.key_for(settings)
        if not key:
            raise ProviderSTTError(STTErrorCategory.AUTHENTICATION, False, 401, None, None,
                                   self.provider, model, "Provider credential is not configured.")
        size = audio_path.stat().st_size
        info = STT_PROVIDER_REGISTRY[self.provider]
        if size > info.max_file_size:
            raise ProviderSTTError(STTErrorCategory.FILE_TOO_LARGE, False, 413, None, None,
                                   self.provider, model, "Audio file exceeds the provider upload limit.")
        duration = get_audio_duration_seconds(audio_path)
        if duration and info.max_audio_duration and duration > info.max_audio_duration:
            raise ProviderSTTError(STTErrorCategory.DURATION_TOO_LONG, False, 400, None, None,
                                   self.provider, model, "Audio duration exceeds the provider request limit.")
        if self.provider == "groq":
            result, headers, status, req_id = await self._groq(session, audio_path, settings, key, model, options)
        elif self.provider == "gemini_transcribe":
            result, headers, status, req_id = await self._gemini(session, audio_path, settings, key, model, options)
        elif self.provider == "assemblyai":
            result, headers, status, req_id = await self._assemblyai(session, audio_path, settings, key, model, options)
        elif self.provider == "gladia":
            result, headers, status, req_id = await self._gladia(session, audio_path, settings, key, model, options)
        elif self.provider == "google_cloud_stt":
            result, headers, status, req_id = await self._google_cloud(session, audio_path, settings, key, model, options)
        elif self.provider == "ibm_watson_stt":
            result, headers, status, req_id = await self._ibm(session, audio_path, settings, key, model, options)
        elif self.provider == "azure_speech":
            result, headers, status, req_id = await self._azure(session, audio_path, settings, key, model, options)
        elif self.provider == "soniox":
            result, headers, status, req_id = await self._soniox(session, audio_path, settings, key, model, options)
        elif self.provider == "elevenlabs_scribe":
            result, headers, status, req_id = await self._elevenlabs(session, audio_path, settings, key, model, options)
        else:
            raise ProviderSTTError(STTErrorCategory.MODEL_UNAVAILABLE, False, None, None,
                                   None, self.provider, model, "Provider adapter is disabled.")
        observations = self.extract_quota(headers, model=model)
        if quota_tracker is not None and observations:
            await quota_tracker.record_many(observations)
        # Replace request ID with the one associated with the transcription
        # response when the normalizer did not already receive it.
        if result.request_id is None and req_id:
            from dataclasses import replace

            result = replace(result, request_id=req_id)
        return result

    async def _send_json(self, session, method: str, url: str, *, headers: dict,
                         json_body: dict | None = None, data=None, provider: str,
                         model: str, timeout: int = 3600) -> tuple[Any, Any, int, str | None]:
        try:
            async with session.request(
                method, url, headers=headers, json=json_body, data=data,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                request_id = _request_id(response.headers)
                if response.status < 200 or response.status >= 300:
                    try:
                        body = await response.json(content_type=None)
                    except Exception:
                        body = None
                    _raise_http(provider, model, response.status, response.headers, body)
                if response.content_type == "text/plain":
                    payload = await response.text()
                else:
                    payload = await response.json(content_type=None)
                return payload, dict(response.headers), int(response.status), request_id
        except ProviderSTTError:
            raise
        except asyncio.TimeoutError as exc:
            raise ProviderSTTError(
                STTErrorCategory.PROVIDER_TIMEOUT, True, None, None, None,
                provider, model, "Provider request timed out.",
            ) from exc
        except aiohttp.ClientError as exc:
            raise ProviderSTTError(
                STTErrorCategory.NETWORK_FAILURE, True, None, None, None,
                provider, model, "Network failure while contacting the STT provider.",
            ) from exc

    async def _groq(self, session, audio_path, settings, key, model, options):
        language = resolve_language("groq", settings.stt_language, model=model)
        form = aiohttp.FormData()
        form.add_field("model", model)
        if language:
            form.add_field("language", language)
        form.add_field("temperature", "0.0")
        timestamps = bool(options.word_timestamps)
        response_format = "verbose_json" if timestamps else "json"
        form.add_field("response_format", response_format)
        if timestamps:
            # timestamp_granularities is only legal with verbose_json.
            form.add_field("timestamp_granularities[]", "word")
        if options.vocabulary.terms:
            prompt = ", ".join(options.vocabulary.terms[:100])
            form.add_field("prompt", prompt[:1000])
        with audio_path.open("rb") as audio:
            form.add_field("file", audio, filename=audio_path.name,
                           content_type=_mime_type(audio_path))
            payload, headers, status, req_id = await self._send_json(
                session, "POST", self.get_url(settings, "audio/transcriptions"),
                headers={"Authorization": f"Bearer {key}"}, data=form,
                provider="groq", model=model,
                timeout=int(getattr(settings, "stt_job_timeout", 3600)),
            )
        return normalize_groq(payload, model=model, request_id=req_id), headers, status, req_id

    async def _gemini(self, session, audio_path, settings, key, model, options):
        mime = _mime_type(audio_path)
        size = audio_path.stat().st_size
        upload_url = "https://generativelanguage.googleapis.com/upload/v1beta/files"
        # The key travels only in a header: a URL query string leaks into proxy
        # and access logs and into exception reprs.
        start_headers = {
            "x-goog-api-key": key,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        }
        try:
            async with session.post(
                upload_url, headers=start_headers,
                json={"file": {"display_name": f"gamas-stt-{uuid.uuid4().hex[:12]}"}},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                if response.status < 200 or response.status >= 300:
                    body = None
                    try:
                        body = await response.json(content_type=None)
                    except Exception:
                        pass
                    _raise_http("gemini_transcribe", model, response.status, response.headers, body)
                upload_endpoint = response.headers.get("X-Goog-Upload-URL")
                start_request_id = _request_id(response.headers)
                if not upload_endpoint:
                    raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False,
                                           response.status, None, start_request_id,
                                           "gemini_transcribe", model,
                                           "Gemini Files API did not return an upload URL.")
        except ProviderSTTError:
            raise
        except asyncio.TimeoutError as exc:
            raise ProviderSTTError(STTErrorCategory.PROVIDER_TIMEOUT, True, None, None, None,
                                   "gemini_transcribe", model, "Gemini file upload timed out.") from exc
        except aiohttp.ClientError as exc:
            raise ProviderSTTError(STTErrorCategory.NETWORK_FAILURE, True, None, None, None,
                                   "gemini_transcribe", model, "Network failure while uploading to Gemini.") from exc

        file_info: dict | None = None
        try:
            with audio_path.open("rb") as audio:
                headers = {
                    "Content-Length": str(size),
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                }
                async with session.post(
                    upload_endpoint, headers=headers, data=audio,
                    timeout=aiohttp.ClientTimeout(total=int(getattr(settings, "stt_job_timeout", 3600))),
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        body = None
                        try:
                            body = await response.json(content_type=None)
                        except Exception:
                            pass
                        _raise_http("gemini_transcribe", model, response.status, response.headers, body)
                    file_info = await response.json(content_type=None)
                    upload_req_id = _request_id(response.headers) or start_request_id
            file_obj = file_info.get("file", file_info) if isinstance(file_info, dict) else {}
            file_uri = file_obj.get("uri") if isinstance(file_obj, dict) else None
            file_name = file_obj.get("name") if isinstance(file_obj, dict) else None
            if not file_uri:
                raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False,
                                       None, None, upload_req_id, "gemini_transcribe", model,
                                       "Gemini Files API did not return a file URI.")

            trans_config: dict[str, Any] = {}
            language = resolve_language("gemini_transcribe", settings.stt_language, model=model)
            if language:
                trans_config["language_codes"] = [language]
            if options.vocabulary.terms:
                if options.diarization or options.word_timestamps:
                    raise ProviderSTTError(
                        STTErrorCategory.INVALID_REQUEST, False, None, None, upload_req_id,
                        "gemini_transcribe", model,
                        "Gemini custom vocabulary cannot be combined with diarization or word timestamps.",
                    )
                trans_config["custom_vocabulary"] = list(options.vocabulary.terms[:100])
            if options.smart_transcription:
                if options.diarization or options.word_timestamps:
                    raise ProviderSTTError(
                        STTErrorCategory.INVALID_REQUEST, False, None, None, upload_req_id,
                        "gemini_transcribe", model,
                        "Gemini smart mode cannot be combined with diarization or word timestamps.",
                    )
                trans_config["mode"] = "smart"
            elif options.diarization or options.word_timestamps:
                mode = {"type": "verbatim"}
                if options.diarization:
                    mode["diarization_mode"] = "speaker"
                if options.word_timestamps:
                    mode["timestamp_granularities"] = ["word"]
                trans_config["mode"] = mode
            body: dict[str, Any] = {
                "model": model,
                "input": [{"type": "audio", "uri": file_uri, "mime_type": mime}],
            }
            if trans_config:
                body["generation_config"] = {"transcription_config": trans_config}
            payload, headers, status, req_id = await self._send_json(
                session, "POST", self.get_url(settings, "interactions"),
                headers={"x-goog-api-key": key}, json_body=body,
                provider="gemini_transcribe", model=model,
                timeout=int(getattr(settings, "stt_job_timeout", 3600)),
            )
            transcript = normalize_gemini(payload, model=model, request_id=req_id or upload_req_id)
            return transcript, headers, status, req_id or upload_req_id
        finally:
            # Files API files persist for up to 48 h; delete promptly. Never
            # let cleanup failure hide the actual transcription result.
            if file_info:
                try:
                    file_obj = file_info.get("file", file_info) if isinstance(file_info, dict) else {}
                    file_name = file_obj.get("name") if isinstance(file_obj, dict) else None
                    if file_name:
                        async with session.delete(
                            f"https://generativelanguage.googleapis.com/v1beta/{quote(str(file_name), safe='/')}",
                            headers={"x-goog-api-key": key},
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as response:
                            await response.read()
                except Exception:
                    logger.warning("STT temporary file cleanup failed provider=gemini_transcribe")

    async def _assemblyai(self, session, audio_path, settings, key, model, options):
        base = self.base_for(settings)
        # Upload uses a raw byte stream; the API gives back a provider file URL.
        try:
            with audio_path.open("rb") as audio:
                payload, headers, status, upload_req_id = await self._send_json(
                    session, "POST", f"{base}/upload",
                    headers={"Authorization": key, "Content-Type": "application/octet-stream"},
                    data=audio, provider="assemblyai", model=model,
                    timeout=int(getattr(settings, "stt_job_timeout", 3600)),
                )
        except ProviderSTTError:
            raise
        upload_url = payload.get("upload_url") if isinstance(payload, dict) else None
        if not upload_url:
            raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, status, None,
                                   upload_req_id, "assemblyai", model,
                                   "AssemblyAI upload response did not include an upload URL.")
        language = resolve_language("assemblyai", settings.stt_language, model=model)
        speech_models = [model] if model not in {"universal", "universal-2", "universal-3-pro"} else [
            "universal-2" if model == "universal" else model
        ]
        body: dict[str, Any] = {
            "audio_url": upload_url,
            "speech_models": speech_models,
            "punctuate": True,
            "format_text": True,
            "speaker_labels": bool(options.diarization),
            "language_detection": not bool(language),
        }
        if language:
            body["language_code"] = language
        if options.vocabulary.terms:
            body["keyterms_prompt"] = list(options.vocabulary.terms[:100])
        request, headers, status, req_id = await self._send_json(
            session, "POST", f"{base}/transcript", headers={"Authorization": key},
            json_body=body, provider="assemblyai", model=model,
            timeout=int(getattr(settings, "stt_job_timeout", 3600)),
        )
        transcript_id = request.get("id") if isinstance(request, dict) else None
        if not transcript_id:
            raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, status, None,
                                   req_id, "assemblyai", model,
                                   "AssemblyAI did not return a transcription job ID.")
        try:
            deadline = time.monotonic() + int(getattr(settings, "stt_job_timeout", 3600))
            current = request
            while True:
                status_value = str(current.get("status", "")).lower()
                if status_value == "completed":
                    result = normalize_assemblyai(current, model=model, request_id=req_id or upload_req_id)
                    return result, headers, 200, req_id or upload_req_id
                if status_value == "error":
                    raise ProviderSTTError(STTErrorCategory.QUALITY_FAILURE, False, None, None,
                                           req_id, "assemblyai", model,
                                           "AssemblyAI could not transcribe this audio.")
                if time.monotonic() >= deadline:
                    raise ProviderSTTError(STTErrorCategory.PROVIDER_TIMEOUT, True, None, None,
                                           req_id, "assemblyai", model,
                                           "AssemblyAI transcription timed out.")
                await asyncio.sleep(min(max(float(getattr(settings, "stt_poll_interval", 5)), 0.1), 15))
                current, poll_headers, poll_status, poll_req_id = await self._send_json(
                    session, "GET", f"{base}/transcript/{quote(str(transcript_id), safe='')}",
                    headers={"Authorization": key}, provider="assemblyai", model=model,
                    timeout=60,
                )
                headers = poll_headers
                req_id = poll_req_id or req_id
        finally:
            # AssemblyAI provides a transcript-delete endpoint. Best effort; do
            # not mask the transcript/error if the cleanup call fails.
            try:
                async with session.delete(
                    f"{base}/transcript/{quote(str(transcript_id), safe='')}",
                    headers={"Authorization": key}, timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    await response.read()
            except Exception:
                logger.warning("STT temporary job cleanup failed provider=assemblyai")

    async def _gladia(self, session, audio_path, settings, key, model, options):
        base = self.base_for(settings)
        form = aiohttp.FormData()
        with audio_path.open("rb") as audio:
            form.add_field("audio", audio, filename=audio_path.name, content_type=_mime_type(audio_path))
            upload, headers, status, upload_req_id = await self._send_json(
                session, "POST", f"{base}/upload", headers={"x-gladia-key": key},
                data=form, provider="gladia", model=model,
                timeout=int(getattr(settings, "stt_job_timeout", 3600)),
            )
        audio_url = upload.get("audio_url") if isinstance(upload, dict) else None
        if not audio_url:
            raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, status, None,
                                   upload_req_id, "gladia", model,
                                   "Gladia upload response did not include an audio URL.")
        language = resolve_language("gladia", settings.stt_language, model=model)
        body: dict[str, Any] = {
            "audio_url": audio_url,
            "model": model,
            "diarization": bool(options.diarization),
            "language_config": {
                "languages": [language] if language else [],
                "code_switching": not bool(language),
            },
            "punctuation_enhanced": True,
            "custom_vocabulary": bool(options.vocabulary.terms),
        }
        if options.vocabulary.terms:
            body["custom_vocabulary_config"] = {
                "vocabulary": [{"value": term} for term in options.vocabulary.terms[:100]],
            }
        initial, headers, status, req_id = await self._send_json(
            session, "POST", f"{base}/pre-recorded", headers={"x-gladia-key": key},
            json_body=body, provider="gladia", model=model,
            timeout=int(getattr(settings, "stt_job_timeout", 3600)),
        )
        job_id = initial.get("id") if isinstance(initial, dict) else None
        poll_url = initial.get("url") if isinstance(initial, dict) else None
        if not job_id and not poll_url:
            raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, status, None,
                                   req_id, "gladia", model,
                                   "Gladia did not return a pre-recorded job identifier.")
        get_url = poll_url or f"{base}/pre-recorded/{quote(str(job_id), safe='')}"
        try:
            deadline = time.monotonic() + int(getattr(settings, "stt_job_timeout", 3600))
            current = initial
            while True:
                status_value = str(current.get("status", "")).lower()
                if status_value in {"done", "completed", "success"} or isinstance(current.get("result"), dict):
                    return normalize_gladia(current, model=model, request_id=req_id or upload_req_id), headers, 200, req_id or upload_req_id
                if status_value in {"error", "failed"} or current.get("error_code"):
                    raise ProviderSTTError(STTErrorCategory.QUALITY_FAILURE, False, None, None,
                                           req_id, "gladia", model,
                                           "Gladia could not transcribe this audio.")
                if time.monotonic() >= deadline:
                    raise ProviderSTTError(STTErrorCategory.PROVIDER_TIMEOUT, True, None, None,
                                           req_id, "gladia", model,
                                           "Gladia transcription timed out.")
                await asyncio.sleep(min(max(float(getattr(settings, "stt_poll_interval", 5)), 0.1), 15))
                current, headers, poll_status, poll_req = await self._send_json(
                    session, "GET", get_url, headers={"x-gladia-key": key},
                    provider="gladia", model=model, timeout=60,
                )
                req_id = poll_req or req_id
        finally:
            if job_id:
                try:
                    async with session.delete(
                        f"{base}/pre-recorded/{quote(str(job_id), safe='')}",
                        headers={"x-gladia-key": key}, timeout=aiohttp.ClientTimeout(total=10),
                    ) as response:
                        await response.read()
                except Exception:
                    logger.warning("STT temporary job cleanup failed provider=gladia")

    async def _google_cloud(self, session, audio_path, settings, key, model, options):
        language = resolve_language("google_cloud_stt", settings.stt_language, model=model)
        if audio_path.stat().st_size > 10_000_000:
            raise ProviderSTTError(STTErrorCategory.FILE_TOO_LARGE, False, 413, None, None,
                                   "google_cloud_stt", model,
                                   "Google Cloud v1 inline audio is limited to 10 MB; this adapter does not create a GCS object.")
        try:
            content = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        except OSError as exc:
            raise ProviderSTTError(STTErrorCategory.NETWORK_FAILURE, False, None, None, None,
                                   "google_cloud_stt", model, "Could not read the audio file.") from exc
        recognition: dict[str, Any] = {
            "model": model,
            "enableWordTimeOffsets": bool(options.word_timestamps),
            "enableAutomaticPunctuation": True,
        }
        if language:
            recognition["languageCode"] = language
        else:
            # The v1 schema's autoDetectDecodingConfig is used only when
            # decoding metadata can be inferred from container; keep the
            # language field omitted rather than inventing an "auto" value.
            recognition["languageCode"] = "fa-IR" if settings.stt_language.strip().lower() == "fa" else "en-US"
        if options.vocabulary.terms:
            recognition["speechContexts"] = [{
                "phrases": list(options.vocabulary.terms[:500]),
                "boost": 15.0,
            }]
        body = {"config": recognition, "audio": {"content": content}}
        headers: dict[str, str] = {"Content-Type": "application/json"}
        url = self.get_url(settings, "speech:longrunningrecognize")
        if key.startswith("ya29.") or key.count(".") >= 2:
            headers["Authorization"] = f"Bearer {key}"
        else:
            # API keys go in a header, not the URL (Google: "x-goog-api-key").
            headers["x-goog-api-key"] = key
        operation, response_headers, status, req_id = await self._send_json(
            session, "POST", url, headers=headers, json_body=body,
            provider="google_cloud_stt", model=model,
            timeout=int(getattr(settings, "stt_job_timeout", 3600)),
        )
        name = operation.get("name") if isinstance(operation, dict) else None
        if not name:
            raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, status, None,
                                   req_id, "google_cloud_stt", model,
                                   "Google Cloud did not return an operation name.")
        deadline = time.monotonic() + int(getattr(settings, "stt_job_timeout", 3600))
        poll_url = f"https://speech.googleapis.com/v1/operations/{quote(str(name), safe='/')}"
        while not operation.get("done"):
            if time.monotonic() >= deadline:
                raise ProviderSTTError(STTErrorCategory.PROVIDER_TIMEOUT, True, None, None,
                                       req_id, "google_cloud_stt", model,
                                       "Google Cloud transcription timed out.")
            await asyncio.sleep(min(max(float(getattr(settings, "stt_poll_interval", 5)), 0.1), 15))
            operation, response_headers, status, poll_req = await self._send_json(
                session, "GET", poll_url, headers={k: v for k, v in headers.items() if k != "Content-Type"},
                provider="google_cloud_stt", model=model, timeout=60,
            )
            req_id = poll_req or req_id
        if operation.get("error"):
            raise ProviderSTTError(STTErrorCategory.SERVER_ERROR, False, None, None,
                                   req_id, "google_cloud_stt", model,
                                   "Google Cloud operation completed with an error.")
        transcript = normalize_google_cloud(operation, model=model, request_id=req_id)
        return transcript, response_headers, status, req_id

    async def _ibm(self, session, audio_path, settings, key, model, options):
        language_model = resolve_language("ibm_watson_stt", settings.stt_language, model=model)
        region = str(getattr(settings, "stt_region", lambda _p: "us-south")("ibm_watson_stt") or "us-south")
        base = self.base_for(settings).replace("{region}", region)
        params = {
            "model": language_model,
            "timestamps": "true" if options.word_timestamps else "false",
            "word_confidence": "true",
        }
        headers = {"Content-Type": _mime_type(audio_path), "Accept": "application/json"}
        # IBM Cloud IAM API keys are sent using HTTP Basic auth with username
        # "apikey" and the API key as the password.
        auth = aiohttp.BasicAuth("apikey", key)
        try:
            with audio_path.open("rb") as audio:
                async with session.post(
                    f"{base}/v1/recognize", params=params, headers=headers, data=audio,
                    auth=auth, timeout=aiohttp.ClientTimeout(total=int(getattr(settings, "stt_job_timeout", 3600))),
                ) as response:
                    req_id = _request_id(response.headers)
                    if response.status < 200 or response.status >= 300:
                        body = None
                        try:
                            body = await response.json(content_type=None)
                        except Exception:
                            pass
                        _raise_http("ibm_watson_stt", model, response.status, response.headers, body)
                    payload = await response.json(content_type=None)
                    response_headers, status = dict(response.headers), int(response.status)
        except ProviderSTTError:
            raise
        except asyncio.TimeoutError as exc:
            raise ProviderSTTError(STTErrorCategory.PROVIDER_TIMEOUT, True, None, None, None,
                                   "ibm_watson_stt", model, "IBM Watson request timed out.") from exc
        except aiohttp.ClientError as exc:
            raise ProviderSTTError(STTErrorCategory.NETWORK_FAILURE, True, None, None, None,
                                   "ibm_watson_stt", model, "Network failure contacting IBM Watson.") from exc
        return normalize_ibm(payload, model=model, request_id=req_id), response_headers, status, req_id

    async def _azure(self, session, audio_path, settings, key, model, options):
        region = str(getattr(settings, "stt_region", lambda _p: "")("azure_speech") or "").strip()
        if not region:
            raise ProviderSTTError(STTErrorCategory.INVALID_REQUEST, False, None, None, None,
                                   "azure_speech", model, "Azure Speech region is required.")
        base = self.base_for(settings).replace("{region}", region)
        url = base.rstrip("/") + "/speechtotext/transcriptions:transcribe?api-version=2024-11-15"
        locale = resolve_language("azure_speech", settings.stt_language, model=model)
        definition: dict[str, Any] = {"locales": [locale] if locale else ["fa-IR"]}
        props: dict[str, Any] = {
            "wordLevelTimestampsEnabled": bool(options.word_timestamps),
            "diarizationEnabled": bool(options.diarization),
        }
        if options.vocabulary.terms:
            # PhraseList is a documented fast-transcription feature.
            props["phraseList"] = {"phrases": list(options.vocabulary.terms[:500])}
        definition["properties"] = props
        form = aiohttp.FormData()
        form.add_field("definition", json.dumps(definition), content_type="application/json")
        with audio_path.open("rb") as audio:
            form.add_field("audio", audio, filename=audio_path.name, content_type=_mime_type(audio_path))
            payload, headers, status, req_id = await self._send_json(
                session, "POST", url,
                headers={"Ocp-Apim-Subscription-Key": key}, data=form,
                provider="azure_speech", model=model,
                timeout=int(getattr(settings, "stt_job_timeout", 3600)),
            )
        return normalize_azure(payload, model=model, request_id=req_id), headers, status, req_id

    async def _soniox(self, session, audio_path, settings, key, model, options):
        base = self.base_for(settings)
        # Local files are uploaded via the official Files API, then referenced
        # by file_id in an async transcription job.
        form = aiohttp.FormData()
        with audio_path.open("rb") as audio:
            form.add_field("file", audio, filename=audio_path.name, content_type=_mime_type(audio_path))
            file_info, headers, status, upload_req = await self._send_json(
                session, "POST", f"{base}/v1/files", headers={"Authorization": f"Bearer {key}"},
                data=form, provider="soniox", model=model,
                timeout=int(getattr(settings, "stt_job_timeout", 3600)),
            )
        file_id = file_info.get("id") if isinstance(file_info, dict) else None
        if not file_id:
            raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, status, None,
                                   upload_req, "soniox", model, "Soniox upload did not return a file ID.")
        language = resolve_language("soniox", settings.stt_language, model=model)
        body: dict[str, Any] = {"model": model, "file_id": file_id}
        if language:
            body["language_hints"] = [language]
        if options.vocabulary.terms:
            body["context"] = {"terms": list(options.vocabulary.terms[:100])}
        job, headers, status, req_id = await self._send_json(
            session, "POST", f"{base}/v1/transcriptions", headers={"Authorization": f"Bearer {key}"},
            json_body=body, provider="soniox", model=model,
            timeout=int(getattr(settings, "stt_job_timeout", 3600)),
        )
        job_id = job.get("id") if isinstance(job, dict) else None
        if not job_id:
            raise ProviderSTTError(STTErrorCategory.RESPONSE_SCHEMA_ERROR, False, status, None,
                                   req_id, "soniox", model, "Soniox did not return a transcription ID.")
        try:
            deadline = time.monotonic() + int(getattr(settings, "stt_job_timeout", 3600))
            while True:
                state = str(job.get("status", "")).lower()
                if state in {"completed", "done"}:
                    transcript_payload, headers, status, result_req = await self._send_json(
                        session, "GET", f"{base}/v1/transcriptions/{quote(str(job_id), safe='')}/transcript",
                        headers={"Authorization": f"Bearer {key}"}, provider="soniox", model=model, timeout=60,
                    )
                    return normalize_soniox(transcript_payload, model=model, request_id=result_req or req_id or upload_req), headers, status, result_req or req_id
                if state in {"error", "failed"}:
                    raise ProviderSTTError(STTErrorCategory.QUALITY_FAILURE, False, None, None,
                                           req_id, "soniox", model, "Soniox could not transcribe this audio.")
                if time.monotonic() >= deadline:
                    raise ProviderSTTError(STTErrorCategory.PROVIDER_TIMEOUT, True, None, None,
                                           req_id, "soniox", model, "Soniox transcription timed out.")
                await asyncio.sleep(min(max(float(getattr(settings, "stt_poll_interval", 5)), 0.1), 15))
                job, headers, status, poll_req = await self._send_json(
                    session, "GET", f"{base}/v1/transcriptions/{quote(str(job_id), safe='')}",
                    headers={"Authorization": f"Bearer {key}"}, provider="soniox", model=model, timeout=60,
                )
                req_id = poll_req or req_id
        finally:
            # Provider documents DELETE for async transcription and uploaded file.
            for endpoint in (
                f"{base}/v1/transcriptions/{quote(str(job_id), safe='')}",
                f"{base}/v1/files/{quote(str(file_id), safe='')}",
            ):
                try:
                    async with session.delete(
                        endpoint, headers={"Authorization": f"Bearer {key}"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as response:
                        await response.read()
                except Exception:
                    logger.warning("STT temporary job cleanup failed provider=soniox")

    async def _elevenlabs(self, session, audio_path, settings, key, model, options):
        form = aiohttp.FormData()
        form.add_field("model_id", model)
        form.add_field("timestamps_granularity", "word" if options.word_timestamps else "none")
        form.add_field("diarize", str(bool(options.diarization)).lower())
        form.add_field("tag_audio_events", "false")
        language = resolve_language("elevenlabs_scribe", settings.stt_language, model=model)
        if language:
            form.add_field("language_code", language)
        if options.diarization and options.max_speakers:
            form.add_field("num_speakers", str(min(max(1, int(options.max_speakers)), 32)))
        if options.vocabulary.terms:
            # ElevenLabs docs accept keyterm prompt fields in current multipart schema.
            for term in options.vocabulary.terms[:100]:
                form.add_field("keyterm_prompt", term)
        with audio_path.open("rb") as audio:
            form.add_field("file", audio, filename=audio_path.name, content_type=_mime_type(audio_path))
            payload, headers, status, req_id = await self._send_json(
                session, "POST", self.get_url(settings, "speech-to-text"),
                headers={"xi-api-key": key}, data=form,
                provider="elevenlabs_scribe", model=model,
                timeout=int(getattr(settings, "stt_job_timeout", 3600)),
            )
        return normalize_elevenlabs(payload, model=model, request_id=req_id), headers, status, req_id


class DisabledAWSAdapter(STTProviderAdapter):
    """Safety placeholder: AWS requires S3+SigV4 and is not auto-routable."""

    provider = "aws_transcribe"
    default_model = "standard"

    def build_preview(self, settings, audio_path: Path, *, options=None) -> RequestPreview:
        return RequestPreview(
            provider=self.provider, model=self.default_model,
            endpoint="https://transcribe.{region}.amazonaws.com",
            method="POST", protocol=STTProtocol.PROVIDER_ASYNC_JOB.value,
            audio_bytes=audio_path.stat().st_size,
            audio_duration_seconds=get_audio_duration_seconds(audio_path),
            language=resolve_language(self.provider, settings.stt_language),
            payload_shape="Disabled: requires an S3 bucket/object + SigV4 credentials; fa-IR batch is unsupported.",
        )

    def normalize_response(self, payload, *, model, request_id=None):
        raise ProviderSTTError(STTErrorCategory.MODEL_UNAVAILABLE, False, None, None,
                               request_id, self.provider, model, "AWS Transcribe adapter is disabled.")

    async def transcribe(self, *args, **kwargs):
        raise ProviderSTTError(STTErrorCategory.MODEL_UNAVAILABLE, False, None, None,
                               None, self.provider, self.default_model,
                               "AWS Transcribe is disabled until its S3/SigV4 workflow is configured.")


#: Provider adapter registry. Existing providers are wrapped by the public
#: ``stt.py`` adapter delegates, avoiding a rewrite of their battle-tested
#: payload/polling/normalization logic.
STT_ADAPTERS: dict[str, STTProviderAdapter] = {
    provider: NativeSTTAdapter(provider)
    for provider in (
        "groq", "gemini_transcribe", "assemblyai", "gladia",
        "google_cloud_stt", "ibm_watson_stt", "azure_speech",
        "soniox", "elevenlabs_scribe",
    )
}
STT_ADAPTERS["aws_transcribe"] = DisabledAWSAdapter()


def adapter_for(provider: str) -> STTProviderAdapter | None:
    """Return the provider-specific adapter (legacy adapters live in stt.py)."""
    return STT_ADAPTERS.get(provider)
