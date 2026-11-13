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

from telethon.errors import FloodWaitError, MessageIdInvalidError

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
from gamas_bot.llm_profiles import to_gemini_schema, to_strict_openai_json_schema
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

    async def assert_route_order(self, entry, expected, *, task="", prefer="", sensitivity="STANDARD_EDUCATIONAL", **overrides):
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
        kwargs = {"system": "Synthetic instruction", "user": "Synthetic lesson", "task": task, "data_sensitivity": sensitivity}
        if entry == "complete_json":
            kwargs["prefer_provider"] = prefer
        result = await getattr(llm, entry)(session, settings, **kwargs)
        self.assertEqual(result, {"ok": True} if entry == "complete_json" else text)
        self.assertEqual([self.HOSTS[urlsplit(c["url"]).hostname] for c in session.calls], list(expected))
        # Each endpoint gets only its own credential; the removed slot's key
        # cannot leak through either preference or classification.
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
            (("BOOKLET_GENERATION", "BOOKLET_REPAIR", "BOOKLET_COMPILE", "QUIZ_20", "QUIZ_DIFFICULT"),
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

    async def test_unknown_content_never_uses_openrouter_even_with_sensitive_opt_in(self):
        for entry in ("complete", "complete_json"):
            for label in ("UNKNOWN", "unrecognized-label"):
                with self.subTest(entry=entry, sensitivity=label):
                    await self.assert_route_order(
                        entry, ("gemini", "avalai"), sensitivity=label, prefer="openrouter",
                        openrouter_free_allow_sensitive_data=True,
                    )

    async def test_sensitive_content_requires_individual_provider_approval(self):
        flags = {"avalai": "avalai_allow_sensitive_data", "gemini": "gemini_free_allow_sensitive_data",
                 "openrouter": "openrouter_free_allow_sensitive_data"}
        for entry in ("complete", "complete_json"):
            for label in ("INTERNAL", "SENSITIVE", "PHI_OR_PII"):
                session = _FakeHTTPSession([])
                with self.subTest(entry=entry, label=label, approved="none"), self.assertRaises(llm.LLMAuthError):
                    await getattr(llm, entry)(session, self.settings(), system="S", user="Synthetic lesson", data_sensitivity=label)
                self.assertEqual(session.calls, [])
                for provider, flag in flags.items():
                    with self.subTest(entry=entry, label=label, approved=provider):
                        await self.assert_route_order(entry, (provider,), sensitivity=label, **{flag: True})

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

    def test_gemini_schema_preserves_required_strips_additional_properties_and_handles_nullable(self) -> None:
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
            },
            "required": ["title", "nonexistent"],
            "additionalProperties": False,
        }
        converted = to_gemini_schema(raw)
        self.assertEqual(converted["type"], "OBJECT")
        self.assertNotIn("additionalProperties", converted)
        self.assertEqual(converted["required"], ["title"])
        self.assertEqual(converted["properties"]["title"]["type"], "STRING")
        self.assertTrue(converted["properties"]["title"]["nullable"])
        self.assertNotIn("pattern", converted["properties"]["title"])
        self.assertNotIn("minItems", converted["properties"]["items"])
        self.assertNotIn("maxItems", converted["properties"]["items"])

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
        self.assertNotIn("responseSchema", payload["generationConfig"])
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")

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
            openrouter_model="google/gemma-4-31b-it:free",
            note_structured_output=True,
        )
        free_route = note_routes(free_settings)[0]
        _u, _h, free_payload = llm._build_request_for(
            free_route, free_settings, "S", "U", schema=QUIZ_SCHEMA
        )
        self.assertEqual(free_payload["response_format"], {"type": "json_object"})

        paid_settings = make_settings(
            note_providers=("openrouter",),
            openrouter_api_key="or-key",
            openrouter_model="google/gemini-2.5-flash",
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

    async def test_render_curation_retries_short_flood_wait_and_falls_back_on_deleted_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(db_path=root / "cur.db", temp_dir=root / "tmp")
            settings.temp_dir.mkdir(parents=True, exist_ok=True)
            db = Database(settings.db_path)
            await db.connect()
            client = FakeTelegramClient()
            bot = GamasBot(settings, db=db, client=client)
            try:
                bot._curation[77] = {
                    "kind": "deck",
                    "items": [{"front": "پ۱", "back": "ج۱", "source": "بخش ۱"}],
                    "page": 0,
                    "title": "درس",
                    "requested": 1,
                }
                flaky_msg = mock.AsyncMock()
                flood = FloodWaitError(request=None, capture=1)
                flaky_msg.edit = mock.AsyncMock(side_effect=[flood, None])
                with mock.patch("gamas_bot.bot_learning.asyncio.sleep", new=mock.AsyncMock()):
                    ok = await bot._render_curation(flaky_msg, 77)
                self.assertTrue(ok)
                self.assertEqual(flaky_msg.edit.await_count, 2)

                gone_msg = mock.AsyncMock()
                gone_msg.edit = mock.AsyncMock(side_effect=MessageIdInvalidError(request=None))
                ok_fallback = await bot._render_curation(gone_msg, 77)
                self.assertTrue(ok_fallback)
                gone_msg.respond.assert_awaited_once()
                self.assertIn("پیش‌نمایش فلش‌کارت", gone_msg.respond.await_args.args[0])
            finally:
                await bot.shutdown()


if __name__ == "__main__":
    unittest.main()
