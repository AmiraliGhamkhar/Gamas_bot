"""Tests for the polished RTL Word document and raw-text exporters."""

from __future__ import annotations

import io
import re
import unittest
import zipfile
from datetime import datetime

from gamas_bot.bidi import is_rtl_dominant
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
    resolve_design,
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
        self.assertIn("HbA1c | هموگلوبین گلیکوزیله", text)  # glossary is a table now
        self.assertIn("دارو | دوز روزانه", text)
        self.assertIn("Metformin | 500-2000 mg", text)
        # Internal backend metadata MUST NOT appear in the student document
        self.assertNotIn("موتور تبدیل گفتار", text)
        self.assertNotIn("speechmatics", text)
        self.assertNotIn("deepgram", text)
        self.assertNotIn("کد پیگیری", text)
        self.assertNotIn("GMS-000123", text)
        self.assertNotIn("backend", text.lower())
        self.assertNotIn("provider", text.lower())
        # Date is shown as educational context
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
        # Internal backend metadata MUST NOT appear in raw text
        self.assertNotIn("GMS-000123", content)
        self.assertNotIn("موتور تبدیل گفتار", content)
        self.assertNotIn("deepgram", content)
        self.assertNotIn("lecture.mp3", content)
        # Date is shown as educational context
        self.assertIn("2026-09-28 10:30", content)

    def test_empty_sections_are_skipped(self):
        content = build_raw_text_document(
            title="متن خام",
            sections=[("خالی", "   "), ("پر", "محتوا")],
            meta=META,
        )
        self.assertNotIn("خالی", content)
        self.assertIn("محتوا", content)


class MetadataLeakageTests(unittest.TestCase):
    """Backend/internal metadata must NOT appear in student-facing documents.

    The architectural boundary is:
        internal metadata (engine, provider, submission_id, reference, etc.)
            -> application internals (logs, database, debugging)
        visible educational content (title, sections, notes, summaries, etc.)
            -> DOCX renderer / raw text file

    Student-facing DOCX and raw text files must never expose:
    - STT provider/engine name (speechmatics, deepgram, etc.)
    - backend/provider name
    - submission/job IDs or tracking codes
    - internal filenames
    - debug information
    """

    def test_notes_docx_does_not_expose_engine(self):
        """The STT engine name must not appear in the notes document."""
        notes = parse_structured_notes(FULL_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        text = docx_text(data)
        # Engine names from any provider
        for engine in ("speechmatics", "deepgram", "gemini", "openai", "whisper"):
            self.assertNotIn(engine, text.lower(), f"engine '{engine}' leaked into notes docx")
        self.assertNotIn("موتور تبدیل گفتار", text)

    def test_notes_docx_does_not_expose_reference_id(self):
        """The submission tracking reference must not appear in the notes document."""
        notes = parse_structured_notes(FULL_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        text = docx_text(data)
        self.assertNotIn("GMS-000123", text)
        self.assertNotIn("کد پیگیری", text)

    def test_notes_docx_does_not_expose_source_filename(self):
        """The original source filename must not appear in the notes document."""
        notes = parse_structured_notes(FULL_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        text = docx_text(data)
        self.assertNotIn("lecture.mp3", text)
        self.assertNotIn("source", text.lower())

    def test_plain_docx_does_not_expose_backend_metadata(self):
        """Plain/fallback DOCX must also not expose internal metadata."""
        body = "# متن\n\nمضمون خام"
        data = build_plain_docx("جزوهٔ خام", body, meta=META)
        text = docx_text(data)
        self.assertNotIn("GMS-000123", text)
        self.assertNotIn("موتور تبدیل گفتار", text)
        self.assertNotIn("deepgram", text.lower())
        self.assertNotIn("lecture.mp3", text)

    def test_raw_text_does_not_expose_backend_metadata(self):
        """Raw text companion file must not expose internal metadata."""
        content = build_raw_text_document(
            title="متن خام",
            sections=[("بخش", "مضمون")],
            meta=META,
        )
        self.assertNotIn("GMS-000123", content)
        self.assertNotIn("موتور تبدیل گفتار", content)
        self.assertNotIn("deepgram", content.lower())
        self.assertNotIn("lecture.mp3", content)


class WordLayoutTests(unittest.TestCase):
    """The booklet must open in Word without a repair prompt and read well."""

    PPR_ORDER = (
        "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
        "widowControl", "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs",
        "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
        "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
        "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents",
        "suppressOverlap", "jc", "textDirection", "textAlignment",
        "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr",
    )
    TBLPR_ORDER = (
        "tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize",
        "tblStyleColBandSize", "tblW", "jc", "tblCellSpacing", "tblInd",
        "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook", "tblCaption",
        "tblDescription",
    )
    TRPR_ORDER = (
        "cnfStyle", "divId", "gridBefore", "gridAfter", "wBefore", "wAfter",
        "cantSplit", "trHeight", "tblHeader", "tblCellSpacing", "jc", "hidden",
    )
    RPR_ORDER = (
        "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps",
        "strike", "dstrike", "outline", "shadow", "emboss", "imprint",
        "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing",
        "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect",
        "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang",
    )

    def _assert_children_ordered(self, data: bytes) -> None:
        """OOXML is order-sensitive; Word asks to repair an out-of-sequence file."""
        import xml.etree.ElementTree as ET

        word_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        orders = {
            f"{word_ns}pPr": self.PPR_ORDER,
            f"{word_ns}rPr": self.RPR_ORDER,
            f"{word_ns}tblPr": self.TBLPR_ORDER,
            f"{word_ns}trPr": self.TRPR_ORDER,
        }
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            parts = [
                name for name in archive.namelist()
                if name.startswith("word/") and name.endswith(".xml")
            ]
            self.assertTrue(parts)
            for part in parts:
                root = ET.fromstring(archive.read(part))
                for tag, order in orders.items():
                    for element in root.iter(tag):
                        names = [
                            child.tag[len(word_ns):]
                            for child in element
                            if child.tag.startswith(word_ns)
                            and child.tag[len(word_ns):] in order
                        ]
                        positions = [order.index(name) for name in names]
                        self.assertEqual(
                            positions,
                            sorted(positions),
                            f"{part}: <{tag}> children out of order: {names}",
                        )

    def test_notes_document_is_schema_ordered(self):
        notes = parse_structured_notes(FULL_NOTES_JSON)
        self._assert_children_ordered(build_notes_docx(notes, meta=META))

    def test_plain_document_is_schema_ordered(self):
        body = "# متن\n\n- مورد\n\n| الف | ب |\n|---|---|\n| ۱ | ۲ |"
        self._assert_children_ordered(build_plain_docx("جزوهٔ خام", body, meta=META))

    def test_table_direction_precedes_table_width(self):
        # w:bidiVisual used to be appended after w:tblLook, which is the wrong
        # CT_TblPr order and makes Word treat the file as damaged.
        notes = parse_structured_notes(FULL_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
        tbl_pr = body[body.index("<w:tblPr>"):body.index("</w:tblPr>")]
        self.assertLess(
            tbl_pr.index("<w:bidiVisual/>"),
            tbl_pr.index("<w:tblW"),
            f"w:bidiVisual must precede w:tblW: {tbl_pr}",
        )

    def test_table_header_repeats_on_every_page(self):
        notes = parse_structured_notes(FULL_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
        first_row = body[body.index("<w:tr>"):body.index("</w:tr>")]
        self.assertIn("<w:tblHeader", first_row)
        # One repeating header per table: the content table plus the glossary
        # table that every titled note now renders.
        self.assertEqual(body.count("<w:tblHeader"), 2)

    def test_headings_stay_with_their_first_body_line(self):
        notes = parse_structured_notes(FULL_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("<w:keepNext/>", body)
        self.assertIn("<w:keepLines/>", body)
        # keepNext must precede keepLines (CT_PPrBase order).
        self.assertLess(body.index("<w:keepNext/>"), body.index("<w:keepLines/>"))


class FilenameTruncationTests(unittest.TestCase):
    def test_truncation_never_leaves_a_trailing_space(self):
        # A title cut mid-word exposed a trailing space, producing filenames
        # like "جزوه - عنوان  - GMS-000001.docx" with a doubled separator.
        title = "عنوان " * 30
        part = sanitize_filename_part(title)
        self.assertEqual(part, part.strip())
        self.assertNotIn(part[-1], " .")

    def test_long_title_filename_has_no_double_separator(self):
        title = "الف " * 40
        filename = plain_docx_filename(title, "GMS-000001")
        self.assertNotIn("  ", filename)
        self.assertTrue(filename.endswith(" - GMS-000001.docx"))


class StyleLayerDirectionTests(unittest.TestCase):
    """Direction must live in the styles, not only on the rendered paragraphs.

    Regression: every paragraph carried ``w:bidi`` and every Persian run
    ``w:rtl``, yet style-generated content and newly typed or pasted paragraphs
    still inherited a left-to-right context. Word derives direction from style
    definitions and ``w:docDefaults``, so the body was RTL while its defaults
    and surrounding content were not.
    """

    def _parts(self, data: bytes) -> dict[str, str]:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {
                name: archive.read(name).decode("utf-8")
                for name in archive.namelist()
                if name.endswith(".xml")
            }

    def _document(self) -> dict[str, str]:
        notes = parse_structured_notes(FULL_NOTES_JSON)
        return self._parts(build_notes_docx(notes, meta=META))

    def test_document_defaults_declare_an_rtl_persian_context(self):
        styles = self._document()["word/styles.xml"]
        doc_defaults = re.search(r"<w:docDefaults>.*?</w:docDefaults>", styles, re.S)
        self.assertIsNotNone(doc_defaults, "the template must keep its docDefaults")
        defaults = doc_defaults.group(0)
        # The paragraph default is what a paragraph no style covers inherits.
        p_pr_default = re.search(r"<w:pPrDefault>.*?</w:pPrDefault>", defaults, re.S)
        self.assertIsNotNone(p_pr_default)
        self.assertIn("<w:bidi/>", p_pr_default.group(0))
        # The run default: complex-script face, RTL runs, Persian locale.
        r_pr_default = re.search(r"<w:rPrDefault>.*?</w:rPrDefault>", defaults, re.S)
        self.assertIsNotNone(r_pr_default)
        self.assertIn("<w:rtl/>", r_pr_default.group(0))
        self.assertIn('w:bidi="fa-IR"', r_pr_default.group(0))
        # The template ships ``ar-SA``; Arabic locale shapes Persian wrongly.
        self.assertNotIn('w:bidi="ar-SA"', defaults)

    def test_every_configured_style_is_marked_rtl(self):
        styles = self._document()["word/styles.xml"]
        for style_id in (
            "Normal", "Heading1", "Heading2", "Heading3", "Title", "Subtitle",
            "Quote", "Definition", "Example", "Note", "Warning", "Tabletext",
            "TOCHeading", "Header", "Footer",
        ):
            match = re.search(
                r'<w:style [^>]*w:styleId="%s".*?</w:style>' % style_id, styles, re.S
            )
            self.assertIsNotNone(match, f"missing style {style_id}")
            style = match.group(0)
            self.assertIn("<w:bidi/>", style, f"{style_id} paragraph direction")
            self.assertIn("<w:rtl/>", style, f"{style_id} run direction")

    def test_static_table_of_contents_entry_styles_are_rtl(self):
        """Static TOC paragraphs need explicit RTL styles as well as RTL runs."""
        styles = self._document()["word/styles.xml"]
        for style_id in ("TOC1", "TOC2", "TOC3"):
            match = re.search(
                r'<w:style [^>]*w:styleId="%s".*?</w:style>' % style_id, styles, re.S
            )
            self.assertIsNotNone(match, f"missing {style_id}")
            style = match.group(0)
            self.assertIn("<w:bidi/>", style)
            self.assertIn("<w:rtl/>", style)
            # Right-aligned with a dot leader, so the page number lands on the
            # left of the Persian entry instead of hanging off the right edge.
            self.assertIn('<w:jc w:val="right"/>', style)
            self.assertIn('w:leader="dot"', style)

    def test_toc_entry_styles_survive_when_the_toc_is_disabled(self):
        # They are document-level definitions: Word still needs them if the
        # reader inserts their own table of contents later.
        notes = parse_structured_notes(FULL_NOTES_JSON)
        data = build_notes_docx(notes, meta=META, design=resolve_design({"toc_enabled": False}))
        self.assertIn('w:styleId="TOC1"', self._parts(data)["word/styles.xml"])

    def test_paragraph_marks_are_rtl_so_the_document_types_rtl(self):
        """The paragraph mark decides the caret and the empty-paragraph side."""
        parts = self._document()
        body = parts["word/document.xml"]
        marks = re.findall(r"<w:pPr>(?:(?!</w:pPr>).)*?</w:pPr>", body, re.S)
        rtl_marks = [m for m in marks if "<w:rPr><w:rtl/></w:rPr>" in m]
        self.assertTrue(rtl_marks, "no paragraph mark carries w:rtl")
        # The empty spacer/rule paragraphs have no runs at all, so their mark
        # is the only thing that can make them RTL.
        empty = [
            m for m in re.findall(r"<w:p\b(?:(?!</w:p>).)*</w:p>", body, re.S)
            if not re.search(r"<w:t[ >]", m)
        ]
        self.assertTrue(empty, "expected the cover's empty spacer paragraphs")
        for paragraph in empty:
            self.assertIn("<w:rtl/>", paragraph)
        # Header and footer are separate parts and need the same treatment.
        for name, xml in parts.items():
            if name.startswith(("word/header", "word/footer")):
                self.assertIn("<w:bidi", xml)

    def test_section_marks_the_gutter_as_right_to_left(self):
        body = self._document()["word/document.xml"]
        self.assertIn("<w:rtlGutter", body)

    def test_latin_only_lines_stay_ltr_after_the_style_change(self):
        """Making the styles RTL must not force Persian onto Latin-only lines."""
        self.assertFalse(is_rtl_dominant("F = ma"))
        self.assertTrue(is_rtl_dominant("تشخیص بر پایهٔ HbA1c است."))


class RightAlignmentTests(unittest.TestCase):
    """Every paragraph is right-aligned -- never justified, never left.

    Alignment is a property of its own, separate from direction: a paragraph
    marked ``w:bidi`` with no ``w:jc`` aligns to its logical start edge, and
    the body text of the previous build was justified
    (``w:jc w:val="both"``), which is not the right-aligned booklet this
    project promises. The contract is pinned at all three layers Word reads:
    ``w:docDefaults``, the style definitions, and the paragraphs the build
    writes.
    """

    #: Style ids that must declare a right alignment of their own.
    RIGHT_ALIGNED_STYLES = (
        "Normal", "Heading1", "Heading2", "Heading3", "Title", "Subtitle",
        "Quote", "Definition", "Example", "Note", "Warning", "Tabletext",
        "TOCHeading", "TOC1", "TOC2", "TOC3",
    )

    def _parts(self, data: bytes) -> dict[str, str]:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {
                name: archive.read(name).decode("utf-8")
                for name in archive.namelist()
                if name.endswith(".xml")
            }

    def _document(self) -> dict[str, str]:
        notes = parse_structured_notes(FULL_NOTES_JSON)
        return self._parts(build_notes_docx(notes, meta=META))

    def test_document_defaults_align_right(self):
        styles = self._document()["word/styles.xml"]
        p_pr_default = re.search(r"<w:pPrDefault>.*?</w:pPrDefault>", styles, re.S)
        self.assertIsNotNone(p_pr_default, "the template must keep its pPrDefault")
        self.assertIn('<w:jc w:val="right"/>', p_pr_default.group(0))

    def test_every_configured_style_aligns_right(self):
        styles = self._document()["word/styles.xml"]
        for style_id in self.RIGHT_ALIGNED_STYLES:
            match = re.search(
                r'<w:style [^>]*w:styleId="%s".*?</w:style>' % style_id, styles, re.S
            )
            self.assertIsNotNone(match, f"missing style {style_id}")
            self.assertIn('<w:jc w:val="right"/>', match.group(0), style_id)

    def test_no_paragraph_is_justified_or_left_aligned(self):
        body = self._document()["word/document.xml"]
        self.assertNotIn('w:val="both"', body, "body text must not be justified")
        self.assertNotIn('w:val="left"', body, "no paragraph may be left-aligned")

    def test_every_written_paragraph_aligns_right_or_center(self):
        body = self._document()["word/document.xml"]
        for xml in re.findall(r"<w:p\b.*?</w:p>", body, re.S):
            p_pr = re.search(r"<w:pPr>(.*?)</w:pPr>", xml, re.S)
            jc = re.search(r'<w:jc w:val="([^"]+)"', p_pr.group(1) if p_pr else "")
            # A paragraph with no explicit ``w:jc`` inherits the document
            # default, which is right.
            value = jc.group(1) if jc else "right"
            self.assertIn(value, ("right", "center", "end"), xml)

    def test_latin_only_line_is_right_aligned_and_keeps_ltr_direction(self):
        body = self._parts(
            build_plain_docx("جزوه", "## بخش\n\nF = ma", meta=META)
        )["word/document.xml"]
        targets = [
            xml
            for xml in re.findall(r"<w:p\b.*?</w:p>", body, re.S)
            if "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S)) == "F = ma"
        ]
        self.assertTrue(targets, "the Latin-only line must be present")
        paragraph = targets[0]
        # Alignment is right even though the direction stays LTR, so a formula
        # lines up with the Persian text around it.
        self.assertIn('<w:jc w:val="right"/>', paragraph)
        self.assertNotIn("<w:bidi", paragraph)



if __name__ == "__main__":
    unittest.main()
