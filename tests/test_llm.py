"""Tests for the single LLM client."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import aiohttp


from gamas_bot import llm
from gamas_bot.config import note_routes
from gamas_bot.llm import (
    LLMAuthError,
    LLMContextOverflowError,
    LLMResponseError,
    _build_request,
    _endpoint,
    _parse_response,
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
        self.calls.append({"url": url, **kwargs})
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
            "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}]
        })])
        settings = make_settings()
        result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "answer")
        self.assertEqual(len(session.calls), 1)
        timeout = session.calls[0]["timeout"]
        self.assertEqual(timeout.total, llm.LLM_REQUEST_TIMEOUT_SECONDS)

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


async def _noop():
    return None


if __name__ == "__main__":
    unittest.main()
