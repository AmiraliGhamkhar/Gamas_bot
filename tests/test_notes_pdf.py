"""Smoke/regression coverage for the native StructuredNotes PDF booklet."""

from __future__ import annotations

from datetime import datetime
import io
import unittest

from pypdf import PdfReader

from gamas_bot.docx_export import DocumentMeta
from gamas_bot.notes import (
    GlossaryEntry,
    NoteCallout,
    NoteDefinition,
    NoteSection,
    NoteTable,
    StructuredNotes,
)
from gamas_bot.notes_pdf import build_notes_pdf, notes_pdf_filename


class NotesPdfTests(unittest.TestCase):
    def _notes(self) -> StructuredNotes:
        return StructuredNotes(
            title="تعرفه‌گذاری و رسیدگی",
            summary="خلاصهٔ ساختارمند جلسه.",
            sections=(
                NoteSection(
                    heading="مبانی تعرفه‌گذاری",
                    paragraphs=("مبنای محاسبه باید از متن تدریس‌شده استخراج شود.",),
                    definitions=(NoteDefinition("تعرفه", "مبلغ یا قاعدهٔ محاسبه."),),
                    examples=("مثال: یک مورد آموزشی.",),
                    steps=("داده را بررسی کنید.", "نتیجه را ثبت کنید."),
                    key_points=("عددهای اصلاح‌شده را حفظ کنید.",),
                    table=NoteTable(
                        headers=["عنوان", "مقدار"], rows=[["خدمت", "۳۰۰"]]
                    ),
                    callouts=(NoteCallout("هشدار", "اصلاح مدرس باید ثبت شود."),),
                ),
                NoteSection(
                    heading="رسیدگی به اسناد",
                    bullets=("کنترل اولیه", "ثبت نتیجه"),
                ),
            ),
            glossary=(GlossaryEntry("تعرفه", "قاعدهٔ قیمت‌گذاری", "Tariff"),),
        )

    def test_pdf_is_parseable_and_byte_stable_for_fixed_inputs(self):
        notes = self._notes()
        meta = DocumentMeta(
            reference="GMS-42",
            source_name="lecture.mp3",
            created_at=datetime(2026, 11, 10, 12, 0, 0),
        )
        first = build_notes_pdf(
            notes, meta=meta, duration_seconds=10_800, session_number=4
        )
        second = build_notes_pdf(
            notes, meta=meta, duration_seconds=10_800, session_number=4
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(b"%PDF-"))
        self.assertGreater(len(first), 20_000)

        reader = PdfReader(io.BytesIO(first))
        # Cover plus structured body/final material; all generated pages carry
        # a content stream rather than an accidental blank trailing page.
        self.assertGreaterEqual(len(reader.pages), 2)
        for page in reader.pages:
            contents = page.get_contents()
            self.assertIsNotNone(contents)
            self.assertTrue(contents.get_data())

    def test_pdf_filename_is_a_pdf_and_uses_the_submission_reference(self):
        name = notes_pdf_filename(self._notes(), "GMS-42")
        self.assertTrue(name.endswith(".pdf"))
        self.assertIn("GMS-42", name)


if __name__ == "__main__":
    unittest.main()
