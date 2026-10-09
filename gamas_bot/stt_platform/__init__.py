"""Gamas Speech Platform: provider-aware, quota-aware, accuracy-first STT.

This package is the STT counterpart of :mod:`gamas_bot.ai` (the note
platform). It owns:

* the **provider registry** — one reviewed, single-source-of-truth record per
  STT provider (protocol, capabilities, free classification, limits, docs);
* the **canonical Transcript** model every adapter normalizes into;
* **provider profiles** — per-provider timeout/retry/concurrency/quota policy;
* **adapters** — request builders, response normalizers, error normalizers,
  quota extractors and health strategies per provider protocol;
* the **route engine** — scoring, free-only/trial/paid gates, size/duration
  routing and quality-based fallback;
* the **quality gate** — deterministic transcript validation and quality
  signals (never transcript content in logs);
* **quota tracking** and **structured event logging**.

:mod:`gamas_bot.stt` stays the public entry point (``transcribe``,
``Transcript``, ``normalize_language_for_provider``); it delegates candidate
selection, gating and quality control to this package without changing the
existing Speechmatics/Deepgram/OpenAI-compatible request behaviour.
"""

from .models import (
    NormalizedConfidence,
    STTModelInfo,
    STTModelRegistry,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    VocabularyHints,
)
from .registry import (
    STT_PROVIDER_REGISTRY,
    STTAuthType,
    STTProviderClass,
    STTProviderInfo,
    STTProtocol,
    capability_matrix,
    free_class_fa,
    persian_batch_supported,
    stt_registry_info,
)

__all__ = [
    "NormalizedConfidence",
    "STTModelInfo",
    "STTModelRegistry",
    "STT_PROVIDER_REGISTRY",
    "STTAuthType",
    "STTProviderClass",
    "STTProviderInfo",
    "STTProtocol",
    "Transcript",
    "TranscriptSegment",
    "TranscriptWord",
    "VocabularyHints",
    "capability_matrix",
    "free_class_fa",
    "persian_batch_supported",
    "stt_registry_info",
]
