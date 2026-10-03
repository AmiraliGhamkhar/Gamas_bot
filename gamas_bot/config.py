from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

TRUTHY = {"1", "true", "yes", "on"}

# Note-generation compression modes. ``full`` (the default) preserves detail;
# only ``summary`` intentionally compresses. Defined here so the environment
# parser does not need to import the (heavier) structuring module.
NOTE_MODES = ("full", "standard", "summary")

#: Named font profiles so an operator picks a coherent set with one variable
#: instead of four.
#:
#: ``body``/``heading`` are the *Persian* faces and are written into the OOXML
#: complex-script slot (``w:cs``). ``latin`` is a separate face for embedded
#: English and is written into ``w:ascii``/``w:hAnsi``.
#:
#: The two are deliberately different. Vazirmatn does contain Latin glyphs, but
#: its Latin is a *derived* set merged from Roboto by the font's build script
#: (rastikerdar/vazirmatn), so using it as both roles makes the English in a
#: Persian paragraph visually indistinguishable from the Persian and gives no
#: typographic separation. A dedicated Latin face renders `HbA1c` and
#: `Type 2 Diabetes` as English, which is the whole point of a mixed-script
#: document.
#:
#: Individual DOCX_FONT_* variables always win over the profile, so a profile is
#: only a default. Fonts are referenced by name and advertised via w:altName for
#: substitution; they are NOT embedded in the file.
FONT_PROFILES = {
    "persian_modern": {
        "body": "Vazirmatn",
        "heading": "Vazirmatn",
        # Modern Word default Latin face; pairs well with Vazirmatn's x-height.
        "latin": "Aptos",
        "fallback": "Tahoma",
    },
    "traditional": {
        "body": "B Nazanin",
        "heading": "B Nazanin",
        "latin": "Times New Roman",
        "fallback": "Tahoma",
    },
    # Single-face behaviour: every role uses one face, so mixed runs are
    # visually identical. Kept for byte-comparable output with old documents.
    "legacy": {"body": "Tahoma", "heading": "Tahoma", "latin": "Tahoma", "fallback": "Tahoma"},
    # Latin in a humanist face that ships with most systems; useful where
    # Aptos (a recent Microsoft face) may not be installed.
    "persian_modern_alt": {
        "body": "Vazirmatn",
        "heading": "Vazirmatn",
        "latin": "Calibri",
        "fallback": "Tahoma",
    },
}


def resolve_font_profile(name: str | None) -> dict:
    """Return the four font roles for a named profile (unknown -> persian_modern)."""
    key = (name or "").strip().lower()
    return dict(FONT_PROFILES.get(key, FONT_PROFILES["persian_modern"]))


def resolve_note_mode(mode: str | None) -> str:
    """Normalise a configured/selected mode; unknown values fall back to full."""
    value = (mode or "").strip().lower()
    return value if value in NOTE_MODES else "full"

# Relative paths in the configuration (``.env``, ``data/...``) are anchored to
# the project directory, never to the process's working directory.  Cron jobs,
# Passenger and ``su -c`` all start processes in ``$HOME`` or ``/``, where a
# working-directory-relative ``data/`` would silently create a second, empty
# database and Telegram session.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROXY_SCHEMES = {"socks5": "socks5", "socks5h": "socks5", "socks4": "socks4", "http": "http"}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in TRUTHY


def _text(name: str, default: str) -> str:
    """Environment value, falling back to the default when blank.

    A commented-out or emptied line in ``.env`` must not turn into an empty
    base URL, model name or file path.
    """
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else default


def _anchor(value: str) -> Path:
    """``~`` expanded; relative paths anchored to the project directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _path(name: str, default: str) -> Path:
    return _anchor(_text(name, default))


def _default_env_file() -> Path:
    """``.env`` from the working directory if present, else from the project root."""
    local = Path(".env")
    return local if local.is_file() else PROJECT_ROOT / ".env"


def parse_proxy(value: str) -> tuple:
    """Turn ``socks5://user:pass@host:1080`` into a Telethon proxy tuple."""
    parsed = urlparse(value)
    scheme = PROXY_SCHEMES.get(parsed.scheme.lower())
    try:
        port = parsed.port
    except ValueError:
        port = None
    if scheme is None or not parsed.hostname or not port:
        raise ValueError(
            "TELEGRAM_PROXY باید مانند socks5://host:port یا http://user:pass@host:port باشد."
        )
    return (
        scheme,
        parsed.hostname,
        port,
        True,
        unquote(parsed.username) if parsed.username else None,
        unquote(parsed.password) if parsed.password else None,
    )


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    admin_ids: frozenset[int]
    database_path: Path
    session_path: Path
    temp_dir: Path
    max_file_size: int
    stt_primary: str
    stt_language: str
    stt_fallback_enabled: bool
    stt_min_confidence: float
    speechmatics_api_key: str | None
    speechmatics_base_url: str
    deepgram_api_key: str | None
    deepgram_model: str
    gemini_api_key: str | None
    gemini_model: str
    max_concurrent_jobs: int
    stt_poll_interval: float
    stt_job_timeout: int
    # Accuracy-first Speechmatics tier: "enhanced" is the provider's highest
    # accuracy model; "standard" only exists for throughput-bound deployments.
    speechmatics_model: str = "enhanced"
    # Optional custom dictionary (native Speechmatics additional_vocab) for
    # drug names and English technical terms; empty disables it.
    speechmatics_additional_vocab: tuple[str, ...] = ()
    # Optional OpenAI-compatible STT endpoint (POST /audio/transcriptions) such
    # as OpenAI whisper-1, Groq whisper-large-v3, or a local vLLM/Ollama
    # gateway. Unset base URL means the provider is not configured. The key is
    # optional for local endpoints. Files at/above stt_openai_max_upload are
    # routed to another configured engine instead of being chunked, because
    # splitting audio would hurt boundary accuracy.
    stt_openai_base_url: str | None = None
    stt_openai_api_key: str | None = None
    stt_openai_model: str = "whisper-1"
    stt_openai_max_upload: int = 25_000_000
    # The historical Gemini fields remain for backwards compatibility.  New
    # installations can select Gemini, Anthropic, or any OpenAI-compatible API
    # through the provider-neutral NOTE_API_* settings below.
    note_api_provider: str = "gemini"
    note_api_key: str | None = None
    note_api_base_url: str | None = None
    note_api_model: str | None = None
    note_api_extra_headers: tuple[tuple[str, str], ...] = ()
    note_api_timeout: int = 240
    note_api_retries: int = 2
    note_api_max_output_tokens: int = 8192
    # Opt-in response_format={"type":"json_object"} for OpenAI-compatible note
    # providers. Not every gateway implements it, so the strict system prompt
    # is the default and this only tightens providers that support JSON mode.
    note_api_json_mode: bool = False
    # Complex-script font used inside the generated Word document (Tahoma is
    # present everywhere; set B Nazanin/Vazirmatn when the audience has it).
    docx_font: str = "Tahoma"
    # Per-role document faces. ``fallback`` is advertised in word/fontTable.xml
    # (w:altName) so readers without the Persian face substitute it gracefully.
    # A blank per-role value defers to the named profile (see FONT_PROFILES).
    docx_font_body: str = ""
    docx_font_heading: str = ""
    docx_font_latin: str = ""
    docx_font_fallback: str = "Tahoma"
    docx_font_profile: str = "persian_modern"
    # Note-generation compression mode: full (default), standard or summary.
    # Only ``summary`` intentionally compresses; ``full`` preserves detail.
    note_mode: str = "full"
    # Optional second provider pass, used ONLY when deterministic QA shows the
    # notes lost numbers/terms or were compressed far below a compiled lecture.
    # Disabled by setting NOTE_REPAIR_ENABLED=false; the normal path is always
    # a single call.
    note_repair_enabled: bool = True
    # Global-context layer for multi-part lectures: one cheap outline call, a
    # shared context block in every part, and one controlled editorial
    # compilation at the end. Single-part lectures always stay one call.
    # NOTE_GLOBAL_CONTEXT_ENABLED=false restores the chunk-only behaviour.
    note_global_context_enabled: bool = True
    # --- Word document design (cover / TOC / page frame / footer brand) ---
    docx_cover_enabled: bool = True
    docx_toc_enabled: bool = True
    #: Heading levels in the automatic table of contents ("1-1" = topics only).
    docx_toc_levels: str = "1-1"
    #: Section count from which a document earns a table of contents.
    docx_toc_min_sections: int = 4
    docx_page_border_enabled: bool = True
    docx_page_border_style: str = "single"
    docx_page_border_color: str = "BFCEE4"
    docx_page_border_size: int = 8
    docx_page_border_space: int = 24
    docx_show_footer_brand: bool = True
    # Optional local logo. Empty means "use the typographic Gamas mark"; no
    # image is ever fetched at generation time and a missing file never fails.
    docx_logo_path: str = ""
    # The playful progress bar; when disabled only real stage updates are sent.
    progress_animation: bool = True
    presentation_enabled: bool = True
    presentation_include_slide_text: bool = True
    presentation_include_video_audio: bool = True
    presentation_legacy_enabled: bool = True
    presentation_min_clip_seconds: float = 1.0
    presentation_silence_seconds: float = 0.5
    presentation_max_clips: int = 300
    presentation_max_total_duration: int = 21600
    presentation_max_unpacked_bytes: int = 4_000_000_000
    presentation_wav_limit_bytes: int = 700_000_000
    # The media pipeline runs in a Python child process (PyAV / ppt2pptx), so
    # no FFMPEG_BIN / FFPROBE_BIN / SOFFICE_BIN settings exist any more.  The
    # legacy FFMPEG_TIMEOUT_SECONDS and SOFFICE_TIMEOUT_SECONDS variables are
    # still honoured as fallbacks for the two timeouts below.
    media_timeout: int = 3600
    convert_timeout: int = 600
    log_level: str = "INFO"
    log_format: str = "text"
    log_file: Path | None = None
    log_max_bytes: int = 10_000_000
    log_backup_count: int = 5
    # Optional Telethon proxy tuple (type, host, port, rdns, user, password)
    # for hosts that cannot reach Telegram's MTProto servers directly.
    telegram_proxy: tuple | None = None

    @property
    def lock_path(self) -> Path:
        """Advisory lock guarding the Telegram session against a second instance."""
        return self.session_path.with_name(self.session_path.name + ".lock")

    @property
    def docx_fonts(self) -> dict:
        """Per-role document faces.

        Resolution order per role: the explicit ``DOCX_FONT_*`` variable, then
        the named profile (``DOCX_FONT_PROFILE``), then the legacy single
        ``DOCX_FONT``. An operator who sets one variable keeps it, so this
        stays backward compatible with existing deployments.
        """
        profile = resolve_font_profile(self.docx_font_profile)
        return {
            "body": self.docx_font_body or profile["body"] or self.docx_font,
            "heading": self.docx_font_heading or profile["heading"] or self.docx_font,
            "latin": self.docx_font_latin or profile["latin"] or self.docx_font,
            "fallback": self.docx_font_fallback or profile["fallback"] or self.docx_font,
        }

    @property
    def docx_design(self) -> dict:
        """Document-design options (see :func:`gamas_bot.docx_export.resolve_design`)."""
        return {
            "cover_enabled": self.docx_cover_enabled,
            "toc_enabled": self.docx_toc_enabled,
            "toc_levels": self.docx_toc_levels,
            "toc_min_sections": self.docx_toc_min_sections,
            "page_border_enabled": self.docx_page_border_enabled,
            "border_style": self.docx_page_border_style,
            "border_color": self.docx_page_border_color,
            "border_size": self.docx_page_border_size,
            "border_space": self.docx_page_border_space,
            "footer_brand": self.docx_show_footer_brand,
            "logo_path": self.docx_logo_path,
        }

    @property
    def effective_note_api_key(self) -> str | None:
        """Return the provider-neutral key, falling back to the legacy Gemini key."""
        if self.note_api_key:
            return self.note_api_key
        if self.note_api_provider == "gemini":
            return self.gemini_api_key
        return None

    @property
    def effective_note_model(self) -> str:
        if self.note_api_model:
            return self.note_api_model
        if self.note_api_provider == "gemini":
            return self.gemini_model
        if self.note_api_provider == "anthropic":
            return "claude-3-5-haiku-latest"
        return "gpt-4o-mini"

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "Settings":
        load_dotenv(env_file if env_file is not None else _default_env_file(), override=False)
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        try:
            api_id = int(_text("TELEGRAM_API_ID", "0"))
            admins = frozenset(
                int(item.strip())
                for item in os.getenv("ADMIN_IDS", "").split(",")
                if item.strip()
            )
        except ValueError as exc:
            raise ValueError("TELEGRAM_API_ID و ADMIN_IDS باید عددی باشند.") from exc

        primary = _text("STT_PRIMARY", "speechmatics").lower()
        if primary not in {"speechmatics", "deepgram", "openai_compatible"}:
            raise ValueError(
                "STT_PRIMARY فقط می‌تواند speechmatics، deepgram یا openai_compatible باشد."
            )
        language = _text("STT_LANGUAGE", "fa")
        if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", language):
            raise ValueError("STT_LANGUAGE باید کد زبان معتبر مانند fa یا en-US باشد.")

        speechmatics_model = _text("SPEECHMATICS_MODEL", "enhanced").lower()
        if speechmatics_model not in {"standard", "enhanced"}:
            raise ValueError("SPEECHMATICS_MODEL فقط می‌تواند standard یا enhanced باشد.")
        vocab_terms: list[str] = []
        seen_terms: set[str] = set()
        for term in re.split(r"[,،؛\n]+", os.getenv("SPEECHMATICS_ADDITIONAL_VOCAB", "")):
            term = term.strip()
            if term and term.casefold() not in seen_terms:
                seen_terms.add(term.casefold())
                vocab_terms.append(term)
        if len(vocab_terms) > 20_000:
            raise ValueError(
                "تعداد اصطلاح‌های SPEECHMATICS_ADDITIONAL_VOCAB از سقف سرویس (۲۰ هزار) بیشتر است."
            )
        stt_openai_base = os.getenv("STT_OPENAI_BASE_URL", "").strip() or None
        if stt_openai_base:
            parsed_stt_url = urlparse(stt_openai_base)
            if parsed_stt_url.scheme not in {"http", "https"} or not parsed_stt_url.netloc:
                raise ValueError("STT_OPENAI_BASE_URL باید یک نشانی کامل http یا https باشد.")

        note_provider = _text("NOTE_API_PROVIDER", "gemini").lower()
        note_provider = {
            "openai": "openai_compatible",
            "openai-compatible": "openai_compatible",
            "custom": "openai_compatible",
            "none": "disabled",
            "off": "disabled",
        }.get(note_provider, note_provider)
        if note_provider not in {"gemini", "openai_compatible", "anthropic", "disabled"}:
            raise ValueError(
                "NOTE_API_PROVIDER باید یکی از gemini، openai_compatible، anthropic یا disabled باشد."
            )
        note_base_url = os.getenv("NOTE_API_BASE_URL", "").strip() or None
        if note_base_url:
            parsed_url = urlparse(note_base_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ValueError("NOTE_API_BASE_URL باید یک نشانی کامل http یا https باشد.")
        try:
            extra_headers_value = json.loads(
                os.getenv("NOTE_API_EXTRA_HEADERS_JSON", "{}").strip() or "{}"
            )
        except json.JSONDecodeError as exc:
            raise ValueError("NOTE_API_EXTRA_HEADERS_JSON باید یک JSON object معتبر باشد.") from exc
        if not isinstance(extra_headers_value, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in extra_headers_value.items()
        ):
            raise ValueError("NOTE_API_EXTRA_HEADERS_JSON فقط باید شامل کلید و مقدار متنی باشد.")
        note_headers = tuple((key, value) for key, value in extra_headers_value.items())

        try:
            min_confidence = float(_text("STT_MIN_CONFIDENCE", "0.65"))
            max_file_size = int(_text("MAX_FILE_SIZE_BYTES", "2000000000"))
            max_jobs = int(_text("MAX_CONCURRENT_JOBS", "3"))
            poll_interval = float(_text("STT_POLL_INTERVAL_SECONDS", "5"))
            job_timeout = int(_text("STT_JOB_TIMEOUT_SECONDS", "21600"))
            note_timeout = int(_text("NOTE_API_TIMEOUT_SECONDS", "240"))
            note_retries = int(_text("NOTE_API_RETRIES", "2"))
            note_max_tokens = int(_text("NOTE_API_MAX_OUTPUT_TOKENS", "8192"))
            stt_openai_max = int(_text("STT_OPENAI_MAX_UPLOAD_BYTES", "25000000"))
            log_max_bytes = int(_text("LOG_MAX_BYTES", "10000000"))
            log_backup_count = int(_text("LOG_BACKUP_COUNT", "5"))
        except ValueError as exc:
            raise ValueError("مقادیر عددی تنظیمات محیط معتبر نیستند.") from exc
        if not all(math.isfinite(value) for value in (min_confidence, poll_interval)):
            raise ValueError("مقادیر اعشاری STT باید عدد متناهی باشند.")
        if not 0 <= min_confidence <= 1:
            raise ValueError("STT_MIN_CONFIDENCE باید بین صفر و یک باشد.")
        if min(max_file_size, max_jobs, poll_interval, job_timeout, note_timeout, note_max_tokens) <= 0:
            raise ValueError("اندازه فایل، هم‌زمانی و زمان‌های انتظار باید مثبت باشند.")
        if note_retries < 0 or note_retries > 10:
            raise ValueError("NOTE_API_RETRIES باید بین صفر تا ۱۰ باشد.")
        if stt_openai_max <= 0:
            raise ValueError("STT_OPENAI_MAX_UPLOAD_BYTES باید مثبت باشد.")
        if log_max_bytes <= 0 or log_backup_count < 0:
            raise ValueError("تنظیمات چرخش فایل لاگ معتبر نیستند.")
        log_level = _text("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL باید DEBUG، INFO، WARNING، ERROR یا CRITICAL باشد.")
        log_format = _text("LOG_FORMAT", "text").lower()
        if log_format not in {"text", "json"}:
            raise ValueError("LOG_FORMAT فقط می‌تواند text یا json باشد.")
        docx_border_color = _text("DOCX_PAGE_BORDER_COLOR", "BFCEE4").lstrip("#").upper()
        if not re.fullmatch(r"[0-9A-F]{6}", docx_border_color):
            raise ValueError(
                "DOCX_PAGE_BORDER_COLOR باید یک رنگ هگز شش‌رقمی باشد؛ مثل BFCEE4."
            )
        # An unknown style silently falls back to the default single line (see
        # resolve_design); the numbers below are strict, because a wrong value
        # would produce an invalid w:pgBorders element.
        docx_border_style = _text("DOCX_PAGE_BORDER_STYLE", "single")
        try:
            docx_border_size = int(_text("DOCX_PAGE_BORDER_WIDTH", "8"))
            docx_border_space = int(_text("DOCX_PAGE_BORDER_SPACE", "24"))
        except ValueError as exc:
            raise ValueError(
                "DOCX_PAGE_BORDER_WIDTH و DOCX_PAGE_BORDER_SPACE باید عددی باشند."
            ) from exc
        if not 2 <= docx_border_size <= 96:
            raise ValueError("DOCX_PAGE_BORDER_WIDTH باید بین ۲ و ۹۶ باشد (هشتم نقطه).")
        if not 0 <= docx_border_space <= 31:
            raise ValueError("DOCX_PAGE_BORDER_SPACE باید بین ۰ و ۳۱ نقطه باشد.")
        # The TOC levels must be a level range Word understands. Unlike the
        # border *style* (which can fall back to a plain line), a typo here is
        # rejected at startup: silently building a different TOC than the one
        # that was configured is worse than a clear error. ``resolve_design()``
        # stays permissive for programmatic callers and falls back to level 1.
        docx_toc_levels = _text("DOCX_TOC_LEVELS", "1-1").replace("–", "-").replace(" ", "")
        if docx_toc_levels not in {"1", "1-1", "1-2", "1-3"}:
            raise ValueError("DOCX_TOC_LEVELS باید یکی از 1، 1-1، 1-2 یا 1-3 باشد.")
        try:
            docx_toc_min_sections = int(_text("DOCX_TOC_MIN_SECTIONS", "4"))
        except ValueError as exc:
            raise ValueError("DOCX_TOC_MIN_SECTIONS باید عددی باشد.") from exc
        if not 1 <= docx_toc_min_sections <= 200:
            raise ValueError("DOCX_TOC_MIN_SECTIONS باید بین ۱ و ۲۰۰ باشد.")
        log_file_value = os.getenv("LOG_FILE", "").strip()
        proxy_value = os.getenv("TELEGRAM_PROXY", "").strip()
        telegram_proxy = parse_proxy(proxy_value) if proxy_value else None

        # Timeouts keep their historical environment variables as fallbacks so
        # existing .env files continue to work unchanged.
        media_timeout_raw = (
            _text("MEDIA_TIMEOUT_SECONDS", "")
            or _text("FFMPEG_TIMEOUT_SECONDS", "3600")
        )
        convert_timeout_raw = (
            _text("PPT_CONVERT_TIMEOUT_SECONDS", "")
            or _text("SOFFICE_TIMEOUT_SECONDS", "600")
        )
        try:
            min_clip = float(_text("PPTX_MIN_CLIP_SECONDS", "1.0"))
            silence = float(_text("PPTX_SILENCE_SECONDS", "0.5"))
            max_clips = int(_text("PPTX_MAX_CLIPS", "300"))
            max_total_duration = int(_text("PPTX_MAX_TOTAL_DURATION_SECONDS", "21600"))
            max_unpacked = int(_text("PPTX_MAX_UNPACKED_BYTES", "4000000000"))
            wav_limit = int(_text("PPTX_WAV_LIMIT_BYTES", "700000000"))
            media_timeout = int(media_timeout_raw)
            convert_timeout = int(convert_timeout_raw)
        except ValueError as exc:
            raise ValueError("مقادیر عددی مربوط به پردازش فایل ارائه معتبر نیستند.") from exc
        if not all(math.isfinite(value) for value in (min_clip, silence)):
            raise ValueError("زمان‌های فایل ارائه باید عدد متناهی باشند.")
        if min_clip < 0 or silence < 0:
            raise ValueError("PPTX_MIN_CLIP_SECONDS و PPTX_SILENCE_SECONDS نمی‌توانند منفی باشند.")
        if min(max_clips, max_total_duration, max_unpacked, wav_limit) <= 0:
            raise ValueError("محدودیت‌های عددی فایل ارائه باید مثبت باشند.")
        if min(media_timeout, convert_timeout) <= 0:
            raise ValueError("زمان‌های انتظار پردازش رسانه و تبدیل ارائه باید مثبت باشند.")

        return cls(
            telegram_bot_token=token,
            telegram_api_id=api_id,
            telegram_api_hash=api_hash,
            admin_ids=admins,
            database_path=_path("DATABASE_PATH", "data/bot.sqlite3"),
            session_path=_path("TELEGRAM_SESSION_PATH", "data/telegram_bot"),
            temp_dir=_path("TEMP_DIR", "data/tmp"),
            max_file_size=max_file_size,
            stt_primary=primary,
            stt_language=language,
            stt_fallback_enabled=_flag("STT_FALLBACK_ENABLED", True),
            stt_min_confidence=min_confidence,
            speechmatics_api_key=(os.getenv("SPEECHMATICS_API_KEY", "").strip() or None),
            speechmatics_base_url=_text(
                "SPEECHMATICS_BASE_URL", "https://eu1.asr.api.speechmatics.com/v2"
            ).rstrip("/"),
            deepgram_api_key=(os.getenv("DEEPGRAM_API_KEY", "").strip() or None),
            deepgram_model=_text("DEEPGRAM_MODEL", "nova-3"),
            gemini_api_key=(os.getenv("GEMINI_API_KEY", "").strip() or None),
            gemini_model=_text("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            max_concurrent_jobs=max_jobs,
            stt_poll_interval=poll_interval,
            stt_job_timeout=job_timeout,
            speechmatics_model=speechmatics_model,
            speechmatics_additional_vocab=tuple(vocab_terms),
            stt_openai_base_url=stt_openai_base.rstrip("/") if stt_openai_base else None,
            stt_openai_api_key=(os.getenv("STT_OPENAI_API_KEY", "").strip() or None),
            stt_openai_model=_text("STT_OPENAI_MODEL", "whisper-1"),
            stt_openai_max_upload=stt_openai_max,
            note_api_provider=note_provider,
            note_api_key=(os.getenv("NOTE_API_KEY", "").strip() or None),
            note_api_base_url=note_base_url,
            note_api_model=(os.getenv("NOTE_API_MODEL", "").strip() or None),
            note_api_extra_headers=note_headers,
            note_api_timeout=note_timeout,
            note_api_retries=note_retries,
            note_api_max_output_tokens=note_max_tokens,
            note_api_json_mode=_flag("NOTE_API_JSON_MODE", False),
            docx_font=_text("DOCX_FONT", "Tahoma"),
            # ``DOCX_FONT_*`` is the canonical spelling; the symmetrical
            # ``DOCX_<ROLE>_FONT`` form is accepted as an alias so a deployment
            # written against either name behaves identically. Blank means
            # "use the profile" (see FONT_PROFILES).
            docx_font_body=_text("DOCX_FONT_BODY", "") or _text("DOCX_BODY_FONT", ""),
            docx_font_heading=_text("DOCX_FONT_HEADING", "") or _text("DOCX_HEADING_FONT", ""),
            docx_font_latin=_text("DOCX_FONT_LATIN", "") or _text("DOCX_LATIN_FONT", ""),
            # Blank defers to the profile's fallback (Tahoma in every profile).
            docx_font_fallback=_text("DOCX_FONT_FALLBACK", "")
            or _text("DOCX_FALLBACK_FONT", ""),
            docx_font_profile=_text("DOCX_FONT_PROFILE", "persian_modern"),
            note_mode=resolve_note_mode(_text("NOTE_MODE", "full")),
            note_repair_enabled=_flag("NOTE_REPAIR_ENABLED", True),
            note_global_context_enabled=_flag("NOTE_GLOBAL_CONTEXT_ENABLED", True),
            docx_cover_enabled=_flag("DOCX_COVER_ENABLED", True),
            docx_toc_enabled=_flag("DOCX_TOC_ENABLED", True),
            docx_toc_levels=docx_toc_levels,
            docx_toc_min_sections=docx_toc_min_sections,
            docx_page_border_enabled=_flag("DOCX_PAGE_BORDER_ENABLED", True),
            docx_page_border_style=docx_border_style,
            docx_page_border_color=docx_border_color,
            docx_page_border_size=docx_border_size,
            docx_page_border_space=docx_border_space,
            docx_show_footer_brand=_flag("DOCX_SHOW_FOOTER_BRAND", True),
            docx_logo_path=_text("DOCX_LOGO_PATH", ""),
            progress_animation=_flag("PROGRESS_ANIMATION_ENABLED", True),
            presentation_enabled=_flag("PPTX_ENABLED", True),
            presentation_include_slide_text=_flag("PPTX_INCLUDE_SLIDE_TEXT", True),
            presentation_include_video_audio=_flag("PPTX_INCLUDE_VIDEO_AUDIO", True),
            presentation_legacy_enabled=_flag("PPTX_LEGACY_ENABLED", True),
            presentation_min_clip_seconds=min_clip,
            presentation_silence_seconds=silence,
            presentation_max_clips=max_clips,
            presentation_max_total_duration=max_total_duration,
            presentation_max_unpacked_bytes=max_unpacked,
            presentation_wav_limit_bytes=wav_limit,
            media_timeout=media_timeout,
            convert_timeout=convert_timeout,
            log_level=log_level,
            log_format=log_format,
            log_file=_anchor(log_file_value) if log_file_value else None,
            log_max_bytes=log_max_bytes,
            log_backup_count=log_backup_count,
            telegram_proxy=telegram_proxy,
        )

    def validate_runtime(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if self.telegram_api_id <= 0:
            missing.append("TELEGRAM_API_ID")
        if not self.telegram_api_hash:
            missing.append("TELEGRAM_API_HASH")
        if missing:
            raise ValueError("متغیرهای ضروری تنظیم نشده‌اند: " + ", ".join(missing))
        if (
            not self.speechmatics_api_key
            and not self.deepgram_api_key
            and not self.stt_openai_base_url
        ):
            raise ValueError(
                "حداقل یکی از SPEECHMATICS_API_KEY، DEEPGRAM_API_KEY یا STT_OPENAI_BASE_URL لازم است."
            )
