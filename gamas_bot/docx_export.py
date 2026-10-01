"""RTL Persian Word (.docx) and raw-text exporters for finished jobs.

The Word document is the polished deliverable: right-to-left paragraphs,
per-direction runs (Persian text and embedded English terms keep their own
direction and font), complex-script fonts with document-level fallbacks,
shaded summary/callout boxes, RTL tables with a coloured header row, a
Persian (Jalali) date line, a document header and page-number footers.
A plain ``build_plain_docx`` path keeps the same polished look when the note
API was unavailable and only raw material could be delivered.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .bidi import TextRun, split_direction_runs
from .progress import to_persian_digits
from .structuring import NoteSection, StructuredNotes
from .textnorm import normalize_display

logger = logging.getLogger(__name__)

ACCENT = RGBColor(0x1F, 0x38, 0x64)
ACCENT_HEX = "1F3864"
MUTED = RGBColor(0x59, 0x59, 0x59)
SUMMARY_SHADE = "EEF3FA"
KEYPOINT_SHADE = "E7F3E8"
DEFINITION_SHADE = "F5F0FA"
CALLOUT_SHADES = {"هشدار": "FDE7E9", "یادآوری": "FFF6E0"}
CALLOUT_EMOJI = {"هشدار": "⚠️", "یادآوری": "🔔"}
DEFAULT_FONT = "Tahoma"

#: Document defaults per font role. ``body`` is the workhorse face; ``heading``
#: may differ (e.g. a display Persian font); ``latin`` renders embedded English
#: terms; ``fallback`` is written into word/fontTable.xml as w:altName so a
#: reader without the primary Persian font substitutes gracefully.
DEFAULT_FONT_CONFIG = {
    "body": "Tahoma",
    "heading": "Tahoma",
    "latin": "Tahoma",
    "fallback": "Tahoma",
}
FONT_ROLES = tuple(DEFAULT_FONT_CONFIG)

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

#: Footer/header caption added next to the page number.
PAGE_HEADER_TEXT = "جزوهٔ درسی — Gamas Bot"


def xml_safe(text: str) -> str:
    """Prepare text for storage in a .docx part.

    Three deterministic steps, in this order:

    1. PowerPoint soft line breaks (VT/FF) become ordinary spaces — they are
       legal in a transcript but would break a Word paragraph;
    2. characters XML 1.0 cannot store are dropped, because python-docx raises
       ``ValueError`` on them and that used to cost the user the whole document;
    3. Persian letter/whitespace normalization folds the Arabic KAF/YEH that a
       second keyboard or an STT engine emits to their Persian equivalents
       (IANA fa-IR table), so one document is not a mix of two alphabets.

    Normalization is idempotent and never touches ZWNJ, Latin text, formulas,
    URLs or digit values — see :mod:`gamas_bot.textnorm`.
    """
    return normalize_display(XML_INVALID_CHARS.sub("", SOFT_BREAK_CHARS.sub(" ", text)))


@dataclass(frozen=True, slots=True)
class DocumentFonts:
    """Resolved font faces for one document (see ``resolve_fonts``)."""

    body: str
    heading: str
    latin: str
    fallback: str


def resolve_fonts(font_config: dict | None = None, *, font: str | None = None) -> DocumentFonts:
    """Normalise a font configuration into a complete role set.

    ``font=`` keeps the historical single-font API working: it fills every
    role with one face. Keys may also be prefixed (``body_font``, ...), which
    is how :mod:`gamas_bot.config` passes environment values.
    """
    config = dict(font_config or {})
    if font is not None:
        config.setdefault("body", font)
        config.setdefault("heading", font)
        config.setdefault("latin", font)
        config.setdefault("fallback", font)
    resolved = {role: (config.get(role) or "").strip() or DEFAULT_FONT_CONFIG[role] for role in FONT_ROLES}
    return DocumentFonts(**resolved)


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
    # Truncating can expose a trailing space or dot ("title …" -> "title "), which
    # Telegram and desktop file managers strip or turn into a confusing extension.
    return cleaned[:max_chars].strip(" .")


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


#: Elements that must follow ``w:keepNext``/``w:keepLines`` inside ``w:pPr``
#: (CT_PPrBase order). OOXML is order-sensitive and Word rejects a document
#: whose paragraph properties are out of sequence.
PPR_SUCCESSORS = (
    "w:pageBreakBefore", "w:framePr", "w:widowControl", "w:numPr",
    "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs", "w:suppressAutoHyphens",
    "w:kinsoku", "w:wordWrap", "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE",
    "w:autoSpaceDN", "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing",
    "w:ind", "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap",
    "w:jc", "w:textDirection", "w:textAlignment", "w:textboxTightWrap",
    "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr",
)


def _keep_with_next(paragraph, *, keep_lines: bool = True) -> None:
    """Stop Word from leaving a heading (or its first line) alone at a page foot."""
    p_pr = paragraph._p.get_or_add_pPr()
    _remove_ppr_child(p_pr, "w:keepNext")
    _insert_ppr_child(
        p_pr, OxmlElement("w:keepNext"), ("w:keepLines",) + PPR_SUCCESSORS
    )
    if keep_lines:
        _remove_ppr_child(p_pr, "w:keepLines")
        _insert_ppr_child(p_pr, OxmlElement("w:keepLines"), PPR_SUCCESSORS)


def _repeat_table_header(row) -> None:
    """Mark a table row as a header that repeats on every page.

    Without ``w:tblHeader`` a table split across pages loses its column
    headings on the second and later pages, which makes wide Persian tables
    unreadable.
    """
    tr_pr = row._tr.get_or_add_trPr()
    for found in tr_pr.findall(qn("w:tblHeader")):
        tr_pr.remove(found)
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)
    cant_split = OxmlElement("w:cantSplit")
    tr_pr.insert(0, cant_split)


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
    rtl: bool = True,
) -> None:
    """Style one run with the correct direction and font slots.

    ``rtl=True`` sets ``w:rtl`` (complex-script formatting, ECMA-376 §17.3.2.30)
    and fills the ``w:cs`` font slot; ``rtl=False`` explicitly marks the run
    LTR (``w:rtl w:val="0"``) so embedded English keeps Latin rendering, and
    fills the ``w:ascii``/``w:hAnsi`` slots instead.
    """
    run.font.name = font  # w:ascii + w:hAnsi
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.rtl = rtl
    if color is not None:
        run.font.color.rgb = color
    r_pr = run._r.get_or_add_rPr()
    r_fonts = r_pr.get_or_add_rFonts()
    if rtl:
        # Complex-script face/weight/size: this is what actually renders Persian.
        r_fonts.set(qn("w:cs"), font)
        sz = r_pr.find(qn("w:sz"))
        _remove_rpr_child(r_pr, "w:szCs")
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
    else:
        # A declared Persian fallback keeps Latin readers on a sane face when
        # the configured Latin font is missing, without ever reversing text.
        if font != DEFAULT_FONT_CONFIG["latin"]:
            r_fonts.set(qn("w:cs"), font)


def _add_directional_text(paragraph, text: str, *, fonts: DocumentFonts, size: float,
                          bold: bool = False, color: RGBColor | None = None,
                          italic: bool = False, rtl_bold: bool | None = None) -> None:
    """Append ``text`` to ``paragraph`` as one run per direction segment.

    This is the single funnel for every run in a document body, so the text is
    passed through :func:`xml_safe` here (XML-illegal characters removed,
    Persian letter variants folded). Doing it here rather than in each caller
    means a block added by a new code path cannot reintroduce mixed alphabets
    or characters Word would reject.
    """
    text = xml_safe(text)
    runs: list[TextRun] = split_direction_runs(text)
    if not runs:
        return
    for run in runs:
        styled = paragraph.add_run(run.text)
        if run.rtl:
            _style_run(
                styled,
                font=fonts.body,
                size=size,
                bold=bold if rtl_bold is None else rtl_bold,
                color=color,
                italic=italic,
                rtl=True,
            )
        else:
            _style_run(
                styled,
                font=fonts.latin,
                size=size,
                bold=bold,
                color=color,
                italic=italic,
                rtl=False,
            )


def _add_rtl_paragraph(
    container,
    text: str = "",
    *,
    fonts: DocumentFonts,
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
        fonts=fonts,
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
    fonts: DocumentFonts,
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
        _add_directional_text(
            paragraph, text, fonts=fonts, size=size, bold=bold, color=color, italic=italic
        )
    return paragraph


def _add_page_number_footer(section, *, fonts: DocumentFonts) -> None:
    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    _style_run(run, font=fonts.body, size=9, color=MUTED)
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


def _add_document_header(section, *, fonts: DocumentFonts, title: str) -> None:
    """A small running header: document title on one side, brand on the other."""
    paragraph = section.header.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _enable_bidi(paragraph)
    paragraph.paragraph_format.space_after = Pt(2)
    _add_directional_text(
        paragraph,
        f"{xml_safe(title)} | {PAGE_HEADER_TEXT}",
        fonts=fonts,
        size=8.5,
        color=MUTED,
    )
    p_pr = paragraph._p.get_or_add_pPr()
    _remove_ppr_child(p_pr, "w:pBdr")
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), ACCENT_HEX)
    borders.append(bottom)
    _insert_ppr_child(p_pr, borders, ("w:shd", "w:bidi", "w:spacing", "w:ind", "w:jc", "w:rPr", "w:sectPr"))


def _apply_document_defaults(document, fonts: DocumentFonts) -> None:
    """Set the Normal style: Persian via w:cs, Latin via w:ascii/w:hAnsi."""
    normal = document.styles["Normal"]
    normal.font.name = fonts.latin
    normal.font.size = Pt(11)
    r_pr = normal.element.get_or_add_rPr()
    r_fonts = r_pr.get_or_add_rFonts()
    r_fonts.set(qn("w:cs"), fonts.body)
    r_fonts.set(qn("w:ascii"), fonts.latin)
    r_fonts.set(qn("w:hAnsi"), fonts.latin)


def _inject_font_fallbacks(docx_bytes: bytes, fonts: DocumentFonts) -> bytes:
    """Declare w:altName fallbacks for every used font in word/fontTable.xml.

    Readers without e.g. Vazirmatn installed substitute the configured
    fallback (usually Tahoma, which ships everywhere) instead of picking an
    arbitrary system font. This is metadata only — it never changes layout
    when the primary font is present and no fonts are embedded in the file.
    """
    try:
        buffer = io.BytesIO(docx_bytes)
        with zipfile.ZipFile(buffer) as archive:
            names = archive.namelist()
            if "word/fontTable.xml" not in names:
                return docx_bytes
            members = {name: archive.read(name) for name in names}
        from lxml import etree

        root = etree.fromstring(members["word/fontTable.xml"])
        fonts_by_name = {el.get(qn("w:name")): el for el in root.findall(qn("w:font"))}
        wanted = [(fonts.body, fonts.fallback), (fonts.heading, fonts.fallback), (fonts.latin, fonts.fallback)]
        for name, fallback in wanted:
            if not name or not fallback or name == fallback:
                continue
            element = fonts_by_name.get(name)
            if element is None:
                element = etree.SubElement(root, qn("w:font"))
                element.set(qn("w:name"), name)
                fonts_by_name[name] = element
                etree.SubElement(element, qn("w:family")).set(qn("w:val"), "auto")
                etree.SubElement(element, qn("w:pitch")).set(qn("w:val"), "variable")
            for found in element.findall(qn("w:altName")):
                element.remove(found)
            alt = etree.SubElement(element, qn("w:altName"))
            alt.set(qn("w:val"), fallback)
        members["word/fontTable.xml"] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
        return out.getvalue()
    except Exception:
        # The fallback table is a nicety; never lose the document over it.
        logger.debug("fontTable fallback injection failed", exc_info=True)
        return docx_bytes


def _setup_document(meta: DocumentMeta, title: str, *, fonts: DocumentFonts) -> tuple[Document, object]:
    document = Document()
    # A4 with comfortable Persian-document margins.
    section = document.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.4)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(2.2)

    _apply_document_defaults(document, fonts)
    document.core_properties.title = xml_safe(title)
    document.core_properties.author = "Gamas Bot"
    document.core_properties.comments = meta.reference
    _add_page_number_footer(section, fonts=fonts)
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


def _add_title_block(document, title: str, meta: DocumentMeta, *, fonts: DocumentFonts, mode_label: str = "") -> None:
    title_fonts = DocumentFonts(
        body=fonts.heading, heading=fonts.heading, latin=fonts.latin, fallback=fonts.fallback
    )
    _add_rtl_paragraph(
        document,
        title,
        fonts=title_fonts,
        size=20,
        bold=True,
        color=ACCENT,
        align=WD_ALIGN_PARAGRAPH.CENTER,
        space_after=4,
        line_spacing=1.1,
    )
    if mode_label:
        _add_rtl_paragraph(
            document,
            mode_label,
            fonts=fonts,
            size=10,
            color=ACCENT,
            align=WD_ALIGN_PARAGRAPH.CENTER,
            space_after=2,
        )
    _add_rtl_paragraph(
        document,
        _meta_line(meta),
        fonts=fonts,
        size=9,
        color=MUTED,
        align=WD_ALIGN_PARAGRAPH.CENTER,
        space_after=10,
    )
    divider = _add_rtl_paragraph(
        document, "", fonts=fonts, size=2, space_after=12, line_spacing=1.0
    )
    _paragraph_borders(divider, ACCENT_HEX, size="12")


def _add_heading(document, text: str, *, fonts: DocumentFonts, size: float = 14, space_before: float = 14) -> None:
    heading_fonts = DocumentFonts(
        body=fonts.heading, heading=fonts.heading, latin=fonts.latin, fallback=fonts.fallback
    )
    paragraph = _add_rtl_paragraph(
        document,
        text,
        fonts=heading_fonts,
        size=size,
        bold=True,
        color=ACCENT,
        align=WD_ALIGN_PARAGRAPH.RIGHT,
        space_after=6,
        space_before=space_before,
    )
    # A heading alone at the foot of a page reads as a broken booklet.
    _keep_with_next(paragraph)


def _add_boxed_lines(
    document,
    label: str,
    lines: list[str],
    *,
    fonts: DocumentFonts,
    fill: str,
    marker: str = "✦",
) -> None:
    """A shaded, bordered box of marked lines with a bold label."""
    label_paragraph = _add_rtl_paragraph(
        document,
        label,
        fonts=fonts,
        size=11.5,
        bold=True,
        align=WD_ALIGN_PARAGRAPH.RIGHT,
        space_after=2,
    )
    _keep_with_next(label_paragraph)
    for index, line in enumerate(lines):
        paragraph = _add_rtl_paragraph(
            document,
            f"{marker} {line}",
            fonts=fonts,
            size=11,
            align=WD_ALIGN_PARAGRAPH.RIGHT,
            space_after=2 if index < len(lines) - 1 else 8,
        )
        paragraph.paragraph_format.right_indent = Cm(0.35)
        _shade_paragraph(paragraph, fill)
        _paragraph_borders(paragraph, fill)


def _apply_rtl_table_direction(table) -> None:
    """Make the first logical column render on the right (``w:bidiVisual``).

    CT_TblPr is order-sensitive and w:bidiVisual must precede w:tblW/w:tblLook;
    appending it last produces a document Word reports as needing repair.
    """
    tbl_pr = table._tbl.tblPr
    _remove_ppr_child(tbl_pr, "w:bidiVisual")
    bidi_visual = OxmlElement("w:bidiVisual")
    anchor = None
    for tag in ("w:tblStyleRowBandSize", "w:tblStyleColBandSize", "w:tblW", "w:jc",
                "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd",
                "w:tblLayout", "w:tblCellMar", "w:tblLook", "w:tblCaption",
                "w:tblDescription"):
        found = tbl_pr.find(qn(tag))
        if found is not None:
            anchor = found
            break
    if anchor is not None:
        anchor.addprevious(bidi_visual)
    else:
        tbl_pr.append(bidi_visual)


def _add_table(document, table_data, *, fonts: DocumentFonts) -> None:
    headers = table_data.headers
    rows = table_data.rows
    table = document.add_table(rows=len(rows) + 1, cols=len(headers))
    table.style = "Table Grid"
    _apply_rtl_table_direction(table)

    for column, header in enumerate(headers):
        cell = table.cell(0, column)
        paragraph = _fill_paragraph(
            cell.paragraphs[0],
            header,
            fonts=fonts,
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
    # Keep the column headings visible on every page of a long table.
    _repeat_table_header(table.rows[0])
    for row_index, row in enumerate(rows, start=1):
        for column, value in enumerate(row):
            cell = table.cell(row_index, column)
            _fill_paragraph(
                cell.paragraphs[0],
                value,
                fonts=fonts,
                size=10.5,
                align=WD_ALIGN_PARAGRAPH.CENTER,
                space_after=0,
            )
    _add_rtl_paragraph(document, "", fonts=fonts, size=4, space_after=6, line_spacing=1.0)


def _add_callout(document, callout, *, fonts: DocumentFonts) -> None:
    fill = CALLOUT_SHADES.get(callout.kind, "FFF6E0")
    emoji = CALLOUT_EMOJI.get(callout.kind, "💡")
    paragraph = _add_rtl_paragraph(
        document,
        f"{emoji} {callout.kind}: {callout.text}",
        fonts=fonts,
        size=11,
        align=WD_ALIGN_PARAGRAPH.RIGHT,
        space_after=6,
    )
    _shade_paragraph(paragraph, fill)
    _paragraph_borders(paragraph, fill)


def _add_definitions(document, section: NoteSection, *, fonts: DocumentFonts) -> None:
    """Term/definition pairs as lightly shaded definition rows."""
    for entry in section.definitions:
        paragraph = _add_rtl_paragraph(document, "", fonts=fonts, space_after=4)
        _add_directional_text(
            paragraph,
            f"◆ {entry.term}",
            fonts=fonts, size=11, bold=True, color=ACCENT,
        )
        if entry.term_en:
            _add_directional_text(paragraph, f" ({entry.term_en}) ", fonts=fonts, size=10.5, color=MUTED)
        _add_directional_text(paragraph, f": {entry.definition}", fonts=fonts, size=11)
        paragraph.paragraph_format.right_indent = Cm(0.25)
        _shade_paragraph(paragraph, DEFINITION_SHADE)


def _add_numbered_steps(document, steps: tuple[str, ...], *, fonts: DocumentFonts) -> None:
    """A procedure as numbered RTL lines with a hanging indent."""
    for index, step in enumerate(steps, start=1):
        paragraph = _add_rtl_paragraph(
            document,
            f"{to_persian_digits(index)}. {step}",
            fonts=fonts,
            size=11,
            align=WD_ALIGN_PARAGRAPH.RIGHT,
            space_after=3,
        )
        paragraph.paragraph_format.right_indent = Cm(0.5)
        # Hanging indent so wrapped lines align under the text, not the number.
        p_pr = paragraph._p.get_or_add_pPr()
        ind = p_pr.find(qn("w:ind"))
        if ind is None:
            ind = OxmlElement("w:ind")
            _insert_ppr_child(p_pr, ind, ("w:jc", "w:rPr", "w:sectPr"))
        ind.set(qn("w:hanging"), "283")  # 0.5 cm in twentieths of a point


def _add_formulas(document, formulas: tuple[str, ...], *, fonts: DocumentFonts) -> None:
    """Formula lines kept verbatim, centered and lightly emphasised."""
    for formula in formulas:
        paragraph = _add_rtl_paragraph(
            document,
            formula,
            fonts=fonts,
            size=11.5,
            bold=True,
            align=WD_ALIGN_PARAGRAPH.CENTER,
            space_after=4,
        )
        _shade_paragraph(paragraph, "F2F2F2")


def _add_examples(document, examples: tuple[str, ...], *, fonts: DocumentFonts) -> None:
    for example in examples:
        paragraph = _add_rtl_paragraph(
            document,
            f"✎ {example}",
            fonts=fonts,
            size=11,
            space_after=4,
        )
        paragraph.paragraph_format.right_indent = Cm(0.25)


def _add_section(document, index: int, section: NoteSection, *, fonts: DocumentFonts) -> None:
    _add_heading(document, f"{to_persian_digits(index)}. {section.heading}", fonts=fonts)
    for paragraph_text in section.paragraphs:
        _add_rtl_paragraph(document, paragraph_text, fonts=fonts)
    _add_definitions(document, section, fonts=fonts)
    for bullet in section.bullets:
        paragraph = _add_rtl_paragraph(
            document, f"• {bullet}", fonts=fonts, align=WD_ALIGN_PARAGRAPH.RIGHT
        )
        paragraph.paragraph_format.right_indent = Cm(0.35)
    if section.examples:
        _add_heading(document, "مثال‌ها", fonts=fonts, size=12, space_before=8)
        _add_examples(document, section.examples, fonts=fonts)
    if section.steps:
        _add_heading(document, "مراحل انجام", fonts=fonts, size=12, space_before=8)
        _add_numbered_steps(document, section.steps, fonts=fonts)
    if section.formulas:
        _add_formulas(document, section.formulas, fonts=fonts)
    if section.key_points:
        _add_boxed_lines(
            document,
            "نکته‌های کلیدی این بخش",
            list(section.key_points),
            fonts=fonts,
            fill=KEYPOINT_SHADE,
        )
    for callout in section.callouts:
        _add_callout(document, callout, fonts=fonts)
    if section.table is not None:
        _add_table(document, section.table, fonts=fonts)


#: Persian labels for the note modes, shown on the title block.
MODE_LABELS = {
    "full": "حالت تولید: کامل (حفظ کامل محتوای درس)",
    "standard": "حالت تولید: استاندارد",
    "summary": "حالت تولید: خلاصه",
}


def build_notes_docx(
    notes: StructuredNotes,
    *,
    fonts: DocumentFonts | None = None,
    meta: DocumentMeta,
    font: str | None = None,
) -> bytes:
    """Render validated structured notes as a polished RTL Word document."""
    resolved = fonts or resolve_fonts(font=font)
    document, section = _setup_document(meta, notes.display_title, fonts=resolved)
    _add_document_header(section, fonts=resolved, title=notes.display_title)
    _add_title_block(
        document,
        notes.display_title,
        meta,
        fonts=resolved,
        mode_label=MODE_LABELS.get(notes.note_mode, ""),
    )

    if notes.summary:
        _add_heading(document, "✨ خلاصه", fonts=resolved, space_before=4)
        paragraph = _add_rtl_paragraph(document, notes.summary, fonts=resolved, size=11.5, space_after=10)
        _shade_paragraph(paragraph, SUMMARY_SHADE)
        _paragraph_borders(paragraph, SUMMARY_SHADE)

    for index, section_model in enumerate(notes.sections, start=1):
        _add_section(document, index, section_model, fonts=resolved)

    if notes.key_points:
        _add_heading(document, "💡 نکته‌های کلیدی", fonts=resolved)
        _add_boxed_lines(document, "مهم‌ترین نکته‌های این جزوه", list(notes.key_points), fonts=resolved, fill=KEYPOINT_SHADE)

    if notes.glossary:
        _add_heading(document, "📖 واژه‌نامه", fonts=resolved)
        glossary_table = document.add_table(rows=len(notes.glossary) + 1, cols=2)
        glossary_table.style = "Table Grid"
        _apply_rtl_table_direction(glossary_table)
        header = glossary_table.rows[0]
        _repeat_table_header(header)
        for column, label in enumerate(("اصطلاح", "توضیح")):
            cell = glossary_table.cell(0, column)
            paragraph = _fill_paragraph(
                cell.paragraphs[0], label, fonts=resolved, size=10.5,
                bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0,
            )
            _shade_paragraph(paragraph, ACCENT_HEX)
            tc_pr = cell._tc.get_or_add_tcPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:fill"), ACCENT_HEX)
            tc_pr.append(shd)
        for row_index, entry in enumerate(notes.glossary, start=1):
            term_cell = glossary_table.cell(row_index, 0)
            _fill_paragraph(
                term_cell.paragraphs[0], entry.term, fonts=resolved, size=10.5,
                bold=True, align=WD_ALIGN_PARAGRAPH.RIGHT, space_after=0,
            )
            definition_cell = glossary_table.cell(row_index, 1)
            _fill_paragraph(
                definition_cell.paragraphs[0], entry.definition, fonts=resolved,
                size=10.5, align=WD_ALIGN_PARAGRAPH.RIGHT, space_after=0,
            )
        _add_rtl_paragraph(document, "", fonts=resolved, size=4, space_after=6, line_spacing=1.0)

    buffer = io.BytesIO()
    document.save(buffer)
    docx_bytes = buffer.getvalue()
    return _inject_font_fallbacks(docx_bytes, resolved)


def build_plain_docx(
    title: str, text: str, *, fonts: DocumentFonts | None = None, meta: DocumentMeta,
    font: str | None = None,
) -> bytes:
    """Polished RTL Word document built from raw/fallback material.

    Understands the light Markdown the pipeline itself emits (``#`` headings,
    ``-`` bullets, ``## بخش n`` part headers); everything else is a paragraph.
    """
    resolved = fonts or resolve_fonts(font=font)
    document, section = _setup_document(meta, title, fonts=resolved)
    _add_document_header(section, fonts=resolved, title=title)
    _add_title_block(document, title, meta, fonts=resolved)
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if heading:
            _add_heading(document, heading.group(1).strip(), fonts=resolved, size=13)
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if bullet:
            paragraph = _add_rtl_paragraph(
                document, f"• {bullet.group(1).strip()}", fonts=resolved, align=WD_ALIGN_PARAGRAPH.RIGHT
            )
            paragraph.paragraph_format.right_indent = Cm(0.35)
            continue
        _add_rtl_paragraph(document, line.strip(), fonts=resolved)
    buffer = io.BytesIO()
    document.save(buffer)
    return _inject_font_fallbacks(buffer.getvalue(), resolved)


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
