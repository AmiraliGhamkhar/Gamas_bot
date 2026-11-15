"""Regression tests for the canonical source-grounded educational booklet."""

from __future__ import annotations

import io
import unittest
from datetime import datetime

from docx import Document
from pypdf import PdfReader

from gamas_bot.docx_export import DocumentMeta, build_notes_docx
from gamas_bot.notes import (
    FORBIDDEN_BOOKLET_METADATA_FIELDS,
    GlossaryEntry,
    NoteDefinition,
    NoteQualityError,
    NoteSection,
    NoteTable,
    StructuredNotes,
    StructuringError,
    acceptance_failure,
    booklet_output_violations,
    enforce_final_acceptance,
    gate_notes,
    instructional_source_text,
    notes_response_schema,
)
from gamas_bot.notes_pdf import build_notes_pdf
from gamas_bot.transcript import Paragraph, TranscriptDocument


SOURCE = (
    "Metformin خط اول درمان است. دوز ۵۰۰ mg باید بدون تغییر ثبت شود. "
    "در نارسایی کلیه، دوز را تغییر ندهید مگر منبع معیار دیگری بیان کرده باشد."
)


def complete_notes() -> StructuredNotes:
    return StructuredNotes(
        title="فارماکولوژی دیابت",
        subtitle="Metformin و ثبت دوز",
        learning_objectives=("ثبت دقیق دوز ۵۰۰ mg",),
        sections=(
            NoteSection(
                heading="Metformin",
                paragraphs=("Metformin خط اول درمان است.",),
                definitions=(
                    NoteDefinition("Metformin", "خط اول درمان است."),
                ),
                tables=(
                    NoteTable(
                        headers=["دارو", "دوز"],
                        rows=[["Metformin", "۵۰۰ mg"]],
                        caption="ثبت دوز",
                    ),
                ),
            ),
        ),
        common_mistakes=("دوز را تغییر ندهید مگر منبع معیار دیگری بیان کرده باشد.",),
        glossary=(GlossaryEntry("Metformin", "خط اول درمان است."),),
        final_review=("دوز ۵۰۰ mg باید بدون تغییر ثبت شود.",),
    )


class CanonicalBookletContractTests(unittest.TestCase):
    def test_schema_is_closed_and_has_no_transcript_metadata(self) -> None:
        schema = notes_response_schema()
        self.assertFalse(schema["additionalProperties"])
        properties = schema["properties"]
        self.assertIn("final_review", properties)
        self.assertIn("common_mistakes", properties)
        self.assertTrue(FORBIDDEN_BOOKLET_METADATA_FIELDS.isdisjoint(properties))
        section = properties["sections"]["items"]
        self.assertFalse(section["additionalProperties"])
        self.assertTrue(FORBIDDEN_BOOKLET_METADATA_FIELDS.isdisjoint(section["properties"]))

    def test_parser_rejects_speaker_and_timeline_fields_at_any_level(self) -> None:
        base = {
            "title": "جزوه",
            "sections": [{"heading": "مبحث", "paragraphs": ["متن منبع"]}],
            "final_review": ["متن منبع"],
        }
        for field, value in (
            ("speakers", ["استاد"]),
            ("timeline", ["[00:30] متن"]),
            ("timestamp", "00:30"),
        ):
            with self.subTest(field=field):
                payload = dict(base)
                payload[field] = value
                with self.assertRaises(StructuringError):
                    StructuredNotes.from_payload(payload)

        nested = dict(base)
        nested["sections"] = [{"heading": "مبحث", "paragraphs": ["متن"], "speaker_id": "s1"}]
        with self.assertRaises(StructuringError):
            StructuredNotes.from_payload(nested)

    def test_final_gate_refuses_missing_critical_number(self) -> None:
        notes = StructuredNotes(
            title="دوز",
            sections=(NoteSection(heading="دوز", paragraphs=("دوز ثبت می‌شود.",)),),
            final_review=("دوز ثبت می‌شود.",),
        )
        _gated, report = gate_notes(notes, SOURCE, strict=True)
        self.assertEqual(acceptance_failure(report), "missing_numbers")
        with self.assertRaises(NoteQualityError):
            enforce_final_acceptance(notes, report, stage="test")

    def test_presentation_source_metadata_is_not_booklet_evidence_or_output(self) -> None:
        source = (
            "### اسلاید ۷ — ایندکس\n"
            "- نیم‌سال: بهار ۱۴۰۵\n"
            "- گوینده: استاد نمونه\n"
            "یادداشت گوینده: ایندکس سرعت خواندن را بالا می‌برد."
        )
        projected = instructional_source_text(source)
        self.assertIn("ایندکس سرعت خواندن را بالا می‌برد.", projected)
        self.assertNotIn("۱۴۰۵", projected)
        self.assertNotIn("گوینده", projected)
        leaked = StructuredNotes(
            title="ایندکس",
            sections=(NoteSection(
                heading="اسلاید ۷",
                paragraphs=("یادداشت گوینده: ایندکس سرعت خواندن را بالا می‌برد.",),
            ),),
            final_review=("ایندکس سرعت خواندن را بالا می‌برد.",),
        )
        self.assertIn("speaker_reference", booklet_output_violations(leaked))
        self.assertIn("source_metadata_reference", booklet_output_violations(leaked))

    def test_booklet_object_and_markdown_contain_no_transcript_metadata(self) -> None:
        notes = complete_notes()
        payload = notes.to_payload()
        self.assertTrue(FORBIDDEN_BOOKLET_METADATA_FIELDS.isdisjoint(payload))
        self.assertEqual(booklet_output_violations(notes), ())
        markdown = notes.to_markdown()
        self.assertIn("مرور نهایی", markdown)
        self.assertNotIn("گویندگان", markdown)
        self.assertNotIn("زمان‌بندی", markdown)


class BookletRenderingContractTests(unittest.TestCase):
    def test_docx_and_pdf_render_educational_sections_without_speaker_appendix(self) -> None:
        notes = complete_notes()
        meta = DocumentMeta(
            reference="GMS-42",
            source_name="lecture.mp3",
            engine="speechmatics",
            created_at=datetime(2026, 11, 14, 9, 30),
        )
        docx = build_notes_docx(notes, meta=meta)
        document = Document(io.BytesIO(docx))
        docx_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        self.assertIn("مرور نهایی", docx_text)
        self.assertIn("خطاها و دام‌های رایج", docx_text)
        self.assertNotIn("گویندگان و زمان", docx_text)
        self.assertNotIn("حالت تولید", docx_text)
        self.assertNotIn("تاریخ", docx_text)
        self.assertEqual(document.core_properties.comments, "")
        # Metadata may remain in the job/database layer, but no source or
        # processing identifier is serialized into the learner booklet.
        for forbidden in (b"lecture.mp3", b"GMS-42", b"speechmatics"):
            self.assertNotIn(forbidden, docx)

        pdf = build_notes_pdf(notes, meta=meta, duration_seconds=3600, session_number=7)
        try:
            import pymupdf
        except ImportError:
            pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
        else:
            with pymupdf.open(stream=pdf, filetype="pdf") as doc:
                pdf_text = "\n".join(page.get_text("text") or "" for page in doc)
        # Typst embeds logical Persian UTF-8 glyphs directly (and ReportLab
        # fallback emits shaped presentation forms). Normalize NFKC so both pass.
        import unicodedata
        norm_pdf = unicodedata.normalize("NFKC", pdf_text)
        self.assertIn("مرور نهایی", norm_pdf)
        self.assertIn("Metformin", norm_pdf)
        self.assertNotIn("گویندگان و زمان", norm_pdf)
        self.assertNotIn("جلسه", norm_pdf)
        for forbidden in (b"lecture.mp3", b"GMS-42", b"speechmatics"):
            self.assertNotIn(forbidden, pdf)


class TranscriptBoundaryTests(unittest.TestCase):
    def test_booklet_input_excludes_speaker_and_timing_but_transcript_keeps_them(self) -> None:
        document = TranscriptDocument(
            (
                Paragraph("محتوای آموزشی", start=30.0, end=32.0, speaker="0"),
                Paragraph("ادامهٔ محتوا", start=33.0, end=34.0, speaker="1"),
            ),
            speakers=2,
        )
        self.assertIn("گوینده", document.plain())
        self.assertNotIn("گوینده", document.llm_document())
        self.assertNotIn("00:30", document.llm_document())
        self.assertEqual(document.llm_document(), "محتوای آموزشی\n\nادامهٔ محتوا")
