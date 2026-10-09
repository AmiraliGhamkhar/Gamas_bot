"""STT provider registry: the single source of truth for provider metadata.

Every STT provider Gamas knows about is described exactly once here — the
routing engine, the admin panel, the health checker and the benchmark tool all
read this registry, so capabilities can never drift between two files.

Free classification is *reviewed metadata*, never marketing: a provider is not
"free" because its website shows "Start for free". The ``free_type`` values
distinguish quota models that are not interchangeable (spec §3/§26):

* ``permanent_free``    — no metered quota at all (none of the current
  providers qualify; kept for completeness);
* ``free_monthly``      — a recurring monthly allocation (IBM Lite 500 min,
  Google Cloud STT 60 min, Speechmatics 480 min, Gladia 10 h, Azure F0 5 h
  realtime-only);
* ``free_allocation``   — a standing no-cost tier with rate/audio quotas
  (Groq Whisper, Gemini API free tier);
* ``free_credit``       — a one-time credit grant that does not refresh
  (Deepgram $200, AssemblyAI $50, Gladia €50);
* ``promotional_free``  — a time-limited new-customer allowance (AWS Transcribe
  60 min/month for the first 12 months);
* ``trial_only``        — evaluation keys, not production capacity;
* ``paid_only``         — no no-charge entitlement (Soniox API after the
  2025-10 free-credit discontinuation, generic OpenAI-compatible gateways);
* ``region_restricted`` / ``unsupported`` — fail-closed states.

All quota numbers are *hints last verified on ``last_verified_at``*; live
provider headers and quota endpoints always win at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Date the classifications/limits below were last cross-checked against the
#: providers' official documentation.
VERIFIED_AT = "2026-10-09"


class STTProviderClass(str, Enum):
    """Free-tier classification of an STT provider (spec §3)."""

    PERMANENT_FREE = "permanent_free"
    FREE_MONTHLY = "free_monthly"
    FREE_ALLOCATION = "free_allocation"
    FREE_CREDIT = "free_credit"
    PROMOTIONAL_FREE = "promotional_free"
    TRIAL_ONLY = "trial_only"
    PAID_ONLY = "paid_only"
    REGION_RESTRICTED = "region_restricted"
    UNSUPPORTED = "unsupported"


class STTProtocol(str, Enum):
    """Protocol families the STT adapters implement (spec §5).

    Providers are NOT equivalent and must not all pretend to be OpenAI
    Whisper; each adapter speaks its provider's real protocol.
    """

    BATCH_REST = "batch_rest"
    REALTIME_WEBSOCKET = "realtime_websocket"
    OPENAI_AUDIO_TRANSCRIPTIONS = "openai_audio_transcriptions"
    GEMINI_INTERACTIONS_AUDIO = "gemini_interactions_audio"
    #: Submit a job, then poll it (AssemblyAI, AWS Transcribe, Azure batch,
    #: Google Cloud longRunningRecognize).
    PROVIDER_ASYNC_JOB = "provider_async_job"
    #: Upload the file first, then create+poll a job (Gladia pre-recorded).
    PROVIDER_UPLOAD_THEN_POLL = "provider_upload_then_poll"


class STTAuthType(str, Enum):
    """How an STT adapter authenticates a request."""

    BEARER = "bearer"
    TOKEN = "token"                      # Deepgram "Token <key>"
    API_KEY_HEADER = "api_key_header"    # x-api-key / x-gladia-key / xi-api-key / Ocp-Apim-Subscription-Key
    X_GOOG_API_KEY = "x_goog_api_key"
    BASIC = "basic"                      # IBM Cloud apikey as basic-auth password
    SIGV4 = "sigv4"                      # AWS Signature Version 4
    NONE = "none"


#: Providers configured the historical way (environment keys) keep working in
#: FREE_ONLY mode without any migration: an explicitly configured key is the
#: operator's explicit choice (spec §66 backward compatibility). The
#: trial/paid gates below govern *automatic platform routing* to providers the
#: operator has not explicitly configured.
LEGACY_STT_PROVIDERS = frozenset({"speechmatics", "deepgram", "openai_compatible"})

#: Free classes that count as "trial credit" for STT_ALLOW_TRIAL_PROVIDERS.
TRIAL_FREE_CLASSES = frozenset(
    {
        STTProviderClass.FREE_CREDIT,
        STTProviderClass.TRIAL_ONLY,
        STTProviderClass.PROMOTIONAL_FREE,
    }
)

#: Free classes usable in FREE_ONLY mode without a trial opt-in.
FREE_ONLY_CLASSES = frozenset(
    {
        STTProviderClass.PERMANENT_FREE,
        STTProviderClass.FREE_MONTHLY,
        STTProviderClass.FREE_ALLOCATION,
    }
)


@dataclass(frozen=True, slots=True)
class STTProviderInfo:
    """One reviewed STT provider record (spec §3 field list)."""

    provider_slug: str
    display_name: str
    protocol: str
    base_url: str | None
    service_type: str
    batch_supported: bool
    realtime_supported: bool
    streaming_supported: bool
    authentication_type: str
    language_capabilities: tuple[str, ...]
    model_capabilities: tuple[str, ...]
    max_file_size: int
    #: Longest audio one request accepts, seconds; ``None`` = provider-documented
    #: as effectively unlimited or unknown.
    max_audio_duration: int | None
    max_concurrency: int
    rate_limits: str
    free_type: str
    free_limit: str
    free_reset: str
    trial_expiration: str
    commercial_use: bool
    region_restrictions: str
    data_retention: str
    official_docs_url: str
    pricing_url: str
    last_verified_at: str
    enabled: bool
    # -- capability matrix (spec §47) --
    persian_batch: bool
    supports_diarization: bool
    max_speakers: int
    supports_word_timestamps: bool
    supports_segment_timestamps: bool
    supports_confidence: bool
    vocabulary_supported: bool
    vocabulary_max_terms: int
    vocabulary_parameter: str
    supports_smart_formatting: bool
    supports_audio_enhancement: bool
    experimental: bool
    # -- routing hints --
    #: Reviewed Persian-weighted accuracy hint (0..1). Benchmarks refine it;
    #: it is never invented from marketing claims (spec §58/§82).
    quality_score: float
    #: Below this score a model is not routed for Gamas Persian work (spec §51).
    min_quality_score: float
    default_model: str
    models: tuple[str, ...]
    #: Default per-provider concurrency (independent of MAX_CONCURRENT_JOBS).
    concurrency: int
    #: "whole_file_only" (default) or "emergency_only"; chunking is never
    #: silently enabled (spec §7).
    chunking_policy: str

    @property
    def classification(self) -> STTProviderClass:
        return STTProviderClass(self.free_type)

    @property
    def is_trial_class(self) -> bool:
        return self.classification in TRIAL_FREE_CLASSES

    @property
    def is_free_only_class(self) -> bool:
        return self.classification in FREE_ONLY_CLASSES


# ---------------------------------------------------------------------------
# Registry data (all free-tier facts verified against official documentation
# on 2026-10-09; see docs/STT_PROVIDERS.md for the per-provider evidence).
# ---------------------------------------------------------------------------

_SPEECHMATICS = STTProviderInfo(
    provider_slug="speechmatics",
    display_name="Speechmatics",
    protocol=STTProtocol.BATCH_REST.value,
    base_url="https://eu1.asr.api.speechmatics.com/v2",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.BEARER.value,
    language_capabilities=("fa", "en-US", "auto", "multi", "bilingual packs"),
    model_capabilities=("enhanced", "standard", "melia-1", "oak-1", "additional_vocab", "confidence"),
    max_file_size=1_000_000_000,
    max_audio_duration=None,
    max_concurrency=10,
    rate_limits="Free plan: 480 minutes/month (batch+realtime); Pro: 10 file jobs/second (verified 2026-10-09)",
    free_type=STTProviderClass.FREE_MONTHLY.value,
    free_limit="480 minutes/month recurring free plan (not a one-time credit)",
    free_reset="monthly",
    trial_expiration="",
    commercial_use=True,
    region_restrictions="",
    data_retention="See Speechmatics data-security documentation; enterprise options offer no-training deployments.",
    official_docs_url="https://docs.speechmatics.com/",
    pricing_url="https://www.speechmatics.com/pricing",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=True,
    supports_diarization=True,
    max_speakers=9,
    supports_word_timestamps=True,
    supports_segment_timestamps=True,
    supports_confidence=True,
    vocabulary_supported=True,
    vocabulary_max_terms=1000,
    vocabulary_parameter="transcription_config.additional_vocab",
    supports_smart_formatting=True,
    supports_audio_enhancement=False,
    experimental=False,
    quality_score=0.93,
    min_quality_score=0.80,
    default_model="enhanced",
    models=("enhanced", "standard", "melia-1", "oak-1"),
    concurrency=2,
    chunking_policy="whole_file_only",
)

_DEEPGRAM = STTProviderInfo(
    provider_slug="deepgram",
    display_name="Deepgram",
    protocol=STTProtocol.BATCH_REST.value,
    base_url="https://api.deepgram.com/v1",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.TOKEN.value,
    language_capabilities=("fa", "en-US", "multi (10 languages, no Persian)"),
    model_capabilities=("nova-3", "nova-2", "flux", "smart_format", "punctuate", "confidence", "keyterm prompting (add-on)"),
    max_file_size=2_000_000_000,
    max_audio_duration=None,
    max_concurrency=50,
    rate_limits="No published free-tier RPM; usage billed per minute after the starter credit (verified 2026-10-09)",
    free_type=STTProviderClass.FREE_CREDIT.value,
    free_limit="$200 one-time starter credit, no card, no expiration; then pay-as-you-go",
    free_reset="one_time",
    trial_expiration="credit does not expire; paid usage starts when it is exhausted",
    commercial_use=True,
    region_restrictions="",
    data_retention="See Deepgram privacy policy; zero-retention endpoints available on enterprise plans.",
    official_docs_url="https://developers.deepgram.com/",
    pricing_url="https://deepgram.com/pricing",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=True,
    supports_diarization=True,
    max_speakers=9,
    supports_word_timestamps=True,
    supports_segment_timestamps=True,
    supports_confidence=True,
    vocabulary_supported=True,
    vocabulary_max_terms=1000,
    vocabulary_parameter="keyterm prompting (paid add-on; keywords parameter on some models)",
    supports_smart_formatting=True,
    supports_audio_enhancement=False,
    experimental=False,
    quality_score=0.90,
    min_quality_score=0.80,
    default_model="nova-3",
    models=("nova-3", "nova-2", "nova-3-medical"),
    concurrency=2,
    chunking_policy="whole_file_only",
)

_GROQ = STTProviderInfo(
    provider_slug="groq",
    display_name="Groq (Whisper)",
    protocol=STTProtocol.OPENAI_AUDIO_TRANSCRIPTIONS.value,
    base_url="https://api.groq.com/openai/v1",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.BEARER.value,
    language_capabilities=("fa", "en-US", "auto (omit language)"),
    model_capabilities=("whisper-large-v3", "whisper-large-v3-turbo", "prompt", "temperature", "verbose_json timestamps"),
    max_file_size=25_000_000,
    max_audio_duration=None,
    max_concurrency=5,
    rate_limits=(
        "Free plan (verified 2026-10-09): whisper-large-v3/-turbo 20 RPM, 2,000 requests/day, "
        "7,200 audio-seconds/hour, 28,800 audio-seconds/day; 25 MB max upload; "
        "10-second minimum billing per request"
    ),
    free_type=STTProviderClass.FREE_ALLOCATION.value,
    free_limit="Standing no-cost Free plan with rate/audio-second quotas (RPM/RPD/ASH/ASD)",
    free_reset="hourly/daily windows",
    trial_expiration="",
    commercial_use=True,
    region_restrictions="",
    data_retention="Zero-data-retention option documented; see console.groq.com/docs/your-data.",
    official_docs_url="https://console.groq.com/docs/speech-to-text",
    pricing_url="https://console.groq.com/docs/rate-limits",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=True,
    supports_diarization=False,
    max_speakers=0,
    supports_word_timestamps=True,
    supports_segment_timestamps=True,
    supports_confidence=False,
    vocabulary_supported=True,
    vocabulary_max_terms=1,
    vocabulary_parameter="prompt (single free-form prompt field)",
    supports_smart_formatting=True,
    supports_audio_enhancement=False,
    experimental=False,
    quality_score=0.86,
    min_quality_score=0.80,
    default_model="whisper-large-v3",
    models=("whisper-large-v3", "whisper-large-v3-turbo"),
    concurrency=1,
    chunking_policy="whole_file_only",
)

_GEMINI_TRANSCRIBE = STTProviderInfo(
    provider_slug="gemini_transcribe",
    display_name="Google Gemini 3.5 Transcribe",
    protocol=STTProtocol.GEMINI_INTERACTIONS_AUDIO.value,
    base_url="https://generativelanguage.googleapis.com/v1beta",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.X_GOOG_API_KEY.value,
    language_capabilities=("fa (fa-IR)", "en-US", "auto (omit language_codes)", "85+ languages, native code-switching"),
    model_capabilities=(
        "gemini-3.5-transcribe",
        "gemini-3.5-transcribe-live",
        "verbatim/smart modes",
        "custom_vocabulary (<=1000 terms)",
        "word timestamps",
        "speaker diarization (<=8)",
    ),
    max_file_size=2_000_000_000,
    max_audio_duration=3600,
    max_concurrency=5,
    rate_limits=(
        "Gemini API free tier is free of charge per model with per-minute/day quotas "
        "(read live from responses); paid tier ~$0.005/min blended (verified 2026-10-09)"
    ),
    free_type=STTProviderClass.FREE_ALLOCATION.value,
    free_limit="Gemini API free tier (no billing linked): free of charge, rate-limited per model",
    free_reset="per-minute/per-day windows",
    trial_expiration="",
    commercial_use=True,
    region_restrictions="",
    data_retention=(
        "Free-of-charge (unpaid) API traffic may be used by Google to improve its "
        "products; paid-tier traffic is not. See the Gemini API terms of service."
    ),
    official_docs_url="https://ai.google.dev/gemini-api/docs/transcribe",
    pricing_url="https://ai.google.dev/gemini-api/docs/pricing",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=True,
    supports_diarization=True,
    max_speakers=8,
    supports_word_timestamps=True,
    supports_segment_timestamps=False,
    supports_confidence=False,
    vocabulary_supported=True,
    vocabulary_max_terms=1000,
    vocabulary_parameter="generation_config.transcription_config.custom_vocabulary",
    supports_smart_formatting=True,
    supports_audio_enhancement=False,
    experimental=False,
    quality_score=0.91,
    min_quality_score=0.80,
    default_model="gemini-3.5-transcribe",
    models=("gemini-3.5-transcribe", "gemini-3.5-transcribe-live"),
    concurrency=2,
    chunking_policy="whole_file_only",
)

_OPENAI_COMPATIBLE = STTProviderInfo(
    provider_slug="openai_compatible",
    display_name="OpenAI-compatible STT (generic)",
    protocol=STTProtocol.OPENAI_AUDIO_TRANSCRIPTIONS.value,
    base_url=None,
    service_type="gateway",
    batch_supported=True,
    realtime_supported=False,
    streaming_supported=False,
    authentication_type=STTAuthType.BEARER.value,
    language_capabilities=("fa", "en-US", "auto (omit language)"),
    model_capabilities=("whisper-1 and any Whisper-compatible model",),
    max_file_size=25_000_000,
    max_audio_duration=None,
    max_concurrency=3,
    rate_limits="Gateway-specific; STT_OPENAI_MAX_UPLOAD_BYTES caps direct uploads (default 25 MB)",
    free_type=STTProviderClass.PAID_ONLY.value,
    free_limit="Generic gateways are paid unless the base URL resolves to a known free provider",
    free_reset="",
    trial_expiration="",
    commercial_use=True,
    region_restrictions="",
    data_retention="Depends on the configured gateway.",
    official_docs_url="https://platform.openai.com/docs/api-reference/audio/createTranscription",
    pricing_url="https://openai.com/pricing",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=True,
    supports_diarization=False,
    max_speakers=0,
    supports_word_timestamps=True,
    supports_segment_timestamps=True,
    supports_confidence=False,
    vocabulary_supported=True,
    vocabulary_max_terms=1,
    vocabulary_parameter="prompt (single free-form prompt field)",
    supports_smart_formatting=True,
    supports_audio_enhancement=False,
    experimental=False,
    quality_score=0.84,
    min_quality_score=0.80,
    default_model="whisper-1",
    models=("whisper-1",),
    concurrency=2,
    chunking_policy="whole_file_only",
)

_ASSEMBLYAI = STTProviderInfo(
    provider_slug="assemblyai",
    display_name="AssemblyAI",
    protocol=STTProtocol.PROVIDER_ASYNC_JOB.value,
    base_url="https://api.assemblyai.com/v2",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.API_KEY_HEADER.value,
    language_capabilities=("fa", "en-US", "auto (language_detection)", "99+ languages"),
    model_capabilities=("universal (default)", "best", "nano", "speaker_labels", "keyterms_prompt", "punctuate", "format_text"),
    max_file_size=2_200_000_000,
    max_audio_duration=None,
    max_concurrency=5,
    rate_limits="Free accounts: 5 concurrent pre-recorded transcriptions, 5 new streams/minute (verified 2026-10-09)",
    free_type=STTProviderClass.FREE_CREDIT.value,
    free_limit="$50 one-time credit for new accounts, no card, does not expire; then pay-as-you-go",
    free_reset="one_time",
    trial_expiration="credit does not expire; paid usage starts when it is exhausted",
    commercial_use=True,
    region_restrictions="",
    data_retention=(
        "Training on transcripts is opt-in for paid accounts; free-tier users cannot "
        "opt out (documented on the AssemblyAI pricing page, verified 2026-10-09)."
    ),
    official_docs_url="https://www.assemblyai.com/docs",
    pricing_url="https://www.assemblyai.com/pricing",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=True,
    supports_diarization=True,
    max_speakers=10,
    supports_word_timestamps=True,
    supports_segment_timestamps=True,
    supports_confidence=True,
    vocabulary_supported=True,
    vocabulary_max_terms=1000,
    vocabulary_parameter="keyterms_prompt",
    supports_smart_formatting=True,
    supports_audio_enhancement=False,
    experimental=False,
    quality_score=0.88,
    min_quality_score=0.80,
    default_model="universal",
    models=("universal", "best", "nano"),
    concurrency=2,
    chunking_policy="whole_file_only",
)

_GLADIA = STTProviderInfo(
    provider_slug="gladia",
    display_name="Gladia",
    protocol=STTProtocol.PROVIDER_UPLOAD_THEN_POLL.value,
    base_url="https://api.gladia.io/v2",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.API_KEY_HEADER.value,
    language_capabilities=("fa", "en-US", "auto (omit languages)", "100+ languages, code_switching"),
    model_capabilities=("solaria-1", "solaria-3", "diarization", "custom vocabulary", "audio enhancement", "translation"),
    max_file_size=1_400_000_000,
    max_audio_duration=None,
    max_concurrency=10,
    rate_limits="Concurrency and rate limits are plan-scoped; free plan: 10 hours/month (verified 2026-10-09)",
    free_type=STTProviderClass.FREE_MONTHLY.value,
    free_limit="Free plan 10 hours/month plus a one-time €50 credit grant (no expiry)",
    free_reset="monthly (10 h) + one_time (€50 credit)",
    trial_expiration="€50 credit does not expire; paid plans start when free hours/credits are used up",
    commercial_use=True,
    region_restrictions="EU/US data regions selectable on paid plans.",
    data_retention="Pro/Enterprise transcripts are excluded from training by default; see Gladia security docs.",
    official_docs_url="https://docs.gladia.io/",
    pricing_url="https://www.gladia.io/pricing",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=True,
    supports_diarization=True,
    max_speakers=32,
    supports_word_timestamps=True,
    supports_segment_timestamps=True,
    supports_confidence=True,
    vocabulary_supported=True,
    vocabulary_max_terms=1000,
    vocabulary_parameter="custom_vocabulary (list of {value, ...})",
    supports_smart_formatting=True,
    supports_audio_enhancement=True,
    experimental=False,
    quality_score=0.87,
    min_quality_score=0.80,
    default_model="solaria-1",
    models=("solaria-1", "solaria-3"),
    concurrency=2,
    chunking_policy="whole_file_only",
)

_GOOGLE_CLOUD_STT = STTProviderInfo(
    provider_slug="google_cloud_stt",
    display_name="Google Cloud Speech-to-Text",
    protocol=STTProtocol.PROVIDER_ASYNC_JOB.value,
    base_url="https://speech.googleapis.com/v1",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.NONE.value,
    language_capabilities=("fa (fa-IR)", "en-US", "100+ language codes (languageCode parameter)"),
    model_capabilities=("latest_long", "latest_short", "video", "phone_call", "medical_dictation", "enhanced models", "word timestamps"),
    max_file_size=10_000_000,
    max_audio_duration=28800,
    max_concurrency=100,
    rate_limits="Free tier: 60 minutes/month (V1); paid per 15 seconds (verified 2026-10-09)",
    free_type=STTProviderClass.FREE_MONTHLY.value,
    free_limit="60 minutes/month free (Speech-to-Text V1); V2 free allocation is not documented",
    free_reset="monthly",
    trial_expiration="",
    commercial_use=True,
    region_restrictions="Regional endpoints; API key or OAuth service account required.",
    data_retention="See Google Cloud data retention documentation; 30-day default logging can be disabled.",
    official_docs_url="https://cloud.google.com/speech-to-text/docs",
    pricing_url="https://cloud.google.com/speech-to-text/pricing",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=True,
    supports_diarization=True,
    max_speakers=2,
    supports_word_timestamps=True,
    supports_segment_timestamps=False,
    supports_confidence=True,
    vocabulary_supported=True,
    vocabulary_max_terms=5000,
    vocabulary_parameter="speechContexts.phrases (inline SpeechContext)",
    supports_smart_formatting=False,
    supports_audio_enhancement=True,
    experimental=False,
    quality_score=0.85,
    min_quality_score=0.80,
    default_model="latest_long",
    models=("latest_long", "latest_short", "video", "medical_dictation"),
    concurrency=2,
    chunking_policy="whole_file_only",
)

#: Google Cloud STT notes: synchronous ``speech:recognize`` is capped at ~60 s
#: of audio; ``longRunningRecognize`` accepts inline content up to 10 MB, which
#: is why max_file_size is conservative. Larger files need a GCS URI, which the
#: adapter does not fabricate — the router sends them to a provider that
#: accepts the whole file (spec §7/§8).

_IBM_WATSON_STT = STTProviderInfo(
    provider_slug="ibm_watson_stt",
    display_name="IBM Watson Speech to Text",
    protocol=STTProtocol.BATCH_REST.value,
    base_url="https://api.{region}.speech-to-text.watson.cloud.ibm.com",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.BASIC.value,
    language_capabilities=("fa (fa-IR_BroadbandModel)", "en-US", "60+ languages via model selection"),
    model_capabilities=("broadband/narrowband models per language", "word timestamps", "confidence", "speaker labels (paid)"),
    max_file_size=100_000_000,
    max_audio_duration=None,
    max_concurrency=100,
    rate_limits="Lite plan: 500 minutes/month; services deleted after 30 days of inactivity (verified 2026-10-09)",
    free_type=STTProviderClass.FREE_MONTHLY.value,
    free_limit="Lite plan: 500 minutes/month at no cost; customization requires a paid plan",
    free_reset="monthly",
    trial_expiration="",
    commercial_use=True,
    region_restrictions="Regional endpoints (us-south, eu-de, ...); Lite services expire after 30 idle days.",
    data_retention="IBM Cloud data handling per service terms; Lite plan has no customization.",
    official_docs_url="https://cloud.ibm.com/docs/speech-to-text",
    pricing_url="https://www.ibm.com/products/speech-to-text/pricing",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=True,
    supports_diarization=True,
    max_speakers=2,
    supports_word_timestamps=True,
    supports_segment_timestamps=False,
    supports_confidence=True,
    vocabulary_supported=False,
    vocabulary_max_terms=0,
    vocabulary_parameter="customization is unavailable on the Lite plan (documented)",
    supports_smart_formatting=False,
    supports_audio_enhancement=False,
    experimental=False,
    quality_score=0.83,
    min_quality_score=0.80,
    default_model="fa-IR_BroadbandModel",
    models=("fa-IR_BroadbandModel", "en-US_BroadbandModel", "en-GB_BroadbandModel"),
    concurrency=2,
    chunking_policy="whole_file_only",
)

_AWS_TRANSCRIBE = STTProviderInfo(
    provider_slug="aws_transcribe",
    display_name="Amazon Transcribe",
    protocol=STTProtocol.PROVIDER_ASYNC_JOB.value,
    base_url="https://transcribe.{region}.amazonaws.com",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.SIGV4.value,
    language_capabilities=("en-US and others", "fa-IR is STREAMING-only — batch Persian is NOT supported"),
    model_capabilities=("standard batch", "medical", "call analytics", "custom vocabulary (paid add-on)", "PII redaction (paid add-on)"),
    max_file_size=2_000_000_000,
    max_audio_duration=14400,
    max_concurrency=100,
    rate_limits="Free tier: 60 minutes/month for the first 12 months (new accounts); 15-second minimum billing (verified 2026-10-09)",
    free_type=STTProviderClass.PROMOTIONAL_FREE.value,
    free_limit="60 minutes/month for the first 12 months after account creation (standard transcription only)",
    free_reset="monthly for 12 months",
    trial_expiration="free allowance ends 12 months after the first transcription request",
    commercial_use=True,
    region_restrictions="Regional endpoints; batch jobs require an S3 bucket for input/output.",
    data_retention="AWS service terms; batch output is written to the customer's S3 bucket.",
    official_docs_url="https://docs.aws.amazon.com/transcribe/",
    pricing_url="https://aws.amazon.com/transcribe/pricing/",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=False,
    supports_diarization=True,
    max_speakers=30,
    supports_word_timestamps=True,
    supports_segment_timestamps=False,
    supports_confidence=True,
    vocabulary_supported=True,
    vocabulary_max_terms=100,
    vocabulary_parameter="VocabularyName (requires a pre-created custom vocabulary)",
    supports_smart_formatting=False,
    supports_audio_enhancement=False,
    experimental=True,
    quality_score=0.80,
    min_quality_score=0.80,
    default_model="standard",
    models=("standard",),
    concurrency=1,
    chunking_policy="whole_file_only",
)

_AZURE_SPEECH = STTProviderInfo(
    provider_slug="azure_speech",
    display_name="Azure Speech in Foundry Tools",
    protocol=STTProtocol.PROVIDER_ASYNC_JOB.value,
    base_url="https://{region}.api.cognitive.microsoft.com",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.API_KEY_HEADER.value,
    language_capabilities=("fa (fa-IR)", "en-US", "100+ locales"),
    model_capabilities=("realtime", "fast transcription", "batch transcription (v3.2+)", "diarization", "custom models (paid)"),
    max_file_size=500_000_000,
    max_audio_duration=36000,
    max_concurrency=20,
    rate_limits="F0: 5 audio hours/month REAL-TIME only — the official pricing page states batch is NOT supported on F0 (verified 2026-10-09)",
    free_type=STTProviderClass.FREE_MONTHLY.value,
    free_limit="F0: 5 audio hours/month realtime standard STT; batch transcription requires paid S0",
    free_reset="monthly (realtime only)",
    trial_expiration="",
    commercial_use=True,
    region_restrictions="Regional endpoints; F0 batch is unavailable — do not route batch files to F0.",
    data_retention="See Microsoft data privacy documentation; customer content is processed in the resource's region.",
    official_docs_url="https://learn.microsoft.com/azure/ai-services/speech-service/",
    pricing_url="https://azure.microsoft.com/en-us/pricing/details/speech/",
    last_verified_at=VERIFIED_AT,
    enabled=True,
    persian_batch=True,
    supports_diarization=True,
    max_speakers=2,
    supports_word_timestamps=True,
    supports_segment_timestamps=False,
    supports_confidence=True,
    vocabulary_supported=False,
    vocabulary_max_terms=0,
    vocabulary_parameter="custom model phrases (paid custom models only)",
    supports_smart_formatting=False,
    supports_audio_enhancement=False,
    experimental=True,
    quality_score=0.82,
    min_quality_score=0.80,
    default_model="standard",
    models=("standard",),
    concurrency=1,
    chunking_policy="whole_file_only",
)

#: Azure F0 note: batch transcription is NOT available on the free tier. The
#: adapter implements the batch API, but the router never selects azure_speech
#: for a batch file while only an F0 (free) entitlement is known — batch on F0
#: would deterministically fail (spec §24).

_SONIOX = STTProviderInfo(
    provider_slug="soniox",
    display_name="Soniox",
    protocol=STTProtocol.PROVIDER_ASYNC_JOB.value,
    base_url="https://api.soniox.com/v1",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.API_KEY_HEADER.value,
    language_capabilities=("fa", "en-US", "auto (omit language_hints)", "60+ languages"),
    model_capabilities=("stt-async-v3", "stt-rt-v5", "speaker diarization", "translation", "custom context"),
    max_file_size=1_000_000_000,
    max_audio_duration=None,
    max_concurrency=20,
    rate_limits="Pay-as-you-go token metering (~$0.10/hour async); no published free API quota (verified 2026-10-09)",
    free_type=STTProviderClass.PAID_ONLY.value,
    free_limit=(
        "No new-account free API credits: Soniox discontinued free API credits for new "
        "signups on 2025-10-27 (official blog). The free 'credits' belong to the Soniox "
        "app, not the API."
    ),
    free_reset="",
    trial_expiration="",
    commercial_use=True,
    region_restrictions="US/EU/JP regions.",
    data_retention="See Soniox privacy policy.",
    official_docs_url="https://soniox.com/docs",
    pricing_url="https://soniox.com/pricing",
    last_verified_at=VERIFIED_AT,
    enabled=False,
    persian_batch=True,
    supports_diarization=True,
    max_speakers=15,
    supports_word_timestamps=True,
    supports_segment_timestamps=False,
    supports_confidence=True,
    vocabulary_supported=True,
    vocabulary_max_terms=10000,
    vocabulary_parameter="custom_instructions / context terms",
    supports_smart_formatting=False,
    supports_audio_enhancement=False,
    experimental=False,
    quality_score=0.84,
    min_quality_score=0.80,
    default_model="stt-async-v3",
    models=("stt-async-v3", "stt-rt-v5"),
    concurrency=1,
    chunking_policy="whole_file_only",
)

_ELEVENLABS_SCRIBE = STTProviderInfo(
    provider_slug="elevenlabs_scribe",
    display_name="ElevenLabs Scribe v2",
    protocol=STTProtocol.OPENAI_AUDIO_TRANSCRIPTIONS.value,
    base_url="https://api.elevenlabs.io/v1",
    service_type="batch_saas",
    batch_supported=True,
    realtime_supported=True,
    streaming_supported=True,
    authentication_type=STTAuthType.API_KEY_HEADER.value,
    language_capabilities=("fa", "en-US", "auto (omit language_code)", "90+ languages, within-file detection"),
    model_capabilities=("scribe_v2", "scribe_v2_realtime", "keyterm prompting (<=1000)", "entity detection", "speaker diarization (<=32)"),
    max_file_size=3_000_000_000,
    max_audio_duration=36000,
    max_concurrency=20,
    rate_limits="Free plan: 10,000 credits/month shared across TTS/STT/agents (~30 min STT at 330 credits/min); attribution required (verified 2026-10-09)",
    free_type=STTProviderClass.FREE_MONTHLY.value,
    free_limit="Free plan 10,000 credits/month shared across products; ~30 minutes of Scribe API per month",
    free_reset="monthly",
    trial_expiration="",
    commercial_use=False,
    region_restrictions="",
    data_retention=(
        "The free plan requires ElevenLabs attribution and does not include a "
        "commercial license (official pricing page, verified 2026-10-09)."
    ),
    official_docs_url="https://elevenlabs.io/docs/capabilities/speech-to-text",
    pricing_url="https://elevenlabs.io/pricing",
    last_verified_at=VERIFIED_AT,
    enabled=False,
    persian_batch=True,
    supports_diarization=True,
    max_speakers=32,
    supports_word_timestamps=True,
    supports_segment_timestamps=False,
    supports_confidence=False,
    vocabulary_supported=True,
    vocabulary_max_terms=1000,
    vocabulary_parameter="keyterm_prompting (paid add-on $0.05/hour)",
    supports_smart_formatting=False,
    supports_audio_enhancement=False,
    experimental=False,
    quality_score=0.85,
    min_quality_score=0.80,
    default_model="scribe_v2",
    models=("scribe_v2", "scribe_v2_realtime"),
    concurrency=1,
    chunking_policy="whole_file_only",
)

#: ElevenLabs note: Scribe is NOT permanently free. The free plan's 10k credits
#: are shared with TTS/agents, require attribution and exclude commercial use,
#: so Gamas (a commercial product) must not route production traffic there on
#: the free plan. The provider stays disabled until a paid plan is configured.

STT_PROVIDER_REGISTRY: dict[str, STTProviderInfo] = {
    info.provider_slug: info
    for info in (
        _SPEECHMATICS,
        _DEEPGRAM,
        _GROQ,
        _GEMINI_TRANSCRIBE,
        _OPENAI_COMPATIBLE,
        _ASSEMBLYAI,
        _GLADIA,
        _GOOGLE_CLOUD_STT,
        _IBM_WATSON_STT,
        _AWS_TRANSCRIBE,
        _AZURE_SPEECH,
        _SONIOX,
        _ELEVENLABS_SCRIBE,
    )
}

#: Default accuracy-first route for the standard Gamas job (Persian batch
#: lecture). Data-driven: ``stt_routes`` rows and STT_DEFAULT_ROUTE override it
#: without code changes (spec §28/§50).
DEFAULT_STT_ROUTE: tuple[str, ...] = (
    "gemini_transcribe",
    "groq",
    "speechmatics",
    "deepgram",
    "assemblyai",
    "gladia",
    "ibm_watson_stt",
    "google_cloud_stt",
)

#: Every slug the credential store may hold for the STT service.
STT_PROVIDER_CHOICES = frozenset(STT_PROVIDER_REGISTRY)

_CLASS_FA = {
    STTProviderClass.PERMANENT_FREE: "رایگان دائمی",
    STTProviderClass.FREE_MONTHLY: "سهمیهٔ رایگان ماهانه",
    STTProviderClass.FREE_ALLOCATION: "پلن رایگان با محدودیت ریت",
    STTProviderClass.FREE_CREDIT: "اعتبار اولیهٔ یک‌بارمصرف",
    STTProviderClass.PROMOTIONAL_FREE: "رایگان تبلیغاتی (موقت)",
    STTProviderClass.TRIAL_ONLY: "فقط آزمایشی",
    STTProviderClass.PAID_ONLY: "فقط پولی",
    STTProviderClass.REGION_RESTRICTED: "محدود به منطقه",
    STTProviderClass.UNSUPPORTED: "پشتیبانی‌نشده",
}


def stt_registry_info(slug: str) -> STTProviderInfo | None:
    """Registry record for ``slug`` (``None`` when Gamas does not know it)."""
    return STT_PROVIDER_REGISTRY.get((slug or "").strip().lower())


def free_class_fa(slug: str) -> str:
    """Human-readable Persian label for the admin panel."""
    info = stt_registry_info(slug)
    if info is None:
        return "ناشناخته"
    return _CLASS_FA.get(info.classification, info.free_type)


def persian_batch_supported(slug: str) -> bool:
    """Hard Persian-first gate (spec §10): no Persian speech to English-only engines."""
    info = stt_registry_info(slug)
    return bool(info and info.persian_batch)


def capability_matrix() -> list[dict]:
    """One row per provider for the admin capability matrix (spec §47).

    This is the ONLY place capabilities live; routing and the admin panel both
    read the registry directly.
    """
    rows = []
    for info in STT_PROVIDER_REGISTRY.values():
        rows.append(
            {
                "provider": info.provider_slug,
                "display_name": info.display_name,
                "protocol": info.protocol,
                "persian_batch": info.persian_batch,
                "auto_language_detection": "auto" in " ".join(info.language_capabilities),
                "multilingual": "multi" in " ".join(info.language_capabilities),
                "batch": info.batch_supported,
                "realtime": info.realtime_supported,
                "streaming": info.streaming_supported,
                "diarization": info.supports_diarization,
                "max_speakers": info.max_speakers,
                "word_timestamps": info.supports_word_timestamps,
                "segment_timestamps": info.supports_segment_timestamps,
                "confidence": info.supports_confidence,
                "vocabulary": info.vocabulary_supported,
                "vocabulary_max_terms": info.vocabulary_max_terms,
                "vocabulary_parameter": info.vocabulary_parameter,
                "smart_formatting": info.supports_smart_formatting,
                "audio_enhancement": info.supports_audio_enhancement,
                "max_file_size": info.max_file_size,
                "max_duration_seconds": info.max_audio_duration,
                "free_type": info.free_type,
                "free_limit": info.free_limit,
                "commercial_use": info.commercial_use,
                "region_restrictions": info.region_restrictions,
                "experimental": info.experimental,
                "enabled": info.enabled,
                "quality_score": info.quality_score,
                "concurrency": info.concurrency,
            }
        )
    return rows
