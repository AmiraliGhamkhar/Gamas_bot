"""Tests for the canonical document pipeline representations (Phase 2).

Pure dataclass/JSON tests: no LLM, no Telegram, no database. The pipeline
shape under test is

    raw input → NormalizedDocument → ContentPlan → StructuredSection
    → StructuredNotes

with provenance (source → topic → section → note) checkable at every hop.
"""

from __future__ import annotations

import json
import unittest

from gamas_bot.doc_extract import DocSection, ExtractedDoc
from gamas_bot.document_model import (
    ContentPlan,
    NormalizedDocument,
    PlanTopic,
    ProvenanceRecord,
    StructuredSection,
    build_provenance_trace,
    detect_language,
    notes_from_sections,
    plan_from_document,
)
from gamas_bot.learning import MAX_MODEL_INPUT_CHARS
from gamas_bot.notes import NoteDefinition, NoteSection, StructuredNotes
from gamas_bot.transcript import Paragraph, TranscriptDocument


def _extracted_doc() -> ExtractedDoc:
    return ExtractedDoc(
        "pdf",
        3,
        (
            DocSection("صفحهٔ ۱", "مقدمه", "متن کامل صفحهٔ اول با 500 mg."),
            DocSection("صفحهٔ ۲", "", "بدنهٔ صفحهٔ دوم بدون سرفصل."),
            DocSection("صفحهٔ ۳", "نتیجه", "جمع‌بندی نهایی منبع.", 2),
        ),
    )


def _section(heading: str, *paragraphs: str, locators: tuple[str, ...] = ()) -> NoteSection:
    return NoteSection(
        heading=heading,
        paragraphs=tuple(paragraphs) or ("بدنهٔ " + heading,),
        key_points=("نکتهٔ کلیدی " + heading,),
        definitions=(NoteDefinition("اصطلاح " + heading, "تعریف معتبر از منبع"),),
        source_locators=locators,
    )


class NormalizedDocumentTests(unittest.TestCase):
    def test_represents_complete_source_content(self):
        extracted = _extracted_doc()
        document = NormalizedDocument.from_extracted(extracted, title="درس فیزیولوژی")

        self.assertEqual(len(document.units), 3)
        self.assertEqual([unit.locator for unit in document.units], ["صفحهٔ ۱", "صفحهٔ ۲", "صفحهٔ ۳"])
        self.assertEqual([unit.ordinal for unit in document.units], [0, 1, 2])
        self.assertEqual([unit.kind for unit in document.units], ["page", "page", "page"])
        self.assertEqual(document.units[2].heading_level, 2)
        # Complete source: identical rendering to the extraction layer, char for char.
        self.assertEqual(document.text, extracted.text)
        self.assertEqual(len(document.text), len(extracted.text))
        self.assertEqual(document.page_count, 3)
        self.assertEqual(document.source_type, "pdf")
        self.assertEqual(document.language, "fa")
        self.assertEqual(document.provenance, "upload>extract:pdf")
        # Unit ids are stable and addressable.
        for index, unit in enumerate(document.units):
            self.assertEqual(unit.unit_id, f"u{index + 1:04d}")
            self.assertIs(document.unit(unit.unit_id), unit)
        self.assertIsNone(document.unit("u9999"))

    def test_from_text_and_to_extracted_round_trip(self):
        document = NormalizedDocument.from_text("  متن   خام   ساده  ", source_type="text")
        self.assertEqual(document.text, "متن خام ساده")
        self.assertEqual(len(document.units), 1)
        self.assertEqual(document.units[0].kind, "text")

        extracted = document.to_extracted()
        self.assertEqual(extracted.text, document.text)
        again = NormalizedDocument.from_extracted(extracted)
        self.assertEqual(again.text, document.text)
        self.assertEqual([u.text for u in again.units], [u.text for u in document.units])

    def test_extracted_round_trip_preserves_sections(self):
        extracted = _extracted_doc()
        restored = NormalizedDocument.from_extracted(extracted).to_extracted()
        self.assertEqual(restored.source_type, extracted.source_type)
        self.assertEqual(restored.page_count, extracted.page_count)
        self.assertEqual(restored.sections, extracted.sections)
        self.assertEqual(restored.text, extracted.text)

    def test_empty_heading_only_section_survives_serialization(self):
        extracted = ExtractedDoc(
            "docx", 1, (DocSection("بخش ۱", "سرفصل مستقل", ""),)
        )
        document = NormalizedDocument.from_extracted(extracted)
        restored = NormalizedDocument.from_json(document.to_json())

        self.assertEqual(len(restored.units), 1)
        self.assertEqual(restored.units[0].heading, "سرفصل مستقل")
        self.assertEqual(restored.units[0].text, "")
        self.assertEqual(restored.to_extracted().sections, extracted.sections)

    def test_from_transcript_uses_llm_document_and_keeps_stats_as_metadata(self):
        transcript = TranscriptDocument(
            paragraphs=(
                Paragraph("پاراگراف اول درس.", 0.0, 4.0, "0"),
                Paragraph("پاراگراف دوم درس با فرمول E=mc².", 4.0, 9.5, "1"),
            ),
            confidence=0.92,
            speakers=2,
            diarization_attempted=True,
            overlap_count=1,
        )
        document = NormalizedDocument.from_transcript(transcript, title="جلسهٔ ۳")

        self.assertEqual(document.text, transcript.llm_document())
        self.assertEqual(document.source_type, "transcript")
        self.assertEqual(document.provenance, "audio>transcribe")
        metadata = document.metadata_map
        self.assertEqual(metadata["speakers"], "2")
        self.assertEqual(metadata["confidence"], "0.920")
        self.assertEqual(metadata["diarization_attempted"], "true")
        self.assertEqual(metadata["overlap_count"], "1")

    def test_serialization_round_trip(self):
        document = NormalizedDocument.from_extracted(
            _extracted_doc(),
            title="درس",
            document_id="doc-fixed-1",
            metadata=(("sha256", "abc123"), ("filename", "lesson.pdf")),
        )
        restored = NormalizedDocument.from_json(document.to_json())
        self.assertEqual(restored, document)
        self.assertEqual(restored.text, document.text)
        self.assertEqual(restored.stable_id, "doc-fixed-1")

    def test_stable_id_is_content_derived_when_not_explicit(self):
        document = NormalizedDocument.from_text("متن یکسان")
        self.assertTrue(document.stable_id.startswith("doc-"))
        self.assertEqual(
            document.stable_id, NormalizedDocument.from_text("متن یکسان").stable_id
        )
        self.assertNotEqual(
            document.stable_id, NormalizedDocument.from_text("متن دیگر").stable_id
        )

    def test_from_payload_rejects_non_object_and_tolerates_junk_units(self):
        with self.assertRaises(ValueError):
            NormalizedDocument.from_payload(["not", "a", "dict"])
        restored = NormalizedDocument.from_payload(
            {"units": [{"unit_id": "u1", "text": "متن", "ordinal": 0}, {"bad": True}, 42]}
        )
        self.assertEqual(len(restored.units), 1)

    def test_long_source_is_never_truncated(self):
        body = "کلمهٔ آموزشی " * 120_000  # ~1.56M chars, far beyond any model budget
        marker = "انتهای سند بسیار بلند"
        long_text = body + marker
        self.assertGreater(len(long_text), MAX_MODEL_INPUT_CHARS)

        document = NormalizedDocument.from_text(long_text)
        self.assertEqual(document.text, long_text.strip())
        self.assertTrue(document.text.endswith(marker))
        self.assertEqual(len(document.text), len(long_text.strip()))

        extracted = ExtractedDoc("pdf", 2, (
            DocSection("صفحهٔ ۱", "", long_text),
            DocSection("صفحهٔ ۲", "", marker),
        ))
        document = NormalizedDocument.from_extracted(extracted)
        self.assertEqual(document.text, extracted.text)
        self.assertTrue(document.text.endswith(marker))
        # Serialization also carries the full content.
        self.assertEqual(NormalizedDocument.from_json(document.to_json()).text, document.text)


class LanguageDetectionTests(unittest.TestCase):
    def test_detect_language(self):
        self.assertEqual(detect_language("این یک متن فارسی است"), "fa")
        self.assertEqual(detect_language("This is an English sentence"), "en")
        self.assertEqual(detect_language("12345 !!!"), "und")
        self.assertEqual(detect_language(""), "und")
        self.assertEqual(detect_language(None), "und")


class ContentPlanTests(unittest.TestCase):
    def test_plan_represents_ordered_topics_with_coverage(self):
        document = NormalizedDocument.from_extracted(_extracted_doc())
        plan = plan_from_document(document)

        self.assertEqual(plan.document_id, document.stable_id)
        self.assertEqual(plan.language, document.language)
        self.assertEqual(len(plan.topics), 3)
        # Ordered topics: tuple order is document order.
        self.assertEqual(
            [topic.title for topic in plan.topics], ["مقدمه", "صفحهٔ ۲", "نتیجه"]
        )
        self.assertEqual([topic.topic_id for topic in plan.topics], ["t001", "t002", "t003"])
        # Complete source coverage: every unit is covered by exactly one topic.
        covered = [unit_id for topic in plan.topics for unit_id in topic.unit_ids]
        self.assertEqual(covered, [unit.unit_id for unit in document.units])
        # Relationships: a linear dependency chain in source order.
        self.assertEqual(plan.topics[0].depends_on, ())
        self.assertEqual(plan.topics[1].depends_on, ("t001",))
        self.assertEqual(plan.topics[2].depends_on, ("t002",))
        # Purpose, depth and human-readable source references are populated.
        self.assertTrue(all(topic.purpose for topic in plan.topics))
        self.assertTrue(all(topic.depth == "standard" for topic in plan.topics))
        self.assertEqual(plan.topics[0].locators, ("صفحهٔ ۱",))
        self.assertEqual(plan.issues(document), [])

    def test_plan_supports_concepts_and_depth(self):
        topic = PlanTopic(
            topic_id="t1",
            title="تنفس سلولی",
            purpose="مفهوم محوری فصل",
            unit_ids=("u0001",),
            concepts=("گلیکولیز", "چرخهٔ کربس"),
            depth="detailed",
        )
        plan = ContentPlan(document_id="doc-1", language="fa", topics=(topic,))
        self.assertEqual(plan.topic("t1").concepts, ("گلیکولیز", "چرخهٔ کربس"))
        self.assertEqual(plan.topic("t1").depth, "detailed")
        self.assertIsNone(plan.topic("nope"))

    def test_issues_report_dangling_references(self):
        document = NormalizedDocument.from_text("متن")
        plan = ContentPlan(
            document_id=document.stable_id,
            topics=(
                PlanTopic("t1", "الف", unit_ids=("u-missing",), depends_on=("t9",)),
                PlanTopic("t1", "ب"),
                PlanTopic("t2", ""),
            ),
        )
        problems = plan.issues(document)
        self.assertTrue(any("duplicate topic id" in problem for problem in problems))
        self.assertTrue(any("depends on unknown topic t9" in problem for problem in problems))
        self.assertTrue(any("covers unknown unit u-missing" in problem for problem in problems))
        self.assertTrue(any("has no title" in problem for problem in problems))

    def test_plan_serialization_round_trip(self):
        document = NormalizedDocument.from_extracted(_extracted_doc())
        plan = plan_from_document(document, depth="detailed")
        restored = ContentPlan.from_json(plan.to_json())
        self.assertEqual(restored, plan)
        # Unknown depth values fall back to the default rather than failing.
        tolerant = ContentPlan.from_payload(
            {"topics": [{"topic_id": "t1", "title": "الف", "depth": "extreme"}]}
        )
        self.assertEqual(tolerant.topics[0].depth, "standard")
        with self.assertRaises(ValueError):
            ContentPlan.from_payload("not-a-plan")

    def test_empty_document_yields_empty_plan(self):
        plan = plan_from_document(NormalizedDocument())
        self.assertEqual(plan.topics, ())
        self.assertEqual(plan.issues(), [])


class StructuredSectionTests(unittest.TestCase):
    def test_sections_preserve_ordering(self):
        sections = [
            StructuredSection(f"s{i:03d}", i, _section(f"بخش {i}"), topic_id=f"t{i:03d}")
            for i in (3, 1, 2)  # deliberately unordered input
        ]
        notes = notes_from_sections(sections)
        self.assertEqual(
            [section.heading for section in notes.sections], ["بخش 1", "بخش 2", "بخش 3"]
        )

    def test_notes_compatibility_and_structured_elements(self):
        body = _section("تنفس", "پاراگراف آموزشی.", locators=("صفحهٔ ۲",))
        section = StructuredSection.from_note_section(
            body, section_id="s001", order=0, topic_id="t001", source_refs=("u0002",)
        )
        self.assertEqual(section.title, "تنفس")
        self.assertEqual(section.locators, ("صفحهٔ ۲",))
        self.assertIs(section.to_note_section(), body)

        # The assembled note is an ordinary StructuredNotes: the established
        # payload contract (and therefore every renderer) works unchanged.
        notes = notes_from_sections(
            [section], base=StructuredNotes(title="جزوه", summary="خلاصه")
        )
        self.assertEqual(notes.title, "جزوه")
        self.assertEqual(notes.summary, "خلاصه")
        payload = notes.to_payload()
        self.assertEqual(payload["sections"][0]["heading"], "تنفس")
        self.assertEqual(payload["sections"][0]["source_locators"], ["صفحهٔ ۲"])
        self.assertEqual(payload["sections"][0]["definitions"][0]["term"], "اصطلاح تنفس")
        restored = StructuredNotes.from_payload(json.loads(notes.to_json()))
        self.assertEqual(restored.sections[0], body)
        self.assertEqual(restored.title, "جزوه")

    def test_section_serialization_round_trip(self):
        section = StructuredSection(
            "s010",
            9,
            _section("جدول‌دار", "بدنه.", locators=("اسلاید ۴",)),
            topic_id="t010",
            source_refs=("u0010", "u0011"),
        )
        restored = StructuredSection.from_json(section.to_json())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.section_id, "s010")
        self.assertEqual(restored.order, 9)
        self.assertEqual(restored.topic_id, "t010")
        self.assertEqual(restored.source_refs, ("u0010", "u0011"))
        self.assertEqual(restored.body, section.body)
        self.assertIsNone(StructuredSection.from_payload({"body": {"heading": "بدون محتوا"}}))
        self.assertIsNone(StructuredSection.from_json("not json"))

    def test_notes_from_sections_drops_empty_bodies(self):
        empty = StructuredSection("s1", 0, NoteSection(heading="خالی"))
        full = StructuredSection("s2", 1, _section("پر"))
        notes = notes_from_sections([empty, full])
        self.assertEqual([section.heading for section in notes.sections], ["پر"])


class ProvenanceTests(unittest.TestCase):
    def test_source_to_topic_to_section_to_note_chain(self):
        document = NormalizedDocument.from_extracted(_extracted_doc())
        plan = plan_from_document(document)
        sections = [
            StructuredSection(
                f"s{index:03d}",
                index,
                _section(topic.title, f"بدنهٔ ساخته‌شده از {topic.title}.", locators=topic.locators),
                topic_id=topic.topic_id,
                source_refs=topic.unit_ids,
            )
            for index, topic in enumerate(plan.topics)
        ]
        notes = notes_from_sections(sections)

        trace = build_provenance_trace(document, plan, sections)
        self.assertEqual(len(trace), 3)
        self.assertTrue(all(isinstance(record, ProvenanceRecord) for record in trace))
        for record, unit, topic, note_section in zip(
            trace, document.units, plan.topics, notes.sections, strict=True
        ):
            # source → topic
            self.assertEqual(record.unit_id, unit.unit_id)
            self.assertEqual(record.locator, unit.locator)
            self.assertEqual(record.topic_id, topic.topic_id)
            self.assertEqual(record.topic_title, topic.title)
            self.assertIn(record.unit_id, topic.unit_ids)
            # topic → generated section → final structured note
            self.assertEqual(record.section_id, f"s{record.order:03d}")
            self.assertEqual(record.section_title, note_section.heading)
            # The final note section still names its human-readable source.
            if unit.locator:
                self.assertIn(unit.locator, note_section.source_locators)

    def test_trace_survives_serialization_of_every_layer(self):
        document = NormalizedDocument.from_extracted(_extracted_doc())
        plan = plan_from_document(document)
        sections = [
            StructuredSection(
                "s001", 0, _section(plan.topics[0].title), topic_id="t001",
                source_refs=plan.topics[0].unit_ids,
            )
        ]
        # Serialize and restore every layer, then re-trace.
        document = NormalizedDocument.from_json(document.to_json())
        plan = ContentPlan.from_json(plan.to_json())
        sections = [StructuredSection.from_json(section.to_json()) for section in sections]
        trace = build_provenance_trace(document, plan, sections)
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0].unit_id, "u0001")
        self.assertEqual(trace[0].topic_id, "t001")
        self.assertEqual(trace[0].section_id, "s001")

    def test_trace_falls_back_to_topic_coverage_and_skips_unknown_units(self):
        document = NormalizedDocument.from_text("متن واحد")
        plan = plan_from_document(document)
        # No explicit source_refs: the topic's coverage carries provenance.
        section = StructuredSection("s001", 0, _section("بخش 1"), topic_id="t001")
        trace = build_provenance_trace(document, plan, [section])
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0].unit_id, "u0001")
        # A dangling reference is skipped by the trace and reported by issues().
        dangling = StructuredSection(
            "s002", 1, _section("نا معتبر"), topic_id="t001", source_refs=("u9999",)
        )
        self.assertEqual(build_provenance_trace(document, plan, [dangling]), ())


if __name__ == "__main__":
    unittest.main()
