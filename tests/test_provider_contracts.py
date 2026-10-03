"""Provider contract tests for the STT and note-generation integrations.

Everything here runs offline. The point of the suite is the *serialized
contract*: the exact JSON/query/headers each provider receives, the language
semantics each engine can actually honour, the bounded retry policy, and how
malformed provider answers are reported. Those are the things a mocked test
suite can verify without credentials — and the things that silently break a
deployment when they drift.

Provider facts are pinned against the official documentation:

* Speechmatics Batch input/models/languages:
  https://docs.speechmatics.com/speech-to-text/batch/input
  https://docs.speechmatics.com/speech-to-text/models
  https://docs.speechmatics.com/speech-to-text/batch/language-identification
  custom dictionary: https://docs.speechmatics.com/speech-to-text/features/custom-dictionary
* Deepgram models & languages: https://developers.deepgram.com/docs/models-languages-overview
  language parameter: https://developers.deepgram.com/docs/language
  language detection: https://developers.deepgram.com/docs/language-detection
* Anthropic models/deprecations: https://platform.claude.com/docs/en/about-claude/model-deprecations
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from gamas_bot.config import (
    SPEECHMATICS_VOCAB_HARD_LIMIT,
    SPEECHMATICS_VOCAB_RECOMMENDED_LIMIT,
    Settings,
)
from gamas_bot.stt import (
    STTConfigurationError,
    STTError,
    STTTransientError,
    Transcript,
    _openai_transcript,
    _speechmatics_text,
    deepgram_params,
    normalize_language_for_provider,
    speechmatics_config,
    speechmatics_vocab,
    transcribe,
)
from gamas_bot.structuring import (
    StructuringError,
    _provider_request,
    _provider_response,
    anthropic_sampling_params,
    anthropic_supports_temperature,
)

from support import make_settings


class _FakeResponse:
    """Minimal aiohttp response double (status, headers, JSON body)."""

    def __init__(self, status: int, payload=None, headers=None):
        self.status = status
        self.headers = headers or {}
        self._payload = payload
        self.calls = 0

    async def read(self):
        return json.dumps(self._payload).encode("utf-8")

    async def json(self, content_type=None):
        self.calls += 1
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ScriptedSession:
    """Session double that replays queued responses and records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, params=None, json=None, data=None):
        self.requests.append({"url": url, "headers": headers, "params": params, "data": data})
        return self.responses.pop(0)

    def get(self, url, headers=None, params=None):
        self.requests.append({"url": url, "headers": headers, "params": params})
        return self.responses.pop(0)


def _deepgram_payload(text: str = "سلام", confidence: float = 0.9) -> dict:
    return {
        "results": {
            "channels": [{"alternatives": [{"transcript": text, "confidence": confidence}]}]
        }
    }


class SpeechmaticsRequestContractTests(unittest.TestCase):
    """The serialized job config must match the documented Batch schema."""

    def test_selection_is_serialized_as_the_documented_model_field(self):
        config = speechmatics_config(make_settings())
        transcription = config["transcription_config"]
        self.assertEqual(config["type"], "transcription")
        self.assertEqual(transcription["model"], "enhanced")
        self.assertEqual(transcription["language"], "fa")
        # ``operating_point`` is the deprecated alias: it must not be sent
        # unless a deployment explicitly asks for the legacy spelling.
        self.assertNotIn("operating_point", transcription)
        self.assertNotIn("additional_vocab", transcription)

    def test_legacy_operating_point_field_is_opt_in_only(self):
        legacy = make_settings(speechmatics_model_field="operating_point")
        transcription = speechmatics_config(legacy)["transcription_config"]
        self.assertEqual(transcription["operating_point"], "enhanced")
        self.assertNotIn("model", transcription)

        both = make_settings(speechmatics_model_field="both")
        transcription = speechmatics_config(both)["transcription_config"]
        self.assertEqual(transcription["model"], "enhanced")
        self.assertEqual(transcription["operating_point"], "enhanced")

    def test_invalid_field_selection_is_rejected_at_configuration_time(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
                "SPEECHMATICS_API_KEY=k\nSPEECHMATICS_MODEL_FIELD=engine\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env(env)

    def test_operating_point_accepts_every_documented_batch_model(self):
        for value in ("standard", "enhanced", "melia-1", "oak-1"):
            with self.subTest(value=value):
                settings = make_settings(speechmatics_operating_point=value)
                self.assertEqual(
                    speechmatics_config(settings)["transcription_config"]["model"], value
                )

    def test_unknown_operating_point_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
                "SPEECHMATICS_API_KEY=k\nSPEECHMATICS_OPERATING_POINT=ultra\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env(env)

    def test_new_variable_wins_and_the_legacy_name_still_works(self):
        body = (
            "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
            "SPEECHMATICS_API_KEY=k\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"

            def load(extra: str) -> Settings:
                env.write_text(body + extra, encoding="utf-8")
                with patch.dict("os.environ", {}, clear=True):
                    return Settings.from_env(env)

            self.assertEqual(load("").speechmatics_operating_point, "enhanced")
            self.assertEqual(
                load("SPEECHMATICS_MODEL=standard\n").speechmatics_operating_point, "standard"
            )
            self.assertEqual(
                load("SPEECHMATICS_OPERATING_POINT=melia-1\n").speechmatics_operating_point,
                "melia-1",
            )
            # The new name takes precedence, so a stale legacy value cannot
            # silently win after an operator upgrades the .env.
            self.assertEqual(
                load(
                    "SPEECHMATICS_MODEL=standard\nSPEECHMATICS_OPERATING_POINT=enhanced\n"
                ).speechmatics_operating_point,
                "enhanced",
            )
            # The legacy attribute name keeps working for older call sites.
            self.assertEqual(load("SPEECHMATICS_MODEL=standard\n").speechmatics_model, "standard")

    def test_custom_dictionary_is_sent_as_native_additional_vocab(self):
        settings = make_settings(speechmatics_additional_vocab=("Metformin", "HbA1c"))
        self.assertEqual(
            speechmatics_config(settings)["transcription_config"]["additional_vocab"],
            [{"content": "Metformin"}, {"content": "HbA1c"}],
        )

    def test_multilingual_models_do_not_receive_a_custom_dictionary(self):
        # melia-1/oak-1 do not support additional_vocab, so sending one would
        # make the job invalid.
        settings = make_settings(
            speechmatics_operating_point="melia-1",
            speechmatics_additional_vocab=("Metformin",),
            stt_language="multi",
        )
        self.assertFalse(settings.speechmatics_vocab_supported)
        self.assertEqual(speechmatics_vocab(settings), [])
        self.assertNotIn(
            "additional_vocab", speechmatics_config(settings)["transcription_config"]
        )


class SpeechmaticsVocabularyLimitTests(unittest.TestCase):
    """``additional_vocab`` must never exceed the provider's documented size."""

    @staticmethod
    def _vocab(count: int) -> tuple[str, ...]:
        return tuple(f"term{index}" for index in range(count))

    def _sent(self, count: int, **overrides) -> list[dict]:
        settings = make_settings(
            speechmatics_additional_vocab=self._vocab(count), **overrides
        )
        return speechmatics_vocab(settings)

    def test_boundaries_zero_and_one(self):
        self.assertEqual(self._sent(0), [])
        self.assertEqual(self._sent(1), [{"content": "term0"}])

    def test_999_and_1000_are_sent_in_full(self):
        self.assertEqual(len(self._sent(999)), 999)
        self.assertEqual(len(self._sent(1000)), 1000)

    def test_1001_is_truncated_to_the_configured_limit(self):
        with self.assertLogs("gamas_bot.stt", level="WARNING"):
            sent = self._sent(1001)
        self.assertEqual(len(sent), SPEECHMATICS_VOCAB_RECOMMENDED_LIMIT)
        self.assertEqual(sent[0], {"content": "term0"})
        self.assertEqual(sent[-1], {"content": "term999"})
        self.assertNotIn({"content": "term1000"}, sent)

    def test_truncation_keeps_the_highest_priority_terms(self):
        # The configured order *is* the priority order: medical terms first.
        terms = ("Metformin", "HbA1c", "SpO2", *(f"filler{index}" for index in range(2000)))
        settings = make_settings(speechmatics_additional_vocab=terms)
        with self.assertLogs("gamas_bot.stt", level="WARNING"):
            sent = speechmatics_vocab(settings)
        self.assertEqual(len(sent), 1000)
        self.assertEqual(
            [entry["content"] for entry in sent[:3]], ["Metformin", "HbA1c", "SpO2"]
        )

    def test_limit_is_configurable_and_still_bounded(self):
        settings = make_settings(
            speechmatics_additional_vocab=self._vocab(1500),
            speechmatics_vocab_max_items=1200,
        )
        self.assertEqual(len(speechmatics_vocab(settings)), 1200)

    def test_dictionary_above_the_provider_hard_cap_is_a_configuration_error(self):
        # SaaS on Cloud rejects the job, so the deployment must fail fast
        # instead of discovering it at upload time.
        terms = ",".join(f"term{index}" for index in range(SPEECHMATICS_VOCAB_HARD_LIMIT + 1))
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
                f"SPEECHMATICS_API_KEY=k\nSPEECHMATICS_ADDITIONAL_VOCAB={terms}\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env(env)

    def test_vocab_max_items_cannot_exceed_the_hard_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
                "SPEECHMATICS_API_KEY=k\nSPEECHMATICS_VOCAB_MAX_ITEMS=99999\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env(env)


class ProviderLanguageTests(unittest.TestCase):
    """``STT_LANGUAGE`` is a request, not a provider parameter."""

    def test_speechmatics_persian_and_locales_pass_through(self):
        self.assertEqual(normalize_language_for_provider("speechmatics", "fa"), "fa")
        self.assertEqual(
            normalize_language_for_provider("speechmatics", "en-US", model="enhanced"), "en-US"
        )

    def test_speechmatics_auto_is_language_identification(self):
        self.assertEqual(
            normalize_language_for_provider("speechmatics", "auto", model="enhanced"), "auto"
        )

    def test_speechmatics_multilingual_models_reject_auto_and_require_multi(self):
        for model in ("melia-1", "oak-1"):
            with self.subTest(model=model):
                with self.assertRaises(STTConfigurationError):
                    normalize_language_for_provider("speechmatics", "auto", model=model)
                self.assertEqual(
                    normalize_language_for_provider("speechmatics", "multi", model=model),
                    "multi",
                )

    def test_multi_is_rejected_for_single_language_speechmatics_models(self):
        for model in ("enhanced", "standard"):
            with self.subTest(model=model):
                with self.assertRaises(STTConfigurationError):
                    normalize_language_for_provider("speechmatics", "multi", model=model)

    def test_bilingual_packs_are_speechmatics_only(self):
        # A pack identifies two Speechmatics language models at once; no other
        # provider has that concept, so forwarding the string there would only
        # produce a guaranteed 400 (and a fallback instead of a clear error).
        self.assertEqual(
            normalize_language_for_provider("speechmatics", "ar_en", model="enhanced"), "ar_en"
        )
        self.assertEqual(
            normalize_language_for_provider("speechmatics", "cmn_en_ms_ta", model="standard"),
            "cmn_en_ms_ta",
        )
        for model in ("melia-1", "oak-1"):
            with self.subTest(model=model):
                with self.assertRaises(STTConfigurationError):
                    normalize_language_for_provider("speechmatics", "ar_en", model=model)
        for provider in ("deepgram", "openai_compatible"):
            with self.subTest(provider=provider):
                with self.assertRaises(STTConfigurationError):
                    normalize_language_for_provider(provider, "ar_en")

    def test_bilingual_pack_codes_are_accepted(self):
        from gamas_bot.config import _is_supported_stt_language

        self.assertTrue(_is_supported_stt_language("ar_en"))
        self.assertTrue(_is_supported_stt_language("cmn_en_ms_ta"))
        self.assertTrue(_is_supported_stt_language("auto"))
        self.assertTrue(_is_supported_stt_language("multi"))
        self.assertFalse(_is_supported_stt_language("not a language"))
        self.assertFalse(_is_supported_stt_language(""))

    def test_deepgram_persian_and_english(self):
        settings = make_settings(stt_language="fa")
        self.assertEqual(deepgram_params(settings)["language"], "fa")
        self.assertEqual(deepgram_params(settings)["model"], "nova-3")
        self.assertEqual(deepgram_params(make_settings(stt_language="en"))["language"], "en")

    def test_deepgram_has_no_auto_language(self):
        # Deepgram's `detect_language` flag does not support Persian, so `auto`
        # is refused instead of silently mis-detecting a Persian lecture.
        with self.assertRaises(STTConfigurationError):
            deepgram_params(make_settings(stt_language="auto"))

    def test_deepgram_multi_is_accepted_for_multilingual_models(self):
        with self.assertLogs("gamas_bot.stt", level="WARNING") as logs:
            params = deepgram_params(make_settings(stt_language="multi"))
        self.assertEqual(params["language"], "multi")
        joined = "\n".join(logs.output)
        self.assertIn("Persian", joined)

    def test_deepgram_multi_is_rejected_for_a_model_without_a_multilingual_mode(self):
        with self.assertRaises(STTConfigurationError):
            deepgram_params(make_settings(stt_language="multi", deepgram_model="nova-3-medical"))

    def test_openai_compatible_auto_omits_the_language_field(self):
        self.assertIsNone(normalize_language_for_provider("openai_compatible", "auto"))

    def test_openai_compatible_multi_is_rejected(self):
        with self.assertRaises(STTConfigurationError):
            normalize_language_for_provider("openai_compatible", "multi")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(STTConfigurationError):
            normalize_language_for_provider("whisper", "fa")

    def test_empty_language_is_rejected(self):
        with self.assertRaises(STTConfigurationError):
            normalize_language_for_provider("deepgram", "  ")


class AnthropicRequestTests(unittest.TestCase):
    def test_retired_haiku_3_5_is_not_the_default_any_more(self):
        settings = make_settings(note_api_provider="anthropic", note_api_key="k")
        model = settings.effective_note_model
        self.assertNotIn("3-5", model)
        self.assertNotIn("3.5", model)
        self.assertTrue(model.startswith("claude-haiku-"))

    def test_request_omits_sampling_the_model_cannot_accept(self):
        settings = make_settings(note_api_provider="anthropic", note_api_key="k")
        url, headers, payload, _params = _provider_request("chunk", settings, "prompt")
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "k")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(payload["model"], settings.effective_note_model)
        self.assertEqual(payload["temperature"], 0.2)
        # temperature and top_p are mutually exclusive on the Messages API and
        # top_k is not supported at all, so neither is ever sent.
        self.assertNotIn("top_p", payload)
        self.assertNotIn("top_k", payload)

    def test_newer_models_receive_no_sampling_parameter(self):
        settings = make_settings(
            note_api_provider="anthropic", note_api_key="k", note_api_model="claude-opus-4-7"
        )
        _url, _headers, payload, _params = _provider_request("chunk", settings, "prompt")
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)
        self.assertNotIn("top_k", payload)

    def test_sampling_support_is_decided_from_the_model_generation(self):
        self.assertTrue(anthropic_supports_temperature("claude-haiku-4-5"))
        self.assertTrue(anthropic_supports_temperature("claude-haiku-4-5-20251001"))
        self.assertTrue(anthropic_supports_temperature("claude-sonnet-4-6"))
        self.assertFalse(anthropic_supports_temperature("claude-opus-4-7"))
        self.assertFalse(anthropic_supports_temperature("claude-sonnet-5"))
        self.assertFalse(anthropic_supports_temperature("claude-fable-5-1"))
        # Unknown naming (proxy, alias) is treated as unsupported: omitting a
        # hint is harmless, sending a rejected one fails the request.
        self.assertFalse(anthropic_supports_temperature("my-clone"))
        self.assertEqual(anthropic_sampling_params("claude-opus-4-7"), {})

    def test_authentication_headers_are_reserved(self):
        settings = make_settings(
            note_api_provider="anthropic",
            note_api_key="real-key",
            note_api_extra_headers=(("x-api-key", "attacker"), ("X-Title", "Gamas")),
        )
        _url, headers, _payload, _params = _provider_request("chunk", settings, "prompt")
        self.assertEqual(headers["x-api-key"], "real-key")
        self.assertEqual(headers["X-Title"], "Gamas")

    def test_reserved_headers_are_dropped_while_parsing_the_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
                "SPEECHMATICS_API_KEY=k\nNOTE_API_PROVIDER=openai_compatible\n"
                'NOTE_API_EXTRA_HEADERS_JSON={"Authorization":"Bearer evil","X-App":"Gamas"}\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                with self.assertLogs("gamas_bot.config", level="WARNING"):
                    settings = Settings.from_env(env)
        self.assertEqual(settings.note_api_extra_headers, (("X-App", "Gamas"),))

    def test_malformed_header_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
                "SPEECHMATICS_API_KEY=k\n"
                'NOTE_API_EXTRA_HEADERS_JSON={"bad name":"x"}\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env(env)


class NoteResponseSchemaTests(unittest.TestCase):
    def test_openai_compatible_choices_are_parsed(self):
        self.assertEqual(
            _provider_response({"choices": [{"message": {"content": "جزوه"}}]}, "openai_compatible"),
            "جزوه",
        )

    def test_anthropic_content_blocks_are_parsed(self):
        self.assertEqual(
            _provider_response(
                {"content": [{"type": "text", "text": "جزوه"}], "stop_reason": "end_turn"},
                "anthropic",
            ),
            "جزوه",
        )

    def test_malformed_payloads_report_a_schema_problem_not_a_key_error(self):
        cases = [
            ("openai_compatible", {"choices": []}),
            ("openai_compatible", {"choices": [{}]}),
            ("openai_compatible", {"choices": "not-a-list"}),
            ("openai_compatible", {"results": []}),
            ("anthropic", {"content": "not-a-list"}),
            ("anthropic", {}),
        ]
        for provider, payload in cases:
            with self.subTest(provider=provider, payload=payload):
                with self.assertRaises(StructuringError) as caught:
                    _provider_response(payload, provider)
                self.assertNotIn("KeyError", str(caught.exception))
                self.assertIsInstance(caught.exception.__cause__, (KeyError, IndexError, TypeError, AttributeError))

    def test_truncated_answer_is_reported_as_truncated(self):
        with self.assertRaises(StructuringError):
            _provider_response(
                {"choices": [{"finish_reason": "length", "message": {"content": "..."}}]},
                "openai_compatible",
            )


class OpenAICompatibleSTTTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_payload_is_parsed(self):
        self.assertEqual(_openai_transcript({"text": "  سلام  "}).text, "سلام")

    def test_malformed_stt_payloads_are_schema_errors(self):
        for payload in ({}, {"text": None}, {"text": "   "}, [], {"choices": []}):
            with self.subTest(payload=payload):
                with self.assertRaises(STTError) as caught:
                    _openai_transcript(payload)
                self.assertNotIn("KeyError", str(caught.exception))

    async def test_auto_language_omits_the_form_field(self):
        from gamas_bot.stt import _openai_compatible_stt

        captured: dict = {}
        session = SimpleNamespace(
            post=lambda url, headers=None, data=None: captured.update(
                {"url": url, "fields": [field[0]["name"] for field in data._fields]}
            )
            or _FakeResponse(200, {"text": "سلام"})
        )
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            await _openai_compatible_stt(
                session,
                Path(audio.name),
                make_settings(
                    stt_openai_base_url="https://stt.example.test/v1",
                    stt_language="auto",
                ),
            )
        self.assertIn("model", captured["fields"])
        self.assertIn("file", captured["fields"])
        self.assertNotIn("language", captured["fields"])

    async def test_language_is_sent_when_it_is_a_real_code(self):
        from gamas_bot.stt import _openai_compatible_stt

        captured: dict = {}
        session = SimpleNamespace(
            post=lambda url, headers=None, data=None: captured.update(
                {"fields": [field[0]["name"] for field in data._fields]}
            )
            or _FakeResponse(200, {"text": "سلام"})
        )
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            await _openai_compatible_stt(
                session,
                Path(audio.name),
                make_settings(stt_openai_base_url="https://stt.example.test/v1"),
            )
        self.assertIn("language", captured["fields"])


class STTRetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    """Only transient failures are retried, and the retries are bounded."""

    def _settings(self, **overrides):
        return make_settings(
            deepgram_api_key="dg",
            speechmatics_api_key=None,
            stt_primary="deepgram",
            stt_fallback_enabled=False,
            stt_retry_base_delay=0.001,
            stt_retry_max_delay=0.01,
            **overrides,
        )

    async def _run_with_statuses(self, statuses, **overrides):
        responses = [
            _FakeResponse(status, _deepgram_payload() if 200 <= status < 300 else {})
            for status in statuses
        ]
        session = _ScriptedSession(responses)
        settings = self._settings(**overrides)
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            with patch("gamas_bot.stt.aiohttp.ClientSession", lambda **kwargs: session):
                try:
                    result = await transcribe(Path(audio.name), settings)
                except STTError as exc:
                    return None, len(session.requests), exc
        return result, len(session.requests), None

    async def test_transient_statuses_are_retried_until_success(self):
        for status in (429, 500, 502, 503, 504, 408, 425):
            with self.subTest(status=status):
                result, calls, error = await self._run_with_statuses([status, 200])
                self.assertIsNone(error)
                self.assertEqual(result.engine, "deepgram")
                self.assertEqual(calls, 2)

    async def test_permanent_statuses_are_not_retried(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                result, calls, error = await self._run_with_statuses([status, 200])
                self.assertIsNone(result)
                self.assertEqual(calls, 1)
                self.assertIsInstance(error, STTError)
                self.assertNotIsInstance(error, STTTransientError)

    async def test_retry_after_header_is_honoured_and_bounded(self):
        session = _ScriptedSession(
            [
                _FakeResponse(429, {}, headers={"Retry-After": "0"}),
                _FakeResponse(200, _deepgram_payload()),
            ]
        )
        settings = self._settings()
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            with patch("gamas_bot.stt.aiohttp.ClientSession", lambda **kwargs: session):
                result = await transcribe(Path(audio.name), settings)
        self.assertEqual(result.text, "سلام")
        self.assertEqual(len(session.requests), 2)

    async def test_retries_are_bounded_by_the_configuration(self):
        _result, calls, error = await self._run_with_statuses(
            [503, 503, 503, 503, 503], stt_max_attempts=3
        )
        self.assertEqual(calls, 3)
        self.assertIsInstance(error, STTError)

    async def test_network_failures_are_retried(self):
        for error in (aiohttp.ClientError("reset"), asyncio.TimeoutError()):
            with self.subTest(error=type(error).__name__):
                session = _ScriptedSession([_FakeResponse(200, _deepgram_payload())])
                settings = self._settings()
                with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
                    with patch(
                        "gamas_bot.stt._deepgram", new=AsyncMock(side_effect=[error, Transcript("deepgram", "ok", 1.0)])
                    ), patch("gamas_bot.stt.aiohttp.ClientSession", lambda **kwargs: session):
                        result = await transcribe(Path(audio.name), settings)
                self.assertEqual(result.text, "ok")

    async def test_attempt_timeout_is_retried_and_then_falls_back(self):
        calls: list[int] = []

        async def slow(session, path, settings):
            calls.append(1)
            await asyncio.sleep(5)

        session = _ScriptedSession([])
        settings = make_settings(
            deepgram_api_key="dg",
            speechmatics_api_key="sm",
            stt_primary="deepgram",
            stt_fallback_enabled=True,
            stt_job_timeout=0.02,
            stt_retry_base_delay=0.001,
            stt_retry_max_delay=0.01,
        )
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            with patch("gamas_bot.stt._deepgram", new=slow), patch(
                "gamas_bot.stt._speechmatics",
                new=AsyncMock(return_value=Transcript("speechmatics", "fallback", 1.0)),
            ), patch("gamas_bot.stt.aiohttp.ClientSession", lambda **kwargs: session):
                result = await transcribe(Path(audio.name), settings)
        self.assertEqual(result.engine, "speechmatics")
        self.assertEqual(len(calls), settings.stt_max_attempts)

    async def test_cancellation_is_never_swallowed_by_the_retry_loop(self):
        async def cancelled(session, path, settings):
            raise asyncio.CancelledError

        settings = self._settings()
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            with patch("gamas_bot.stt._deepgram", new=cancelled), patch(
                "gamas_bot.stt.aiohttp.ClientSession",
                lambda **kwargs: _ScriptedSession([]),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await transcribe(Path(audio.name), settings)

    async def test_a_configuration_error_falls_through_to_the_next_engine(self):
        # Deepgram cannot do `auto`, Speechmatics can: the operator's provider
        # order is honoured instead of failing the job outright.
        settings = make_settings(
            speechmatics_api_key="sm",
            deepgram_api_key="dg",
            stt_primary="deepgram",
            stt_language="auto",
            stt_retry_base_delay=0.001,
            stt_retry_max_delay=0.01,
        )
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            with patch(
                "gamas_bot.stt._speechmatics",
                new=AsyncMock(return_value=Transcript("speechmatics", "ok", 1.0)),
            ), patch(
                "gamas_bot.stt.aiohttp.ClientSession",
                lambda **kwargs: _ScriptedSession([]),
            ):
                result = await transcribe(Path(audio.name), settings)
        self.assertEqual(result.engine, "speechmatics")


class PunctuationAttachmentTests(unittest.TestCase):
    """Speechmatics reports which token a punctuation mark belongs to."""

    @staticmethod
    def _result(item_type: str, content: str, attaches_to: str | None = None) -> dict:
        alternative: dict = {"content": content}
        if attaches_to is not None:
            alternative["attaches_to"] = attaches_to
        return {"type": item_type, "alternatives": [alternative]}

    def test_previous_closes_the_previous_token(self):
        results = [self._result("word", "سلام"), self._result("punctuation", ".", "previous")]
        self.assertEqual(_speechmatics_text(results), "سلام.")

    def test_next_opens_the_next_token(self):
        results = [
            self._result("punctuation", "«", "next"),
            self._result("word", "سلام"),
            self._result("punctuation", "»", "previous"),
        ]
        self.assertEqual(_speechmatics_text(results), "«سلام»")

    def test_both_closes_and_opens(self):
        results = [
            self._result("word", "یک"),
            self._result("punctuation", "-", "both"),
            self._result("word", "دو"),
        ]
        self.assertEqual(_speechmatics_text(results), "یک- -دو")

    def test_missing_attachment_degrades_to_previous(self):
        results = [self._result("word", "سلام"), self._result("punctuation", "؟")]
        self.assertEqual(_speechmatics_text(results), "سلام؟")

    def test_unknown_attachment_degrades_to_previous(self):
        results = [
            self._result("word", "سلام"),
            self._result("punctuation", "!", "sideways"),
        ]
        self.assertEqual(_speechmatics_text(results), "سلام!")

    def test_leading_punctuation_with_no_previous_token(self):
        results = [self._result("punctuation", "(", "previous"), self._result("word", "نکته")]
        self.assertEqual(_speechmatics_text(results), "(نکته")

    def test_trailing_punctuation_with_no_following_token(self):
        results = [self._result("word", "نکته"), self._result("punctuation", ":", "next")]
        self.assertEqual(_speechmatics_text(results), "نکته:")

    def test_no_double_spaces_and_no_lost_words(self):
        results = [
            self._result("word", "فشار"),
            self._result("word", "خون"),
            self._result("word", "145/90"),
            self._result("punctuation", ".", "previous"),
            self._result("word", "SpO2"),
            self._result("word", "96%"),
        ]
        text = _speechmatics_text(results)
        self.assertEqual(text, "فشار خون 145/90. SpO2 96%")
        self.assertNotIn("  ", text)

    def test_unknown_result_types_are_skipped(self):
        results = [
            self._result("word", "دوز"),
            {"type": "entity", "alternatives": [{"content": "500 mg"}]},
            self._result("word", "دیگر"),
        ]
        self.assertEqual(_speechmatics_text(results), "دوز دیگر")

    def test_malformed_results_do_not_raise(self):
        for payload in (None, "text", [None, 5], [{"alternatives": "bad"}], [{}]):
            with self.subTest(payload=payload):
                self.assertEqual(_speechmatics_text(payload), "")


class MixedScriptTextTests(unittest.TestCase):
    """Persian/English medical text must survive transcription rendering."""

    SAMPLES = (
        "فشار خون بیمار BP 145/90 بود",
        "SpO2 96% و PR 88 ثبت شد",
        "دمای بیمار T 36.7 درجه است",
        "رگ‌گیری با آنژیوکت 20G IV انجام شد",
        "نمرهٔ Morse 45 و Braden 20 محاسبه شد",
        "MRI و CT درخواست شد",
        "Metformin 500 mg هر هشت ساعت",
    )

    def test_transcript_builder_preserves_units_and_numbers(self):
        for sample in self.SAMPLES:
            with self.subTest(sample=sample):
                results = [
                    {"type": "word", "alternatives": [{"content": token}]}
                    for token in sample.split()
                ]
                self.assertEqual(_speechmatics_text(results), sample)

    def test_transcript_builder_adds_no_bidi_or_zero_width_characters(self):
        results = [
            {"type": "word", "alternatives": [{"content": token}]}
            for token in "دوز Metformin 500 mg است".split()
        ]
        text = _speechmatics_text(results)
        self.assertEqual(text, "دوز Metformin 500 mg است")
        for invisible in ("\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\ufeff"):
            self.assertNotIn(invisible, text)

    def test_persian_normalization_keeps_medical_formatting(self):
        from gamas_bot.textnorm import normalize_for_compare

        for sample in self.SAMPLES:
            with self.subTest(sample=sample):
                normalized = normalize_for_compare(sample)
                self.assertTrue(normalized)
        # Numbers, units and Latin terms must not be folded away: only the
        # comparison key is normalized, never the stored transcript.
        self.assertEqual(
            normalize_for_compare("Metformin 500 mg"), normalize_for_compare("metformin 500 mg")
        )


if __name__ == "__main__":
    unittest.main()
