"""Tests for the polished RTL Word document and raw-text exporters."""

from __future__ import annotations

import io
import unittest
import zipfile
from datetime import datetime

from gamas_bot.docx_export import (
    DocumentMeta,
    build_notes_docx,
    build_plain_docx,
    build_raw_text_document,
    gregorian_to_jalali,
    jalali_date,
    notes_docx_filename,
    plain_docx_filename,
    raw_text_filename,
    sanitize_filename_part,
)
from gamas_bot.structuring import parse_structured_notes

from support import docx_text, sample_notes_json

META = DocumentMeta(
    reference="GMS-000123",
    source_name="lecture.mp3",
    engine="deepgram",
    created_at=datetime(2026, 9, 28, 10, 30),
)

FULL_NOTES_JSON = (
    '{"title": "فارماکولوژی دیابت", "summary": "مرور داروهای کاهندهٔ قند خون.", '
    '"sections": [{'
    '"heading": "Metformin", '
    '"paragraphs": ["خط اول درمان دیابت نوع ۲ است."], '
    '"bullets": ["شروع با دوز کم"], '
    '"key_points": ["در نارسایی کلیوی منع مصرف دارد"], '
    '"table": {"headers": ["دارو", "دوز روزانه"], "rows": [["Metformin", "500-2000 mg"]]}, '
    '"callouts": [{"kind": "هشدار", "text": "در AKI قطع شود"}, '
    '{"kind": "یادآوری", "text": "پایش HbA1c هر سه ماه"}]'
    "}], "
    '"key_points": ["HbA1c هدف زیر ۷ درصد"], '
    '"glossary": [{"term": "HbA1c", "definition": "هموگلوبین گلیکوزیله"}]}'
)


class JalaliDateTests(unittest.TestCase):
    def test_known_dates_convert_correctly(self):
        self.assertEqual(gregorian_to_jalali(2026, 9, 28), (1405, 7, 6))
        self.assertEqual(gregorian_to_jalali(2026, 1, 1), (1404, 10, 11))
        self.assertEqual(gregorian_to_jalali(2024, 3, 20), (1403, 1, 1))
        self.assertIn("مهر", jalali_date(datetime(2026, 9, 28)))
        self.assertIn("۱۴۰۵", jalali_date(datetime(2026, 9, 28)))


class FilenameTests(unittest.TestCase):
    def test_filenames_are_safe_and_reference_tagged(self):
        notes = parse_structured_notes(FULL_NOTES_JSON)
        self.assertEqual(
            notes_docx_filename(notes, "GMS-000123"),
            "جزوه - فارماکولوژی دیابت - GMS-000123.docx",
        )
        self.assertEqual(
            plain_docx_filename("جزوهٔ کلاس", "GMS-000001"),
            "جزوه - جزوهٔ کلاس - GMS-000001.docx",
        )
        self.assertEqual(raw_text_filename("GMS-000123"), "متن خام - GMS-000123.txt")
        self.assertEqual(sanitize_filename_part('بد/مجاز: "نام"*?'), "بد مجاز نام")
        self.assertEqual(sanitize_filename_part("   "), "")


class NotesDocxTests(unittest.TestCase):
    def test_document_contains_all_structured_content(self):
        notes = parse_structured_notes(FULL_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        text = docx_text(data)
        self.assertIn("فارماکولوژی دیابت", text)
        self.assertIn("مرور داروهای کاهندهٔ قند خون.", text)
        self.assertIn("۱. Metformin", text)
        self.assertIn("خط اول درمان دیابت نوع ۲ است.", text)
        self.assertIn("• شروع با دوز کم", text)
        self.assertIn("هشدار: در AKI قطع شود", text)
        self.assertIn("یادآوری: پایش HbA1c هر سه ماه", text)
        self.assertIn("HbA1c: هموگلوبین گلیکوزیله", text)
        self.assertIn("دارو | دوز روزانه", text)
        self.assertIn("Metformin | 500-2000 mg", text)
        self.assertIn("کد پیگیری: GMS-000123", text)
        self.assertIn("۶ مهر ۱۴۰۵", text)

    def test_document_is_genuinely_rtl(self):
        notes = parse_structured_notes(FULL_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        footer = ""
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
            footer_names = [
                name for name in archive.namelist() if name.startswith("word/footer")
            ]
            self.assertTrue(footer_names)  # page-number footer part exists
            for name in footer_names:
                footer += archive.read(name).decode("utf-8")
        self.assertIn('<w:bidi w:val="1"/>', body)  # RTL paragraphs
        self.assertIn("w:rtl", body)  # RTL runs
        self.assertIn("<w:bidiVisual/>", body)  # RTL table column order
        self.assertIn('w:cs="Tahoma"', body)  # complex-script font honoured
        self.assertIn("w:shd", body)  # shaded summary/callout boxes
        self.assertIn("w:szCs", body)  # complex-script font size honoured
        self.assertIn("PAGE", footer)  # page-number field code

    def test_custom_font_is_applied(self):
        notes = parse_structured_notes(sample_notes_json())
        data = build_notes_docx(notes, font="B Nazanin", meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
        self.assertIn('w:cs="B Nazanin"', body)

    def test_document_metadata_records_the_job(self):
        notes = parse_structured_notes(FULL_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        import docx as docx_library

        document = docx_library.Document(io.BytesIO(data))
        self.assertEqual(document.core_properties.title, "فارماکولوژی دیابت")
        self.assertEqual(document.core_properties.author, "Gamas Bot")


class PlainDocxTests(unittest.TestCase):
    def test_markdown_fallback_material_is_rendered(self):
        body = "# متن پیاده‌سازی‌شده\n\nسلام این متن خام است.\n\n- مورد اول\n- مورد دوم"
        data = build_plain_docx("جزوهٔ خام", body, meta=META)
        text = docx_text(data)
        self.assertIn("جزوهٔ خام", text)
        self.assertIn("متن پیاده‌سازی‌شده", text)
        self.assertIn("سلام این متن خام است.", text)
        self.assertIn("• مورد اول", text)
        self.assertIn("• مورد دوم", text)


class RawTextDocumentTests(unittest.TestCase):
    def test_raw_sections_are_included_verbatim(self):
        content = build_raw_text_document(
            title="متن خام — جزوهٔ کلاس",
            sections=[
                ("متن پیاده‌سازی‌شده", "این متن خام است."),
                ("متن اسلایدها", "### اسلاید ۱"),
            ],
            meta=META,
        )
        self.assertIn("متن خام — جزوهٔ کلاس", content)
        self.assertIn("متن پیاده‌سازی‌شده", content)
        self.assertIn("این متن خام است.", content)
        self.assertIn("### اسلاید ۱", content)
        self.assertIn("GMS-000123", content)
        self.assertIn("2026-09-28 10:30", content)

    def test_empty_sections_are_skipped(self):
        content = build_raw_text_document(
            title="متن خام",
            sections=[("خالی", "   "), ("پر", "محتوا")],
            meta=META,
        )
        self.assertNotIn("خالی", content)
        self.assertIn("محتوا", content)


if __name__ == "__main__":
    unittest.main()
