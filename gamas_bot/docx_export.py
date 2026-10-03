"""RTL Persian Word (.docx) and raw-text exporters for finished jobs.

The Word document is the polished deliverable of the bot: a real cover page, a
section-level page border (``w:pgBorders``), a table of contents built from
actual Word heading styles, a subtle running header, a footer with a real
``PAGE`` field, and right-to-left body text where embedded English keeps its
own direction and font.

Layout rules that matter and are implemented here:

* **Heading styles are real styles.** Section headings use ``Heading 1``,
  sub-blocks use ``Heading 2`` and definition terms use ``Heading 3``, so the
  Navigation Pane, an automatic table of contents and document restructuring
  all work. A styled ``Normal`` paragraph is never used as a heading.
* **The page frame is a page frame.** ``w:pgBorders`` is written into each
  section's ``w:sectPr``; the previous implementation drew a bordered empty
  paragraph, which is a paragraph border and not a page frame.
* **Mixed-script text is split into direction runs** (see :mod:`gamas_bot.bidi`)
  so Persian runs get ``w:cs`` complex-script faces and Latin tokens keep
  ``w:ascii``/``w:hAnsi`` with ``w:rtl`` set to zero. Logical order is never
  reversed in the file.
* **Fonts are referenced, never embedded.** ``word/fontTable.xml`` advertises
  the configured fallback through ``w:altName`` so a reader without the
  Persian face substitutes gracefully.

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
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .bidi import TextRun, split_direction_runs
from .config import PROJECT_ROOT
from .progress import to_persian_digits
from .structuring import NoteSection, StructuredNotes
from .textnorm import normalize_display

logger = logging.getLogger(__name__)

ACCENT = RGBColor(0x1F, 0x38, 0x64)
ACCENT_HEX = "1F3864"
MUTED = RGBColor(0x59, 0x59, 0x59)
#: Text on the accent-filled (dark) table header cells.
ON_ACCENT = RGBColor(0xFF, 0xFF, 0xFF)
RULE_FILL = "D6DEEB"
SUMMARY_SHADE = "EEF3FA"
KEYPOINT_SHADE = "E7F3E8"
DEFINITION_SHADE = "F5F0FA"
QUOTE_SHADE = "F7F4EC"
CALLOUT_SHADES = {"هشدار": "FDE7E9", "یادآوری": "FFF6E0"}
CALLOUT_EMOJI = {"هشدار": "⚠️", "یادآوری": "🔔"}
DEFAULT_FONT = "Tahoma"

#: The exact cover quotation. It is quoted verbatim and must never be
#: reworded, translated or normalized away.
COVER_QUOTE = "دانش اگر در ثریا باشد مردمی از سرزمین پارس به آن دست خواهند یافت"

#: Cover lines for the brand presentation.
COVER_GOD_LINE = "به نام خدا"
BRAND_NAME = "GAMAS"
BRAND_SUBTITLE = "Gamas Bot"

#: Default page-border look: a thin, elegant line 24 pt inside the page edge,
#: which clears the 2.2 cm text margin and the footer.
DEFAULT_BORDER_COLOR = "BFCEE4"
DEFAULT_BORDER_SIZE = 8  # eighths of a point -> 1 pt
DEFAULT_BORDER_SPACE = 24  # points from the page edge (0-31)
ALLOWED_BORDER_STYLES = frozenset(
    {
        "single", "double", "dotted", "dashed", "dotDash", "dotDotDash",
        "triple", "thinThickSmallGap", "thickThinSmallGap",
        "thickThickThinSmallGap", "thinThickMediumGap", "thickThinMediumGap",
        "thickThickThinMediumGap", "thinThickLargeGap", "thickThinLargeGap",
        "thickThickThinLargeGap", "wave", "doubleWave", "dashSmallGap",
        "dashDotStroked", "threeDEmboss", "threeDEngrave", "outset", "inset",
    }
)

#: Word style names used by the document. The rendering code applies these by
#: name, and tests assert they are present in ``word/styles.xml``/``document.xml``.
HEADING_STYLES = {1: "Heading 1", 2: "Heading 2", 3: "Heading 3"}
TITLE_STYLE = "Title"
SUBTITLE_STYLE = "Subtitle"
QUOTE_STYLE = "Quote"
TABLE_TEXT_STYLE = "Table text"
DEFINITION_STYLE = "Definition"
EXAMPLE_STYLE = "Example"
NOTE_STYLE = "Note"
WARNING_STYLE = "Warning"
CALLOUT_STYLE_BY_KIND = {"هشدار": WARNING_STYLE, "یادآوری": NOTE_STYLE, "نکته": NOTE_STYLE}

#: Heading typography: level -> (size, space before, space after).
HEADING_SIZES = {1: 15.0, 2: 12.5, 3: 11.5}
HEADING_SPACING = {1: (14, 6), 2: (10, 4), 3: (8, 3)}

#: A table of contents needs a document with enough structure to browse; below
#: this many sections the cover plus a short body is short enough to read
#: without one.
TOC_MIN_SECTIONS = 3

#: Auto-detected optional logo, used when DOCX_LOGO_PATH is not configured.
#: Fonts and images are never downloaded at generation time.
LOGO_CANDIDATES = ("assets/gamas_logo.png", "assets/gamas_logo.jpg")

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
FOOTER_BRAND_TEXT = "Gamas Bot"

#: ``w:sectPr`` child order (CT_SectPr). ``w:pgBorders`` and ``w:pgNumType``
#: are order-sensitive: appending them at the end produces a file Word offers
#: to "repair".
SECTPR_SUCCESSORS = (
    "w:lnNumType", "w:pgNumType", "w:cols", "w:formProt", "w:vAlign",
    "w:noEndnote", "w:titlePg", "w:textDirection", "w:bidi", "w:rtlGutter",
    "w:docGrid", "w:printerSettings", "w:sectPrChange",
)
SECTPR_PAGE_SIZE_SUCCESSORS = (
    "w:pgMar", "w:paperSrc", "w:pgBorders",
) + SECTPR_SUCCESSORS
SECTPR_AFTER_MARGINS = ("w:paperSrc", "w:pgBorders") + SECTPR_SUCCESSORS


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
class DocxDesign:
    """Configurable document design (cover, TOC, page frame, footer brand)."""

    cover_enabled: bool = True
    toc_enabled: bool = True
    page_border_enabled: bool = True
    border_style: str = "single"
    border_color: str = DEFAULT_BORDER_COLOR
    border_size: int = DEFAULT_BORDER_SIZE
    border_space: int = DEFAULT_BORDER_SPACE
    footer_brand: bool = True
    logo_path: str = ""

    @property
    def logo_file(self):
        """Absolute path of a usable local logo, or ``None``.

        A configured path wins; otherwise the repository's conventional
        ``assets/gamas_logo.*`` is used when it exists. Missing images never
        fail generation — the caller falls back to a typographic mark.
        """
        candidates = [self.logo_path] if self.logo_path else list(LOGO_CANDIDATES)
        for candidate in candidates:
            if not candidate:
                continue
            path = PROJECT_ROOT / candidate
            try:
                if path.is_file():
                    return path
            except OSError:  # pragma: no cover - defensive
                return None
        return None


def _as_flag(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def resolve_design(design_config: dict | None = None) -> DocxDesign:
    """Normalise a document-design configuration with safe defaults.

    Unknown or malformed values fall back to the built-in look instead of
    failing generation: a wrong border colour must never cost a user the file.
    """
    config = dict(design_config or {})
    color = str(config.get("border_color") or DEFAULT_BORDER_COLOR).strip().lstrip("#").upper()
    if not re.fullmatch(r"[0-9A-F]{6}", color):
        color = DEFAULT_BORDER_COLOR
    style = str(config.get("border_style") or "single").strip()
    if style not in ALLOWED_BORDER_STYLES:
        style = "single"
    try:
        size = int(config.get("border_size") or DEFAULT_BORDER_SIZE)
    except (TypeError, ValueError):
        size = DEFAULT_BORDER_SIZE
    try:
        space = int(config.get("border_space") or DEFAULT_BORDER_SPACE)
    except (TypeError, ValueError):
        space = DEFAULT_BORDER_SPACE
    return DocxDesign(
        cover_enabled=_as_flag(config.get("cover_enabled"), True),
        toc_enabled=_as_flag(config.get("toc_enabled"), True),
        page_border_enabled=_as_flag(config.get("page_border_enabled"), True),
        border_style=style,
        border_color=color,
        border_size=min(max(size, 2), 96),
        border_space=min(max(space, 0), 31),
        footer_brand=_as_flag(config.get("footer_brand"), True),
        logo_path=str(config.get("logo_path") or "").strip(),
    )


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
            ("w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:jc", "w:rPr", "w:sectPr"),
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
        p_pr, shd, ("w:tabs",) + PPR_SUCCESSORS
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
    _insert_ppr_child(p_pr, borders, ("w:shd",) + PPR_SUCCESSORS)


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


def _heading_fonts(fonts: DocumentFonts) -> DocumentFonts:
    """Body/heading roles swapped for the heading face."""
    return DocumentFonts(
        body=fonts.heading, heading=fonts.heading, latin=fonts.latin, fallback=fonts.fallback
    )


def _add_page_break(container) -> None:
    """Start a new page (a break, not a section break)."""
    paragraph = container.add_paragraph()
    paragraph.add_run().add_break(WD_BREAK.PAGE)


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
    style: str | None = None,
) -> object:
    paragraph = container.add_paragraph()
    if style:
        try:
            paragraph.style = container.styles[style] if hasattr(container, "styles") else style
        except KeyError:  # pragma: no cover - defensive: unknown style name
            logger.debug("Unknown Word style %s; falling back to the default", style)
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


# ---------------------------------------------------------------------------
# Section-level layout: page frame, page numbering, header and footer
# ---------------------------------------------------------------------------


def _insert_sectpr_child(sect_pr, element, successors: tuple[str, ...]) -> None:
    for tag in successors:
        found = sect_pr.find(qn(tag))
        if found is not None:
            found.addprevious(element)
            return
    # ``w:sectPr`` always contains page size/margins; appending is a last resort.
    sect_pr.append(element)


def apply_page_border(section, design: DocxDesign) -> bool:
    """Draw a page-wide frame with ``w:pgBorders`` in the section properties.

    The border is measured from the *page* edge (``w:offsetFrom="page"``), so
    it frames the page area rather than a text block, stays clear of the
    footer, and is inherited by every page of the section. Returns ``True``
    when a frame was written.
    """
    sect_pr = section._sectPr
    for found in sect_pr.findall(qn("w:pgBorders")):
        sect_pr.remove(found)
    if not design.page_border_enabled:
        return False
    borders = OxmlElement("w:pgBorders")
    borders.set(qn("w:offsetFrom"), "page")
    for edge in ("top", "left", "bottom", "right"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), design.border_style)
        element.set(qn("w:sz"), str(design.border_size))
        element.set(qn("w:space"), str(design.border_space))
        element.set(qn("w:color"), design.border_color)
        borders.append(element)
    _insert_sectpr_child(sect_pr, borders, SECTPR_AFTER_MARGINS)
    return True


def _set_section_page_numbering(section, *, start: int | None = None, fmt: str | None = None) -> None:
    """Write ``w:pgNumType`` so a section's numbering is explicit."""
    sect_pr = section._sectPr
    for found in sect_pr.findall(qn("w:pgNumType")):
        sect_pr.remove(found)
    if start is None and fmt is None:
        return
    element = OxmlElement("w:pgNumType")
    if fmt:
        element.set(qn("w:fmt"), fmt)
    if start is not None:
        element.set(qn("w:start"), str(start))
    _insert_sectpr_child(sect_pr, element, ("w:cols",) + SECTPR_SUCCESSORS)


def _set_section_rtl(section) -> None:
    """Mark the section as right-to-left (``w:bidi`` in ``w:sectPr``)."""
    sect_pr = section._sectPr
    for found in sect_pr.findall(qn("w:bidi")):
        sect_pr.remove(found)
    element = OxmlElement("w:bidi")
    element.set(qn("w:val"), "1")
    _insert_sectpr_child(sect_pr, element, ("w:rtlGutter", "w:docGrid") + SECTPR_SUCCESSORS)


def _configure_section(section) -> None:
    """A4 with comfortable Persian-document margins."""
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.4)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(2.2)
    section.header_distance = Cm(1.2)
    section.footer_distance = Cm(1.2)
    _set_section_rtl(section)


def _add_page_number_footer(section, *, fonts: DocumentFonts, design: DocxDesign) -> None:
    """A professional footer: optional brand plus a real ``PAGE`` field.

    The page number is a Word field (``w:fldChar``/``w:instrText``), never a
    hardcoded integer, so it stays correct after pages are added, the table of
    contents is refreshed, or the section count changes.
    """
    footer = section.footer
    footer.is_linked_to_previous = False
    paragraph = footer.paragraphs[0]
    for run in list(paragraph.runs):
        run._r.getparent().remove(run._r)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _enable_bidi(paragraph)
    paragraph.paragraph_format.space_before = Pt(2)
    paragraph.paragraph_format.space_after = Pt(0)
    if design.footer_brand:
        _add_directional_text(
            paragraph, FOOTER_BRAND_TEXT, fonts=fonts, size=9, color=MUTED
        )
        _add_directional_text(
            paragraph, "  —  ", fonts=fonts, size=9, color=MUTED, rtl_bold=None
        )
        _add_directional_text(paragraph, "صفحهٔ ", fonts=fonts, size=9, color=MUTED)
    _add_field(paragraph, "PAGE", fonts=fonts, size=9, color=MUTED)


def _add_field(paragraph, instruction: str, *, fonts: DocumentFonts, size: float,
               color: RGBColor | None = None, placeholder: str = "") -> None:
    """Append a Word field (page number, table of contents, ...) to a paragraph."""
    run = paragraph.add_run()
    _style_run(run, font=fonts.body, size=size, color=color)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction_element = OxmlElement("w:instrText")
    instruction_element.set(qn("xml:space"), "preserve")
    instruction_element.text = f" {instruction} "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instruction_element)
    if placeholder:
        separate = OxmlElement("w:fldChar")
        separate.set(qn("w:fldCharType"), "separate")
        run._r.append(separate)
        text = OxmlElement("w:t")
        text.set(qn("xml:space"), "preserve")
        text.text = placeholder
        run._r.append(text)
    run._r.append(end)


def _add_document_header(section, *, fonts: DocumentFonts, title: str) -> None:
    """A small running header: document title on one side, brand on the other."""
    header = section.header
    header.is_linked_to_previous = False
    paragraph = header.paragraphs[0]
    for run in list(paragraph.runs):
        run._r.getparent().remove(run._r)
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
    _insert_ppr_child(p_pr, borders, ("w:shd",) + PPR_SUCCESSORS)


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


def _style_complex_face(style, fonts: DocumentFonts, *, size: float | None = None,
                        color: RGBColor | None = None) -> None:
    """Fill the complex-script (Persian) slots of a *style* definition.

    Document body runs set their own ``w:cs`` font, but style definitions need
    it too: Word derives table-of-contents entries and any unstyled text from
    the style, and a style with only ``w:ascii`` renders Persian with Word's
    arbitrary substitution.
    """
    r_pr = style.element.get_or_add_rPr()
    r_fonts = r_pr.get_or_add_rFonts()
    r_fonts.set(qn("w:cs"), fonts.body)
    r_fonts.set(qn("w:ascii"), fonts.latin)
    r_fonts.set(qn("w:hAnsi"), fonts.latin)
    if size is not None:
        sz = r_pr.find(qn("w:sz"))
        _remove_rpr_child(r_pr, "w:szCs")
        sz_cs = OxmlElement("w:szCs")
        sz_cs.set(qn("w:val"), str(int(size * 2)))
        if sz is not None:
            sz.addnext(sz_cs)
        else:
            r_pr.append(sz_cs)
    if color is not None:
        col = r_pr.find(qn("w:color"))
        if col is not None:
            col.set(qn("w:val"), f"{color}")


def _ensure_paragraph_style(document, name: str, base: str = "Normal"):
    """Return a paragraph style, creating it when the template lacks it."""
    try:
        return document.styles[name]
    except KeyError:
        style = document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        try:
            style.base_style = document.styles[base]
        except KeyError:  # pragma: no cover - defensive
            pass
        style.quick_style = True
        return style


def _configure_styles(document, fonts: DocumentFonts) -> None:
    """Define the document's real Word styles (headings included)."""
    normal = document.styles["Normal"]
    normal.font.name = fonts.latin
    normal.font.size = Pt(11)
    _style_complex_face(normal, fonts, size=11)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15
    normal.paragraph_format.widow_control = True

    # Real heading styles: Word's Navigation Pane, automatic TOC and
    # "restructure the document" features all depend on these, not on looks.
    heading_specs = (
        (HEADING_STYLES[1], fonts.heading, HEADING_SIZES[1], 14, 6),
        (HEADING_STYLES[2], fonts.heading, HEADING_SIZES[2], 10, 4),
        (HEADING_STYLES[3], fonts.heading, HEADING_SIZES[3], 8, 3),
    )
    for name, face, size, before, after in heading_specs:
        style = document.styles[name]
        style.font.name = fonts.latin
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = ACCENT
        _style_complex_face(style, DocumentFonts(face, face, fonts.latin, fonts.fallback), size=size)
        paragraph_format = style.paragraph_format
        paragraph_format.space_before = Pt(before)
        paragraph_format.space_after = Pt(after)
        paragraph_format.keep_with_next = True
        paragraph_format.keep_together = True
        paragraph_format.line_spacing = 1.1

    for name, size, before, after, color in (
        (TITLE_STYLE, 26, 0, 10, ACCENT),
        (SUBTITLE_STYLE, 13, 0, 12, MUTED),
    ):
        style = document.styles[name]
        style.font.name = fonts.latin
        style.font.size = Pt(size)
        style.font.bold = name == TITLE_STYLE
        style.font.color.rgb = color
        _style_complex_face(style, fonts, size=size)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.line_spacing = 1.1

    body_like = (
        (DEFINITION_STYLE, 11, 4, 4),
        (EXAMPLE_STYLE, 11, 4, 4),
        (NOTE_STYLE, 11, 6, 6),
        (WARNING_STYLE, 11, 6, 6),
        (TABLE_TEXT_STYLE, 10.5, 0, 0),
    )
    for name, size, before, after in body_like:
        style = _ensure_paragraph_style(document, name)
        style.font.name = fonts.latin
        style.font.size = Pt(size)
        style.font.bold = False
        _style_complex_face(style, fonts, size=size)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.line_spacing = 1.15
        style.paragraph_format.widow_control = True

    quote = _ensure_paragraph_style(document, QUOTE_STYLE)
    quote.font.name = fonts.latin
    quote.font.size = Pt(11.5)
    quote.font.italic = False
    _style_complex_face(quote, fonts, size=11.5)
    quote.paragraph_format.space_before = Pt(6)
    quote.paragraph_format.space_after = Pt(6)
    quote.paragraph_format.line_spacing = 1.2

    toc_heading = _ensure_paragraph_style(document, "TOC Heading", base="Normal")
    toc_heading.font.name = fonts.latin
    toc_heading.font.size = Pt(16)
    toc_heading.font.bold = True
    toc_heading.font.color.rgb = ACCENT
    _style_complex_face(toc_heading, fonts, size=16)
    toc_heading.paragraph_format.space_before = Pt(0)
    toc_heading.paragraph_format.space_after = Pt(12)


def _apply_document_defaults(document, fonts: DocumentFonts) -> None:
    """Backwards-compatible alias for the style configuration."""
    _configure_styles(document, fonts)


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


def _new_document(meta: DocumentMeta, title: str) -> Document:
    document = Document()
    document.core_properties.title = xml_safe(title)
    document.core_properties.author = "Gamas Bot"
    document.core_properties.comments = meta.reference
    return document


def _meta_line(meta: DocumentMeta) -> str:
    created = meta.created_at or datetime.now()
    parts = [f"تاریخ: {jalali_date(created)}"]
    if meta.source_name:
        parts.append(f"منبع: {sanitize_filename_part(meta.source_name, max_chars=40)}")
    if meta.engine:
        parts.append(f"موتور تبدیل گفتار: {meta.engine}")
    parts.append(f"کد پیگیری: {meta.reference}")
    return " • ".join(parts)


# ---------------------------------------------------------------------------
# Cover page
# ---------------------------------------------------------------------------


def _add_rule(
    document,
    *,
    fonts: DocumentFonts,
    fill: str = RULE_FILL,
    space_after: float = 10,
    right_indent: float = 3.0,
) -> None:
    """A thin decorative rule (a shaded hairline, not a bordered paragraph)."""
    paragraph = _add_rtl_paragraph(
        document, "", fonts=fonts, size=4,
        space_after=space_after, space_before=2, line_spacing=1.0,
    )
    paragraph.paragraph_format.right_indent = Cm(right_indent)
    paragraph.paragraph_format.left_indent = Cm(right_indent)
    _shade_paragraph(paragraph, fill)


def _add_brand_mark(document, *, fonts: DocumentFonts, design: DocxDesign) -> None:
    """The Gamas mark: a local logo when one exists, else a typographic wordmark."""
    logo = design.logo_file
    if logo is not None:
        try:
            paragraph = _add_rtl_paragraph(
                document, "", fonts=fonts, align=WD_ALIGN_PARAGRAPH.CENTER,
                space_after=6, line_spacing=1.0,
            )
            paragraph.add_run().add_picture(str(logo), width=Cm(3.4))
            return
        except Exception:
            logger.warning("Gamas logo could not be embedded; using the typographic mark", exc_info=True)
    heading_fonts = _heading_fonts(fonts)
    _add_rtl_paragraph(
        document, BRAND_NAME, fonts=heading_fonts, size=34, bold=True, color=ACCENT,
        align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0, line_spacing=1.0,
    )
    _add_rtl_paragraph(
        document, BRAND_SUBTITLE, fonts=fonts, size=13, color=MUTED,
        align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2, line_spacing=1.0,
    )


def _add_cover_page(
    document,
    title: str,
    meta: DocumentMeta,
    *,
    fonts: DocumentFonts,
    design: DocxDesign,
    mode_label: str = "",
) -> None:
    """The dedicated first page: brand, lecture title, metadata, quotation.

    Deliberately restrained: the page carries only what identifies the
    booklet, so it stays calm and uncrowded.
    """
    heading_fonts = _heading_fonts(fonts)
    _add_rtl_paragraph(
        document, COVER_GOD_LINE, fonts=fonts, size=12, color=MUTED,
        align=WD_ALIGN_PARAGRAPH.CENTER, space_after=18, line_spacing=1.0,
    )
    _add_brand_mark(document, fonts=fonts, design=design)
    _add_rule(document, fonts=fonts, space_after=16)

    title_paragraph = _add_rtl_paragraph(
        document, "", fonts=heading_fonts, align=WD_ALIGN_PARAGRAPH.CENTER,
        space_after=6, line_spacing=1.15, style=TITLE_STYLE,
    )
    _add_directional_text(title_paragraph, title, fonts=heading_fonts, size=24, bold=True, color=ACCENT)
    if mode_label:
        _add_rtl_paragraph(
            document, mode_label, fonts=fonts, size=10.5, color=ACCENT,
            align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2, line_spacing=1.0,
        )
    _add_rtl_paragraph(
        document, _meta_line(meta), fonts=fonts, size=9.5, color=MUTED,
        align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2, line_spacing=1.2,
    )
    _add_rule(document, fonts=fonts, fill=RULE_FILL, space_after=14)

    # Flexible space so the quotation sits quietly in the lower third of the
    # page (fixed spacer paragraphs: Word has no "flexible space" that survives
    # a font substitution, so the offset is deliberately conservative).
    for _ in range(9):
        _add_rtl_paragraph(
            document, "", fonts=fonts, size=11, space_after=12, line_spacing=1.0
        )
    quote_paragraph = _add_rtl_paragraph(
        document, "", fonts=fonts, size=12, align=WD_ALIGN_PARAGRAPH.CENTER,
        space_after=0, space_before=0, line_spacing=1.4, style=QUOTE_STYLE,
    )
    _add_directional_text(
        quote_paragraph, f"«{COVER_QUOTE}»", fonts=fonts, size=12, bold=False, color=ACCENT,
    )
    _shade_paragraph(quote_paragraph, QUOTE_SHADE)
    _paragraph_borders(quote_paragraph, RULE_FILL, size="4")


def _add_body_title_block(
    document,
    title: str,
    meta: DocumentMeta,
    *,
    fonts: DocumentFonts,
    mode_label: str = "",
) -> None:
    """The title block of a cover-less document.

    Without the cover page nothing else in the file states what the booklet is
    called, so the first page opens with the title, the mode and the same
    metadata line the cover would have shown.
    """
    heading_fonts = _heading_fonts(fonts)
    _add_brand_mark(document, fonts=fonts, design=DocxDesign())
    paragraph = _add_rtl_paragraph(
        document, "", fonts=heading_fonts, align=WD_ALIGN_PARAGRAPH.CENTER,
        space_after=6, line_spacing=1.15, style=TITLE_STYLE,
    )
    _add_directional_text(paragraph, title, fonts=heading_fonts, size=22, bold=True, color=ACCENT)
    if mode_label:
        _add_rtl_paragraph(
            document, mode_label, fonts=fonts, size=10.5, color=ACCENT,
            align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2, line_spacing=1.0,
        )
    _add_rtl_paragraph(
        document, _meta_line(meta), fonts=fonts, size=9.5, color=MUTED,
        align=WD_ALIGN_PARAGRAPH.CENTER, space_after=6, line_spacing=1.2,
    )
    _add_rule(document, fonts=fonts, space_after=14)


def _add_toc(document, *, fonts: DocumentFonts, levels: str = "1-2") -> None:
    """An automatic Word table of contents built from the heading styles.

    The field is marked ``w:dirty`` so Word offers to build it on open; until
    then the placeholder text explains how to refresh it. It is never a
    hand-typed list.
    """
    paragraph = _add_rtl_paragraph(
        document, "", fonts=fonts, align=WD_ALIGN_PARAGRAPH.RIGHT,
        space_after=8, line_spacing=1.0, style="TOC Heading",
    )
    _add_directional_text(paragraph, "فهرست مطالب", fonts=_heading_fonts(fonts), size=16, bold=True, color=ACCENT)

    field_paragraph = _add_rtl_paragraph(
        document, "", fonts=fonts, align=WD_ALIGN_PARAGRAPH.RIGHT, space_after=6,
    )
    run = field_paragraph.add_run()
    _style_run(run, font=fonts.body, size=11)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    begin.set(qn("w:dirty"), "true")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = f' TOC \\o "{levels}" \\h \\z \\u '
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.set(qn("xml:space"), "preserve")
    placeholder.text = "برای ساخت فهرست، در Word کلید F9 را بزنید (به‌روزرسانی فیلدها)."
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for element in (begin, instruction, separate, placeholder, end):
        run._r.append(element)


# ---------------------------------------------------------------------------
# Body blocks
# ---------------------------------------------------------------------------


def _add_heading(
    container,
    text: str,
    *,
    fonts: DocumentFonts,
    level: int = 1,
    size: float | None = None,
    space_before: float | None = None,
    space_after: float | None = None,
) -> object:
    """A *real* Word heading: ``w:pStyle`` Heading 1/2/3, not a styled Normal.

    The explicit run formatting keeps the Persian complex-script face and the
    configured sizes; the style is what gives Word the outline level.
    """
    level = level if level in HEADING_STYLES else 1
    resolved_size = size if size is not None else HEADING_SIZES[level]
    heading_fonts = _heading_fonts(fonts)
    paragraph = container.add_paragraph()
    try:
        paragraph.style = container.styles[HEADING_STYLES[level]]
    except (KeyError, AttributeError):  # pragma: no cover - defensive
        logger.debug("Heading %s style unavailable", level)
    _fill_paragraph(
        paragraph,
        text,
        fonts=heading_fonts,
        size=resolved_size,
        bold=True,
        color=ACCENT,
        align=WD_ALIGN_PARAGRAPH.RIGHT,
        space_after=(HEADING_SPACING[level][1] if space_after is None else space_after),
        space_before=(HEADING_SPACING[level][0] if space_before is None else space_before),
    )
    # A heading alone at the foot of a page reads as a broken booklet.
    _keep_with_next(paragraph)
    return paragraph


def _add_boxed_lines(
    document,
    label: str,
    lines: list[str],
    *,
    fonts: DocumentFonts,
    fill: str,
    marker: str = "✦",
    level: int = 2,
    style: str = NOTE_STYLE,
) -> None:
    """A shaded, bordered box of marked lines with a bold label."""
    if label:
        _add_heading(document, label, fonts=fonts, level=level, space_before=8, space_after=3)
    for index, line in enumerate(lines):
        paragraph = _add_rtl_paragraph(
            document,
            f"{marker} {line}",
            fonts=fonts,
            size=11,
            align=WD_ALIGN_PARAGRAPH.RIGHT,
            space_after=2 if index < len(lines) - 1 else 8,
            style=style,
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


def _set_table_widths(table, widths_cm: list[float]) -> None:
    """Give every column a fixed, sensible width (header and body cells alike)."""
    for column_index, width in enumerate(widths_cm):
        if width <= 0:
            continue
        for row in table.rows:
            try:
                cell = row.cells[column_index]
            except IndexError:  # pragma: no cover - defensive
                continue
            cell.width = Cm(width)


def _shade_cell(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    for found in tc_pr.findall(qn("w:shd")):
        tc_pr.remove(found)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _add_table(document, table_data, *, fonts: DocumentFonts) -> None:
    headers = table_data.headers
    rows = table_data.rows
    table = document.add_table(rows=len(rows) + 1, cols=len(headers))
    table.style = "Table Grid"
    table.autofit = True
    _apply_rtl_table_direction(table)

    for column, header in enumerate(headers):
        cell = table.cell(0, column)
        paragraph = _fill_paragraph(
            cell.paragraphs[0],
            header,
            fonts=fonts,
            size=10.5,
            bold=True,
            color=ON_ACCENT,
            align=WD_ALIGN_PARAGRAPH.CENTER,
            space_after=0,
        )
        try:
            paragraph.style = document.styles[TABLE_TEXT_STYLE]
        except KeyError:  # pragma: no cover - defensive
            pass
        _shade_paragraph(paragraph, ACCENT_HEX)
        _shade_cell(cell, ACCENT_HEX)
    # Keep the column headings visible on every page of a long table.
    _repeat_table_header(table.rows[0])
    for row_index, row in enumerate(rows, start=1):
        for column, value in enumerate(row):
            cell = table.cell(row_index, column)
            paragraph = _fill_paragraph(
                cell.paragraphs[0],
                value,
                fonts=fonts,
                size=10.5,
                align=WD_ALIGN_PARAGRAPH.RIGHT,
                space_after=0,
            )
            try:
                paragraph.style = document.styles[TABLE_TEXT_STYLE]
            except KeyError:  # pragma: no cover - defensive
                pass
    if headers:
        width = 17.0 / max(len(headers), 1)
        _set_table_widths(table, [width] * len(headers))
    _add_rtl_paragraph(document, "", fonts=fonts, size=4, space_after=6, line_spacing=1.0)


def _add_callout(document, callout, *, fonts: DocumentFonts) -> None:
    fill = CALLOUT_SHADES.get(callout.kind, "FFF6E0")
    emoji = CALLOUT_EMOJI.get(callout.kind, "💡")
    style = CALLOUT_STYLE_BY_KIND.get(callout.kind, NOTE_STYLE)
    paragraph = _add_rtl_paragraph(
        document,
        f"{emoji} {callout.kind}: {callout.text}",
        fonts=fonts,
        size=11,
        align=WD_ALIGN_PARAGRAPH.RIGHT,
        space_after=6,
        style=style,
    )
    _shade_paragraph(paragraph, fill)
    _paragraph_borders(paragraph, fill)


def _add_definitions(document, section: NoteSection, *, fonts: DocumentFonts) -> None:
    """Term/definition pairs; each term is a real ``Heading 3``."""
    if not section.definitions:
        return
    _add_heading(document, "تعریف‌ها", fonts=fonts, level=2, space_before=8, space_after=3)
    for entry in section.definitions:
        term_paragraph = _add_heading(
            document,
            "",
            fonts=fonts,
            level=3,
            space_before=6,
            space_after=2,
        )
        _add_directional_text(
            term_paragraph, f"◆ {entry.term}", fonts=_heading_fonts(fonts), size=11.5, bold=True, color=ACCENT,
        )
        if entry.term_en:
            _add_directional_text(term_paragraph, f" ({entry.term_en}) ", fonts=fonts, size=10.5, color=MUTED)
        _shade_paragraph(term_paragraph, DEFINITION_SHADE)
        definition_paragraph = _add_rtl_paragraph(
            document,
            entry.definition,
            fonts=fonts,
            size=11,
            space_after=4,
            style=DEFINITION_STYLE,
        )
        definition_paragraph.paragraph_format.right_indent = Cm(0.25)
        _shade_paragraph(definition_paragraph, DEFINITION_SHADE)


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
            style=EXAMPLE_STYLE,
        )
        paragraph.paragraph_format.right_indent = Cm(0.25)


def _add_bullets(document, bullets: tuple[str, ...], *, fonts: DocumentFonts) -> None:
    for bullet in bullets:
        paragraph = _add_rtl_paragraph(
            document, f"• {bullet}", fonts=fonts, align=WD_ALIGN_PARAGRAPH.RIGHT
        )
        paragraph.paragraph_format.right_indent = Cm(0.35)


def _add_section(document, index: int, section: NoteSection, *, fonts: DocumentFonts) -> None:
    _add_heading(document, f"{to_persian_digits(index)}. {section.heading}", fonts=fonts, level=1)
    for paragraph_text in section.paragraphs:
        _add_rtl_paragraph(document, paragraph_text, fonts=fonts)
    _add_definitions(document, section, fonts=fonts)
    _add_bullets(document, section.bullets, fonts=fonts)
    if section.examples:
        _add_heading(document, "مثال‌ها", fonts=fonts, level=2)
        _add_examples(document, section.examples, fonts=fonts)
    if section.steps:
        _add_heading(document, "مراحل انجام", fonts=fonts, level=2)
        _add_numbered_steps(document, section.steps, fonts=fonts)
    if section.formulas:
        _add_heading(document, "فرمول‌ها", fonts=fonts, level=2)
        _add_formulas(document, section.formulas, fonts=fonts)
    if section.key_points:
        _add_boxed_lines(
            document,
            "نکته‌های کلیدی این بخش",
            list(section.key_points),
            fonts=fonts,
            fill=KEYPOINT_SHADE,
            level=2,
        )
    for callout in section.callouts:
        _add_callout(document, callout, fonts=fonts)
    if section.table is not None:
        _add_heading(document, "مقایسه و دسته‌بندی", fonts=fonts, level=2)
        _add_table(document, section.table, fonts=fonts)


#: Persian labels for the note modes, shown on the cover.
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
    design: DocxDesign | None = None,
) -> bytes:
    """Render validated structured notes as a polished RTL Word document.

    Layout: cover page (optional) -> table of contents (optional, long
    documents only) -> summary -> sections -> key points -> glossary.
    """
    resolved = fonts or resolve_fonts(font=font)
    style = design or DocxDesign()
    document = _new_document(meta, notes.display_title)
    _configure_styles(document, resolved)

    first_section = document.sections[0]
    _configure_section(first_section)
    apply_page_border(first_section, style)
    if style.cover_enabled:
        _add_cover_page(
            document,
            notes.display_title,
            meta,
            fonts=resolved,
            design=style,
            mode_label=MODE_LABELS.get(notes.note_mode, ""),
        )
        # The cover is a section of its own: no running header, no page number,
        # and the body restarts at page one behind a page break.
        body_section = document.add_section(WD_SECTION.NEW_PAGE)
        _configure_section(body_section)
        apply_page_border(body_section, style)
    else:
        # Without a cover the first page *is* body content. Adding a section
        # break here would silently create a blank leading page.
        body_section = first_section
        _add_body_title_block(
            document,
            notes.display_title,
            meta,
            fonts=resolved,
            mode_label=MODE_LABELS.get(notes.note_mode, ""),
        )
    _set_section_page_numbering(body_section, start=1)
    _add_document_header(body_section, fonts=resolved, title=notes.display_title)
    _add_page_number_footer(body_section, fonts=resolved, design=style)

    if style.toc_enabled and len(notes.sections) >= TOC_MIN_SECTIONS:
        _add_toc(document, fonts=resolved)
        _add_page_break(document)

    if notes.learning_objectives:
        _add_heading(document, "اهداف یادگیری", fonts=resolved, level=1, space_before=0)
        _add_bullets(document, notes.learning_objectives, fonts=resolved)

    if notes.summary:
        _add_heading(document, "خلاصه", fonts=resolved, level=1, space_before=4)
        paragraph = _add_rtl_paragraph(document, notes.summary, fonts=resolved, size=11.5, space_after=10)
        _shade_paragraph(paragraph, SUMMARY_SHADE)
        _paragraph_borders(paragraph, SUMMARY_SHADE)

    for index, section_model in enumerate(notes.sections, start=1):
        _add_section(document, index, section_model, fonts=resolved)

    if notes.key_points:
        _add_heading(document, "نکته‌های کلیدی", fonts=resolved, level=1)
        _add_boxed_lines(
            document, "مهم‌ترین نکته‌های این جزوه", list(notes.key_points),
            fonts=resolved, fill=KEYPOINT_SHADE, level=2,
        )

    if notes.review_questions:
        _add_heading(document, "پرسش‌های مرور", fonts=resolved, level=1)
        _add_numbered_steps(document, notes.review_questions, fonts=resolved)

    if notes.glossary:
        _add_heading(document, "واژه‌نامه", fonts=resolved, level=1)
        glossary_table = document.add_table(rows=len(notes.glossary) + 1, cols=2)
        glossary_table.style = "Table Grid"
        _apply_rtl_table_direction(glossary_table)
        header = glossary_table.rows[0]
        _repeat_table_header(header)
        for column, label in enumerate(("اصطلاح", "توضیح")):
            cell = glossary_table.cell(0, column)
            paragraph = _fill_paragraph(
                cell.paragraphs[0], label, fonts=resolved, size=10.5,
                bold=True, color=ON_ACCENT,
                align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0,
            )
            try:
                paragraph.style = document.styles[TABLE_TEXT_STYLE]
            except KeyError:  # pragma: no cover - defensive
                pass
            _shade_paragraph(paragraph, ACCENT_HEX)
            _shade_cell(cell, ACCENT_HEX)
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
        _set_table_widths(glossary_table, [4.6, 12.4])
        _add_rtl_paragraph(document, "", fonts=resolved, size=4, space_after=6, line_spacing=1.0)

    buffer = io.BytesIO()
    document.save(buffer)
    return _inject_font_fallbacks(buffer.getvalue(), resolved)


def build_plain_docx(
    title: str, text: str, *, fonts: DocumentFonts | None = None, meta: DocumentMeta,
    font: str | None = None, design: DocxDesign | None = None,
) -> bytes:
    """Polished RTL Word document built from raw/fallback material.

    Understands the light Markdown the pipeline itself emits (``#`` headings,
    ``-`` bullets, ``## بخش n`` part headers); everything else is a paragraph.
    """
    resolved = fonts or resolve_fonts(font=font)
    style = design or DocxDesign()
    document = _new_document(meta, title)
    _configure_styles(document, resolved)

    first_section = document.sections[0]
    _configure_section(first_section)
    apply_page_border(first_section, style)
    if style.cover_enabled:
        _add_cover_page(document, title, meta, fonts=resolved, design=style)
        body_section = document.add_section(WD_SECTION.NEW_PAGE)
        _configure_section(body_section)
        apply_page_border(body_section, style)
    else:
        body_section = first_section
        _add_body_title_block(document, title, meta, fonts=resolved)
    _set_section_page_numbering(body_section, start=1)
    _add_document_header(body_section, fonts=resolved, title=title)
    _add_page_number_footer(body_section, fonts=resolved, design=style)

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", line)
        if heading:
            # ``#``/``##``/``###`` map onto real Heading 1-3 styles.
            level = min(max(len(heading.group(1)) - 1, 1), 3)
            _add_heading(document, heading.group(2).strip(), fonts=resolved, level=level)
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
