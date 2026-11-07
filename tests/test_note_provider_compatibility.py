"""Offline regressions for the 2026-11-07 note-provider compatibility profile.

Everything here is deterministic: no live API key, no network call, no
generation quota. The settings under test are the *deployment* profile
(``gemini,openrouter`` with structured output and reasoning disabled), while
the shipped project defaults stay unchanged and are covered by the existing
configuration tests.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import unittest
from unittest import mock

from gamas_bot import llm
from gamas_bot.config import (
    DEFAULT_NOTE_MAX_OUTPUT_TOKENS,
    DEFAULT_NOTE_PROVIDERS,
    from_env,
    note_route_order,
    note_route_summary,
    note_routes,
)
from gamas_bot.llm import (
    LLMAuthError,
    LLMOutputBudgetError,
    LLMRequestError,
    _build_request_for,
    _parse_json_object,
    complete,
    complete_json,
)
from gamas_bot.notes import StructuringError, _load_json_payload
from tests.support import make_settings

GEMINI_MODEL = "gemini-3.5-flash-lite"
OPENROUTER_MODEL = "nvidia/nemotron-3.5-super-vl:free"
SCHEMA = {
    "type": "object",
    "properties": {"sections": {"type": "array", "items": {"type": "string"}}},
    "required": ["sections"],
}
VALID_NOTES_JSON = json.dumps(
    {
        "title": "فیزیولوژی کلیه",
        "summary": "خلاصهٔ درس",
        "sections": [
            {"heading": "بخش ۱", "paragraphs": ["نفرون واحد عملکردی کلیه است."]},
            {"heading": "بخش ۲", "paragraphs": ["فیلتراسیون در گلومرول انجام می‌شود."]},
            {"heading": "بخش ۳", "bullets": ["بازجذب آب"]},
            {"heading": "بخش ۴", "bullets": ["ترشح و دفع"]},
        ],
    },
    ensure_ascii=False,
)


def deployment_env(**overrides):
    """The exact tested profile, plus the credentials a route needs."""
    base = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_API_ID": "42",
        "TELEGRAM_API_HASH": "hash",
        "ADMIN_IDS": "111",
        "SPEECHMATICS_API_KEY": "sm",
        "GEMINI_API_KEY": "g-key-not-a-secret",
        "OPENROUTER_API_KEY": "o-key-not-a-secret",
        "NOTE_PROVIDERS": "gemini,openrouter",
        "NOTE_STRUCTURED_OUTPUT": "0",
        "NOTE_REASONING_EFFORT": "none",
        "NOTE_MAX_OUTPUT_TOKENS": "32768",
        "NOTE_GEMINI_THINKING_BUDGET": "-1",
        "GEMINI_MODEL": GEMINI_MODEL,
        "OPENROUTER_MODEL": OPENROUTER_MODEL,
    }
    base.update({k: v for k, v in overrides.items() if v is not None})
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
    return base


def settings_from_env(**overrides):
    with mock.patch.dict(os.environ, deployment_env(**overrides), clear=True):
        return from_env(env_file="/nonexistent")


def deployed_settings(**overrides):
    """The same effective settings without going through ``os.environ``."""
    base = {
        "note_providers": ("gemini", "openrouter"),
        "note_provider": "",
        "note_api_key": "",
        "note_api_base_url": "",
        "gemini_api_key": "g-key",
        "gemini_model": GEMINI_MODEL,
        "openrouter_api_key": "o-key",
        "openrouter_model": OPENROUTER_MODEL,
        "note_structured_output": False,
        "note_reasoning_effort": "none",
        "note_max_output_tokens": 32768,
        "gemini_thinking_budget": -1,
        "note_ttfb_timeout": 0.0,
        "note_hedge_after": 0.0,
    }
    base.update(overrides)
    return make_settings(**base)


class _AsyncCM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return False


class _Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    async def read(self):
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    async def json(self, content_type=None):
        return self._payload


class _Session:
    """Records the live payload objects so in-place fallback edits are visible."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        recorded = dict(kwargs)
        # The retry path mutates the live payload in place; snapshot it so a
        # test can compare the original request with the compatibility retry.
        if "json" in recorded:
            recorded["json"] = copy.deepcopy(recorded["json"])
        self.calls.append({"url": url, **recorded})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return _AsyncCM(response)


def openai_answer(text, *, status=200, finish_reason="stop", usage=None):
    return _Response(
        status,
        {
            "model": "served-model",
            "choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
            "usage": usage or {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        },
    )


def gemini_answer(text, finish_reason="STOP"):
    return _Response(
        200,
        {
            "modelVersion": "served-model",
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish_reason}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 30},
        },
    )


def gemini_invalid_argument():
    """Google AI Studio's documented 400 INVALID_ARGUMENT envelope."""
    return _Response(
        400,
        {
            "error": {
                "code": 400,
                "message": "Request contains an invalid argument.",
                "status": "INVALID_ARGUMENT",
            }
        },
    )


class ConfigurationTests(unittest.TestCase):
    """The deployment profile resolves to exactly two routes, in order."""

    def test_recommended_profile_creates_exactly_gemini_then_openrouter(self):
        settings = settings_from_env()
        routes = note_routes(settings)
        self.assertEqual(tuple(r.slug for r in routes), ("gemini", "openrouter"))
        self.assertEqual(note_route_order(settings), ("gemini", "openrouter"))
        self.assertEqual(routes[0].model, GEMINI_MODEL)
        self.assertEqual(routes[1].model, OPENROUTER_MODEL)
        # Each provider keeps its own credential; neither is shared or empty.
        self.assertEqual(routes[0].api_key, "g-key-not-a-secret")
        self.assertEqual(routes[1].api_key, "o-key-not-a-secret")
        self.assertNotIn("nara", [r.slug for r in routes])
        self.assertNotIn("openai_compatible", [r.slug for r in routes])
        # The shipped, project-wide default is deliberately unchanged.
        self.assertEqual(DEFAULT_NOTE_PROVIDERS, "gemini,nara,openrouter,deepseek,avalai")
        self.assertEqual(DEFAULT_NOTE_MAX_OUTPUT_TOKENS, 16384)

    def test_no_legacy_openai_compatible_route_without_legacy_credentials(self):
        settings = settings_from_env(
            NOTE_PROVIDER="openai_compatible", NOTE_BASE_URL=None, NOTE_API_KEY=None
        )
        self.assertEqual(tuple(r.slug for r in note_routes(settings)), ("gemini", "openrouter"))
        settings = settings_from_env(NOTE_PROVIDER="gemini")
        self.assertEqual(tuple(r.slug for r in note_routes(settings)), ("gemini", "openrouter"))

    def test_legacy_slot_still_works_and_is_never_duplicated(self):
        explicit = settings_from_env(
            NOTE_PROVIDER="openai_compatible",
            NOTE_API_KEY="legacy-key",
            NOTE_BASE_URL="https://local.example/v1",
        )
        routes = note_routes(explicit)
        self.assertEqual(
            tuple(r.slug for r in routes), ("gemini", "openrouter", "openai_compatible")
        )
        self.assertEqual(routes[2].api_key, "legacy-key")
        # An explicitly declared legacy slug is the same leg, not a second one.
        inline = settings_from_env(NOTE_PROVIDERS="gemini,openai_compatible,openrouter")
        self.assertEqual(
            inline.note_providers,
            ("gemini", "openai_compatible", "openrouter"),
        )
        # A duplicate legacy declaration cannot produce a second Gemini leg.
        duplicated = settings_from_env(NOTE_PROVIDERS="gemini,openrouter,gemini")
        self.assertEqual(tuple(r.slug for r in note_routes(duplicated)), ("gemini", "openrouter"))

    def test_structured_output_off_and_reasoning_none_are_resolved(self):
        settings = settings_from_env()
        self.assertFalse(settings.note_structured_output)
        self.assertEqual(settings.note_reasoning_effort, "none")
        self.assertEqual(settings.gemini_thinking_budget, -1)
        self.assertEqual(settings.note_max_output_tokens, 32768)
        for route in note_routes(settings):
            _url, _headers, payload = _build_request_for(
                route, settings, "S", "U", schema=SCHEMA
            )
            self.assertNotIn("response_format", payload)
            if route.style == "gemini":
                generation = payload["generationConfig"]
                self.assertNotIn("responseSchema", generation)
                self.assertNotIn("thinkingConfig", generation)
                self.assertEqual(generation["responseMimeType"], "application/json")
                self.assertNotIn("reasoning_effort", payload)
            else:
                self.assertEqual(payload["reasoning_effort"], "none")

    def test_reasoning_none_is_sent_only_where_it_is_supported(self):
        settings = deployed_settings(
            note_providers=("nara", "openrouter", "deepseek", "openai_compatible", "gemini"),
            nara_api_key="nara-key",
            deepseek_api_key="deepseek-key",
            note_api_key="legacy-key",
            note_provider="openai_compatible",
            note_api_base_url="https://local.example/v1",
        )
        by_slug = {route.slug: route for route in note_routes(settings)}
        for slug in ("nara", "openrouter", "deepseek"):
            _url, _headers, payload = _build_request_for(
                by_slug[slug], settings, "S", "U", schema=SCHEMA
            )
            self.assertEqual(payload["reasoning_effort"], "none", slug)
        for slug in ("openai_compatible", "gemini"):
            _url, _headers, payload = _build_request_for(
                by_slug[slug], settings, "S", "U", schema=SCHEMA
            )
            self.assertNotIn("reasoning_effort", payload, slug)

    def test_note_route_summary_is_inspectable_and_contains_no_secrets(self):
        settings = settings_from_env()
        summary = "\n".join(note_route_summary(settings))
        self.assertIn("Provider order: gemini, openrouter", summary)
        self.assertIn("Structured output: disabled", summary)
        self.assertIn("Reasoning effort: none", summary)
        self.assertIn("Maximum output tokens: 32768", summary)
        self.assertIn("Gemini thinking budget: disabled", summary)
        self.assertNotIn("g-key-not-a-secret", summary)
        self.assertNotIn("o-key-not-a-secret", summary)

    def test_show_config_command_is_offline_and_secret_free(self):
        from scripts import probe_note_providers

        out = io.StringIO()
        with mock.patch.dict(os.environ, deployment_env(), clear=True):
            with contextlib.redirect_stdout(out):
                code = probe_note_providers.main(["--show-config"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Provider order: gemini, openrouter", text)
        self.assertIn("requested_output_tokens=32768", text)
        self.assertIn("no network request was made", text)
        self.assertNotIn("g-key-not-a-secret", text)
        self.assertNotIn("o-key-not-a-secret", text)

    def test_probe_never_prints_a_key_hidden_in_a_base_url(self):
        from scripts.probe_note_providers import _safe_base_url

        self.assertEqual(
            _safe_base_url("https://user:secret@api.example/v1?key=secret#frag"),
            "https://api.example/v1",
        )


class GeminiCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    """Non-structured payload, strict validation, bounded INVALID_ARGUMENT fallback."""

    def test_the_tested_working_payload_has_no_schema_or_thinking_config(self):
        settings = deployed_settings()
        _url, headers, payload = _build_request_for(
            note_routes(settings)[0], settings, "S", "U", schema=SCHEMA
        )
        self.assertEqual(headers["x-goog-api-key"], "g-key")
        generation = payload["generationConfig"]
        self.assertEqual(generation["responseMimeType"], "application/json")
        self.assertEqual(generation["maxOutputTokens"], 32768)
        self.assertNotIn("responseSchema", generation)
        self.assertNotIn("thinkingConfig", generation)

    def test_structured_mode_still_builds_a_schema_when_enabled(self):
        settings = deployed_settings(note_structured_output=True)
        _url, _headers, payload = _build_request_for(
            note_routes(settings)[0], settings, "S", "U", schema=SCHEMA
        )
        self.assertEqual(payload["generationConfig"]["responseSchema"]["type"], "OBJECT")

    async def test_non_structured_answer_is_parsed_and_domain_validated(self):
        settings = deployed_settings(note_providers=("gemini",))
        session = _Session([gemini_answer(VALID_NOTES_JSON)])
        payload = await complete_json(session, settings, system="S", user="U")
        notes = _load_json_payload(payload)
        self.assertEqual(notes.title, "فیزیولوژی کلیه")
        self.assertEqual(len(notes.sections), 4)
        first_call = session.calls[0]["json"]["generationConfig"]
        self.assertNotIn("responseSchema", first_call)

    async def test_incomplete_notes_json_is_rejected_not_silently_accepted(self):
        settings = deployed_settings(note_providers=("gemini",))
        # Valid JSON, but no usable study-note content: the domain validator
        # must refuse it even without a responseSchema enforcing the shape.
        session = _Session([gemini_answer('{"title": "فیزیولوژی کلیه"}')])
        payload = await complete_json(session, settings, system="S", user="U")
        with self.assertRaises(StructuringError):
            _load_json_payload(payload)
        # Malformed JSON is rejected by the same strict parser, too.
        with self.assertRaises(llm.LLMResponseError):
            _parse_json_object('{"title": "ناقص"')

    async def test_gemini_invalid_argument_drops_only_the_schema_once(self):
        settings = deployed_settings(note_providers=("gemini",), note_structured_output=True)
        session = _Session([gemini_invalid_argument(), gemini_answer(VALID_NOTES_JSON)])
        payload = await complete_json(
            session, settings, system="S", user="U", schema=SCHEMA
        )
        self.assertEqual(payload["title"], "فیزیولوژی کلیه")
        self.assertEqual(len(session.calls), 2)
        first = session.calls[0]["json"]["generationConfig"]
        second = session.calls[1]["json"]["generationConfig"]
        self.assertIn("responseSchema", first)
        self.assertNotIn("responseSchema", second)
        self.assertEqual(second["responseMimeType"], "application/json")
        self.assertEqual(second["maxOutputTokens"], 32768)
        # No other field was removed or changed by the compatibility retry.
        self.assertEqual(set(first) - {"responseSchema"}, set(second))

    async def test_invalid_argument_without_a_schema_does_not_retry(self):
        settings = deployed_settings(note_providers=("gemini",))
        session = _Session([gemini_invalid_argument(), gemini_answer(VALID_NOTES_JSON)])
        with self.assertRaises(LLMRequestError):
            await complete_json(session, settings, system="S", user="U")
        self.assertEqual(len(session.calls), 1)

    async def test_unrelated_400_does_not_trigger_the_compatibility_fallback(self):
        settings = deployed_settings(note_providers=("gemini",), note_structured_output=True)
        rejection = _Response(400, {"error": {"message": "Bad request", "status": "FAILED_PRECONDITION"}})
        session = _Session([rejection, gemini_answer(VALID_NOTES_JSON)])
        with self.assertRaises(LLMRequestError):
            await complete_json(session, settings, system="S", user="U", schema=SCHEMA)
        self.assertEqual(len(session.calls), 1)

    async def test_auth_failure_is_never_retried_as_a_schema_problem(self):
        settings = deployed_settings(note_providers=("gemini",), note_structured_output=True)
        auth = _Response(400, {"error": {
            "code": 400, "message": "API key not valid. Please pass a valid API key.",
            "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}],
        }})
        session = _Session([auth, gemini_answer(VALID_NOTES_JSON)])
        with self.assertRaises(LLMAuthError):
            await complete_json(session, settings, system="S", user="U", schema=SCHEMA)
        self.assertEqual(len(session.calls), 1)

    async def test_compatibility_adjustments_are_bounded(self):
        """A gateway alternating two parameter spellings cannot loop forever."""
        settings = deployed_settings(note_providers=("openrouter",))
        def rejection():
            return _Response(400, {"error": {"message": (
                "Unsupported parameter: max_tokens and max_completion_tokens are not supported"
            )}})

        session = _Session([rejection() for _ in range(12)])
        with self.assertRaises(LLMRequestError):
            await complete(session, settings, system="S", user="U")
        self.assertEqual(len(session.calls), llm.MAX_COMPATIBILITY_ADJUSTMENTS + 1)


class OpenRouterTests(unittest.IsolatedAsyncioTestCase):
    """The 32,768 budget, reasoning effort, JSON handling and truncation."""

    def test_request_uses_the_tested_model_and_the_full_output_budget(self):
        settings = deployed_settings()
        route = note_routes(settings)[1]
        _url, headers, payload = _build_request_for(route, settings, "S", "U", schema=SCHEMA)
        self.assertEqual(payload["model"], OPENROUTER_MODEL)
        self.assertEqual(payload["max_tokens"], 32768)
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertNotIn("response_format", payload)
        self.assertEqual(headers["Authorization"], "Bearer o-key")
        self.assertEqual(llm.request_payload_output_tokens(payload), 32768)

    async def test_valid_json_is_accepted_without_a_response_schema(self):
        settings = deployed_settings(note_providers=("openrouter",))
        session = _Session([openai_answer(VALID_NOTES_JSON)])
        payload = await complete_json(session, settings, system="S", user="U", schema=SCHEMA)
        self.assertEqual(len(_load_json_payload(payload).sections), 4)
        self.assertNotIn("response_format", session.calls[0]["json"])

    async def test_truncated_completion_is_not_a_complete_note(self):
        settings = deployed_settings()
        gemini_truncated = gemini_answer(
            '{"title": "فیزیولوژی کلیه", "sections": [', finish_reason="MAX_TOKENS"
        )
        openai_truncated = openai_answer(
            '{"title": "فیزیولوژی کلیه", "sections": [', finish_reason="length"
        )
        session = _Session([gemini_truncated, openai_answer(VALID_NOTES_JSON)])
        payload = await complete_json(session, settings, system="S", user="U")
        self.assertEqual(payload["title"], "فیزیولوژی کلیه")
        # Gemini was abandoned after one truncated reply, not retried as bad
        # JSON; OpenRouter served the complete answer.
        self.assertEqual(len(session.calls), 2)
        self.assertIn("/chat/completions", session.calls[1]["url"])
        with self.assertRaises(LLMOutputBudgetError):
            llm._parse_response(openai_truncated._payload, "openai")
        with self.assertRaises(LLMOutputBudgetError):
            llm._parse_response(gemini_truncated._payload, "gemini")

    async def test_usage_log_separates_requested_budget_from_actual_usage(self):
        settings = deployed_settings(note_providers=("openrouter",))
        session = _Session([openai_answer("ok")])
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            await complete(session, settings, system="S", user="U")
        usage_lines = [line for line in logs.output if "note provider usage" in line]
        self.assertTrue(usage_lines)
        self.assertIn("requested_output_tokens=32768", usage_lines[0])
        self.assertIn("output_tokens=30", usage_lines[0])
        self.assertIn("requested_output_tokens=32768", "\n".join(logs.output))


class FailoverRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Gemini first, OpenRouter second, Nara only when explicitly listed."""

    def test_nara_is_not_attempted_when_it_is_not_in_the_chain(self):
        settings = deployed_settings(nara_api_key="nara-key", note_providers=("gemini", "openrouter"))
        self.assertEqual(tuple(r.slug for r in note_routes(settings)), ("gemini", "openrouter"))

    async def test_gemini_is_attempted_before_openrouter(self):
        settings = deployed_settings()
        session = _Session([gemini_answer("gemini-answer")])
        result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "gemini-answer")
        self.assertEqual(len(session.calls), 1)
        self.assertIn("/models/", session.calls[0]["url"])

    async def test_openrouter_is_attempted_after_an_eligible_gemini_failure(self):
        settings = deployed_settings()
        rejection = _Response(503, {"error": {"message": "temporarily unavailable"}})
        session = _Session([rejection, openai_answer("openrouter-answer")])
        with mock.patch.object(llm, "MAX_TRANSIENT_RETRIES", 0):
            result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "openrouter-answer")
        self.assertEqual(len(session.calls), 2)
        self.assertIn("/chat/completions", session.calls[1]["url"])
        self.assertEqual(session.calls[1]["headers"]["Authorization"], "Bearer o-key")

    async def test_permanent_400_is_not_retried_on_the_same_provider(self):
        settings = deployed_settings()
        rejection = _Response(400, {"error": {"message": "Bad request"}})
        session = _Session([rejection, openai_answer("openrouter-answer")])
        result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "openrouter-answer")
        self.assertEqual(len(session.calls), 2)

    async def test_all_permanent_failures_stop_after_one_attempt_each(self):
        settings = deployed_settings()
        def rejection():
            return _Response(400, {"error": {"message": "Bad request"}})

        session = _Session([rejection(), rejection()])
        with self.assertRaises(LLMRequestError):
            await complete(session, settings, system="S", user="U")
        self.assertEqual(len(session.calls), 2)
