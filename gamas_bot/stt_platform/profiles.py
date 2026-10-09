"""Per-provider STT request profiles (spec §68).

``STTProviderProfile`` is resolved before every request: budgets, retries,
concurrency, language/vocabulary/diarization/timestamp policy, quality
threshold and quota/free policy. Provider concurrency is *independent* of
``MAX_CONCURRENT_JOBS`` (spec §42/§69): a deployment can run three jobs while
Groq transcribes one request at a time.

Chunking policy is ``whole_file_only`` everywhere: Gamas intentionally avoids
audio segmentation because split boundaries destroy word context (spec §7).
``emergency_only`` exists as an explicit, never-silent mode.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class STTRequestPolicy:
    """Retry/timeout behaviour for one provider attempt."""

    timeout_seconds: int = 3600
    max_retries: int = 2
    retry_on_rate_limit: bool = True
    backoff_base_seconds: float = 2.0
    backoff_cap_seconds: float = 30.0
    retry_after_cap_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class STTProviderProfile:
    """Provider-wide STT pipeline policy."""

    provider: str
    protocol: str = "batch_rest"
    batch: bool = True
    realtime: bool = False
    #: "whole_file_only" (default) or "emergency_only". Never "always":
    #: segmentation is an explicit operator decision, never silent.
    chunking_policy: str = "whole_file_only"
    max_upload: int = 25_000_000
    max_duration: int | None = None
    timeout: int = 3600
    retries: int = 2
    concurrency: int = 1
    #: How the requested STT_LANGUAGE is translated for this provider.
    language_policy: str = "explicit_code"   # explicit_code | omit_for_auto | model_selection
    #: How VocabularyHints are applied (spec §11).
    vocabulary_policy: str = "unsupported"   # additional_vocab | keyterms | custom_vocabulary | prompt | unsupported
    diarization_policy: str = "optional_off"  # optional_off | optional_on | unsupported
    timestamp_policy: str = "optional_off"   # optional_off | optional_on | unsupported
    quality_threshold: float = 0.65
    #: "observe" (track headers/DB), "guard" (block when exhausted), "off".
    quota_policy: str = "observe"
    free_policy: str = "free_monthly"        # mirrors registry free_type
    policy: STTRequestPolicy = field(default_factory=STTRequestPolicy)


DEFAULT_STT_PROFILES: dict[str, STTProviderProfile] = {
    "speechmatics": STTProviderProfile(
        provider="speechmatics",
        protocol="batch_rest",
        realtime=True,
        max_upload=1_000_000_000,
        concurrency=2,
        language_policy="explicit_code",
        vocabulary_policy="additional_vocab",
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="observe",
        free_policy="free_monthly",
    ),
    "deepgram": STTProviderProfile(
        provider="deepgram",
        protocol="batch_rest",
        realtime=True,
        max_upload=2_000_000_000,
        concurrency=2,
        language_policy="explicit_code",
        vocabulary_policy="keyterms",
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="guard",   # one-time $200 credit: never auto-overage
        free_policy="free_credit",
    ),
    "groq": STTProviderProfile(
        provider="groq",
        protocol="openai_audio_transcriptions",
        max_upload=25_000_000,
        concurrency=1,
        language_policy="explicit_code",
        vocabulary_policy="prompt",
        diarization_policy="unsupported",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="guard",   # audio-seconds/day is a hard free-tier cap
        free_policy="free_allocation",
        policy=STTRequestPolicy(max_retries=1),
    ),
    "gemini_transcribe": STTProviderProfile(
        provider="gemini_transcribe",
        protocol="gemini_interactions_audio",
        realtime=True,
        max_upload=2_000_000_000,
        max_duration=3600,
        concurrency=2,
        language_policy="explicit_code",
        vocabulary_policy="custom_vocabulary",
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="guard",
        free_policy="free_allocation",
    ),
    "openai_compatible": STTProviderProfile(
        provider="openai_compatible",
        protocol="openai_audio_transcriptions",
        max_upload=25_000_000,
        concurrency=2,
        language_policy="explicit_code",
        vocabulary_policy="prompt",
        diarization_policy="unsupported",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="observe",
        free_policy="paid_only",
    ),
    "assemblyai": STTProviderProfile(
        provider="assemblyai",
        protocol="provider_async_job",
        realtime=True,
        max_upload=2_200_000_000,
        concurrency=2,
        language_policy="explicit_code",
        vocabulary_policy="keyterms",
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="guard",   # one-time $50 credit
        free_policy="free_credit",
    ),
    "gladia": STTProviderProfile(
        provider="gladia",
        protocol="provider_upload_then_poll",
        realtime=True,
        max_upload=1_400_000_000,
        concurrency=2,
        language_policy="explicit_code",
        vocabulary_policy="custom_vocabulary",
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="guard",
        free_policy="free_monthly",
    ),
    "google_cloud_stt": STTProviderProfile(
        provider="google_cloud_stt",
        protocol="provider_async_job",
        realtime=True,
        max_upload=10_000_000,
        max_duration=28800,
        concurrency=2,
        language_policy="explicit_code",
        vocabulary_policy="speech_contexts",
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="guard",   # 60 free minutes/month
        free_policy="free_monthly",
    ),
    "ibm_watson_stt": STTProviderProfile(
        provider="ibm_watson_stt",
        protocol="batch_rest",
        realtime=True,
        max_upload=100_000_000,
        concurrency=2,
        language_policy="model_selection",
        vocabulary_policy="unsupported",  # Lite plan has no customization (documented)
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="guard",   # 500 free minutes/month
        free_policy="free_monthly",
    ),
    "aws_transcribe": STTProviderProfile(
        provider="aws_transcribe",
        protocol="provider_async_job",
        realtime=True,
        max_upload=2_000_000_000,
        max_duration=14400,
        concurrency=1,
        language_policy="explicit_code",
        vocabulary_policy="unsupported",
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="off",     # experimental; eligibility verified per account
        free_policy="promotional_free",
    ),
    "azure_speech": STTProviderProfile(
        provider="azure_speech",
        protocol="provider_async_job",
        realtime=True,
        max_upload=500_000_000,
        max_duration=36000,
        concurrency=1,
        language_policy="explicit_code",
        vocabulary_policy="unsupported",
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="guard",   # F0 realtime 5 h/month; batch is paid-only
        free_policy="free_monthly",
    ),
    "soniox": STTProviderProfile(
        provider="soniox",
        protocol="provider_async_job",
        realtime=True,
        max_upload=1_000_000_000,
        concurrency=1,
        language_policy="explicit_code",
        vocabulary_policy="custom_context",
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="off",     # paid-only; no free API credits for new accounts
        free_policy="paid_only",
    ),
    "elevenlabs_scribe": STTProviderProfile(
        provider="elevenlabs_scribe",
        protocol="openai_audio_transcriptions",
        realtime=True,
        max_upload=3_000_000_000,
        max_duration=36000,
        concurrency=1,
        language_policy="explicit_code",
        vocabulary_policy="keyterm_prompting",
        diarization_policy="optional_off",
        timestamp_policy="optional_off",
        quality_threshold=0.65,
        quota_policy="guard",
        free_policy="free_monthly",
    ),
}


def profile_for(provider: str) -> STTProviderProfile:
    """Resolved profile; unknown providers get the conservative generic one."""
    return DEFAULT_STT_PROFILES.get(provider, DEFAULT_STT_PROFILES["openai_compatible"])


class ProviderConcurrencyRegistry:
    """Per-provider async semaphores (spec §42/§69).

    ``MAX_CONCURRENT_JOBS`` bounds Gamas jobs; these semaphores bound each
    provider independently, so a slow provider never starves the others and a
    free-tier provider with concurrency=1 is never over-submitted.
    """

    def __init__(self, settings=None):
        self._settings = settings
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _limit(self, provider: str) -> int:
        profile = profile_for(provider)
        override = getattr(self._settings, "stt_provider_concurrency", {})
        if isinstance(override, dict) and provider in override:
            try:
                return max(1, int(override[provider]))
            except (TypeError, ValueError):
                pass
        return max(1, profile.concurrency)

    def semaphore(self, provider: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(provider)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._limit(provider))
            self._semaphores[provider] = semaphore
        return semaphore

    def limit(self, provider: str) -> int:
        return self._limit(provider)
