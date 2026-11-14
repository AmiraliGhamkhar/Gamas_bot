"""The Gamas PDF booklet renderer.

This is the print deliverable of the same validated ``StructuredNotes`` model
that :mod:`gamas_bot.docx_export` renders to Word. Both renderers read the same
design system (:mod:`gamas_bot.document`): the same palette, type scale, page
grid, component vocabulary and artwork. That shared source of truth — not a
parallel look — is what makes the PDF and the DOCX read as one publication.

Layout:

* **Cover** — cream paper, navy frame, the Gamas monogram, a navy title panel,
  the topic-aware hero artwork, the quotation panel.
* **Body** — chapter eyebrows and navy chapter banners, teal subheads, cream
  and tinted cards for definitions/examples/formulas, navy high-yield cards for
  exam-oriented lecturer notes, RTL tables with a repeating navy header, and a
  running header/footer with real page numbers.
* **Close** — glossary, editorial note and the automatic-generation disclaimer.

Persian is shaped with ``arabic_reshaper`` and reordered with ``python-bidi``;
wrapping happens on the *logical* text (see :mod:`gamas_bot.document.rtl`), so a
line break can never split a contextual letter form. The canvas is invariant:
identical notes always produce identical bytes.

The module is deliberately a *renderer*: it never decides what the notes say,
never adds a scientific claim, and never labels a sentence as exam material
unless the source pipeline already attributed that tip to the lecturer.
"""

from __future__ import annotations

import io
import logging
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image as RLImage,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from .document import components as design
from .document.assets import booklet_text, topic_for
from .document.assets import optional as optional_asset
from .document.layout import pdf_frame
from .document.rtl import visual, visual_ltr, wrap_logical
from .document.theme import THEME
from .docx_export import DocumentMeta
from .notes import NoteSection, NoteTable, StructuredNotes
from .pdf_common import (
    BOLD_NAME,
    REGULAR_NAME,
    InvariantCanvas as _InvariantCanvas,
    register_fonts,
)
from .progress import to_persian_digits

logger = logging.getLogger(__name__)

PAL = THEME.palette
TYPE = THEME.type
SPACE = THEME.space
FRAME = pdf_frame()

_PAGE_W = FRAME.page_width
_PAGE_H = FRAME.page_height
_MARGIN_LR = FRAME.margin_lr
_MARGIN_TOP = FRAME.margin_top
_MARGIN_BOTTOM = FRAME.margin_bottom
_FRAME_W = FRAME.width
_FRAME_H = FRAME.height


def _c(value: str) -> colors.Color:
    return colors.HexColor(value)


NAVY = _c(PAL.navy_900)
NAVY_DEEP = _c(PAL.navy_950)
TEAL = _c(PAL.teal_700)
TEAL_LIGHT = _c(PAL.teal_400)
CYAN = _c(PAL.cyan_500)
CREAM = _c(PAL.cream_50)
CREAM_100 = _c(PAL.cream_100)
PAPER = _c(PAL.paper)
INK = _c(PAL.ink)
BODY = _c(PAL.body)
MUTED = _c(PAL.muted)
LINE = _c(PAL.line)
ZEBRA = _c(PAL.zebra)
ON_DARK = _c(PAL.on_dark)
WHITE = colors.white


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _measure(font: str, size: float):
    def measure(text: str) -> float:
        return pdfmetrics.stringWidth(text, font, size)

    return measure


def _rtl(
    text: str, style: ParagraphStyle, width: float, *, marker: str = ""
) -> Paragraph:
    """A right-to-left paragraph pre-wrapped in logical order, then shaped.

    ``marker`` is appended to each *visual* line: appending to the logical text
    would place a leading bullet at the visual left edge, because BiDi treats
    it as a neutral character and the paragraph is RTL.
    """
    lines = wrap_logical(text or "", _measure(style.fontName, style.fontSize), width)
    shaped = []
    for line in lines:
        rendered = _xml_escape(visual(line))
        shaped.append(f"{rendered} {marker}" if marker else rendered)
    return Paragraph("<br/>".join(shaped) or " ", style)


def _ltr(text: str, style: ParagraphStyle, width: float) -> Paragraph:
    """A left-to-right paragraph (formulas, Latin-only technical lines)."""
    lines = wrap_logical(text or "", _measure(style.fontName, style.fontSize), width)
    body = "<br/>".join(_xml_escape(visual_ltr(line)) for line in lines) or " "
    return Paragraph(body, style)


def _mixed(text: str, style: ParagraphStyle, width: float) -> Paragraph:
    """Shape Persian, keep pure-Latin lines LTR (used for glossary terms)."""
    lines = wrap_logical(text or "", _measure(style.fontName, style.fontSize), width)
    shaped = []
    for line in lines:
        latin_only = all(ord(char) < 0x0590 for char in line)
        shaped.append(_xml_escape(line) if latin_only else _xml_escape(visual(line)))
    return Paragraph("<br/>".join(shaped) or " ", style)


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


def _styles() -> dict[str, ParagraphStyle]:
    regular, bold = register_fonts()

    def style(name, font, size, leading, color, align, **kwargs):
        return ParagraphStyle(
            name, fontName=font, fontSize=size, leading=leading,
            textColor=color, alignment=align, **kwargs,
        )

    return {
        "body": style("GmsBody", regular, TYPE.body, TYPE.body_pdf_leading, BODY, TA_RIGHT,
                      spaceAfter=6),
        "bullet": style("GmsBullet", regular, TYPE.body, TYPE.body_pdf_leading, BODY, TA_RIGHT,
                        spaceAfter=4, rightIndent=6, leftIndent=4),
        "h2": style("GmsH2", bold, TYPE.h2, TYPE.h2 * 1.35, TEAL, TA_RIGHT,
                    spaceBefore=10, spaceAfter=4, keepWithNext=1),
        "h3": style("GmsH3", bold, TYPE.h3, TYPE.h3 * 1.35, _c(PAL.navy_700), TA_RIGHT,
                    spaceBefore=7, spaceAfter=3, keepWithNext=1),
        "banner": style("GmsBanner", bold, TYPE.banner, TYPE.banner * 1.3, ON_DARK, TA_RIGHT),
        "h1_light": style("GmsH1Light", bold, TYPE.banner, TYPE.banner * 1.35, TEAL, TA_RIGHT,
                          spaceBefore=10, spaceAfter=3, keepWithNext=1),
        "eyebrow": style("GmsEyebrow", bold, TYPE.eyebrow, TYPE.eyebrow * 1.4, TEAL, TA_RIGHT),
        "card_label": style("GmsCardLabel", bold, 8.8, 11.5, TEAL, TA_RIGHT, spaceAfter=2),
        "card_label_dark": style("GmsCardLabelDark", bold, 8.8, 11.5, CYAN, TA_RIGHT, spaceAfter=2),
        "card_term": style("GmsCardTerm", bold, 11, 15, NAVY, TA_RIGHT, spaceAfter=2),
        "card_body": style("GmsCardBody", regular, 10.6, 16, BODY, TA_RIGHT),
        "card_body_dark": style("GmsCardBodyDark", regular, 10.6, 16, CREAM, TA_RIGHT),
        "card_item": style("GmsCardItem", regular, 10.4, 15.5, BODY, TA_RIGHT,
                           rightIndent=6, spaceAfter=2),
        "card_item_dark": style("GmsCardItemDark", regular, 10.4, 15.5, CREAM, TA_RIGHT,
                                rightIndent=6, spaceAfter=2),
        "formula": style("GmsFormula", regular, 10.6, 15.5, _c(PAL.navy_800), TA_LEFT),
        "caption": style("GmsCaption", regular, TYPE.caption, 12, MUTED, TA_CENTER, spaceAfter=4),
        "cell_head": style("GmsCellHead", bold, TYPE.table_head, 13.5, WHITE, TA_CENTER),
        "cell": style("GmsCell", regular, TYPE.table, 14, BODY, TA_RIGHT),
        "cell_strong": style("GmsCellStrong", bold, TYPE.table, 14, NAVY, TA_RIGHT),
        "cell_ltr": style("GmsCellLtr", regular, TYPE.table, 14, BODY, TA_LEFT),
        # cover
        "cover_brand": style("GmsCoverBrand", bold, 30, 34, NAVY, TA_CENTER),
        "cover_sub": style("GmsCoverSub", regular, 7.8, 10, TEAL, TA_CENTER),
        "cover_god": style("GmsCoverGod", regular, 10, 13, MUTED, TA_CENTER, spaceAfter=6),
        "cover_label": style("GmsCoverLabel", bold, 9.2, 12, CYAN, TA_CENTER, spaceAfter=4),
        "cover_title": style("GmsCoverTitle", bold, 20, 27, ON_DARK, TA_CENTER, spaceAfter=6),
        "cover_meta": style("GmsCoverMeta", regular, 9, 12.5, CREAM_100, TA_CENTER),
        "quote": style("GmsQuote", regular, 11.5, 17, NAVY, TA_CENTER),
        "attribution": style("GmsAttribution", regular, 8.6, 12, MUTED, TA_CENTER, spaceAfter=2),
        "disclaimer": style("GmsDisclaimer", regular, 8.4, 12.5, MUTED, TA_RIGHT, spaceAfter=4),
        "note_small": style("GmsNoteSmall", regular, 8.2, 11.5, MUTED, TA_RIGHT),
        "step_num": style("GmsStepNum", bold, 9.5, 12, WHITE, TA_CENTER),
    }


# ---------------------------------------------------------------------------
# Flowables
# ---------------------------------------------------------------------------


class _EcgRule(Flowable):
    """The Gamas motif as a hairline: a flat ECG complex, drawn as vectors.

    Canvas primitives need no font coverage and stay crisp at any size, unlike
    a glyph (Vazirmatn has no arrow/ECG character and would draw ``.notdef``).
    """

    def __init__(self, width: float, *, height: float = 9, color: colors.Color = CYAN,
                 thickness: float = 1.0) -> None:
        super().__init__()
        self._width = width
        self._height = height
        self._color = color
        self._thickness = thickness
        # Flush the motif to the reading start (right) in this RTL document.
        self.hAlign = "RIGHT"

    def wrap(self, availWidth, availHeight):
        return self._width, self._height

    def draw(self):
        canv = self.canv
        canv.saveState()
        canv.setStrokeColor(self._color)
        canv.setLineWidth(self._thickness)
        canv.setLineCap(1)
        base = self._height / 2
        span = self._width
        path = canv.beginPath()
        path.moveTo(0, base)
        for fraction, height in (
            (0.18, 0), (0.24, 2.4), (0.27, -1.6), (0.30, 6.4), (0.33, -4.2),
            (0.37, 1.2), (0.42, 0), (0.78, 0),
        ):
            path.lineTo(span * fraction, base + height * (self._height / 12))
        path.lineTo(span, base)
        canv.drawPath(path, stroke=1, fill=0)
        canv.restoreState()


class _StepBadge(Flowable):
    """A teal numbered circle, drawn as vectors."""

    def __init__(self, number: int, size: float = 16) -> None:
        super().__init__()
        self._number = number
        self._size = size

    def wrap(self, availWidth, availHeight):
        return self._size, self._size

    def draw(self):
        canv = self.canv
        radius = self._size / 2
        canv.setFillColor(TEAL)
        canv.setStrokeColor(CYAN)
        canv.setLineWidth(0.8)
        canv.circle(radius, radius, radius, stroke=1, fill=1)
        canv.setFillColor(WHITE)
        canv.setFont(BOLD_NAME, 9)
        canv.drawCentredString(radius, radius - 3.2, visual(to_persian_digits(str(self._number))))


# ---------------------------------------------------------------------------
# Component builders
# ---------------------------------------------------------------------------


def _banner(text: str, styles) -> Table:
    """The navy chapter banner: white title on a deep-navy bar."""
    body = _rtl(text, styles["banner"], _FRAME_W - 26)
    table = Table([[body]], colWidths=[_FRAME_W], hAlign="CENTER")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("LEFTPADDING", (0, 0), (-1, -1), 13),
        ("RIGHTPADDING", (0, 0), (-1, -1), 13),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 1.4, CYAN),
    ]))
    return table


def _display_heading(text: str, styles) -> list[Flowable]:
    """A front-matter heading: teal type over a short cyan ECG rule.

    Deliberately lighter than a chapter banner. Chapters, the key-points
    summary and the glossary own the navy bars; the reading guide, the overview
    and the closing material use this register, so a navy bar always means "a
    major destination in this booklet".
    """
    return [
        _rtl(text, styles["h1_light"], _FRAME_W),
        _EcgRule(96, height=7, color=TEAL_LIGHT),
        Spacer(1, 5),
    ]


def _subheading(text: str, styles, *, level: int = 2) -> Table:
    """A teal subheading with a short accent bar on its right edge."""
    paragraph = _rtl(text, styles["h2" if level == 2 else "h3"], _FRAME_W - 14)
    accent = TEAL_LIGHT if level == 2 else LINE
    bar = Table([[""]], colWidths=[3.2], rowHeights=[max(13.0, TYPE.h2 * 1.1)])
    bar.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    table = Table([[paragraph, bar]], colWidths=[_FRAME_W - 6, 6], hAlign="RIGHT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def _card(block: design.BlockStyle, rows: list[Flowable], *, width: float) -> Table:
    """One Gamas card: tinted panel, deeper frame, accent edge, rounded corners."""
    inner_width = width - 24
    inner = Table([[row] for row in rows], colWidths=[inner_width])
    inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    outer = Table([[inner]], colWidths=[width])
    outer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _c(block.fill)),
        ("BOX", (0, 0), (-1, -1), 0.7, _c(block.border)),
        ("LINEAFTER", (0, 0), (0, -1), 3.4, _c(block.accent)),
        ("ROUNDEDCORNERS", [5, 5, 5, 5]),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return outer


def _definition_card(term: str, term_en: str, definition: str, styles) -> Flowable:
    """Label, bold term (with its Latin name), then the definition.

    The definition is its own paragraph rather than bracketed after the term:
    a parenthetical that wraps across two lines leaves its opening bracket
    orphaned at the visual start of the second line, which looks like a typo.
    Word's version of this card already separates the two.
    """
    rows: list[Flowable] = [
        _rtl(design.DEFINITION.label, styles["card_label"], _FRAME_W - 26),
        _mixed(f"{term} ({term_en})" if term_en else term,
               styles["card_term"], _FRAME_W - 26),
    ]
    if definition:
        rows.append(_rtl(definition, styles["card_body"], _FRAME_W - 26))
    return _card(design.DEFINITION, rows, width=_FRAME_W)


def _example_card(example: str, styles) -> Flowable:
    return _card(
        design.EXAMPLE,
        [
            _rtl(design.EXAMPLE.label, styles["card_label"], _FRAME_W - 26),
            _rtl(example, styles["card_body"], _FRAME_W - 26),
        ],
        width=_FRAME_W,
    )


def _formula_card(formula: str, styles) -> Flowable:
    return _card(
        design.FORMULA,
        [
            _rtl(design.FORMULA.label, styles["card_label"], _FRAME_W - 26),
            _ltr(formula, styles["formula"], _FRAME_W - 26),
        ],
        width=_FRAME_W,
    )


def _high_yield_card(label: str, items: list[str], styles) -> Flowable:
    """The dark high-yield card: navy panel, cyan label, cream text."""
    rows: list[Flowable] = [_rtl(label, styles["card_label_dark"], _FRAME_W - 26)]
    for item in items:
        rows.append(_rtl(item, styles["card_item_dark"], _FRAME_W - 34, marker="•"))
    return _card(design.HIGH_YIELD, rows, width=_FRAME_W)


def _tip_card(kind: str, text: str, styles) -> Flowable:
    block = design.tip_style(kind)
    dark = block is design.TIPS["آزمونی"]
    label_style = styles["card_label_dark"] if dark else styles["card_label"]
    body_style = styles["card_body_dark"] if dark else styles["card_body"]
    return _card(
        block,
        [
            _rtl(block.label, label_style, _FRAME_W - 26),
            _rtl(text, body_style, _FRAME_W - 26),
        ],
        width=_FRAME_W,
    )


def _callout_card(kind: str, text: str, styles) -> Flowable:
    block = design.callout_style(kind)
    return _card(
        block,
        [_rtl(f"{block.label}: ", styles["card_label"], _FRAME_W - 26),
         _rtl(text, styles["card_body"], _FRAME_W - 26)],
        width=_FRAME_W,
    )


def _steps_table(steps: list[str], styles) -> Table:
    """Numbered steps in one panel; the badge leads each row."""
    rows: list[list] = []
    for index, step in enumerate(steps, start=1):
        rows.append([
            _rtl(step, styles["card_body"], _FRAME_W - 52),
            _StepBadge(index),
        ])
    table = Table(rows, colWidths=[_FRAME_W - 34, 26], hAlign="CENTER")
    commands: list = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), _c(PAL.note_fill)),
        ("BOX", (0, 0), (-1, -1), 0.7, _c(PAL.note_border)),
        ("ROUNDEDCORNERS", [5, 5, 5, 5]),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    table.setStyle(TableStyle(commands))
    return table


def _column_widths(headers: list[str], rows: list[list[str]]) -> list[float]:
    """Column widths for an RTL table, sized from the text that must fit.

    A character count says nothing about rendered width: a short drug name in a
    bold Persian face is wider than several Latin characters. Columns start from
    a proportional share of the frame, then any column whose longest single word
    would still be clipped claims the width it needs, taken from the roomiest
    column. That is what keeps a name like "سیتاگلیپتین" on one line instead of
    breaking it with a stray final letter.
    """
    pad = 14.0
    regular = _measure(REGULAR_NAME, TYPE.table)
    bold = _measure(BOLD_NAME, TYPE.table)
    count = len(headers)

    weights: list[float] = []
    for index in range(count):
        longest = len(headers[index] or "")
        for row in rows:
            longest = max(longest, min(len(row[index] or ""), 90))
        weights.append(float(min(max(longest + 2, 10), 52)))
    total = sum(weights) or 1.0
    widths = [_FRAME_W * weight / total for weight in weights]

    needs: list[float] = []
    for index in range(count):
        cells = [headers[index] or ""] + [row[index] or "" for row in rows]
        widest = 0.0
        for cell in cells:
            measure = bold if index == 0 else regular
            for word in cell.split():
                widest = max(widest, measure(word))
        needs.append(min(widest + pad, _FRAME_W * 0.42))

    for _ in range(count):
        short = [i for i in range(count) if widths[i] < needs[i] - 0.5]
        if not short:
            break
        index = max(short, key=lambda i: needs[i] - widths[i])
        deficit = needs[index] - widths[index]
        donors = [i for i in range(count) if i != index and widths[i] - needs[i] > 6]
        if not donors:
            break
        pool = sum(widths[i] - needs[i] for i in donors)
        for i in donors:
            widths[i] -= deficit * (widths[i] - needs[i]) / pool
        widths[index] = needs[index]

    scale = _FRAME_W / sum(widths)
    return [width * scale for width in widths]


def _table_flowable(table: NoteTable, styles) -> Table:
    """An RTL content table: first logical column on the right, navy header.

    The caption is the first (spanning) row rather than a separate paragraph:
    a caption alone at the foot of a page, with its table overleaf, tells the
    reader nothing. As a repeating row the title travels with the table across
    a page break.
    """
    headers = list(table.headers) or [""]
    rows = [list(row) + [""] * (len(headers) - len(row)) for row in table.rows]
    width = len(headers)

    col_widths = _column_widths(headers, rows)

    order = list(range(width - 1, -1, -1))
    data: list[list[Paragraph]] = []
    if table.caption:
        data.append([_rtl(table.caption, styles["caption"], _FRAME_W - 14)])
        data[-1].extend([""] * (width - 1))
    # Cell text is measured unshaped, while shaping can widen a line; the slack
    # keeps a long name or a Latin term from being split mid-word inside a cell.
    def cell_width(index: int) -> float:
        return max(20.0, col_widths[index] * 0.92 - 12)

    header_cells = [
        _mixed(headers[index] or " ", styles["cell_head"], cell_width(index))
        for index in order
    ]
    data.append(header_cells)
    for row in rows:
        data.append([
            _mixed(
                row[index] or " ",
                styles["cell_strong"] if position == 0 else styles["cell"],
                cell_width(index),
            )
            for position, index in enumerate(order)
        ])

    body_start = 2 if table.caption else 1
    flowable = Table(
        data,
        colWidths=[col_widths[index] for index in order],
        repeatRows=body_start,
    )
    commands: list = [
        ("BACKGROUND", (0, body_start - 1), (-1, body_start - 1), NAVY),
        ("TEXTCOLOR", (0, body_start - 1), (-1, body_start - 1), WHITE),
        ("GRID", (0, body_start - 1), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    if table.caption:
        commands.extend([
            ("SPAN", (0, 0), (-1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), CREAM),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, CYAN),
        ])
    if rows:
        commands.append(("ROWBACKGROUNDS", (0, body_start), (-1, -1), [WHITE, ZEBRA]))
        commands.append(("BOX", (0, body_start), (-1, -1), 0.6, _c(PAL.border)))
    flowable.setStyle(TableStyle(commands))
    return flowable


def _glossary_table(entries, styles) -> Table:
    """Glossary: term (with its English equivalent) and definition."""
    data: list[list] = [[
        _rtl("توضیح", styles["cell_head"], _FRAME_W * 0.55 - 14),
        _rtl("اصطلاح", styles["cell_head"], _FRAME_W * 0.45 - 14),
    ]]
    for entry in entries:
        term = entry.term if not entry.term_en else f"{entry.term} ({entry.term_en})"
        data.append([
            _rtl(entry.definition, styles["cell"], _FRAME_W * 0.55 - 16),
            _mixed(term, styles["cell_strong"], _FRAME_W * 0.45 - 16),
        ])
    table = Table(data, colWidths=[_FRAME_W * 0.55, _FRAME_W * 0.45], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ZEBRA]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _asset_image(key: str, *, width: float, height: float | None = None) -> RLImage | None:
    asset = optional_asset(key)
    if asset is None:
        return None
    try:
        if height is None:
            height = width / asset.aspect if asset.aspect else width
        image = RLImage(str(asset.path), width=width, height=height)
        image.hAlign = "CENTER"
        return image
    except Exception:
        logger.warning("booklet asset %s could not be placed in the PDF", key, exc_info=True)
        return None


def _section_flowables(section: NoteSection, index: int, styles) -> list[Flowable]:
    out: list[Flowable] = []
    eyebrow = _rtl(
        f"فصل {design.chapter_number(index)}", styles["eyebrow"], _FRAME_W
    )
    out.append(KeepTogether([Spacer(1, SPACE.section), eyebrow, _banner(section.heading, styles)]))
    out.append(Spacer(1, 6))
    out.append(_EcgRule(96, height=7, color=TEAL_LIGHT))
    out.append(Spacer(1, 4))
    if section.source_locators:
        out.append(_rtl(
            "منبع: " + "، ".join(section.source_locators), styles["note_small"], _FRAME_W
        ))
        out.append(Spacer(1, 3))

    for paragraph_text in section.paragraphs:
        out.append(_rtl(paragraph_text, styles["body"], _FRAME_W))
    for definition in section.definitions:
        out.append(_definition_card(
            definition.term, definition.term_en, definition.definition, styles
        ))
        out.append(Spacer(1, 6))
    if section.bullets:
        bullets = [
            _rtl(bullet, styles["bullet"], _FRAME_W - 16, marker="•")
            for bullet in section.bullets
        ]
        out.append(KeepTogether(bullets) if len(bullets) <= 8 else bullets)

    # A subsection heading travels with the first block it introduces: a heading
    # alone at the foot of a page, with its card overleaf, is an orphan.
    if section.examples:
        cards: list[Flowable] = []
        for example in section.examples:
            cards.extend([_example_card(example, styles), Spacer(1, 6)])
        out.append(KeepTogether([_subheading("مثال‌ها", styles), cards[0]]))
        out.extend(cards[1:])

    if section.steps:
        out.append(KeepTogether(
            [_subheading("مراحل انجام", styles), _steps_table(list(section.steps), styles)]
        ))
        out.append(Spacer(1, 8))

    if section.formulas:
        cards = []
        for formula in section.formulas:
            cards.extend([_formula_card(formula, styles), Spacer(1, 6)])
        out.append(KeepTogether([_subheading("فرمول‌ها", styles), cards[0]]))
        out.extend(cards[1:])

    if section.key_points:
        out.append(_high_yield_card("نکته‌های کلیدی این بخش", list(section.key_points), styles))
        out.append(Spacer(1, 8))

    for callout in section.callouts:
        out.append(_callout_card(callout.kind, callout.text, styles))
        out.append(Spacer(1, 6))

    for table_model in section.all_tables:
        out.append(_table_flowable(table_model, styles))
        out.append(Spacer(1, 10))
    return out


def _guide_flowables(styles) -> list[Flowable]:
    rows = [
        _rtl("پیش از مطالعه: «در یک نگاه» را بخوانید تا نقشهٔ کار روشن شود.",
             styles["card_item"], _FRAME_W - 30),
        _rtl("هنگام مطالعه: هر فصل را با جدول‌ها، مثال‌ها و کادرهای کلیدی جلو ببرید.",
             styles["card_item"], _FRAME_W - 30),
        _rtl("پس از مطالعه: پرسش‌های مرور را پاسخ دهید و اصطلاح‌های تازه را مرور کنید.",
             styles["card_item"], _FRAME_W - 30),
    ]
    return [
        *_display_heading(design.LABELS["guide"], styles),
        _card(design.GUIDE, rows, width=_FRAME_W),
        Spacer(1, SPACE.lg),
    ]


def _glance_flowables(notes: StructuredNotes, styles) -> list[Flowable]:
    lines = [f"موضوع: {notes.display_title}"]
    headings = [section.heading for section in notes.sections if section.heading]
    if headings:
        lines.append(f"تعداد فصل‌ها: {to_persian_digits(len(headings))}")
        route = headings[:5]
        suffix = " · …" if len(headings) > len(route) else ""
        lines.append("مسیر مطالعه: " + " · ".join(route) + suffix)
    rows: list[Flowable] = []
    rows.extend(_rtl(line, styles["card_item"], _FRAME_W - 30) for line in lines)
    return [
        *_display_heading(design.LABELS["glance"], styles),
        _card(design.GLANCE, rows, width=_FRAME_W),
        Spacer(1, SPACE.lg),
    ]


def _body_flowables(notes: StructuredNotes, styles) -> list[Flowable]:
    out: list[Flowable] = []
    out.extend(_guide_flowables(styles))
    out.extend(_glance_flowables(notes, styles))

    if notes.learning_objectives:
        out.extend(_display_heading(design.LABELS["objectives"], styles))
        out.append(_high_yield_card("آنچه این جزوه پوشش می‌دهد",
                                    list(notes.learning_objectives), styles))
        out.append(Spacer(1, SPACE.lg))

    if notes.summary:
        out.extend(_display_heading(design.LABELS["summary"], styles))
        out.append(_card(
            design.SUMMARY,
            [_rtl(notes.summary, styles["card_body"], _FRAME_W - 26)],
            width=_FRAME_W,
        ))
        out.append(Spacer(1, SPACE.lg))

    for index, section in enumerate(notes.sections, start=1):
        out.extend(_section_flowables(section, index, styles))

    if notes.key_points:
        out.append(KeepTogether([
            Spacer(1, SPACE.section),
            _banner(design.LABELS["key_points"], styles),
            Spacer(1, 5),
            _high_yield_card("مهم‌ترین نکته‌های این جزوه", list(notes.key_points), styles),
        ]))

    if notes.practical_tips:
        out.append(KeepTogether([Spacer(1, SPACE.section),
                                 *_display_heading(design.LABELS["tips"], styles)]))
        for tip in notes.practical_tips:
            out.append(Spacer(1, 5))
            out.append(_tip_card(tip.kind, tip.text, styles))

    if notes.review_questions:
        out.append(KeepTogether([Spacer(1, SPACE.section),
                                 *_display_heading(design.LABELS["review"], styles)]))
        out.append(_steps_table(list(notes.review_questions), styles))

    if notes.common_mistakes:
        out.append(KeepTogether([Spacer(1, SPACE.section),
                                 *_display_heading("خطاها و دام‌های رایج", styles)]))
        out.append(_high_yield_card(
            "هشدارها و استثناهای پشتیبانی‌شده توسط منبع",
            list(notes.common_mistakes),
            styles,
        ))

    if notes.glossary:
        out.append(KeepTogether([Spacer(1, SPACE.section),
                                 _banner(design.LABELS["glossary"], styles),
                                 Spacer(1, 5)]))
        out.append(_glossary_table(list(notes.glossary), styles))

    if notes.final_review:
        out.append(KeepTogether([
            Spacer(1, SPACE.section),
            _banner("مرور نهایی", styles),
            Spacer(1, 5),
            _high_yield_card("نکته‌های ضروری برای مرور پایانی", list(notes.final_review), styles),
        ]))
    return out


def _cover_flowables(
    notes: StructuredNotes, meta: DocumentMeta, styles, *,
    duration_seconds: int | None, session_number: int | None,
) -> list[Flowable]:
    # Audio duration and session/job labels remain operational data. They are
    # deliberately accepted for caller compatibility but never rendered.
    _ = (meta, duration_seconds, session_number)
    topic = topic_for(booklet_text(notes))
    meta_lines: list[str] = []

    panel_inner: list[Flowable] = [
        _rtl("جزوهٔ جلسه", styles["cover_label"], _FRAME_W * 0.78),
        _rtl(notes.display_title, styles["cover_title"], _FRAME_W * 0.78),
    ]
    if notes.subtitle:
        panel_inner.append(_rtl(notes.subtitle, styles["cover_meta"], _FRAME_W * 0.78))
    panel_inner.extend(_rtl(line, styles["cover_meta"], _FRAME_W * 0.78) for line in meta_lines)
    panel_table = Table([[item] for item in panel_inner], colWidths=[_FRAME_W * 0.78])
    panel_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    panel = Table([[panel_table]], colWidths=[_FRAME_W * 0.9])
    panel.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("ROUNDEDCORNERS", [10, 10, 10, 10]),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (-1, -1), 15),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))

    out: list[Flowable] = [
        _rtl("به نام خدا", styles["cover_god"], _FRAME_W),
        _asset_image("brand/monogram", width=2.3 * cm, height=2.3 * cm) or Spacer(1, 1),
        Spacer(1, 4),
        _rtl("GAMAS", styles["cover_brand"], _FRAME_W),
        Spacer(1, 9),
        _rtl("SMART STUDY ASSISTANT", styles["cover_sub"], _FRAME_W),
        Spacer(1, 8),
        panel,
        Spacer(1, 10),
    ]
    hero = _asset_image(topic.cover, width=_FRAME_W)
    if hero is not None:
        out.append(hero)
        out.append(Spacer(1, 5))

    out.append(_card(
        design.QUOTE,
        [_rtl(f"«{QUOTE_TEXT}»", styles["quote"], _FRAME_W - 26)],
        width=_FRAME_W,
    ))
    out.append(Spacer(1, 8))
    out.append(_rtl("Gamas · دستیار مطالعهٔ هوشمند", styles["attribution"], _FRAME_W))
    return out


#: Quoted verbatim from the Word cover; never reworded or normalized.
QUOTE_TEXT = "دانش اگر در ثریا باشد مردمی از سرزمین پارس به آن دست خواهند یافت"


def _final_flowables(meta: DocumentMeta, styles) -> list[Flowable]:
    """No processing appendix: the educational final review is the booklet ending."""
    _ = (meta, styles)
    return []


# ---------------------------------------------------------------------------
# Page furniture
# ---------------------------------------------------------------------------


def _cover_background(canv) -> None:
    """The cover's paper field, frame, teal curve and ECG motif (vectors)."""
    canv.saveState()
    canv.setFillColor(CREAM)
    canv.rect(0, 0, _PAGE_W, _PAGE_H, stroke=0, fill=1)
    # One restrained teal sweep in the upper third, behind the title panel and
    # artwork — never behind the quotation or the closing line.
    canv.setStrokeColor(_c(PAL.teal_400))
    canv.setLineWidth(1.0)
    path = canv.beginPath()
    path.moveTo(-10, _PAGE_H * 0.62)
    path.curveTo(_PAGE_W * 0.28, _PAGE_H * 0.80,
                 _PAGE_W * 0.62, _PAGE_H * 0.55,
                 _PAGE_W + 10, _PAGE_H * 0.74)
    canv.drawPath(path, stroke=1, fill=0)
    canv.setStrokeColor(_c(PAL.cyan_500))
    canv.setLineWidth(0.7)
    path = canv.beginPath()
    path.moveTo(-10, _PAGE_H * 0.575)
    path.curveTo(_PAGE_W * 0.30, _PAGE_H * 0.74,
                 _PAGE_W * 0.64, _PAGE_H * 0.505,
                 _PAGE_W + 10, _PAGE_H * 0.695)
    canv.drawPath(path, stroke=1, fill=0)
    # The navy frame, then its cyan inner hairline.
    canv.setStrokeColor(NAVY)
    canv.setLineWidth(1.2)
    canv.rect(FRAME.frame_inset, FRAME.frame_inset,
              _PAGE_W - 2 * FRAME.frame_inset, _PAGE_H - 2 * FRAME.frame_inset)
    canv.setStrokeColor(_c(PAL.cyan_500))
    canv.setLineWidth(0.35)
    inset = FRAME.frame_inset + 5
    canv.rect(inset, inset, _PAGE_W - 2 * inset, _PAGE_H - 2 * inset)
    canv.restoreState()


def _page_furniture(title: str):
    """The ``onPage`` callback: paper on page 1, header/footer on the rest."""
    regular, _bold = register_fonts()

    def on_page(canv, _doc) -> None:
        page = canv.getPageNumber()
        canv.setTitle(title)
        canv.setAuthor("Gamas Bot")
        if page == 1:
            _cover_background(canv)
            return
        canv.saveState()
        canv.setStrokeColor(_c(PAL.line))
        canv.setLineWidth(0.6)
        canv.rect(FRAME.frame_inset, FRAME.frame_inset,
                  _PAGE_W - 2 * FRAME.frame_inset, _PAGE_H - 2 * FRAME.frame_inset)

        # Running header: brand at the Far Right (start), title at the other end.
        baseline = FRAME.header_baseline
        canv.setFillColor(TEAL)
        canv.setFont(BOLD_NAME, 8.4)
        canv.drawString(_MARGIN_LR, baseline, "GAMAS")
        canv.setFillColor(MUTED)
        canv.setFont(regular, 8.4)
        canv.drawRightString(_PAGE_W - _MARGIN_LR, baseline, visual(title[:44]))
        canv.setStrokeColor(_c(PAL.cyan_500))
        canv.setLineWidth(0.5)
        canv.line(_MARGIN_LR, baseline - 5, _PAGE_W - _MARGIN_LR, baseline - 5)

        # Footer: a small ECG motif, the brand, and the real page number.
        footer_y = FRAME.footer_baseline
        canv.setStrokeColor(_c(PAL.line))
        canv.setLineWidth(0.5)
        canv.line(_MARGIN_LR, footer_y + 9, _PAGE_W - _MARGIN_LR, footer_y + 9)
        canv.setFillColor(MUTED)
        canv.setFont(regular, 8.4)
        number = page - 1
        canv.drawCentredString(
            _PAGE_W / 2, footer_y,
            visual(f"GAMAS  ·  صفحهٔ {to_persian_digits(number)}"),
        )
        canv.setStrokeColor(_c(PAL.teal_400))
        canv.setLineWidth(0.7)
        path = canv.beginPath()
        base = footer_y + 2
        path.moveTo(_MARGIN_LR, base)
        path.lineTo(_MARGIN_LR + 16, base)
        path.lineTo(_MARGIN_LR + 19, base + 3)
        path.lineTo(_MARGIN_LR + 22, base - 4)
        path.lineTo(_MARGIN_LR + 25, base + 1)
        path.lineTo(_MARGIN_LR + 40, base)
        canv.drawPath(path, stroke=1, fill=0)
        canv.restoreState()

    return on_page


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_notes_pdf(
    notes: StructuredNotes,
    *,
    meta: DocumentMeta,
    duration_seconds: int | None = None,
    session_number: int | None = None,
) -> bytes:
    """Render validated structured notes as the Gamas PDF booklet.

    Deterministic: invariant canvas, no wall clock beyond ``meta.created_at``,
    and the same notes always yield the same bytes.
    """
    styles = _styles()
    title = notes.display_title or "جزوهٔ کلاس"
    register_fonts()

    story: list[Flowable] = _cover_flowables(
        notes, meta, styles,
        duration_seconds=duration_seconds, session_number=session_number,
    )
    story.append(PageBreak())
    story.extend(_body_flowables(notes, styles))
    story.extend(_final_flowables(meta, styles))

    buffer = io.BytesIO()
    frame = Frame(
        _MARGIN_LR, _MARGIN_BOTTOM, _FRAME_W, _FRAME_H,
        id="gamas_main",
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    template = PageTemplate(id="gamas", frames=[frame], onPage=_page_furniture(title))
    document = BaseDocTemplate(
        buffer, pagesize=(_PAGE_W, _PAGE_H), title=title, author="Gamas Bot",
    )
    document.addPageTemplates([template])
    document.build(story, canvasmaker=_InvariantCanvas)
    return buffer.getvalue()


def notes_pdf_filename(notes: StructuredNotes, reference: str) -> str:
    from .docx_export import sanitize_filename_part

    title_part = sanitize_filename_part(notes.display_title)
    name = f"جزوه - {title_part}" if title_part else "جزوه"
    return f"{name} - {reference}.pdf"
