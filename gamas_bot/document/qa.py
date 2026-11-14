"""Automated quality checks for a rendered booklet.

A renderer that returns bytes has proved nothing. These checks inspect the
*deliverable*:

* **PDF** — page count, A4 geometry, non-empty content streams, embedded
  Vazirmatn, blank pages (ink coverage), text outside the content frame, and
  significant text-block overlaps. Raster/layout checks need ``pymupdf``, an
  optional dev tool; without it the structural checks still run and the report
  says which ones were skipped.
* **DOCX** — an intact OOXML package, embedded artwork, RTL/heading structure,
  the live page-number field, and that the design tokens actually reached the
  file.

Nothing here fails a job. It returns findings; the caller decides. The booklet
pipeline's own gate remains :mod:`gamas_bot.artifact_validation`.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .assets import validate_manifest
from .layout import pdf_frame
from .theme import THEME

#: Word's drawing namespace, used to count embedded pictures.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True, slots=True)
class PageQuality:
    """One rasterized page's findings."""

    number: int
    ink_ratio: float
    blank: bool
    out_of_frame: int = 0
    overlaps: int = 0


@dataclass(frozen=True, slots=True)
class PdfQuality:
    """What inspecting one PDF established."""

    pages: int
    fonts: tuple[str, ...]
    page_reports: tuple[PageQuality, ...] = ()
    issues: tuple[str, ...] = ()
    rasterized: bool = False

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class DocxQuality:
    """What inspecting one DOCX established."""

    paragraphs: int
    tables: int
    headings: int
    media: int
    fonts: tuple[str, ...]
    issues: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.issues


def _pymupdf():
    try:
        import pymupdf

        return pymupdf
    except ImportError:  # pragma: no cover - depends on the dev environment
        return None


def rasterize_pdf(pdf_bytes: bytes, *, dpi: int = 110) -> list[tuple[int, bytes]]:
    """Page PNGs as ``(page_number, png_bytes)``; empty without pymupdf."""
    module = _pymupdf()
    if module is None:
        return []
    document = module.open(stream=pdf_bytes, filetype="pdf")
    try:
        pages: list[tuple[int, bytes]] = []
        for number in range(document.page_count):
            pixmap = document.load_page(number).get_pixmap(dpi=dpi)
            pages.append((number + 1, pixmap.tobytes("png")))
        return pages
    finally:
        document.close()


def write_pdf_pages(pdf_bytes: bytes, directory: Path, *, prefix: str = "page",
                    dpi: int = 110) -> list[Path]:
    """Write page PNGs into ``directory`` and return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for number, payload in rasterize_pdf(pdf_bytes, dpi=dpi):
        path = directory / f"{prefix}-{number:02d}.png"
        path.write_bytes(payload)
        written.append(path)
    return written


def _frame_issues(page, number: int, tolerance: float = 6.0) -> tuple[int, int]:
    """Count text blocks outside the frame and significant span overlaps."""
    frame = pdf_frame()
    width, height = frame.page_width, frame.page_height
    border = frame.frame_inset
    frame_top = frame.margin_top
    frame_bottom = height - frame.margin_bottom
    outside = 0
    for block in page.get_text("blocks"):
        x0, y0, x1, y1 = block[:4]
        if (
            x0 < frame.margin_lr - tolerance
            or x1 > width - frame.margin_lr + tolerance
            or y0 < border - tolerance
            or y1 > height - border + tolerance
        ):
            outside += 1
            continue
        in_header = y1 < frame_top - tolerance
        in_footer = y0 > frame_bottom + tolerance
        if in_header or in_footer:
            continue
        if y0 < frame_top - tolerance or y1 > frame_bottom + tolerance:
            outside += 1

    boxes = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if not (span.get("text") or "").strip():
                    continue
                sx0, sy0, sx1, sy1 = span["bbox"]
                slack = max(0.0, (sy1 - sy0) - span["size"]) / 2.0
                boxes.append((sx0, sy0 + slack, sx1, sy1 - slack))
    overlaps = 0
    for index, first in enumerate(boxes):
        for second in boxes[index + 1 :]:
            overlap_w = min(first[2], second[2]) - max(first[0], second[0])
            overlap_h = min(first[3], second[3]) - max(first[1], second[1])
            if overlap_w <= 0 or overlap_h <= 0:
                continue
            area_first = (first[2] - first[0]) * (first[3] - first[1])
            area_second = (second[2] - second[0]) * (second[3] - second[1])
            if overlap_w * overlap_h / max(1.0, min(area_first, area_second)) > 0.2:
                overlaps += 1
    return outside, overlaps


def inspect_pdf(pdf_bytes: bytes, *, min_ink: float = 0.002, dpi: int = 80) -> PdfQuality:
    """Structural + (when possible) rasterized checks for a booklet PDF."""
    from pypdf import PdfReader

    issues: list[str] = []
    fonts: set[str] = set()
    page_reports: list[PageQuality] = []
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    pages = len(reader.pages)
    if pages < 2:
        issues.append("a booklet needs at least a cover and one body page")
    frame = pdf_frame()
    for index, page in enumerate(reader.pages, start=1):
        box = page.mediabox
        if abs(float(box.width) - frame.page_width) > 1.0 or abs(float(box.height) - frame.page_height) > 1.0:
            issues.append(f"page {index} is not A4")
        contents = page.get_contents()
        if contents is None or not contents.get_data().strip():
            issues.append(f"page {index} has an empty content stream")
        resources = page.get("/Resources") or {}
        for font in (resources.get("/Font") or {}).values():
            base = str(font.get_object().get("/BaseFont") or "")
            if base:
                fonts.add(base)
    if not any("Vazirmatn" in name for name in fonts):
        issues.append("the Persian face Vazirmatn is not embedded")

    module = _pymupdf()
    rasterized = module is not None
    if module is not None:
        document = module.open(stream=pdf_bytes, filetype="pdf")
        try:
            for number in range(document.page_count):
                page = document.load_page(number)
                pixmap = page.get_pixmap(dpi=dpi)
                total = pixmap.width * pixmap.height
                samples = pixmap.samples
                ink = sum(
                    1 for offset in range(0, len(samples), pixmap.n)
                    if samples[offset] < 245
                )
                ratio = ink / total if total else 0.0
                outside, overlaps = _frame_issues(page, number + 1)
                blank = ratio < min_ink
                page_reports.append(
                    PageQuality(number + 1, round(ratio, 4), blank, outside, overlaps)
                )
                if blank:
                    issues.append(f"page {number + 1} is blank")
                if outside:
                    issues.append(f"page {number + 1} has {outside} block(s) outside the frame")
                if overlaps:
                    issues.append(f"page {number + 1} has {overlaps} overlapping text block(s)")
        finally:
            document.close()
    return PdfQuality(
        pages=pages,
        fonts=tuple(sorted(fonts)),
        page_reports=tuple(page_reports),
        issues=tuple(issues),
        rasterized=rasterized,
    )


def inspect_docx(docx_bytes: bytes) -> DocxQuality:
    """Structure, artwork and design-token checks for a booklet DOCX."""
    issues: list[str] = []
    notes: list[str] = []
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
        names = set(archive.namelist())
        required = {"[Content_Types].xml", "word/document.xml", "word/styles.xml"}
        missing = required - names
        if missing:
            issues.append("DOCX package is missing " + ", ".join(sorted(missing)))
        media = sorted(name for name in names if name.startswith("word/media/"))
        if not media:
            issues.append("no artwork is embedded in the cover")
        document_xml = archive.read("word/document.xml").decode("utf-8") if (
            "word/document.xml" in names
        ) else ""
        styles_xml = archive.read("word/styles.xml").decode("utf-8") if (
            "word/styles.xml" in names
        ) else ""
        footers = "".join(
            archive.read(name).decode("utf-8", "ignore")
            for name in names
            if re.fullmatch(r"word/footer\d*\.xml", name)
        )

    if 'w:cs="Vazirmatn"' not in document_xml and 'w:cs="Vazirmatn"' not in styles_xml:
        issues.append("the Persian complex-script font is not applied")
    if '<w:bidi w:val="1"/>' not in document_xml and "<w:bidi/>" not in document_xml:
        issues.append("no right-to-left paragraph is declared")
    if "PAGE" not in footers:
        issues.append("the footer page-number field is missing")
    navy = THEME.ooxml(THEME.palette.navy_900)
    if navy not in document_xml:
        issues.append("the Gamas navy never reaches the document body")
    if THEME.ooxml(THEME.palette.cream_100) not in document_xml:
        notes.append("the cream panel tone was not used on this document")

    headings = len(re.findall(r'w:val="Heading[123]"', document_xml))
    if headings == 0:
        issues.append("no Word heading styles were applied")
    tables = len(re.findall(r"<w:tbl>", document_xml))
    paragraphs = len(re.findall(r"<w:p[ >]", document_xml))
    media_dimensions = _media_dimensions(docx_bytes)
    if media_dimensions and min(min(width, height) for width, height in media_dimensions) < 200:
        issues.append("an embedded asset is too small to print")
    return DocxQuality(
        paragraphs=paragraphs,
        tables=tables,
        headings=headings,
        media=len(media),
        fonts=("Vazirmatn",) if "Vazirmatn" in styles_xml else (),
        issues=tuple(issues),
        notes=tuple(notes),
    )


def _media_dimensions(docx_bytes: bytes) -> list[tuple[int, int]]:
    """``(width, height)`` of every embedded raster image, via Pillow."""
    from PIL import Image

    dimensions: list[tuple[int, int]] = []
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
        for name in archive.namelist():
            if not name.startswith("word/media/"):
                continue
            try:
                with Image.open(io.BytesIO(archive.read(name))) as image:
                    dimensions.append(image.size)
            except Exception:  # noqa: BLE001 - a vector/EMF has no raster size
                continue
    return dimensions


def validate_assets() -> tuple[str, ...]:
    """Manifest/file problems (thin wrapper so QA has one entry point)."""
    return tuple(validate_manifest())


__all__ = [
    "DocxQuality",
    "PageQuality",
    "PdfQuality",
    "inspect_docx",
    "inspect_pdf",
    "rasterize_pdf",
    "validate_assets",
    "write_pdf_pages",
]
