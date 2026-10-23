"""Tests for the v3 configuration surface."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from gamas_bot.config import (
    DEEPGRAM_PERSIAN_MODELS,
    DEFAULT_CEREBRAS_BASE_URL,
    DEFAULT_CEREBRAS_MODEL,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_THINKING_BUDGET,
    DEFAULT_GROQ_BASE_URL,
    DEFAULT_GROQ_MODEL,
    DEFAULT_NOTE_CHUNK_CHARS,
    DEFAULT_NOTE_CONTEXT_TOKENS,
    DEFAULT_NOTE_MODELS,
    DEFAULT_NOTE_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_STT_PROVIDERS,
    NOTE_BASE_URL_DEFAULTS,
    NOTE_HOST_PROVIDERS,
    NOTE_JSON_OBJECT_PROVIDERS,
    NOTE_PROVIDER_LABELS,
    NOTE_PROVIDER_SLUGS,
    NOTE_REQUEST_STYLE,
    PLAN_ENV_FIELDS,
    Settings,
    check,
    classify_note_provider,
    first_configured_stt_provider,
    format_payment_card,
    from_env,
    is_advisory,
    normalize_note_base_url,
    note_routes,
    note_slot_credentials,
    resolve_note_mode,
    resolve_note_reasoning_effort,
    resolve_raw_text_profile,
)


def env(**values):
    base = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_API_ID": "42",
        "TELEGRAM_API_HASH": "hash",
        "ADMIN_IDS": "111, 222",
        "SPEECHMATICS_API_KEY": "sm",
        "NOTE_PROVIDER": "openai_compatible",
        "NOTE_API_KEY": "k",
    }
    base.update({key: value for key, value in values.items() if value is not None})
    removed = [key for key, value in values.items() if value is None]
    for key in removed:
        base.pop(key, None)
    return base


class FromEnvTests(unittest.TestCase):
    def test_minimal_configuration(self):
        with mock.patch.dict(os.environ, env(), clear=False):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.bot_token, "123:abc")
        self.assertEqual(settings.api_id, 42)
        self.assertEqual(settings.admin_ids, frozenset({111, 222}))
        self.assertTrue(settings.speechmatics_configured)
        self.assertTrue(settings.note_configured)
        self.assertEqual(settings.note_model, "gpt-4o-mini")
        self.assertEqual(settings.stt_openai_max_upload, 25_000_000)
        self.assertEqual(settings.stt_providers, tuple(DEFAULT_STT_PROVIDERS.split(",")))
        # No *fatal* problems: a Speechmatics-only env legitimately earns
        # the capacity-coherence advisory (2 slots < 3 active jobs).
        self.assertEqual(
            [p for p in check(settings) if not is_advisory(p)], []
        )

    def test_provider_order_parsed_and_deduplicated(self):
        with mock.patch.dict(
            os.environ, env(STT_PROVIDERS="deepgram, speechmatics, deepgram, bogus"), clear=False
        ):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.stt_providers, ("deepgram", "speechmatics"))

    def test_legacy_gemini_key_selects_gemini_and_its_default_model(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g-key"}, clear=True):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.note_provider, "gemini")
        self.assertEqual(settings.note_api_key, "g-key")
        self.assertEqual(settings.note_model, DEFAULT_GEMINI_MODEL)

    def test_keyless_custom_base_url_keeps_openai_compatible_provider(self):
        with mock.patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "legacy-key", "NOTE_BASE_URL": "https://local.example/v1"},
            clear=True,
        ):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.note_provider, "openai_compatible")
        self.assertEqual(settings.note_api_key, "")
        self.assertTrue(settings.note_configured)

    def test_default_deepgram_model_can_transcribe_persian(self):
        """Persian is a Nova-3 language; Nova-2 has no Persian pack."""
        with mock.patch.dict(os.environ, env(), clear=False):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.deepgram_model, DEFAULT_DEEPGRAM_MODEL)
        self.assertIn(settings.deepgram_model, DEEPGRAM_PERSIAN_MODELS)

    def test_selected_but_unconfigured_stt_provider_is_not_ready(self):
        with mock.patch.dict(os.environ, env(STT_PROVIDERS="deepgram"), clear=False):
            settings = from_env(env_file="/nonexistent")
        self.assertFalse(settings.stt_configured)
        self.assertTrue(any("speech-to-text" in problem for problem in check(settings)))

    def test_openai_stt_upload_limit_is_configurable(self):
        with mock.patch.dict(
            os.environ, env(STT_OPENAI_MAX_UPLOAD_BYTES="100000000"), clear=False
        ):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.stt_openai_max_upload, 100_000_000)

    def test_relative_paths_anchored_to_project(self):
        with mock.patch.dict(os.environ, env(DB_PATH="data/x.db", TEMP_DIR="t"), clear=False):
            settings = from_env(env_file="/nonexistent")
        self.assertTrue(settings.db_path.is_absolute())
        self.assertTrue(settings.temp_dir.is_absolute())

    def test_media_and_silence_limits_are_bounded(self):
        with mock.patch.dict(
            os.environ,
            env(MEDIA_WORKER_CONCURRENCY="99", PRESENTATION_SILENCE_SECONDS="inf"),
            clear=False,
        ):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.media_worker_concurrency, 8)
        self.assertEqual(settings.presentation_silence_seconds, 0.25)

        with mock.patch.dict(
            os.environ, env(PRESENTATION_SILENCE_SECONDS="120"), clear=False
        ):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.presentation_silence_seconds, 60.0)

    def test_plan_overrides(self):
        values = {"PLAN_5_HOURS": "7", "PLAN_5_PRICE_TOMAN": "99000"}
        with mock.patch.dict(os.environ, env(**values), clear=False):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.plan_5_hours, 7)
        self.assertEqual(settings.plan_5_price_toman, 99000)
        self.assertEqual(settings.plan_values["plan_5_hours"], 7)

    def test_tariff_values_cover_every_plan_field(self):
        # Every plan field on the dataclass appears in tariff_values().
        with mock.patch.dict(os.environ, env(), clear=False):
            settings = from_env(env_file="/nonexistent")
        values = settings.tariff_values()
        for _name, attribute, _default in PLAN_ENV_FIELDS:
            self.assertIn(attribute, values)

    def test_note_mode_resolution(self):
        self.assertEqual(resolve_note_mode("summary"), "summary")
        self.assertEqual(resolve_note_mode("bogus"), "full")
        self.assertEqual(resolve_note_mode(None), "full")

    def test_raw_text_profile_resolution(self):
        self.assertEqual(resolve_raw_text_profile("timestamped"), "timestamped")
        self.assertEqual(resolve_raw_text_profile("bogus"), "plain")

    def test_payment_card_formatting(self):
        self.assertEqual(format_payment_card("5022291332906625"), "5022 2913 3290 6625")

    def test_card_digits_normalized(self):
        with mock.patch.dict(os.environ, env(PAYMENT_CARD_NUMBER="5022-2913 3290 6625"), clear=False):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.payment_card_number, "5022291332906625")

    def test_payment_card_has_no_baked_in_default(self):
        """A deployment that sets no PAYMENT_CARD_* gets no card at all."""
        with mock.patch.dict(os.environ, env(), clear=True):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.payment_card_number, "")
        self.assertEqual(settings.payment_card_holder, "")
        self.assertEqual(settings.payment_bank_name, "")

    def test_zero_queued_jobs_clamps_to_one_not_unbounded(self):
        """0 must not reach ``asyncio.Queue(maxsize=0)`` (which is unbounded)."""
        with mock.patch.dict(os.environ, env(MAX_QUEUED_JOBS="0"), clear=False):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.max_queued_jobs, 1)


def note_env(**values):
    """Telegram plus exactly the note-provider settings a test gives."""
    base = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_API_ID": "42",
        "TELEGRAM_API_HASH": "hash",
        "ADMIN_IDS": "111",
        "SPEECHMATICS_API_KEY": "sm",
    }
    base.update(values)
    return base


class NoteProviderOrderTests(unittest.TestCase):
    """``NOTE_PROVIDERS`` is the note failover policy, and it is testable."""

    def settings(self, **values):
        with mock.patch.dict(os.environ, note_env(**values), clear=True):
            return from_env(env_file="/nonexistent")

    @staticmethod
    def slugs(settings):
        return [route.slug for route in note_routes(settings)]

    def test_default_order_is_the_fast_preset(self):
        """Gemini flash-lite first, then Nara, OpenRouter, Groq and Cerebras."""
        settings = self.settings(
            GEMINI_API_KEY="g",
            NARA_API_KEY="nara",
            OPENROUTER_API_KEY="or",
            GROQ_API_KEY="groq",
            CEREBRAS_API_KEY="cerebras",
        )
        routes = note_routes(settings)
        self.assertEqual(
            self.slugs(settings),
            ["gemini", "nara", "openrouter", "groq", "cerebras"],
        )
        self.assertEqual(tuple(settings.note_providers), (
            "gemini", "nara", "openrouter", "groq", "cerebras",
        ))
        # Each provider keeps its own documented base URL and model.
        self.assertEqual(routes[0].base_url, "https://generativelanguage.googleapis.com/v1beta")
        self.assertEqual(routes[0].model, DEFAULT_GEMINI_MODEL)
        self.assertEqual(routes[1].base_url, "https://router.bynara.id/v1")
        self.assertEqual(routes[1].model, "agnes-2.5-flash")
        self.assertEqual(routes[2].base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(routes[2].model, "google/gemma-4-31b-it:free")
        self.assertEqual(routes[3].base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(routes[3].model, "openai/gpt-oss-120b")
        self.assertEqual(routes[4].base_url, "https://api.cerebras.ai/v1")
        self.assertEqual(routes[4].model, "gpt-oss-120b")
        self.assertTrue(settings.note_configured)
        # No *fatal* problems: a Speechmatics-only env legitimately earns
        # the capacity-coherence advisory (2 slots < 3 active jobs).
        self.assertEqual(
            [p for p in check(settings) if not is_advisory(p)], []
        )

    def test_the_default_chain_degrades_to_the_credentialed_legs(self):
        """A deployment with only the old keys keeps exactly its old legs."""
        settings = self.settings(NARA_API_KEY="nara", OPENROUTER_API_KEY="or")
        self.assertEqual(self.slugs(settings), ["nara", "openrouter"])
        self.assertFalse(settings.groq_configured)
        self.assertFalse(settings.cerebras_configured)

    def test_only_a_groq_key_adds_only_the_groq_leg(self):
        settings = self.settings(GROQ_API_KEY="gsk-test")
        self.assertEqual(self.slugs(settings), ["groq"])
        self.assertTrue(settings.groq_configured)

    def test_only_a_cerebras_key_adds_only_the_cerebras_leg(self):
        settings = self.settings(CEREBRAS_API_KEY="csk-test")
        self.assertEqual(self.slugs(settings), ["cerebras"])
        self.assertTrue(settings.cerebras_configured)

    def test_a_missing_key_is_not_a_configured_provider(self):
        settings = self.settings(NARA_API_KEY="nara")
        self.assertEqual(self.slugs(settings), ["nara"])
        self.assertFalse(settings.openrouter_configured)

    def test_the_configured_order_is_the_attempted_order(self):
        settings = self.settings(
            NOTE_PROVIDERS="openrouter, nara", NARA_API_KEY="n", OPENROUTER_API_KEY="o"
        )
        self.assertEqual(self.slugs(settings), ["openrouter", "nara"])

    def test_unknown_provider_slugs_are_ignored(self):
        settings = self.settings(NOTE_PROVIDERS="nara,bogus,openrouter")
        self.assertEqual(settings.note_providers, ("nara", "openrouter"))

    def test_a_legacy_nararouter_slot_is_the_nara_leg(self):
        """The production v2 deployment stays Nara-first, with no duplicate leg."""
        settings = self.settings(
            NOTE_API_KEY="legacy-key",
            NOTE_BASE_URL="https://router.bynara.id/v1",
            OPENROUTER_API_KEY="or",
        )
        self.assertEqual(settings.note_provider, "nara")
        routes = note_routes(settings)
        self.assertEqual([route.slug for route in routes], ["nara", "openrouter"])
        self.assertEqual(routes[0].api_key, "legacy-key")
        # The model default follows the classified provider, not the generic one.
        self.assertEqual(routes[0].model, "agnes-2.5-flash")

    def test_bearer_prefix_and_endpoint_suffix_are_normalized(self):
        settings = self.settings(
            NARA_API_KEY="Bearer sk-nry-test",
            NARA_BASE_URL="https://router.bynara.web.id/v1/chat/completions",
            OPENROUTER_API_KEY="bearer sk-or-v1-test",
            OPENROUTER_BASE_URL="https://openrouter.ai",
            GEMINI_API_KEY="Bearer AIzaSyTestKey1234567890",
            NOTE_PROVIDERS="nara,openrouter,gemini",
        )
        routes = note_routes(settings)
        self.assertEqual([r.slug for r in routes], ["nara", "openrouter", "gemini"])
        self.assertEqual(routes[0].api_key, "sk-nry-test")
        self.assertEqual(routes[0].base_url, "https://router.bynara.web.id/v1")
        self.assertEqual(routes[1].api_key, "sk-or-v1-test")
        self.assertEqual(routes[1].base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(routes[2].api_key, "AIzaSyTestKey1234567890")
        self.assertEqual(routes[2].base_url, "https://generativelanguage.googleapis.com/v1beta")

    def test_google_ai_studio_native_vs_openai_compat_classification(self):
        native = self.settings(
            NOTE_API_KEY="g-key",
            NOTE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/models",
        )
        self.assertEqual(native.note_provider, "gemini")
        self.assertEqual(native.note_api_base_url, "https://generativelanguage.googleapis.com/v1beta")

        compat = self.settings(
            NOTE_API_KEY="g-key",
            NOTE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai",
        )
        self.assertEqual(compat.note_provider, "openai_compatible")
        self.assertEqual(
            compat.note_api_base_url,
            "https://generativelanguage.googleapis.com/v1beta/openai",
        )

    def test_an_explicit_gateway_is_tried_after_the_chain(self):
        settings = self.settings(
            NARA_API_KEY="n",
            NOTE_PROVIDER="openai_compatible",
            NOTE_API_KEY="k",
            NOTE_BASE_URL="https://local.example/v1",
        )
        self.assertEqual(self.slugs(settings), ["nara", "openai_compatible"])

    def test_one_credential_never_serves_two_providers(self):
        settings = self.settings(NOTE_PROVIDERS="nara,openrouter,gemini", NARA_API_KEY="n")
        self.assertEqual(self.slugs(settings), ["nara"])

    def test_gemini_can_be_a_fallback_with_its_own_key(self):
        settings = self.settings(
            NOTE_PROVIDERS="nara,gemini", NARA_API_KEY="n", GEMINI_API_KEY="g"
        )
        routes = note_routes(settings)
        self.assertEqual(self.slugs(settings), ["nara", "gemini"])
        self.assertEqual(routes[1].api_key, "g")
        self.assertEqual(routes[1].model, DEFAULT_GEMINI_MODEL)

    def test_a_dedicated_key_does_not_auto_select_gemini_outside_the_chain(self):
        """Only a listed slug is attempted: an explicit chain stays explicit."""
        settings = self.settings(
            NOTE_PROVIDERS="nara", GEMINI_API_KEY="g", NARA_API_KEY="n"
        )
        self.assertEqual(self.slugs(settings), ["nara"])

    def test_default_window_chunk_and_models_are_the_matched_pair(self):
        """The default sizing assumes the matched 262K served windows.

        Both default models serve 262,144 tokens; the operational assumption
        is half of that, and the output budget stays within both models'
        documented completion caps (32,768 OpenRouter / 65.5K Agnes).
        """
        settings = self.settings(NARA_API_KEY="nara", OPENROUTER_API_KEY="or")
        self.assertEqual(settings.openrouter_model, DEFAULT_OPENROUTER_MODEL)
        self.assertEqual(settings.openrouter_model, "google/gemma-4-31b-it:free")
        self.assertEqual(settings.note_context_tokens, DEFAULT_NOTE_CONTEXT_TOKENS)
        self.assertEqual(settings.note_context_tokens, 131072)
        self.assertEqual(settings.note_chunk_chars, DEFAULT_NOTE_CHUNK_CHARS)
        self.assertEqual(settings.note_chunk_chars, 64000)
        self.assertEqual(settings.note_max_output_tokens, 16384)
        self.assertLess(settings.note_max_output_tokens, 32768)
        # Automatic compile budget and provider-default reasoning out of the box.
        self.assertEqual(settings.note_compile_max_chars, 0)
        self.assertEqual(settings.note_reasoning_effort, "")

    def test_compile_cap_and_reasoning_effort_from_env(self):
        settings = self.settings(
            NARA_API_KEY="n", NOTE_COMPILE_MAX_CHARS="90000", NOTE_REASONING_EFFORT="low"
        )
        self.assertEqual(settings.note_compile_max_chars, 90000)
        self.assertEqual(settings.note_reasoning_effort, "low")

    def test_the_request_deadline_is_configurable(self):
        settings = self.settings(NARA_API_KEY="n")
        self.assertEqual(settings.note_request_timeout, 900.0)
        self.assertEqual(settings.note_request_timeout, DEFAULT_NOTE_REQUEST_TIMEOUT_SECONDS)
        settings = self.settings(NARA_API_KEY="n", NOTE_REQUEST_TIMEOUT_SECONDS="600")
        self.assertEqual(settings.note_request_timeout, 600.0)
        # The deadline is bounded: a note request never runs for hours.
        settings = self.settings(NARA_API_KEY="n", NOTE_REQUEST_TIMEOUT_SECONDS="99999")
        self.assertEqual(settings.note_request_timeout, 3600.0)

    def test_an_output_budget_that_cannot_finish_in_time_is_advisory(self):
        """The context window decides whether a request fits, the deadline
        whether it finishes: a 16K output budget cannot be produced inside a
        300 s deadline on a slow gateway, and that is worth warning about."""
        settings = self.settings(
            NARA_API_KEY="n",
            NOTE_MAX_OUTPUT_TOKENS="16384",
            NOTE_REQUEST_TIMEOUT_SECONDS="300",
        )
        problems = check(settings)
        self.assertTrue(
            any(
                is_advisory(problem)
                and "will time out on a slow gateway" in problem
                for problem in problems
            ),
            problems,
        )

    def test_a_generous_deadline_is_not_reported(self):
        settings = self.settings(
            NARA_API_KEY="n",
            NOTE_MAX_OUTPUT_TOKENS="16384",
            NOTE_REQUEST_TIMEOUT_SECONDS="900",
        )
        problems = check(settings)
        self.assertFalse(
            any("will time out on a slow gateway" in problem for problem in problems),
            problems,
        )

    def test_reasoning_effort_resolution(self):
        self.assertEqual(resolve_note_reasoning_effort("LOW"), "low")
        self.assertEqual(resolve_note_reasoning_effort(" ultra "), "max")
        self.assertEqual(resolve_note_reasoning_effort("xhigh"), "xhigh")
        # Unknown levels fall back to "provider default" instead of being sent.
        self.assertEqual(resolve_note_reasoning_effort("bogus"), "")
        self.assertEqual(resolve_note_reasoning_effort(""), "")
        self.assertEqual(resolve_note_reasoning_effort(None), "")

    def test_a_tight_context_window_is_reported_as_advisory(self):
        settings = self.settings(
            NARA_API_KEY="n", NOTE_CONTEXT_TOKENS="8000", NOTE_MAX_OUTPUT_TOKENS="6000"
        )
        problems = check(settings)
        self.assertTrue(
            any(is_advisory(problem) and "no room for the transcript" in problem
                for problem in problems),
            problems,
        )

    def test_prompt_reserve_is_included_in_the_note_window_check(self):
        settings = self.settings(
            NARA_API_KEY="n", NOTE_CONTEXT_TOKENS="8192", NOTE_MAX_OUTPUT_TOKENS="4096"
        )
        problems = check(settings)
        self.assertTrue(
            any(is_advisory(problem) and "no room for the transcript" in problem
                for problem in problems),
            problems,
        )

    def test_a_credential_without_a_model_is_reported(self):
        settings = self.settings(
            NOTE_PROVIDER="anthropic", NOTE_API_KEY="k", NOTE_MODEL=""
        )
        problems = check(settings)
        self.assertTrue(any("NOTE_MODEL" in problem for problem in problems), problems)
        self.assertFalse(settings.note_configured)


class GroqAndCerebrasRouteTests(unittest.TestCase):
    """First-class ``groq``/``cerebras`` slugs, wired through every registry."""

    def settings(self, **values):
        with mock.patch.dict(os.environ, note_env(**values), clear=True):
            return from_env(env_file="/nonexistent")

    def test_new_slugs_are_in_every_registry_structure(self):
        for slug, label in (("groq", "Groq"), ("cerebras", "Cerebras")):
            self.assertIn(slug, NOTE_PROVIDER_SLUGS)
            self.assertEqual(NOTE_REQUEST_STYLE[slug], "openai")
            self.assertEqual(NOTE_PROVIDER_LABELS[slug], label)
            self.assertIn(slug, NOTE_JSON_OBJECT_PROVIDERS)
            self.assertIn(slug, DEFAULT_NOTE_MODELS)
            self.assertIn(slug, NOTE_BASE_URL_DEFAULTS)
        hosts = dict(NOTE_HOST_PROVIDERS)
        self.assertEqual(hosts["groq.com"], "groq")
        self.assertEqual(hosts["cerebras.ai"], "cerebras")
        self.assertEqual(DEFAULT_GROQ_BASE_URL, "https://api.groq.com/openai/v1")
        self.assertEqual(DEFAULT_GROQ_MODEL, "openai/gpt-oss-120b")
        self.assertEqual(DEFAULT_CEREBRAS_BASE_URL, "https://api.cerebras.ai/v1")
        self.assertEqual(DEFAULT_CEREBRAS_MODEL, "gpt-oss-120b")

    def test_from_env_parses_the_new_provider_and_gemini_variables(self):
        settings = self.settings(
            GROQ_API_KEY="gsk-test",
            GROQ_BASE_URL="https://api.groq.com",
            GROQ_MODEL="openai/gpt-oss-20b",
            CEREBRAS_API_KEY="csk-test",
            CEREBRAS_BASE_URL="https://api.cerebras.ai",
            CEREBRAS_MODEL="llama3.1-8b",
            GEMINI_MODEL="gemini-2.5-flash",
            NOTE_GEMINI_THINKING_BUDGET="24576",
        )
        self.assertEqual(settings.groq_api_key, "gsk-test")
        self.assertEqual(settings.groq_base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(settings.groq_model, "openai/gpt-oss-20b")
        self.assertEqual(settings.cerebras_api_key, "csk-test")
        self.assertEqual(settings.cerebras_base_url, "https://api.cerebras.ai/v1")
        self.assertEqual(settings.cerebras_model, "llama3.1-8b")
        self.assertEqual(settings.gemini_model, "gemini-2.5-flash")
        self.assertEqual(settings.gemini_thinking_budget, 24576)
        self.assertTrue(settings.groq_configured)
        self.assertTrue(settings.cerebras_configured)

    def test_gemini_defaults_are_flash_lite_with_zero_thinking(self):
        settings = self.settings()
        self.assertEqual(settings.gemini_model, "gemini-2.5-flash-lite")
        self.assertEqual(settings.gemini_thinking_budget, 0)
        self.assertEqual(DEFAULT_GEMINI_THINKING_BUDGET, 0)

    def test_thinking_budget_accepts_minus_one_and_is_bounded(self):
        self.assertEqual(
            self.settings(NOTE_GEMINI_THINKING_BUDGET="-1").gemini_thinking_budget, -1
        )
        self.assertEqual(
            self.settings(NOTE_GEMINI_THINKING_BUDGET="-5").gemini_thinking_budget, -1
        )
        self.assertEqual(
            self.settings(NOTE_GEMINI_THINKING_BUDGET="999999").gemini_thinking_budget,
            32768,
        )

    def test_bare_groq_and_cerebras_origins_are_healed(self):
        self.assertEqual(
            normalize_note_base_url("https://api.groq.com", "groq"),
            "https://api.groq.com/openai/v1",
        )
        self.assertEqual(
            normalize_note_base_url("https://api.cerebras.ai", "cerebras"),
            "https://api.cerebras.ai/v1",
        )
        # The host is enough: an unlabelled origin heals by host, not by slug.
        self.assertEqual(
            normalize_note_base_url("https://api.groq.com"), "https://api.groq.com/openai/v1"
        )
        self.assertEqual(
            normalize_note_base_url("https://api.cerebras.ai/"), "https://api.cerebras.ai/v1"
        )
        # An explicit path is never overwritten.
        self.assertEqual(
            normalize_note_base_url("https://api.groq.com/openai/v1/chat/completions", "groq"),
            "https://api.groq.com/openai/v1",
        )

    def test_slot_credentials_are_per_provider_and_gemini_uses_its_own_model(self):
        settings = self.settings(
            GEMINI_API_KEY="g",
            GROQ_API_KEY="gsk",
            CEREBRAS_API_KEY="csk",
            GEMINI_MODEL="gemini-2.5-flash-lite",
        )
        self.assertEqual(
            note_slot_credentials(settings, "groq"),
            ("gsk", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
        )
        self.assertEqual(
            note_slot_credentials(settings, "cerebras"),
            ("csk", "https://api.cerebras.ai/v1", "gpt-oss-120b"),
        )
        self.assertEqual(
            note_slot_credentials(settings, "gemini")[0],
            "g",
        )
        self.assertEqual(note_slot_credentials(settings, "gemini")[2], settings.gemini_model)

    def test_a_legacy_slot_on_a_groq_host_is_the_groq_leg(self):
        """A ``NOTE_BASE_URL`` at api.groq.com is Groq, never a duplicate leg."""
        settings = self.settings(
            NOTE_PROVIDER="openai_compatible",
            NOTE_API_KEY="gsk-legacy",
            NOTE_BASE_URL="https://api.groq.com",
        )
        self.assertEqual(settings.note_provider, classify_note_provider(
            "openai_compatible", "https://api.groq.com/openai/v1"
        ))
        self.assertEqual(settings.note_provider, "groq")
        routes = note_routes(settings)
        self.assertEqual([route.slug for route in routes], ["groq"])
        self.assertEqual(routes[0].api_key, "gsk-legacy")
        self.assertEqual(routes[0].base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(routes[0].model, "openai/gpt-oss-120b")

    def test_note_not_configured_message_names_the_new_keys(self):
        settings = self.settings()
        problems = [p for p in check(settings) if "raw transcripts only" in p]
        self.assertTrue(problems, check(settings))
        for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY"):
            self.assertIn(name, problems[0])


class CheckTests(unittest.TestCase):
    def make(self, **overrides):
        values = env(**overrides)
        with mock.patch.dict(os.environ, values, clear=False):
            return from_env(env_file="/nonexistent")

    def test_missing_token_reported(self):
        settings = self.make(TELEGRAM_BOT_TOKEN="")
        problems = check(settings)
        self.assertTrue(any("TELEGRAM_BOT_TOKEN" in p for p in problems))

    def test_missing_stt_reported(self):
        settings = self.make(
            SPEECHMATICS_API_KEY="", DEEPGRAM_API_KEY=None, STT_OPENAI_BASE_URL=""
        )
        problems = check(settings)
        self.assertTrue(any("speech-to-text" in p for p in problems))

    def test_unknown_stt_provider_list_is_reported_as_such(self):
        """A typo that empties STT_PROVIDERS must not read as 'no key set'."""
        settings = self.make(STT_PROVIDERS="bogus")
        problems = check(settings)
        named = [p for p in problems if "STT_PROVIDERS" in p]
        self.assertEqual(len(named), 1, problems)
        self.assertIn("deepgram", named[0])
        self.assertFalse(
            any("No speech-to-text provider is configured" in p for p in problems),
            problems,
        )

    def test_note_not_configured_is_a_warning_only(self):
        settings = self.make(NOTE_API_KEY="")
        problems = check(settings)
        self.assertTrue(any("raw transcripts only" in p for p in problems))

    def test_anthropic_requires_explicit_model(self):
        settings = self.make(NOTE_PROVIDER="anthropic", NOTE_MODEL="")
        self.assertTrue(any("NOTE_MODEL" in problem for problem in check(settings)))

    def test_no_admins_reported(self):
        settings = self.make(ADMIN_IDS="")
        problems = check(settings)
        self.assertTrue(any("ADMIN_IDS" in p for p in problems))

    def test_persian_with_a_non_persian_deepgram_model_is_reported(self):
        """nova-2 + language=fa is rejected by Deepgram: catch it at startup."""
        settings = self.make(
            STT_PROVIDERS="deepgram",
            SPEECHMATICS_API_KEY="",
            DEEPGRAM_API_KEY="dg",
            DEEPGRAM_MODEL="nova-2",
        )
        problems = check(settings)
        persian = [p for p in problems if "Persian" in p]
        self.assertEqual(len(persian), 1, problems)
        self.assertIn("nova-3", persian[0])
        # Advisory: another provider may still transcribe, so the bot runs.
        self.assertTrue(is_advisory(persian[0]))

    def test_persian_with_nova_3_is_not_reported(self):
        settings = self.make(
            STT_PROVIDERS="deepgram",
            SPEECHMATICS_API_KEY="",
            DEEPGRAM_API_KEY="dg",
            DEEPGRAM_MODEL="nova-3",
        )
        self.assertFalse(any("Persian" in p for p in check(settings)))

    def test_non_persian_language_does_not_flag_the_deepgram_model(self):
        settings = self.make(
            STT_PROVIDERS="deepgram",
            SPEECHMATICS_API_KEY="",
            DEEPGRAM_API_KEY="dg",
            DEEPGRAM_MODEL="nova-2",
            STT_LANGUAGE="en",
        )
        self.assertFalse(any("Persian" in p for p in check(settings)))

    def test_inactive_deepgram_model_is_not_reported(self):
        """Only the configured failover order counts, like stt_configured."""
        settings = self.make(
            STT_PROVIDERS="speechmatics",
            DEEPGRAM_API_KEY="dg",
            DEEPGRAM_MODEL="nova-2",
        )
        # The slow-order advisory *is* expected here (speechmatics is first);
        # what must not be reported is a problem with the inactive model.
        self.assertFalse(any("does not support Persian" in p for p in check(settings)))

    def test_speechmatics_primary_deepgram_overflow_is_valid_without_warning(self):
        """Speechmatics primary + Deepgram overflow is the shipped policy."""
        settings = self.make(
            STT_PROVIDERS="speechmatics,deepgram",
            SPEECHMATICS_API_KEY="sm",
            DEEPGRAM_API_KEY="dg",
            SPEECHMATICS_MODEL="enhanced",
        )
        self.assertEqual(check(settings), [])

    def test_stage_and_provider_concurrency_bounds_clamp_safely(self):
        with mock.patch.dict(
            os.environ,
            env(
                SPEECHMATICS_MAX_IN_FLIGHT="0",
                DEEPGRAM_MAX_IN_FLIGHT="99",
                MAX_CONCURRENT_DOWNLOADS="0",
                MAX_CONCURRENT_UPLOADS="99",
                LLM_PRIMARY_MAX_IN_FLIGHT="0",
                STT_PROVIDER_TIMEOUT_SECONDS="1",
            ),
            clear=False,
        ):
            clamped = from_env(env_file="/nonexistent")
        self.assertEqual(clamped.speechmatics_max_in_flight, 1)
        self.assertEqual(clamped.deepgram_max_in_flight, 32)
        self.assertEqual(clamped.max_concurrent_downloads, 1)
        self.assertEqual(clamped.max_concurrent_uploads, 8)
        self.assertEqual(clamped.llm_primary_max_in_flight, 1)
        self.assertEqual(clamped.stt_provider_timeout, 5.0)


class ShippedDefaultTests(unittest.TestCase):
    """The defaults an operator inherits when nothing is set."""

    def setUp(self):
        with mock.patch.dict(os.environ, env(), clear=False):
            self.settings = from_env(env_file="/nonexistent")

    def test_speechmatics_leads_with_deepgram_overflow(self):
        """Speechmatics is primary; Deepgram is non-blocking overflow/backup."""
        self.assertEqual(
            self.settings.stt_providers,
            ("speechmatics", "deepgram", "openai_compatible"),
        )
        self.assertEqual(first_configured_stt_provider(self.settings), "speechmatics")
        self.assertTrue(self.settings.stt_fallback_enabled)
        self.assertEqual(self.settings.speechmatics_max_in_flight, 2)
        self.assertEqual(self.settings.deepgram_max_in_flight, 4)
        self.assertEqual(self.settings.max_concurrent_downloads, 2)
        self.assertEqual(self.settings.max_concurrent_uploads, 2)
        self.assertEqual(self.settings.llm_primary_max_in_flight, 4)
        self.assertEqual(self.settings.llm_fallback_max_in_flight, 4)

    def test_note_performance_defaults(self):
        self.assertEqual(self.settings.note_ttfb_timeout, 25.0)
        self.assertEqual(self.settings.note_hedge_after, 60.0)
        self.assertEqual(self.settings.note_concurrency, 3)
        self.assertTrue(self.settings.note_structured_output)
        self.assertTrue(self.settings.note_progressive_booklet)
        self.assertTrue(self.settings.note_openrouter_throughput)

    def test_zero_still_turns_the_guards_off(self):
        """An explicit ``0`` is how an operator opts back into plain waiting."""
        with mock.patch.dict(
            os.environ,
            env(
                NOTE_TTFB_TIMEOUT_SECONDS="0",
                NOTE_HEDGE_AFTER_SECONDS="0",
                NOTE_STRUCTURED_OUTPUT="false",
                NOTE_PROGRESSIVE_BOOKLET="0",
                NOTE_OPENROUTER_THROUGHPUT="0",
                NOTE_CONCURRENCY="2",
            ),
            clear=False,
        ):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.note_ttfb_timeout, 0.0)
        self.assertEqual(settings.note_hedge_after, 0.0)
        self.assertEqual(settings.note_concurrency, 2)
        self.assertFalse(settings.note_structured_output)
        self.assertFalse(settings.note_progressive_booklet)
        self.assertFalse(settings.note_openrouter_throughput)

    def test_first_configured_provider_skips_keyless_slugs(self):
        """A listed-but-unconfigured provider never serves a job."""
        with mock.patch.dict(
            os.environ, env(STT_PROVIDERS="deepgram,speechmatics", SPEECHMATICS_API_KEY="sm"),
            clear=False,
        ):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(first_configured_stt_provider(settings), "speechmatics")


class SettingsSurfaceTests(unittest.TestCase):
    def test_frozen(self):
        import dataclasses

        with mock.patch.dict(os.environ, env(), clear=False):
            settings = from_env(env_file="/nonexistent")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.bot_token = "x"  # type: ignore[misc]

    def test_openai_compatible_needs_either_key_or_url(self):
        base = Settings.__dataclass_fields__
        self.assertIn("note_provider", base)
        self.assertIn("raw_text_profile", base)
        self.assertIn("note_concurrency", base)


if __name__ == "__main__":
    unittest.main()
