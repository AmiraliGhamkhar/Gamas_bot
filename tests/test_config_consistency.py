"""Configuration ↔ documentation consistency.

``.env.example`` is the operator-facing contract of this project: every
variable ``config.py`` reads is documented there, and the value shown next to
a variable is the value the code actually uses. Both kinds of drift have
caused real confusion (a Deepgram model without Persian support, a stale log
path, a database name that no longer matched ``config.py``), so they are
locked here instead of being caught by reading the code.

The third check keeps the v3 documentation honest: the deleted v2 provider
platform must not be advertised as configuration again.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from gamas_bot import config
from gamas_bot.config import PLAN_ENV_FIELDS, PROJECT_DIR, from_env

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CONFIG_SOURCE = Path(config.__file__)

#: ``NAME=value`` and ``#NAME=value`` lines of the example file.
ENV_LINE = re.compile(r"^#?\s*([A-Z0-9_]+)=(.*)$", re.M)

#: Variables that only exist to document an optional integration and are read
#: conditionally rather than through ``_env`` in the code path scanned below.
EXTRA_DOCUMENTED = frozenset()

#: ``(variable, Settings attribute, kind)`` for the values an operator reads as
#: "defaults" in the example file. ``kind`` drives the comparison.
DOCUMENTED_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("DB_PATH", "db_path", "path"),
    ("TEMP_DIR", "temp_dir", "path"),
    ("LOG_FILE", "log_file", "path"),
    ("LOG_LEVEL", "log_level", "str"),
    ("LOG_FORMAT", "log_format", "str"),
    ("LOG_MAX_BYTES", "log_max_bytes", "int"),
    ("LOG_BACKUP_COUNT", "log_backup_count", "int"),
    ("MAX_FILE_SIZE", "max_file_size", "int"),
    ("MAX_MEDIA_DURATION_SECONDS", "max_media_duration_seconds", "float"),
    ("MAX_ACTIVE_JOBS", "max_active_jobs", "int"),
    ("MAX_ACTIVE_JOBS_PER_USER", "max_active_jobs_per_user", "int"),
    ("MAX_QUEUED_JOBS", "max_queued_jobs", "int"),
    ("MAX_CONCURRENT_DOWNLOADS", "max_concurrent_downloads", "int"),
    ("MEDIA_WORKER_CONCURRENCY", "media_worker_concurrency", "int"),
    ("MAX_CONCURRENT_UPLOADS", "max_concurrent_uploads", "int"),
    ("DEDUP_WINDOW_SECONDS", "dedup_window_seconds", "float"),
    ("MEDIA_TIMEOUT", "media_timeout", "float"),
    ("CONVERT_TIMEOUT", "convert_timeout", "float"),
    ("STT_JOB_TIMEOUT", "stt_job_timeout", "float"),
    ("STT_PROVIDER_TIMEOUT_SECONDS", "stt_provider_timeout", "float"),
    ("STT_SLOT_WAIT_TIMEOUT_SECONDS", "stt_slot_wait_timeout", "float"),
    ("STT_POLL_INTERVAL", "stt_poll_interval", "float"),
    ("STT_TRANSFER_TIMEOUT_SECONDS", "stt_transfer_timeout", "float"),
    ("MAX_STT_RESPONSE_BYTES", "max_stt_response_bytes", "int"),
    ("TEMP_MIN_FREE_BYTES", "temp_min_free_bytes", "int"),
    ("DOWNLOAD_STALL_SECONDS", "download_stall_seconds", "float"),
    ("TELEGRAM_DOWNLOAD_CONNECTIONS", "telegram_download_connections", "int"),
    ("AUDIO_FORMAT_POLICY", "audio_format_policy", "str"),
    ("STT_STATUS_LONG_POLL", "stt_status_long_poll", "float"),
    ("STT_PROVIDERS", "stt_providers", "csv"),
    ("STT_FALLBACK_ENABLED", "stt_fallback_enabled", "bool"),
    ("STT_LANGUAGE", "stt_language", "str"),
    ("SPEECHMATICS_MODEL", "speechmatics_model", "str"),
    ("SPEECHMATICS_MAX_IN_FLIGHT", "speechmatics_max_in_flight", "int"),
    ("DEEPGRAM_BASE_URL", "deepgram_base_url", "str"),
    ("DEEPGRAM_MODEL", "deepgram_model", "str"),
    ("DEEPGRAM_MAX_IN_FLIGHT", "deepgram_max_in_flight", "int"),
    ("STT_OPENAI_MODEL", "stt_openai_model", "str"),
    ("STT_OPENAI_MAX_UPLOAD_BYTES", "stt_openai_max_upload", "int"),
    ("STT_OPENAI_MAX_IN_FLIGHT", "stt_openai_max_in_flight", "int"),
    ("NOTE_PROVIDERS", "note_providers", "csv"),
    ("NOTE_PROVIDER", "note_provider", "str"),
    ("NOTE_MODEL", "note_model", "str"),
    ("NARA_API_KEY", "nara_api_key", "str"),
    ("NARA_BASE_URL", "nara_base_url", "str"),
    ("NARA_MODEL", "nara_model", "str"),
    ("OPENROUTER_API_KEY", "openrouter_api_key", "str"),
    ("OPENROUTER_BASE_URL", "openrouter_base_url", "str"),
    ("OPENROUTER_MODEL", "openrouter_model", "str"),
    ("GROQ_BASE_URL", "groq_base_url", "str"),
    ("GROQ_MODEL", "groq_model", "str"),
    ("CEREBRAS_BASE_URL", "cerebras_base_url", "str"),
    ("CEREBRAS_MODEL", "cerebras_model", "str"),
    ("GEMINI_MODEL", "gemini_model", "str"),
    ("NOTE_GEMINI_THINKING_BUDGET", "gemini_thinking_budget", "int"),
    ("NOTE_CONTEXT_TOKENS", "note_context_tokens", "int"),
    ("NOTE_MODE", "note_mode", "str"),
    ("NOTE_MAX_OUTPUT_TOKENS", "note_max_output_tokens", "int"),
    ("NOTE_REQUEST_TIMEOUT_SECONDS", "note_request_timeout", "float"),
    ("NOTE_CHUNK_CHARS", "note_chunk_chars", "int"),
    ("NOTE_CONCURRENCY", "note_concurrency", "int"),
    ("LLM_PRIMARY_MAX_IN_FLIGHT", "llm_primary_max_in_flight", "int"),
    ("LLM_FALLBACK_MAX_IN_FLIGHT", "llm_fallback_max_in_flight", "int"),
    ("LLM_SLOT_WAIT_TIMEOUT_SECONDS", "llm_slot_wait_timeout", "float"),
    ("NOTE_OPENROUTER_THROUGHPUT", "note_openrouter_throughput", "bool"),
    ("NOTE_STRUCTURED_OUTPUT", "note_structured_output", "bool"),
    ("NOTE_PROGRESSIVE_BOOKLET", "note_progressive_booklet", "bool"),
    ("NOTE_COMPILE_PROVIDER", "note_compile_provider", "str"),
    ("NOTE_COMPILE_MAX_CHARS", "note_compile_max_chars", "int"),
    ("NOTE_REASONING_EFFORT", "note_reasoning_effort", "str"),
    ("RAW_TEXT_PROFILE", "raw_text_profile", "str"),
    ("RAW_FALLBACK_FILE_CHARS", "raw_fallback_file_chars", "int"),
    ("RAW_FALLBACK_MAX_FILES", "raw_fallback_max_files", "int"),
    ("OFFICE_ENABLED", "office_enabled", "bool"),
    ("OFFICE_MAX_INPUT_BYTES", "office_max_input_bytes", "int"),
    ("OFFICE_MAX_CHARS", "office_max_chars", "int"),
    ("OFFICE_MAX_SLIDES", "office_max_slides", "int"),
    ("OFFICE_SESSION_TTL_MINUTES", "office_session_ttl_minutes", "int"),
    ("OFFICE_JOB_CONCURRENCY", "office_job_concurrency", "int"),
    ("OFFICE_MIN_INTERVAL_SECONDS", "office_min_interval_seconds", "float"),
    ("PRESENTATION_MAX_CLIPS", "presentation_max_clips", "int"),
    ("PRESENTATION_MAX_TOTAL_DURATION", "presentation_max_total_duration", "float"),
    ("PRESENTATION_MAX_UNPACKED_BYTES", "presentation_max_unpacked_bytes", "int"),
    ("PRESENTATION_MAX_TEMP_BYTES", "presentation_max_temp_bytes", "int"),
    ("PRESENTATION_MIN_CLIP_SECONDS", "presentation_min_clip_seconds", "float"),
    ("PRESENTATION_SILENCE_SECONDS", "presentation_silence_seconds", "float"),
    ("PRESENTATION_WAV_LIMIT_BYTES", "presentation_wav_limit_bytes", "int"),
    ("PRESENTATION_INCLUDE_UNREFERENCED_MEDIA", "presentation_include_unreferenced_media", "bool"),
    ("RECEIPT_DIR", "receipt_dir", "path"),
    ("RECEIPT_RETENTION_DAYS", "receipt_retention_days", "int"),
    ("MAX_PAYMENT_RECEIPT_BYTES", "max_payment_receipt_bytes", "int"),
    ("TRANSCRIPT_RETENTION_DAYS", "transcript_retention_days", "int"),
)

#: v2 provider-platform settings that no longer exist in the code. Documentation
#: must not tell an operator to set them.
REMOVED_V2_SETTINGS = (
    "STT_PRIMARY",
    "STT_DEFAULT_ROUTE",
    "STT_MAX_PROVIDER_FAILOVERS",
    "STT_MAX_ATTEMPTS",
    "STT_FREE_ONLY",
    "STT_TRIAL_ALLOWLIST",
    "STT_ALLOW_TRIAL_PROVIDERS",
    "STT_ALLOW_PAID_FALLBACK",
    "STT_QUOTA_SAFETY_MARGIN",
    "STT_QUALITY_GATE_ENABLED",
    "STT_REQUEST_DIARIZATION",
    "STT_REQUEST_WORD_TIMESTAMPS",
    "SPEECHMATICS_OPERATING_POINT",
    "SPEECHMATICS_ADDITIONAL_VOCAB",
    "SPEECHMATICS_MODEL_FIELD",
)

STT_DOC_PAGES = (
    "docs/STT_PROVIDERS.md",
    "docs/CONFIGURATION.md",
    "docs/TROUBLESHOOTING.md",
    "docs/PROVIDERS.md",
    "docs/DEPLOY_FA.md",
    ".env.example",
)


def example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for name, value in ENV_LINE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")):
        values[name] = value.strip()
    return values


def configured_names() -> set[str]:
    """Every environment variable name ``config.py`` reads."""
    source = CONFIG_SOURCE.read_text(encoding="utf-8")
    names = set(re.findall(r'_env(?:_int|_float|_bool)?\(\s*"([A-Z0-9_]+)"', source))
    names.update(name for name, _attribute, _default in PLAN_ENV_FIELDS)
    return names


def default_settings():
    """``Settings`` built from an empty environment: the code defaults."""
    with mock.patch.dict(os.environ, {}, clear=True):
        return from_env(env_file="/nonexistent")


class EnvExampleCoverageTests(unittest.TestCase):
    def test_every_variable_config_reads_is_documented(self):
        documented = set(example_values())
        missing = sorted(name for name in configured_names() if name not in documented)
        self.assertEqual(missing, [], f"undocumented configuration variables: {missing}")

    def test_every_documented_variable_is_read_by_the_code(self):
        read = configured_names() | EXTRA_DOCUMENTED
        unknown = sorted(name for name in example_values() if name not in read)
        self.assertEqual(unknown, [], f".env.example documents unused variables: {unknown}")


class DocumentedDefaultsTests(unittest.TestCase):
    def test_documented_defaults_are_what_the_code_produces(self):
        """Setting a variable to the documented value must give that value.

        ``.env.example`` shows a default next to each variable; this asserts the
        code parses the documented value into exactly the documented setting,
        which is how an operator reads the file. ``LOG_FILE`` is the one
        variable whose *unset* default is intentionally "no file", so its
        documented path is verified through the configured form.
        """
        documented = example_values()
        mismatches: list[str] = []
        for name, attribute, kind in DOCUMENTED_DEFAULTS:
            raw = documented.get(name)
            if raw is None:
                mismatches.append(f"{name}: missing from .env.example")
                continue
            with mock.patch.dict(os.environ, {name: raw}, clear=True):
                settings = from_env(env_file="/nonexistent")
            actual = getattr(settings, attribute)
            if kind == "csv":
                ok = list(actual) == [part.strip() for part in raw.split(",") if part.strip()]
            elif kind == "path":
                ok = actual == PROJECT_DIR / Path(raw)
            elif kind == "int":
                ok = int(actual) == int(raw)
            elif kind == "float":
                ok = float(actual) == float(raw)
            elif kind == "bool":
                ok = bool(actual) == (raw.strip().lower() in {"1", "true", "yes", "on"})
            else:
                ok = str(actual) == raw
            if not ok:
                mismatches.append(f"{name}: .env.example={raw!r} code={actual!r}")
        self.assertEqual(mismatches, [], "; ".join(mismatches))

    #: Variables whose `.env.example` line must show the *shipped* default, not
    #: merely some valid value. An operator who copies the file (or reads it as
    #: documentation) must get the behaviour the code actually has, so a stale
    #: number here is a documentation bug with a performance cost.
    SHIPPED_DEFAULT_EXAMPLES = (
        ("STT_PROVIDERS", "stt_providers", "csv"),
        ("NOTE_CONCURRENCY", "note_concurrency", "int"),
        ("NOTE_TTFB_TIMEOUT_SECONDS", "note_ttfb_timeout", "float"),
        ("NOTE_HEDGE_AFTER_SECONDS", "note_hedge_after", "float"),
        ("NOTE_STRUCTURED_OUTPUT", "note_structured_output", "bool"),
        ("NOTE_PROGRESSIVE_BOOKLET", "note_progressive_booklet", "bool"),
        ("NOTE_OPENROUTER_THROUGHPUT", "note_openrouter_throughput", "bool"),
    )

    def test_example_shows_the_shipped_default(self):
        documented = example_values()
        shipped = default_settings()
        mismatches: list[str] = []
        for name, attribute, kind in self.SHIPPED_DEFAULT_EXAMPLES:
            raw = documented.get(name)
            if raw is None:
                mismatches.append(f"{name}: missing from .env.example")
                continue
            actual = getattr(shipped, attribute)
            if kind == "csv":
                shown = tuple(part.strip() for part in raw.split(",") if part.strip())
            elif kind == "int":
                shown = int(raw)
            elif kind == "float":
                shown = float(raw)
            else:
                shown = raw.strip().lower() in {"1", "true", "yes", "on"}
            if shown != actual:
                mismatches.append(f"{name}: .env.example={raw!r} code={actual!r}")
        self.assertEqual(mismatches, [], "; ".join(mismatches))

    def test_stt_defaults_are_the_persian_ready_ones(self):
        settings = default_settings()
        # Persian-first deployment: the defaults must be able to transcribe fa.
        self.assertEqual(settings.stt_language, "fa")
        self.assertIn(settings.deepgram_model, config.DEEPGRAM_PERSIAN_MODELS)
        self.assertEqual(settings.speechmatics_model, "enhanced")
        self.assertEqual(
            settings.stt_providers, tuple(config.DEFAULT_STT_PROVIDERS.split(","))
        )


class SttDocumentationTests(unittest.TestCase):
    def test_stt_pages_do_not_advertise_removed_settings(self):
        for page in STT_DOC_PAGES:
            text = (REPO_ROOT / page).read_text(encoding="utf-8")
            for removed in REMOVED_V2_SETTINGS:
                self.assertNotIn(
                    removed, text, f"{page} still documents the removed setting {removed}"
                )

    def test_stt_providers_page_documents_the_real_settings(self):
        text = (REPO_ROOT / "docs/STT_PROVIDERS.md").read_text(encoding="utf-8")
        for name in (
            "STT_PROVIDERS",
            "STT_LANGUAGE",
            "SPEECHMATICS_API_KEY",
            "SPEECHMATICS_MODEL",
            "DEEPGRAM_API_KEY",
            "DEEPGRAM_MODEL",
            "DEEPGRAM_BASE_URL",
            "STT_OPENAI_BASE_URL",
            "STT_OPENAI_API_KEY",
            "STT_OPENAI_MODEL",
            "STT_OPENAI_MAX_UPLOAD_BYTES",
        ):
            self.assertIn(name, text, f"docs/STT_PROVIDERS.md does not document {name}")


class VersionConsistencyTests(unittest.TestCase):
    def test_package_version_matches_pyproject(self):
        import tomllib

        import gamas_bot

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(gamas_bot.__version__, data["project"]["version"])


if __name__ == "__main__":
    unittest.main()
