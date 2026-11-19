"""Offline regressions for lossless merge and bounded edit-only synthesis."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest import mock

from gamas_bot import notes as n
from gamas_bot.document_model import NormalizedDocument, plan_from_document
from gamas_bot.llm import LLMContextOverflowError, LLMOutputBudgetError
from tests.support import make_settings


class DeterministicMergeTests(unittest.TestCase):
    def test_exact_repetition_only_and_provenance_union(self):
        first = n.NoteSection("انتقال", paragraphs=("واحدها بررسی شدند.", "طول نمونه 5 cm است."),
                              definitions=(n.NoteDefinition("طول", "فاصلهٔ دو نقطه"),),
                              source_locators=("صفحهٔ نخست",), source_block_ids=("block-a",))
        second = n.NoteSection("ادامهٔ انتقال", paragraphs=("واحدها بررسی شدند.", "طول نمونه 7 cm است.",
                                                         "طول نمونه 5 mm است."),
                               definitions=(n.NoteDefinition("طول", "فاصله در راستای نمونه"),),
                               source_locators=("صفحهٔ دوم",), source_block_ids=("block-b",))
        pieces = [n.StructuredNotes(sections=(first,)), n.StructuredNotes(sections=(second,))]
        merged = n.merge_structured_notes(pieces)
        self.assertEqual(merged, n.merge_structured_notes(pieces))
        self.assertEqual(len(merged.sections), 1)
        section = merged.sections[0]
        self.assertEqual(section.paragraphs, (*first.paragraphs, *second.paragraphs[1:]))
        self.assertEqual(section.definitions, (*first.definitions, *second.definitions))
        self.assertEqual(section.source_block_ids, ("block-a", "block-b"))
        self.assertEqual(section.source_locators, ("صفحهٔ نخست", "صفحهٔ دوم"))
        self.assertTrue(any(item.kind == "potential_value_conflict" for item in merged.merge_conflicts))

    def test_duplicate_headings_and_conflicting_identifiers_are_not_equivalence(self):
        a = n.NoteSection("نمونه", paragraphs=("گزارهٔ اول",), section_id="same", topic_id="first")
        b = replace(a, paragraphs=("گزارهٔ دوم",), parent_topic_id="unexpected")
        c = replace(a, section_id="another", topic_id="second", paragraphs=("گزارهٔ سوم",))
        draft = n.StructuredNotes(sections=(a, b, c))
        merged = n.merge_structured_notes([draft])
        self.assertEqual([section.section_id for section in merged.sections], ["same", "same~2", "another"])
        self.assertEqual([section.paragraphs for section in merged.sections], [s.paragraphs for s in draft.sections])
        self.assertIn(n.MergeConflict("section_id", ("same", "same~2")), merged.merge_conflicts)
        self.assertIn(n.MergeConflict("topic_parent", ("first",)), merged.merge_conflicts)

    def test_round_trip_preserves_distinct_table_captions_and_large_summary(self):
        a = n.NoteTable(["ویژگی"], [["الف"]], "گروه نخست")
        b = n.NoteTable(["ویژگی"], [["ب"]], "گروه دوم")
        first = n.StructuredNotes(summary="الف" * 16_000, sections=(n.NoteSection("درس", table=a),))
        second = n.StructuredNotes(summary="ب" * 16_000, sections=(n.NoteSection("درس", table=b),))
        merged = n.merge_structured_notes([first, second])
        self.assertEqual(merged.sections[0].all_tables, (a, b))
        stored = n.StructuredNotes.from_payload(json.loads(merged.to_json()))
        self.assertEqual(stored, merged)


class BookletSynthesisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.document = NormalizedDocument.from_text("طول نمونه 5 cm است. طول نمونه 7 cm است. شرح ویژگی نمونه.")
        self.plan = plan_from_document(self.document)
        topic = self.plan.topics[0]
        ref = topic.unit_ids[0]
        self.sections = tuple(n.NoteSection(
            heading=heading, paragraphs=(text, "شرح مستند ویژگی نمونه و تفاوت شرایط اندازه‌گیری در آزمایشگاه. " * 5),
            topic_id=topic.id, section_id=f"section-{i}", source_block_ids=(ref,),
            source_locators=(label,), steps=("گام نخست", "گام دوم"),
        ) for i, (heading, text, label) in enumerate((
            ("ادامهٔ طول نمونه", "طول نمونه 5 cm است.", "صفحهٔ نخست"),
            ("اندازه‌گیری دیگر", "طول نمونه 7 cm است.", "صفحهٔ دوم"),
        )))
        self.baseline = n.merge_structured_notes([n.StructuredNotes(
            title="مرور ویژگی نمونه", sections=self.sections,
            input_ranges=tuple(topic.source_ranges), glossary=(n.GlossaryEntry("نمونه", "مادهٔ بررسی‌شده"),),
        )])

    async def synthesize(self, fn, *, baseline=None, **settings):
        async def complete(_session, _settings, *, system, user, **kwargs):
            self.assertEqual(system, n.COMPILE_SYSTEM_PROMPT)
            self.assertEqual(kwargs["task"], "BOOKLET_COMPILE")
            self.assertTrue(kwargs["adaptive_capacity"] and kwargs["strict_json"])
            return fn(json.loads(user))

        router = n.SectionWriter(make_settings(note_compile=True, **settings), object())
        with mock.patch.object(n, "complete_json", complete):
            return await n.synthesize_booklet(baseline or self.baseline, self.plan, self.document, router)

    async def test_edit_only_adds_grounded_summary_comparison_and_conflict_link(self):
        def reply(request):
            first, second = request["sections"]
            self.assertEqual(request["potential_conflicts"], [[first["id"], second["id"]]])
            return {
                "headings": [{"id": first["id"], "heading": "طول نمونه"}],
                "summaries": [{"id": first["id"], "quotes": [first["paragraphs"][0]]}],
                "comparisons": [{"id": first["id"], "rows": [
                    {"id": section["id"], "quote": section["paragraphs"][0]} for section in (first, second)
                ]}],
                "links": [{"from_id": first["id"], "to_id": second["id"], "kind": "potential_conflict"}],
            }

        result = await self.synthesize(reply)
        self.assertTrue(n.compile_accepts(result, self.baseline))
        first, second = result.sections
        self.assertEqual(first.heading, "طول نمونه")
        self.assertEqual(first.paragraphs, self.sections[0].paragraphs)
        self.assertEqual(first.steps, self.sections[0].steps)
        self.assertEqual(first.key_points, ("طول نمونه 5 cm است.",))
        self.assertEqual(first.source_locators, ("صفحهٔ نخست", "صفحهٔ دوم"))
        self.assertEqual(first.source_block_ids, self.sections[0].source_block_ids)
        self.assertEqual(second.section_id, self.sections[1].section_id)
        self.assertIn("ناهمخوانی احتمالی", first.callouts[-1].text)
        assert first.table is not None
        self.assertEqual([row[1] for row in first.table.rows], ["طول نمونه 5 cm است.", "طول نمونه 7 cm است."])
        self.assertEqual(result.input_ranges, self.baseline.input_ranges)
        self.assertEqual(result.glossary, self.baseline.glossary)

    async def test_exact_repetition_is_removed_without_touching_near_matches(self):
        first, second = self.baseline.sections
        first = replace(first, paragraphs=("طول نمونه 5 cm است.", "طول نمونه 5 cm است.",
                                           "طول نمونه 5 mm است.", *first.paragraphs[1:]))
        draft = replace(self.baseline, sections=(first, second))
        result = await self.synthesize(lambda _: {
            "order": None, "title": None, "headings": None, "links": None,
            "summaries": None, "comparisons": None,
            "deduplicate": [{"id": "s0001", "field": "paragraphs"}],
        }, baseline=draft, note_structured_output=True)
        self.assertEqual(result.sections[0].paragraphs, tuple(dict.fromkeys(first.paragraphs)))
        self.assertIn("طول نمونه 5 mm است.", result.sections[0].paragraphs)
        self.assertTrue(n.compile_accepts(result, draft))

    async def test_lossy_or_ungrounded_proposals_leave_baseline(self):
        proposals = (
            {"sections": [{"heading": "بازنویسی", "paragraphs": ["متن جایگزین"]}]},
            {"order": ["s0001"]},
            {"order": ["s0002", "s0001"]},  # same topic, original continuation order
            {"headings": [{"id": "s0001", "heading": "نمونه"}]},
            {"summaries": [{"id": "s0001", "quotes": ["طول نمونه 50 cm است."]}]},
            {"links": [{"from_id": "s0001", "to_id": "missing", "kind": "related"}]},
        )
        for proposal in proposals:
            with self.subTest(proposal=proposal):
                result = await self.synthesize(lambda _, proposal=proposal: proposal)
                self.assertEqual(result, replace(self.baseline, quality_state=n.QUALITY_RECOVERED))
                self.assertEqual(result.sections[0].source_block_ids, self.sections[0].source_block_ids)

    async def test_reordering_moves_an_independent_chapter_not_its_prerequisite(self):
        self.document = NormalizedDocument.from_source_text(
            "# مبانی نور\nشرح مفهوم نخست.\n## انتشار نور\nشرح زیرموضوع.\n# مرور اندازه‌گیری\nشرح مستقل."
        )
        self.plan = plan_from_document(self.document)
        self.assertEqual(len(self.plan.topics), 3)
        root, child, independent = self.plan.topics
        self.assertEqual(child.parent_id, root.id)
        # Supply a valid plan that explicitly makes the third topic independent.
        self.plan = replace(self.plan, topics=(root, child, replace(independent, depends_on=())))
        sections = tuple(replace(self.sections[0], heading=topic.title, topic_id=topic.id,
                                 parent_topic_id=topic.parent_id, section_id=f"section-{i}",
                                 source_block_ids=topic.unit_ids) for i, topic in enumerate(self.plan.topics))
        draft = n.StructuredNotes(title="آموزش نور", sections=sections)
        good = await self.synthesize(lambda _: {"order": ["s0003", "s0001", "s0002"]}, baseline=draft)
        self.assertEqual([s.topic_id for s in good.sections], [independent.id, root.id, child.id])
        self.assertTrue(n.compile_accepts(good, draft))
        bad = await self.synthesize(lambda _: {"order": ["s0002", "s0001", "s0003"]}, baseline=draft)
        self.assertEqual(bad, replace(draft, quality_state=n.QUALITY_RECOVERED))

    async def test_typed_overflow_walks_groups_then_chapter_and_final(self):
        for error in (LLMContextOverflowError, LLMOutputBudgetError):
            with self.subTest(error=error.__name__):
                requests: list[dict] = []

                def reply(request, requests=requests, error=error):
                    requests.append(request)
                    if request["level"] == "whole":
                        raise error("limit")
                    if request["level"] == "topic_group" and request["sections"][0]["id"] == "s0002":
                        raise error("single section cannot fit")
                    if request["level"] == "chapter":
                        first = request["sections"][0]
                        return {"summaries": [{"id": first["id"], "quotes": [first["highlight"]]}]}
                    return {}

                result = await self.synthesize(reply)
                self.assertEqual([p["level"] for p in requests], ["whole", "topic_group", "topic_group", "chapter", "final"])
                self.assertEqual([r["sections"][0]["paragraphs"] for r in requests[1:3]],
                                 [list(section.paragraphs) for section in self.sections])
                self.assertEqual([s["id"] for s in requests[-1]["sections"]], ["s0001", "s0002"])
                self.assertNotIn("paragraphs", requests[-1]["sections"][0])
                self.assertEqual(result.quality_state, n.QUALITY_RECOVERED)
                self.assertEqual(result.sections[0].key_points, (self.sections[0].paragraphs[0],))
                self.assertEqual(result.sections[1], self.sections[1])

    async def test_strict_synthesis_rejection_never_replaces_approved_baseline(self):
        original_gate = n.gate_notes

        def reject_edits(candidate, *args, **kwargs):
            gated, report = original_gate(candidate, *args, **kwargs)
            return replace(gated, sections=()), report

        with mock.patch.object(n, "gate_notes", reject_edits):
            result = await self.synthesize(lambda _: {
                "headings": [{"id": "s0001", "heading": "طول نمونه"}],
            }, note_strict_grounding=True)
        self.assertEqual(result, replace(self.baseline, quality_state=n.QUALITY_RECOVERED))

    async def test_disabled_or_no_complete_request_fits(self):
        settings = make_settings(note_compile=False)
        router = n.SectionWriter(settings, object())
        with mock.patch.object(n, "complete_json", side_effect=AssertionError("must not send")):
            self.assertIs(await n.synthesize_booklet(self.baseline, self.plan, self.document, router), self.baseline)
        result = await self.synthesize(lambda _: self.fail("must not send"), note_compile_max_chars=20)
        self.assertEqual(result, replace(self.baseline, quality_state=n.QUALITY_RECOVERED))

    def test_preservation_gate_detects_moved_or_removed_refs(self):
        candidate = replace(self.baseline, sections=(replace(self.sections[0], source_block_ids=()), self.sections[1]))
        self.assertFalse(n.compile_accepts(candidate, self.baseline))
        candidate = replace(self.baseline, sections=(replace(self.sections[0], section_id="changed"), self.sections[1]))
        self.assertFalse(n.compile_accepts(candidate, self.baseline))

    def test_required_prompt_instructions_and_separate_repair(self):
        for line in (
            "EDIT AND SYNTHESIZE EXISTING STRUCTURED CONTENT.", "DO NOT REWRITE THE ENTIRE BOOKLET FROM SCRATCH.",
            "DO NOT REMOVE UNIQUE EDUCATIONAL INFORMATION.", "DO NOT INVENT SOURCE-SUPPORTED FACTS.",
            "DO NOT CHANGE VALUES OR UNITS WITHOUT EVIDENCE.",
        ):
            self.assertIn(line, n.COMPILE_SYSTEM_PROMPT)
            self.assertNotIn(line, n.edit_system_prompt())
