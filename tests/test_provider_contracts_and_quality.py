"""Regression tests for 2026 LLM provider contracts, SSE streaming, grounding, and learning."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
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
from gamas_bot.llm import (
    LLMContextOverflowError,
    LLMOutputBudgetError,
    LLMRequestError,
    extract_json_object,
)
from gamas_bot.llm_profiles import to_gemini_schema
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


class ProviderDialectContractTests(unittest.TestCase):
    """Official 2026 provider dialect contracts across Gemini, OpenRouter, Nara, OpenAI, Anthropic."""

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

    def test_deepseek_uses_documented_json_object_and_never_strict_schema(self) -> None:
        settings = make_settings(
            note_providers=("deepseek",),
            deepseek_api_key="ds-test",
            deepseek_model="deepseek-flash",
            note_structured_output=True,
        )
        route = note_routes(settings)[0]
        url, headers, payload = llm._build_request_for(
            route, settings, "S", "U", schema=COMPACT_NOTE_SCHEMA
        )
        self.assertEqual(url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer ds-test")
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("json_schema", json.dumps(payload["response_format"]))
        self.assertNotIn("stream_options", payload)

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

    def test_nara_suppresses_undocumented_response_format_and_stream_options(self) -> None:
        settings = make_settings(
            note_providers=("nara",),
            nara_api_key="nara-key",
            note_structured_output=True,
        )
        route = note_routes(settings)[0]
        _u, _h, payload = llm._build_request_for(
            route, settings, "S", "U", schema=QUIZ_SCHEMA
        )
        self.assertNotIn("response_format", payload)

    def test_direct_openai_reasoning_model_uses_max_completion_tokens_and_omits_temperature(self) -> None:
        route = NoteRoute(
            slug="openai_compatible",
            label="OpenAI",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model="o3-mini",
            style="openai",
            json_object_mode=True,
        )
        settings = make_settings(note_structured_output=True)
        _u, _h, payload = llm._build_request_for(
            route, settings, "S", "U", schema=FLASHCARD_SCHEMA
        )
        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])

    def test_anthropic_stop_reasons_and_thinking_exhaustion(self) -> None:
        dialect = llm.DIALECTS["anthropic"]
        thinking_payload = {
            "content": [{"type": "thinking", "thinking": "long reasoning..."}],
            "stop_reason": "max_tokens",
        }
        self.assertTrue(dialect.is_reasoning_exhausted(thinking_payload))
        with self.assertRaises(LLMOutputBudgetError):
            dialect.parse_response(thinking_payload)

        overflow_payload = {
            "content": [{"type": "text", "text": "partial"}],
            "stop_reason": "model_context_window_exceeded",
        }
        with self.assertRaises(LLMContextOverflowError):
            dialect.parse_response(overflow_payload)

        refusal_payload = {
            "content": [{"type": "text", "text": ""}],
            "stop_reason": "refusal",
        }
        with self.assertRaises(LLMRequestError):
            dialect.parse_response(refusal_payload)

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
        settings = make_settings(note_ttfb_timeout=10.0)
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
            note_providers=("nara",),
            nara_api_key="nara-key",
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
        # Nara must not receive stream_options.
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

    async def test_anthropic_sse_streaming_assembles_text_and_usage(self) -> None:
        route = NoteRoute(
            slug="anthropic",
            label="Anthropic",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
            model="claude-sonnet-4-5",
            style="anthropic",
            json_object_mode=False,
        )
        settings = make_settings(note_ttfb_timeout=10.0)
        events = [
            b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-5","usage":{"input_tokens":25}}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"sections\\": "}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"[]}"}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}\n\n',
        ]
        session = _FakeHTTPSession([_FakeHTTPResponse(200, chunks=events)])
        result = await llm._complete_on_route(
            route, session, settings, system="S", user="U"
        )
        self.assertEqual(result, '{"sections": []}')
        self.assertTrue(session.calls[0]["json"]["stream"])
        self.assertNotIn("stream_options", session.calls[0]["json"])

    async def test_rejected_stream_options_retries_without_stream_options(self) -> None:
        settings = make_settings(note_ttfb_timeout=10.0)
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

                async def fake_complete_json(_session, _settings, *, system, user, schema=None, max_output_tokens=None, wait_for_capacity=True):
                    batch_no = len(calls)
                    calls.append({"system": system, "user": user, "schema": schema})
                    start = batch_no * 20
                    return {
                        "cards": [
                            {
                                "front": f"مفهوم شماره {i} چیست؟",
                                "back": f"توضیح مفهوم {i}",
                                "source": "صفحهٔ ۱",
                            }
                            for i in range(start, start + 20)
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
                self.assertEqual(len(calls), 2)
                self.assertIsNotNone(calls[0]["schema"])
                self.assertIsNotNone(calls[1]["schema"])
                self.assertEqual(calls[0]["system"], calls[1]["system"])
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
