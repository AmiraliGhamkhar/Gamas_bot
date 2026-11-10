"""Visual regression QA for generated booklet documents.

A successful ``build_notes_pdf`` call proves nothing about what the student
sees: broken glyph shaping, clipped tables or blank pages all "succeed". These
tests therefore inspect the *rendered* output at two levels:

* **Structural (always on, pypdf only)** — page count, A4 geometry, non-empty
  content streams, embedded Vazirmatn fonts, and the presence of expected
  headings in extracted text.
* **Rasterized (optional, PyMuPDF)** — when ``pymupdf`` is installed (it is a
  dev-only tool, not a runtime dependency), every page is rasterized and
  checked for ink coverage (no blank pages) and for text staying inside the
  page frame (no clipped or overflowing layout). Text-block bounding boxes are
  also checked for significant overlaps, which catches the classic
  "table swallowed the footer" class of defect.

Reference documents come from the curated note-quality corpus in
``tests/fixtures/notes``. The original classroom recording and the
hand-formatted reference booklet are **not** in the repository, so no
page-count or pixel-diff against a human-made PDF is claimed here; the
fixtures stand in as representative content (see docs/TESTING.md).
"""

from __future__ import annotations

import io
import json
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader

from gamas_bot.docx_export import DocumentMeta, build_notes_docx
from gamas_bot.notes import (
    GlossaryEntry,
    NoteCallout,
    NoteDefinition,
    NoteSection,
    NoteTable,
    StructuredNotes,
)
from gamas_bot.notes_pdf import build_notes_pdf

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "notes" / "references"
META = DocumentMeta(
    reference="GMS-QA",
    source_name="qa-lecture.mp3",
    created_at=datetime(2026, 11, 10, 9, 30, 0),
)

#: The PDF frame geometry from notes_pdf, in points (A4, 2.0 cm side and
#: bottom margins, 2.2 cm top margin) plus the decorative border rectangle
#: that the running header and footer live inside (20 pt page inset).
A4_WIDTH, A4_HEIGHT = 595.27, 841.89
MARGIN_LR = 56.69  # _MARGIN_LR = 2.0 cm
FRAME_TOP = 62.36  # _MARGIN_TOP = 2.2 cm
FRAME_BOTTOM = A4_HEIGHT - 56.69  # _MARGIN_BOTTOM = 2.0 cm
BORDER_INSET = 20.0  # decorative frame drawn by the on-page callback
TOLERANCE = 6.0


def _normalize(text: str) -> str:
    """Make extracted and source Persian comparable for substring checks.

    The PDF renderer pre-shapes text (arabic_reshaper), so extracted text
    arrives as presentation forms; NFKC folds those back to the logical
    alphabet. Extraction also drops ZWNJ and some combining marks (ٔ), and
    justified lines may gain tatweel — strip all of those from both sides.
    """
    import unicodedata

    folded = unicodedata.normalize("NFKC", text)
    dropped = "\u200c\u0640"  # ZWNJ, tatweel
    dropped += "".join(chr(cp) for cp in range(0x064B, 0x0660))  # Arabic marks
    return folded.translate(str.maketrans(dict.fromkeys(dropped)))

try:
    import pymupdf  # noqa: F401

    HAVE_PYMUPDF = True
except ImportError:  # pragma: no cover - depends on the dev environment
    HAVE_PYMUPDF = False


def fixture_notes() -> list[tuple[str, StructuredNotes]]:
    """Every curated reference document, as the pipeline would parse it."""
    documents: list[tuple[str, StructuredNotes]] = []
    if FIXTURES.is_dir():
        for path in sorted(FIXTURES.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            documents.append((path.stem, StructuredNotes.from_payload(payload)))
    return documents


def stress_notes() -> StructuredNotes:
    """A document built to break naive renderers, not to read nicely.

    * a table with 30 rows and long Persian/English cells (must span pages
      with a repeated header, without overflowing the frame);
    * mixed-direction lines (Persian text with embedded English terms);
    * every block type the schema supports, plus a long section chain.
    """
    rows = []
    for index in range(1, 31):
        rows.append(
            [
                f"پارامتر {index} از آزمون",
                f"Value {index} (range {index * 2}–{index * 3} mg/mL)",
                "توضیح " + "بلند " * (1 + index % 5),
            ]
        )
    sections = []
    for index in range(1, 9):
        sections.append(
            NoteSection(
                heading=f"بخش {index}: عنوان نسبتاً بلند برای آزمون شکستن سطر",
                paragraphs=(
                    "پاراگرافی با ترکیب فارسی و English terms که باید در یک سطر "
                    "با جهت‌دهی درست بیفتد و چیزی بریده نشود. " * 3,
                ),
                definitions=(
                    NoteDefinition(
                        "اصطلاح تخصصی",
                        "Special Term",
                        "تعریفی که چند سطر می‌شود و باید درست بسته شود تا از کادر بیرون نزند.",
                    ),
                ),
                bullets=("مورد فهرست با متن فارسی", "bullet with English tail"),
                examples=("مثال بالینی با اعداد: ۱۲۰/۸۰ mmHg و ۹۸.۶٪",),
                steps=("گام نخست", "گام دوم", "گام سوم"),
                formulas=("CI = Q / BSA", "GFR = (140 − age) × weight / 72 × SCr"),
                key_points=("نکتهٔ کلیدی این بخش باید در کادر بیاید.",),
                callouts=(NoteCallout("هشدار", "عدد را جابه‌جا نکنید."),),
                tables=(
                    NoteTable(
                        headers=["نام پارامتر", "مقدار/واحد", "توضیح"],
                        rows=rows,
                        caption="جدول بزرگ برای آزمون چندصفحه‌شدن",
                    ),
                ),
            )
        )
    return StructuredNotes(
        title="جزوهٔ آزمون استرس رندر",
        summary="سندی مصنوعی برای QA بصری؛ شامل جدول‌های چندصفحه‌ای و متن دوجهته.",
        sections=tuple(sections),
        key_points=tuple(f"نکتهٔ کلیدی {index}" for index in range(1, 9)),
        glossary=tuple(
            GlossaryEntry(f"واژهٔ {index}", f"تعریف واژهٔ {index} برای آزمون.", f"Term {index}")
            for index in range(1, 9)
        ),
        learning_objectives=("هدف آموزشی یک", "Objective two"),
        review_questions=("پرسش مرور یک؟",),
    )


def render(notes: StructuredNotes, **kwargs) -> bytes:
    return build_notes_pdf(notes, meta=META, **kwargs)


class PdfStructureTests(unittest.TestCase):
    """Always-on checks: geometry, fonts, extracted headings, no empty pages."""

    def _documents(self) -> list[tuple[str, StructuredNotes]]:
        documents = fixture_notes()
        documents.append(("stress_document", stress_notes()))
        self.assertTrue(documents, "no representative documents were found")
        return documents

    def test_every_representative_document_is_a_valid_multi_page_a4_pdf(self):
        for name, notes in self._documents():
            with self.subTest(document=name):
                payload = render(notes, duration_seconds=10_800, session_number=3)
                self.assertTrue(payload.startswith(b"%PDF-"), name)
                reader = PdfReader(io.BytesIO(payload))
                self.assertGreaterEqual(
                    len(reader.pages), 3, f"{name}: cover + body expected"
                )
                for page in reader.pages:
                    box = page.mediabox
                    self.assertAlmostEqual(float(box.width), A4_WIDTH, delta=1.0)
                    self.assertAlmostEqual(float(box.height), A4_HEIGHT, delta=1.0)
                    contents = page.get_contents()
                    self.assertIsNotNone(contents, f"{name}: empty page object")
                    self.assertTrue(
                        contents.get_data().strip(),
                        f"{name}: blank content stream",
                    )

    def test_expected_headings_survive_text_extraction(self):
        for name, notes in self._documents():
            with self.subTest(document=name):
                payload = render(notes)
                text = "\n".join(
                    page.extract_text() or "" for page in PdfReader(io.BytesIO(payload)).pages
                )
                text = _normalize(text or "")
                self.assertIn("خلاصه", text, f"{name}: summary heading missing")
                for section in notes.sections[:3]:
                    words = [
                        w for w in _normalize(section.heading).split() if len(w) > 2
                    ]
                    if words:
                        self.assertIn(
                            words[0], text, f"{name}: heading '{section.heading}' not found"
                        )

    def test_persian_text_is_present_not_replacement_glyphs(self):
        payload = render(stress_notes())
        text = "\n".join(
            page.extract_text() or "" for page in PdfReader(io.BytesIO(payload)).pages
        )
        self.assertNotIn("\ufffd", text, "replacement glyph in extracted text")
        persian_chars = [ch for ch in _normalize(text) if "\u0600" <= ch <= "\u06FF"]
        self.assertGreater(
            len(persian_chars), 200, "extracted text lost its Persian glyphs"
        )

    def test_vazirmatn_fonts_are_embedded(self):
        payload = render(stress_notes())
        reader = PdfReader(io.BytesIO(payload))
        fonts: set[tuple[str, str]] = set()
        for page in reader.pages:
            resources = page.get("/Resources") or {}
            for font in (resources.get("/Font") or {}).values():
                obj = font.get_object()
                fonts.add((str(obj.get("/BaseFont") or ""), str(obj.get("/Subtype") or "")))
        # reportlab subsets the TrueType faces (AAAAAA+Vazirmatn-…).
        self.assertTrue(
            any("Vazirmatn" in name and subtype == "/TrueType" for name, subtype in fonts),
            f"fonts found: {fonts}",
        )

    def test_step_connectors_draw_as_vectors_not_missing_glyphs(self):
        """Regression: the step arrow used a ↓ that Vazirmatn does not carry.

        The missing glyph extracted as NUL and drew as an empty box; the
        connector is now a canvas-drawn vector arrow.
        """
        payload = render(stress_notes())
        text = "\n".join(
            page.extract_text() or "" for page in PdfReader(io.BytesIO(payload)).pages
        )
        self.assertNotIn("\x00", text, "missing-glyph (NUL) in extracted text")
        from pypdf.generic import ContentStream

        reader = PdfReader(io.BytesIO(payload))
        for page in reader.pages:
            operators = {
                operand
                for operands, operator in ContentStream(page.get_contents(), reader).operations
                if operator == b"m"  # path moves: the vector arrow draws paths
                for operand in operands
            }
            self.assertTrue(operators, "no vector drawing operators on page")


@unittest.skipUnless(HAVE_PYMUPDF, "pymupdf is an optional dev-only rasterizer")
class PdfRasterizedTests(unittest.TestCase):
    """Image-level checks: ink coverage, frame containment, block overlaps."""

    #: Minimum share of non-white pixels for a body page (cover is sparser).
    MIN_INK_RATIO = 0.002

    def _pages(self, payload: bytes):
        import pymupdf

        document = pymupdf.open(stream=payload, filetype="pdf")
        try:
            for number in range(document.page_count):
                yield number, document.load_page(number)
        finally:
            document.close()

    def test_no_page_is_blank_and_text_stays_inside_the_frame(self):
        for name, notes in fixture_notes() + [("stress_document", stress_notes())]:
            with self.subTest(document=name):
                payload = render(notes)
                for number, page in self._pages(payload):
                    raster = page.get_pixmap(dpi=72)
                    samples = raster.samples
                    total = raster.width * raster.height
                    ink = sum(
                        1
                        for index in range(0, len(samples), raster.n)
                        if samples[index] < 245
                    )
                    self.assertGreater(
                        ink / total,
                        self.MIN_INK_RATIO,
                        f"{name} page {number + 1} looks blank",
                    )
                    # pymupdf measures from the top-left corner. Two design
                    # zones: the running header/footer are drawn inside the
                    # decorative border rectangle (20 pt page inset) but above
                    # and below the text frame, so vertical containment is
                    # checked against the border; everything else must respect
                    # the full content frame. Horizontally nothing ever crosses
                    # the side margins.
                    for block in page.get_text("blocks"):
                        x0, y0, x1, y1 = block[:4]
                        self.assertGreaterEqual(
                            x0, MARGIN_LR - TOLERANCE,
                            f"{name} page {number + 1}: text clipped at left edge",
                        )
                        self.assertLessEqual(
                            x1, A4_WIDTH - MARGIN_LR + TOLERANCE,
                            f"{name} page {number + 1}: text overflows right margin",
                        )
                        self.assertGreaterEqual(
                            y0, BORDER_INSET - TOLERANCE,
                            f"{name} page {number + 1}: text above the border frame",
                        )
                        self.assertLessEqual(
                            y1, A4_HEIGHT - BORDER_INSET + TOLERANCE,
                            f"{name} page {number + 1}: text below the border frame",
                        )
                        in_header_band = y1 < FRAME_TOP - TOLERANCE
                        in_footer_band = y0 > FRAME_BOTTOM + TOLERANCE
                        if in_header_band or in_footer_band:
                            continue
                        self.assertGreaterEqual(
                            y0, FRAME_TOP - TOLERANCE,
                            f"{name} page {number + 1}: body text above the frame",
                        )
                        self.assertLessEqual(
                            y1, FRAME_BOTTOM + TOLERANCE,
                            f"{name} page {number + 1}: body text below the frame "
                            "(footer collision?)",
                        )

    def _ink_boxes(self, page):
        """Tight per-span text boxes, shrunk to the plausible ink height.

        Vazirmatn declares a ~1.56 em line box, so the raw span bboxes of
        adjacent lines at a normal 1.2-1.3x leading overlap in font-metric
        space while the glyphs themselves never touch. Comparing boxes shrunk
        to ~1 em keeps the check honest: real collisions (a table printed over
        the footer, two flowables stacked on one spot) still overlap almost
        completely, wrapped paragraphs no longer false-positive.
        """
        boxes = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if not (span.get("text") or "").strip():
                        continue
                    x0, y0, x1, y1 = span["bbox"]
                    slack = max(0.0, (y1 - y0) - span["size"] * 1.0) / 2.0
                    boxes.append((x0, y0 + slack, x1, y1 - slack))
        return boxes

    def test_text_blocks_do_not_significantly_overlap(self):
        for name, notes in fixture_notes() + [("stress_document", stress_notes())]:
            with self.subTest(document=name):
                payload = render(notes)
                for number, page in self._pages(payload):
                    boxes = self._ink_boxes(page)
                    for i in range(len(boxes)):
                        for j in range(i + 1, len(boxes)):
                            a, b = boxes[i], boxes[j]
                            overlap_w = min(a[2], b[2]) - max(a[0], b[0])
                            overlap_h = min(a[3], b[3]) - max(a[1], b[1])
                            if overlap_w <= 0 or overlap_h <= 0:
                                continue
                            area_a = (a[2] - a[0]) * (a[3] - a[1])
                            area_b = (b[2] - b[0]) * (b[3] - b[1])
                            overlap = overlap_w * overlap_h
                            self.assertLess(
                                overlap / max(1, min(area_a, area_b)),
                                0.2,
                                f"{name} page {number + 1}: overlapping text blocks",
                            )


class DocxStructureTests(unittest.TestCase):
    """The editable Word export stays a valid, RTL, heading-structured file."""

    def _documents(self) -> list[tuple[str, StructuredNotes]]:
        documents = fixture_notes()
        documents.append(("stress_document", stress_notes()))
        return documents

    def test_docx_is_a_valid_package_with_rtl_and_headings(self):
        for name, notes in self._documents():
            with self.subTest(document=name):
                payload = build_notes_docx(notes, meta=META)
                self.assertTrue(payload.startswith(b"PK"), name)
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    document_xml = archive.read("word/document.xml").decode("utf-8")
                    styles = archive.read("word/styles.xml").decode("utf-8")
                self.assertIn("<w:bidi ", document_xml, f"{name}: RTL flag missing")
                self.assertIn("Vazirmatn", styles, f"{name}: Persian theme font missing")
                # Mixed-script headings are split across separate runs (a
                # Tahoma run for "Metformin", a Vazirmatn run for the Persian
                # tail), so match against the concatenated run text rather
                # than the raw XML.
                import html
                import re

                run_text = _normalize(
                    html.unescape(
                        "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", document_xml))
                    )
                )
                for section in notes.sections[:3]:
                    words = [
                        w for w in _normalize(section.heading).split() if len(w) > 2
                    ]
                    if words:
                        self.assertIn(
                            words[0], run_text, f"{name}: heading missing in DOCX"
                        )
                # The glossary renders as a table with an RTL column order.
                if notes.glossary:
                    self.assertIn("<w:tbl>", document_xml, f"{name}: glossary table missing")

    def test_docx_carries_the_edited_title_not_a_stale_one(self):
        notes = stress_notes()
        baseline = build_notes_docx(notes, meta=META)
        edited = StructuredNotes(
            title="عنوان کاملاً تازه",
            summary=notes.summary,
            sections=notes.sections[:2],
        )
        changed = build_notes_docx(edited, meta=META)
        self.assertNotEqual(baseline, changed)
        with zipfile.ZipFile(io.BytesIO(changed)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("عنوان کاملاً تازه", document_xml)


if __name__ == "__main__":
    unittest.main()
