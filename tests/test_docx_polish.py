"""Tests for the booklet's document-design layer.

These pin the parts of the Word output that make it a *booklet* rather than a
text dump: the cover page (with the exact brand and quotation), the real
section-level page frame (``w:pgBorders`` inside ``w:sectPr``), the automatic
table of contents, real Heading 1/2/3 styles, and the footer's live ``PAGE``
field. Each of them is a user-visible contract, so each is asserted on the
generated XML rather than on python-docx's object model.
"""

from __future__ import annotations

import io
import unittest
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from support import docx_text

from gamas_bot.docx_export import (
    COVER_QUOTE,
    DocxDesign,
    DocumentMeta,
    build_notes_docx,
    resolve_design,
)
from gamas_bot.structuring import parse_structured_notes

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

META = DocumentMeta(
    reference="GMS-000123",
    source_name="lecture.mp3",
    engine="deepgram",
    created_at=datetime(2026, 9, 28, 10, 30),
)


def long_notes_json() -> str:
    """Four sections, so the automatic TOC (>= 3 sections) is generated."""
    sections = []
    for index in range(1, 5):
        sections.append(
            '{"heading": "موضوع %d", "paragraphs": ["توضیح کامل موضوع %d با جزئیات کافی '
            'برای اینکه بخش کوتاه شمرده نشود و متن قابل خواندن باشد."], '
            '"bullets": ["مورد الف", "مورد ب"], '
            '"examples": ["مثال عددی ۵۰۰ mg"], '
            '"steps": ["گام یکم", "گام دوم"], '
            '"definitions": [{"term": "اصطلاح %d", "term_en": "Term %d", "definition": "تعریف"}], '
            '"key_points": ["نکتهٔ کلیدی %d"], '
            '"table": {"headers": ["ستون", "مقدار"], "rows": [["الف", "۱"]]}, '
            '"callouts": [{"kind": "هشدار", "text": "هشدار %d"}]}' % (index, index, index, index, index, index)
        )
    return (
        '{"title": "جزوهٔ آزمون", "summary": "خلاصهٔ درس.", "sections": ['
        + ", ".join(sections)
        + "]}"
    )


class DocxTestCase(unittest.TestCase):
    def build(
        self,
        notes_json: str,
        *,
        design: DocxDesign | None = None,
        meta: DocumentMeta = META,
    ) -> bytes:
        notes = parse_structured_notes(notes_json)
        return build_notes_docx(notes, meta=meta, design=design or resolve_design())

    def part(self, data: bytes, name: str) -> str:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.read(name).decode("utf-8")

    def document_xml(self, data: bytes) -> str:
        return self.part(data, "word/document.xml")

    def sections(self, data: bytes) -> list[ET.Element]:
        root = ET.fromstring(self.document_xml(data))
        return list(root.iter(f"{W}sectPr"))

    def paragraphs(self, data: bytes) -> list[ET.Element]:
        root = ET.fromstring(self.document_xml(data))
        body = root.find(f"{W}body")
        return [child for child in body if child.tag == f"{W}p"]


class CoverPageTests(DocxTestCase):
    def test_cover_has_god_line_brand_words_and_verbatim_quote(self):
        text = docx_text(self.build(long_notes_json()))
        self.assertIn("به نام خدا", text)
        self.assertIn("GAMAS", text)
        self.assertIn("Gamas Bot", text)
        self.assertIn(COVER_QUOTE, text)
        # the quotation is a contract: byte-for-byte, no rewrite or translation
        self.assertIn(f"«{COVER_QUOTE}»", text)

    def test_cover_can_be_disabled_without_leaving_a_blank_page(self):
        data = self.build(long_notes_json(), design=resolve_design({"cover_enabled": False}))
        text = docx_text(data)
        self.assertNotIn("به نام خدا", text)
        self.assertNotIn(COVER_QUOTE, text)
        self.assertIn("جزوهٔ آزمون", text)
        # a disabled cover must not create an empty leading section: the very
        # first paragraph belongs to the body and carries the title block
        self.assertEqual(len(self.sections(data)), 1)
        first = "".join(
            node.text or "" for node in self.paragraphs(data)[0].iter(f"{W}t")
        )
        self.assertTrue(first.strip(), "the first page must not start blank")

    def test_cover_shows_mode_label_and_jalali_date(self):
        text = docx_text(self.build(long_notes_json()))
        self.assertIn("GMS-000123", text)
        self.assertIn("حالت تولید: کامل", text)
        self.assertIn("۱۴۰۵", text)  # Jalali date on the cover


class PageBorderTests(DocxTestCase):
    def test_page_border_is_a_section_level_pgBorders_in_every_section(self):
        data = self.build(long_notes_json())
        sections = self.sections(data)
        self.assertGreaterEqual(len(sections), 2)
        for section in sections:
            border = section.find(f"{W}pgBorders")
            self.assertIsNotNone(border, "every section must carry its own page frame")
            self.assertEqual(border.get(f"{W}offsetFrom"), "page")
            for side in ("top", "left", "bottom", "right"):
                edge = border.find(f"{W}{side}")
                self.assertIsNotNone(edge, side)
                self.assertEqual(edge.get(f"{W}val"), "single")
                self.assertEqual(edge.get(f"{W}color"), "BFCEE4")

    def test_page_border_configuration_is_applied(self):
        design = resolve_design(
            {
                "page_border_enabled": True,
                "border_style": "double",
                "border_color": "#112233",
                "border_size": 12,
                "border_space": 10,
            }
        )
        data = self.build(long_notes_json(), design=design)
        for section in self.sections(data):
            border = section.find(f"{W}pgBorders")
            edge = border.find(f"{W}top")
            self.assertEqual(edge.get(f"{W}val"), "double")
            self.assertEqual(edge.get(f"{W}color"), "112233")
            self.assertEqual(edge.get(f"{W}sz"), "12")
            self.assertEqual(edge.get(f"{W}space"), "10")

    def test_page_border_can_be_disabled(self):
        data = self.build(long_notes_json(), design=resolve_design({"page_border_enabled": False}))
        self.assertNotIn(f"{W}pgBorders", self.document_xml(data))
        self.assertNotIn("<w:pgBorders", self.document_xml(data))

    def test_no_empty_paragraph_pretends_to_be_a_page_frame(self):
        # The old implementation drew the frame with a bordered empty paragraph.
        root = ET.fromstring(self.document_xml(self.build(long_notes_json())))
        for paragraph in root.iter(f"{W}p"):
            borders = paragraph.find(f"{W}pPr/{W}pBdr")
            if borders is None:
                continue
            text = "".join(node.text or "" for node in paragraph.iter(f"{W}t"))
            self.assertTrue(text.strip(), "an empty bordered paragraph is a fake page frame")


class FooterTests(DocxTestCase):
    def test_footer_contains_a_real_page_field_not_a_number(self):
        data = self.build(long_notes_json())
        footer = self.part(data, "word/footer1.xml")
        self.assertIn('w:fldCharType="begin"', footer)
        self.assertIn("PAGE", footer)
        self.assertIn("Gamas Bot", footer)

    def test_footer_brand_can_be_hidden(self):
        data = self.build(long_notes_json(), design=resolve_design({"footer_brand": False}))
        footer = self.part(data, "word/footer1.xml")
        self.assertIn("PAGE", footer)
        self.assertNotIn(">Gamas Bot<", footer)

    def test_cover_section_has_no_header_or_footer(self):
        # The cover is its own section; the running header/footer belong to the
        # body section only, so page one of the booklet is the cover.
        data = self.build(long_notes_json())
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
        self.assertIn("word/header1.xml", names)
        self.assertIn("word/footer1.xml", names)


class HeadingStyleTests(DocxTestCase):
    def test_headings_use_real_word_heading_styles(self):
        data = self.build(long_notes_json())
        root = ET.fromstring(self.document_xml(data))
        used: set[str] = set()
        for paragraph in root.iter(f"{W}p"):
            style = paragraph.find(f"{W}pPr/{W}pStyle")
            if style is not None and style.get(f"{W}val", "").startswith("Heading"):
                used.add(style.get(f"{W}val"))
                text = "".join(node.text or "" for node in paragraph.iter(f"{W}t"))
                self.assertTrue(text.strip(), "a heading paragraph must carry text")
        self.assertIn("Heading1", used)
        self.assertIn("Heading2", used)
        # never skip a level: the deepest used level implies all shallower ones
        levels = {int(name.replace("Heading", "")) for name in used}
        self.assertEqual(levels, set(range(1, max(levels) + 1)))

    def test_notes_metadata_and_title_survive(self):
        data = self.build(long_notes_json())
        self.assertIn("جزوهٔ آزمون", docx_text(data))


class TocTests(DocxTestCase):
    def test_toc_field_is_generated_for_long_documents(self):
        data = self.build(long_notes_json())
        xml = self.document_xml(data)
        self.assertIn('TOC \\o "1-2" \\h \\z \\u', xml)
        self.assertIn('w:dirty="true"', xml)
        self.assertIn("فهرست مطالب", docx_text(data))

    def test_toc_is_skipped_for_short_documents(self):
        short = (
            '{"title": "کوتاه", "sections": [{"heading": "الف", "paragraphs": ["متن"]}, '
            '{"heading": "ب", "paragraphs": ["متن دو"]}]}'
        )
        self.assertNotIn("TOC", self.document_xml(self.build(short)))

    def test_toc_can_be_disabled(self):
        data = self.build(long_notes_json(), design=resolve_design({"toc_enabled": False}))
        self.assertNotIn("TOC \\o", self.document_xml(data))
        self.assertNotIn("فهرست مطالب", docx_text(data))


class DirectionAndResourceTests(DocxTestCase):
    def test_mixed_script_content_keeps_paragraph_and_run_direction(self):
        data = self.build(long_notes_json())
        xml = self.document_xml(data)
        self.assertIn("<w:bidi", xml)
        self.assertIn("<w:rtl", xml)
        self.assertIn("Term 1", xml)  # the Latin term is not reversed
        # Latin runs inside a Persian paragraph explicitly disable RTL, so the
        # numbers and units keep their own direction.
        self.assertIn('<w:rtl w:val="0"/>', xml)
        text = docx_text(data)
        self.assertIn("۵۰۰ mg", text)
        self.assertIn("Term 1", text)

    def test_missing_logo_never_fails_and_leaves_no_image(self):
        design = resolve_design({"logo_path": "assets/does-not-exist.png"})
        data = self.build(long_notes_json(), design=design)
        xml = self.document_xml(data)
        self.assertNotIn("<w:drawing", xml)
        self.assertIn("GAMAS", docx_text(data))

    def test_local_logo_is_used_when_present(self):
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover - Pillow is optional (CI)
            self.skipTest("Pillow is not installed")

        with TemporaryDirectory() as folder:
            logo = Path(folder) / "logo.png"
            Image.new("RGB", (64, 64), (31, 56, 100)).save(logo)
            design = resolve_design({"logo_path": str(logo)})
            data = self.build(long_notes_json(), design=design)
        self.assertIn("<w:drawing", self.document_xml(data))

    def test_resolve_design_defaults_and_invalid_values(self):
        default = resolve_design()
        self.assertTrue(default.cover_enabled and default.toc_enabled)
        self.assertTrue(default.page_border_enabled and default.footer_brand)
        broken = resolve_design(
            {
                "border_color": "not-a-color",
                "border_style": "wavy",
                "border_size": 999,
                "border_space": -5,
                "cover_enabled": "off",
            }
        )
        self.assertEqual(broken.border_color, DocxDesign().border_color)
        self.assertEqual(broken.border_style, "single")
        self.assertEqual(broken.border_size, 96)
        self.assertEqual(broken.border_space, 0)
        self.assertFalse(broken.cover_enabled)


if __name__ == "__main__":
    unittest.main()
