"""Tests for the polished RTL Word document exporter."""

from __future__ import annotations

import io
import json
import unittest
import zipfile
from datetime import datetime

from lxml import etree

from gamas_bot.docx_export import (
    ACCENT_HEX,
    ACCENT_SOFT_HEX,
    CALLOUT_BORDERS,
    CALLOUT_SHADES,
    DocumentMeta,
    FORMULA_SHADE,
    HEADING_COLORS,
    KEYPOINT_BORDER,
    KEYPOINT_SHADE,
    SUMMARY_BORDER,
    SUMMARY_SHADE,
    TABLE_BORDER_COLOR,
    ZEBRA_SHADE,
    build_notes_docx,
    build_plain_docx,
    gregorian_to_jalali,
    jalali_date,
    notes_docx_filename,
    plain_docx_filename,
    raw_text_filename,
    resolve_design,
    sanitize_filename_part,
    xml_safe,
)
from gamas_bot.notes import NoteSection, NoteTable, PracticalTip, StructuredNotes
from tests.support import docx_text, sample_notes_json

#: Shorthand for the wordprocessingml namespace used by the raw-XML assertions.
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def document_root(data: bytes):
    """Parsed ``word/document.xml`` of a built booklet."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


def paragraph_text(paragraph) -> str:
    return "".join(node.text or "" for node in paragraph.iter(f"{W}t"))


def has_persian(text: str) -> bool:
    return any("\u0600" <= char <= "\u06ff" for char in text)

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


def full_notes() -> StructuredNotes:
    return StructuredNotes.from_payload(json.loads(FULL_NOTES_JSON))


class JalaliDateTests(unittest.TestCase):
    def test_known_dates_convert_correctly(self):
        self.assertEqual(gregorian_to_jalali(2026, 9, 28), (1405, 7, 6))
        self.assertEqual(gregorian_to_jalali(2026, 1, 1), (1404, 10, 11))
        self.assertEqual(gregorian_to_jalali(2024, 3, 20), (1403, 1, 1))
        self.assertIn("مهر", jalali_date(datetime(2026, 9, 28)))
        self.assertIn("۱۴۰۵", jalali_date(datetime(2026, 9, 28)))


class FilenameTests(unittest.TestCase):
    def test_xml_safe_preserves_nbsp_and_private_use_characters(self):
        self.assertEqual(xml_safe("500\u00a0mg"), "500\u00a0mg")
        self.assertEqual(xml_safe("\ue000\u00a0X"), "\ue000\u00a0X")

    def test_filenames_are_safe_and_reference_tagged(self):
        notes = full_notes()
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
        data = build_notes_docx(full_notes(), meta=META)
        text = docx_text(data)
        self.assertIn("فارماکولوژی دیابت", text)
        self.assertIn("مرور داروهای کاهندهٔ قند خون.", text)
        self.assertIn("۱. Metformin", text)
        self.assertIn("خط اول درمان دیابت نوع ۲ است.", text)
        self.assertIn("• شروع با دوز کم", text)
        self.assertIn("هشدار: در AKI قطع شود", text)
        self.assertIn("یادآوری: پایش HbA1c هر سه ماه", text)
        self.assertIn("HbA1c | هموگلوبین گلیکوزیله", text)  # glossary is a table
        self.assertIn("دارو | دوز روزانه", text)
        self.assertIn("Metformin | 500-2000\u00a0mg", text)
        # Internal backend metadata MUST NOT appear in the student document
        self.assertNotIn("موتور تبدیل گفتار", text)
        self.assertNotIn("speechmatics", text)
        self.assertNotIn("deepgram", text)
        self.assertNotIn("کد پیگیری", text)
        self.assertNotIn("GMS-000123", text)
        self.assertNotIn("backend", text.lower())
        self.assertNotIn("provider", text.lower())

    def test_document_is_genuinely_rtl(self):
        data = build_notes_docx(full_notes(), meta=META)
        footer = ""
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
            footer_names = [
                name for name in archive.namelist() if name.startswith("word/footer")
            ]
            self.assertTrue(footer_names)  # page-number footer part exists
            for name in footer_names:
                footer += archive.read(name).decode("utf-8")
        self.assertIn('<w:bidi w:val="1"/>', body)   # RTL paragraphs
        self.assertIn("w:rtl", body)                 # RTL runs
        self.assertIn("<w:bidiVisual/>", body)       # RTL table column order
        self.assertIn('w:cs="Vazirmatn"', body)      # complex-script font honoured
        self.assertIn("w:shd", body)                 # shaded summary/callout boxes
        self.assertIn("w:szCs", body)                # complex-script font size honoured
        self.assertIn("PAGE", footer)                # page-number field code

    def test_mixed_persian_and_english_render_in_one_paragraph(self):
        data = build_notes_docx(full_notes(), meta=META)
        import docx as docx_library

        document = docx_library.Document(io.BytesIO(data))
        mixed = [
            p for p in document.paragraphs
            if "Metformin" in p.text and any("\u0600" <= ch <= "\u06ff" for ch in p.text)
        ]
        self.assertTrue(mixed)  # bidi splitting produced directional runs
        for paragraph in mixed:
            directions = {
                run.font.rtl for run in paragraph.runs if run.text.strip()
            }
            # Persian runs are RTL, English runs are explicitly LTR.
            self.assertIn(True, directions)
            self.assertIn(False, directions)

    def test_mixed_script_heading_keeps_logical_order_and_parentheses(self):
        from gamas_bot.docx_export import build_plain_docx

        data = build_plain_docx(
            "جزوهٔ ترکیبی",
            "# شبکهٔ عصبی (Artificial Neural Network) در تشخیص تصویر",
            meta=META,
            heading_offset=0,
        )
        import docx as docx_library

        document = docx_library.Document(io.BytesIO(data))
        heading = next(p for p in document.paragraphs if "Artificial Neural Network" in p.text)
        self.assertEqual(
            heading.text,
            "شبکهٔ عصبی (Artificial Neural Network) در تشخیص تصویر",
        )
        self.assertTrue(any(run.font.rtl is True for run in heading.runs))
        self.assertTrue(any(run.font.rtl is False for run in heading.runs))

    def test_table_is_right_to_left(self):
        data = build_notes_docx(full_notes(), meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("<w:bidiVisual/>", body)
        self.assertIn("tblHeader", body)  # header row repeats across pages

    def test_custom_font_is_applied(self):
        notes = StructuredNotes.from_payload(json.loads(sample_notes_json()))
        data = build_notes_docx(notes, font="B Nazanin", meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
        self.assertIn('w:cs="B Nazanin"', body)

    def test_cover_section_is_bordered_unpaginated_and_body_restarts_at_one(self):
        data = build_notes_docx(full_notes(), meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = etree.fromstring(archive.read("word/document.xml"))
            sections = list(root.iter(f"{W}sectPr"))
            self.assertEqual(len(sections), 2)
            self.assertIsNotNone(sections[0].find(f"{W}pgBorders"))
            self.assertIsNone(sections[1].find(f"{W}pgBorders"))
            self.assertIsNone(sections[0].find(f"{W}headerReference"))
            self.assertIsNone(sections[0].find(f"{W}footerReference"))
            numbering = sections[1].find(f"{W}pgNumType")
            self.assertIsNotNone(numbering)
            self.assertEqual(numbering.get(f"{W}start"), "1")
            for section in sections:
                self.assertIsNotNone(section.find(f"{W}bidi"))
            for name in archive.namelist():
                if name.endswith(".xml") or name.endswith(".rels"):
                    etree.fromstring(archive.read(name))

    def test_cover_page_included_when_design_allows(self):
        design = resolve_design({"enabled": True, "cover": True, "toc": True})
        data = build_notes_docx(full_notes(), design=design, meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
        # The cover is its own section: a mid-document section break plus the
        # final one, and page numbering restarts after it.
        self.assertGreaterEqual(body.count("<w:sectPr"), 2)
        self.assertIn("به نام خدا", body)

    def test_document_metadata_records_the_job(self):
        data = build_notes_docx(full_notes(), meta=META)
        import docx as docx_library

        document = docx_library.Document(io.BytesIO(data))
        self.assertEqual(document.core_properties.title, "فارماکولوژی دیابت")
        self.assertEqual(document.core_properties.author, "Gamas Bot")


class PracticalTipsAndTablesTests(unittest.TestCase):
    """The v4 booklet surfaces the lecturer's advice and every table."""

    def _notes(self) -> StructuredNotes:
        return StructuredNotes(
            title="جزوهٔ کاربردی",
            sections=[
                NoteSection(
                    heading="دوز دارو",
                    paragraphs=["دوز شروع ۵۰۰ mg است."],
                    table=NoteTable(
                        ["دارو", "دوز"], [["Metformin", "۵۰۰ mg"]], "دوزهای شروع"
                    ),
                    tables=(
                        NoteTable(
                            ["عارضه", "شیوع"],
                            [["تهوع", "شایع"]],
                            "عارضه‌های شایع",
                        ),
                    ),
                )
            ],
            practical_tips=[
                PracticalTip(
                    "Metformin در نارسایی کلیوی قطع می‌شود.",
                    basis="بیمار با نارسایی کلیوی نباید Metformin بگیرد",
                    kind="هشدار",
                )
            ],
        )

    def test_every_table_and_its_caption_is_rendered(self):
        import docx as docx_library

        data = build_notes_docx(self._notes(), meta=META)
        document = docx_library.Document(io.BytesIO(data))
        texts = [paragraph.text for paragraph in document.paragraphs]
        self.assertIn("دوزهای شروع", texts)
        self.assertIn("عارضه‌های شایع", texts)
        self.assertEqual(len(document.tables) >= 2, True)

    def test_the_practical_tips_section_is_rendered_as_a_box(self):
        import docx as docx_library

        base = self._notes()
        notes = StructuredNotes(
            title=base.title,
            sections=base.sections,
            practical_tips=base.practical_tips,
            review_questions=("دوز شروع Metformin چقدر است؟",),
        )
        data = build_notes_docx(notes, meta=META)
        document = docx_library.Document(io.BytesIO(data))
        texts = [paragraph.text for paragraph in document.paragraphs]
        self.assertIn("نکته‌های کاربردی", texts)

        tip_index = texts.index("نکته‌های کاربردی")
        review_index = texts.index("پرسش‌های مرور")
        self.assertLess(tip_index, review_index)
        tip_paragraphs = [text for text in texts if "Metformin در نارسایی کلیوی" in text]
        self.assertTrue(tip_paragraphs)
        self.assertIn("هشدار", tip_paragraphs[0])

        # The section is a box: shading and border, not just a heading.
        body = document_root(data)
        tip_element = next(
            paragraph._p
            for paragraph in document.paragraphs
            if "Metformin در نارسایی کلیوی" in paragraph.text
        )
        properties = tip_element.find(f"{W}pPr")
        self.assertIsNotNone(properties)
        shading = properties.find(f"{W}shd")
        self.assertIsNotNone(shading)
        self.assertEqual(shading.get(f"{W}fill"), KEYPOINT_SHADE)
        borders = properties.find(f"{W}pBdr")
        self.assertIsNotNone(borders)
        self.assertIn(KEYPOINT_BORDER, etree.tostring(borders, encoding="unicode"))
        self.assertIn("w:shd", etree.tostring(body, encoding="unicode"))

    def test_tips_are_part_of_the_length_estimate(self):
        from gamas_bot.docx_export import _document_chars

        base = self._notes()
        without_tips = StructuredNotes(
            title=base.title, sections=base.sections, practical_tips=()
        )
        self.assertGreater(
            _document_chars(base),
            _document_chars(without_tips) + len(base.practical_tips[0].text),
        )

    def test_tables_are_part_of_the_length_estimate(self):
        from gamas_bot.docx_export import _document_chars, _sections_chars

        section = self._notes().sections[0]
        bare = NoteSection(heading=section.heading, paragraphs=section.paragraphs)
        self.assertGreater(_sections_chars([section]), _sections_chars([bare]))
        self.assertEqual(_document_chars(self._notes()), _sections_chars([section])
                         + len(self._notes().title)
                         + sum(len(tip.kind) + len(tip.text)
                               for tip in self._notes().practical_tips))


class OOXMLChildOrderTests(unittest.TestCase):
    """Check schema-sensitive child ordering in the generated DOCX parts."""

    def test_core_properties_follow_ooxml_sequence(self):
        notes = StructuredNotes.from_payload(json.loads(PALETTE_NOTES_JSON))
        data = build_notes_docx(notes, meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            document = etree.fromstring(archive.read("word/document.xml"))
            styles = etree.fromstring(archive.read("word/styles.xml"))

        ppr_order = (
            "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
            "widowControl", "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs",
            "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
            "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
            "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents",
            "suppressOverlap", "jc", "textDirection", "textAlignment",
            "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr",
            "pPrChange",
        )
        rpr_order = (
            "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps",
            "strike", "dstrike", "outline", "shadow", "emboss", "imprint", "noProof",
            "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern",
            "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd",
            "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
            "specVanish", "oMath",
        )
        sect_order = (
            "headerReference", "footerReference", "footnotePr", "endnotePr", "type",
            "pgSz", "pgMar", "paperSrc", "pgBorders", "lnNumType", "pgNumType",
            "cols", "formProt", "vAlign", "noEndnote", "titlePg", "textDirection",
            "bidi", "rtlGutter", "docGrid", "printerSettings", "sectPrChange",
        )
        tblpr_order = (
            "tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize",
            "tblStyleColBandSize", "tblW", "jc", "tblCellSpacing", "tblInd",
            "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook", "tblCaption",
            "tblDescription", "tblPrChange",
        )

        def assert_children_ordered(elements, order, description):
            ranks = {f"{W}{name}": index for index, name in enumerate(order)}
            for element in elements:
                actual = [ranks[child.tag] for child in element if child.tag in ranks]
                self.assertEqual(actual, sorted(actual), description)

        assert_children_ordered(
            list(document.iter(f"{W}pPr")) + list(styles.iter(f"{W}pPr")),
            ppr_order,
            "pPr child sequence",
        )
        assert_children_ordered(
            list(document.iter(f"{W}rPr")) + list(styles.iter(f"{W}rPr")),
            rpr_order,
            "rPr child sequence",
        )
        assert_children_ordered(list(document.iter(f"{W}sectPr")), sect_order, "sectPr child sequence")
        assert_children_ordered(list(document.iter(f"{W}tblPr")), tblpr_order, "tblPr child sequence")


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

    def test_plain_docx_is_rtl_with_page_numbers(self):
        body = "متن ساده برای بررسی راست‌به‌چپ بودن سند."
        data = build_plain_docx("جزوهٔ خام", body, meta=META)
        footer = ""
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            doc_xml = archive.read("word/document.xml").decode("utf-8")
            for name in archive.namelist():
                if name.startswith("word/footer"):
                    footer += archive.read(name).decode("utf-8")
        self.assertIn('<w:bidi w:val="1"/>', doc_xml)
        self.assertIn("PAGE", footer)


PALETTE_NOTES_JSON = (
    '{"title": "درس رنگ‌ها و جدول‌ها", "summary": "خلاصهٔ درس با یک اصطلاح English.", '
    '"sections": [{'
    '"heading": "جدول‌ها", '
    '"paragraphs": ["توضیح مختصر دربارهٔ Metformin و دوز آن."], '
    '"formulas": ["eGFR = 175 × (Scr)^-1.154"], '
    '"key_points": ["نکتهٔ کلیدی اول", "نکتهٔ کلیدی دوم"], '
    '"table": {"headers": ["دارو", "دوز روزانه"], "rows": ['
    '["Metformin", "دوز 500 mg روزانه"], ["Insulin", "10 units nightly"], '
    '["Aspirin", "۸۰ میلی‌گرم"]]}, '
    '"callouts": [{"kind": "هشدار", "text": "در AKI قطع شود"}, '
    '{"kind": "یادآوری", "text": "پایش HbA1c"}, {"kind": "نکته", "text": "مصرف با غذا"}]'
    "}], "
    '"glossary": [{"term": "HbA1c", "definition": "هموگلوبین گلیکوزیله"}, '
    '{"term": "AKI", "definition": "آسیب حاد کلیه"}, {"term": "CKD", "definition": "بیماری مزمن کلیه"}]}'
)

TOC_NOTES_JSON = (
    '{"title": "درس بلند", "summary": "خلاصه.", "sections": ['
    + ",".join(
        '{"heading": "بخش %d", "paragraphs": ["%s"]}'
        % (index, "پاراگراف بلند فارسی برای عبور از آستانهٔ فهرست مطالب. " * 4)
        for index in range(1, 5)
    )
    + "]}"
)


class RtlAlignmentContractTests(unittest.TestCase):
    """Every paragraph of the deliverable is explicitly right- or centre-aligned.

    The booklet's contract is that no paragraph — body text, table cell,
    structural spacer, TOC row or the sectPr-bearing break paragraph — relies
    on a reader default for its alignment, and every paragraph containing
    Persian declares its right-to-left base direction (``w:bidi``).
    """

    def assert_alignment_contract(self, data: bytes) -> None:
        root = document_root(data)
        bad_right_jc = []
        bad_bidi = []
        paragraphs = 0
        for paragraph in root.iter(f"{W}p"):
            paragraphs += 1
            properties = paragraph.find(f"{W}pPr")
            jc = properties.find(f"{W}jc") if properties is not None else None
            # RTL right/start is represented by no jc. Center and justified
            # paragraphs are explicit; jc=right is the known Word desktop trap.
            if jc is not None and jc.get(f"{W}val") not in {"center", "both"}:
                bad_right_jc.append(paragraph_text(paragraph)[:40])
            if has_persian(paragraph_text(paragraph)):
                bidi = properties.find(f"{W}bidi") if properties is not None else None
                if bidi is None or bidi.get(f"{W}val", "1") != "1":
                    bad_bidi.append(paragraph_text(paragraph)[:40])
        self.assertEqual(bad_right_jc, [])
        self.assertEqual(bad_bidi, [])
        self.assertGreater(paragraphs, 20)

    def test_body_uses_logical_start_for_headings_and_justifies_prose(self):
        data = build_notes_docx(full_notes(), meta=META)
        root = document_root(data)
        headings = [p for p in root.iter(f"{W}p") if paragraph_text(p).startswith("۱. Metformin")]
        self.assertTrue(headings)
        for heading in headings:
            p_pr = heading.find(f"{W}pPr")
            jc = p_pr.find(f"{W}jc") if p_pr is not None else None
            self.assertIsNone(jc)
        prose = [p for p in root.iter(f"{W}p") if paragraph_text(p) == "خط اول درمان دیابت نوع ۲ است."]
        self.assertTrue(prose)
        self.assertEqual(prose[0].find(f"{W}pPr/{W}jc").get(f"{W}val"), "both")

    def test_theme_language_and_complex_run_locale_are_persian(self):
        with zipfile.ZipFile(io.BytesIO(build_notes_docx(full_notes(), meta=META))) as archive:
            settings = etree.fromstring(archive.read("word/settings.xml"))
            styles = etree.fromstring(archive.read("word/styles.xml"))
            self.assertFalse(
                any(node.get(f"{W}val") == "right" for node in styles.iter(f"{W}jc"))
            )
            theme_lang = settings.find(f"{W}themeFontLang")
            self.assertIsNotNone(theme_lang)
            self.assertEqual(theme_lang.get(f"{W}val"), "fa-IR")
            self.assertEqual(theme_lang.get(f"{W}bidi"), "fa-IR")
            body = etree.fromstring(archive.read("word/document.xml"))
        persian_runs = []
        for run in body.iter(f"{W}r"):
            text = "".join(node.text or "" for node in run.iter(f"{W}t"))
            if has_persian(text):
                persian_runs.append(run)
        self.assertTrue(persian_runs)
        for run in persian_runs:
            r_pr = run.find(f"{W}rPr")
            self.assertEqual(r_pr.find(f"{W}lang").get(f"{W}val"), "fa-IR")
            self.assertEqual(r_pr.find(f"{W}lang").get(f"{W}bidi"), "fa-IR")
            self.assertIsNotNone(r_pr.find(f"{W}szCs"))
            self.assertIsNotNone(r_pr.find(f"{W}rtl"))

    def test_notes_booklet_satisfies_the_contract(self):
        notes = StructuredNotes.from_payload(json.loads(PALETTE_NOTES_JSON))
        self.assert_alignment_contract(build_notes_docx(notes, meta=META))

    def test_booklet_with_toc_satisfies_the_contract(self):
        # The TOC page adds linked rows in a borderless RTL table; every row
        # obeys the same paragraph direction/alignment contract.
        notes = StructuredNotes.from_payload(json.loads(TOC_NOTES_JSON))
        data = build_notes_docx(notes, meta=META)
        self.assertIn("فهرست مطالب", docx_text(data))  # the TOC page really was built
        self.assert_alignment_contract(data)

    def test_plain_docx_satisfies_the_contract(self):
        body = "# عنوان\n\nمتن ساده با یک واژهٔ English در میان جمله.\n\n- مورد"
        self.assert_alignment_contract(build_plain_docx("جزوهٔ خام", body, meta=META))


class RtlTableTests(unittest.TestCase):
    """Tables are right-to-left, right-aligned and readable in mixed scripts."""

    @classmethod
    def setUpClass(cls):
        notes = StructuredNotes.from_payload(json.loads(PALETTE_NOTES_JSON))
        cls.data = build_notes_docx(notes, meta=META)
        cls.root = document_root(cls.data)

    def test_every_table_is_bidi_visual(self):
        tables = list(self.root.iter(f"{W}tbl"))
        self.assertTrue(tables)
        for table in tables:
            tbl_pr = table.find(f"{W}tblPr")
            self.assertIsNotNone(tbl_pr.find(f"{W}bidiVisual"))

    def test_table_headers_are_centred_and_body_uses_rtl_start(self):
        for table in self.root.iter(f"{W}tbl"):
            for row_index, row in enumerate(table.findall(f"{W}tr")):
                for cell in row.findall(f"{W}tc"):
                    for paragraph in cell.findall(f"{W}p"):
                        jc = paragraph.find(f"{W}pPr/{W}jc")
                        if row_index == 0:
                            self.assertIsNotNone(jc)
                            self.assertEqual(jc.get(f"{W}val"), "center")
                        else:
                            self.assertTrue(jc is None or jc.get(f"{W}val") == "both")

    def test_latin_cell_terms_use_nonbreaking_spaces(self):
        table_texts = [
            "".join(node.text or "" for node in cell.iter(f"{W}t"))
            for cell in self.root.iter(f"{W}tc")
        ]
        self.assertTrue(any("500\u00a0mg" in text for text in table_texts))

    def test_mixed_script_cells_split_into_direction_runs(self):
        import docx as docx_library

        document = docx_library.Document(io.BytesIO(self.data))
        mixed_cells = 0
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        text = paragraph.text
                        if not (has_persian(text) and any(ch.isascii() and ch.isalpha() for ch in text)):
                            continue
                        mixed_cells += 1
                        directions = {
                            run.font.rtl for run in paragraph.runs if run.text.strip()
                        }
                        self.assertIn(True, directions)   # Persian run is RTL
                        self.assertIn(False, directions)  # Latin run is explicitly LTR
        self.assertGreaterEqual(mixed_cells, 1)


class BookletPaletteTests(unittest.TestCase):
    """The palette is one harmonised family with visible frames on every box."""

    @classmethod
    def setUpClass(cls):
        notes = StructuredNotes.from_payload(json.loads(PALETTE_NOTES_JSON))
        cls.data = build_notes_docx(notes, meta=META)
        cls.root = document_root(cls.data)
        cls.body = etree.tostring(cls.root).decode()

    def test_every_shaded_box_is_framed_with_a_deeper_tone(self):
        # A box framed with its own fill colour is invisible; every pBdr next
        # to a shading must use a different (deeper) tone of the same family.
        framed = 0
        for borders in self.root.iter(f"{W}pBdr"):
            properties = borders.getparent()
            shading = properties.find(f"{W}shd")
            if shading is None:
                continue
            fill = shading.get(f"{W}fill")
            colors = {edge.get(f"{W}color") for edge in borders}
            self.assertNotIn(fill, colors)
            framed += 1
        self.assertGreaterEqual(framed, 4)  # summary, key points, 3 callouts, quote

    def test_callout_kinds_have_distinct_fills_and_borders(self):
        for kind, fill in CALLOUT_SHADES.items():
            self.assertIn(fill, self.body)
            self.assertNotEqual(CALLOUT_BORDERS[kind], fill)
        fills = set(CALLOUT_SHADES.values())
        self.assertEqual(len(fills), 3)  # «نکته» no longer shares «یادآوری»'s tint

    def test_palette_tints_are_present(self):
        for color in (
            ACCENT_HEX, ACCENT_SOFT_HEX, SUMMARY_SHADE, SUMMARY_BORDER,
            KEYPOINT_SHADE, KEYPOINT_BORDER, FORMULA_SHADE, ZEBRA_SHADE,
            TABLE_BORDER_COLOR,
        ):
            self.assertIn(color, self.body)

    def test_heading_hierarchy_uses_two_accent_steps(self):
        self.assertNotEqual(HEADING_COLORS[1], HEADING_COLORS[2])
        self.assertNotEqual(HEADING_COLORS[1], HEADING_COLORS[3])

    def test_content_tables_have_soft_borders_and_zebra_rows(self):
        striped_cells = 0
        for table in self.root.iter(f"{W}tbl"):
            borders = table.find(f"{W}tblPr/{W}tblBorders")
            if borders is None:
                continue
            for edge in borders:
                if edge.get(f"{W}val") == "nil":
                    continue  # the borderless TOC layout table
                # Content tables never draw black grid lines any more.
                self.assertEqual(edge.get(f"{W}color"), TABLE_BORDER_COLOR)
            for shading in table.iter(f"{W}shd"):
                if shading.get(f"{W}fill") == ZEBRA_SHADE:
                    striped_cells += 1
        # 3-row content table + 3-entry glossary -> one striped 2-cell row each.
        self.assertEqual(striped_cells, 4)

    def test_header_row_is_accent_filled_and_vertically_centred(self):
        centred = 0
        for table in self.root.iter(f"{W}tbl"):
            rows = table.findall(f"{W}tr")
            if not rows:
                continue
            for cell in rows[0].findall(f"{W}tc"):
                tc_pr = cell.find(f"{W}tcPr")
                shading = tc_pr.find(f"{W}shd") if tc_pr is not None else None
                if shading is not None and shading.get(f"{W}fill") == ACCENT_HEX:
                    self.assertIsNotNone(tc_pr.find(f"{W}vAlign"))
                    centred += 1
        self.assertEqual(centred, 4)  # 2 content-table + 2 glossary header cells




class BookletSkeletonDocxTests(unittest.TestCase):
    """CR-4: the exporter appends a final «جمع‌بندی» and a glossary section."""

    def test_end_summary_and_glossary_are_rendered(self):
        text = docx_text(build_notes_docx(full_notes(), meta=META))
        self.assertIn("جمع‌بندی", text)
        self.assertIn("واژه‌نامه", text)
        self.assertIn("HbA1c", text)

    def test_glossary_is_derived_from_definitions_when_absent(self):
        notes = StructuredNotes.from_payload(json.loads(
            '{"title": "درس", "summary": "خلاصه", "sections": [{"heading": "بخش", '
            '"paragraphs": ["متن درس"], "definitions": ['
            '{"term": "آنزیم", "term_en": "Enzyme", "definition": "کاتالیزگر زیستی"}]}]}'
        ))
        text = docx_text(build_notes_docx(notes, meta=META))
        self.assertIn("واژه‌نامه", text)
        self.assertIn("Enzyme", text)          # derived from the section definition
        self.assertIn("کاتالیزگر زیستی", text)
        self.assertIn("جمع‌بندی", text)       # end recap from summary

    def test_toc_is_generated_for_a_multi_section_booklet(self):
        notes = full_notes()
        # add sibling sections so the long-document TOC gate is satisfied
        from gamas_bot.notes import NoteSection
        notes = StructuredNotes(
            title=notes.title, summary=notes.summary, learning_objectives=notes.learning_objectives,
            key_points=notes.key_points, glossary=notes.glossary,
            sections=tuple(notes.sections) + tuple(
                NoteSection(heading=f"بخش {i}", paragraphs=["توضیح بلند فارسی برای فهرست مطالب. " * 4])
                for i in range(2, 6)
            ),
        )
        text = docx_text(build_notes_docx(notes, meta=META))
        self.assertIn("فهرست مطالب", text)



if __name__ == "__main__":
    unittest.main()
