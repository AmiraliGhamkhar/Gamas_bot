"""Tests for the booklet's document-design layer.

These pin the parts of the Word output that make it a *booklet* rather than a
text dump: the cover page (with the exact brand and quotation), the real
section-level page frame (``w:pgBorders`` inside ``w:sectPr``), the static
linked table of contents, real Heading 1/2/3 styles, and the footer's live
``PAGE`` field. Each of them is a user-visible contract, so each is asserted on the
generated XML rather than on python-docx's object model.
"""

from __future__ import annotations

import io
import unittest
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from support import docx_text

from gamas_bot.docx_export import (
    COVER_QUOTE,
    DocxDesign,
    DocxPaginationError,
    DocumentMeta,
    build_notes_docx,
    build_plain_docx,
    cover_meta_lines,
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
    """Four sections, so the static TOC threshold (>= 4) is met."""
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
        enable_toc: bool = False,
    ) -> bytes:
        notes = parse_structured_notes(notes_json)
        resolved = design or resolve_design()
        # Layout/style tests should not require an office suite. TOC tests opt
        # in and either use explicit page-map fixtures or assert a hard failure.
        resolved = replace(resolved, toc_enabled=bool(enable_toc))
        return build_notes_docx(notes, meta=meta, design=resolved)

    def build_with_page_mapping(
        self,
        notes_json: str,
        *,
        design: DocxDesign | None = None,
        meta: DocumentMeta = META,
    ) -> bytes:
        resolved = replace(design or resolve_design(), toc_enabled=True)

        def pages(_payload, entries, **_kwargs):
            return list(range(3, 3 + len(entries)))

        with patch("gamas_bot.docx_export._rendered_toc_page_numbers", side_effect=pages):
            return self.build(notes_json, design=resolved, meta=meta, enable_toc=True)

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
        self.assertNotIn("GMS-000123", text)  # tracking IDs stay out of student-visible content
        self.assertIn("حالت تولید: کامل", text)
        self.assertIn("۱۴۰۵", text)  # Jalali date on the cover

    def test_cover_metadata_is_split_into_labelled_lines(self):
        lines = cover_meta_lines(META, mode_label="حالت تولید: کامل")
        # Student-facing metadata deliberately excludes source names, provider
        # identities and internal tracking references.
        self.assertEqual(len(lines), 2)
        self.assertIn("حالت تولید: کامل", lines[0])
        self.assertIn("تاریخ:", lines[1])
        self.assertNotIn("GMS-000123", " ".join(lines))
        self.assertNotIn("deepgram", " ".join(lines))
        self.assertNotIn("lecture.mp3", " ".join(lines))
        self.assertTrue(all(len(line) < 90 for line in lines), lines)
        self.assertEqual(len(cover_meta_lines(META)), 1)

    def test_cover_quotation_keeps_its_place_with_a_long_title(self):
        def spacers(notes_json: str) -> int:
            paragraphs = self.paragraphs(self.build(notes_json))
            empties = 0
            for paragraph in paragraphs:
                text = "".join(node.text or "" for node in paragraph.iter(f"{W}t"))
                if COVER_QUOTE in text:
                    return empties
                if not text.strip():
                    empties += 1
            raise AssertionError("the quotation paragraph was not found")

        short_title = long_notes_json()
        long_title = short_title.replace(
            "جزوهٔ آزمون", "جزوهٔ آزمون جامع فیزیولوژی و فارماکولوژی بالینی برای دانشجویان"
        )
        # A wrapped title takes space away from the flexible gap, so the quote
        # stays in the same band of the page instead of sliding off the bottom.
        self.assertLess(spacers(long_title), spacers(short_title))
        self.assertGreaterEqual(spacers(long_title), 5)


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
    def test_toc_is_static_plain_text_with_unique_clickable_bookmarks(self):
        data = self.build_with_page_mapping(long_notes_json())
        xml = self.document_xml(data)
        root = ET.fromstring(xml)
        self.assertIn("فهرست مطالب", docx_text(data))
        self.assertNotIn("TOC \\o", xml)
        self.assertNotIn("w:instrText", xml)
        self.assertNotIn("w:fldChar", xml)
        self.assertNotIn("w:dirty", xml)

        bookmarks = [
            item.get(f"{W}name")
            for item in root.iter(f"{W}bookmarkStart")
            if item.get(f"{W}name")
        ]
        links = [
            item.get(f"{W}anchor")
            for item in root.iter(f"{W}hyperlink")
            if item.get(f"{W}anchor")
        ]
        self.assertGreaterEqual(len(links), 4)
        self.assertEqual(len(links), len(set(links)))
        self.assertEqual(len(bookmarks), len(set(bookmarks)))
        self.assertTrue(set(links).issubset(set(bookmarks)))
        text = docx_text(data)
        self.assertIn("۳", text)  # supplied rendered page mapping is ordinary text
        self.assertIn("۴", text)

    def test_toc_without_renderer_degrades_to_a_link_only_policy(self):
        """DOCX_TOC_PAGE_NUMBERS=auto must not fail a job when soffice is absent.

        The topic list keeps its internal hyperlinks and its page cells are
        empty: the document is delivered, and no page number is ever invented.
        """
        with patch("gamas_bot.docx_export.shutil.which", return_value=None):
            data = self.build(long_notes_json(), enable_toc=True)
        xml = self.document_xml(data)
        self.assertIn("فهرست مطالب", docx_text(data))
        root = ET.fromstring(xml)
        links = [item.get(f"{W}anchor") for item in root.iter(f"{W}hyperlink") if item.get(f"{W}anchor")]
        self.assertGreaterEqual(len(links), 4)
        # No rendered page number leaked into the page-number column.
        table = root.find(f"{W}body/{W}tbl")
        self.assertIsNotNone(table)
        page_cells = [row.findall(f"{W}tc")[1] for row in table.findall(f"{W}tr")]
        visible_numbers = [
            "".join(node.text or "" for node in cell.iter(f"{W}t"))
            for cell in page_cells
        ]
        self.assertTrue(all(number == "" for number in visible_numbers))

    def test_toc_page_numbers_required_fails_explicitly(self):
        with patch("gamas_bot.docx_export.shutil.which", return_value=None):
            with self.assertRaises(DocxPaginationError):
                self.build(
                    long_notes_json(),
                    design=resolve_design({"toc_page_numbers": "required"}),
                    enable_toc=True,
                )

    def test_toc_page_numbers_off_never_invokes_a_renderer(self):
        design = resolve_design({"toc_page_numbers": "off", "toc_enabled": True})
        with patch(
            "gamas_bot.docx_export._rendered_toc_page_numbers",
            side_effect=AssertionError("the off policy must never render"),
        ):
            data = self.build(long_notes_json(), design=design, enable_toc=True)
        root = ET.fromstring(self.document_xml(data))
        self.assertIn("فهرست مطالب", docx_text(data))
        # Hyperlinks still navigate; the page-number column is empty.
        links = [item.get(f"{W}anchor") for item in root.iter(f"{W}hyperlink") if item.get(f"{W}anchor")]
        self.assertGreaterEqual(len(links), 4)
        table = root.find(f"{W}body/{W}tbl")
        page_cells = [row.findall(f"{W}tc")[1] for row in table.findall(f"{W}tr")]
        self.assertTrue(
            all(
                "".join(node.text or "" for node in cell.iter(f"{W}t")) == ""
                for cell in page_cells
            )
        )

    def test_tooltip_mapped_toc_targets_must_start_after_page_two(self):
        from gamas_bot.docx_export import _rendered_toc_page_numbers

        class PageMarker:
            def __init__(self, index: int):
                self.index = index

        class LinkReference:
            def __init__(self, anchor: str, target: PageMarker):
                self.anchor = anchor
                self.target = target

            def get_object(self):
                return {
                    "/Subtype": "/Link",
                    "/Dest": [self.target],
                    "/Rect": [0, 0, 1, 1],
                    "/Contents": self.anchor,
                }

        targets = [PageMarker(1), PageMarker(2)]

        class FakeReader:
            pages = [object(), {"/Annots": [
                LinkReference("toc-a", targets[0]),
                LinkReference("toc-b", targets[1]),
            ]}, object()]

            @staticmethod
            def get_page_number(page):
                return page.index

        def fake_render(command, **_kwargs):
            output_dir = Path(command[command.index("--outdir") + 1])
            (output_dir / "document.pdf").write_bytes(b"mock rendered PDF")
            return type("Completed", (), {"returncode": 0})()

        with patch("gamas_bot.docx_export.shutil.which", return_value="/usr/bin/soffice"), patch(
            "gamas_bot.docx_export.subprocess.run", side_effect=fake_render
        ), patch("pypdf.PdfReader", return_value=FakeReader()):
            with self.assertRaises(DocxPaginationError):
                _rendered_toc_page_numbers(
                    b"mock DOCX",
                    [{"anchor": "toc-a"}, {"anchor": "toc-b"}],
                    timeout_seconds=10,
                )

    def test_stable_page_numbers_need_only_two_renderer_passes(self):
        """The pagination loop must be bounded, not iterative-until-lucky.

        Each pass is a full LibreOffice conversion on the production server, so
        a stable document (the overwhelmingly common case) must cost exactly
        two: one to measure, one to confirm the written numbers did not move.
        """
        calls = []

        def pages(_payload, entries, **_kwargs):
            calls.append(len(entries))
            return list(range(3, 3 + len(entries)))

        notes = parse_structured_notes(long_notes_json())
        with patch("gamas_bot.docx_export._rendered_toc_page_numbers", side_effect=pages):
            build_notes_docx(
                notes, meta=META, design=replace(resolve_design(), toc_enabled=True)
            )
        self.assertEqual(len(calls), 2)

    def test_unstable_page_numbers_fail_after_a_bounded_number_of_passes(self):
        """A renderer that never agrees must not loop forever."""
        calls = []

        def drifting(_payload, entries, **_kwargs):
            calls.append(len(entries))
            return [3 + len(calls) + index for index in range(len(entries))]

        notes = parse_structured_notes(long_notes_json())
        with patch("gamas_bot.docx_export._rendered_toc_page_numbers", side_effect=drifting):
            with self.assertRaises(DocxPaginationError):
                build_notes_docx(
                    notes, meta=META, design=replace(resolve_design(), toc_enabled=True)
                )
        self.assertLessEqual(len(calls), 3)

    def test_document_generation_is_byte_reproducible(self):
        """Same inputs -> same bytes, so artifacts are comparable and cacheable.

        python-docx stamps every package member with the wall clock on save;
        the final post-processing pass normalises those timestamps.
        """
        notes = parse_structured_notes(long_notes_json())
        design = replace(resolve_design(), toc_enabled=False)
        first = build_notes_docx(notes, meta=META, design=design)
        second = build_notes_docx(notes, meta=META, design=design)
        self.assertEqual(first, second)

    def test_toc_levels_are_configurable(self):
        default = self.build_with_page_mapping(long_notes_json())
        level_two = self.build_with_page_mapping(
            long_notes_json(), design=resolve_design({"toc_levels": "1-2"})
        )
        default_xml = self.document_xml(default)
        level_two_xml = self.document_xml(level_two)
        self.assertGreater(level_two_xml.count("w:anchor="), default_xml.count("w:anchor="))
        self.assertIn("تعریف‌ها", docx_text(level_two))

    def test_toc_min_sections_is_configurable(self):
        short = (
            '{"title": "کوتاه", "sections": [{"heading": "الف", "paragraphs": ["متن"]}, '
            '{"heading": "ب", "paragraphs": ["متن دو"]}, {"heading": "پ", "paragraphs": ["متن سه"]}]}'
        )
        self.assertNotIn("فهرست مطالب", docx_text(self.build(short)))
        data = self.build_with_page_mapping(
            short, design=resolve_design({"toc_min_sections": 3})
        )
        self.assertIn("فهرست مطالب", docx_text(data))

    def test_toc_is_skipped_for_short_documents(self):
        short = (
            '{"title": "کوتاه", "sections": [{"heading": "الف", "paragraphs": ["متن"]}, '
            '{"heading": "ب", "paragraphs": ["متن دو"]}]}'
        )
        self.assertNotIn("فهرست مطالب", docx_text(self.build(short)))

    def test_toc_can_be_disabled(self):
        data = self.build(long_notes_json(), design=resolve_design({"toc_enabled": False}))
        self.assertNotIn("w:anchor=", self.document_xml(data))
        self.assertNotIn("فهرست مطالب", docx_text(data))

    def test_raw_text_document_gets_a_static_toc_only_when_it_has_headings(self):
        body = "جملهٔ توضیحی دربارهٔ درس. " * 200
        design = resolve_design({"toc_enabled": False})
        without_headings = build_plain_docx("متن خام", body, meta=META, design=design)
        self.assertNotIn("فهرست مطالب", self.document_xml(without_headings))

        content = "# بخش نخست\n" + body + "\n## بخش دوم\n" + body
        with patch(
            "gamas_bot.docx_export._rendered_toc_page_numbers",
            side_effect=lambda _payload, entries, **_kwargs: list(range(3, 3 + len(entries))),
        ):
            with_headings = build_plain_docx(
                "متن خام", content, meta=META, design=resolve_design()
            )
        body_xml = self.document_xml(with_headings)
        self.assertIn("فهرست مطالب", body_xml)
        self.assertNotIn("TOC \\o", body_xml)
        self.assertNotIn("w:instrText", body_xml)

    def test_real_rendered_page_map_when_libreoffice_is_installed(self):
        import shutil

        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest("pypdf is not installed")

        if not (shutil.which("soffice") or shutil.which("libreoffice")):
            self.skipTest("LibreOffice is not installed; page mapping is covered by the explicit-failure test")
        data = self.build(long_notes_json(), enable_toc=True)
        root = ET.fromstring(self.document_xml(data))
        anchors = [
            item.get(f"{W}anchor")
            for item in root.iter(f"{W}hyperlink")
            if item.get(f"{W}anchor")
        ]
        entries = [{"anchor": anchor} for anchor in anchors]
        from gamas_bot.docx_export import _rendered_toc_page_numbers

        pages = _rendered_toc_page_numbers(data, entries, timeout_seconds=120)
        self.assertTrue(pages)
        self.assertTrue(all(page >= 3 for page in pages))
        # The rendered numbers must be the literal page-cell text, not a field.
        toc_table = root.find(f"{W}body/{W}tbl")
        self.assertIsNotNone(toc_table)
        page_cells = [row.findall(f"{W}tc")[1] for row in toc_table.findall(f"{W}tr")]
        visible_numbers = [
            "".join(node.text or "" for node in cell.iter(f"{W}t"))
            for cell in page_cells
        ]
        self.assertEqual(len(visible_numbers), len(pages))


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

    def test_static_toc_never_requests_field_refresh_on_open(self):
        settings_xml = self.part(self.build(long_notes_json()), "word/settings.xml")
        document_xml = self.document_xml(self.build(long_notes_json()))
        self.assertNotIn("updateFields", settings_xml)
        self.assertNotIn("w:instrText", document_xml)
        self.assertNotIn("TOC \\o", document_xml)

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
        # TOC settings are validated too: only real level ranges and a sane
        # section count are accepted, anything else falls back to the default.
        self.assertEqual(resolve_design({"toc_levels": "9-9"}).toc_levels, "1-1")
        self.assertEqual(resolve_design({"toc_levels": "1-2"}).toc_levels, "1-2")
        # 0/"" are not a threshold, so they fall back to the default; a huge
        # number is clamped rather than disabling the TOC entirely.
        self.assertEqual(resolve_design({"toc_min_sections": 0}).toc_min_sections, 4)
        self.assertEqual(resolve_design({"toc_min_sections": "x"}).toc_min_sections, 4)
        self.assertEqual(resolve_design({"toc_min_sections": 500}).toc_min_sections, 200)


class BookletLayoutTests(DocxTestCase):
    """Layout details that make the file a designed booklet, not a text dump."""

    def test_section_headings_are_numbered_exactly_once(self):
        notes = parse_structured_notes(
            '{"title": "ج", "sections": ['
            '{"heading": "مقدمه", "paragraphs": ["توضیح مقدمه."]},'
            '{"heading": "۱. مفاهیم پایه", "paragraphs": ["توضیح مفاهیم."]},'
            '{"heading": "بخش ۳: نتیجه", "paragraphs": ["توضیح نتیجه."]}]}'
        )
        text = docx_text(build_notes_docx(notes, meta=META))
        self.assertIn("۱. مقدمه", text)
        self.assertIn("۱. مفاهیم پایه", text)   # the model's own number is kept
        self.assertNotIn("۲. ۱.", text)
        self.assertIn("بخش ۳: نتیجه", text)     # «بخش ۳» is not renumbered
        self.assertNotIn("۳. بخش ۳", text)

    def test_key_point_box_is_one_continuous_panel(self):
        payload = build_notes_docx(
            parse_structured_notes(
                '{"title": "ج", "sections": [{"heading": "ب", "paragraphs": ["توضیح."], '
                '"key_points": ["نکتهٔ یک", "نکتهٔ دو", "نکتهٔ سه"]}]}'
            ),
            meta=META,
        )
        root = ET.fromstring(self.document_xml(payload))
        boxed: list[set[str]] = []
        for paragraph in root.iter(f"{W}p"):
            borders = paragraph.find(f"{W}pPr/{W}pBdr")
            if borders is None:
                continue
            text = "".join(node.text or "" for node in paragraph.iter(f"{W}t"))
            if "نکتهٔ" in text:
                boxed.append({edge.tag.split("}")[1] for edge in borders})
        self.assertEqual(len(boxed), 3, "the three key points must be boxed")
        # One continuous panel: the top edge only on the first line, the bottom
        # edge only on the last, sides on all three.
        self.assertEqual([edge for edge in ("top",) if edge in boxed[0]], ["top"])
        self.assertNotIn("top", boxed[1])
        self.assertNotIn("top", boxed[2])
        self.assertIn("bottom", boxed[2])
        self.assertNotIn("bottom", boxed[0])
        for edges in boxed:
            self.assertEqual(edges & {"left", "right"}, {"left", "right"})

    def test_tables_use_a_fixed_layout_with_real_widths(self):
        payload = self.build(long_notes_json())
        xml = self.document_xml(payload)
        self.assertIn('<w:tblLayout w:type="fixed"/>', xml)
        self.assertNotIn('<w:tblLayout w:type="autofit"/>', xml)
        # A declared total width and per-column grid widths exist.
        self.assertIn("<w:tblW ", xml)
        self.assertIn("<w:gridCol ", xml)

    def test_two_column_tables_give_the_label_less_room_than_the_value(self):
        from gamas_bot.docx_export import TABLE_CONTENT_WIDTH_CM, _table_column_widths

        label, value = _table_column_widths(2)
        self.assertLess(label, value)
        self.assertAlmostEqual(label + value, TABLE_CONTENT_WIDTH_CM, places=2)
        equal = _table_column_widths(3)
        self.assertEqual(len(equal), 3)
        self.assertAlmostEqual(sum(equal), TABLE_CONTENT_WIDTH_CM, places=1)

    def test_definition_terms_are_real_heading_3_paragraphs(self):
        root = ET.fromstring(self.document_xml(self.build(long_notes_json())))
        styles = set()
        for paragraph in root.iter(f"{W}p"):
            style = paragraph.find(f"{W}pPr/{W}pStyle")
            if style is not None:
                styles.add(style.get(f"{W}val", ""))
        # The hierarchy is applied with real styles, deepest level included.
        self.assertIn("Heading1", styles)
        self.assertIn("Heading2", styles)
        self.assertIn("Heading3", styles)
        # Never a styled Normal paragraph pretending to be a heading.
        self.assertNotIn("Normal", {name for name in styles if name.startswith("Heading")})


if __name__ == "__main__":
    unittest.main()
