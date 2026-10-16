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
from urllib.parse import urlsplit

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

TRUTHY = {"1", "true", "yes", "on"}

#: Project directory: the folder that contains this package's parent.
PROJECT_DIR = Path(__file__).resolve().parent.parent

NOTE_MODES = ("full", "standard", "summary")
RAW_TEXT_PROFILES = ("plain", "timestamped")

STT_PROVIDER_ORDER = ("speechmatics", "deepgram", "openai_compatible")

#: Every note provider the pipeline knows, with the request style each one
#: speaks. ``nara``/``openrouter``/``openai_compatible`` are OpenAI-compatible
#: chat-completions APIs; ``gemini`` and ``anthropic`` have their own contracts.
NOTE_PROVIDER_SLUGS = ("nara", "openrouter", "openai_compatible", "gemini", "anthropic")
NOTE_REQUEST_STYLE = {
    "nara": "openai",
    "openrouter": "openai",
    "openai_compatible": "openai",
    "gemini": "gemini",
    "anthropic": "anthropic",
}
NOTE_PROVIDER_LABELS = {
    "nara": "NaraRouter",
    "openrouter": "OpenRouter",
    "openai_compatible": "OpenAI-compatible",
    "gemini": "Gemini",
    "anthropic": "Anthropic",
}
#: OpenAI ``response_format`` JSON mode is only sent to providers whose public
#: API documents it (NaraRouter does not document it, so asking for it there
#: would be an unsupported parameter).
NOTE_JSON_OBJECT_PROVIDERS = frozenset({"openrouter", "openai_compatible"})
#: Provider hostnames, so a deployment that configures one OpenAI-compatible
#: slot with a ``NOTE_BASE_URL`` pointing at a documented provider keeps that
#: provider's place in the priority order (and never becomes a duplicate leg).
NOTE_HOST_PROVIDERS = (("bynara.id", "nara"), ("openrouter.ai", "openrouter"))

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
NOTE_BASE_URL_DEFAULTS = {
    "nara": DEFAULT_NARA_BASE_URL,
    "openrouter": DEFAULT_OPENROUTER_BASE_URL,
    "openai_compatible": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "anthropic": "https://api.anthropic.com/v1",
}

#: Default note failover order: Nara first, OpenRouter second, everything else
#: only when it is explicitly listed and configured. The order in
#: ``NOTE_PROVIDERS`` *is* the failover policy, exactly like ``STT_PROVIDERS``.
DEFAULT_NOTE_PROVIDERS = "nara,openrouter"

DEFAULT_NOTE_MODELS = {
    "openai_compatible": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "nara": DEFAULT_NARA_MODEL,
    "openrouter": DEFAULT_OPENROUTER_MODEL,
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
#: ``reasoning_effort`` levels accepted on the Nara/OpenRouter note routes
#: (the two gateways verified for the field). ``ultra`` is accepted as the
#: documented alias of ``max`` (https://router.bynara.id/docs).
NOTE_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
#: A request needs at least this much room for the transcript itself.
MIN_NOTE_INPUT_TOKENS = 4096

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
    gemini_api_key: str
    note_context_tokens: int
    note_mode: str
    note_max_output_tokens: int
    note_json_mode: bool
    note_chunk_chars: int
    note_concurrency: int
    note_compile: bool
    note_compile_max_chars: int
    note_reasoning_effort: str

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
    def nara_configured(self) -> bool:
        return bool(self.nara_api_key)

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def note_configured(self) -> bool:
        """Whether at least one provider in the active order is usable."""
        return bool(note_routes(self))

    @property
    def note_provider_labels(self) -> tuple[str, ...]:
        """The eligible providers, in the order they will be attempted."""
        return tuple(route.label for route in note_routes(self))

    def tariff_values(self) -> dict[str, int]:
        """The resolved tariff mapping consumed by ``billing.plan_catalog``."""
        return dict(self.plan_values)


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


def classify_note_provider(slug: str, base_url: str) -> str:
    """The provider a configured slot really points at.

    A v2-era deployment configures a single OpenAI-compatible slot through
    ``NOTE_PROVIDER``/``NOTE_BASE_URL``; recognising a documented provider host
    keeps that deployment in its priority place (a NaraRouter base URL stays the
    Nara leg of the chain) instead of turning it into a generic gateway that
    would be tried last — or twice.
    """
    host = (urlsplit(base_url).hostname or "").lower()
    if not host:
        return slug
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

    A credential belongs to exactly one provider: ``NARA_API_KEY`` and
    ``OPENROUTER_API_KEY`` to theirs, ``GEMINI_API_KEY`` to Gemini, and the
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
    elif slug == "gemini":
        key, base, model = settings.gemini_api_key, "", ""
    else:  # openai_compatible / anthropic: configured through the v2-era slot
        key, base, model = "", "", ""
    if slug == settings.note_provider and _legacy_note_configured(settings):
        key = settings.note_api_key or key
        base = settings.note_api_base_url or base
        model = settings.note_model or model
    return key, base, model


def note_route_order(settings: Settings) -> tuple[str, ...]:
    """The explicit provider order: the declared chain, then the v2-era slot.

    ``NOTE_PROVIDERS`` is the policy (default ``nara,openrouter``). A configured
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
        base_url=(base or NOTE_BASE_URL_DEFAULTS[slug]).rstrip("/"),
        model=model,
        api_key=key,
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
    note_api_key = _env("NOTE_API_KEY") or (
        _env("GEMINI_API_KEY") if legacy_slug == "gemini" else ""
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
        note_providers=_parse_note_providers(raw_note_providers),
        note_provider=note_provider,
        note_api_key=note_api_key,
        note_api_base_url=note_api_base_url,
        note_model=note_model,
        nara_api_key=_env("NARA_API_KEY"),
        nara_base_url=_env("NARA_BASE_URL", DEFAULT_NARA_BASE_URL).rstrip("/"),
        nara_model=_env("NARA_MODEL", DEFAULT_NARA_MODEL),
        openrouter_api_key=_env("OPENROUTER_API_KEY"),
        openrouter_base_url=_env("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL).rstrip("/"),
        openrouter_model=_env("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        gemini_api_key=_env("GEMINI_API_KEY"),
        note_context_tokens=_env_int(
            "NOTE_CONTEXT_TOKENS", DEFAULT_NOTE_CONTEXT_TOKENS, minimum=2048
        ),
        note_mode=resolve_note_mode(_env("NOTE_MODE")),
        note_max_output_tokens=_env_int(
            "NOTE_MAX_OUTPUT_TOKENS", DEFAULT_NOTE_MAX_OUTPUT_TOKENS, minimum=256
        ),
        note_json_mode=_env_bool("NOTE_JSON_MODE", False),
        note_chunk_chars=_env_int(
            "NOTE_CHUNK_CHARS", DEFAULT_NOTE_CHUNK_CHARS, minimum=2000, maximum=200_000
        ),
        note_concurrency=_env_int("NOTE_CONCURRENCY", 3, minimum=1, maximum=8),
        note_compile=_env_bool("NOTE_COMPILE", True),
        note_compile_max_chars=_env_int(
            "NOTE_COMPILE_MAX_CHARS", 0, minimum=0, maximum=2_000_000
        ),
        note_reasoning_effort=resolve_note_reasoning_effort(_env("NOTE_REASONING_EFFORT")),
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
ADVISORY_PROBLEMS = (
    "raw transcripts only",
    "does not support Persian",
    "no room for the transcript",
)


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
    if settings.note_provider not in NOTE_PROVIDER_SLUGS and _legacy_note_configured(settings):
        problems.append(
            f"NOTE_PROVIDER must be one of: {', '.join(NOTE_PROVIDER_SLUGS)} "
            f"(got {settings.note_provider!r})."
        )
    if not settings.note_configured:
        problems.append(
            "Note generation is not configured (NOTE_PROVIDERS / NOTE_API_KEY / "
            "NARA_API_KEY / OPENROUTER_API_KEY); the bot will deliver raw transcripts only."
        )
    # A listed provider with a credential but no model can never build a request.
    problems.extend(note_route_diagnostics(settings))
    # One request must fit the transcript, the system prompt and the output.
    if settings.note_context_tokens - settings.note_max_output_tokens < MIN_NOTE_INPUT_TOKENS:
        problems.append(
            f"NOTE_CONTEXT_TOKENS={settings.note_context_tokens} leaves no room for the "
            f"transcript in one note request (NOTE_MAX_OUTPUT_TOKENS="
            f"{settings.note_max_output_tokens}); lower NOTE_MAX_OUTPUT_TOKENS or raise "
            "NOTE_CONTEXT_TOKENS."
        )
    if not settings.admin_ids:
        problems.append("ADMIN_IDS is empty; nobody will be able to approve payments.")
    return problems
