"""Configuration for Gamas Bot v3.

One frozen dataclass, one ``from_env`` factory, one ``check`` self-test. Every
value the process needs lives here; nothing else reads ``os.environ`` at
runtime (``load_dotenv`` is applied once, here).

Design rules:

* **~55 settings, not ~194.** Anything that existed only to support the deleted
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
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

TRUTHY = {"1", "true", "yes", "on"}

#: Project directory: the folder that contains this package's parent.
PROJECT_DIR = Path(__file__).resolve().parent.parent

NOTE_MODES = ("full", "standard", "summary")
RAW_TEXT_PROFILES = ("plain", "timestamped")

STT_PROVIDER_ORDER = ("speechmatics", "deepgram", "openai_compatible")
NOTE_PROVIDERS = ("openai_compatible", "gemini", "anthropic")
DEFAULT_NOTE_MODELS = {
    "openai_compatible": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}

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
DEFAULT_STT_PROVIDERS = "speechmatics,deepgram,openai_compatible"

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


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        value = default
    return max(value, minimum)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in TRUTHY


def resolve_note_mode(mode: str | None) -> str:
    """Resolve a note compression mode, defaulting to ``full``."""
    value = (mode or "").strip().lower()
    return value if value in NOTE_MODES else "full"


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
    max_active_jobs: int
    max_queued_jobs: int
    media_timeout: float
    convert_timeout: float
    stt_job_timeout: float
    stt_poll_interval: float

    # --- Presentations ------------------------------------------------------
    presentation_max_clips: int
    presentation_max_total_duration: float
    presentation_max_unpacked_bytes: int
    presentation_min_clip_seconds: float
    presentation_silence_seconds: float
    presentation_wav_limit_bytes: int
    presentation_include_slide_text: bool
    presentation_include_video_audio: bool

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

    # --- Note generation ----------------------------------------------------
    note_provider: str
    note_api_key: str
    note_api_base_url: str
    note_model: str
    note_mode: str
    note_max_output_tokens: int
    note_json_mode: bool
    note_chunk_chars: int
    note_concurrency: int
    note_compile: bool

    # --- Raw text -----------------------------------------------------------
    raw_text_profile: str

    # --- Billing / payments --------------------------------------------------
    payment_card_number: str
    payment_card_holder: str
    payment_bank_name: str
    receipt_dir: Path
    receipt_retention_days: int
    max_payment_receipt_bytes: int
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

    # --- Derived --------------------------------------------------------------
    plan_values: dict[str, int] = field(default_factory=dict, compare=False)

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
    def note_configured(self) -> bool:
        """A note provider is usable without falling back to raw-only mode."""
        if self.note_provider == "openai_compatible":
            return bool(self.note_api_base_url or self.note_api_key)
        return bool(self.note_api_key)

    def tariff_values(self) -> dict[str, int]:
        """The resolved tariff mapping consumed by ``billing.plan_catalog``."""
        return dict(self.plan_values)


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
    explicit_note_provider = _env("NOTE_PROVIDER").lower()
    # Preserve the old Gemini-only environment setup while keeping the
    # documented OpenAI-compatible provider as the ordinary default.
    note_provider = explicit_note_provider or (
        "gemini"
        if _env("GEMINI_API_KEY") and not _env("NOTE_API_KEY") and not _env("NOTE_BASE_URL")
        else "openai_compatible"
    )
    note_api_key = _env("NOTE_API_KEY") or (
        _env("GEMINI_API_KEY") if note_provider == "gemini" else ""
    )
    note_model = _env("NOTE_MODEL", DEFAULT_NOTE_MODELS.get(note_provider, ""))
    plan_values = {
        attribute: _env_int(name, default)
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
        max_file_size=_env_int("MAX_FILE_SIZE", 2 * 1024**3, minimum=1024),
        max_active_jobs=_env_int("MAX_ACTIVE_JOBS", 3, minimum=1, maximum=32),
        max_queued_jobs=_env_int("MAX_QUEUED_JOBS", 8, minimum=0, maximum=100),
        media_timeout=_env_float("MEDIA_TIMEOUT", 3600.0, minimum=1.0),
        convert_timeout=_env_float("CONVERT_TIMEOUT", 600.0, minimum=1.0),
        stt_job_timeout=_env_float("STT_JOB_TIMEOUT", 21600.0, minimum=60.0),
        stt_poll_interval=_env_float("STT_POLL_INTERVAL", 10.0, minimum=1.0),
        presentation_max_clips=_env_int("PRESENTATION_MAX_CLIPS", 300, minimum=1),
        presentation_max_total_duration=_env_float(
            "PRESENTATION_MAX_TOTAL_DURATION", 21600.0, minimum=1.0
        ),
        presentation_max_unpacked_bytes=_env_int(
            "PRESENTATION_MAX_UNPACKED_BYTES", 4 * 1024**3, minimum=1024
        ),
        presentation_min_clip_seconds=_env_float("PRESENTATION_MIN_CLIP_SECONDS", 0.5),
        presentation_silence_seconds=_env_float("PRESENTATION_SILENCE_SECONDS", 0.25),
        presentation_wav_limit_bytes=_env_int(
            "PRESENTATION_WAV_LIMIT_BYTES", 2 * 1024**3, minimum=1024
        ),
        presentation_include_slide_text=_env_bool("PRESENTATION_INCLUDE_SLIDE_TEXT", True),
        presentation_include_video_audio=_env_bool("PRESENTATION_INCLUDE_VIDEO_AUDIO", True),
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
        note_provider=note_provider,
        note_api_key=note_api_key,
        note_api_base_url=_env("NOTE_BASE_URL").rstrip("/"),
        note_model=note_model,
        note_mode=resolve_note_mode(_env("NOTE_MODE")),
        note_max_output_tokens=_env_int("NOTE_MAX_OUTPUT_TOKENS", 16384, minimum=256),
        note_json_mode=_env_bool("NOTE_JSON_MODE", False),
        note_chunk_chars=_env_int("NOTE_CHUNK_CHARS", 22000, minimum=2000, maximum=200_000),
        note_concurrency=_env_int("NOTE_CONCURRENCY", 3, minimum=1, maximum=8),
        note_compile=_env_bool("NOTE_COMPILE", True),
        raw_text_profile=resolve_raw_text_profile(_env("RAW_TEXT_PROFILE")),
        payment_card_number=re.sub(r"\D", "", _env("PAYMENT_CARD_NUMBER", "5022291332906625"))[-16:],
        payment_card_holder=_env("PAYMENT_CARD_HOLDER", "امیرعلی غمخوار"),
        payment_bank_name=_env("PAYMENT_BANK_NAME", "بانک پاسارگاد"),
        receipt_dir=_project_path(_env("RECEIPT_DIR"), "data/receipts"),
        receipt_retention_days=_env_int("RECEIPT_RETENTION_DAYS", 90, minimum=1),
        max_payment_receipt_bytes=_env_int("MAX_PAYMENT_RECEIPT_BYTES", 5 * 1024 * 1024, minimum=1024),
        plan_values=plan_values,
        **plan_values,
    )


#: Substrings that mark a problem as advisory: the bot still runs correctly,
#: with reduced functionality. ``run()`` and the self-check only fail on the
#: rest.
ADVISORY_PROBLEMS = ("raw transcripts only", "does not support Persian")


def is_advisory(problem: str) -> bool:
    return any(marker in problem for marker in ADVISORY_PROBLEMS)


def check(settings: Settings) -> list[str]:
    """Human-readable configuration problems; empty list means ready."""
    problems: list[str] = []
    if not settings.bot_token:
        problems.append("TELEGRAM_BOT_TOKEN is not set.")
    if settings.api_id <= 0:
        problems.append("TELEGRAM_API_ID is not a positive number.")
    if not settings.api_hash:
        problems.append("TELEGRAM_API_HASH is not set.")
    if not settings.stt_configured:
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
    if settings.note_provider not in NOTE_PROVIDERS:
        problems.append(
            f"NOTE_PROVIDER must be one of: {', '.join(NOTE_PROVIDERS)} "
            f"(got {settings.note_provider!r})."
        )
    if settings.note_configured and not settings.note_model:
        problems.append("NOTE_MODEL must be set for the selected note provider.")
    if not settings.note_configured:
        problems.append(
            "Note generation is not configured (NOTE_API_KEY / NOTE_BASE_URL); "
            "the bot will deliver raw transcripts only."
        )
    if not settings.admin_ids:
        problems.append("ADMIN_IDS is empty; nobody will be able to approve payments.")
    return problems
