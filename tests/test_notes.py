"""Tests for the note model, the deterministic merge and the map-reduce pipeline."""

from __future__ import annotations

import json
import math
import unittest
from unittest import mock

from gamas_bot.llm import LLMContextOverflowError, LLMError
from gamas_bot.notes import (
    COMPILE_HARD_MAX_CHARS,
    MODE_RULES,
    MAX_PRACTICAL_TIPS,
    COMPLETENESS_THRESHOLD,
    MAX_COVERAGE_GAPS,
    REQUIRED_SKELETON_BLOCKS,
    COVERAGE_FILL_SYSTEM_PROMPT,
    COMPILE_SYSTEM_PROMPT,
    CompletenessReport,
    assess_completeness,
    booklet_skeleton,
    build_fill_document,
    find_coverage_gaps,
    meets_completeness_threshold,
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
    MAX_NOTE_CONCURRENCY,
    build_chunk_message,
    build_context,
    _note_semaphore,
    _response_schema,
    build_repair_document,
    build_system_prompt,
    build_presentation_system_prompt,
    build_presentation_document,
    compile_accepts,
    context_block_for,
    gate_notes,
    notes_need_repair,
    repair_improves,
    scrub_fabricated_numbers,
    heading_topic_key,
    headings_overlap,
    headings_share_a_topic,
    merge_structured_notes,
    notes_response_schema,
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
    def test_only_full_mode_is_available(self):
        # CR-3: standard/summary were deleted; every mode value now resolves
        # to the single FULL rule set (no SUMMARY/STANDARD prompt exists).
        full = build_system_prompt("full")
        for legacy in ("standard", "summary", "bogus", None):
            with self.subTest(mode=legacy):
                self.assertEqual(build_system_prompt(legacy), full)
        self.assertIn("FULL", full)
        self.assertIn("JSON", full)
        self.assertNotIn("SUMMARY", full)
        self.assertNotIn("STANDARD", full)
        self.assertEqual(tuple(MODE_RULES), ("full",))

    def test_full_mode_is_compact_but_lossless(self):
        """The full-mode contract: compact wording, no dropped facts, no added facts.

        Regression guard for the prompt-edit that made `full` produce a compact
        booklet. If someone reintroduces the old verbose rule, or a garbled
        forbid-list bullet, this fails.
        """
        full = build_system_prompt("full")
        # Compaction is instructed explicitly…
        self.assertIn("جزوه باید «فشرده» باشد", full)
        # …but only in wording: facts may not be dropped for brevity…
        self.assertIn("فشرده‌سازی فقط در بیان است", full)
        self.assertIn("هیچ مطلب علمیِ متن حذف نشود", full)
        # …and nothing may be invented.
        self.assertIn("هیچ مطلب تازه‌ای اضافه نکنید", full)
        # The forbid-list bullet is clean Persian, not the old garbled text, and
        # no bullet is glued onto its neighbour by a missing newline.
        self.assertNotIn("similares", full)
        self.assertNotIn("نیاورید.-", full)

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
        # The v4 system prompt is larger than v3's (schema v4, table and tip
        # rules), so the smallest window that still leaves input room grew with
        # it; the invariant under test is unchanged.
        settings = make_settings(note_context_tokens=16384, note_max_output_tokens=4096)
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


class CacheStablePrefixTests(unittest.TestCase):
    """The system prompt must not vary per chunk, or the provider cannot cache it."""

    def test_the_rules_block_is_identical_for_every_chunk(self):
        full = build_system_prompt("full")
        # What the map phase sends: the same string for chunk 1, 2 and 3.
        self.assertEqual(len({full, build_system_prompt("full")}), 1)
        self.assertNotIn("موضوع همین بخش", full)

    def test_the_varying_context_moves_into_the_user_message(self):
        block = "موضوع همین بخش: «انتقال فعال»"
        message = build_chunk_message(block, "متن این بخش")
        self.assertTrue(message.startswith("\nقواعد نوشتن یک «بخش»"))
        self.assertIn(block, message)
        self.assertTrue(message.endswith("متن این بخش"))
        # No block, no wrapper: the message is exactly the chunk.
        self.assertEqual(build_chunk_message("", "متن"), "متن")


class ConcurrencyBoundTests(unittest.TestCase):
    def test_the_semaphore_is_bounded_by_the_work_and_the_ceiling(self):
        settings = make_settings(note_concurrency=6)
        self.assertEqual(_note_semaphore(settings, 2)._value, 2)
        self.assertEqual(_note_semaphore(settings, 6)._value, 6)
        self.assertEqual(_note_semaphore(make_settings(note_concurrency=8), 50)._value, 8)
        self.assertEqual(MAX_NOTE_CONCURRENCY, 8)


class ResponseSchemaTests(unittest.TestCase):
    def test_the_schema_is_only_sent_when_the_setting_is_on(self):
        self.assertIsNone(_response_schema(make_settings()))
        schema = _response_schema(make_settings(note_structured_output=True))
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["sections"])
        section = schema["properties"]["sections"]["items"]
        for key in ("heading", "paragraphs", "tables", "callouts", "definitions"):
            self.assertIn(key, section["properties"])

    def test_every_field_the_loader_reads_is_in_the_schema(self):
        schema = notes_response_schema()
        top = schema["properties"]
        for key in (
            "title", "summary", "learning_objectives", "sections",
            "practical_tips", "key_points", "review_questions", "glossary",
        ):
            self.assertIn(key, top)


class ProgressiveBookletTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_merge_is_handed_over_before_the_compile_runs(self):
        from gamas_bot.transcript import split_transcript

        settings = make_settings(note_chunk_chars=400, note_compile=True)
        document = "\n\n".join(
            f"موضوع {index}: " + "جمله‌های بسیار زیاد برای پر کردن حجم. " * 20
            for index in range(4)
        )
        chunks = split_transcript(document, 400)
        # The acceptance gate refuses a compile that keeps far fewer sections
        # than the merge, so the answer carries at least as many.
        compile_answer = {
            "title": "جزوهٔ نهایی",
            "summary": "خلاصهٔ نهایی",
            "sections": [
                {"heading": f"بخش یکپارچهٔ {number}", "paragraphs": ["متن یکپارچه."]}
                for number in range(40)
            ],
        }
        session = FakeSession(
            [big_notes_json(f"جزوهٔ {i}") for i in range(len(chunks))]
            + [json.dumps(compile_answer, ensure_ascii=False)]
        )
        seen: list[tuple[str, int]] = []

        async def on_merged(merged):
            # The compile request has not been sent yet: the callback receives
            # the deterministic merge while the user can already read it.
            seen.append((merged.title, len(session.calls)))
            if False:
                raise AssertionError("unreachable")

        notes = await structure_transcript(
            document, settings, session, on_merged=on_merged
        )
        self.assertEqual(notes.title, "جزوهٔ نهایی")
        self.assertEqual(seen, [("جزوهٔ 0", len(chunks))])

    async def test_no_draft_when_the_compile_is_disabled(self):
        """With the editorial pass off, the draft would be the final file."""
        from gamas_bot.transcript import split_transcript

        settings = make_settings(note_chunk_chars=400, note_compile=False)
        document = "\n\n".join(
            f"موضوع {index}: " + "جمله‌های بسیار زیاد برای پر کردن حجم. " * 20
            for index in range(4)
        )
        chunks = split_transcript(document, 400)
        session = FakeSession([big_notes_json(f"جزوهٔ {i}") for i in range(len(chunks))])
        seen: list[str] = []

        async def on_merged(merged):
            seen.append(merged.title)

        notes = await structure_transcript(document, settings, session, on_merged=on_merged)
        self.assertEqual(seen, [])
        # No compile request was sent, and the merge is the deliverable.
        self.assertEqual(len(session.calls), len(chunks))
        self.assertEqual(notes.title, "جزوهٔ 0")

    async def test_no_draft_for_a_booklet_below_the_compile_floor(self):
        """A merge too small to compile is already the final booklet."""
        from gamas_bot import notes as notes_module

        settings = make_settings(note_compile=True)
        answer = {
            "title": "کوتاه",
            "sections": [{"heading": "یک", "paragraphs": ["متن کوتاه."]}],
        }
        payload = json.dumps(answer, ensure_ascii=False)
        self.assertLess(len(payload), notes_module.COMPILE_MIN_NOTES_CHARS * 4)
        session = FakeSession([payload])
        seen: list[str] = []

        async def on_merged(merged):
            seen.append(merged.title)

        notes = await structure_transcript(
            "یک متن کوتاه برای جزوه.", settings, session, on_merged=on_merged
        )
        self.assertEqual(seen, [])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(notes.title, "کوتاه")

    async def test_the_draft_gate_matches_the_compile_pass(self):
        """The predicate the callback uses is the one the pass uses."""
        from gamas_bot import notes as notes_module

        merged = notes_module.StructuredNotes.from_payload(
            {
                "title": "منبع",
                "sections": [
                    {"heading": f"بخش {number}", "paragraphs": ["متن کافی برای عبور از آستانه."]}
                    for number in range(6)
                ],
            }
        )
        compile_on = make_settings(note_compile=True)
        self.assertTrue(
            notes_module.compile_will_run(merged, compile_on, mode="full", presentation=False)
        )
        self.assertIsNone(
            notes_module.compile_skip_reason(
                merged, compile_on, mode="full", presentation=False
            )
        )
        compile_off = make_settings(note_compile=False)
        self.assertFalse(
            notes_module.compile_will_run(merged, compile_off, mode="full", presentation=False)
        )
        self.assertEqual(
            notes_module.compile_skip_reason(
                merged, compile_off, mode="full", presentation=False
            ),
            "disabled",
        )

    async def test_a_failing_callback_never_fails_the_notes(self):
        from gamas_bot.transcript import split_transcript

        settings = make_settings(note_chunk_chars=400, note_compile=True)
        document = "\n\n".join(
            f"موضوع {index}: " + "جمله‌های بسیار زیاد برای پر کردن حجم. " * 20
            for index in range(4)
        )
        chunks = split_transcript(document, 400)
        session = FakeSession([sample_notes_json(f"جزوهٔ {i}") for i in range(len(chunks))])

        async def on_merged(_merged):
            raise RuntimeError("telegram is down")

        notes = await structure_transcript(
            document, settings, session, on_merged=on_merged
        )
        self.assertEqual(notes.title, "جزوهٔ 0")


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
        # CR-3: a legacy "summary" mode is accepted but always behaves as full.
        notes = await structure_presentation(outline, "", settings, session, mode="summary")
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(
            session.calls[0]["json"]["messages"][1]["content"].startswith(PRESENTATION_PROMPT)
        )
        self.assertEqual(notes.note_mode, "full")


if __name__ == "__main__":
    unittest.main()


# ===========================================================================
# Grounding gate, repair and scrubbing
# ===========================================================================


GATE_SOURCE = (
    "دوز شروع ۵۰۰ mg همراه با وعدهٔ غذایی است. "
    "بیمار با نارسایی کلیوی نباید Metformin بگیرد. "
    "HbA1c برابر ۸.۵ درصد بود."
)


class GroundingGateTests(unittest.TestCase):
    def test_a_table_with_an_invented_number_is_dropped(self):
        payload = {
            "sections": [
                {
                    "heading": "دوز",
                    "paragraphs": ["دوز شروع ۵۰۰ mg است."],
                    "tables": [
                        {
                            "caption": "دوزها",
                            "headers": ["دارو", "دوز"],
                            "rows": [["Metformin", "500 mg"], ["Metformin", "750 mg"]],
                        }
                    ],
                }
            ]
        }
        gated, report = gate_notes(notes_from_dict(payload), GATE_SOURCE)
        self.assertEqual(gated.sections[0].all_tables, ())
        self.assertEqual(report.tables_rejected, 1)
        self.assertIn("750 mg", report.dropped_cells)

    def test_a_table_of_source_facts_survives(self):
        payload = {
            "sections": [
                {
                    "heading": "دوز",
                    "paragraphs": ["دوز شروع ۵۰۰ mg است."],
                    "tables": {
                        "caption": "دوزها",
                        "headers": ["دارو", "دوز"],
                        "rows": [["Metformin", "500 mg"]],
                    },
                }
            ]
        }
        gated, report = gate_notes(notes_from_dict(payload), GATE_SOURCE)
        self.assertEqual(report.tables_accepted, 0)
        self.assertEqual(report.tables_rejected, 1)
        self.assertEqual(len(gated.sections[0].all_tables), 0)

    def test_a_tip_without_a_basis_is_dropped(self):
        payload = {
            "sections": [{"heading": "دوز", "paragraphs": ["دوز شروع ۵۰۰ mg است."]}],
            "practical_tips": [
                {
                    "text": "در نارسایی کلیوی Metformin قطع می‌شود.",
                    "basis": "بیمار با نارسایی کلیوی نباید Metformin بگیرد",
                },
                {"text": "هر شب درس را مرور کنید.", "kind": "کاربردی"},
            ],
        }
        gated, report = gate_notes(notes_from_dict(payload), GATE_SOURCE)
        self.assertEqual(len(gated.practical_tips), 1)
        self.assertEqual(report.tips_rejected, 1)

    def test_tips_are_capped(self):
        payload = {
            "sections": [{"heading": "دوز", "paragraphs": ["دوز شروع ۵۰۰ mg است."]}],
            "practical_tips": [
                {
                    "text": f"توصیهٔ عملی شمارهٔ {index}",
                    "basis": "بیمار با نارسایی کلیوی نباید Metformin بگیرد",
                }
                for index in range(12)
            ],
        }
        gated, _ = gate_notes(notes_from_dict(payload), GATE_SOURCE)
        self.assertEqual(len(gated.practical_tips), MAX_PRACTICAL_TIPS)

    def test_objectives_and_questions_without_source_support_are_dropped(self):
        payload = {
            "sections": [{"heading": "دوز", "paragraphs": ["دوز شروع ۵۰۰ mg است."]}],
            "learning_objectives": [
                "دوز شروع ۵۰۰ mg همراه با وعدهٔ غذایی",
                "طراحی یک برنامهٔ ورزشی هفتگی برای بیماران دیابتی",
            ],
            "review_questions": [
                "دوز شروع Metformin چقدر است؟",
                "نقش تمرینات هوازی در کاهش قند خون چیست؟",
            ],
        }
        gated, report = gate_notes(notes_from_dict(payload), GATE_SOURCE)
        self.assertEqual(
            gated.learning_objectives, ("دوز شروع ۵۰۰ mg همراه با وعدهٔ غذایی",)
        )
        self.assertEqual(gated.review_questions, ("دوز شروع Metformin چقدر است؟",))
        self.assertEqual(report.objectives_dropped, 1)
        self.assertEqual(report.review_questions_dropped, 1)

    def test_warn_is_not_a_gate_flag(self):
        """``strict=False`` keeps everything; the audit still reports it."""
        payload = {
            "sections": [
                {
                    "heading": "دوز",
                    "paragraphs": ["دوز شروع ۵۰۰ mg است."],
                    "tables": {"headers": ["a"], "rows": [["۷۵۰ mg"]]},
                }
            ],
            "practical_tips": [{"text": "متن بی‌پشتوانه"}],
        }
        gated, report = gate_notes(notes_from_dict(payload), GATE_SOURCE, strict=False)
        self.assertEqual(len(gated.sections[0].all_tables), 1)
        self.assertEqual(len(gated.practical_tips), 1)
        # The counters say what ``repair`` mode would have dropped.
        self.assertEqual(report.tables_rejected, 1)
        self.assertEqual(report.tips_rejected, 1)
        self.assertTrue(report.audit.fabricated)


class RepairDocumentTests(unittest.TestCase):
    def test_missing_signal_is_listed_with_its_source_window(self):
        payload = {"sections": [{"heading": "دوز", "paragraphs": ["دارو بررسی شد."]}]}
        notes = notes_from_dict(payload)
        gated, report = gate_notes(notes, GATE_SOURCE)
        document = build_repair_document(gated, report, GATE_SOURCE)
        self.assertIn("موارد جامانده", document)
        self.assertIn("500 mg", document)
        self.assertIn("همراه با وعدهٔ غذایی", document)

    def test_fabricated_labels_are_listed(self):
        payload = {
            "sections": [
                {"heading": "دوز", "paragraphs": ["دوز ۵۰۰ mg و ۷۵۰ mg است."]}
            ]
        }
        gated, report = gate_notes(notes_from_dict(payload), GATE_SOURCE)
        document = build_repair_document(gated, report, GATE_SOURCE)
        self.assertIn("در متن منبع نیست", document)
        self.assertIn("750 mg", document)

    def test_repair_improves_requires_grounding(self):
        payload = {"sections": [{"heading": "h", "paragraphs": ["متن."]}]}
        base, base_report = gate_notes(notes_from_dict(payload), GATE_SOURCE)
        self.assertFalse(repair_improves(base_report, base_report))
        fabricated = {
            "sections": [{"heading": "h", "paragraphs": ["دوز ۷۵۰ mg است."]}]
        }
        _, fabricated_report = gate_notes(notes_from_dict(fabricated), GATE_SOURCE)
        self.assertFalse(repair_improves(fabricated_report, base_report))

    def test_notes_need_repair_for_missing_numbers(self):
        payload = {"sections": [{"heading": "دوز", "paragraphs": ["دارو بررسی شد."]}]}
        _, report = gate_notes(notes_from_dict(payload), GATE_SOURCE)
        self.assertTrue(notes_need_repair(report, mode="full"))

    def test_a_complete_note_does_not_need_repair(self):
        payload = {"sections": [{"heading": "دوز", "paragraphs": [GATE_SOURCE]}]}
        _, report = gate_notes(notes_from_dict(payload), GATE_SOURCE)
        self.assertFalse(notes_need_repair(report, mode="full"))


class FabricatedNumberScrubTests(unittest.TestCase):
    def test_the_whole_sentence_holding_a_fabricated_number_is_removed(self):
        payload = {
            "sections": [
                {
                    "heading": "دوز",
                    "paragraphs": [
                        "دوز شروع ۵۰۰ mg است. در برخی منابع دوز ۷۵۰ mg توصیه شده است."
                    ],
                }
            ]
        }
        notes = notes_from_dict(payload)
        _, report = gate_notes(notes, GATE_SOURCE)
        scrubbed, removed = scrub_fabricated_numbers(notes, report.audit)
        self.assertEqual(removed, 1)
        text = json.dumps(scrubbed.to_payload(), ensure_ascii=False)
        self.assertNotIn("750", text)
        self.assertIn("۵۰۰ mg", text)

    def test_each_removed_sentence_is_counted_once(self):
        """The same sentence in a callout, a tip and the glossary is one removal each."""
        payload = {
            "sections": [
                {
                    "heading": "دوز",
                    "paragraphs": ["دوز شروع ۵۰۰ mg است."],
                    "callouts": [{"kind": "هشدار", "text": "دوز ۷۵۰ mg خطرناک است."}],
                }
            ],
            "practical_tips": [
                {"text": "دوز ۷۵۰ mg را قطع کنید.", "basis": "دوز شروع ۵۰۰ mg", "kind": "هشدار"}
            ],
            "glossary": [{"term": "دوز", "definition": "مقدار دارو؛ ۷۵۰ mg حد کشنده است."}],
        }
        notes = notes_from_dict(payload)
        _, report = gate_notes(notes, GATE_SOURCE)
        scrubbed, removed = scrub_fabricated_numbers(notes, report.audit)
        self.assertEqual(removed, 3)
        self.assertEqual(scrubbed.sections[0].callouts, ())
        self.assertEqual(scrubbed.glossary, ())
        self.assertEqual(scrubbed.practical_tips, ())

    def test_clean_notes_are_untouched(self):
        payload = {"sections": [{"heading": "دوز", "paragraphs": [GATE_SOURCE]}]}
        notes = notes_from_dict(payload)
        _, report = gate_notes(notes, GATE_SOURCE)
        scrubbed, removed = scrub_fabricated_numbers(notes, report.audit)
        self.assertEqual(removed, 0)
        self.assertEqual(scrubbed, notes)


class StrictPipelineTests(unittest.IsolatedAsyncioTestCase):
    SOURCE = "دوز شروع 500 mg همراه با وعدهٔ غذایی است.\n\nبیمار نباید Metformin بگیرد."
    LOSSY = {
        "title": "جزوه",
        "sections": [{"heading": "دوز", "paragraphs": ["دوز شروع دارو بررسی شد."]}],
    }
    REPAIRED = {
        "title": "جزوه",
        "sections": [
            {
                "heading": "دوز",
                "paragraphs": [
                    "دوز شروع 500 mg همراه با وعدهٔ غذایی است.",
                    "بیمار نباید Metformin بگیرد.",
                ],
            }
        ],
    }

    async def test_missing_facts_trigger_one_repair_and_are_restored(self):
        settings = make_settings(note_strict_grounding=True)
        session = FakeSession([
            json.dumps(self.LOSSY, ensure_ascii=False),
            json.dumps(self.REPAIRED, ensure_ascii=False),
        ])
        notes = await structure_transcript(self.SOURCE, settings, session)
        self.assertEqual(len(session.calls), 2)
        text = json.dumps(notes.to_payload(), ensure_ascii=False)
        self.assertIn("500 mg", text)
        self.assertIn("Metformin", text)
        repair_prompt = session.calls[1]["json"]["messages"][1]["content"]
        self.assertIn("موارد جامانده", repair_prompt)

    async def test_warn_mode_never_calls_the_model_twice(self):
        settings = make_settings(note_strict_grounding=True, note_quality_gate="warn")
        session = FakeSession([json.dumps(self.LOSSY, ensure_ascii=False)])
        notes = await structure_transcript(self.SOURCE, settings, session)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(notes.sections), 1)

    async def test_warn_mode_changes_nothing_but_still_audits(self):
        """``warn`` is observation: not even a provably false table is dropped."""
        answer = {
            "title": "جزوه",
            "sections": [
                {
                    "heading": "دوز",
                    "paragraphs": ["دوز شروع 500 mg همراه با وعدهٔ غذایی است."],
                    "tables": [
                        {
                            "caption": "دوزها",
                            "headers": ["دارو", "دوز"],
                            "rows": [["Metformin", "1000 mg"]],
                        }
                    ],
                }
            ],
            "practical_tips": [
                {"text": "دوز را دو برابر کنید.", "basis": "چیزی که گفته نشد", "kind": "هشدار"}
            ],
        }
        settings = make_settings(note_strict_grounding=True, note_quality_gate="warn")
        session = FakeSession([json.dumps(answer, ensure_ascii=False)])
        with self.assertLogs("gamas_bot.notes", level="INFO") as logs:
            notes = await structure_transcript(self.SOURCE, settings, session)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(notes.practical_tips), 1)
        self.assertEqual(len(notes.sections[0].all_tables), 1)
        self.assertTrue(any("gate=warn" in line for line in logs.output))

    async def test_a_fabricated_number_is_scrubbed_when_repair_keeps_it(self):
        fabricated = {
            "title": "جزوه",
            "sections": [
                {
                    "heading": "دوز",
                    "paragraphs": [
                        "دوز شروع 500 mg همراه با وعدهٔ غذایی است.",
                        "دوز نگه‌دارنده 750 mg است.",
                        "بیمار نباید Metformin بگیرد.",
                    ],
                }
            ],
        }
        settings = make_settings(note_strict_grounding=True)
        session = FakeSession([
            json.dumps(fabricated, ensure_ascii=False),
            json.dumps(fabricated, ensure_ascii=False),
        ])
        notes = await structure_transcript(self.SOURCE, settings, session)
        text = json.dumps(notes.to_payload(), ensure_ascii=False)
        self.assertNotIn("750", text)
        self.assertIn("500 mg", text)
        self.assertIn("Metformin", text)

    async def test_grounding_off_keeps_the_legacy_single_call(self):
        settings = make_settings(note_strict_grounding=False)
        session = FakeSession([json.dumps(self.LOSSY, ensure_ascii=False)])
        await structure_transcript(self.SOURCE, settings, session)
        self.assertEqual(len(session.calls), 1)


# ===========================================================================
# CR-4: booklet completeness validator + coverage-gap fill
# ===========================================================================

class CompletenessValidatorTests(unittest.TestCase):
    """Pure, deterministic skeleton + source-coverage validation."""

    def full_notes(self) -> StructuredNotes:
        return StructuredNotes.from_payload({
            "title": "فارماکولوژی دیابت",
            "summary": "مرور داروهای کاهندهٔ قند خون.",
            "learning_objectives": ["آشنایی با Metformin"],
            "glossary": [{"term": "HbA1c", "definition": "هموگلوبین گلیکوزیله"}],
            "key_points": ["HbA1c هدف زیر ۷ درصد"],
            "sections": [{
                "heading": "Metformin",
                "paragraphs": ["خط اول درمان دیابت نوع ۲ است."],
                "definitions": [{"term": "Metformin", "definition": "داروی کاهندهٔ قند خون"}],
            }],
        })

    def test_skeleton_reports_every_block(self):
        skeleton = booklet_skeleton(self.full_notes())
        for key in (
            "cover_title", "objectives", "toc", "sections", "definitions",
            "examples", "steps", "formulas", "tables", "callouts",
            "key_points", "end_summary", "glossary",
        ):
            self.assertIn(key, skeleton)
        for key in REQUIRED_SKELETON_BLOCKS:
            self.assertTrue(skeleton[key], f"required skeleton block missing: {key}")

    def test_complete_booklet_meets_threshold(self):
        source = "خط اول درمان دیابت نوع ۲ است. HbA1c هدف زیر ۷ درصد است."
        report = assess_completeness(source, self.full_notes())
        self.assertIsInstance(report, CompletenessReport)
        self.assertTrue(report.skeleton_complete)
        self.assertGreaterEqual(report.coverage, COMPLETENESS_THRESHOLD)
        self.assertTrue(report.meets_threshold)
        self.assertTrue(meets_completeness_threshold(report))
        self.assertIn("coverage=", report.summary())

    def test_gutted_booklet_fails_skeleton_and_coverage(self):
        source = " ".join(
            f"مطلب آموزشی شمارهٔ {i} دربارهٔ الگوریتم و ساختمان داده است." for i in range(8)
        )
        gutted = StructuredNotes(title="جزوه")  # no sections/summary/glossary
        report = assess_completeness(source, gutted)
        self.assertFalse(report.skeleton_complete)
        self.assertLess(report.coverage, COMPLETENESS_THRESHOLD)
        self.assertFalse(report.meets_threshold)
        self.assertTrue(report.missing_units)

    def test_meets_threshold_honours_a_higher_floor(self):
        report = assess_completeness("متن کوتاه", self.full_notes())
        self.assertTrue(meets_completeness_threshold(report, threshold=0.0))
        self.assertFalse(meets_completeness_threshold(report, threshold=1.01))

    def test_coverage_gaps_are_detected_and_bounded(self):
        source = " ".join(
            f"نکتهٔ مستقل {i} دربارهٔ پردازش سیگنال دیجیتال و فیلترها است." for i in range(14)
        )
        notes = StructuredNotes(
            title="درس", summary="خلاصه", key_points=["نکته"],
            sections=[one_section("مقدمه", paragraphs=["سلام به کلاس"])],
            glossary=[GlossaryEntry("DSP", "پردازش سیگنال دیجیتال")],
        )
        gaps = find_coverage_gaps(source, notes, max_gaps=3)
        self.assertTrue(gaps)
        self.assertLessEqual(len(gaps), 9)  # bounded at max_gaps * 3
        self.assertTrue(all(isinstance(gap, str) for gap in gaps))

    def test_fill_prompt_and_document_enforce_grounding(self):
        # The no-hallucination contract is literally embedded in the fill prompt.
        for phrase in ("ممنوعیت افزودن", "متن منبع", "sections", "عدد"):
            self.assertIn(phrase, COVERAGE_FILL_SYSTEM_PROMPT)
        self.assertIn("هیچ ادعایی", COVERAGE_FILL_SYSTEM_PROMPT)
        doc = build_fill_document(["یک جملهٔ منبع"], StructuredNotes())
        self.assertIn("یک جملهٔ منبع", doc)  # source unit passed through verbatim
        self.assertIn("متن منبع", doc)
        self.assertIn("هیچ مطلب تازه", doc)


class SkeletonPromptTests(unittest.TestCase):
    def test_generation_prompts_mandate_the_complete_skeleton(self):
        for prompt in (build_system_prompt("full"), build_presentation_system_prompt("full")):
            for phrase in (
                "جلد و عنوان درس", "فهرست مطالب", "هدف کلی درس",
                "جمع‌بندی", "واژه‌نامه", "قانون پوشش",
                "learning_objectives", "glossary", "definitions",
                "examples", "steps", "formulas", "tables", "callouts",
            ):
                self.assertIn(phrase, prompt)
        self.assertIn("اسکلت لازم جزوه", COMPILE_SYSTEM_PROMPT)


class CoverageFillTests(unittest.IsolatedAsyncioTestCase):
    """The fill pass is opt-in, bounded, grounded and non-fatal."""

    SOURCE = " ".join(
        f"جملهٔ مستقل شمارهٔ {i} دربارهٔ ترمودینامیک و آنتروپی و تعادل گرمایی است."
        for i in range(6)
    )
    DRAFT = json.dumps({
        "title": "درس", "summary": "خلاصه", "key_points": ["نکته"],
        "glossary": [{"term": "entropy", "definition": "آنتروپی"}],
        "sections": [{"heading": "مقدمه", "paragraphs": ["سلام به کلاس"]}],
    }, ensure_ascii=False)
    FILL = json.dumps({
        "title": "درس",
        "sections": [{"heading": "ترمودینامیک", "paragraphs": [
            "جملهٔ مستقل شمارهٔ ۰ دربارهٔ ترمودینامیک و آنتروپی و تعادل گرمایی است."
        ]}],
    }, ensure_ascii=False)

    async def test_fill_disabled_keeps_a_single_request(self):
        settings = make_settings(note_chunk_chars=100_000, note_compile=False,
                                 note_strict_grounding=False)  # fill defaults off
        session = FakeSession([self.DRAFT])
        await structure_transcript(self.SOURCE, settings, session)
        self.assertEqual(len(session.calls), 1)

    async def test_fill_enabled_issues_grounded_bounded_requests_and_merges(self):
        settings = make_settings(note_chunk_chars=100_000, note_compile=False,
                                 note_strict_grounding=False, note_completeness_fill=True)
        session = FakeSession([self.DRAFT] + [self.FILL] * (MAX_COVERAGE_GAPS + 2))
        notes = await structure_transcript(self.SOURCE, settings, session)
        # one map call + at least one fill, never more than the bounded budget
        self.assertGreater(len(session.calls), 1)
        self.assertLessEqual(len(session.calls), 1 + MAX_COVERAGE_GAPS)
        self.assertGreater(len(notes.sections), 1)
        fill_call = session.calls[1]["json"]["messages"]
        system = fill_call[0]["content"]
        user = fill_call[1]["content"]
        self.assertIn("ممنوعیت افزودن", system)      # forbidden-additions contract
        self.assertIn("متن منبع", user)              # grounded, source-only input

    async def test_fill_failure_is_non_fatal(self):
        settings = make_settings(note_chunk_chars=100_000, note_compile=False,
                                 note_strict_grounding=False, note_completeness_fill=True)
        # only the map answer is available; the fill request raises and must be
        # swallowed so the audited draft is still delivered (never a refunded job).
        session = FakeSession([self.DRAFT])
        notes = await structure_transcript(self.SOURCE, settings, session)
        self.assertEqual(len(session.calls), 2)  # map + one failing fill
        self.assertEqual(notes.title, "درس")
