"""Canonical Gamas transcript model and the STT model registry.

Every STT adapter — Speechmatics, Deepgram, Groq, Gemini Transcribe,
AssemblyAI, Gladia, Google Cloud, IBM, Azure, AWS, Soniox, ElevenLabs and the
generic OpenAI-compatible gateways — normalizes its provider-specific response
into :class:`Transcript` (spec §6/§70). Providers that do not expose a field
return ``None``/empty; confidence is never invented.

:class:`Transcript` is also re-exported by :mod:`gamas_bot.stt` as the public
type. The first three positional fields (``engine``, ``text``, ``confidence``)
are unchanged, so every existing caller keeps working.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One timed transcript segment (utterance/paragraph)."""

    start: float = 0.0
    end: float = 0.0
    text: str = ""
    speaker: str | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptWord:
    """One word-level timestamp (only requested explicitly, spec §13)."""

    text: str = ""
    start: float = 0.0
    end: float = 0.0
    confidence: float | None = None
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedConfidence:
    """A provider confidence on a comparable footing (spec §31).

    Providers use different scales and semantics; ``value`` is the provider's
    own number, ``method`` records how it was produced, and ``comparable``
    tells the router whether it may be compared with other providers' values
    at all. Deepgram's word-confidence average is NOT numerically equivalent to
    a Speechmatics alternative confidence — the router never assumes it is.
    """

    value: float | None
    source: str            # "provider_reported" | "computed_average" | "unavailable"
    provider: str
    method: str            # e.g. "deepgram_alternative_confidence", "speechmatics_mean_alternative"
    comparable: bool

    @classmethod
    def unavailable(cls, provider: str) -> "NormalizedConfidence":
        return cls(None, "unavailable", provider, "none", False)


@dataclass(frozen=True, slots=True)
class VocabularyHints:
    """Provider-neutral medical/technical vocabulary (spec §11).

    Each adapter translates these into the provider's actual feature
    (Speechmatics ``additional_vocab``, AssemblyAI ``keyterms_prompt``, Gladia
    ``custom_vocabulary``, ElevenLabs ``keyterm_prompting``, Gemini
    ``custom_vocabulary``, Whisper-style ``prompt``). Providers without
    vocabulary support return ``None`` from the adapter instead of pretending.
    """

    terms: tuple[str, ...] = ()
    max_terms: int = 0
    max_term_length: int = 0
    cost_multiplier: float = 1.0
    provider_parameter_name: str = ""

    @classmethod
    def from_terms(cls, terms, *, max_terms: int = 0, max_term_length: int = 0,
                   cost_multiplier: float = 1.0, provider_parameter_name: str = "") -> "VocabularyHints":
        cleaned = tuple(dict.fromkeys(str(term).strip() for term in terms if str(term).strip()))
        return cls(
            terms=cleaned,
            max_terms=max_terms,
            max_term_length=max_term_length,
            cost_multiplier=cost_multiplier,
            provider_parameter_name=provider_parameter_name,
        )


@dataclass(frozen=True, slots=True)
class Transcript:
    """The provider-neutral Gamas transcript (spec §6).

    ``engine``/``text``/``confidence`` keep their historical meaning so existing
    callers (bot flow, QA, DOCX, benchmarks) are untouched. Everything else is
    additive and defaults to "the provider did not report it".
    """

    engine: str
    text: str
    confidence: float | None = None
    #: Canonical provider slug (defaults to ``engine`` for legacy callers).
    provider: str = ""
    model: str = ""
    language: str | None = None
    language_confidence: float | None = None
    segments: tuple[TranscriptSegment, ...] = ()
    words: tuple[TranscriptWord, ...] = ()
    speakers: tuple[str, ...] = ()
    duration_seconds: float | None = None
    metadata: dict = field(default_factory=dict)
    request_id: str | None = None
    usage: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def overall_confidence(self) -> float | None:
        """Backward-compatible alias for :attr:`confidence`."""
        return self.confidence

    @property
    def canonical_provider(self) -> str:
        return self.provider or self.engine

    def normalized_confidence(self) -> NormalizedConfidence:
        if self.confidence is None:
            return NormalizedConfidence.unavailable(self.canonical_provider)
        method = str(self.metadata.get("confidence_method") or "provider_reported")
        comparable = bool(self.metadata.get("confidence_comparable", False))
        return NormalizedConfidence(self.confidence, "provider_reported", self.canonical_provider, method, comparable)


# ---------------------------------------------------------------------------
# STT model registry: static seeds + live discovery + SQLite cache
# ---------------------------------------------------------------------------

#: Model-level status vocabulary.
MODEL_AVAILABLE = "available"
MODEL_DEPRECATED = "deprecated"
MODEL_RETIRED = "retired"
MODEL_UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class STTModelInfo:
    """One STT model on one provider (spec §48)."""

    provider: str
    model: str
    display_name: str = ""
    status: str = MODEL_AVAILABLE
    language_support: tuple[str, ...] = ()
    persian_supported: bool = False
    feature_support: tuple[str, ...] = ()
    free_status: str = "unknown"     # free_plan | free_credit | paid | unknown
    max_duration_seconds: int | None = None
    max_file_size: int | None = None
    quality_score: float | None = None
    deprecated: bool = False
    deprecation_date: str | None = None
    available: bool = True
    source: str = "static_seed"      # static_seed | live:<endpoint> | admin
    last_verified: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.model

    def to_row(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "display_name": self.display_name,
            "status": self.status,
            "language_support_json": json.dumps(list(self.language_support)),
            "persian_supported": 1 if self.persian_supported else 0,
            "feature_support_json": json.dumps(list(self.feature_support)),
            "free_status": self.free_status,
            "max_duration_seconds": self.max_duration_seconds,
            "max_file_size": self.max_file_size,
            "quality_score": self.quality_score,
            "deprecated": 1 if self.deprecated else 0,
            "deprecation_date": self.deprecation_date,
            "available": 1 if self.available else 0,
            "source": self.source,
            "last_verified": self.last_verified,
        }

    @classmethod
    def from_row(cls, row: dict) -> "STTModelInfo":
        def _loads(value) -> tuple:
            try:
                data = json.loads(value) if value else []
            except (ValueError, TypeError):
                data = []
            return tuple(item for item in data if isinstance(item, str))

        return cls(
            provider=str(row["provider"]),
            model=str(row["model"]),
            display_name=str(row.get("display_name") or ""),
            status=str(row.get("status") or MODEL_AVAILABLE),
            language_support=_loads(row.get("language_support_json")),
            persian_supported=bool(row.get("persian_supported", 0)),
            feature_support=_loads(row.get("feature_support_json")),
            free_status=str(row.get("free_status") or "unknown"),
            max_duration_seconds=row.get("max_duration_seconds"),
            max_file_size=row.get("max_file_size"),
            quality_score=row.get("quality_score"),
            deprecated=bool(row.get("deprecated", 0)),
            deprecation_date=row.get("deprecation_date"),
            available=bool(row.get("available", 1)),
            source=str(row.get("source") or "static_seed"),
            last_verified=row.get("last_verified"),
        )


def _seed(provider: str, model: str, **kwargs) -> STTModelInfo:
    return STTModelInfo(provider=provider, model=model, **kwargs)


#: Reviewed static seeds. Facts here were verified against official
#: documentation (Gemini Transcribe and Groq re-verified 2026-10-10); live
#: discovery overlays availability and never deletes history (spec §48/§49).
STATIC_STT_MODELS: dict[str, list[STTModelInfo]] = {
    "speechmatics": [
        _seed("speechmatics", "enhanced", display_name="Enhanced (highest accuracy)",
              persian_supported=True, free_status="free_plan",
              feature_support=("confidence", "additional_vocab", "word_timestamps", "diarization"),
              quality_score=0.93),
        _seed("speechmatics", "standard", display_name="Standard",
              persian_supported=True, free_status="free_plan",
              feature_support=("confidence", "additional_vocab", "word_timestamps"),
              quality_score=0.88),
        _seed("speechmatics", "melia-1", display_name="Melia-1 (multilingual)",
              persian_supported=True, free_status="free_plan",
              feature_support=("multi", "code_switching"),
              quality_score=0.85),
        _seed("speechmatics", "oak-1", display_name="Oak-1 (multilingual)",
              persian_supported=True, free_status="free_plan",
              feature_support=("multi", "code_switching"),
              quality_score=0.84),
    ],
    "deepgram": [
        _seed("deepgram", "nova-3", display_name="Nova-3",
              persian_supported=True, free_status="free_credit",
              feature_support=("confidence", "smart_format", "word_timestamps", "diarization", "keyterms"),
              quality_score=0.90),
        _seed("deepgram", "nova-2", display_name="Nova-2",
              persian_supported=True, free_status="free_credit",
              feature_support=("confidence", "smart_format", "word_timestamps"),
              quality_score=0.87),
    ],
    "groq": [
        _seed("groq", "whisper-large-v3", display_name="Whisper Large V3",
              persian_supported=True, free_status="free_plan",
              feature_support=("prompt", "temperature", "segment_timestamps", "word_timestamps"),
              max_file_size=25_000_000, quality_score=0.86),
        _seed("groq", "whisper-large-v3-turbo", display_name="Whisper Large V3 Turbo",
              persian_supported=True, free_status="free_plan",
              feature_support=("prompt", "temperature", "segment_timestamps"),
              max_file_size=25_000_000, quality_score=0.84),
    ],
    "gemini_transcribe": [
        _seed("gemini_transcribe", "gemini-3.5-transcribe", display_name="Gemini 3.5 Transcribe",
              persian_supported=True, free_status="free_plan",
              feature_support=("custom_vocabulary", "diarization", "word_timestamps", "smart_mode", "code_switching"),
              max_duration_seconds=3600, quality_score=0.91),
        _seed("gemini_transcribe", "gemini-3.5-transcribe-live", display_name="Gemini 3.5 Transcribe Live",
              persian_supported=True, free_status="free_plan",
              feature_support=("realtime", "code_switching"),
              max_duration_seconds=600, quality_score=0.88),
    ],
    "openai_compatible": [
        _seed("openai_compatible", "whisper-1", display_name="Whisper-1 (generic)",
              persian_supported=True, free_status="paid",
              feature_support=("prompt", "segment_timestamps"),
              quality_score=0.84),
    ],
    "assemblyai": [
        _seed("assemblyai", "universal", display_name="Universal (default)",
              persian_supported=True, free_status="free_credit",
              feature_support=("confidence", "keyterms_prompt", "speaker_labels", "word_timestamps", "format_text"),
              quality_score=0.88),
        _seed("assemblyai", "best", display_name="Best",
              persian_supported=True, free_status="paid",
              feature_support=("confidence", "keyterms_prompt", "speaker_labels", "word_timestamps"),
              quality_score=0.90),
    ],
    "gladia": [
        _seed("gladia", "solaria-1", display_name="Solaria-1",
              persian_supported=True, free_status="free_plan",
              feature_support=("custom_vocabulary", "diarization", "word_timestamps", "code_switching", "audio_enhancement"),
              quality_score=0.87),
        _seed("gladia", "solaria-3", display_name="Solaria-3",
              persian_supported=False, free_status="paid",
              feature_support=("diarization", "word_timestamps"),
              quality_score=0.86),
    ],
    "google_cloud_stt": [
        _seed("google_cloud_stt", "latest_long", display_name="Latest long-audio model",
              persian_supported=True, free_status="free_plan",
              feature_support=("confidence", "word_timestamps", "speech_contexts", "diarization"),
              max_duration_seconds=28800, max_file_size=10_000_000,
              quality_score=0.85),
        _seed("google_cloud_stt", "latest_short", display_name="Latest short-audio model",
              persian_supported=True, free_status="free_plan",
              feature_support=("confidence", "word_timestamps"),
              max_duration_seconds=60, quality_score=0.84),
    ],
    "ibm_watson_stt": [
        _seed("ibm_watson_stt", "fa-IR_BroadbandModel", display_name="Persian broadband",
              persian_supported=True, free_status="free_plan",
              feature_support=("confidence", "word_timestamps"),
              quality_score=0.83),
        _seed("ibm_watson_stt", "en-US_BroadbandModel", display_name="English (US) broadband",
              persian_supported=False, free_status="free_plan",
              feature_support=("confidence", "word_timestamps"),
              quality_score=0.85),
    ],
    "aws_transcribe": [
        _seed("aws_transcribe", "standard", display_name="Standard batch",
              persian_supported=False, free_status="promotional",
              feature_support=("confidence", "word_timestamps", "diarization"),
              max_duration_seconds=14400, quality_score=0.80),
    ],
    "azure_speech": [
        _seed("azure_speech", "standard", display_name="Standard (batch v3.2+)",
              persian_supported=True, free_status="paid",
              feature_support=("confidence", "word_timestamps", "diarization"),
              max_duration_seconds=36000, quality_score=0.82),
    ],
    "soniox": [
        _seed("soniox", "stt-async-v5", display_name="STT async v5",
              persian_supported=True, free_status="paid",
              feature_support=("confidence", "word_timestamps", "diarization", "custom_context"),
              quality_score=0.84),
    ],
    "elevenlabs_scribe": [
        _seed("elevenlabs_scribe", "scribe_v2", display_name="Scribe v2",
              persian_supported=True, free_status="free_plan",
              feature_support=("word_timestamps", "diarization", "keyterm_prompting"),
              max_duration_seconds=36000, max_file_size=3_000_000_000,
              quality_score=0.85),
    ],
}


def static_stt_models(provider: str) -> list[STTModelInfo]:
    return list(STATIC_STT_MODELS.get(provider, []))


def default_stt_model(provider: str) -> str | None:
    seeds = STATIC_STT_MODELS.get(provider) or []
    return seeds[0].model if seeds else None


class STTModelRegistry:
    """Reads/writes the ``stt_model_registry`` cache; sync is explicit."""

    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    @property
    def ttl_seconds(self) -> float:
        return max(60.0, float(getattr(self.settings, "stt_provider_sync_ttl", 86400)))

    async def cached(self, provider: str) -> list[STTModelInfo]:
        rows = await self.db.stt_models_list(provider)
        if rows:
            return [STTModelInfo.from_row(row) for row in rows]
        return static_stt_models(provider)

    async def resolve(self, provider: str, model: str) -> STTModelInfo:
        row = await self.db.stt_model_get(provider, model)
        if row:
            return STTModelInfo.from_row(row)
        for seed in STATIC_STT_MODELS.get(provider, []):
            if seed.model == model:
                return seed
        return STTModelInfo(provider=provider, model=model, source="inferred")

    async def apply_discovery(self, provider: str, discovered: list[STTModelInfo]) -> dict:
        """Persist a live catalog; vanished models become unavailable, never deleted."""
        if not discovered:
            return {"synced": 0, "deactivated": 0}
        stats = await self.db.stt_models_upsert_discovery(
            provider, [info.to_row() for info in discovered]
        )
        return stats

    def stale(self, synced_at: str | None) -> bool:
        if not synced_at:
            return True
        try:
            last = datetime.fromisoformat(str(synced_at).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError):
            return True
        age = datetime.now(timezone.utc) - last.astimezone(timezone.utc)
        return age.total_seconds() > self.ttl_seconds


def monotonic() -> float:
    import time

    return time.monotonic()
