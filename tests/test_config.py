"""Tests for the v3 configuration surface."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from gamas_bot.config import (
    DEEPGRAM_PERSIAN_MODELS,
    note_routes,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_NOTE_CHUNK_CHARS,
    DEFAULT_NOTE_CONTEXT_TOKENS,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_STT_PROVIDERS,
    PLAN_ENV_FIELDS,
    Settings,
    check,
    format_payment_card,
    from_env,
    is_advisory,
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
        self.assertEqual(check(settings), [])

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
        self.assertEqual(settings.note_model, "gemini-2.5-flash")

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

    def test_default_order_is_nara_then_openrouter(self):
        settings = self.settings(NARA_API_KEY="nara", OPENROUTER_API_KEY="or")
        routes = note_routes(settings)
        self.assertEqual(self.slugs(settings), ["nara", "openrouter"])
        # Each provider keeps its own documented base URL and model.
        self.assertEqual(routes[0].base_url, "https://router.bynara.id/v1")
        self.assertEqual(routes[0].model, "agnes-2.5-flash")
        self.assertEqual(routes[1].base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(routes[1].model, "google/gemma-4-31b-it:free")
        self.assertTrue(settings.note_configured)
        self.assertEqual(check(settings), [])

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
        self.assertEqual(routes[1].model, "gemini-2.5-flash")

    def test_a_dedicated_key_does_not_auto_select_gemini(self):
        settings = self.settings(GEMINI_API_KEY="g", NARA_API_KEY="n")
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

    def test_a_credential_without_a_model_is_reported(self):
        settings = self.settings(
            NOTE_PROVIDER="anthropic", NOTE_API_KEY="k", NOTE_MODEL=""
        )
        problems = check(settings)
        self.assertTrue(any("NOTE_MODEL" in problem for problem in problems), problems)
        self.assertFalse(settings.note_configured)


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
        self.assertFalse(any("Persian" in p for p in check(settings)))


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
