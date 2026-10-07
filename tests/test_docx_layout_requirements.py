"""Deterministic cover/TOC/body layout and the static (F9-free) table of contents.

These tests are the page-layout contract of the delivered Word file:

* page 1 is the branded cover, page 2 is the topic list, page 3 starts the notes;
* the topic list is ordinary, already-populated text — never a Word ``TOC``
  field and never something the reader has to refresh with F9;
* each topic row carries the measured page number and an internal hyperlink to
  the matching heading bookmark, including when two topics share a title.

The page numbers used here come from the same injection point the real
LibreOffice/PDF measurement uses (``_rendered_toc_page_numbers``), so the test
proves the numbers reach the document as ordinary visible text.
"""

from __future__ import annotations

import html
import io
import re
import unittest
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from gamas_bot.docx_export import DocumentMeta, build_notes_docx, resolve_design
from gamas_bot.progress import to_persian_digits
from gamas_bot.structuring import parse_structured_notes

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: ``<w:t>`` (or any prefixed spelling of it) in a document part.
_TEXT_NODE = re.compile(r"<[A-Za-z0-9_]+:t(?:\s[^<>]*)?>(.*?)</[A-Za-z0-9_]+:t>", re.S)

META = DocumentMeta(
    reference="GMS-000777",
    source_name="lecture.mp3",
    engine="deepgram",
    created_at=datetime(2026, 10, 6, 9, 0),
)


def notes_json(
    headings: list[str], *, summary: bool = False, glossary: bool = False
) -> str:
    """A valid note payload with one section per heading."""
    body = (
        "توضیح کامل و روشن این موضوع برای دانشجو، همراه با جزئیات کافی و "
        "مثال‌های عددی مانند ۵۰۰ mg و نسبت ۱۲۰/۸۰ و اصطلاح HbA1c."
    )
    sections = []
    for heading in headings:
        escaped = heading.replace('"', "'")
        sections.append(
            '{"heading": "%s", "paragraphs": ["%s"], "bullets": ["نکتهٔ مهم"], '
            '"key_points": ["جمع‌بندی"]}' % (escaped, body)
        )
    parts = ['"title": "جزوهٔ آزمون"']
    if summary:
        parts.append('"summary": "خلاصهٔ درس"')
    parts.append('"sections": [' + ", ".join(sections) + "]")
    if glossary:
        parts.append('"glossary": [{"term": "HbA1c", "definition": "هموگلوبین گلیکوزیله"}]')
    return "{" + ", ".join(parts) + "}"


class DocxLayoutTests(unittest.TestCase):
    #: A deliberately non-trivial page map: page numbers are neither the row
    #: index nor the page of the TOC, so a hardcoded number cannot pass.
    PAGE_STEP = 3
    FIRST_PAGE = 11

    def build(self, payload: str) -> bytes:
        design = replace(resolve_design(), toc_enabled=True)

        def pages(_payload, entries, **_kwargs):
            return [self.FIRST_PAGE + self.PAGE_STEP * index for index in range(len(entries))]

        with patch("gamas_bot.docx_export._rendered_toc_page_numbers", side_effect=pages):
            return build_notes_docx(parse_structured_notes(payload), meta=META, design=design)

    # -- helpers ----------------------------------------------------------
    def part(self, data: bytes, name: str) -> str:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.read(name).decode("utf-8")

    def root(self, data: bytes) -> ET.Element:
        return ET.fromstring(self.part(data, "word/document.xml"))

    def body_children(self, data: bytes) -> list[ET.Element]:
        return list(self.root(data).find(f"{W}body"))

    @staticmethod
    def paragraph_text(paragraph: ET.Element) -> str:
        return "".join(node.text or "" for node in paragraph.iter(f"{W}t"))

    @staticmethod
    def has_page_break(paragraph: ET.Element) -> bool:
        return any(br.get(f"{W}type") == "page" for br in paragraph.iter(f"{W}br"))

    def style_of(self, paragraph: ET.Element) -> str:
        style = paragraph.find(f"{W}pPr/{W}pStyle")
        return "" if style is None else style.get(f"{W}val", "")

    def sequence(self, data: bytes) -> list[tuple[str, str]]:
        """Ordered markers of the body as (kind, text)."""
        items: list[tuple[str, str]] = []
        for index, child in enumerate(self.body_children(data)):
            if child.tag == f"{W}tbl":
                items.append(("table", str(index)))
            elif child.tag == f"{W}p":
                if self.has_page_break(child):
                    items.append(("page-break", str(index)))
                text = self.paragraph_text(child).strip()
                if text:
                    items.append((f"p:{self.style_of(child)}", text))
        return items

    def toc_rows(self, data: bytes) -> list[tuple[ET.Element, str]]:
        """The static topic list rows: (row element, visible text)."""
        tables = list(self.root(data).iter(f"{W}tbl"))
        self.assertTrue(tables, "the document must contain the topic-list table")
        table = tables[0]
        return [
            (row, "".join(node.text or "" for node in row.iter(f"{W}t")))
            for row in table.iter(f"{W}tr")
        ]

    @staticmethod
    def visible_text(xml_text: str) -> str:
        """All text the reader actually sees (``w:t`` nodes), never XML attributes."""
        return " ".join(
            html.unescape(match) for match in _TEXT_NODE.findall(xml_text)
        )

    def heading_bookmarks(self, data: bytes, *, level: str) -> list[str]:
        """Bookmark names attached to paragraphs of one heading style."""
        names: list[str] = []
        for paragraph in self.root(data).iter(f"{W}p"):
            style = self.style_of(paragraph)
            if style != level:
                continue
            for node in paragraph.iter(f"{W}bookmarkStart"):
                if node.get(f"{W}name"):
                    names.append(node.get(f"{W}name"))
        return names

    def anchors(self, data: bytes) -> list[str]:
        return [
            node.get(f"{W}anchor")
            for node in self.root(data).iter(f"{W}hyperlink")
            if node.get(f"{W}anchor")
        ]

    # -- tests ------------------------------------------------------------
    def test_cover_toc_and_body_appear_in_that_order_with_page_breaks(self):
        data = self.build(notes_json(["موضوع نخست", "موضوع دوم", "موضوع سوم", "موضوع چهارم"]))
        items = self.sequence(data)
        cover_index = next(i for i, (_kind, text) in enumerate(items) if "به نام خدا" in text)
        toc_index = next(i for i, (_kind, text) in enumerate(items) if text == "فهرست مطالب")
        table_index = next(
            i for i, (kind, _text) in enumerate(items) if kind == "table" and i > toc_index
        )
        break_index = next(
            i
            for i, (kind, _text) in enumerate(items)
            if kind == "page-break" and i > table_index
        )
        first_topic_index = next(
            i
            for i, (kind, text) in enumerate(items)
            if kind.endswith("Heading1") and "موضوع نخست" in text
        )
        self.assertLess(cover_index, toc_index, "the cover must come before the topic list")
        self.assertLess(toc_index, table_index, "the topic list heading precedes its rows")
        self.assertLess(table_index, break_index, "the topic list ends with a page break")
        self.assertLess(break_index, first_topic_index, "the notes start after the topic list")
        # The cover is its own section, so no body content can leak onto page one.
        section_breaks = [
            child
            for child in self.body_children(data)
            if child.tag == f"{W}p" and child.find(f"{W}pPr/{W}sectPr") is not None
        ]
        self.assertTrue(section_breaks, "the cover must be a separate section")

    def test_topic_list_is_static_text_with_measured_page_numbers(self):
        data = self.build(notes_json(["موضوع نخست", "موضوع دوم", "موضوع سوم", "موضوع چهارم"]))
        rows = self.toc_rows(data)
        self.assertEqual(len(rows), 4)
        for index, (_row, text) in enumerate(rows):
            expected = to_persian_digits(str(self.FIRST_PAGE + self.PAGE_STEP * index))
            self.assertIn(expected, text)
        # Ordinary characters, not a field: no field instruction anywhere in the row.
        for row, _text in rows:
            xml = ET.tostring(row, encoding="unicode")
            self.assertNotIn("fldChar", xml)
            self.assertNotIn("instrText", xml)
        # Every topic title is visible in the list itself.
        combined = " ".join(text for _row, text in rows)
        for title in ["موضوع نخست", "موضوع دوم", "موضوع سوم", "موضوع چهارم"]:
            self.assertIn(title, combined)

    def test_topic_list_covers_topics_summary_and_glossary_headings(self):
        data = self.build(
            notes_json(
                ["موضوع نخست", "موضوع دوم", "موضوع سوم", "موضوع چهارم"],
                summary=True,
                glossary=True,
            )
        )
        rows = self.toc_rows(data)
        combined = " ".join(text for _row, text in rows)
        self.assertIn("موضوع نخست", combined)
        self.assertIn("خلاصه", combined)
        self.assertIn("واژه‌نامه", combined)
        # primary topics + summary + glossary
        self.assertEqual(len(rows), 6)

    def test_topic_list_contains_no_refresh_placeholder_or_toc_field(self):
        data = self.build(notes_json(["موضوع نخست", "موضوع دوم", "موضوع سوم", "موضوع چهارم"]))
        document = self.part(data, "word/document.xml")
        settings = self.part(data, "word/settings.xml")
        self.assertNotIn("w:instrText", document)
        self.assertNotIn("TOC \\o", document)
        self.assertNotIn("updateFields", settings)
        self.assertNotIn("F9", self.visible_text(document))
        self.assertNotIn("F9", self.visible_text(settings))
        # The footer keeps its real PAGE field: the topic list is static, the
        # footer pagination is not.
        footer = self.part(data, "word/footer1.xml")
        self.assertIn("PAGE", footer)
        self.assertIn("w:fldChar", footer)

    def test_all_visible_parts_are_free_of_the_f9_instruction(self):
        data = self.build(notes_json(["موضوع نخست", "موضوع دوم", "موضوع سوم", "موضوع چهارم"]))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                if not name.endswith(".xml"):
                    continue
                payload = archive.read(name).decode("utf-8", "replace")
                # No visible "press F9" instruction anywhere, and no field-refresh
                # request (which is the other way a document demands a keypress).
                self.assertNotIn("F9", self.visible_text(payload), name)
                self.assertNotIn("updateFields", payload, name)

    def test_duplicate_topic_titles_receive_unique_bookmarks(self):
        data = self.build(notes_json(["موضوع تکراری", "موضوع تکراری", "موضوع تکراری", "موضوع دیگر"]))
        root = self.root(data)
        bookmarks = [
            node.get(f"{W}name")
            for node in root.iter(f"{W}bookmarkStart")
            if node.get(f"{W}name")
        ]
        self.assertEqual(len(bookmarks), len(set(bookmarks)))
        top_level = self.heading_bookmarks(data, level="Heading1")
        self.assertEqual(len(top_level), 4, top_level)
        anchors = self.anchors(data)
        self.assertEqual(len(anchors), 4)
        self.assertEqual(len(set(anchors)), 4)
        self.assertTrue(set(anchors).issubset(set(top_level)))
        for anchor in anchors:
            self.assertRegex(anchor, r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
        # The three identical titles are three distinct destinations, reached by
        # three distinct links.
        duplicate_anchors = [anchor for anchor in anchors if anchor != anchors[3]]
        self.assertEqual(len(set(duplicate_anchors)), 3)
        self.assertEqual(len(anchors), 4)

    def test_twenty_topic_document_lists_every_topic_with_a_link(self):
        headings = [f"موضوع شمارهٔ {index}" for index in range(1, 21)]
        data = self.build(notes_json(headings))
        root = self.root(data)
        anchors = [
            node.get(f"{W}anchor")
            for node in root.iter(f"{W}hyperlink")
            if node.get(f"{W}anchor")
        ]
        self.assertEqual(len(anchors), 20)
        rows = self.toc_rows(data)
        self.assertEqual(len(rows), 20)
        combined = " ".join(text for _row, text in rows)
        for heading in headings:
            self.assertIn(heading, combined)
        for index in range(20):
            expected = to_persian_digits(str(self.FIRST_PAGE + self.PAGE_STEP * index))
            self.assertIn(expected, rows[index][1])

    def test_long_titles_do_not_break_the_topic_rows(self):
        long_title = "بررسی جامع و کامل سازوکارهای پیچیدهٔ تنظیم قند خون و پیامدهای بالینی آن"
        data = self.build(notes_json([long_title, "موضوع دوم", "موضوع سوم", "موضوع چهارم"]))
        rows = self.toc_rows(data)
        combined = " ".join(text for _row, text in rows)
        self.assertIn(long_title, combined)
        for row, _text in rows:
            self.assertEqual(len(list(row.iter(f"{W}tc"))), 2)

    def test_topic_rows_are_rtl_two_column_rows(self):
        data = self.build(notes_json(["موضوع نخست", "موضوع دوم", "موضوع سوم", "موضوع چهارم"]))
        table = list(self.root(data).iter(f"{W}tbl"))[0]
        self.assertIn("bidiVisual", ET.tostring(table, encoding="unicode"))
        paragraphs = list(table.iter(f"{W}p"))
        self.assertTrue(paragraphs)
        for paragraph in paragraphs:
            self.assertIsNotNone(
                paragraph.find(f"{W}pPr/{W}bidi"),
                "every topic row paragraph must be marked RTL",
            )
        # The page number lives in its own cell, so the title and the number are
        # never glued together by a fragile tab stop.
        for index, (row, _text) in enumerate(self.toc_rows(data)):
            cells = list(row.iter(f"{W}tc"))
            self.assertEqual(len(cells), 2)
            expected = to_persian_digits(str(self.FIRST_PAGE + self.PAGE_STEP * index))
            self.assertIn(expected, "".join(node.text or "" for node in cells[1].iter(f"{W}t")))


if __name__ == "__main__":
    unittest.main()
