"""Regression tests for 2026 LLM provider contracts, SSE streaming, grounding, and learning."""

from __future__ import annotations

import copy
import json
import random
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest import mock

from gamas_bot import llm, office_ai
from gamas_bot.bot import GamasBot
from gamas_bot.config import NoteRoute, note_routes
from gamas_bot.db import Database
from gamas_bot.doc_extract import DocChunk
from gamas_bot.learning import (
    FLASHCARD_SCHEMA,
    QUIZ_SCHEMA,
    parse_flashcard_payload,
    parse_quiz_payload,
)
from gamas_bot.llm import extract_json_object
from gamas_bot.llm_profiles import (
    MODEL_PROFILES,
    ModelCapabilityProfile,
    get_model_profile,
    normalize_model_id,
    register_model_profile,
    to_gemini_response_format_schema,
    to_strict_openai_json_schema,
)
from gamas_bot.notes import (
    COVERAGE_FILL_SYSTEM_PROMPT,
    NoteSection,
    StructuredNotes,
    build_fill_document,
    fill_coverage_gaps,
)
from gamas_bot.notes_evidence import audit_notes, extract_signals, mask_structural
from gamas_bot.office_notes import (
    COMPACT_NOTE_SCHEMA,
    OfficeSourceChunk,
    _full_result_is_grounded,
    _parse_compact_payload,
)
from tests.support import FakeTelegramClient, make_settings


class _ChunkedStreamContent:
    """Fake ``aiohttp.StreamReader`` exposing ``iter_chunked`` for >64 KiB lines."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeHTTPResponse:
    def __init__(
        self,
        status: int = 200,
        *,
        chunks: list[bytes] | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        if chunks is not None:
            self.content = _ChunkedStreamContent(chunks)
            self._raw = b"".join(chunks)
        else:
            raw = json.dumps(json_body or {}, ensure_ascii=False).encode("utf-8")
            self.content = _ChunkedStreamContent([raw])
            self._raw = raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def read(self) -> bytes:
        return self._raw


class _FakeHTTPSession:
    def __init__(self, responses: list[_FakeHTTPResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(
        self,
        url: str,
        *,
        headers: dict,
        json: dict,
        timeout=None,
        allow_redirects=None,
    ):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "json": json,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        if not self._responses:
            raise AssertionError("unexpected HTTP post call")
        return self._responses.pop(0)


class ApprovedRoutingEntryPointTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real dispatch and request building, not just ordering helpers."""

    HOSTS = {
        "api.avalai.ir": "avalai", "generativelanguage.googleapis.com": "gemini",
        "openrouter.ai": "openrouter",
    }

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.case_index = 0

    def settings(self, **overrides):
        self.case_index += 1
        values = {
            "db_path": self.root / f"route-{self.case_index}.sqlite3",
            "note_providers": ("openrouter", "gemini", "nara", "avalai", "deepseek"),
            "note_provider": "anthropic",
            "note_api_key": "obsolete-slot-key",
            "note_api_base_url": "https://obsolete.example.test/v1",
            "avalai_api_key": "av-test", "gemini_api_key": "g-test", "openrouter_api_key": "or-test",
        }
        values.update(overrides)
        return make_settings(**values)

    async def assert_route_order(self, entry, expected, *, task="", prefer="", **overrides):
        settings = self.settings(**overrides)
        text = '{"ok": true}'
        if expected[-1] == "gemini":
            answer = {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}
        else:
            answer = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
        responses = [
            _FakeHTTPResponse(400, json_body={"error": {"message": "synthetic input rejected"}})
            for _ in expected[:-1]
        ] + [_FakeHTTPResponse(200, json_body=answer)]
        session = _FakeHTTPSession(responses)
        kwargs = {"system": "Synthetic instruction", "user": "Synthetic lesson", "task": task}
        if entry == "complete_json":
            kwargs["prefer_provider"] = prefer
        result = await getattr(llm, entry)(session, settings, **kwargs)
        self.assertEqual(result, {"ok": True} if entry == "complete_json" else text)
        self.assertEqual([self.HOSTS[urlsplit(c["url"]).hostname] for c in session.calls], list(expected))
        # Each endpoint gets only its own credential; the removed slot's key
        # cannot leak through provider preferences or routing.
        for call in session.calls:
            slug = self.HOSTS[urlsplit(call["url"]).hostname]
            headers = call["headers"]
            self.assertNotIn("obsolete-slot-key", str(headers))
            if slug == "gemini":
                self.assertEqual(headers["x-goog-api-key"], "g-test")
            else:
                self.assertEqual(headers["Authorization"], f"Bearer {'av-test' if slug == 'avalai' else 'or-test'}")

    async def test_task_priorities_override_reversed_chain_and_primary_preferences(self):
        families = (
            (("BOOKLET_GENERATION", "BOOKLET_REPAIR", "BOOKLET_VERIFY", "BOOKLET_COMPILE", "BOOKLET_REVIEW_PLAN", "QUIZ_20", "QUIZ_DIFFICULT"),
             ("avalai", "gemini", "openrouter"), "gemini"),
            (("KEY_POINTS", "COMPACT_NOTES", "QUIZ_GENERATION", "FLASHCARD_GENERATION", "OFFICE_TABLE_GENERATION",
              "OFFICE_SLIDE_PLANNING", "OFFICE_DOCUMENT_GENERATION"),
             ("gemini", "avalai", "openrouter"), "avalai"),
        )
        for tasks, expected, contrary_preference in families:
            for task in tasks:
                with self.subTest(entry="complete", task=task):
                    await self.assert_route_order("complete", expected, task=task)
                for preference in ("openrouter", contrary_preference):
                    with self.subTest(entry="complete_json", task=task, preference=preference):
                        await self.assert_route_order("complete_json", expected, task=task, prefer=preference)

    async def test_unknown_task_and_removed_preferences_never_promote_openrouter_or_add_routes(self):
        for preference in ("", "openrouter", "nara", "deepseek", "groq", "cerebras", "anthropic", "openai_compatible"):
            with self.subTest(preference=preference):
                await self.assert_route_order(
                    "complete_json", ("gemini", "avalai", "openrouter"), task="custom_task", prefer=preference,
                )
        await self.assert_route_order("complete_json", ("avalai", "gemini", "openrouter"), prefer="avalai")
        await self.assert_route_order("complete", ("gemini", "avalai", "openrouter"))

    async def test_all_configured_routes_remain_eligible_for_content(self):
        for entry in ("complete", "complete_json"):
            with self.subTest(entry=entry):
                await self.assert_route_order(
                    entry,
                    ("avalai", "gemini", "openrouter"),
                    task="BOOKLET_GENERATION",
                )

    async def test_removed_programmatic_slots_cannot_build_or_send_requests(self):
        for slug in ("nara", "deepseek", "groq", "cerebras", "anthropic", "openai_compatible"):
            settings = self.settings(
                note_provider=slug, note_providers=(slug,),
                avalai_api_key="", gemini_api_key="", openrouter_api_key="",
            )
            for entry in ("complete", "complete_json"):
                session = _FakeHTTPSession([])
                kwargs = {"prefer_provider": slug} if entry == "complete_json" else {}
                with self.subTest(slug=slug, entry=entry), self.assertRaises(llm.LLMAuthError):
                    await getattr(llm, entry)(session, settings, system="S", user="Synthetic lesson", **kwargs)
                self.assertEqual(session.calls, [])
            route = NoteRoute(slug=slug, label="Removed", style="openai", base_url="https://obsolete.example.test/v1",
                              api_key="obsolete-slot-key", model="synthetic", json_object_mode=True)
            with self.subTest(slug=slug, entry="request builder"), self.assertRaisesRegex(ValueError, "Unapproved"):
                llm._build_request_for(route, settings, "S", "Synthetic lesson")


class ProviderDialectContractTests(unittest.TestCase):
    """Approved Gemini, AvalAI and OpenRouter contracts; all HTTP is scripted."""

    def test_model_profiles_registry_contains_strictly_the_three_active_models(self) -> None:
        self.assertEqual(
            set(MODEL_PROFILES.keys()),
            {
                "gemini-3.5-flash-lite",
                "deepseek-v4.1-flash",
                "nvidia/nemotron-3.5-super-vl:free",
            },
        )
        gem = get_model_profile("gemini-3.5-flash-lite", provider_slug="gemini")
        self.assertEqual((gem.context_window, gem.max_output_tokens), (1_048_576, 65_536))
        self.assertFalse(gem.send_sampling_params)
        self.assertEqual(gem.gemini_response_format_style, "response_format")
        self.assertEqual(gem.gemini_thinking_style, "thinking_level")

        av = get_model_profile("deepseek-v4.1-flash", provider_slug="avalai")
        self.assertEqual((av.context_window, av.max_output_tokens), (1_000_000, 393_216))
        self.assertEqual(av.reasoning_param_style, "none")
        self.assertEqual(av.structured_output_mode, "json_schema")
        self.assertTrue(av.supports_prompt_caching)

        or_m = get_model_profile("nvidia/nemotron-3.5-super-vl:free", provider_slug="openrouter")
        self.assertEqual((or_m.context_window, or_m.max_output_tokens), (1_000_000, 131_072))
        self.assertEqual(or_m.reasoning_param_style, "openrouter_reasoning")
        self.assertEqual(or_m.structured_output_mode, "json_object")

    def test_register_model_profile_extends_wire_behavior_without_editing_llm(self) -> None:
        custom_id = "vendor/future-reasoning-model"
        try:
            register_model_profile(
                ModelCapabilityProfile(
                    model_id=custom_id,
                    provider_slug="avalai",
                    context_window=256_000,
                    max_output_tokens=8_192,
                    supports_system_prompt=False,
                    send_sampling_params=False,
                    reasoning_param_style="reasoning_effort",
                    structured_output_mode="json_object",
                )
            )
            settings = make_settings(
                note_providers=("avalai",),
                avalai_api_key="av-test",
                avalai_model=custom_id,
                note_max_output_tokens=32_768,
                note_reasoning_effort="medium",
                note_structured_output=True,
            )
            route = note_routes(settings)[0]
            _url, _headers, payload = llm._build_request_for(
                route, settings, "SYS", "USER", schema=QUIZ_SCHEMA
            )
            self.assertEqual(payload["max_tokens"], 8_192)
            self.assertNotIn("temperature", payload)
            self.assertEqual(payload["reasoning_effort"], "medium")
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertEqual(len(payload["messages"]), 1)
            self.assertEqual(payload["messages"][0]["role"], "user")
            self.assertIn("SYS", payload["messages"][0]["content"])
        finally:
            MODEL_PROFILES.pop(normalize_model_id(custom_id), None)

    def test_gemini_response_format_schema_keeps_json_schema_type_dialect(self) -> None:
        raw = {
            "type": "object",
            "properties": {
                "title": {"type": ["string", "null"], "pattern": "^.+$"},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": {"type": "string"},
                },
                "ignored": {"type": "boolean"},
            },
            "required": ["title", "missing"],
            "additionalProperties": False,
        }
        converted = to_gemini_response_format_schema(raw)
        self.assertEqual(converted["type"], "object")
        self.assertEqual(converted["properties"]["title"]["type"], ["string", "null"])
        self.assertNotIn("pattern", converted["properties"]["title"])
        self.assertNotIn("minItems", converted["properties"]["items"])
        self.assertNotIn("maxItems", converted["properties"]["items"])
        self.assertEqual(converted["required"], ["title"])
        self.assertNotIn("additionalProperties", converted)

    def test_shared_schema_transform_keeps_required_tags_and_does_not_mutate_input(self) -> None:
        for schema in (QUIZ_SCHEMA, FLASHCARD_SCHEMA):
            original = copy.deepcopy(schema)
            transformed = to_strict_openai_json_schema(schema)["schema"]
            key = "questions" if schema is QUIZ_SCHEMA else "cards"
            item = transformed["properties"][key]["items"]
            self.assertIn("source", item["required"])
            if key == "questions":
                self.assertIn("difficulty", item["required"])
            self.assertFalse(item["additionalProperties"])
            self.assertEqual(schema, original)

    def test_gemini_omits_response_schema_when_structured_output_setting_is_disabled(self) -> None:
        settings = make_settings(
            note_providers=("gemini",),
            gemini_api_key="gem-key",
            note_structured_output=False,
        )
        route = note_routes(settings)[0]
        _url, _headers, payload = llm._build_request_for(
            route, settings, "S", "U", schema=QUIZ_SCHEMA
        )
        generation = payload["generationConfig"]
        self.assertNotIn("schema", generation["responseFormat"]["text"])
        self.assertEqual(
            generation["responseFormat"]["text"]["mimeType"], "APPLICATION_JSON"
        )
        self.assertNotIn("temperature", generation)

    def test_avalai_sends_documented_json_schema_with_a_json_object_fallback(self) -> None:
        """AvalAI documents ``json_schema``; a refusal degrades, never fails.

        https://docs.avalai.ir/en/guides/structured-outputs — the gateway warns
        that support is per-model, and the request builder already downgrades a
        rejected ``json_schema`` to ``json_object`` before dropping the field.
        """
        settings = make_settings(
            note_providers=("avalai",),
            avalai_api_key="av-test",
            avalai_model="deepseek-v4.1-flash",
            note_structured_output=True,
        )
        route = note_routes(settings)[0]
        url, headers, payload = llm._build_request_for(
            route, settings, "S", "U", schema=COMPACT_NOTE_SCHEMA
        )
        self.assertEqual(url, "https://api.avalai.ir/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer av-test")
        self.assertEqual(payload["model"], "deepseek-v4.1-flash")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(
            payload["response_format"]["json_schema"]["name"],
            "gamas_notes",
        )
        # The documented degradation ladder: a refused schema becomes
        # ``json_object``, and a refused ``json_object`` is dropped — so an
        # unsupported model still answers instead of failing the booklet.
        rejected = {
            "model": payload["model"],
            "response_format": payload["response_format"],
        }
        adjustment = llm._adjust_rejected_request_param(
            rejected, "unsupported parameter: response_format.json_schema"
        )
        self.assertEqual(adjustment, "response_format.json_schema_to_json_object")
        self.assertEqual(rejected["response_format"], {"type": "json_object"})

        dropped = {"model": payload["model"], "response_format": {"type": "json_object"}}
        adjustment = llm._adjust_rejected_request_param(
            dropped, "response_format is not supported for this model"
        )
        self.assertEqual(adjustment, "response_format")
        self.assertNotIn("response_format", dropped)

    def test_openrouter_free_model_avoids_json_schema_routing_404_and_downgrades_gracefully(self) -> None:
        free_settings = make_settings(
            note_providers=("openrouter",),
            openrouter_api_key="or-key",
            openrouter_model="nvidia/nemotron-3.5-super-vl:free",
            note_structured_output=True,
            note_reasoning_effort="low",
        )
        free_route = note_routes(free_settings)[0]
        _u, _h, free_payload = llm._build_request_for(
            free_route, free_settings, "S", "U", schema=QUIZ_SCHEMA
        )
        self.assertEqual(free_payload["response_format"], {"type": "json_object"})
        self.assertEqual(free_payload["reasoning"], {"effort": "low", "exclude": True})

        # Rejection of unified ``reasoning`` (e.g. mandatory reasoning or 404 filter)
        # removes only ``reasoning`` cleanly.
        mandatory_payload = copy.deepcopy(free_payload)
        adj_mandatory = llm._adjust_rejected_request_param(
            mandatory_payload,
            "HTTP 400: Reasoning is mandatory for this endpoint and cannot be disabled.",
        )
        self.assertEqual(adj_mandatory, "reasoning")
        self.assertNotIn("reasoning", mandatory_payload)

        paid_settings = make_settings(
            note_providers=("openrouter",),
            openrouter_api_key="or-key",
            openrouter_model="nvidia/nemotron-3.5-super-vl",
            note_structured_output=True,
        )
        paid_route = note_routes(paid_settings)[0]
        _u2, _h2, paid_payload = llm._build_request_for(
            paid_route, paid_settings, "S", "U", schema=QUIZ_SCHEMA
        )
        self.assertEqual(paid_payload["response_format"]["type"], "json_schema")
        adj1 = llm._adjust_rejected_request_param(
            paid_payload,
            "404: No endpoints found that can handle the requested parameters.",
        )
        self.assertEqual(adj1, "response_format.json_schema_to_json_object")
        self.assertEqual(paid_payload["response_format"], {"type": "json_object"})

    def test_openrouter_reasoning_details_and_cached_usage_are_detected(self) -> None:
        exhausted = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_details": [{"type": "reasoning.text", "text": "thinking..."}],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 500,
                "prompt_tokens_details": {"cached_tokens": 300},
                "completion_tokens": 200,
                "completion_tokens_details": {"reasoning_tokens": 200},
                "total_tokens": 700,
            },
        }
        with self.assertRaises(llm.LLMOutputBudgetError):
            llm._parse_response(exhausted, "openai")
        self.assertEqual(llm._usage_detail_counts(exhausted, "openai"), (300, 200))

        avalai_cached = {
            "usage": {
                "prompt_tokens": 1000,
                "prompt_cache_hit_tokens": 800,
                "completion_tokens": 100,
                "total_tokens": 1100,
            }
        }
        self.assertEqual(llm._usage_detail_counts(avalai_cached, "openai"), (800, None))

    def test_explicit_numeric_output_cap_error_clamps_budget(self) -> None:
        payload = {"model": "x", "max_tokens": 16_384}
        adj = llm._adjust_rejected_request_param(
            payload,
            "400: Invalid value for 'max_tokens': max_tokens must be less than or equal to 8192",
        )
        self.assertEqual(adj, "max_tokens_clamped")
        self.assertEqual(payload["max_tokens"], 8192)


class StreamingAndExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_stream_handles_single_line_over_64kib_without_chunk_too_big(self) -> None:
        settings = make_settings(avalai_api_key="avalai-test-key", note_ttfb_timeout=10.0)
        route = note_routes(settings)[0]
        big_text = "الف" * 40_000  # >80 KiB UTF-8 in one single SSE data line
        sse_line = (
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"content": big_text}, "finish_reason": "stop"}]},
                ensure_ascii=False,
            )
            + "\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        # Feed in 16 KiB slices just like aiohttp iter_chunked(16384).
        slices = [sse_line[i : i + 16384] for i in range(0, len(sse_line), 16384)]
        session = _FakeHTTPSession([_FakeHTTPResponse(200, chunks=slices)])
        result = await llm._complete_on_route(
            route, session, settings, system="S", user="U"
        )
        self.assertEqual(result, big_text)

    async def test_sse_stream_falls_back_when_gateway_returns_plain_json_body(self) -> None:
        settings = make_settings(
            note_providers=("avalai",),
            avalai_api_key="avalai-test-key",
            note_ttfb_timeout=10.0,
        )
        route = note_routes(settings)[0]
        plain_json = {
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        session = _FakeHTTPSession([_FakeHTTPResponse(200, json_body=plain_json)])
        result = await llm._complete_on_route(
            route, session, settings, system="S", user="U"
        )
        self.assertEqual(result, '{"ok": true}')
        # AvalAI never receives stream_options when its profile documents none.
        self.assertNotIn("stream_options", session.calls[0]["json"])

    async def test_avalai_stream_logs_its_request_id_for_cost_reconciliation(self) -> None:
        settings = make_settings(
            note_providers=("avalai",),
            avalai_api_key="av-test",
            note_ttfb_timeout=10.0,
        )
        route = note_routes(settings)[0]
        events = [
            b'data: {"model":"deepseek-v4.1-flash","choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}\n\n',
            b"data: [DONE]\n\n",
        ]
        session = _FakeHTTPSession([
            _FakeHTTPResponse(
                200,
                chunks=events,
                headers={"AvalAI-Request-Id": "avalai_stream_123"},
            )
        ])
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            result = await llm._complete_on_route(
                route, session, settings, system="S", user="U"
            )
        self.assertEqual(result, "answer")
        usage_lines = [line for line in logs.output if "note provider usage" in line]
        self.assertEqual(len(usage_lines), 1, logs.output)
        self.assertIn("request_id=avalai_stream_123", usage_lines[0])
        self.assertFalse(session.calls[0]["allow_redirects"])
        self.assertEqual(
            session.calls[0]["timeout"].sock_connect,
            llm.LLM_CONNECT_TIMEOUT_SECONDS,
        )

    async def test_rejected_stream_options_retries_without_stream_options(self) -> None:
        """A gateway that rejects ``stream_options`` gets one bounded retry.

        OpenRouter is the shipped provider whose profile documents
        ``stream_options``; AvalAI's profile does not, so this retry ladder is
        exercised on the route that sends the field.
        """
        settings = make_settings(
            openrouter_api_key="or-test-key",
            note_providers=("openrouter",),
            note_ttfb_timeout=10.0,
        )
        route = note_routes(settings)[0]
        err_resp = _FakeHTTPResponse(
            400,
            json_body={"error": {"message": "Unknown parameter: 'stream_options'"}},
        )
        ok_sse = [
            b'data: {"choices":[{"delta":{"content":"{\\"a\\":1}"},"finish_reason":"stop"}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        session = _FakeHTTPSession([err_resp, _FakeHTTPResponse(200, chunks=ok_sse)])
        result = await llm._complete_on_route(
            route, session, settings, system="S", user="U"
        )
        self.assertEqual(result, '{"a":1}')
        self.assertEqual(len(session.calls), 2)
        self.assertIn("stream_options", session.calls[0]["json"])
        self.assertNotIn("stream_options", session.calls[1]["json"])

    def test_extract_json_object_strips_think_tags_and_skips_prose_set_braces(self) -> None:
        raw_with_think = (
            "<think>Let S = {1, 2, 3} be our set.\nWe need valid JSON.</think>\n"
            "در ریاضی مجموعهٔ {a, b} را در نظر بگیرید:\n"
            '```json\n{"sections": [{"heading": "مجموعه‌ها"}]}\n```'
        )
        extracted = extract_json_object(raw_with_think)
        self.assertEqual(json.loads(extracted), {"sections": [{"heading": "مجموعه‌ها"}]})

        orphan_close = 'Reasoning without open tag {x, y}</think>{"cards": []}'
        self.assertEqual(json.loads(extract_json_object(orphan_close)), {"cards": []})


class GroundingAndOfficeFidelityTests(unittest.IsolatedAsyncioTestCase):
    def test_mask_structural_strips_pdf_page_markers_and_split_counters(self) -> None:
        source = "[صفحهٔ ۳ (۱/۲)]\nدوز متفورمین ۵۰۰ میلی‌گرم است."
        masked = mask_structural(source)
        self.assertNotIn("۳", masked)
        signals = extract_signals(source)
        numbers = {s.value for s in signals if s.kind == "number"}
        self.assertEqual(numbers, {"500"})

    def test_audit_notes_inspects_bare_note_section_dataclass_and_catches_fabrication(self) -> None:
        page_one = "فشار خون بیمار ۱۲۰ میلی‌متر جیوه بود."
        bad_section = NoteSection(
            heading="معاینه",
            paragraphs=("فشار خون بیمار ۱۸۵ میلی‌متر جیوه بود.",),
            source_locators=("صفحهٔ ۱",),
        )
        audit = audit_notes(page_one, bad_section)
        self.assertTrue(any(sig.value == "185" for sig in audit.fabricated))

    def test_coverage_fill_prompts_have_real_newlines_and_strict_gate_blocks_hallucination(self) -> None:
        self.assertNotIn("قواعد:\\n", COVERAGE_FILL_SYSTEM_PROMPT)
        self.assertNotIn("برگردان.\\n", COVERAGE_FILL_SYSTEM_PROMPT)
        fill_doc = build_fill_document(["واحد نمونه"], StructuredNotes())
        self.assertNotIn("\\n", fill_doc)

    async def test_coverage_fill_drops_ungrounded_numbers_when_strict_grounding_enabled(self) -> None:
        source = " ".join(
            f"گزارش مستقل شمارهٔ {i} دربارهٔ تعادل گرمایی و قانون اول ترمودینامیک است."
            for i in range(6)
        )
        draft = StructuredNotes(
            title="فیزیک",
            summary="خلاصه",
            sections=(NoteSection(heading="مقدمه", paragraphs=("سلام",)),),
        )
        hallucinated_payload = {
            "sections": [
                {
                    "heading": "ترمودینامیک",
                    "paragraphs": [
                        "گزارش مستقل شمارهٔ ۰ دربارهٔ تعادل گرمایی است. دمای بحرانی ۹۹۹۹ درجه است."
                    ],
                }
            ]
        }
        settings = make_settings(
            note_strict_grounding=True,
            note_completeness_fill=True,
        )
        with mock.patch(
            "gamas_bot.notes.complete_json",
            new=mock.AsyncMock(return_value=hallucinated_payload),
        ):
            merged = await fill_coverage_gaps(object(), settings, source, draft)
        merged_text = " ".join(p for s in merged.sections for p in s.paragraphs)
        self.assertNotIn("۹۹۹۹", merged_text)
        self.assertNotIn("9999", merged_text)

    def test_office_notes_allows_genuine_locator_subset_and_blocks_cross_page_fabrication(self) -> None:
        chunks = [
            OfficeSourceChunk(
                "[صفحهٔ ۱]\nعنوان جلد راهنما\n[صفحهٔ ۲]\nظرفیت سامانه ۵۰ کاربر هم‌زمان است.",
                ("صفحهٔ ۱", "صفحهٔ ۲"),
            )
        ]
        parsed = _parse_compact_payload(
            {
                "heading": "ظرفیت سامانه",
                "points": ["ظرفیت سامانه ۵۰ کاربر هم‌زمان است."],
                "source_locators": ["صفحهٔ ۲"],
            },
            source=chunks[0],
            index=1,
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.source_locators, ("صفحهٔ ۲",))

        # Cross-page hallucination: section claims صفحهٔ ۱ for a number only on صفحهٔ ۲!
        bad_booklet = StructuredNotes(
            title="راهنما",
            sections=(
                NoteSection(
                    heading="ظرفیت",
                    paragraphs=("ظرفیت سامانه ۵۰ کاربر هم‌زمان است.",),
                    source_locators=("صفحهٔ ۱",),
                ),
            ),
        )
        locator_map = {"صفحهٔ ۱": "عنوان جلد راهنما", "صفحهٔ ۲": "ظرفیت سامانه ۵۰ کاربر هم‌زمان است."}
        self.assertFalse(
            _full_result_is_grounded(
                bad_booklet,
                chunks[0].text,
                ("صفحهٔ ۱", "صفحهٔ ۲"),
                locator_map,
            )
        )

    def test_office_table_prompt_matches_verbatim_header_validator(self) -> None:
        self.assertIn("عیناً", office_ai.TABLE_SYSTEM_PROMPT)
        self.assertNotIn("می‌توانی خودت انتخاب کنی", office_ai.TABLE_SYSTEM_PROMPT)


class LearningRobustnessTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_quiz_payload_handles_aliases_dict_options_and_text_answer_keys(self) -> None:
        payload = {
            "quiz": [
                {
                    "سوال": "کدام پروتکل امن است؟",
                    "choices": {
                        "الف": "الف) HTTPS",
                        "ب": "ب) HTTP",
                        "ج": "ج) FTP",
                        "د": "د) Telnet",
                    },
                    "answer": "HTTPS",
                    "rationale": "زیرا از رمزنگاری TLS استفاده می‌کند.",
                    "سطح": "دشوار",
                    "منبع": "صفحهٔ ۴",
                }
            ]
        }
        parsed = parse_quiz_payload(payload, 5, rng=random.Random(7), require_source=True)
        self.assertEqual(len(parsed), 1)
        q = parsed[0]
        self.assertEqual(q["options"][q["correct_index"]], "HTTPS")
        self.assertEqual(q["difficulty"], "hard")
        self.assertEqual(q["source"], "صفحهٔ ۴")

    def test_parse_flashcard_payload_keeps_comparative_aya_ya_and_drops_yes_no(self) -> None:
        payload = {
            "flashcards": [
                {
                    "پرسش": "آیا آنتروپی در فرآیند برگشت‌ناپذیر افزایشی است یا کاهشی؟",
                    "پاسخ": "افزایشی است.",
                    "منبع": "بخش ۲",
                },
                {
                    "پرسش": "آیا آنتروپی کمیت حالت است؟",
                    "پاسخ": "بله",
                    "منبع": "بخش ۲",
                },
            ]
        }
        cards = parse_flashcard_payload(payload, 5, require_source=True)
        self.assertEqual(len(cards), 1)
        self.assertIn("افزایشی", cards[0]["back"])

    async def test_single_chunk_40_card_preset_batches_requests_and_passes_prior_stems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(
                db_path=root / "learn.db",
                temp_dir=root / "tmp",
                note_context_tokens=16_384,
                note_max_output_tokens=4_096,
            )
            settings.temp_dir.mkdir(parents=True, exist_ok=True)
            db = Database(settings.db_path)
            await db.connect()
            client = FakeTelegramClient()
            bot = GamasBot(settings, db=db, client=client)
            try:
                self.assertLess(bot._learning_max_chars(), 56_000)
                calls: list[dict] = []

                async def fake_complete_json(_session, _settings, *, system, user, schema=None, max_output_tokens=None, wait_for_capacity=True, task=""):
                    batch_no = len(calls)
                    calls.append({"system": system, "user": user, "schema": schema, "task": task})
                    start = batch_no * 10
                    return {
                        "cards": [
                            {
                                "front": f"مفهوم شماره {i} چیست؟",
                                "back": f"توضیح مفهوم {i}",
                                "source": "صفحهٔ ۱",
                            }
                            for i in range(start, start + 10)
                        ]
                    }

                bot.deps = bot.deps.__class__(complete_json_fn=fake_complete_json)
                chunk = DocChunk(0, "صفحهٔ ۱", ("صفحهٔ ۱",), "متن علمی کافی " * 80)
                items, failed = await bot._generate_chunks(
                    kind="deck",
                    chunks=[chunk],
                    counts=[40],
                    placeholder=mock.AsyncMock(),
                )
                self.assertEqual(failed, 0)
                self.assertEqual(len(items), 40)
                self.assertEqual(len(calls), 4)
                self.assertTrue(all(call["schema"] is not None for call in calls))
                self.assertTrue(all(call["task"] == "FLASHCARD_GENERATION" for call in calls))
                self.assertEqual(len({call["system"] for call in calls}), 1)
                self.assertIn("مفهوم شماره 0 چیست؟", calls[1]["user"])
            finally:
                await bot.shutdown()

    async def test_learning_html_delivery_failure_refunds_credit_and_stores_no_library_item(self) -> None:
        from tests.support import FakeCallbackEvent

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(
                db_path=root / "cur.db",
                temp_dir=root / "tmp",
                avalai_api_key="avalai-test-key",
            )
            settings.temp_dir.mkdir(parents=True, exist_ok=True)
            db = Database(settings.db_path)
            await db.connect()
            await db.ensure_user(77)
            await db.grant_free_plan_once(77, 1000)
            await db.consume_free_use(77, "deck")
            item_id = await db.library_add(77, "booklet", 77, 10, None, title="درس", content_text="متن کافی برای ساخت فلش‌کارت " * 30)
            client = FakeTelegramClient()
            client.send_file = mock.AsyncMock(side_effect=RuntimeError("telegram upload down"))
            bot = GamasBot(settings, db=db, client=client)
            try:
                cards = {"cards": [{"front": f"پرسش {i}", "back": f"پاسخ {i}", "source": "بخش ۱"} for i in range(6)]}
                ev = FakeCallbackEvent(client, data=f"fc:go:{item_id}".encode(), chat_id=77)
                ev.client = client
                with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=cards)):
                    await bot._handle_callback(ev)
                self.assertEqual(await db.available_seconds(77), 1000)
                self.assertEqual(await db.library_count_kind(77, "flashcards"), 0)
            finally:
                await bot.shutdown()
                await db.close()


if __name__ == "__main__":
    unittest.main()
