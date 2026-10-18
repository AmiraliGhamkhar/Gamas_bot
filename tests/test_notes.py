"""Tests for the note model, the deterministic merge and the map-reduce pipeline."""

from __future__ import annotations

import json
import math
import unittest
from unittest import mock

from gamas_bot.llm import LLMContextOverflowError, LLMError
from gamas_bot.notes import (
    COMPILE_HARD_MAX_CHARS,
    CONTEXT_DIGEST_MAX_CHARS,
    MAX_CONTEXT_SPLITS,
    PRESENTATION_PROMPT,
    PROMPT_RESERVE_TOKENS,
    TRANSCRIPT_PROMPT,
    StructuringError,
    effective_chunk_chars,
    effective_compile_max_chars,
    NoteDefinition,
    NoteSection,
    NoteTable,
    GlossaryEntry,
    StructuredNotes,
    build_context,
    build_system_prompt,
    build_presentation_system_prompt,
    build_presentation_document,
    compile_accepts,
    context_block_for,
    heading_topic_key,
    headings_overlap,
    headings_share_a_topic,
    merge_structured_notes,
    scrub_backend_artifacts,
    split_presentation_document,
    structure_presentation,
    structure_transcript,
)
from tests.support import make_settings, sample_notes_json


def notes_from_dict(payload: dict) -> StructuredNotes:
    return StructuredNotes.from_payload(payload)


def one_section(heading: str, paragraphs=(), bullets=()) -> NoteSection:
    return NoteSection(heading=heading, paragraphs=tuple(paragraphs), bullets=tuple(bullets))


def big_notes_json(title: str) -> str:
    """A valid note answer large enough to clear the compile threshold."""
    payload = {
        "title": title,
        "summary": f"خلاصهٔ نسبتاً طولانی برای بخش {title} از درس.",
        "sections": [
            {
                "heading": f"بخش {number} از {title}",
                "paragraphs": [
                    f"پاراگراف {paragraph} از بخش {number} با توضیح کامل و محتوای آموزشی یکتای {title}."
                    for paragraph in range(2)
                ],
                "bullets": [f"نکتهٔ مهم بخش {number} از {title}"],
            }
            for number in range(3)
        ],
        "key_points": ["نکتهٔ کلیدی"],
    }
    return json.dumps(payload, ensure_ascii=False)


class ModelParsingTests(unittest.TestCase):
    def test_valid_payload(self):
        notes = notes_from_dict(json.loads(sample_notes_json()))
        self.assertEqual(notes.title, "جزوهٔ آزمایشی")
        self.assertEqual(len(notes.sections), 1)
        self.assertEqual(notes.sections[0].heading, "بخش نخست")
        self.assertEqual(notes.glossary[0].term, "HbA1c")

    def test_empty_sections_rejected(self):
        with self.assertRaises(StructuringError):
            notes_from_dict({"title": "x", "sections": []})

    def test_placeholder_heading_for_missing_heading(self):
        payload = {
            "sections": [{"paragraphs": ["محتوا بدون عنوان"]}],
        }
        notes = notes_from_dict(payload)
        self.assertEqual(notes.sections[0].heading, "بخش 1")

    def test_invalid_callout_kind_defaults(self):
        payload = {
            "sections": [{
                "heading": "h",
                "callouts": [{"kind": "unknown", "text": "متن"}],
            }],
        }
        notes = notes_from_dict(payload)
        self.assertEqual(notes.sections[0].callouts[0].kind, "نکته")

    def test_table_rows_padded_to_header(self):
        payload = {
            "sections": [{
                "heading": "h",
                "table": {"headers": ["a", "b", "c"], "rows": [["x"]]},
            }],
        }
        notes = notes_from_dict(payload)
        self.assertEqual(notes.sections[0].table.rows, [["x", "", ""]])

    def test_markdown_round_trip_has_core_blocks(self):
        payload = {
            "title": "تست",
            "summary": "خلاصه",
            "sections": [{
                "heading": "بخش",
                "paragraphs": ["پاراگراف"],
                "bullets": ["بولت"],
                "definitions": [{"term": "اصطلاح", "definition": "تعریف"}],
                "table": {"headers": ["h1", "h2"], "rows": [["a", "b"]]},
            }],
            "key_points": ["نکته"],
            "glossary": [{"term": "واژه", "definition": "معنی"}],
        }
        markdown = notes_from_dict(payload).to_markdown()
        self.assertIn("# تست", markdown)
        self.assertIn("## بخش", markdown)
        self.assertIn("| a | b |", markdown)
        self.assertIn("**واژه:**", markdown)

    def test_to_json_round_trip(self):
        notes = notes_from_dict(json.loads(sample_notes_json()))
        restored = notes_from_dict(json.loads(notes.to_json()))
        self.assertEqual(restored.to_json(), notes.to_json())


class ScrubTests(unittest.TestCase):
    def test_key_shaped_tokens_removed(self):
        text = "کلید sk-abcdefghijklmnop123 است."
        self.assertNotIn("sk-abcdefghijklmnop123", scrub_backend_artifacts(text))

    def test_bare_provider_names_survive(self):
        text = "درس امروز دربارهٔ شبکه‌های عصبی و NVIDIA GPU است."
        self.assertIn("NVIDIA", scrub_backend_artifacts(text))

    def test_provider_self_reference_removed(self):
        self.assertNotIn("OpenAI", scrub_backend_artifacts("generated by OpenAI in 2024"))

    def test_plain_content_untouched(self):
        text = "فشار خون 120/80 و دوز 500 mg هر 8 ساعت."
        self.assertEqual(scrub_backend_artifacts(text), text)


class HeadingTopicTests(unittest.TestCase):
    def test_continuation_markers_are_same_topic(self):
        self.assertEqual(heading_topic_key("مقدمه"), heading_topic_key("ادامهٔ مقدمه"))
        self.assertEqual(heading_topic_key("مقدمه"), heading_topic_key("مقدمه (ادامه)"))

    def test_different_topics_differ(self):
        self.assertNotEqual(heading_topic_key("کلید خارجی"), heading_topic_key("ایندکس"))

    def test_share_a_topic_containment(self):
        # Containment needs two shared content words (the conservative rule).
        self.assertTrue(headings_share_a_topic("نرمال‌سازی داده", "نرمال‌سازی داده و صورت سوم"))
        self.assertFalse(headings_share_a_topic("کلید خارجی", "ایندکس"))

    def test_numbers_keep_topics_distinct(self):
        self.assertFalse(headings_share_a_topic("مرحله ۱", "مرحله ۲"))


class MergeTests(unittest.TestCase):
    def test_verbatim_duplicate_paragraphs_collapse(self):
        first = StructuredNotes(
            title="a",
            sections=[one_section("مقدمه", paragraphs=["جملهٔ مشترک است.", "فقط اینجا."])],
        )
        second = StructuredNotes(
            sections=[one_section("ادامه", paragraphs=["جملهٔ مشترک است.", "جملهٔ تازه."])],
        )
        merged = merge_structured_notes([first, second])
        paragraphs = [p for s in merged.sections for p in s.paragraphs]
        self.assertEqual(paragraphs.count("جملهٔ مشترک است."), 1)
        self.assertIn("جملهٔ تازه.", paragraphs)

    def test_same_topic_adjacent_sections_merge(self):
        first = StructuredNotes(sections=[one_section("شبکه‌های عصبی", paragraphs=["الف"])])
        second = StructuredNotes(sections=[one_section("شبکه‌های عصبی", paragraphs=["ب"])])
        merged = merge_structured_notes([first, second])
        self.assertEqual(len(merged.sections), 1)
        self.assertEqual(merged.sections[0].paragraphs, ("الف", "ب"))

    def test_continuation_heading_merges_across_boundary(self):
        first = StructuredNotes(sections=[one_section("مقدمهٔ درس", paragraphs=["الف"])])
        second = StructuredNotes(sections=[one_section("ادامهٔ مقدمهٔ درس", paragraphs=["ب"])])
        merged = merge_structured_notes([first, second])
        self.assertEqual(len(merged.sections), 1)

    def test_different_topics_stay_separate(self):
        first = StructuredNotes(sections=[one_section("کلید خارجی", paragraphs=["الف"])])
        second = StructuredNotes(sections=[one_section("ایندکس‌ها", paragraphs=["ب"])])
        merged = merge_structured_notes([first, second])
        self.assertEqual(len(merged.sections), 2)

    def test_glossary_dedup_keeps_longest_definition(self):
        first = StructuredNotes(
            sections=[one_section("s", paragraphs=["x"])],
            glossary=[GlossaryEntry("HbA1c", "کوتاه")],
        )
        second = StructuredNotes(
            sections=[one_section("t", paragraphs=["y"])],
            glossary=[GlossaryEntry("HbA1c", "تعریف بسیار طولانی‌تر و کامل‌تر")],
        )
        merged = merge_structured_notes([first, second])
        self.assertEqual(len(merged.glossary), 1)
        self.assertEqual(merged.glossary[0].definition, "تعریف بسیار طولانی‌تر و کامل‌تر")

    def test_compatible_tables_merge_rows(self):
        table_a = NoteTable(["h1", "h2"], [["a", "b"]])
        table_b = NoteTable(["h1", "h2"], [["a", "b"], ["c", "d"]])
        first = StructuredNotes(sections=[NoteSection(heading="جدول", table=table_a)])
        second = StructuredNotes(sections=[NoteSection(heading="ادامهٔ جدول", table=table_b)])
        merged = merge_structured_notes([first, second])
        self.assertEqual(len(merged.sections), 1)
        rows = merged.sections[0].table.rows if merged.sections[0].table else []
        self.assertEqual(rows, [["a", "b"], ["c", "d"]])

    def test_single_chunk_returned_as_is(self):
        notes = notes_from_dict(json.loads(sample_notes_json()))
        self.assertIs(merge_structured_notes([notes]), notes)


class ContextTests(unittest.TestCase):
    def test_topics_from_chunk_openings(self):
        chunks = [
            "امروز دربارهٔ شبکه‌های عصبی صحبت می‌کنیم. جزئیات زیاد است.",
            "حالا می‌رسیم سراغ الگوریتم‌های یادگیری. مطالب بعدی.",
        ]
        context = build_context(chunks)
        self.assertEqual(len(context.topics), 2)
        self.assertIn("شبکه‌های عصبی", context.topics[0])
        self.assertIn("الگوریتم‌های یادگیری", context.topics[1])

    def test_context_block_mentions_topic_and_neighbours(self):
        chunks = ["اول این درس است. ادامه دارد.", "دوم این درس است.", "سوم این درس است."]
        context = build_context(chunks)
        block = context_block_for(context, chunks, 2)
        self.assertIn("موضوع همین بخش", block)
        self.assertIn("اول این درس است", block)      # previous tail
        self.assertIn("سوم این درس است", block)       # next head

    def test_rendered_topic_digest_obeys_its_reserved_character_limit(self):
        chunks = [
            f"### اسلاید {number} — موضوع کوتاه {number}\n- متن"
            for number in range(1, 401)
        ]
        context = build_context(chunks)
        block = context_block_for(context, chunks, 1)
        digest = block.splitlines()[0].partition(": ")[2]
        self.assertLessEqual(len(digest), CONTEXT_DIGEST_MAX_CHARS)
        self.assertLess(len(context.topics), len(chunks))


class PromptTests(unittest.TestCase):
    def test_mode_rules_selected(self):
        full = build_system_prompt("full")
        summary = build_system_prompt("summary")
        self.assertIn("FULL", full)
        self.assertIn("SUMMARY", summary)
        self.assertIn("JSON", full)

    def test_context_block_appended(self):
        prompt = build_system_prompt("full", context_block="موضوع همین بخش: «تست»")
        self.assertIn("موضوع همین بخش", prompt)
        self.assertIn("قواعد نوشتن یک «بخش»", prompt)


class CompileAcceptanceTests(unittest.TestCase):
    def test_rejects_gutted_result(self):
        merged = StructuredNotes(
            sections=[one_section(f"بخش {i}", paragraphs=[f"متن {i}"]) for i in range(10)]
        )
        gutted = StructuredNotes(sections=[one_section("همه", paragraphs=["یک خط"])])
        self.assertFalse(compile_accepts(gutted, merged))

    def test_accepts_restructured_result(self):
        merged = StructuredNotes(
            sections=[one_section(f"بخش {i}", paragraphs=[f"متن {i}"]) for i in range(10)]
        )
        compiled = StructuredNotes(
            sections=[one_section(f"بخش {i}", paragraphs=[f"متن {i}", f"توضیح {i}"]) for i in range(8)]
        )
        self.assertTrue(compile_accepts(compiled, merged))

    def test_rejects_empty(self):
        merged = StructuredNotes(sections=[one_section("h", paragraphs=["x"])])
        self.assertFalse(compile_accepts(StructuredNotes(), merged))


class ContextBudgetTests(unittest.TestCase):
    """The request budget comes from the model window, not from a character cap."""

    def test_a_smaller_window_produces_smaller_chunks(self):
        system = build_system_prompt("full")
        wide = make_settings(note_context_tokens=200_000, note_max_output_tokens=4096)
        narrow = make_settings(note_context_tokens=8192, note_max_output_tokens=2048)
        wide_budget = effective_chunk_chars(wide, system_prompt=system)
        narrow_budget = effective_chunk_chars(narrow, system_prompt=system)
        self.assertEqual(wide_budget, wide.note_chunk_chars)  # window is not binding
        self.assertLess(narrow_budget, wide_budget)
        self.assertLess(narrow_budget, narrow.note_chunk_chars)

    def test_a_configured_chunk_is_never_grown(self):
        settings = make_settings(note_chunk_chars=4000, note_context_tokens=200_000)
        budget = effective_chunk_chars(settings, system_prompt=build_system_prompt("full"))
        self.assertEqual(budget, 4000)

    def test_reserved_material_shrinks_the_budget(self):
        system = build_system_prompt("full")
        settings = make_settings(
            note_context_tokens=40_000, note_max_output_tokens=4096, note_chunk_chars=100_000
        )
        without = effective_chunk_chars(settings, system_prompt=system)
        with_digest = effective_chunk_chars(
            settings, system_prompt=system, extra_reserve_chars=6000
        )
        self.assertLess(with_digest, without)

    def test_small_window_budget_never_exceeds_the_remaining_context(self):
        settings = make_settings(note_context_tokens=8192, note_max_output_tokens=4096)
        system = build_system_prompt("full")
        budget = effective_chunk_chars(settings, system_prompt=system)
        reserve_tokens = math.ceil(len(system) / 2.0) + PROMPT_RESERVE_TOKENS
        available_tokens = settings.note_context_tokens - settings.note_max_output_tokens - reserve_tokens
        self.assertGreater(available_tokens, 0)
        self.assertEqual(budget, min(settings.note_chunk_chars, available_tokens * 2))

    def test_no_input_room_is_reported_instead_of_floored_over_budget(self):
        settings = make_settings(note_context_tokens=8192, note_max_output_tokens=8192)
        with self.assertRaises(StructuringError):
            effective_chunk_chars(settings, system_prompt=build_system_prompt("full"))


class CompileBudgetTests(unittest.TestCase):
    """The compile (reduce) budget is token-derived, not a fixed char bound."""

    def test_the_default_window_lifts_the_former_fixed_bound(self):
        # Under the old fixed 48,000-char bound this booklet size skipped the
        # editorial pass; the matched 262K-window defaults must allow it.
        settings = make_settings()
        self.assertGreater(effective_compile_max_chars(settings), 48000)

    def test_a_smaller_window_produces_a_smaller_compile_budget(self):
        narrow = make_settings(note_context_tokens=8192, note_max_output_tokens=2048)
        wide = make_settings(note_context_tokens=131072, note_max_output_tokens=4096)
        self.assertLess(
            effective_compile_max_chars(narrow), effective_compile_max_chars(wide)
        )

    def test_budget_reserves_the_selected_mode_and_presentation_compile_prompt(self):
        settings = make_settings(note_context_tokens=40_000, note_max_output_tokens=4096)
        full = effective_compile_max_chars(settings)
        summary = effective_compile_max_chars(settings, mode="summary")
        presentation = effective_compile_max_chars(settings, presentation=True)
        self.assertLess(summary, full)
        self.assertLess(presentation, full)

    def test_the_derived_budget_is_bounded_by_the_hard_ceiling(self):
        settings = make_settings(
            note_context_tokens=10_000_000, note_max_output_tokens=4096
        )
        self.assertEqual(effective_compile_max_chars(settings), COMPILE_HARD_MAX_CHARS)

    def test_an_explicit_operator_cap_wins(self):
        settings = make_settings(note_compile_max_chars=12345)
        self.assertEqual(effective_compile_max_chars(settings), 12345)

    def test_explicit_compile_cap_cannot_overrun_a_small_context(self):
        settings = make_settings(
            note_context_tokens=8192,
            note_max_output_tokens=4096,
            note_compile_max_chars=10000,
        )
        self.assertLess(effective_compile_max_chars(settings), settings.note_compile_max_chars)

    def test_output_budget_shrinks_the_compile_budget(self):
        lean = make_settings(note_max_output_tokens=4096)
        hungry = make_settings(note_max_output_tokens=100_000)
        self.assertLess(
            effective_compile_max_chars(hungry), effective_compile_max_chars(lean)
        )


class ContextOverflowRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """An oversized request is split at a safe boundary, never re-sent as is."""

    @staticmethod
    def document() -> str:
        return "\n\n".join(
            f"موضوع {index}: " + "توضیح کامل و مثال برای درس. " * 30
            for index in range(4)
        )

    async def test_overflow_splits_the_input_and_merges_the_answers(self):
        settings = make_settings(note_chunk_chars=100_000)
        document = self.document()
        session = FakeSession([
            context_overflow_response(),
            big_notes_json("نیمهٔ نخست"),
            big_notes_json("نیمهٔ دوم"),
        ])
        notes = await structure_transcript(document, settings, session)
        # The oversized request was replaced by two smaller ones…
        self.assertEqual(len(session.calls), 3)
        first = session.calls[1]["json"]["messages"][1]["content"].removeprefix(TRANSCRIPT_PROMPT)
        second = session.calls[2]["json"]["messages"][1]["content"].removeprefix(TRANSCRIPT_PROMPT)
        self.assertNotEqual(first, second)
        # …and no content was lost between them (the splitter trims boundary
        # whitespace only, never words).
        self.assertEqual(
            " ".join((first + " " + second).split()), " ".join(document.split())
        )
        # The merged booklet keeps both halves.
        headings = [section.heading for section in notes.sections]
        self.assertTrue(any("نیمهٔ نخست" in heading for heading in headings))
        self.assertTrue(any("نیمهٔ دوم" in heading for heading in headings))

    async def test_a_413_oversized_body_splits_too(self):
        """NaraRouter signals an oversized input with HTTP 413, not 400.

        Its documented wording ("The request body or input is too large") names
        no context window, so the status is the only reliable signal — and the
        split recovery must still run, or the user loses the booklet.
        """
        settings = make_settings(note_chunk_chars=100_000)
        document = self.document()
        session = FakeSession([
            nara_oversized_response(),
            big_notes_json("نیمهٔ نخست"),
            big_notes_json("نیمهٔ دوم"),
        ])
        notes = await structure_transcript(document, settings, session)
        self.assertEqual(len(session.calls), 3)
        first = session.calls[1]["json"]["messages"][1]["content"].removeprefix(TRANSCRIPT_PROMPT)
        second = session.calls[2]["json"]["messages"][1]["content"].removeprefix(TRANSCRIPT_PROMPT)
        self.assertNotEqual(first, second)
        self.assertEqual(
            " ".join((first + " " + second).split()), " ".join(document.split())
        )
        headings = [section.heading for section in notes.sections]
        self.assertTrue(any("نیمهٔ نخست" in heading for heading in headings))
        self.assertTrue(any("نیمهٔ دوم" in heading for heading in headings))

    async def test_an_unchanged_oversized_request_is_never_retried_forever(self):
        settings = make_settings(note_chunk_chars=100_000)
        session = FakeSession([context_overflow_response() for _ in range(64)])
        with self.assertRaises(LLMContextOverflowError):
            await structure_transcript(self.document(), settings, session)
        # 1 + 2 + 4 requests: the split depth is bounded, nothing is re-sent.
        self.assertEqual(len(session.calls), 1 + 2 ** 1 + 2 ** MAX_CONTEXT_SPLITS)


class _AsyncCM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return False


class FakeSession:
    """Scripted note-provider session: one answer (or error) per call."""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        scripted = self.contents.pop(0)
        if isinstance(scripted, _FakeResponse):
            return _AsyncCM(scripted)
        return _AsyncCM(_FakeResponse(200, {
            "choices": [{"message": {"content": scripted}, "finish_reason": "stop"}]
        }))


def context_overflow_response() -> "_FakeResponse":
    """A provider 400 describing an input larger than the model's window."""
    return _FakeResponse(400, {
        "error": {
            "message": "This model's maximum context length is 8192 tokens",
            "code": "context_length_exceeded",
        }
    })


def nara_oversized_response() -> "_FakeResponse":
    """NaraRouter's documented 413: an oversized body, no context wording.

    https://router.bynara.id/docs ("Status codes"): ``413 / bad_request —
    The request body or input is too large.`` The status is the only signal
    that the input must be split.
    """
    return _FakeResponse(413, {
        "error": {
            "type": "bad_request",
            "message": "The request body or input is too large.",
            "request_id": "req_7f3a",
        }
    })


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.headers = {}
        self._payload = payload

    async def read(self):
        # A real provider body: the error classifiers read it.
        return json.dumps(self._payload).encode()

    async def json(self, content_type=None):
        return self._payload


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_chunk_one_call(self):
        settings = make_settings(note_chunk_chars=100000)
        session = FakeSession([sample_notes_json("جزوهٔ تک‌بخشی")])
        notes = await structure_transcript("یک متن کوتاه است.", settings, session)
        self.assertEqual(notes.title, "جزوهٔ تک‌بخشی")
        self.assertEqual(len(session.calls), 1)

    async def test_multi_chunk_map_reduce(self):
        # Force multiple chunks with a small budget; script one response per
        # chunk (counted with the real splitter) plus exactly one compile call.
        from gamas_bot.transcript import split_transcript

        settings = make_settings(note_chunk_chars=400, note_compile=True)
        document = "\n\n".join(
            f"موضوع {index}: " + "جمله‌های بسیار زیاد برای پر کردن حجم. " * 20
            for index in range(6)
        )
        chunks = split_transcript(document, 400)
        self.assertGreater(len(chunks), 1)
        compile_answer = {
            "title": "جزوهٔ نهایی",
            "summary": "خلاصهٔ نهایی",
            "sections": [
                {
                    "heading": f"بخش یکپارچهٔ {number}",
                    "paragraphs": [f"متن بازنویسی‌شدهٔ بخش {number} با همان محتوا."],
                }
                for number in range(max(2, len(chunks)))
            ],
        }
        session = FakeSession(
            [big_notes_json(f"جزوهٔ {i}") for i in range(len(chunks))]
            + [json.dumps(compile_answer, ensure_ascii=False)]
        )
        notes = await structure_transcript(document, settings, session)
        # map calls + exactly one compile call
        self.assertEqual(len(session.calls), len(chunks) + 1)
        self.assertEqual(notes.title, "جزوهٔ نهایی")

    async def test_compile_runs_above_the_former_fixed_bound(self):
        """A merged booklet over the old 48,000-char bound still gets compiled.

        Regression test for the fixed ``COMPILE_MAX_CHARS``: under the token-
        derived budget (default settings), the editorial pass must run.
        """
        from gamas_bot.transcript import split_transcript

        settings = make_settings(note_chunk_chars=400, note_compile=True)
        document = "\n\n".join(
            f"موضوع {index}: " + "جمله‌های بسیار زیاد برای پر کردن حجم. " * 20
            for index in range(60)
        )
        chunks = split_transcript(document, 400)
        compile_answer = {
            "title": "جزوهٔ نهایی بزرگ",
            "summary": "خلاصهٔ نهایی",
            "sections": [
                {"heading": f"بخش یکپارچهٔ {number}", "paragraphs": ["متن یکپارچه."]}
                for number in range(max(2, len(chunks)))
            ],
        }
        session = FakeSession(
            [big_notes_json(f"جزوهٔ {i}") for i in range(len(chunks))]
            + [json.dumps(compile_answer, ensure_ascii=False)]
        )
        notes = await structure_transcript(document, settings, session)
        # Map calls + exactly one compile call, despite the merged booklet
        # being far above the former fixed 48,000-char bound.
        self.assertEqual(len(session.calls), len(chunks) + 1)
        self.assertEqual(notes.title, "جزوهٔ نهایی بزرگ")

    async def test_compile_failure_falls_back_to_merge(self):
        from gamas_bot.transcript import split_transcript

        settings = make_settings(note_chunk_chars=400, note_compile=True)
        document = "\n\n".join(
            f"موضوع {index}: " + "جمله‌های بسیار زیاد برای پر کردن حجم. " * 20
            for index in range(4)
        )
        chunks = split_transcript(document, 400)
        good = [sample_notes_json(f"جزوهٔ {i}") for i in range(len(chunks))]
        # The compile call retries once on bad JSON, so two bad answers end it.
        session = FakeSession(good + ["not valid json at all", "still not json"])
        notes = await structure_transcript(document, settings, session)
        # The deterministic merge (first chunk title wins) is delivered.
        self.assertEqual(notes.title, "جزوهٔ 0")

    async def test_compile_disabled_skips_reduce(self):
        from gamas_bot.transcript import split_transcript

        settings = make_settings(note_chunk_chars=400, note_compile=False)
        document = "\n\n".join(
            f"موضوع {index}: " + "جمله‌های بسیار زیاد برای پر کردن حجم. " * 20
            for index in range(4)
        )
        chunks = split_transcript(document, 400)
        session = FakeSession([sample_notes_json(f"جزوهٔ {i}") for i in range(len(chunks))])
        await structure_transcript(document, settings, session)
        self.assertEqual(len(session.calls), len(chunks))

    async def test_explicit_compile_cap_skips_reduce(self):
        """A merged booklet above the operator's compile cap is merged, not sent."""
        from gamas_bot.transcript import split_transcript

        settings = make_settings(
            note_chunk_chars=400, note_compile=True, note_compile_max_chars=10
        )
        document = "\n\n".join(
            f"موضوع {index}: " + "جمله‌های بسیار زیاد برای پر کردن حجم. " * 20
            for index in range(4)
        )
        chunks = split_transcript(document, 400)
        session = FakeSession([sample_notes_json(f"جزوهٔ {i}") for i in range(len(chunks))])
        await structure_transcript(document, settings, session)
        # Map calls only: the compile request was never built.
        self.assertEqual(len(session.calls), len(chunks))

    async def test_provider_failure_propagates(self):
        """A failed chunk fails the step; the caller delivers the raw text."""
        settings = make_settings(note_chunk_chars=100000)
        session = FakeSession([])
        session.post = lambda *a, **k: _AsyncCM(_FakeResponse(500, {}))
        with mock.patch("gamas_bot.llm.MAX_TRANSIENT_RETRIES", 0), \
             mock.patch("asyncio.sleep", new=lambda *_a, **_k: _noop()):
            with self.assertRaises(LLMError):
                await structure_transcript("متن کوتاه.", settings, session)


async def _noop():
    return None


class PresentationPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_slide_text_speaker_notes_and_narration_reach_the_note_model(self):
        outline = (
            "### اسلاید ۱ — شبکه‌های عصبی\n"
            "- لایهٔ ورودی\n"
            "یادداشت گوینده: هر لایه ویژگی‌ها را به مرحلهٔ بعد می‌فرستد."
        )
        transcript = "در صدا هم دربارهٔ آموزش وزن‌ها توضیح داده شد."
        settings = make_settings(note_chunk_chars=100_000, note_compile=False)
        session = FakeSession([sample_notes_json()])

        await structure_presentation(outline, transcript, settings, session)

        self.assertEqual(len(session.calls), 1)
        request = session.calls[0]["json"]
        user_content = request["messages"][1]["content"]
        self.assertIn("## متن اسلایدها", user_content)
        self.assertIn("یادداشت گوینده: هر لایه ویژگی‌ها را", user_content)
        self.assertIn("## متن پیاده‌سازی‌شدهٔ صدای ارائه", user_content)
        self.assertIn(transcript, user_content)
        self.assertIn("یادداشت گویندهٔ هر اسلاید جزو محتوای آموزشی است", request["messages"][0]["content"])

    def test_presentation_document_keeps_both_sources_separate(self):
        document = build_presentation_document("### اسلاید ۱\nیادداشت گوینده: نکته", "صدای کلاس")
        self.assertIn("## متن اسلایدها", document)
        self.assertIn("یادداشت گوینده: نکته", document)
        self.assertIn("## متن پیاده‌سازی‌شدهٔ صدای ارائه", document)
        self.assertIn("صدای کلاس", document)

    def test_aligned_narration_is_grouped_with_its_slide(self):
        outline = (
            "### اسلاید 1 — مقدمه\n- نکتهٔ یک\n\n"
            "### اسلاید 2 — نتیجه\n- نکتهٔ دو"
        )
        transcript = (
            "### گفتار مرتبط با اسلاید 1\n\nتوضیح مرتبط با نکتهٔ یک\n\n"
            "### گفتار بدون نگاشت مطمئن به اسلاید\n\nیک بخشِ بدون زمان‌بندی"
        )
        document = build_presentation_document(outline, transcript)
        first_slide = document.index("### اسلاید ۱ — مقدمه")
        narration = document.index("توضیح مرتبط با نکتهٔ یک")
        second_slide = document.index("### اسلاید ۲ — نتیجه")
        unassigned = document.index("یک بخشِ بدون زمان‌بندی")
        self.assertLess(first_slide, narration)
        self.assertLess(narration, second_slide)
        self.assertLess(second_slide, unassigned)
        self.assertIn("بدون نگاشت مطمئن", document)

    def test_presentation_chunker_prefers_slide_boundaries(self):
        document = (
            "## متن اسلایدها و گفتار ارائه به تفکیک اسلاید\n\n"
            "### اسلاید 1 — الف\n\nمتن کوتاه اسلاید یک\n\n"
            "### اسلاید 2 — ب\n\nمتن کوتاه اسلاید دو"
        )
        chunks = split_presentation_document(document, 90)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertIn("### اسلاید 1", chunks[0])
        self.assertNotIn("### اسلاید 2", chunks[0])
        self.assertIn("### اسلاید 2", chunks[1])
        self.assertTrue(all(len(chunk) <= 90 for chunk in chunks))

    def test_long_slide_continuations_preserve_text_and_stay_within_budget(self):
        body = "واژه " * 70
        document = f"### اسلاید ۳ — طولانی\n\n{body}"
        chunks = split_presentation_document(document, 90)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 90 for chunk in chunks))
        self.assertEqual(sum(chunk.count("واژه") for chunk in chunks), 70)
        self.assertTrue(any("ادامهٔ اسلاید ۳" in chunk for chunk in chunks[1:]))

    def test_oversized_first_slide_does_not_emit_preamble_only_chunk(self):
        body = "توضیح مفصل اسلاید یک. " * 20
        document = (
            "## متن اسلایدها و گفتار ارائه به تفکیک اسلاید\n\n"
            f"### اسلاید ۱ — مقدمه\n\n{body}"
        )
        chunks = split_presentation_document(document, 180)
        self.assertGreater(len(chunks), 1)
        self.assertIn("### اسلاید ۱", chunks[0])
        self.assertIn("توضیح مفصل", chunks[0])

    def test_unassigned_section_header_stays_with_unassigned_block(self):
        outline = "### اسلاید 1 — اول\n- نکتهٔ یک"
        transcript = (
            "### گفتار مرتبط با اسلاید 1\n\nتوضیح کوتاه اسلاید یک.\n\n"
            "### گفتار بدون نگاشت مطمئن به اسلاید\n\n" + ("توضیح بدون نگاشت. " * 15)
        )
        document = build_presentation_document(outline, transcript)
        chunks = split_presentation_document(document, 220)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertNotIn("بدون نگاشت زمانی مطمئن", chunks[0])
        self.assertTrue(
            any(
                "بدون نگاشت زمانی مطمئن" in chunk and "### گفتار بدون نگاشت" in chunk
                for chunk in chunks[1:]
            )
        )

    def test_unaligned_narration_keeps_separate_sections_without_false_slide_grouping(self):
        outline = "### اسلاید 1 — مقدمه\n- نکته"
        transcript = "### گفتار بدون نگاشت مطمئن به اسلاید\n\nکل گفتار بدون زمان‌بندی."
        document = build_presentation_document(outline, transcript)
        self.assertIn("## متن اسلایدها\n\n", document)
        self.assertIn("## متن پیاده‌سازی‌شدهٔ صدای ارائه\n\n", document)
        self.assertNotIn("به تفکیک اسلاید", document)

    def test_unaligned_plain_narration_is_not_labeled_as_last_slide_continuation(self):
        outline = "### اسلاید ۱ — اول\n- نکتهٔ یک\n\n### اسلاید ۲ — دوم\n- نکتهٔ دو"
        transcript = "توضیح طولانی گفتار استاد دربارهٔ کل جلسه. " * 12
        document = build_presentation_document(outline, transcript)
        chunks = split_presentation_document(document, 180)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertFalse(any("ادامهٔ اسلاید ۲" in chunk for chunk in chunks))
        self.assertTrue(
            any("ادامهٔ گفتار بدون نگاشت" in chunk for chunk in chunks[1:])
        )

    def test_presentation_context_lists_all_slides_in_each_chunk(self):
        chunks = [
            "## متن اسلایدها\n\n### اسلاید ۱ — مقدمه\n- الف\n\n### اسلاید ۲ — تعاریف\n- ب",
            "### اسلاید ۳ — روش کار\n- ج\n\n### اسلاید ۴ — نتیجه‌گیری\n- د",
        ]
        context = build_context(chunks)
        self.assertEqual(len(context.topics), 2)
        self.assertIn("اسلاید ۱ — مقدمه", context.topics[0])
        self.assertIn("اسلاید ۲ — تعاریف", context.topics[0])
        self.assertIn("اسلاید ۳ — روش کار", context.topics[1])
        self.assertIn("اسلاید ۴ — نتیجه‌گیری", context.topics[1])

    def test_cross_chunk_merge_keeps_distinctly_numbered_slides_separate(self):
        self.assertFalse(
            headings_overlap(
                "اسلاید ۴: ساختمان داده‌های خطی",
                "اسلاید ۵: ساختمان داده‌های خطی",
            )
        )
        first = StructuredNotes(
            sections=[one_section("اسلاید ۴: ساختمان داده‌های خطی", paragraphs=["الف"])]
        )
        second = StructuredNotes(
            sections=[one_section("اسلاید ۵: ساختمان داده‌های خطی", paragraphs=["ب"])]
        )
        merged = merge_structured_notes([first, second])
        self.assertEqual(len(merged.sections), 2)

    def test_compile_accepts_rejects_dropped_definitions_and_gutted_prose(self):
        defs = (
            NoteDefinition("کیفیت", "توضیح اول"),
            NoteDefinition("پایایی", "توضیح دوم"),
        )
        merged = StructuredNotes(
            sections=(
                NoteSection(
                    heading="بخش ۱",
                    paragraphs=("توضیح بسیار مفصل و کامل آموزشی. " * 20,),
                    definitions=defs,
                ),
                NoteSection(
                    heading="بخش ۲",
                    paragraphs=("توضیح بسیار مفصل و کامل بخش دوم. " * 20,),
                ),
            )
        )
        no_defs = StructuredNotes(
            sections=(
                NoteSection(
                    heading="بخش ۱",
                    paragraphs=("توضیح بسیار مفصل و کامل آموزشی. " * 20,),
                ),
                NoteSection(
                    heading="بخش ۲",
                    paragraphs=("توضیح بسیار مفصل و کامل بخش دوم. " * 20,),
                ),
            )
        )
        self.assertFalse(compile_accepts(no_defs, merged))

        gutted_prose = StructuredNotes(
            sections=(
                NoteSection(heading="بخش ۱", paragraphs=("کوتاه.",), definitions=defs),
                NoteSection(heading="بخش ۲", paragraphs=("کوتاه دو.",)),
            )
        )
        self.assertFalse(compile_accepts(gutted_prose, merged))

    async def test_narrow_window_drops_optional_digest_instead_of_overflowing(self):
        settings = make_settings(
            note_context_tokens=8192,
            note_max_output_tokens=2048,
            note_chunk_chars=100_000,
            note_compile=False,
        )
        outline = "\n\n".join(
            f"### اسلاید {number} — موضوع {number}\n"
            + ("- توضیح آموزشی برای اسلاید. " * 55)
            for number in range(1, 6)
        )
        document = build_presentation_document(outline, "")
        base_system = build_presentation_system_prompt("full")
        chunk_chars = effective_chunk_chars(settings, system_prompt=base_system)
        expected_chunks = split_presentation_document(document, chunk_chars)
        self.assertGreater(len(expected_chunks), 1)
        session = FakeSession([
            sample_notes_json(f"جزوهٔ بخش {index}")
            for index in range(1, len(expected_chunks) + 1)
        ])

        await structure_presentation(outline, "", settings, session)

        self.assertEqual(len(session.calls), len(expected_chunks))
        for call in session.calls:
            request = call["json"]
            self.assertNotIn("موضوع‌های درس به ترتیب", request["messages"][0]["content"])
            self.assertTrue(request["messages"][1]["content"].startswith(PRESENTATION_PROMPT))

    async def test_output_budget_truncation_splits_chunk_and_recovers(self):
        settings = make_settings(note_chunk_chars=100_000, note_compile=False)
        outline = (
            "### اسلاید ۱ — بخش نخست\n" + ("- نکتهٔ اول برای تست.\n" * 15) + "\n"
            "### اسلاید ۲ — بخش دوم\n" + ("- نکتهٔ دوم برای تست.\n" * 15)
        )
        truncated = _FakeResponse(200, {
            "choices": [{"message": {"content": "{\"title\": \"ناقص\""}, "finish_reason": "length"}]
        })
        session = FakeSession([
            truncated,
            big_notes_json("نیمهٔ نخست"),
            big_notes_json("نیمهٔ دوم"),
        ])
        notes = await structure_presentation(outline, "", settings, session, mode="summary")
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(
            session.calls[0]["json"]["messages"][1]["content"].startswith(PRESENTATION_PROMPT)
        )
        self.assertEqual(notes.note_mode, "summary")


if __name__ == "__main__":
    unittest.main()
