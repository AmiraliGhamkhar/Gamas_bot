"""Configuration for Gamas Bot v3.

One frozen dataclass, one ``from_env`` factory, one ``check`` self-test. Every
value the process needs lives here; nothing else reads ``os.environ`` at
runtime (``load_dotenv`` is applied once, here).

Design rules:

* **One settings dataclass.** Anything that existed only to support the deleted
  provider platforms (routes, registries, quotas, credentials) is gone.
* Provider order *is* failover policy: ``STT_PROVIDERS=speechmatics,deepgram``
  means "try Speechmatics, then Deepgram".
* Relative paths are anchored to the project directory, so a cron job or a
  ``su -c`` start cannot accidentally create a second database or session.
* Accounting is always in integer seconds; hours exist only in the tariff
  knobs and the user-facing copy.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

TRUTHY = {"1", "true", "yes", "on"}

#: Project directory: the folder that contains this package's parent.
PROJECT_DIR = Path(__file__).resolve().parent.parent

#: CR-3: the booklet is always produced in FULL (class-replacement) mode.
#: The tuple is kept (with a single value) because the note model still
#: normalises the stored ``note_mode`` field through it for backward
#: compatibility with previously archived booklets.
NOTE_MODES = ("full",)
RAW_TEXT_PROFILES = ("plain", "timestamped")

STT_PROVIDER_ORDER = ("speechmatics", "deepgram", "openai_compatible")

#: Every note provider the pipeline knows, with the request style each one
#: speaks. ``nara``/``openrouter``/``openai_compatible`` are OpenAI-compatible
#: chat-completions APIs; ``gemini`` and ``anthropic`` have their own contracts.
NOTE_PROVIDER_SLUGS = (
    "nara",
    "openrouter",
    "groq",
    "cerebras",
    "openai_compatible",
    "gemini",
    "anthropic",
)
NOTE_REQUEST_STYLE = {
    "nara": "openai",
    "openrouter": "openai",
    "groq": "openai",
    "cerebras": "openai",
    "openai_compatible": "openai",
    "gemini": "gemini",
    "anthropic": "anthropic",
}
NOTE_PROVIDER_LABELS = {
    "nara": "NaraRouter",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "cerebras": "Cerebras",
    "openai_compatible": "OpenAI-compatible",
    "gemini": "Gemini",
    "anthropic": "Anthropic",
}
#: OpenAI ``response_format`` JSON mode is only sent to providers whose public
#: API documents it (NaraRouter does not document it, so asking for it there
#: would be an unsupported parameter). Groq and Cerebras document it on their
#: OpenAI-compatible chat-completions endpoints.
NOTE_JSON_OBJECT_PROVIDERS = frozenset({"openrouter", "groq", "cerebras", "openai_compatible"})
#: Provider hostnames, so a deployment that configures one OpenAI-compatible
#: slot with a ``NOTE_BASE_URL`` pointing at a documented provider keeps that
#: provider's place in the priority order (and never becomes a duplicate leg).
#: NaraRouter documents both ``router.bynara.id`` and ``router.bynara.web.id``
#: (https://router.bynara.id/docs); Google AI Studio's native REST host is
#: ``generativelanguage.googleapis.com``.
NOTE_HOST_PROVIDERS = (
    ("bynara.id", "nara"),
    ("bynara.web.id", "nara"),
    ("openrouter.ai", "openrouter"),
    ("groq.com", "groq"),
    ("cerebras.ai", "cerebras"),
    ("generativelanguage.googleapis.com", "gemini"),
)

#: Documented API defaults per provider. Nara's free plan currently lists
#: ``agnes-2.5-flash`` (https://router.bynara.id/api/plans). The OpenRouter
#: default is one *pinned* free model, ``google/gemma-4-31b-it:free``, not the
#: ``openrouter/free`` router: the router picks a model per request, so the
#: window that actually serves a request is unknown when the bot sizes it,
#: while the pinned model's endpoint serves a documented 262,144-token
#: context with 32,768 completion tokens (https://openrouter.ai/api/v1/models).
#: Both are overridable, and no model is called free without this evidence.
DEFAULT_NARA_BASE_URL = "https://router.bynara.id/v1"
DEFAULT_NARA_MODEL = "agnes-2.5-flash"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "google/gemma-4-31b-it:free"
#: Groq's OpenAI-compatible endpoint. Its default model is the served
#: ``openai/gpt-oss-120b`` id (the namespace is part of the id on Groq).
DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
#: Cerebras' OpenAI-compatible endpoint; its model ids carry no namespace.
DEFAULT_CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
DEFAULT_CEREBRAS_MODEL = "gpt-oss-120b"
#: Google AI Studio's default note model. ``flash-lite`` answers note
#: extraction/organisation requests several times faster than the thinking
#: models while staying inside the free tier.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"
#: Gemini thinking budget sent as ``generationConfig.thinkingConfig.thinkingBudget``:
#:
#: * ``0`` (default) disables thinking outright — the note task is extraction
#:   and organisation, so hidden reasoning tokens are pure latency; the model
#:   answers as soon as it starts emitting JSON.
#: * a positive value caps the reasoning tokens the model may spend.
#: * ``-1`` sends no ``thinkingConfig`` field at all (pre-flag behaviour).
DEFAULT_GEMINI_THINKING_BUDGET = 0
NOTE_BASE_URL_DEFAULTS = {
    "nara": DEFAULT_NARA_BASE_URL,
    "openrouter": DEFAULT_OPENROUTER_BASE_URL,
    "groq": DEFAULT_GROQ_BASE_URL,
    "cerebras": DEFAULT_CEREBRAS_BASE_URL,
    "openai_compatible": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "anthropic": "https://api.anthropic.com/v1",
}

#: Default note failover order: Gemini flash-lite first (fastest documented
#: route), then NaraRouter, OpenRouter, Groq and Cerebras. A slug without its
#: own credential is skipped silently, so this default degrades safely for
#: every existing deployment. The order in ``NOTE_PROVIDERS`` *is* the failover
#: policy, exactly like ``STT_PROVIDERS``.
DEFAULT_NOTE_PROVIDERS = "gemini,nara,openrouter,groq,cerebras"

DEFAULT_NOTE_MODELS = {
    "openai_compatible": "gpt-4o-mini",
    "gemini": DEFAULT_GEMINI_MODEL,
    "nara": DEFAULT_NARA_MODEL,
    "openrouter": DEFAULT_OPENROUTER_MODEL,
    "groq": DEFAULT_GROQ_MODEL,
    "cerebras": DEFAULT_CEREBRAS_MODEL,
}

#: Context window assumed for one note request when the operator does not say.
#: It is the *smallest* window among the configured models that matters, because
#: the chain must fit the tightest provider that may answer. The default pair
#: is matched on purpose: the pinned OpenRouter endpoint serves a 262,144-token
#: window and Agnes 2.5 Flash's official reference window is 512K (Nara's own
#: gateway cap is unpublished), so the operational assumption is half of the
#: matched served window — headroom for gateway caps and for the deliberately
#: conservative Persian character-to-token estimate in ``notes.py``.
DEFAULT_NOTE_CONTEXT_TOKENS = 131072
#: Per-chunk character ceiling of one note request (an upper bound that the
#: token budget in ``notes.effective_chunk_chars`` may still shrink).
DEFAULT_NOTE_CHUNK_CHARS = 64000
#: Output-token budget sent as ``max_tokens`` on every note request. It stays
#: at half (or less) of the default models' documented completion caps
#: (32,768 on the pinned OpenRouter endpoint, 65.5K for Agnes 2.5 Flash), so
#: the budget itself never exceeds what a default model may emit.
DEFAULT_NOTE_MAX_OUTPUT_TOKENS = 16384
#: Wall-clock deadline of one note request, in seconds. A hung provider must
#: not inherit the STT job's multi-hour read timeout — but the deadline must
#: still be long enough for the configured *output* budget to be produced by
#: a slow (free-tier, shared-pool) gateway. The output budget, not the input
#: size, is what fills this window: the default 16,384 output tokens need
#: several minutes on such a gateway, which is why this is configurable
#: instead of hard-wired. The default is paired with the default output
#: budget so the shipped configuration is coherent (see ``check``).
DEFAULT_NOTE_REQUEST_TIMEOUT_SECONDS = 900.0
#: Conservative output speed (tokens per second) a free or shared note
#: gateway is assumed to sustain. Used only to warn that a configured output
#: budget cannot be produced inside the request deadline — the warning is
#: advisory, because a fast or paid gateway easily beats this estimate.
MIN_NOTE_OUTPUT_TOKENS_PER_SECOND = 20.0
#: ``reasoning_effort`` levels accepted on the Nara/OpenRouter note routes
#: (the two gateways verified for the field). ``ultra`` is accepted as the
#: documented alias of ``max`` (https://router.bynara.id/docs).
NOTE_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
#: A request needs at least this much room for the transcript itself.
MIN_NOTE_INPUT_TOKENS = 4096
#: Headroom for the note system prompt and provider/request scaffolding.
MIN_NOTE_PROMPT_TOKENS = 4096

#: Environment variable -> resolved settings attribute for the tariff. The
#: attribute spelling matches the dataclass field exactly.
PLAN_ENV_FIELDS = (
    ("FREE_PLAN_HOURS", "free_plan_hours", 1),
    ("PLAN_5_HOURS", "plan_5_hours", 5),
    ("PLAN_5_PRICE_TOMAN", "plan_5_price_toman", 50_000),
    ("PLAN_5_VALIDITY_DAYS", "plan_5_validity_days", 30),
    ("PLAN_10_HOURS", "plan_10_hours", 10),
    ("PLAN_10_PRICE_TOMAN", "plan_10_price_toman", 75_000),
    ("PLAN_10_VALIDITY_DAYS", "plan_10_validity_days", 30),
    ("PLAN_20_HOURS", "plan_20_hours", 20),
    ("PLAN_20_PRICE_TOMAN", "plan_20_price_toman", 130_000),
    ("PLAN_20_VALIDITY_DAYS", "plan_20_validity_days", 30),
    ("PLAN_25_HOURS", "plan_25_hours", 25),
    ("PLAN_25_PRICE_TOMAN", "plan_25_price_toman", 150_000),
    ("PLAN_25_VALIDITY_DAYS", "plan_25_validity_days", 30),
    ("PLAN_50_HOURS", "plan_50_hours", 50),
    ("PLAN_50_PRICE_TOMAN", "plan_50_price_toman", 250_000),
    ("PLAN_50_VALIDITY_DAYS", "plan_50_validity_days", 30),
)

#: Default failover order for speech-to-text. Only configured providers are
#: ever attempted, so this default is safe with zero STT keys set.
#:
#: The order is a *latency* policy, not a preference: the first configured
#: Speechmatics leads the default chain (Persian accuracy primary), with
#: Deepgram as immediate non-blocking overflow and failover when Speechmatics
#: reaches ``SPEECHMATICS_MAX_IN_FLIGHT`` or returns a recoverable error.
DEFAULT_STT_PROVIDERS = "speechmatics,deepgram,openai_compatible"

#: Conservative disk and media ceilings for a small shared-hosting account.
#: Operators can raise them after checking their actual disk quota and CPU plan.
DEFAULT_MAX_FILE_SIZE_BYTES = 128 * 1024**2
DEFAULT_TEMP_MIN_FREE_BYTES = 64 * 1024**2
DEFAULT_MAX_MEDIA_DURATION_SECONDS = 2 * 60 * 60
DEFAULT_PRESENTATION_MAX_CLIPS = 50
DEFAULT_PRESENTATION_MAX_DURATION_SECONDS = 2 * 60 * 60
DEFAULT_PRESENTATION_MAX_UNPACKED_BYTES = 128 * 1024**2
DEFAULT_PRESENTATION_MAX_TEMP_BYTES = 256 * 1024**2

#: Default stage and provider concurrency controls tuned conservatively for
#: shared cPanel hosting (1 GB RAM, limited CPU, disk I/O and process slots).
DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
DEFAULT_MAX_CONCURRENT_UPLOADS = 2
DEFAULT_SPEECHMATICS_MAX_IN_FLIGHT = 2
DEFAULT_DEEPGRAM_MAX_IN_FLIGHT = 4
DEFAULT_STT_OPENAI_MAX_IN_FLIGHT = 2
DEFAULT_STT_PROVIDER_TIMEOUT_SECONDS = 1800.0
#: How long a job may wait for *any* STT provider slot when every configured
#: provider is simultaneously at its local in-flight cap. The non-blocking
#: overflow pass runs first, so this bound is only reached when there is no
#: provider left to overflow to. It must be comparable to how long an attempt
#: legitimately holds a slot (up to ``STT_PROVIDER_TIMEOUT_SECONDS``): the old
#: 15 s default guaranteed that a job arriving while the default two
#: Speechmatics slots were busy transcribing (minutes, not seconds) failed
#: outright instead of taking the first freed slot. 600 s stays bounded —
#: waiter count and active-job count are separately capped — while covering
#: the ordinary "one of the running transcriptions finishes soon" case.
#: Lower it for deliberate fail-fast behaviour.
DEFAULT_STT_SLOT_WAIT_TIMEOUT_SECONDS = 600.0
DEFAULT_LLM_PRIMARY_MAX_IN_FLIGHT = 4
DEFAULT_LLM_FALLBACK_MAX_IN_FLIGHT = 4
#: Same reasoning as the STT slot wait, against note requests that may
#: legitimately run for minutes (``NOTE_REQUEST_TIMEOUT_SECONDS`` allows
#: 900 s): a 30 s bound made excess chunk requests fail whenever a single
#: configured route's slots were busy with slow completions. 300 s is the
#: bounded wait; primary saturation still overflows to a secondary route in
#: 0 ms whenever one has a free slot.
DEFAULT_LLM_SLOT_WAIT_TIMEOUT_SECONDS = 300.0
DEFAULT_DEDUP_WINDOW_SECONDS = 120.0
#: Legacy Free Office UI compatibility setting. The bot no longer starts an
#: Office job lane; the standalone export modules remain available to callers.
DEFAULT_OFFICE_JOB_CONCURRENCY = 2

#: Shipped note-layer performance defaults. Each is a documented trade-off, so
#: they live here rather than as literals in ``from_env``:
#:
#: * ``NOTE_CONCURRENCY`` — the map phase never starts more requests than there
#:   are chunks (capped at 8); 3 keeps per-job request bursts modest on shared hosts.
#: * ``NOTE_TTFB_TIMEOUT_SECONDS`` — a route that has not produced a first byte
#:   within 25 s is treated as hung and the chain fails over, instead of waiting
#:   out the whole request timeout. Below the hedge delay on purpose.
#: * ``NOTE_HEDGE_AFTER_SECONDS`` — if the first provider has not finished in
#:   60 s, the second starts in parallel; at most one extra in-flight POST.
DEFAULT_NOTE_CONCURRENCY = 3
DEFAULT_NOTE_TTFB_TIMEOUT_SECONDS = 25.0
DEFAULT_NOTE_HEDGE_AFTER_SECONDS = 60.0

#: Per-provider direct-upload ceilings (bytes). A file larger than a
#: provider's ceiling is routed to the next configured provider instead of
#: being split — chunked audio loses word context at every boundary. Both
#: values are the conservative (decimal) reading of the documented limit, so a
#: file is never uploaded only to be rejected.
#:
#: * Speechmatics Batch rejects a ``/jobs`` POST body of 1 GB or more
#:   (https://docs.speechmatics.com/speech-to-text/batch/limits); larger files
#:   would need their ``fetch_data`` URL form, which this bot does not use.
#: * Deepgram pre-recorded audio accepts a maximum of 2 GB
#:   (https://developers.deepgram.com/docs/pre-recorded-audio).
SPEECHMATICS_MAX_UPLOAD = 1_000_000_000
DEEPGRAM_MAX_UPLOAD = 2_000_000_000
# OpenAI's hosted audio transcription endpoint has a 25 MB per-file limit.
# Compatible gateways can set STT_OPENAI_MAX_UPLOAD_BYTES to their own ceiling.
OPENAI_STT_MAX_UPLOAD = 25_000_000

#: Default Deepgram model. Persian (``fa``) is transcribed by Nova-3; Nova-2
#: has no Persian language pack, so ``nova-2`` + ``STT_LANGUAGE=fa`` is
#: rejected by the API (see https://developers.deepgram.com/docs/models-languages-overview).
DEFAULT_DEEPGRAM_MODEL = "nova-3"

#: Deepgram models whose documented language list contains Persian (``fa``).
DEEPGRAM_PERSIAN_MODELS = frozenset({"nova-3", "nova-3-general"})


def _project_path(value: str | None, default: str) -> Path:
    """Anchor a configured path to the project directory."""
    raw = (value or default).strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    raw = _env(name, str(default))
    try:
        value = int(float(raw))
    except ValueError:
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _plan_value(name: str, default: int) -> int:
    """Read one tariff value *without* clamping it.

    ``_env_int`` clamps to its minimum, which turned ``PLAN_5_PRICE_TOMAN=0``
    into a 1-Toman price instead of a visible configuration error — the exact
    kind of silently-quoted number this project refuses elsewhere. The raw
    integer is kept here so ``billing._resolved`` can fall back to the
    canonical default *and* ``check()`` can report the mistake at startup.
    """
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        value = default
    if not math.isfinite(value):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in TRUTHY


#: The two evidence-gate behaviours. ``repair`` is the default; ``warn`` exists
#: so an operator can observe the audit on real lectures before it is allowed
#: to change an answer.
NOTE_QUALITY_GATES = ("repair", "warn")


def resolve_note_quality_gate(value: str | None) -> str:
    """Resolve ``NOTE_QUALITY_GATE``; an unknown value means ``repair``."""
    resolved = (value or "").strip().lower()
    return resolved if resolved in NOTE_QUALITY_GATES else "repair"


#: Container policies for the prepared STT audio. ``auto`` weighs encoder CPU
#: against transfer time; the others force one container.
AUDIO_FORMAT_POLICIES = ("auto", "wav", "opus")


def resolve_audio_format_policy(value: str | None) -> str:
    """Resolve ``AUDIO_FORMAT_POLICY``; an unknown value means ``auto``."""
    resolved = (value or "").strip().lower()
    return resolved if resolved in AUDIO_FORMAT_POLICIES else "auto"


def normalize_note_compile_provider(value: str | None) -> str:
    """Resolve ``NOTE_COMPILE_PROVIDER`` to a known note-provider slug.

    An unknown slug resolves to empty (the configured route order) instead of
    being forwarded: pinning the compile to a provider that does not exist
    would otherwise silently disable the compile pass.
    """
    resolved = (value or "").strip().lower()
    return resolved if resolved in NOTE_PROVIDER_LABELS else ""


def resolve_note_mode(mode: str | None) -> str:
    """Resolve the sole note mode to ``"full"`` for *any* input.

    CR-3 removed the two legacy compression modes. This is deliberately a
    total function: old ``NOTE_MODE`` env values, junk, stored DB values and
    ``None`` all resolve to ``"full"`` so startup and archived booklets never
    crash. ``mode`` is accepted for backward-compatible call sites only.
    """
    return "full"


def resolve_note_reasoning_effort(effort: str | None) -> str:
    """Resolve ``NOTE_REASONING_EFFORT``; empty means \"provider default\".

    An unrecognised value resolves to empty rather than being forwarded: an
    unsupported level must never become a request a provider rejects.
    """
    value = (effort or "").strip().lower()
    if value == "ultra":
        value = "max"
    return value if value in NOTE_REASONING_EFFORTS else ""


def resolve_raw_text_profile(profile: str | None) -> str:
    value = (profile or "").strip().lower()
    return value if value in RAW_TEXT_PROFILES else "plain"


def format_payment_card(number: str) -> str:
    """Group the stored card number for display; never alters the stored value."""
    digits = re.sub(r"\D", "", str(number))
    return " ".join(digits[index : index + 4] for index in range(0, len(digits), 4))


@dataclass(frozen=True)
class Settings:
    """The whole configuration surface of the process."""

    # --- Telegram -----------------------------------------------------------
    bot_token: str
    api_id: int
    api_hash: str
    admin_ids: frozenset[int]
    session_path: Path

    # --- Storage / logging --------------------------------------------------
    db_path: Path
    temp_dir: Path
    log_file: Path | None
    log_level: str
    log_format: str
    log_max_bytes: int
    log_backup_count: int

    # --- Limits -------------------------------------------------------------
    max_file_size: int
    #: Hard ceiling for measured audio/video duration, in seconds. Telegram's
    #: duration hint is checked at intake and PyAV's probe is checked again.
    max_media_duration_seconds: float
    max_active_jobs: int
    max_active_jobs_per_user: int
    max_queued_jobs: int
    media_worker_concurrency: int
    media_timeout: float
    convert_timeout: float
    stt_job_timeout: float
    stt_poll_interval: float
    #: Free space that must remain on ``temp_dir`` before a download starts.
    #: A long lecture plus its extracted audio is several gigabytes; a full
    #: disk mid-download used to fail the job with a provider-level error.
    temp_min_free_bytes: int
    #: A download that produces no bytes for this many seconds is aborted.
    #: Telegram can stall a transfer indefinitely; the job used to inherit no
    #: deadline at all (there is no job-level timeout by design).
    download_stall_seconds: float

    # --- Presentations ------------------------------------------------------
    presentation_max_clips: int
    presentation_max_total_duration: float
    presentation_max_unpacked_bytes: int
    presentation_max_temp_bytes: int
    presentation_min_clip_seconds: float
    presentation_silence_seconds: float
    presentation_wav_limit_bytes: int
    presentation_include_slide_text: bool
    presentation_include_video_audio: bool
    presentation_include_unreferenced_media: bool

    # --- Media preparation ---------------------------------------------------
    #: Which container the prepared STT audio may use: ``auto`` lets the media
    #: layer pick the cheapest of PCM WAV and Opus from the measured transfer
    #: rate, ``wav``/``opus`` force one.
    audio_format_policy: str
    #: Concurrent byte-range downloads of one Telegram file. ``1`` is the
    #: single-stream behaviour Telethon's ``download_media`` uses.
    telegram_download_connections: int

    # --- Speech-to-text -----------------------------------------------------
    stt_providers: tuple[str, ...]
    stt_language: str
    speechmatics_api_key: str
    speechmatics_base_url: str
    speechmatics_model: str
    deepgram_api_key: str
    deepgram_base_url: str
    deepgram_model: str
    stt_openai_base_url: str
    stt_openai_api_key: str
    stt_openai_model: str
    stt_openai_max_upload: int
    #: Deadline for one upload/download transfer of an STT request, in seconds.
    #: It bounds the transfer, never the provider's processing time (that is
    #: ``stt_job_timeout``), so a stalled socket cannot hold a worker for hours.
    stt_transfer_timeout: float
    #: Hard ceiling on an STT response body read into memory. A corrupted or
    #: hostile answer cannot exhaust the worker's memory.
    max_stt_response_bytes: int
    #: Long-poll window, in seconds, requested from Speechmatics on the submit
    #: and on each status read. The API holds the request open until the job
    #: changes state, so a finished job costs one request instead of a poll
    #: every ``stt_poll_interval``; ``0`` disables the parameter.
    stt_status_long_poll: float

    # --- Note generation ----------------------------------------------------
    note_providers: tuple[str, ...]
    note_provider: str
    note_api_key: str
    note_api_base_url: str
    note_model: str
    nara_api_key: str
    nara_base_url: str
    nara_model: str
    openrouter_api_key: str
    openrouter_base_url: str
    openrouter_model: str
    groq_api_key: str
    groq_base_url: str
    groq_model: str
    cerebras_api_key: str
    cerebras_base_url: str
    cerebras_model: str
    gemini_api_key: str
    gemini_model: str
    #: ``0`` disables Gemini thinking, a positive value caps it, ``-1`` sends no
    #: ``thinkingConfig`` field at all (see ``DEFAULT_GEMINI_THINKING_BUDGET``).
    gemini_thinking_budget: int
    note_context_tokens: int
    note_mode: str
    note_max_output_tokens: int
    note_request_timeout: float
    note_json_mode: bool
    note_chunk_chars: int
    note_concurrency: int
    note_compile: bool
    note_compile_max_chars: int
    note_reasoning_effort: str
    #: Evidence gate: audit the generated notes against the source and repair
    #: what is missing. Off restores the pre-v4 behaviour exactly.
    note_strict_grounding: bool
    #: ``repair`` performs the one bounded repair request; ``warn`` only audits
    #: and logs, which is the safe first-week setting for a new deployment.
    note_quality_gate: str
    #: Ask OpenRouter to route to the fastest upstream of the model
    #: (``provider.sort=throughput``) instead of its price-weighted default.
    note_openrouter_throughput: bool
    #: Send a response schema alongside the JSON mime type so the provider
    #: cannot answer with prose (which costs a whole second request).
    note_structured_output: bool
    #: Deliver the deterministic merge as a booklet immediately and the
    #: editorial compile as a second, revised file.
    note_progressive_booklet: bool
    #: Slug of the note provider that should serve the editorial compile
    #: (``""`` keeps the configured route order).
    note_compile_provider: str

    # --- Raw text -----------------------------------------------------------
    raw_text_profile: str
    #: Character budget of one delivery part when the raw ``.txt`` upload fails.
    #: A multi-hour transcript is split into parts of this size, because one
    #: document can no longer be re-uploaded and the paginated text fallback
    #: only carries a bounded number of pages.
    raw_fallback_file_chars: int
    #: Hard ceiling on the number of parts the raw transcript may be split
    #: into. A transcript that needs more is refused instead of truncated.
    raw_fallback_max_files: int

    # --- Retired Free Office compatibility ---------------------------------
    #: Legacy UI/job settings remain environment-readable; the bot no longer
    #: routes user messages or starts jobs through this surface.
    office_enabled: bool
    office_max_input_bytes: int
    office_max_chars: int
    office_max_slides: int
    office_session_ttl_minutes: int
    office_min_interval_seconds: float

    # --- Billing / payments --------------------------------------------------
    payment_card_number: str
    payment_card_holder: str
    payment_bank_name: str
    receipt_dir: Path
    receipt_retention_days: int
    max_payment_receipt_bytes: int
    #: Days a finished transcription row is kept before the retention sweep
    #: deletes its text. 0 disables the sweep (rows are kept forever).
    transcript_retention_days: int
    free_plan_hours: int
    plan_5_hours: int
    plan_5_price_toman: int
    plan_5_validity_days: int
    plan_10_hours: int
    plan_10_price_toman: int
    plan_10_validity_days: int
    plan_20_hours: int
    plan_20_price_toman: int
    plan_20_validity_days: int
    plan_25_hours: int
    plan_25_price_toman: int
    plan_25_validity_days: int
    plan_50_hours: int
    plan_50_price_toman: int
    plan_50_validity_days: int

    # --- Stage concurrency, non-blocking provider capacity & deduplication ---
    max_concurrent_downloads: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS
    max_concurrent_uploads: int = DEFAULT_MAX_CONCURRENT_UPLOADS
    download_timeout: float = 900.0
    upload_timeout: float = 600.0
    stt_fallback_enabled: bool = True
    speechmatics_max_in_flight: int = DEFAULT_SPEECHMATICS_MAX_IN_FLIGHT
    deepgram_max_in_flight: int = DEFAULT_DEEPGRAM_MAX_IN_FLIGHT
    stt_openai_max_in_flight: int = DEFAULT_STT_OPENAI_MAX_IN_FLIGHT
    stt_provider_timeout: float = DEFAULT_STT_PROVIDER_TIMEOUT_SECONDS
    stt_slot_wait_timeout: float = DEFAULT_STT_SLOT_WAIT_TIMEOUT_SECONDS
    llm_primary_max_in_flight: int = DEFAULT_LLM_PRIMARY_MAX_IN_FLIGHT
    llm_fallback_max_in_flight: int = DEFAULT_LLM_FALLBACK_MAX_IN_FLIGHT
    llm_slot_wait_timeout: float = DEFAULT_LLM_SLOT_WAIT_TIMEOUT_SECONDS
    dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS
    #: Legacy compatibility value; no Office job queue is started by the bot.
    office_job_concurrency: int = DEFAULT_OFFICE_JOB_CONCURRENCY
    #: Maximum number of delivered transcript/booklet references retained per user.
    library_max_items: int = 50
    #: Ceiling for an ingested study document (PDF/DOCX/PPTX), in bytes. Kept
    #: separate from ``max_file_size`` because a document is parsed into memory
    #: instead of streamed through the media worker.
    doc_max_bytes: int = 50 * 1024 * 1024

    # --- Mandatory channel subscription --------------------------------------
    #: Telegram channel (``@username`` or ``-100...``) whose current members
    #: gate every user-facing feature. Defaults to the official @gamas_AI.
    subscription_channel: str = "@gamas_AI"
    #: Whether the subscription gate is enforced. An operator emergency
    #: off-switch; leave it on in production.
    subscription_required: bool = True
    #: Short-lived cache for a *confirmed* membership, in seconds. ``0``
    #: re-checks on every interaction — the strict, fail-safe default, because
    #: a leaver must be restricted on their very next interaction.
    subscription_cache_seconds: float = 0.0
    #: Per-request timeout of one ``getChatMember`` call, in seconds.
    subscription_timeout_seconds: float = 20.0
    #: Bot API base URL (overridable for a mirror or an offline test).
    telegram_api_base_url: str = "https://api.telegram.org"

    # --- Learning loop (quiz / flashcards) -----------------------------------
    #: Credit (seconds) charged per generated quiz question, before generation.
    quiz_cost_seconds_per_question: int = 60
    #: An active quiz untouched for longer is lazily expired on the next start.
    quiz_max_active_hours: int = 6
    #: Per-user cap on quiz sessions created in any rolling hour.
    quiz_max_sessions_per_hour: int = 3
    #: Credit (seconds) charged for one AI flashcard deck, before generation.
    flashcard_ai_cost_seconds: int = 300
    #: Number of Q/A pairs requested for an AI deck (minimum 5: the validation
    #: gate refunds a generation that cannot produce at least five cards).
    flashcard_ai_deck_size: int = 10
    #: Per-user cap on stored flashcard decks (AI and manual counted together).
    flashcard_max_decks_per_user: int = 30
    #: Parallel per-chunk model calls used by one generation (3–5 is the useful
    #: range; higher starts tripping provider per-minute limits).
    learning_chunk_concurrency: int = 4

    # --- Derived --------------------------------------------------------------
    plan_values: dict[str, int] = field(default_factory=dict, compare=False)

    # --- LLM transport / performance (see the DEFAULT_NOTE_* constants) ------
    llm_connector_limit: int = 32
    llm_connector_limit_per_host: int = 8
    llm_connector_ttl_dns_cache: int = 300
    llm_connector_keepalive: int = 300
    llm_prewarm: bool = True
    note_ttfb_timeout: float = DEFAULT_NOTE_TTFB_TIMEOUT_SECONDS
    note_hedge_after: float = DEFAULT_NOTE_HEDGE_AFTER_SECONDS
    office_ai_concurrency: int = 3
    #: CR-4: after the map-reduce draft, re-scan the source and fill detected
    #: coverage gaps with bounded, strictly grounded LLM requests. Enabled in
    #: production by default; tests use the dataclass default (``False``) so
    #: offline fixtures keep a fixed request count.
    note_completeness_fill: bool = False

    # ------------------------------------------------------------------ views
    @property
    def speechmatics_configured(self) -> bool:
        return bool(self.speechmatics_api_key)

    @property
    def deepgram_configured(self) -> bool:
        return bool(self.deepgram_api_key)

    @property
    def stt_openai_configured(self) -> bool:
        return bool(self.stt_openai_base_url and self.stt_openai_api_key)

    @property
    def stt_configured(self) -> bool:
        """Whether at least one provider in the active order has credentials."""
        configured = {
            "speechmatics": self.speechmatics_configured,
            "deepgram": self.deepgram_configured,
            "openai_compatible": self.stt_openai_configured,
        }
        return any(configured.get(provider, False) for provider in self.stt_providers)

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def groq_configured(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def cerebras_configured(self) -> bool:
        return bool(self.cerebras_api_key)

    @property
    def note_configured(self) -> bool:
        """Whether at least one provider in the active order is usable."""
        return bool(note_routes(self))

    def tariff_values(self) -> dict[str, int]:
        """The resolved tariff mapping consumed by ``billing.plan_catalog``."""
        return dict(self.plan_values)

    @property
    def subscription_channel_url(self) -> str:
        """The public ``https://t.me/...`` link for the mandatory channel.

        A username becomes ``https://t.me/<name>``; a numeric channel id
        becomes the ``/c/`` form. Empty when no channel is configured.
        """
        ref = (self.subscription_channel or "").strip().lstrip("@")
        if not ref:
            return ""
        digits = ref.lstrip("-")
        if digits.isdigit():
            return f"https://t.me/c/{digits}"
        return f"https://t.me/{ref}"


@dataclass(frozen=True, slots=True)
class NoteRoute:
    """One eligible note provider: everything one request needs, and nothing else."""

    slug: str
    label: str
    style: str
    base_url: str
    model: str
    api_key: str
    json_object_mode: bool


def _normalize_api_key(value: str | None) -> str:
    """Strip surrounding whitespace and any accidental ``Bearer `` prefix.

    Operators sometimes copy ``Bearer sk-...`` directly from curl snippets into
    ``.env``; stripping the scheme prevents ``Authorization: Bearer Bearer ...``
    on NaraRouter/OpenRouter and ``x-goog-api-key: Bearer ...`` on Gemini.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    return re.sub(r"^bearer\s+", "", raw, flags=re.IGNORECASE).strip()


def normalize_note_base_url(base_url: str | None, slug: str = "") -> str:
    """Normalize a note provider base URL without losing explicit paths/queries.

    Heals common operator copy-paste mistakes:
    * Pasting a full endpoint URL (``.../chat/completions``, ``.../messages``,
      ``.../models/<id>:generateContent``, or ``.../v1beta/models``).
    * Pasting a bare origin without the documented API version prefix
      (``https://router.bynara.id`` -> ``/v1``, ``https://openrouter.ai`` ->
      ``/api/v1``, ``https://api.groq.com`` -> ``/openai/v1``,
      ``https://api.cerebras.ai`` -> ``/v1``,
      ``https://generativelanguage.googleapis.com`` -> ``/v1beta``).
    """
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")

    for action in (":generateContent", ":streamGenerateContent"):
        if action in path:
            models_idx = path.rfind("/models/")
            if models_idx >= 0:
                path = path[:models_idx]
            else:
                path = path[: path.find(action)]
            break

    for suffix in ("/chat/completions",):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")

    if slug == "anthropic" and path.endswith("/messages"):
        path = path[: -len("/messages")].rstrip("/")

    is_nara_host = (
        host == "bynara.id"
        or host.endswith(".bynara.id")
        or host == "bynara.web.id"
        or host.endswith(".bynara.web.id")
    )
    is_openrouter_host = host == "openrouter.ai" or host.endswith(".openrouter.ai")
    is_groq_host = host == "groq.com" or host.endswith(".groq.com")
    is_cerebras_host = host == "cerebras.ai" or host.endswith(".cerebras.ai")
    is_gemini_host = host == "generativelanguage.googleapis.com"

    if (slug == "gemini" or is_gemini_host) and path.endswith("/models"):
        path = path[: -len("/models")].rstrip("/")

    if path == "":
        if slug == "nara" or is_nara_host:
            path = "/v1"
        elif slug == "openrouter" or is_openrouter_host:
            path = "/api/v1"
        elif slug == "groq" or is_groq_host:
            # Groq serves only under its OpenAI-compatibility prefix.
            path = "/openai/v1"
        elif slug == "cerebras" or is_cerebras_host:
            path = "/v1"
        elif slug == "gemini" or is_gemini_host:
            path = "/v1beta"
    elif path == "/api" and (slug == "openrouter" or is_openrouter_host):
        path = "/api/v1"

    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def classify_note_provider(slug: str, base_url: str) -> str:
    """The provider a configured slot really points at.

    A v2-era deployment configures a single OpenAI-compatible slot through
    ``NOTE_PROVIDER``/``NOTE_BASE_URL``; recognising a documented provider host
    keeps that deployment in its priority place (a NaraRouter base URL stays the
    Nara leg of the chain) instead of turning it into a generic gateway that
    would be tried last — or twice. Google AI Studio's OpenAI compatibility
    layer (``/v1beta/openai``) stays ``openai_compatible`` while its native
    ``/v1beta`` endpoint is classified as ``gemini``.
    """
    parsed = urlsplit(base_url)
    host = (parsed.hostname or "").lower()
    if not host:
        return slug
    path = (parsed.path or "").rstrip("/").lower()
    if host == "generativelanguage.googleapis.com" and (
        path.endswith("/openai") or "/openai/" in path
    ):
        return "openai_compatible"
    for suffix, provider in NOTE_HOST_PROVIDERS:
        if host == suffix or host.endswith("." + suffix):
            return provider
    return slug


def _parse_note_providers(raw: str) -> tuple[str, ...]:
    """Parse the note failover order: known slugs, de-duplicated, order kept."""
    order: list[str] = []
    for part in re.split(r"[,\s]+", raw):
        slug = part.strip().lower()
        if not slug:
            continue
        if slug not in NOTE_PROVIDER_SLUGS:
            logger.warning(
                "Ignoring unknown note provider %r (known: %s)",
                slug, ", ".join(NOTE_PROVIDER_SLUGS),
            )
            continue
        if slug not in order:
            order.append(slug)
    return tuple(order)


def _legacy_note_configured(settings: Settings) -> bool:
    """True when the v2-era ``NOTE_*`` slot holds an explicit configuration."""
    return bool(settings.note_api_key or settings.note_api_base_url)


def note_slot_credentials(settings: Settings, slug: str) -> tuple[str, str, str]:
    """``(api_key, base_url, model)`` configured for one provider slot.

    A credential belongs to exactly one provider: ``NARA_API_KEY``,
    ``OPENROUTER_API_KEY``, ``GROQ_API_KEY``, ``CEREBRAS_API_KEY`` and
    ``GEMINI_API_KEY`` to theirs, and the
    v2-era ``NOTE_API_KEY``/``NOTE_BASE_URL``/``NOTE_MODEL`` trio to the slot
    ``NOTE_PROVIDER`` names (a ``NOTE_BASE_URL`` pointing at a documented
    provider host is classified as that provider, see
    :func:`classify_note_provider`). Sharing one key between two providers would
    send it to a service that never issued it, so a slot with no credential of
    its own is simply not attempted.
    """
    if slug == "nara":
        key, base, model = settings.nara_api_key, settings.nara_base_url, settings.nara_model
    elif slug == "openrouter":
        key = settings.openrouter_api_key
        base, model = settings.openrouter_base_url, settings.openrouter_model
    elif slug == "groq":
        key, base, model = settings.groq_api_key, settings.groq_base_url, settings.groq_model
    elif slug == "cerebras":
        key = settings.cerebras_api_key
        base, model = settings.cerebras_base_url, settings.cerebras_model
    elif slug == "gemini":
        key = settings.gemini_api_key
        base, model = "", settings.gemini_model
    else:  # openai_compatible / anthropic: configured through the v2-era slot
        key, base, model = "", "", ""
    if slug == settings.note_provider and _legacy_note_configured(settings):
        key = settings.note_api_key or key
        base = settings.note_api_base_url or base
        model = settings.note_model or model
    return _normalize_api_key(key), normalize_note_base_url(base, slug), model.strip()


def note_route_order(settings: Settings) -> tuple[str, ...]:
    """The explicit provider order: the declared chain, then the v2-era slot.

    ``NOTE_PROVIDERS`` is the policy (default
    ``gemini,nara,openrouter,groq,cerebras``; uncredentialed slugs are skipped).
    A configured
    ``NOTE_PROVIDER`` slot (the v2 spelling, still honoured) keeps its place when
    it is already a member of that chain — a NaraRouter deployment is the Nara
    leg — and is otherwise appended as one further explicitly configured
    provider. Nothing is inferred at runtime, so the order survives restarts and
    is testable from the environment alone.
    """
    order = list(settings.note_providers)
    legacy = settings.note_provider if _legacy_note_configured(settings) else ""
    if legacy and legacy not in order:
        order.append(legacy)
    return tuple(order)


def _note_route(settings: Settings, slug: str) -> NoteRoute | None:
    """Build one provider's route, or ``None`` when its slot is not configured.

    Eligibility is configuration, never assumption: a hosted provider needs its
    own credential (a missing key is not a configured provider), the
    OpenAI-compatible slot is also usable as a keyless local gateway when its
    base URL is explicit, and a provider without a model can never build a
    request.
    """
    style = NOTE_REQUEST_STYLE.get(slug)
    if style is None:
        return None
    key, base, model = note_slot_credentials(settings, slug)
    # A provider that has a documented default model uses it; ``anthropic`` has
    # none, so it stays unusable until an operator names one (and says so in
    # check()).
    model = model or DEFAULT_NOTE_MODELS.get(slug, "")
    if slug == "openai_compatible":
        if not (key or base):
            return None
    elif not key:
        return None
    if not model:
        return None
    return NoteRoute(
        slug=slug,
        label=NOTE_PROVIDER_LABELS[slug],
        style=style,
        base_url=normalize_note_base_url(base or NOTE_BASE_URL_DEFAULTS[slug], slug),
        model=model,
        api_key=_normalize_api_key(key),
        json_object_mode=settings.note_json_mode and slug in NOTE_JSON_OBJECT_PROVIDERS,
    )


def note_routes(settings: Settings) -> tuple[NoteRoute, ...]:
    """The ordered, eligible note providers — the note pipeline's whole policy."""
    routes: list[NoteRoute] = []
    for slug in note_route_order(settings):
        route = _note_route(settings, slug)
        if route is not None:
            routes.append(route)
    return tuple(routes)


def note_route_diagnostics(settings: Settings) -> list[str]:
    """Problems in *partly* configured note providers.

    A provider with no credentials in this deployment is not a problem: that is
    what an ordered failover chain looks like. A listed provider that has a
    credential but no model is, because a request could never be built for it.
    """
    problems: list[str] = []
    for slug in note_route_order(settings):
        if NOTE_REQUEST_STYLE.get(slug) is None or _note_route(settings, slug) is not None:
            continue
        key, base, model = note_slot_credentials(settings, slug)
        has_credential = bool(key or base) if slug == "openai_compatible" else bool(key)
        if has_credential and not model:
            problems.append(
                f"NOTE_MODEL must be set for the {NOTE_PROVIDER_LABELS[slug]} note "
                "provider (a credential is configured but no model name is)."
            )
    return problems


def _parse_admin_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in re.split(r"[,\s]+", raw):
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            logger.warning("Ignoring non-numeric ADMIN_IDS entry: %r", part)
            continue
        if value > 0:
            ids.add(value)
    return frozenset(ids)


def _parse_provider_order(raw: str) -> tuple[str, ...]:
    order: list[str] = []
    for part in re.split(r"[,\s]+", raw):
        slug = part.strip().lower()
        if not slug:
            continue
        if slug not in STT_PROVIDER_ORDER:
            logger.warning("Ignoring unknown STT provider %r (known: %s)", slug, ", ".join(STT_PROVIDER_ORDER))
            continue
        if slug not in order:
            order.append(slug)
    return tuple(order)


def from_env(env_file: str | None = None) -> Settings:
    """Build the settings from the process environment (and ``.env``)."""
    if env_file is not None:
        load_dotenv(env_file, override=False)
    else:
        for candidate in (PROJECT_DIR / ".env", Path.cwd() / ".env"):
            if candidate.is_file():
                load_dotenv(candidate, override=False)
                break

    raw_providers = _env("STT_PROVIDERS", DEFAULT_STT_PROVIDERS)
    raw_note_providers = _env("NOTE_PROVIDERS", DEFAULT_NOTE_PROVIDERS)
    explicit_note_provider = _env("NOTE_PROVIDER").lower()
    note_api_base_url = _env("NOTE_BASE_URL").rstrip("/")
    # The v2-era single slot (``NOTE_PROVIDER``/``NOTE_API_KEY``/``NOTE_BASE_URL``)
    # is still honoured, and it is classified by host: a deployment that points
    # that slot at a documented provider keeps that provider's place in the
    # priority order instead of becoming a generic gateway. A Gemini-only
    # environment still selects Gemini, exactly as before.
    # A Gemini-only environment (no NOTE_* slot and no dedicated provider key)
    # still selects Gemini; once a dedicated provider key exists, the declared
    # chain decides and Gemini is only used when it is listed and configured.
    gemini_only = bool(
        _env("GEMINI_API_KEY")
        and not _env("NOTE_API_KEY")
        and not note_api_base_url
        and not _env("NARA_API_KEY")
        and not _env("OPENROUTER_API_KEY")
    )
    legacy_slug = explicit_note_provider or (
        "gemini" if gemini_only else "openai_compatible"
    )
    note_provider = classify_note_provider(legacy_slug, note_api_base_url)
    note_api_key = _normalize_api_key(
        _env("NOTE_API_KEY") or (_env("GEMINI_API_KEY") if legacy_slug == "gemini" else "")
    )
    note_api_base_url = normalize_note_base_url(note_api_base_url, note_provider)
    note_model = _env("NOTE_MODEL", DEFAULT_NOTE_MODELS.get(note_provider, ""))
    plan_values = {
        attribute: _plan_value(name, default)
        for name, attribute, default in PLAN_ENV_FIELDS
    }

    return Settings(
        bot_token=_env("TELEGRAM_BOT_TOKEN"),
        api_id=_env_int("TELEGRAM_API_ID", 0, minimum=0),
        api_hash=_env("TELEGRAM_API_HASH"),
        admin_ids=_parse_admin_ids(_env("ADMIN_IDS")),
        session_path=_project_path(_env("TELEGRAM_SESSION"), "data/gamas"),
        db_path=_project_path(_env("DB_PATH"), "data/gamas_v3.db"),
        temp_dir=_project_path(_env("TEMP_DIR"), "data/tmp"),
        log_file=(
            _project_path(value, "data/gamas.log")
            if (value := _env("LOG_FILE")) not in ("", "-")
            else None
        ),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        log_format=_env("LOG_FORMAT", "plain"),
        log_max_bytes=_env_int("LOG_MAX_BYTES", 5 * 1024 * 1024, minimum=100_000),
        log_backup_count=_env_int("LOG_BACKUP_COUNT", 5, minimum=1),
        max_file_size=_env_int(
            "MAX_FILE_SIZE", DEFAULT_MAX_FILE_SIZE_BYTES, minimum=1024
        ),
        doc_max_bytes=_env_int(
            "DOC_MAX_BYTES", 50 * 1024 * 1024, minimum=1024, maximum=1024**3
        ),
        max_media_duration_seconds=_env_float(
            "MAX_MEDIA_DURATION_SECONDS",
            DEFAULT_MAX_MEDIA_DURATION_SECONDS,
            minimum=1.0,
            maximum=12 * 60 * 60,
        ),
        max_active_jobs=_env_int("MAX_ACTIVE_JOBS", 3, minimum=1, maximum=32),
        # Per-user admission: a balance check at intake cannot see the jobs a
        # user queued one second earlier, so N uploads each individually
        # affordable would all be downloaded before the first reserve fails.
        max_active_jobs_per_user=_env_int(
            "MAX_ACTIVE_JOBS_PER_USER", 2, minimum=1, maximum=16
        ),
        # Minimum 1: ``asyncio.Queue(maxsize=0)`` is unbounded, so 0 would
        # silently remove the queue's back-pressure instead of disabling it.
        max_queued_jobs=_env_int("MAX_QUEUED_JOBS", 8, minimum=1, maximum=100),
        media_worker_concurrency=_env_int(
            "MEDIA_WORKER_CONCURRENCY", 1, minimum=1, maximum=8
        ),
        media_timeout=_env_float("MEDIA_TIMEOUT", 3600.0, minimum=1.0),
        convert_timeout=_env_float("CONVERT_TIMEOUT", 600.0, minimum=1.0),
        stt_job_timeout=_env_float("STT_JOB_TIMEOUT", 21600.0, minimum=60.0),
        stt_poll_interval=_env_float("STT_POLL_INTERVAL", 10.0, minimum=1.0),
        stt_status_long_poll=_env_float(
            "STT_STATUS_LONG_POLL", 25.0, minimum=0.0, maximum=120.0
        ),
        temp_min_free_bytes=_env_int(
            "TEMP_MIN_FREE_BYTES", DEFAULT_TEMP_MIN_FREE_BYTES, minimum=0
        ),
        download_stall_seconds=_env_float(
            "DOWNLOAD_STALL_SECONDS", 300.0, minimum=30.0
        ),
        audio_format_policy=resolve_audio_format_policy(_env("AUDIO_FORMAT_POLICY")),
        telegram_download_connections=_env_int(
            "TELEGRAM_DOWNLOAD_CONNECTIONS", 1, minimum=1, maximum=8
        ),
        presentation_max_clips=_env_int(
            "PRESENTATION_MAX_CLIPS", DEFAULT_PRESENTATION_MAX_CLIPS, minimum=1
        ),
        presentation_max_total_duration=_env_float(
            "PRESENTATION_MAX_TOTAL_DURATION",
            DEFAULT_PRESENTATION_MAX_DURATION_SECONDS,
            minimum=1.0,
        ),
        presentation_max_unpacked_bytes=_env_int(
            "PRESENTATION_MAX_UNPACKED_BYTES",
            DEFAULT_PRESENTATION_MAX_UNPACKED_BYTES,
            minimum=1024,
        ),
        presentation_max_temp_bytes=_env_int(
            "PRESENTATION_MAX_TEMP_BYTES",
            DEFAULT_PRESENTATION_MAX_TEMP_BYTES,
            minimum=1024,
        ),
        presentation_min_clip_seconds=_env_float(
            "PRESENTATION_MIN_CLIP_SECONDS", 0.5, minimum=0.0
        ),
        presentation_silence_seconds=_env_float(
            "PRESENTATION_SILENCE_SECONDS", 0.25, minimum=0.0, maximum=60.0
        ),
        presentation_wav_limit_bytes=_env_int(
            "PRESENTATION_WAV_LIMIT_BYTES", DEFAULT_MAX_FILE_SIZE_BYTES, minimum=1024
        ),
        presentation_include_slide_text=_env_bool("PRESENTATION_INCLUDE_SLIDE_TEXT", True),
        presentation_include_video_audio=_env_bool("PRESENTATION_INCLUDE_VIDEO_AUDIO", True),
        presentation_include_unreferenced_media=_env_bool(
            "PRESENTATION_INCLUDE_UNREFERENCED_MEDIA", False
        ),
        stt_providers=_parse_provider_order(raw_providers),
        stt_language=_env("STT_LANGUAGE", "fa"),
        speechmatics_api_key=_env("SPEECHMATICS_API_KEY"),
        speechmatics_base_url=_env(
            "SPEECHMATICS_BASE_URL", "https://asr.api.speechmatics.com/v2"
        ).rstrip("/"),
        speechmatics_model=_env("SPEECHMATICS_MODEL", "enhanced"),
        deepgram_api_key=_env("DEEPGRAM_API_KEY"),
        deepgram_base_url=_env("DEEPGRAM_BASE_URL", "https://api.deepgram.com/v1").rstrip("/"),
        deepgram_model=_env("DEEPGRAM_MODEL", DEFAULT_DEEPGRAM_MODEL),
        stt_openai_base_url=_env("STT_OPENAI_BASE_URL").rstrip("/"),
        stt_openai_api_key=_env("STT_OPENAI_API_KEY"),
        stt_openai_model=_env("STT_OPENAI_MODEL", "whisper-1"),
        stt_openai_max_upload=_env_int(
            "STT_OPENAI_MAX_UPLOAD_BYTES", OPENAI_STT_MAX_UPLOAD, minimum=1024
        ),
        stt_transfer_timeout=_env_float(
            "STT_TRANSFER_TIMEOUT_SECONDS", 600.0, minimum=30.0
        ),
        max_stt_response_bytes=_env_int(
            "MAX_STT_RESPONSE_BYTES", 64 * 1024 * 1024, minimum=1024 * 1024
        ),
        note_providers=_parse_note_providers(raw_note_providers),
        note_provider=note_provider,
        note_api_key=note_api_key,
        note_api_base_url=note_api_base_url,
        note_model=note_model,
        nara_api_key=_normalize_api_key(_env("NARA_API_KEY")),
        nara_base_url=normalize_note_base_url(
            _env("NARA_BASE_URL", DEFAULT_NARA_BASE_URL), "nara"
        ),
        nara_model=_env("NARA_MODEL", DEFAULT_NARA_MODEL),
        openrouter_api_key=_normalize_api_key(_env("OPENROUTER_API_KEY")),
        openrouter_base_url=normalize_note_base_url(
            _env("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL), "openrouter"
        ),
        openrouter_model=_env("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        groq_api_key=_normalize_api_key(_env("GROQ_API_KEY")),
        groq_base_url=normalize_note_base_url(
            _env("GROQ_BASE_URL", DEFAULT_GROQ_BASE_URL), "groq"
        ),
        groq_model=_env("GROQ_MODEL", DEFAULT_GROQ_MODEL),
        cerebras_api_key=_normalize_api_key(_env("CEREBRAS_API_KEY")),
        cerebras_base_url=normalize_note_base_url(
            _env("CEREBRAS_BASE_URL", DEFAULT_CEREBRAS_BASE_URL), "cerebras"
        ),
        cerebras_model=_env("CEREBRAS_MODEL", DEFAULT_CEREBRAS_MODEL),
        gemini_api_key=_normalize_api_key(_env("GEMINI_API_KEY")),
        gemini_model=_env("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        gemini_thinking_budget=_env_int(
            "NOTE_GEMINI_THINKING_BUDGET",
            DEFAULT_GEMINI_THINKING_BUDGET,
            minimum=-1,
            maximum=32768,
        ),
        note_context_tokens=_env_int(
            "NOTE_CONTEXT_TOKENS", DEFAULT_NOTE_CONTEXT_TOKENS, minimum=2048
        ),
        note_mode=resolve_note_mode(_env("NOTE_MODE")),
        note_max_output_tokens=_env_int(
            "NOTE_MAX_OUTPUT_TOKENS", DEFAULT_NOTE_MAX_OUTPUT_TOKENS, minimum=256
        ),
        note_request_timeout=_env_float(
            "NOTE_REQUEST_TIMEOUT_SECONDS",
            DEFAULT_NOTE_REQUEST_TIMEOUT_SECONDS,
            minimum=30.0,
            maximum=3600.0,
        ),
        note_json_mode=_env_bool("NOTE_JSON_MODE", False),
        note_chunk_chars=_env_int(
            "NOTE_CHUNK_CHARS", DEFAULT_NOTE_CHUNK_CHARS, minimum=2000, maximum=200_000
        ),
        note_concurrency=_env_int(
            "NOTE_CONCURRENCY", DEFAULT_NOTE_CONCURRENCY, minimum=1, maximum=8
        ),
        note_compile=_env_bool("NOTE_COMPILE", True),
        note_compile_max_chars=_env_int(
            "NOTE_COMPILE_MAX_CHARS", 0, minimum=0, maximum=2_000_000
        ),
        note_reasoning_effort=resolve_note_reasoning_effort(_env("NOTE_REASONING_EFFORT")),
        note_strict_grounding=_env_bool("NOTE_STRICT_GROUNDING", True),
        note_quality_gate=resolve_note_quality_gate(_env("NOTE_QUALITY_GATE")),
        note_openrouter_throughput=_env_bool("NOTE_OPENROUTER_THROUGHPUT", True),
        note_structured_output=_env_bool("NOTE_STRUCTURED_OUTPUT", True),
        note_progressive_booklet=_env_bool("NOTE_PROGRESSIVE_BOOKLET", True),
        note_completeness_fill=_env_bool("NOTE_COMPLETENESS_FILL", True),
        note_compile_provider=normalize_note_compile_provider(
            _env("NOTE_COMPILE_PROVIDER")
        ),
        raw_text_profile=resolve_raw_text_profile(_env("RAW_TEXT_PROFILE")),
        raw_fallback_file_chars=_env_int(
            "RAW_FALLBACK_FILE_CHARS", 100_000, minimum=1_000, maximum=400_000
        ),
        raw_fallback_max_files=_env_int(
            "RAW_FALLBACK_MAX_FILES", 8, minimum=1, maximum=20
        ),
        office_enabled=_env_bool("OFFICE_ENABLED", False),
        office_max_input_bytes=_env_int(
            "OFFICE_MAX_INPUT_BYTES", 1_000_000, minimum=1024, maximum=50_000_000
        ),
        office_max_chars=_env_int(
            "OFFICE_MAX_CHARS", 20_000, minimum=200, maximum=1_000_000
        ),
        office_max_slides=_env_int(
            "OFFICE_MAX_SLIDES", 30, minimum=1, maximum=200
        ),
        office_session_ttl_minutes=_env_int(
            "OFFICE_SESSION_TTL_MINUTES", 30, minimum=1, maximum=1440
        ),
        # The section is free and its xlsx/pptx builds can call the note model
        # (``infer_table`` / ``plan_deck``), so one user must not be able to loop
        # paste → build and drain the shared quota. 0 disables the throttle.
        office_min_interval_seconds=_env_float(
            "OFFICE_MIN_INTERVAL_SECONDS", 10.0, minimum=0.0, maximum=3600.0
        ),
        # No fallback card: a deployment that forgets PAYMENT_CARD_* must not
        # silently direct its users' money to a card baked into the source.
        # ``_start_payment`` refuses to open a payment while this is empty.
        payment_card_number=re.sub(r"\D", "", _env("PAYMENT_CARD_NUMBER"))[-16:],
        payment_card_holder=_env("PAYMENT_CARD_HOLDER"),
        payment_bank_name=_env("PAYMENT_BANK_NAME"),
        receipt_dir=_project_path(_env("RECEIPT_DIR"), "data/receipts"),
        receipt_retention_days=_env_int("RECEIPT_RETENTION_DAYS", 90, minimum=1),
        max_payment_receipt_bytes=_env_int("MAX_PAYMENT_RECEIPT_BYTES", 5 * 1024 * 1024, minimum=1024),
        transcript_retention_days=_env_int(
            "TRANSCRIPT_RETENTION_DAYS", 0, minimum=0, maximum=3650
        ),
        llm_connector_limit=_env_int("LLM_CONNECTOR_LIMIT", 32, minimum=1),
        llm_connector_limit_per_host=_env_int(
            "LLM_CONNECTOR_LIMIT_PER_HOST", 8, minimum=1
        ),
        llm_connector_ttl_dns_cache=_env_int(
            "LLM_CONNECTOR_TTL_DNS_CACHE", 300, minimum=0
        ),
        llm_connector_keepalive=_env_int("LLM_CONNECTOR_KEEPALIVE", 300, minimum=0),
        llm_prewarm=_env_bool("LLM_PREWARM", True),
        note_ttfb_timeout=_env_float(
            "NOTE_TTFB_TIMEOUT_SECONDS",
            DEFAULT_NOTE_TTFB_TIMEOUT_SECONDS,
            minimum=0.0,
            maximum=3600.0,
        ),
        note_hedge_after=_env_float(
            "NOTE_HEDGE_AFTER_SECONDS",
            DEFAULT_NOTE_HEDGE_AFTER_SECONDS,
            minimum=0.0,
            maximum=3600.0,
        ),
        office_ai_concurrency=_env_int(
            "OFFICE_AI_CONCURRENCY", 3, minimum=1, maximum=16
        ),
        plan_values=plan_values,
        max_concurrent_downloads=_env_int(
            "MAX_CONCURRENT_DOWNLOADS",
            DEFAULT_MAX_CONCURRENT_DOWNLOADS,
            minimum=1,
            maximum=8,
        ),
        max_concurrent_uploads=_env_int(
            "MAX_CONCURRENT_UPLOADS",
            DEFAULT_MAX_CONCURRENT_UPLOADS,
            minimum=1,
            maximum=8,
        ),
        download_timeout=_env_float(
            "DOWNLOAD_TIMEOUT_SECONDS",
            900.0,
            minimum=10.0,
            maximum=14400.0,
        ),
        upload_timeout=_env_float(
            "UPLOAD_TIMEOUT_SECONDS",
            600.0,
            minimum=10.0,
            maximum=14400.0,
        ),
        stt_fallback_enabled=_env_bool("STT_FALLBACK_ENABLED", True),
        speechmatics_max_in_flight=_env_int(
            "SPEECHMATICS_MAX_IN_FLIGHT",
            DEFAULT_SPEECHMATICS_MAX_IN_FLIGHT,
            minimum=1,
            maximum=32,
        ),
        deepgram_max_in_flight=_env_int(
            "DEEPGRAM_MAX_IN_FLIGHT",
            DEFAULT_DEEPGRAM_MAX_IN_FLIGHT,
            minimum=1,
            maximum=32,
        ),
        stt_openai_max_in_flight=_env_int(
            "STT_OPENAI_MAX_IN_FLIGHT",
            DEFAULT_STT_OPENAI_MAX_IN_FLIGHT,
            minimum=1,
            maximum=32,
        ),
        stt_provider_timeout=_env_float(
            "STT_PROVIDER_TIMEOUT_SECONDS",
            DEFAULT_STT_PROVIDER_TIMEOUT_SECONDS,
            minimum=5.0,
            maximum=86400.0,
        ),
        stt_slot_wait_timeout=_env_float(
            "STT_SLOT_WAIT_TIMEOUT_SECONDS",
            DEFAULT_STT_SLOT_WAIT_TIMEOUT_SECONDS,
            minimum=0.0,
            maximum=3600.0,
        ),
        llm_primary_max_in_flight=_env_int(
            "LLM_PRIMARY_MAX_IN_FLIGHT",
            DEFAULT_LLM_PRIMARY_MAX_IN_FLIGHT,
            minimum=1,
            maximum=64,
        ),
        llm_fallback_max_in_flight=_env_int(
            "LLM_FALLBACK_MAX_IN_FLIGHT",
            DEFAULT_LLM_FALLBACK_MAX_IN_FLIGHT,
            minimum=1,
            maximum=64,
        ),
        llm_slot_wait_timeout=_env_float(
            "LLM_SLOT_WAIT_TIMEOUT_SECONDS",
            DEFAULT_LLM_SLOT_WAIT_TIMEOUT_SECONDS,
            minimum=0.0,
            maximum=1800.0,
        ),
        office_job_concurrency=_env_int(
            "OFFICE_JOB_CONCURRENCY",
            DEFAULT_OFFICE_JOB_CONCURRENCY,
            minimum=1,
            maximum=8,
        ),
        library_max_items=_env_int("LIBRARY_MAX_ITEMS", 50, minimum=1, maximum=10_000),
        subscription_channel=_env("SUBSCRIPTION_CHANNEL", "@gamas_AI").strip(),
        subscription_required=_env_bool("SUBSCRIPTION_REQUIRED", True),
        subscription_cache_seconds=_env_float(
            "SUBSCRIPTION_CACHE_SECONDS", 0.0, minimum=0.0, maximum=3600.0
        ),
        subscription_timeout_seconds=_env_float(
            "SUBSCRIPTION_TIMEOUT_SECONDS", 20.0, minimum=1.0, maximum=120.0
        ),
        telegram_api_base_url=_env(
            "TELEGRAM_API_BASE_URL", "https://api.telegram.org"
        ).rstrip("/"),
        quiz_cost_seconds_per_question=_env_int(
            "QUIZ_COST_SECONDS_PER_QUESTION", 60, minimum=1, maximum=86_400
        ),
        quiz_max_active_hours=_env_int(
            "QUIZ_MAX_ACTIVE_HOURS", 6, minimum=1, maximum=168
        ),
        quiz_max_sessions_per_hour=_env_int(
            "QUIZ_MAX_SESSIONS_PER_HOUR", 3, minimum=1, maximum=100
        ),
        flashcard_ai_cost_seconds=_env_int(
            "FLASHCARD_AI_COST_SECONDS", 300, minimum=1, maximum=86_400
        ),
        flashcard_ai_deck_size=_env_int(
            "FLASHCARD_AI_DECK_SIZE", 10, minimum=5, maximum=50
        ),
        flashcard_max_decks_per_user=_env_int(
            "FLASHCARD_MAX_DECKS_PER_USER", 30, minimum=1, maximum=500
        ),
        learning_chunk_concurrency=_env_int(
            "LEARNING_CHUNK_CONCURRENCY", 4, minimum=1, maximum=8
        ),
        dedup_window_seconds=_env_float(
            "DEDUP_WINDOW_SECONDS",
            DEFAULT_DEDUP_WINDOW_SECONDS,
            minimum=0.0,
            maximum=3600.0,
        ),
        **plan_values,
    )


#: Substrings that mark a problem as advisory: the bot still runs correctly,
#: with reduced functionality. ``run()`` and the self-check only fail on the
#: rest.
ADVISORY_PROBLEMS = (
    "raw transcripts only",
    "does not support Persian",
    "no room for the transcript",
    "the note request will time out on a slow gateway",
    "will wait for a transcription slot",
    "will wait for a note-provider slot",
)


def is_advisory(problem: str) -> bool:
    return any(marker in problem for marker in ADVISORY_PROBLEMS)


def first_configured_stt_provider(settings: Settings) -> str | None:
    """The provider that will actually serve the next job, or ``None``.

    Mirrors ``stt.transcribe``: walk ``STT_PROVIDERS`` in order and take the
    first slug that has credentials, because an unconfigured slug is skipped
    rather than attempted. Kept here so ``check`` can reason about the order
    without importing the transcription layer.
    """
    configured = {
        "speechmatics": settings.speechmatics_configured,
        "deepgram": settings.deepgram_configured,
        "openai_compatible": settings.stt_openai_configured,
    }
    for slug in settings.stt_providers:
        if configured.get(slug, False):
            return slug
    return None


def check(settings: Settings) -> list[str]:
    """Human-readable configuration problems; empty list means ready."""
    problems: list[str] = []
    if not settings.bot_token:
        problems.append("TELEGRAM_BOT_TOKEN is not set.")
    if settings.api_id <= 0:
        problems.append("TELEGRAM_API_ID is not a positive number.")
    if not settings.api_hash:
        problems.append("TELEGRAM_API_HASH is not set.")
    if not settings.stt_providers:
        # ``_parse_provider_order`` drops unknown slugs, so a typo can empty the
        # list while every key is still set. ``stt.transcribe`` then refuses
        # every upload; report the real mistake instead of claiming no key is
        # configured.
        problems.append(
            "STT_PROVIDERS contains no known provider (known: "
            f"{', '.join(STT_PROVIDER_ORDER)}); every upload would fail."
        )
    elif not settings.stt_configured:
        problems.append(
            "No speech-to-text provider is configured: set SPEECHMATICS_API_KEY, "
            "DEEPGRAM_API_KEY, or STT_OPENAI_BASE_URL + STT_OPENAI_API_KEY."
        )
    # A language/model pairing that the provider documents as unsupported would
    # only surface as a per-job HTTP 400; report it before any upload happens.
    if (
        "deepgram" in settings.stt_providers
        and settings.deepgram_configured
        and (settings.stt_language or "").strip().lower().split("-")[0] == "fa"
        and settings.deepgram_model.strip().lower() not in DEEPGRAM_PERSIAN_MODELS
    ):
        problems.append(
            f"DEEPGRAM_MODEL={settings.deepgram_model} does not support Persian "
            f"(STT_LANGUAGE={settings.stt_language}); set DEEPGRAM_MODEL to one of: "
            f"{', '.join(sorted(DEEPGRAM_PERSIAN_MODELS))}."
        )
    # Capacity coherence: active jobs beyond the configured providers' total
    # in-flight slots cannot overflow anywhere — they wait for a slot (bounded
    # by STT_SLOT_WAIT_TIMEOUT_SECONDS) and then fail. That is routine, not
    # exceptional, in a Speechmatics-only deployment (default 2 slots against
    # MAX_ACTIVE_JOBS=3), so say so at startup where the fix is one line of
    # .env instead of a failed paid job.
    stt_in_flight = {
        "speechmatics": settings.speechmatics_max_in_flight,
        "deepgram": settings.deepgram_max_in_flight,
        "openai_compatible": settings.stt_openai_max_in_flight,
    }
    stt_configured_flags = {
        "speechmatics": settings.speechmatics_configured,
        "deepgram": settings.deepgram_configured,
        "openai_compatible": settings.stt_openai_configured,
    }
    eligible_stt = [
        slug for slug in settings.stt_providers if stt_configured_flags.get(slug, False)
    ]
    if eligible_stt:
        # Mirror ``stt.transcribe``: with fallback disabled only the first
        # configured provider is ever attempted, so only its slots count.
        stt_capacity = (
            sum(stt_in_flight[slug] for slug in eligible_stt)
            if settings.stt_fallback_enabled
            else stt_in_flight[eligible_stt[0]]
        )
        if stt_capacity < settings.max_active_jobs:
            problems.append(
                f"MAX_ACTIVE_JOBS={settings.max_active_jobs} exceeds the configured "
                f"transcription capacity ({stt_capacity} in-flight slot(s) across "
                f"{', '.join(eligible_stt)}); additional jobs will wait for a "
                "transcription slot and fail if none frees within "
                f"STT_SLOT_WAIT_TIMEOUT_SECONDS={settings.stt_slot_wait_timeout:.0f}. "
                "Raise the provider *_MAX_IN_FLIGHT limit, configure a Deepgram "
                "overflow key (DEEPGRAM_API_KEY), or lower MAX_ACTIVE_JOBS."
            )
    if settings.note_provider not in NOTE_PROVIDER_SLUGS and _legacy_note_configured(settings):
        problems.append(
            f"NOTE_PROVIDER must be one of: {', '.join(NOTE_PROVIDER_SLUGS)} "
            f"(got {settings.note_provider!r})."
        )
    if not settings.note_configured:
        problems.append(
            "Note generation is not configured (NOTE_PROVIDERS / NOTE_API_KEY / "
            "NARA_API_KEY / OPENROUTER_API_KEY / GEMINI_API_KEY / GROQ_API_KEY / "
            "CEREBRAS_API_KEY); the bot will deliver raw transcripts only."
        )
    # A listed provider with a credential but no model can never build a request.
    problems.extend(note_route_diagnostics(settings))
    # Same coherence check for notes, at single-job granularity: if the total
    # LLM slot capacity is below one job's own chunk fan-out, every long
    # lecture's later chunks wait (bounded by LLM_SLOT_WAIT_TIMEOUT_SECONDS)
    # even when no other user is active.
    configured_routes = note_routes(settings)
    if configured_routes:
        llm_capacity = settings.llm_primary_max_in_flight + (
            settings.llm_fallback_max_in_flight * (len(configured_routes) - 1)
        )
        if llm_capacity < settings.note_concurrency:
            problems.append(
                f"NOTE_CONCURRENCY={settings.note_concurrency} exceeds the configured "
                f"note-provider capacity ({llm_capacity} in-flight slot(s) across "
                f"{len(configured_routes)} route(s)); chunk requests from a single "
                "job will wait for a note-provider slot and fail if none frees within "
                f"LLM_SLOT_WAIT_TIMEOUT_SECONDS={settings.llm_slot_wait_timeout:.0f}. "
                "Raise LLM_PRIMARY_MAX_IN_FLIGHT / LLM_FALLBACK_MAX_IN_FLIGHT, add a "
                "fallback route to NOTE_PROVIDERS, or lower NOTE_CONCURRENCY."
            )
    # One request must fit the transcript, the system prompt and the output.
    required_note_tokens = MIN_NOTE_INPUT_TOKENS + MIN_NOTE_PROMPT_TOKENS
    if settings.note_context_tokens - settings.note_max_output_tokens < required_note_tokens:
        problems.append(
            f"NOTE_CONTEXT_TOKENS={settings.note_context_tokens} leaves no room for the "
            f"transcript and system prompt in one note request (NOTE_MAX_OUTPUT_TOKENS="
            f"{settings.note_max_output_tokens}); lower NOTE_MAX_OUTPUT_TOKENS or raise "
            "NOTE_CONTEXT_TOKENS."
        )
    # The context window decides whether a request *fits*; the request deadline
    # decides whether it *finishes*. A free or shared-pool gateway that cannot
    # emit the configured output budget inside the deadline answers nothing at
    # all — the request is not rejected, it simply times out. Warn the operator
    # before that happens instead of after a 25-minute stall.
    seconds_needed = (
        settings.note_max_output_tokens / MIN_NOTE_OUTPUT_TOKENS_PER_SECOND
    )
    if seconds_needed > settings.note_request_timeout:
        problems.append(
            f"NOTE_MAX_OUTPUT_TOKENS={settings.note_max_output_tokens} needs about "
            f"{seconds_needed:.0f}s of output at "
            f"{MIN_NOTE_OUTPUT_TOKENS_PER_SECOND:.0f} tokens/s, which is more than "
            f"NOTE_REQUEST_TIMEOUT_SECONDS={settings.note_request_timeout:.0f}; the note "
            "request will time out on a slow gateway. Lower NOTE_MAX_OUTPUT_TOKENS, raise "
            "NOTE_REQUEST_TIMEOUT_SECONDS, or lower NOTE_REASONING_EFFORT."
        )
    # Media extraction runs before transcription and has its own deadline. A
    # six-hour video needs minutes of decoding even at many times real time, so
    # a short MEDIA_TIMEOUT fails a lecture whose transcription budget is huge.
    media_floor = max(600.0, settings.stt_job_timeout / 12)
    if settings.media_timeout < media_floor:
        problems.append(
            f"MEDIA_TIMEOUT={settings.media_timeout:.0f}s is short for a multi-hour "
            f"lecture (STT_JOB_TIMEOUT={settings.stt_job_timeout:.0f}s allows one); "
            f"media extraction would be killed at {settings.media_timeout:.0f}s. "
            "Raise MEDIA_TIMEOUT or lower STT_JOB_TIMEOUT."
        )
    if not settings.admin_ids:
        problems.append("ADMIN_IDS is empty; nobody will be able to approve payments.")
    # Mandatory-channel gate. Offline we can only validate the channel
    # reference, not the bot's administrator rights; the runtime self-check
    # reports those, and docs/TELEGRAM_SUBSCRIPTION.md is the setup guide.
    if settings.subscription_required:
        subscription_ref = (settings.subscription_channel or "").strip()
        if not subscription_ref:
            problems.append(
                "SUBSCRIPTION_CHANNEL is required while SUBSCRIPTION_REQUIRED is on; "
                "set it to @gamas_AI (or the channel id) and make the bot an "
                "administrator of that channel."
            )
        else:
            body = subscription_ref[1:] if subscription_ref.startswith("@") else subscription_ref
            if not (
                subscription_ref.lstrip("-").isdigit()
                or re.fullmatch(r"[A-Za-z0-9_]{5,32}", body)
            ):
                problems.append(
                    f"SUBSCRIPTION_CHANNEL={subscription_ref!r} is neither a channel "
                    "username (@name) nor a numeric channel id."
                )
    # A non-positive tariff override is silently ignored by ``billing._resolved``
    # (the canonical default wins), so an operator typo would quote a price they
    # never chose. Say so at startup instead of hiding it.
    if settings.plan_values:
        for name, attribute, default in PLAN_ENV_FIELDS:
            value = settings.plan_values.get(attribute)
            if value is None or value <= 0:
                problems.append(
                    f"{name}={value!r} is not a positive number; the built-in "
                    f"{attribute}={default} is used instead. Fix or remove {name}."
                )
    return problems
