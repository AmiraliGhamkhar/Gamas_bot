"""RTL Persian Word (.docx) and raw-text exporters for finished jobs.

The Word document is the polished deliverable: right-to-left paragraphs,
complex-script fonts, shaded summary/callout boxes, RTL tables with a coloured
header row, a Persian (Jalali) date line and page-number footers.  A plain
``build_plain_docx`` path keeps the same polished look when the note API was
unavailable and only raw material could be delivered.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .progress import to_persian_digits
from .structuring import NoteSection, StructuredNotes

logger = logging.getLogger(__name__)

ACCENT = RGBColor(0x1F, 0x38, 0x64)
ACCENT_HEX = "1F3864"
MUTED = RGBColor(0x59, 0x59, 0x59)
SUMMARY_SHADE = "EEF3FA"
KEYPOINT_SHADE = "E7F3E8"
CALLOUT_SHADES = {"هشدار": "FDE7E9", "یادآوری": "FFF6E0"}
CALLOUT_EMOJI = {"هشدار": "⚠️", "یادآوری": "🔔"}
DEFAULT_FONT = "Tahoma"

JALALI_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)

UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')
# Characters XML 1.0 (and therefore Word) cannot store. python-docx raises
# ValueError on them, which used to cost the user the whole Word document.
XML_INVALID_CHARS = re.compile("[\x00-\x08\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
SOFT_BREAK_CHARS = re.compile("[\x0b\x0c]")
MAX_FILENAME_TITLE_CHARS = 50


def xml_safe(text: str) -> str:
    """Drop XML-illegal characters; PowerPoint soft line breaks (VT) become spaces."""
    return XML_INVALID_CHARS.sub("", SOFT_BREAK_CHARS.sub(" ", text))


@dataclass(frozen=True, slots=True)
class DocumentMeta:
    """Job metadata rendered into the document header and the raw text file."""

    reference: str
    source_name: str | None = None
    engine: str | None = None
    created_at: datetime | None = None


def sanitize_filename_part(value: str, *, max_chars: int = MAX_FILENAME_TITLE_CHARS) -> str:
    """Turn arbitrary text (note titles, filenames) into a safe filename part."""
    cleaned = UNSAFE_FILENAME_CHARS.sub(" ", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:max_chars]


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert a Gregorian date to the Jalali (Solar Hijri) calendar."""
    g_day_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2, gm2, gd2 = gy - 1600, gm - 1, gd - 1
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    g_day_no += g_day_m[gm2]
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0):
        g_day_no += 1
    g_day_no += gd2

    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    if j_day_no < 186:
        jm, jd = 1 + j_day_no // 31, 1 + j_day_no % 31
    else:
        jm, jd = 7 + (j_day_no - 186) // 30, 1 + (j_day_no - 186) % 30
    return jy, jm, jd


def jalali_date(now: datetime) -> str:
    """Persian display date such as «۷ مهر ۱۴۰۴»."""
    jy, jm, jd = gregorian_to_jalali(now.year, now.month, now.day)
    return f"{to_persian_digits(jd)} {JALALI_MONTHS[jm - 1]} {to_persian_digits(jy)}"


# ---------------------------------------------------------------------------
# Low-level RTL helpers
# ---------------------------------------------------------------------------


def _enable_bidi(paragraph) -> None:
    """Mark the paragraph as right-to-left (``w:bidi``)."""
    p_pr = paragraph._p.get_or_add_pPr()
    if p_pr.find(qn("w:bidi")) is None:
        _insert_ppr_child(
            p_pr,
            OxmlElement("w:bidi"),
            ("w:spacing", "w:ind", "w:jc", "w:rPr", "w:sectPr"),
        )
    bidi = p_pr.find(qn("w:bidi"))
    bidi.set(qn("w:val"), "1")


def _shade_paragraph(paragraph, fill: str) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    _remove_ppr_child(p_pr, "w:shd")
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), fill)
    _insert_ppr_child(
        p_pr, shd, ("w:bidi", "w:spacing", "w:ind", "w:jc", "w:rPr", "w:sectPr")
    )


def _paragraph_borders(paragraph, color: str, *, size: str = "6") -> None:
    """Draw a full box border around a paragraph (callout/summary boxes)."""
    p_pr = paragraph._p.get_or_add_pPr()
    _remove_ppr_child(p_pr, "w:pBdr")
    borders = OxmlElement("w:pBdr")
    for edge in ("top", "left", "bottom", "right"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), size)
        element.set(qn("w:space"), "4")
        element.set(qn("w:color"), color)
        borders.append(element)
    _insert_ppr_child(
        p_pr, borders, ("w:shd", "w:bidi", "w:spacing", "w:ind", "w:jc", "w:rPr", "w:sectPr")
    )


def _insert_ppr_child(p_pr, element, successors: tuple[str, ...]) -> None:
    """Insert a pPr child before its first successor (OOXML is order-sensitive)."""
    for tag in successors:
        found = p_pr.find(qn(tag))
        if found is not None:
            found.addprevious(element)
            return
    p_pr.append(element)


def _remove_ppr_child(p_pr, tag: str) -> None:
    for found in p_pr.findall(qn(tag)):
        p_pr.remove(found)


def _remove_rpr_child(r_pr, tag: str) -> None:
    for found in r_pr.findall(qn(tag)):
        r_pr.remove(found)


def _style_run(
    run,
    *,
    font: str,
    size: float,
    bold: bool = False,
    color: RGBColor | None = None,
    italic: bool = False,
) -> None:
    run.font.name = font
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.rtl = True
    if color is not None:
        run.font.color.rgb = color
    # Complex-script face/weight/size: this is what actually renders Persian.
    r_pr = run._r.get_or_add_rPr()
    r_fonts = r_pr.get_or_add_rFonts()
    r_fonts.set(qn("w:cs"), font)
    sz = r_pr.find(qn("w:sz"))
    sz_cs = OxmlElement("w:szCs")
    sz_cs.set(qn("w:val"), str(int(size * 2)))
    if sz is not None:
        sz.addnext(sz_cs)
    else:
        r_pr.append(sz_cs)
    if bold:
        _remove_rpr_child(r_pr, "w:bCs")
        b_cs = OxmlElement("w:bCs")
        b_cs.set(qn("w:val"), "1")
        b = r_pr.find(qn("w:b"))
        if b is not None:
            b.addnext(b_cs)
        else:
            r_pr.append(b_cs)


def _add_rtl_paragraph(
    container,
    text: str = "",
    *,
    font: str,
    size: float = 11,
    bold: bool = False,
    color: RGBColor | None = None,
    italic: bool = False,
    align=WD_ALIGN_PARAGRAPH.JUSTIFY,
    space_after: float = 6,
    space_before: float = 0,
    line_spacing: float = 1.15,
) -> object:
    paragraph = container.add_paragraph()
    return _fill_paragraph(
        paragraph,
        text,
        font=font,
        size=size,
        bold=bold,
        color=color,
        italic=italic,
        align=align,
        space_after=space_after,
        space_before=space_before,
        line_spacing=line_spacing,
    )


def _fill_paragraph(
    paragraph,
    text: str,
    *,
    font: str,
    size: float = 11,
    bold: bool = False,
    color: RGBColor | None = None,
    italic: bool = False,
    align=WD_ALIGN_PARAGRAPH.JUSTIFY,
    space_after: float = 6,
    space_before: float = 0,
    line_spacing: float = 1.15,
) -> object:
    """Apply RTL styling to an existing (possibly pre-created) paragraph."""
    paragraph.alignment = align
    _enable_bidi(paragraph)
    paragraph.paragraph_format.space_after = Pt(space_after)
    paragraph.paragraph_format.space_before = Pt(space_before)
    paragraph.paragraph_format.line_spacing = line_spacing
    text = xml_safe(text)
    if text:
        run = paragraph.add_run(text)
        _style_run(run, font=font, size=size, bold=bold, color=color, italic=italic)
    return paragraph


def _add_page_number_footer(section, *, font: str) -> None:
    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    _style_run(run, font=font, size=9, color=MUTED)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instruction)
    run._r.append(end)


def _setup_document(meta: DocumentMeta, title: str, *, font: str) -> tuple[Document, object]:
    document = Document()
    # A4 with comfortable Persian-document margins.
    section = document.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.4)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(2.2)

    normal = document.styles["Normal"]
    normal.font.name = font
    normal.font.size = Pt(11)
    normal_r_pr = normal.element.get_or_add_rPr()
    normal_r_fonts = normal_r_pr.get_or_add_rFonts()
    normal_r_fonts.set(qn("w:cs"), font)

    document.core_properties.title = xml_safe(title)
    document.core_properties.author = "Gamas Bot"
    document.core_properties.comments = meta.reference
    _add_page_number_footer(section, font=font)
    return document, section


def _meta_line(meta: DocumentMeta) -> str:
    created = meta.created_at or datetime.now()
    parts = [f"تاریخ: {jalali_date(created)}"]
    if meta.source_name:
        parts.append(f"منبع: {sanitize_filename_part(meta.source_name, max_chars=40)}")
    if meta.engine:
        parts.append(f"موتور تبدیل گفتار: {meta.engine}")
    parts.append(f"کد پیگیری: {meta.reference}")
    return " • ".join(parts)


def _add_title_block(document, title: str, meta: DocumentMeta, *, font: str) -> None:
    _add_rtl_paragraph(
        document,
        title,
        font=font,
        size=20,
        bold=True,
        color=ACCENT,
        align=WD_ALIGN_PARAGRAPH.CENTER,
        space_after=4,
        line_spacing=1.1,
    )
    _add_rtl_paragraph(
        document,
        _meta_line(meta),
        font=font,
        size=9,
        color=MUTED,
        align=WD_ALIGN_PARAGRAPH.CENTER,
        space_after=10,
    )
    divider = _add_rtl_paragraph(
        document, "", font=font, size=2, space_after=12, line_spacing=1.0
    )
    _paragraph_borders(divider, ACCENT_HEX, size="12")


def _add_heading(document, text: str, *, font: str, size: float = 14, space_before: float = 14) -> None:
    _add_rtl_paragraph(
        document,
        text,
        font=font,
        size=size,
        bold=True,
        color=ACCENT,
        align=WD_ALIGN_PARAGRAPH.RIGHT,
        space_after=6,
        space_before=space_before,
    )


def _add_boxed_lines(
    document,
    label: str,
    lines: list[str],
    *,
    font: str,
    fill: str,
    marker: str = "✦",
) -> None:
    """A shaded, bordered box of marked lines with a bold label."""
    _add_rtl_paragraph(
        document,
        label,
        font=font,
        size=11.5,
        bold=True,
        align=WD_ALIGN_PARAGRAPH.RIGHT,
        space_after=2,
    )
    for index, line in enumerate(lines):
        paragraph = _add_rtl_paragraph(
            document,
            f"{marker} {line}",
            font=font,
            size=11,
            align=WD_ALIGN_PARAGRAPH.RIGHT,
            space_after=2 if index < len(lines) - 1 else 8,
        )
        paragraph.paragraph_format.right_indent = Cm(0.35)
        _shade_paragraph(paragraph, fill)
        _paragraph_borders(paragraph, fill)


def _add_table(document, table_data, *, font: str) -> None:
    headers = table_data.headers
    rows = table_data.rows
    table = document.add_table(rows=len(rows) + 1, cols=len(headers))
    table.style = "Table Grid"
    # RTL column order: the first logical column renders on the right.
    tbl_pr = table._tbl.tblPr
    bidi_visual = OxmlElement("w:bidiVisual")
    tbl_pr.append(bidi_visual)

    for column, header in enumerate(headers):
        cell = table.cell(0, column)
        paragraph = _fill_paragraph(
            cell.paragraphs[0],
            header,
            font=font,
            size=10.5,
            bold=True,
            align=WD_ALIGN_PARAGRAPH.CENTER,
            space_after=0,
        )
        _shade_paragraph(paragraph, ACCENT_HEX)
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:fill"), ACCENT_HEX)
        tc_pr.append(shd)
    for row_index, row in enumerate(rows, start=1):
        for column, value in enumerate(row):
            cell = table.cell(row_index, column)
            _fill_paragraph(
                cell.paragraphs[0],
                value,
                font=font,
                size=10.5,
                align=WD_ALIGN_PARAGRAPH.CENTER,
                space_after=0,
            )
    _add_rtl_paragraph(document, "", font=font, size=4, space_after=6, line_spacing=1.0)


def _add_callout(document, callout, *, font: str) -> None:
    fill = CALLOUT_SHADES.get(callout.kind, "FFF6E0")
    emoji = CALLOUT_EMOJI.get(callout.kind, "💡")
    paragraph = _add_rtl_paragraph(
        document,
        f"{emoji} {callout.kind}: {callout.text}",
        font=font,
        size=11,
        align=WD_ALIGN_PARAGRAPH.RIGHT,
        space_after=6,
    )
    _shade_paragraph(paragraph, fill)
    _paragraph_borders(paragraph, fill)


def _add_section(document, index: int, section: NoteSection, *, font: str) -> None:
    _add_heading(document, f"{to_persian_digits(index)}. {section.heading}", font=font)
    for paragraph_text in section.paragraphs:
        _add_rtl_paragraph(document, paragraph_text, font=font)
    for bullet in section.bullets:
        paragraph = _add_rtl_paragraph(
            document, f"• {bullet}", font=font, align=WD_ALIGN_PARAGRAPH.RIGHT
        )
        paragraph.paragraph_format.right_indent = Cm(0.35)
    if section.key_points:
        _add_boxed_lines(
            document,
            "نکته‌های کلیدی این بخش",
            list(section.key_points),
            font=font,
            fill=KEYPOINT_SHADE,
        )
    for callout in section.callouts:
        _add_callout(document, callout, font=font)
    if section.table is not None:
        _add_table(document, section.table, font=font)


def build_notes_docx(notes: StructuredNotes, *, font: str = DEFAULT_FONT, meta: DocumentMeta) -> bytes:
    """Render validated structured notes as a polished RTL Word document."""
    document, _section = _setup_document(meta, notes.display_title, font=font)
    _add_title_block(document, notes.display_title, meta, font=font)

    if notes.summary:
        _add_heading(document, "✨ خلاصه", font=font, space_before=4)
        paragraph = _add_rtl_paragraph(document, notes.summary, font=font, size=11.5, space_after=10)
        _shade_paragraph(paragraph, SUMMARY_SHADE)
        _paragraph_borders(paragraph, SUMMARY_SHADE)

    for index, section in enumerate(notes.sections, start=1):
        _add_section(document, index, section, font=font)

    if notes.key_points:
        _add_heading(document, "💡 نکته‌های کلیدی", font=font)
        _add_boxed_lines(document, "مهم‌ترین نکته‌های این جزوه", list(notes.key_points), font=font, fill=KEYPOINT_SHADE)

    if notes.glossary:
        _add_heading(document, "📖 واژه‌نامه", font=font)
        for entry in notes.glossary:
            paragraph = _add_rtl_paragraph(
                document, "", font=font, space_after=4
            )
            term_run = paragraph.add_run(xml_safe(f"{entry.term}: "))
            _style_run(term_run, font=font, size=11, bold=True, color=ACCENT)
            definition_run = paragraph.add_run(xml_safe(entry.definition))
            _style_run(definition_run, font=font, size=11)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def build_plain_docx(
    title: str, text: str, *, font: str = DEFAULT_FONT, meta: DocumentMeta
) -> bytes:
    """Polished RTL Word document built from raw/fallback material.

    Understands the light Markdown the pipeline itself emits (``#`` headings,
    ``-`` bullets, ``## بخش n`` part headers); everything else is a paragraph.
    """
    document, _section = _setup_document(meta, title, font=font)
    _add_title_block(document, title, meta, font=font)
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if heading:
            _add_heading(document, heading.group(1).strip(), font=font, size=13)
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if bullet:
            paragraph = _add_rtl_paragraph(
                document, f"• {bullet.group(1).strip()}", font=font, align=WD_ALIGN_PARAGRAPH.RIGHT
            )
            paragraph.paragraph_format.right_indent = Cm(0.35)
            continue
        _add_rtl_paragraph(document, line.strip(), font=font)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def build_raw_text_document(
    *, title: str, sections: list[tuple[str, str]], meta: DocumentMeta
) -> str:
    """The companion .txt file: labelled raw texts exactly as extracted."""
    created = meta.created_at or datetime.now()
    lines = [title, "=" * 48, _meta_line(meta), "زمان تهیه: " + created.strftime("%Y-%m-%d %H:%M")]
    for heading, body in sections:
        content = body.strip()
        if not content:
            continue
        lines.append("")
        lines.append("")
        lines.append(heading)
        lines.append("-" * min(len(heading), 48))
        lines.append("")
        lines.append(content)
    lines.append("")
    return "\n".join(lines)


def notes_docx_filename(notes: StructuredNotes, reference: str) -> str:
    title_part = sanitize_filename_part(notes.display_title)
    name = f"جزوه - {title_part}" if title_part else "جزوه"
    return f"{name} - {reference}.docx"


def plain_docx_filename(title: str, reference: str) -> str:
    title_part = sanitize_filename_part(title)
    name = f"جزوه - {title_part}" if title_part else "جزوه"
    return f"{name} - {reference}.docx"


def raw_text_filename(reference: str) -> str:
    return f"متن خام - {reference}.txt"
