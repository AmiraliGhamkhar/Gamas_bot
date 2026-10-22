"""Tests for the single LLM client."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import aiohttp


from gamas_bot import llm
from gamas_bot.config import DEFAULT_GEMINI_MODEL, DEFAULT_NARA_MODEL, note_routes
from gamas_bot.llm import (
    LLMAuthError,
    LLMContextOverflowError,
    LLMOutputBudgetError,
    LLMResponseError,
    LLMTransientError,
    _build_request,
    _endpoint,
    _parse_response,
    _usage_summary,
    complete,
    complete_json,
    extract_json_object,
)
from tests.support import make_settings


class EndpointTests(unittest.TestCase):
    def test_appends_suffix(self):
        self.assertEqual(
            _endpoint("https://api.example/v1", "chat/completions"),
            "https://api.example/v1/chat/completions",
        )

    def test_does_not_double_an_exact_endpoint(self):
        self.assertEqual(
            _endpoint("https://api.example/v1/chat/completions", "chat/completions"),
            "https://api.example/v1/chat/completions",
        )


class StructuredOutputAndRoutingTests(unittest.TestCase):
    """Phase 4 request controls: schema, throughput routing, compile provider."""

    schema = {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"heading": {"type": "string"}},
                    "required": ["heading"],
                },
            }
        },
        "required": ["sections"],
    }

    def test_no_schema_is_sent_unless_a_caller_asks(self):
        settings = make_settings()
        route = note_routes(settings)[0]
        _url, _headers, payload = llm._build_request_for(route, settings, "S", "U")
        self.assertNotIn("response_format", payload)

    def test_gemini_schema_uses_the_upper_case_dialect(self):
        settings = make_settings(
            note_providers=("gemini",), gemini_api_key="k", note_structured_output=True
        )
        route = note_routes(settings)[0]
        _url, _headers, payload = llm._build_request_for(
            route, settings, "S", "U", schema=self.schema
        )
        generation = payload["generationConfig"]
        self.assertEqual(generation["responseMimeType"], "application/json")
        self.assertEqual(generation["responseSchema"]["type"], "OBJECT")
        section = generation["responseSchema"]["properties"]["sections"]
        self.assertEqual(section["type"], "ARRAY")
        self.assertEqual(section["items"]["type"], "OBJECT")
        # Gemini infers required-ness; the key itself is not in its dialect.
        self.assertNotIn("required", section["items"])

    def test_openai_style_uses_json_schema_not_strict_mode(self):
        settings = make_settings(note_structured_output=True)
        route = note_routes(settings)[0]
        _url, _headers, payload = llm._build_request_for(
            route, settings, "S", "U", schema=self.schema
        )
        response_format = payload["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "gamas_notes")
        self.assertNotIn("strict", response_format["json_schema"])

    def test_a_rejected_schema_is_dropped_instead_of_failing_the_request(self):
        settings = make_settings(
            note_providers=("gemini",), gemini_api_key="k", note_structured_output=True
        )
        route = note_routes(settings)[0]
        _url, _headers, payload = llm._build_request_for(
            route, settings, "S", "U", schema=self.schema
        )
        changed = llm._adjust_rejected_request_param(
            payload, "400: Unknown name 'responseSchema' at 'generation_config'"
        )
        self.assertEqual(changed, "generationConfig.responseSchema")
        self.assertNotIn("responseSchema", payload["generationConfig"])
        # The JSON mime type stays: the answer must still be a JSON object.
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")

    def test_openrouter_throughput_routing_is_opt_in_and_openrouter_only(self):
        off = make_settings(
            note_providers=("openrouter",), openrouter_api_key="k"
        )
        route = note_routes(off)[0]
        _url, _headers, payload = llm._build_request_for(route, off, "S", "U")
        self.assertNotIn("provider", payload)

        on = make_settings(
            note_providers=("openrouter",),
            openrouter_api_key="k",
            note_openrouter_throughput=True,
        )
        route = note_routes(on)[0]
        _url, _headers, payload = llm._build_request_for(route, on, "S", "U")
        self.assertEqual(payload["provider"], {"sort": "throughput"})

        # A generic OpenAI-compatible gateway never receives an OpenRouter field.
        generic = make_settings(note_openrouter_throughput=True)
        route = note_routes(generic)[0]
        _url, _headers, payload = llm._build_request_for(route, generic, "S", "U")
        self.assertNotIn("provider", payload)

    def test_the_compile_provider_moves_to_the_front_without_dropping_backups(self):
        settings = make_settings(
            note_providers=("openai_compatible", "nara"),
            nara_api_key="nara-key",
            note_compile_provider="nara",
        )
        routes = note_routes(settings)
        ordered = llm._ordered_routes(routes, "nara")
        self.assertEqual([route.slug for route in ordered], ["nara", "openai_compatible"])
        # An unknown or empty slug changes nothing.
        self.assertEqual(llm._ordered_routes(routes, ""), routes)
        self.assertEqual(llm._ordered_routes(routes, "gemini"), routes)


class RequestBuildingTests(unittest.TestCase):
    def test_openai_compatible(self):
        settings = make_settings()
        url, headers, payload = _build_request(settings, "SYS", "USER")
        self.assertEqual(url, "https://note.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer note-key")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["content"], "USER")
        self.assertNotIn("response_format", payload)  # opt-in only
        self.assertNotIn("reasoning_effort", payload)  # provider default unless set

    def test_openai_compatible_uses_the_configured_output_budget(self):
        settings = make_settings(note_max_output_tokens=2048)
        _url, _headers, payload = _build_request(settings, "SYS", "USER")
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertEqual(payload["temperature"], llm.NOTE_TEMPERATURE)
        self.assertNotIn("max_completion_tokens", payload)

    def test_reasoning_effort_sent_on_verified_gateways(self):
        settings = routed_settings(note_reasoning_effort="low")
        nara, openrouter = note_routes(settings)
        for route in (nara, openrouter):
            _url, _headers, payload = _build_request(settings, "S", "U", route=route)
            self.assertEqual(payload["reasoning_effort"], "low")

    def test_reasoning_effort_never_sent_to_a_generic_gateway(self):
        """A generic gateway's model may reject the parameter outright."""
        settings = make_settings(note_reasoning_effort="low")
        _url, _headers, payload = _build_request(settings, "S", "U")
        self.assertNotIn("reasoning_effort", payload)

    def test_reasoning_effort_sent_on_the_gpt_oss_routes(self):
        """Groq and Cerebras document ``reasoning_effort`` on gpt-oss."""
        settings = routed_settings(
            note_providers=("groq", "cerebras"),
            nara_api_key="",
            openrouter_api_key="",
            groq_api_key="gsk-key",
            cerebras_api_key="csk-key",
            note_reasoning_effort="low",
        )
        groq, cerebras = note_routes(settings)
        for route in (groq, cerebras):
            url, headers, payload = _build_request(settings, "S", "U", route=route)
            self.assertEqual(payload["reasoning_effort"], "low")
            self.assertEqual(payload["model"], route.model)
            self.assertTrue(url.endswith("/chat/completions"))
            self.assertEqual(headers["Authorization"], f"Bearer {route.api_key}")

    def test_gemini_thinking_budget_is_zero_by_default(self):
        settings = make_settings(
            note_providers=("gemini",),
            note_provider="",
            note_api_key="",
            note_api_base_url="",
            gemini_api_key="g-key",
        )
        route = note_routes(settings)[0]
        url, _headers, payload = _build_request(settings, "S", "U", route=route)
        self.assertIn(f"models/{DEFAULT_GEMINI_MODEL}:generateContent", url)
        self.assertEqual(
            payload["generationConfig"]["thinkingConfig"], {"thinkingBudget": 0}
        )
        # The rest of the generation config is untouched.
        self.assertEqual(
            payload["generationConfig"]["responseMimeType"], "application/json"
        )

    def test_minus_one_thinking_budget_sends_no_thinking_config(self):
        settings = make_settings(
            note_providers=("gemini",),
            note_provider="",
            note_api_key="",
            note_api_base_url="",
            gemini_api_key="g-key",
            gemini_thinking_budget=-1,
        )
        _url, _headers, payload = _build_request(settings, "S", "U")
        self.assertNotIn("thinkingConfig", payload["generationConfig"])

    def test_positive_thinking_budget_is_sent_verbatim(self):
        settings = make_settings(
            note_providers=("gemini",),
            note_provider="",
            note_api_key="",
            note_api_base_url="",
            gemini_api_key="g-key",
            gemini_thinking_budget=24576,
        )
        _url, _headers, payload = _build_request(settings, "S", "U")
        self.assertEqual(
            payload["generationConfig"]["thinkingConfig"]["thinkingBudget"], 24576
        )

    def test_gemini_route_never_carries_reasoning_effort(self):
        settings = make_settings(
            note_providers=("gemini",),
            note_provider="",
            note_api_key="",
            note_api_base_url="",
            gemini_api_key="g-key",
            note_reasoning_effort="low",
        )
        _url, _headers, payload = _build_request(settings, "S", "U")
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("reasoning_effort", payload["generationConfig"])

    def test_reasoning_effort_not_invented_for_gemini_style(self):
        settings = make_settings(
            note_provider="gemini", note_api_key="g-key", note_api_base_url="",
            note_reasoning_effort="low",
        )
        _url, _headers, payload = _build_request(settings, "S", "U")
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("reasoning_effort", payload["generationConfig"])

    def test_openai_json_mode_opt_in(self):
        settings = make_settings(note_json_mode=True)
        _url, _headers, payload = _build_request(settings, "S", "U")
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_gemini_native_json_and_key_header(self):
        settings = make_settings(
            note_provider="gemini", note_api_key="g-key", note_api_base_url=""
        )
        url, headers, payload = _build_request(settings, "SYS", "USER")
        self.assertIn(":generateContent", url)
        self.assertIn("models/test-model", url)
        self.assertEqual(headers["x-goog-api-key"], "g-key")
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(
            payload["generationConfig"]["maxOutputTokens"], settings.note_max_output_tokens
        )
        self.assertEqual(
            payload["generationConfig"]["temperature"], llm.NOTE_TEMPERATURE
        )
        self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "SYS")

    def test_gemini_models_prefix_not_doubled(self):
        settings = make_settings(
            note_provider="gemini", note_api_key="k",
            note_model="models/gemini-2.5-flash",
        )
        url, _headers, _payload = _build_request(settings, "S", "U")
        self.assertNotIn("models/models", url)

    def test_anthropic_headers_and_system(self):
        settings = make_settings(note_provider="anthropic", note_api_key="a-key")
        url, headers, payload = _build_request(settings, "SYS", "USER")
        self.assertTrue(url.endswith("/messages"))
        self.assertEqual(headers["x-api-key"], "a-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(payload["system"], "SYS")
        self.assertEqual(payload["max_tokens"], settings.note_max_output_tokens)
        self.assertEqual(payload["temperature"], llm.NOTE_TEMPERATURE)

    def test_missing_key_is_auth_error(self):
        settings = make_settings(note_provider="gemini", note_api_key="")
        with self.assertRaises(LLMAuthError):
            _build_request(settings, "S", "U")


class ResponseParsingTests(unittest.TestCase):
    def test_openai_content_string(self):
        payload = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
        self.assertEqual(_parse_response(payload, "openai_compatible"), "hello")

    def test_openai_content_parts(self):
        payload = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
        self.assertEqual(_parse_response(payload, "openai_compatible"), "ab")

    def test_gemini_parts(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "x"}, {"text": "y"}]}}]}
        self.assertEqual(_parse_response(payload, "gemini"), "xy")

    def test_gemini_blocked(self):
        payload = {"promptFeedback": {"blockReason": "SAFETY"}}
        with self.assertRaises(LLMResponseError):
            _parse_response(payload, "gemini")

    def test_anthropic_text_parts(self):
        payload = {"content": [{"type": "text", "text": "one"}, {"type": "tool_use"}]}
        self.assertEqual(_parse_response(payload, "anthropic"), "one")

    def test_malformed_is_response_error(self):
        with self.assertRaises(LLMResponseError):
            _parse_response({"choices": []}, "openai_compatible")

    def test_empty_is_response_error(self):
        payload = {"choices": [{"message": {"content": "   "}}]}
        with self.assertRaises(LLMResponseError):
            _parse_response(payload, "openai_compatible")


class UsageSummaryTests(unittest.TestCase):
    def test_openai_style_usage(self):
        payload = {
            "model": "served-model",
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        }
        self.assertEqual(_usage_summary(payload, "openai"), ("served-model", 120, 30, 150))

    def test_partial_usage_stays_unknown(self):
        payload = {"model": "google/gemma-4-31b-it:free", "usage": {"prompt_tokens": 5}}
        model, prompt, completion, total = _usage_summary(payload, "openai")
        self.assertEqual(model, "google/gemma-4-31b-it:free")
        self.assertEqual(prompt, 5)
        self.assertIsNone(completion)
        self.assertIsNone(total)

    def test_gemini_usage_metadata_and_model_version(self):
        payload = {
            "modelVersion": "gemini-2.5-flash",
            "usageMetadata": {
                "promptTokenCount": 10, "candidatesTokenCount": 4, "totalTokenCount": 14,
            },
        }
        self.assertEqual(_usage_summary(payload, "gemini"), ("gemini-2.5-flash", 10, 4, 14))

    def test_anthropic_total_is_derived(self):
        payload = {"model": "claude-x", "usage": {"input_tokens": 7, "output_tokens": 3}}
        self.assertEqual(_usage_summary(payload, "anthropic"), ("claude-x", 7, 3, 10))

    def test_missing_or_bool_counts_stay_unknown(self):
        payload = {"usage": {"prompt_tokens": True, "completion_tokens": "9"}}
        self.assertEqual(_usage_summary(payload, "openai"), ("", None, None, None))
        self.assertEqual(_usage_summary({}, "openai"), ("", None, None, None))


class ExtractJsonTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json_object('{"a": 1}'), '{"a": 1}')

    def test_prose_wrapped(self):
        text = 'Here you go:\n{"a": 1}\nHope that helps!'
        self.assertEqual(extract_json_object(text), '{"a": 1}')

    def test_code_fence(self):
        text = '```json\n{"a": 1}\n```'
        self.assertEqual(extract_json_object(text), '{"a": 1}')

    def test_nested_braces_in_strings(self):
        text = '{"a": "has } brace", "b": {"c": 2}} trailing'
        self.assertEqual(extract_json_object(text), '{"a": "has } brace", "b": {"c": 2}}')

    def test_no_object_raises(self):
        with self.assertRaises(LLMResponseError):
            extract_json_object("no json here")

    def test_unterminated_raises(self):
        with self.assertRaises(LLMResponseError):
            extract_json_object('{"a": 1')


def routed_settings(**overrides):
    """Settings whose only eligible note providers are Nara and OpenRouter.

    The default order and both providers' documented API shapes come from
    ``gamas_bot.config``; nothing here is inferred from a provider name.
    """
    base = {
        "note_providers": ("nara", "openrouter"),
        "note_provider": "",
        "note_api_key": "",
        "note_api_base_url": "",
        "nara_api_key": "nara-key",
        "openrouter_api_key": "or-key",
    }
    base.update(overrides)
    return make_settings(**base)


def chat_answer(text: str, status: int = 200) -> "FakeResponse":
    return FakeResponse(status, {"choices": [{"message": {"content": text}}]})


def context_overflow(status: int = 400) -> "FakeResponse":
    return FakeResponse(status, {"error": {
        "message": "This model's maximum context length is 8192 tokens",
        "code": "context_length_exceeded",
    }})


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return _AsyncCM(response)

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return _AsyncCM(response)


class _AsyncCM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return False


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    async def read(self):
        return json.dumps(self._payload).encode()

    async def json(self, content_type=None):
        return self._payload


class InvalidJsonResponse(FakeResponse):
    async def json(self, content_type=None):
        raise json.JSONDecodeError("invalid JSON", "<body>", 0)


class CompleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_400_invalid_key_is_classified_as_auth_error(self):
        response = FakeResponse(400, {
            "error": {
                "code": 400,
                "message": "API key not valid. Please pass a valid API key.",
                "status": "INVALID_ARGUMENT",
                "details": [{"reason": "API_KEY_INVALID"}],
            }
        })
        error = await llm._error_from_response("Gemini", response, "secret-key")
        self.assertIsInstance(error, LLMAuthError)
        self.assertIn("API_KEY_INVALID", str(error))
        self.assertNotIn("secret-key", str(error))

    async def test_success(self):
        session = FakeSession([FakeResponse(200, {
            "model": "served-model",
            "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        })])
        settings = make_settings()
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "answer")
        # The served model and its real token counts reach the operator log;
        # the answer text itself never does.
        self.assertTrue(any("model=served-model" in line for line in logs.output), logs.output)
        self.assertTrue(any("input_tokens=120" in line for line in logs.output), logs.output)
        self.assertTrue(any("output_tokens=30" in line for line in logs.output), logs.output)
        self.assertFalse(any("content=answer" in line for line in logs.output), logs.output)
        self.assertEqual(len(session.calls), 1)
        timeout = session.calls[0]["timeout"]
        self.assertEqual(timeout.total, settings.note_request_timeout)

    async def test_served_model_cannot_forge_a_log_line(self):
        """The served-model string is provider-controlled; it is sanitised."""
        session = FakeSession([FakeResponse(200, {
            "model": "evil\n[INFO] forged entry",
            "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })])
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            result = await complete(session, make_settings(), system="S", user="U")
        self.assertEqual(result, "answer")
        self.assertFalse(any("\n" in line for line in logs.output), logs.output)
        self.assertTrue(any("model=evil" in line for line in logs.output), logs.output)

    async def test_network_error_is_retried(self):
        session = FakeSession([
            aiohttp.ClientConnectionError("connection reset"),
            FakeResponse(200, {"choices": [{"message": {"content": "recovered"}}]}),
        ])
        settings = make_settings()
        with mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "recovered")
        self.assertEqual(len(session.calls), 2)

    async def test_renames_rejected_max_tokens_to_max_completion_tokens(self):
        rejected = FakeResponse(400, {"error": {
            "message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."
        }})
        session = FakeSession([rejected, chat_answer("recovered")])
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            result = await complete(session, make_settings(), system="S", user="U")
        self.assertEqual(result, "recovered")
        self.assertEqual(len(session.calls), 2)
        request_logs = [line for line in logs.output if "LLM request sending" in line]
        self.assertIn("attempt=1", request_logs[0])
        self.assertIn("attempt=2", request_logs[1])
        retried_payload = session.calls[-1]["json"]
        self.assertEqual(retried_payload["max_completion_tokens"], 4096)
        self.assertNotIn("max_tokens", retried_payload)

    async def test_drops_temperature_when_model_explicitly_rejects_it(self):
        rejected = FakeResponse(400, {"error": {
            "message": "Unsupported parameter: 'temperature' is not supported with this model."
        }})
        session = FakeSession([rejected, chat_answer("recovered")])
        result = await complete(session, make_settings(), system="S", user="U")
        self.assertEqual(result, "recovered")
        self.assertEqual(len(session.calls), 2)
        retried_payload = session.calls[-1]["json"]
        self.assertNotIn("temperature", retried_payload)
        self.assertEqual(retried_payload["max_tokens"], 4096)

    async def test_gemini_rejected_generation_options_are_removed_in_sequence(self):
        settings = make_settings(
            note_providers=("gemini",),
            note_provider="gemini",
            note_api_key="",
            note_api_base_url="",
            gemini_api_key="gemini-key",
        )
        session = FakeSession([
            FakeResponse(400, {"error": {
                "message": "temperature is not supported for this model"
            }}),
            FakeResponse(400, {"error": {
                "message": "response_mime_type application/json is not supported"
            }}),
            FakeResponse(200, {"candidates": [{
                "content": {"parts": [{"text": '{"ok": true}'}]}
            }]}),
        ])
        result = await complete_json(session, settings, system="S", user="U")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(session.calls), 3)
        generation_config = session.calls[-1]["json"]["generationConfig"]
        self.assertNotIn("temperature", generation_config)
        self.assertNotIn("responseMimeType", generation_config)
        self.assertEqual(generation_config["maxOutputTokens"], 4096)

    async def test_rejected_thinking_config_is_dropped_and_retried_once(self):
        """A model that predates ``thinkingConfig`` costs exactly one retry."""
        settings = make_settings(
            note_providers=("gemini",),
            note_provider="",
            note_api_key="",
            note_api_base_url="",
            gemini_api_key="gemini-key",
        )
        session = FakeSession([
            FakeResponse(400, {"error": {
                "message": "Unknown name \"thinkingConfig\": Cannot find field."
            }}),
            FakeResponse(200, {"candidates": [{
                "content": {"parts": [{"text": '{"ok": true}'}]}
            }]}),
        ])
        result = await complete_json(session, settings, system="S", user="U")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(session.calls), 2)
        # ``FakeSession`` records the live payload object, which the retry path
        # mutates in place — so the surviving payload is the adjusted one.
        self.assertNotIn("thinkingConfig", session.calls[1]["json"]["generationConfig"])
        # Everything else about the retried request is unchanged.
        self.assertEqual(
            session.calls[1]["json"]["generationConfig"]["responseMimeType"],
            "application/json",
        )

    def test_thinking_config_is_kept_without_a_rejection_marker(self):
        """Only an explicit rejection drops the field; a vague error keeps it."""
        payload = {"generationConfig": {
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        }}
        adjustment = llm._adjust_rejected_request_param(
            payload, "thinkingConfig backend hiccup"
        )
        self.assertIsNone(adjustment)
        self.assertIn("thinkingConfig", payload["generationConfig"])

    def test_thinking_budget_error_variants_are_recognized(self):
        for error_text in (
            "Unsupported parameter: thinking_budget",
            "Unknown field thinkingBudget",
            "thinking_config is not supported for this model",
        ):
            payload = {"generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}}
            adjustment = llm._adjust_rejected_request_param(payload, error_text)
            self.assertEqual(adjustment, "generationConfig.thinkingConfig", error_text)
            self.assertNotIn("thinkingConfig", payload["generationConfig"])

    async def test_bad_success_body_is_response_error(self):
        session = FakeSession([InvalidJsonResponse(200)])
        with self.assertRaises(LLMResponseError):
            await complete(session, make_settings(), system="S", user="U")

    async def test_transient_retry_then_success(self):
        session = FakeSession([
            FakeResponse(503, {}),
            FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
        ])
        settings = make_settings()
        with mock.patch("gamas_bot.llm.MAX_TRANSIENT_RETRIES", 2), \
             mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "ok")

    async def test_http_408_is_retried(self):
        session = FakeSession([
            FakeResponse(408, {}),
            FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
        ])
        with mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            result = await complete(session, make_settings(), system="S", user="U")
        self.assertEqual(result, "ok")
        self.assertEqual(len(session.calls), 2)

    async def test_retry_after_zero_is_respected(self):
        delays = []

        async def record_sleep(delay):
            delays.append(delay)

        session = FakeSession([
            FakeResponse(429, {}, headers={"Retry-After": "0"}),
            FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
        ])
        with mock.patch("asyncio.sleep", new=record_sleep):
            result = await complete(session, make_settings(), system="S", user="U")
        self.assertEqual(result, "ok")
        self.assertEqual(delays, [0.0])

    async def test_auth_error_no_retry(self):
        session = FakeSession([FakeResponse(401, {"error": {"message": "bad"}})])
        settings = make_settings()
        with self.assertRaises(LLMAuthError):
            await complete(session, settings, system="S", user="U")
        self.assertEqual(len(session.calls), 1)

    async def test_error_detail_never_leaks_key(self):
        session = FakeSession([
            FakeResponse(400, {"error": {"message": "key note-key rejected"}})
        ])
        settings = make_settings()
        with self.assertRaises(LLMResponseError) as ctx:
            await complete(session, settings, system="S", user="U")
        self.assertNotIn("note-key", str(ctx.exception))


class CompleteJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_json_first_try(self):
        session = FakeSession([FakeResponse(200, {
            "choices": [{"message": {"content": '{"title": "جزوه"}'}}]
        })])
        settings = make_settings()
        payload = await complete_json(session, settings, system="S", user="U")
        self.assertEqual(payload["title"], "جزوه")

    async def test_one_retry_on_bad_json(self):
        session = FakeSession([
            FakeResponse(200, {"choices": [{"message": {"content": "not json"}}]}),
            FakeResponse(200, {"choices": [{"message": {"content": '{"ok": true}'}}]}),
        ])
        settings = make_settings()
        payload = await complete_json(session, settings, system="S", user="U")
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(session.calls), 2)

    async def test_second_failure_raises(self):
        session = FakeSession([
            FakeResponse(200, {"choices": [{"message": {"content": "nope"}}]}),
            FakeResponse(200, {"choices": [{"message": {"content": "still nope"}}]}),
        ])
        settings = make_settings()
        with self.assertRaises(LLMResponseError):
            await complete_json(session, settings, system="S", user="U")

    async def test_empty_model_answer_is_retried(self):
        session = FakeSession([
            FakeResponse(200, {"choices": [{"message": {"content": "   "}}]}),
            FakeResponse(200, {"choices": [{"message": {"content": '{"ok": true}'}}]}),
        ])
        result = await complete_json(session, make_settings(), system="S", user="U")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(session.calls), 2)

    async def test_non_json_nan_is_retried(self):
        session = FakeSession([
            FakeResponse(200, {"choices": [{"message": {"content": '{"value": NaN}'}}]}),
            FakeResponse(200, {"choices": [{"message": {"content": '{"ok": true}'}}]}),
        ])
        result = await complete_json(session, make_settings(), system="S", user="U")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(session.calls), 2)

    async def test_provider_400_does_not_trigger_json_retry(self):
        session = FakeSession([FakeResponse(400, {"error": {"message": "bad request"}})])
        with self.assertRaises(LLMResponseError):
            await complete_json(session, make_settings(), system="S", user="U")
        self.assertEqual(len(session.calls), 1)


def nara_error(
    status: int, error_type: str, message: str, request_id: str = "req_1"
) -> "FakeResponse":
    """NaraRouter's single documented error envelope.

    ``{"error": {"type", "message", "request_id"}}`` — see
    https://router.bynara.id/docs ("Errors"). The gateway has no ``code`` and
    no ``status`` field, so ``type`` is the only machine-readable signal.
    """
    return FakeResponse(status, {
        "error": {"type": error_type, "message": message, "request_id": request_id}
    })


def truncated_answer(text: str = "") -> "FakeResponse":
    """A 200 whose answer stopped at the output budget (``finish_reason``)."""
    return FakeResponse(200, {
        "choices": [{"message": {"content": text}, "finish_reason": "length"}]
    })


class NaraRouterContractTests(unittest.IsolatedAsyncioTestCase):
    """The documented NaraRouter chat-completions contract, end to end.

    Every expectation is a documented one (https://router.bynara.id/docs):
    the endpoint and Bearer key, the request fields, the success shape, the
    one error envelope, the status-code table, and the reasoning models'
    output-budget behaviour.
    """

    async def test_request_matches_the_documented_shape(self):
        settings = routed_settings()
        nara = note_routes(settings)[0]
        url, headers, payload = _build_request(settings, "SYS", "USER", route=nara)
        self.assertEqual(url, "https://router.bynara.id/v1/chat/completions")
        self.assertEqual(headers, {"Authorization": "Bearer nara-key"})
        # Only fields the gateway documents; anything else risks a 400.
        self.assertEqual(sorted(payload), ["max_tokens", "messages", "model", "temperature"])
        self.assertEqual(payload["model"], "agnes-2.5-flash")

    async def test_default_model_is_on_the_published_free_plan(self):
        # https://router.bynara.id/api/plans lists agnes-2.5-flash on `free`.
        self.assertEqual(DEFAULT_NARA_MODEL, "agnes-2.5-flash")

    async def test_response_format_is_never_sent_to_nara(self):
        settings = routed_settings(note_json_mode=True)
        nara = note_routes(settings)[0]
        _url, _headers, payload = _build_request(settings, "S", "U", route=nara)
        self.assertNotIn("response_format", payload)

    async def test_reasoning_effort_reaches_nara_only_when_configured(self):
        """NaraRouter documents ``reasoning_effort``; unset keeps its default."""
        settings = routed_settings(note_reasoning_effort="none")
        nara = note_routes(settings)[0]
        _url, _headers, payload = _build_request(settings, "S", "U", route=nara)
        self.assertEqual(payload["reasoning_effort"], "none")

    async def test_documented_success_shape_parses(self):
        session = FakeSession([FakeResponse(200, {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "agnes-2.5-flash",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 18, "completion_tokens": 8, "total_tokens": 26},
        })])
        result = await complete(session, routed_settings(), system="S", user="U")
        self.assertEqual(result, "Hello!")

    async def test_documented_status_codes_map_to_the_right_class(self):
        expected = {
            400: LLMResponseError,    # validation_error
            401: LLMAuthError,        # unauthorized
            403: LLMAuthError,        # forbidden: the plan lacks the model
            404: LLMResponseError,    # not_found
            415: LLMResponseError,    # unsupported_media_type
            429: LLMTransientError,   # rate_limited
            500: LLMTransientError,   # internal_error
            503: LLMTransientError,   # service_unavailable
        }
        for status, klass in expected.items():
            error = await llm._error_from_response(
                "NaraRouter", nara_error(status, "some_type", "m"), None
            )
            self.assertIsInstance(error, klass, f"HTTP {status} -> {type(error).__name__}")
            self.assertNotIsInstance(error, LLMContextOverflowError, f"HTTP {status}")

    async def test_413_oversized_body_is_a_context_overflow(self):
        """413 is NaraRouter's documented "the request body or input is too large"."""
        error = await llm._error_from_response(
            "NaraRouter",
            nara_error(413, "bad_request", "The request body or input is too large."),
            None,
        )
        self.assertIsInstance(error, LLMContextOverflowError)

    async def test_413_without_context_wording_is_still_an_overflow(self):
        """The status alone classifies it; no message wording is required."""
        error = await llm._error_from_response(
            "NaraRouter", nara_error(413, "bad_request", "Payload rejected."), None
        )
        self.assertIsInstance(error, LLMContextOverflowError)

    async def test_request_id_reaches_the_operator(self):
        error = await llm._error_from_response(
            "NaraRouter",
            nara_error(429, "rate_limited", "Rate limit exceeded.", "req_abc123"),
            None,
        )
        self.assertIn("request_id=req_abc123", str(error))

    async def test_request_id_never_carries_the_key(self):
        body = FakeResponse(429, {"error": {
            "type": "rate_limited", "message": "slow down", "request_id": "nara-key"}})
        error = await llm._error_from_response("NaraRouter", body, "nara-key")
        self.assertNotIn("nara-key", str(error))

    def test_reasoning_starvation_reports_the_output_budget(self):
        """Empty content + ``finish_reason: length`` is the documented case where
        thinking spent the whole ``max_tokens`` budget."""
        payload = {
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "length",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 512},
        }
        with self.assertRaises(LLMOutputBudgetError):
            _parse_response(payload, "openai")

    def test_truncation_is_detected_in_every_request_style(self):
        self.assertTrue(llm._output_truncated({"choices": [{"finish_reason": "length"}]}, "openai"))
        self.assertTrue(llm._output_truncated({"candidates": [{"finishReason": "MAX_TOKENS"}]}, "gemini"))
        self.assertTrue(llm._output_truncated({"stop_reason": "max_tokens"}, "anthropic"))
        self.assertFalse(llm._output_truncated({"choices": [{"finish_reason": "stop"}]}, "openai"))
        self.assertFalse(llm._output_truncated({}, "openai"))

    async def test_a_truncated_answer_is_not_retried_as_bad_json(self):
        """The JSON-reminder retry cannot fix a budget cut, so it is not sent."""
        session = FakeSession([truncated_answer('{"title": "cu'), chat_answer('{"ok": true}')])
        payload = await complete_json(session, routed_settings(), system="S", user="U")
        self.assertEqual(payload, {"ok": True})
        # One request per provider: Nara is left behind, OpenRouter answers.
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["url"], "https://router.bynara.id/v1/chat/completions")
        self.assertEqual(session.calls[1]["url"], "https://openrouter.ai/api/v1/chat/completions")

    async def test_an_empty_answer_without_truncation_is_still_retried(self):
        """A blank answer with no ``length`` signal keeps its one recovery retry."""
        session = FakeSession([chat_answer("   "), chat_answer('{"ok": true}')])
        payload = await complete_json(session, routed_settings(), system="S", user="U")
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            [call["url"] for call in session.calls],
            ["https://router.bynara.id/v1/chat/completions"] * 2,
        )


class NoteFailoverTests(unittest.IsolatedAsyncioTestCase):
    """Nara first, OpenRouter second, bounded retries, no silent retries."""

    async def test_nara_answers_first_with_its_own_documented_request(self):
        session = FakeSession([chat_answer("nara-answer")])
        result = await complete(session, routed_settings(), system="S", user="U")
        self.assertEqual(result, "nara-answer")
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], "https://router.bynara.id/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer nara-key")
        self.assertEqual(call["json"]["model"], "agnes-2.5-flash")

    async def test_auth_failure_moves_on_to_openrouter(self):
        session = FakeSession([
            FakeResponse(401, {"error": {"message": "invalid api key"}}),
            chat_answer("openrouter-answer"),
        ])
        result = await complete(session, routed_settings(), system="S", user="U")
        self.assertEqual(result, "openrouter-answer")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            session.calls[1]["url"], "https://openrouter.ai/api/v1/chat/completions"
        )
        self.assertEqual(session.calls[1]["headers"]["Authorization"], "Bearer or-key")

    async def test_context_overflow_fails_over_to_the_next_provider(self):
        session = FakeSession([context_overflow(), chat_answer("smaller")])
        result = await complete(session, routed_settings(), system="S", user="U")
        self.assertEqual(result, "smaller")
        self.assertEqual(len(session.calls), 2)

    async def test_every_provider_failing_keeps_the_first_error_class(self):
        session = FakeSession([context_overflow(), context_overflow()])
        with self.assertRaises(LLMContextOverflowError) as caught:
            await complete(session, routed_settings(), system="S", user="U")
        message = str(caught.exception)
        self.assertIn("NaraRouter", message)
        self.assertIn("OpenRouter", message)
        self.assertNotIn("nara-key", message)
        # One attempt per provider: nothing is re-sent unchanged.
        self.assertEqual(len(session.calls), 2)

    async def test_a_transient_failure_is_retried_on_the_same_provider(self):
        session = FakeSession([FakeResponse(503, {}), chat_answer("recovered")])
        with mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            result = await complete(session, routed_settings(), system="S", user="U")
        self.assertEqual(result, "recovered")
        self.assertEqual(
            [call["url"] for call in session.calls],
            ["https://router.bynara.id/v1/chat/completions"] * 2,
        )

    async def test_a_permanent_rejection_is_not_retried_per_provider(self):
        bad = FakeResponse(400, {"error": {"message": "unsupported parameter"}})
        session = FakeSession([bad, FakeResponse(400, {"error": {"message": "unsupported parameter"}})])
        with self.assertRaises(LLMResponseError):
            await complete(session, routed_settings(), system="S", user="U")
        self.assertEqual(len(session.calls), 2)

    async def test_a_timeout_moves_on_instead_of_repeating_the_same_request(self):
        """A provider that missed the deadline is left behind, not asked again.

        Re-sending the identical request only waits for the same deadline a
        second time, while the next provider in the chain may answer in
        seconds — so a timeout must not consume the whole retry budget.
        """
        session = FakeSession([asyncio.TimeoutError(), chat_answer("second-answer")])
        with mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            result = await complete(session, routed_settings(), system="S", user="U")
        self.assertEqual(result, "second-answer")
        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://router.bynara.id/v1/chat/completions",
                "https://openrouter.ai/api/v1/chat/completions",
            ],
        )

    async def test_the_last_provider_still_retries_a_timeout(self):
        """With no successor left, the bounded retry budget still applies."""
        session = FakeSession([asyncio.TimeoutError(), chat_answer("late-answer")])
        with mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            result = await complete(
                session, routed_settings(note_providers=("nara",)), system="S", user="U"
            )
        self.assertEqual(result, "late-answer")
        self.assertEqual(len(session.calls), 2)

    async def test_a_timeout_reports_the_deadline_and_the_knobs(self):
        # One provider only: the timeout is retried within the bounded budget
        # and the final failure names the deadline and both knobs.
        session = FakeSession([asyncio.TimeoutError()] * 3)
        settings = routed_settings(
            note_providers=("nara",), note_request_timeout=120.0
        )
        with mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            with self.assertRaises(LLMTransientError) as caught:
                await complete(session, settings, system="S", user="U")
        message = str(caught.exception)
        self.assertIn("120", message)
        self.assertIn("NOTE_REQUEST_TIMEOUT_SECONDS", message)
        self.assertIn("NOTE_MAX_OUTPUT_TOKENS", message)

    async def test_the_configured_deadline_is_used_for_the_request(self):
        session = FakeSession([chat_answer("answer")])
        settings = routed_settings(note_request_timeout=900.0)
        await complete(session, settings, system="S", user="U")
        self.assertEqual(session.calls[0]["timeout"].total, 900.0)

    async def test_a_settings_object_without_a_deadline_uses_the_fallback(self):
        session = FakeSession([chat_answer("answer")])
        settings = routed_settings(note_request_timeout=0.0)
        await complete(session, settings, system="S", user="U")
        self.assertEqual(
            session.calls[0]["timeout"].total, llm.LLM_REQUEST_TIMEOUT_SECONDS
        )

    async def test_a_timeout_in_json_mode_also_moves_on(self):
        session = FakeSession([asyncio.TimeoutError(), chat_answer('{"ok": true}')])
        payload = await complete_json(
            session, routed_settings(), system="S", user="U"
        )
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(session.calls), 2)

    async def test_json_mode_is_only_sent_where_it_is_documented(self):
        settings = routed_settings(note_json_mode=True)
        nara, openrouter = note_routes(settings)
        _url, _headers, nara_payload = _build_request(settings, "S", "U", route=nara)
        self.assertNotIn("response_format", nara_payload)
        _url, _headers, router_payload = _build_request(settings, "S", "U", route=openrouter)
        self.assertEqual(router_payload["response_format"], {"type": "json_object"})

    async def test_malformed_json_fails_over_after_one_retry(self):
        session = FakeSession([
            chat_answer("not json"),
            chat_answer("still not json"),
            chat_answer('{"ok": true}'),
        ])
        payload = await complete_json(session, routed_settings(), system="S", user="U")
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(session.calls), 3)

    async def test_nothing_configured_calls_no_provider(self):
        settings = make_settings(
            note_providers=("nara", "openrouter"),
            note_provider="",
            note_api_key="",
            note_api_base_url="",
        )
        session = FakeSession([])
        with self.assertRaises(LLMAuthError):
            await complete(session, settings, system="S", user="U")
        self.assertEqual(session.calls, [])


class OpenRouterAndGeminiHardeningTests(unittest.IsolatedAsyncioTestCase):
    """Hardened GET/POST contracts for NaraRouter, OpenRouter and Gemini."""

    def test_get_request_building_across_providers(self):
        settings = make_settings(
            note_providers=("nara", "openrouter", "gemini"),
            note_provider="",
            note_api_key="",
            note_api_base_url="",
            nara_api_key="Bearer sk-nry-123",
            openrouter_api_key="sk-or-v1-456",
            gemini_api_key="AIzaSyGem789",
        )
        nara, openrouter, gemini = note_routes(settings)

        # NaraRouter GET /v1/models and public GET /api/plans
        nara_models_url, nara_headers = llm._build_get_request_for(nara, "models")
        self.assertEqual(nara_models_url, "https://router.bynara.id/v1/models")
        self.assertEqual(nara_headers, {"Authorization": "Bearer sk-nry-123"})

        nara_plans_url, nara_plans_headers = llm._build_get_request_for(nara, "plans")
        self.assertEqual(nara_plans_url, "https://router.bynara.id/api/plans")
        self.assertEqual(nara_plans_headers, {})

        # OpenRouter GET /api/v1/models/<author>/<slug>/endpoints and /api/v1/key
        or_ep_url, or_headers = llm._build_get_request_for(openrouter, "model_endpoints")
        self.assertEqual(
            or_ep_url,
            "https://openrouter.ai/api/v1/models/google/gemma-4-31b-it:free/endpoints",
        )
        self.assertEqual(or_headers, {"Authorization": "Bearer sk-or-v1-456"})

        or_key_url, _ = llm._build_get_request_for(openrouter, "key")
        self.assertEqual(or_key_url, "https://openrouter.ai/api/v1/key")

        # Gemini GET /v1beta/models/<model> and GET /v1beta/models
        gem_model_url, gem_headers = llm._build_get_request_for(gemini, "model")
        self.assertEqual(
            gem_model_url,
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + DEFAULT_GEMINI_MODEL,
        )
        self.assertEqual(gem_headers, {"x-goog-api-key": "AIzaSyGem789"})

        gem_list_url, _ = llm._build_get_request_for(gemini, "models")
        self.assertEqual(
            gem_list_url,
            "https://generativelanguage.googleapis.com/v1beta/models",
        )

    async def test_get_json_retries_transient_failure_and_parses_payload(self):
        settings = routed_settings()
        nara = note_routes(settings)[0]
        session = FakeSession([
            FakeResponse(503, {"error": {"type": "service_unavailable", "message": "busy"}}),
            FakeResponse(200, {"data": [{"id": "agnes-2.5-flash"}]}),
        ])
        with mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            payload = await llm.get_json(session, nara, "models")
        self.assertEqual(payload["data"][0]["id"], "agnes-2.5-flash")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["url"], "https://router.bynara.id/v1/models")

    async def test_openrouter_http_200_top_level_in_body_error_is_classified(self):
        """OpenRouter can send 200 OK headers then return a top-level error object."""
        session = FakeSession([
            FakeResponse(200, {
                "error": {
                    "code": 503,
                    "message": "Provider returned error",
                    "metadata": {
                        "error_type": "provider_overloaded",
                        "raw": '{"error": {"message": "Upstream capacity exhausted"}}',
                    },
                }
            }),
            chat_answer("recovered-from-200-error"),
        ])
        with mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            result = await complete(session, routed_settings(), system="S", user="U")
        self.assertEqual(result, "recovered-from-200-error")
        self.assertEqual(len(session.calls), 2)

    async def test_openrouter_http_200_context_overflow_in_body_raises_overflow(self):
        session = FakeSession([
            FakeResponse(200, {
                "error": {
                    "code": 400,
                    "message": "Provider returned error",
                    "metadata": {"error_type": "token_limit_exceeded"},
                }
            }),
            FakeResponse(200, {
                "error": {
                    "code": 400,
                    "message": "Provider returned error",
                    "metadata": {"error_type": "max_tokens_exceeded"},
                }
            }),
        ])
        with self.assertRaises(LLMContextOverflowError):
            await complete(session, routed_settings(), system="S", user="U")

    async def test_openrouter_http_200_choice_finish_reason_error_is_not_accepted(self):
        """Partial content with finish_reason='error' must never be accepted as valid text."""
        session = FakeSession([
            FakeResponse(200, {
                "choices": [{
                    "message": {"content": '{"title": "partial broken'},
                    "finish_reason": "error",
                    "error": {"code": 502, "message": "Provider disconnected mid-stream"},
                }]
            }),
            chat_answer('{"ok": true}'),
        ])
        with mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            result = await complete_json(session, routed_settings(), system="S", user="U")
        self.assertEqual(result, {"ok": True})

    async def test_openrouter_402_in_flight_budget_is_transient_unlike_credit_depletion(self):
        in_flight_402 = FakeResponse(
            402,
            {
                "error": {
                    "code": 402,
                    "message": "In-flight reservation limit reached",
                    "metadata": {
                        "error_type": "payment_required",
                        "reason": "in_flight_budget_exhausted",
                        "limit_source": "openrouter_in_flight_budget",
                    },
                }
            },
            headers={"Retry-After": "1.5"},
        )
        transient_err = await llm._error_from_response("OpenRouter", in_flight_402, "or-key")
        self.assertIsInstance(transient_err, LLMTransientError)
        self.assertEqual(transient_err.retry_after_seconds, 1.5)

        depleted_402 = FakeResponse(
            402,
            {
                "error": {
                    "code": 402,
                    "message": "Insufficient credits",
                    "metadata": {"error_type": "payment_required"},
                }
            },
        )
        auth_err = await llm._error_from_response("OpenRouter", depleted_402, "or-key")
        self.assertIsInstance(auth_err, LLMAuthError)

    async def test_openrouter_403_content_policy_is_request_error_not_auth_error(self):
        mod_403 = FakeResponse(
            403,
            {
                "error": {
                    "code": 403,
                    "message": "Prompt flagged by moderation",
                    "metadata": {"error_type": "content_policy_violation"},
                }
            },
        )
        err = await llm._error_from_response("OpenRouter", mod_403, "or-key")
        self.assertIsInstance(err, llm.LLMRequestError)
        self.assertNotIsInstance(err, LLMAuthError)

    async def test_gemini_retry_info_and_prepay_402_vs_429_resource_exhausted(self):
        # Gemini 402 RESOURCE_EXHAUSTED is depleted prepay balance -> LLMAuthError (no retry)
        gem_402 = FakeResponse(402, {
            "error": {
                "code": 402,
                "message": "Your Prepay credit balance is depleted.",
                "status": "RESOURCE_EXHAUSTED",
            }
        })
        err_402 = await llm._error_from_response("Gemini", gem_402, "g-key")
        self.assertIsInstance(err_402, LLMAuthError)

        # Gemini 429 RESOURCE_EXHAUSTED with google.rpc.RetryInfo -> LLMTransientError with delay
        gem_429 = FakeResponse(429, {
            "error": {
                "code": 429,
                "message": "Resource has been exhausted (e.g. check quota).",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "12.5s",
                    }
                ],
            }
        })
        err_429 = await llm._error_from_response("Gemini", gem_429, "g-key")
        self.assertIsInstance(err_429, LLMTransientError)
        self.assertEqual(err_429.retry_after_seconds, 12.5)

    def test_gemini_thinking_parts_are_excluded_from_answer_text(self):
        payload = {
            "candidates": [{
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "Internal thought summary that is not JSON", "thought": True},
                        {"text": '{"title": "جزوهٔ نهایی"}'},
                    ],
                },
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 50,
                "candidatesTokenCount": 20,
                "thoughtsTokenCount": 80,
                "totalTokenCount": 150,
            },
        }
        self.assertEqual(_parse_response(payload, "gemini"), '{"title": "جزوهٔ نهایی"}')

    def test_gemini_max_tokens_without_parts_raises_output_budget_error(self):
        """When Gemini 2.5 spends maxOutputTokens on thinking, content.parts is omitted."""
        payload = {
            "candidates": [{
                "content": {"role": "model"},
                "finishReason": "MAX_TOKENS",
            }],
            "usageMetadata": {"promptTokenCount": 20, "thoughtsTokenCount": 512},
        }
        with self.assertRaises(LLMOutputBudgetError):
            _parse_response(payload, "gemini")

    def test_gemini_safety_finish_reason_without_parts_raises_response_error(self):
        payload = {
            "candidates": [{
                "finishReason": "SAFETY",
            }]
        }
        with self.assertRaises(LLMResponseError) as ctx:
            _parse_response(payload, "gemini")
        self.assertIn("SAFETY", str(ctx.exception))

    def test_openrouter_reasoning_token_exhaustion_without_length_reason_raises_budget_error(self):
        payload = {
            "choices": [{
                "message": {"role": "assistant", "content": "", "reasoning": "thinking..."},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 512,
                "completion_tokens_details": {"reasoning_tokens": 512},
            },
        }
        with self.assertRaises(LLMOutputBudgetError):
            _parse_response(payload, "openai")

    async def test_optional_response_format_is_downgraded_when_rejected_by_model(self):
        settings = make_settings(
            note_providers=("openrouter",),
            note_provider="",
            note_api_key="",
            note_api_base_url="",
            openrouter_api_key="or-key",
            note_json_mode=True,
        )
        session = FakeSession([
            FakeResponse(400, {
                "error": {
                    "code": 400,
                    "message": "response_format json_object is not supported by this model",
                }
            }),
            chat_answer('{"ok": true}'),
        ])
        result = await complete_json(session, settings, system="S", user="U")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(session.calls), 2)
        self.assertNotIn("response_format", session.calls[1]["json"])

    def test_probe_script_checks_nara_openrouter_and_gemini(self):
        from scripts import probe_note_providers as probe

        settings = make_settings(
            note_providers=("nara", "openrouter", "gemini"),
            note_provider="",
            note_api_key="",
            note_api_base_url="",
            nara_api_key="nara-secret",
            openrouter_api_key="or-secret",
            gemini_api_key="gem-secret",
        )
        nara, openrouter, gemini = note_routes(settings)

        with mock.patch.object(
            probe,
            "_get_json",
            return_value=(200, {"data": [{"id": "agnes-2.5-flash", "reasoning": True}]}),
        ):
            self.assertTrue(probe._check_nara(nara))

        with mock.patch.object(
            probe,
            "_get_json",
            side_effect=[
                (200, {"data": {"endpoints": [{
                    "provider_name": "Google AI Studio",
                    "context_length": 262144,
                    "max_completion_tokens": 32768,
                    "uptime_last_1d": 99.9,
                }]}}),
                (200, {"data": {
                    "is_free_tier": True,
                    "free_model_daily_requests": {"used": 1, "limit": 50, "remaining": 49},
                }}),
            ],
        ):
            self.assertTrue(probe._check_openrouter(openrouter))

        with mock.patch.object(
            probe,
            "_get_json",
            return_value=(200, {
                "name": "models/gemini-2.5-flash",
                "inputTokenLimit": 1048576,
                "outputTokenLimit": 65536,
                "supportedGenerationMethods": ["generateContent", "countTokens"],
                "thinking": True,
            }),
        ):
            self.assertTrue(probe._check_gemini(gemini))

    async def test_probe_reports_an_unusable_context_budget_as_a_failure(self):
        from scripts import probe_note_providers as probe

        settings = make_settings(note_context_tokens=8192, note_max_output_tokens=8192)
        route = note_routes(make_settings())[0]
        self.assertFalse(await probe._generate(route, settings, "متن آزمایشی"))

    async def test_null_error_envelope_on_200_does_not_trigger_false_positive(self):
        session = FakeSession([
            FakeResponse(200, {
                "error": {"message": None, "code": None},
                "choices": [{"message": {"content": "clean answer"}, "finish_reason": "stop"}],
            })
        ])
        result = await complete(session, make_settings(), system="S", user="U")
        self.assertEqual(result, "clean answer")

    def test_openai_refusal_and_content_filter_without_message_raise_response_error(self):
        with self.assertRaises(LLMResponseError) as refusal_ctx:
            _parse_response(
                {"choices": [{"message": {"content": None, "refusal": "Cannot comply"}, "finish_reason": "stop"}]},
                "openai",
            )
        self.assertIn("refusal", str(refusal_ctx.exception))

        with self.assertRaises(LLMResponseError) as filter_ctx:
            _parse_response(
                {"choices": [{"finish_reason": "content_filter"}]},
                "openai",
            )
        self.assertIn("content_filter", str(filter_ctx.exception))


async def _noop():
    return None


if __name__ == "__main__":
    unittest.main()
