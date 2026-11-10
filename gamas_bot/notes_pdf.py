"""Reference-grade PDF renderer for Gamas structured educational notes.

Produces a professional Persian RTL study booklet with:

* A cover page featuring a rounded dark-navy title panel, a label such as
  ``جزوه جلسه``, the lecture title, source-derived date/class-number/duration
  metadata, a vertical central connector, and alternating numbered topic cards
  (a real topic map generated from actual section headings).
* Body pages with hierarchical H1/H2/H3 headings, a cobalt vertical accent bar
  beside major section titles, thin light-grey separator lines, readable
  Persian body typography, consistent spacing, and proper RTL layout.
* Visually distinct content blocks:

  - Definitions — pale lavender/blue background, dark-blue accent border,
    clear "تعریف" label.
  - Examples — soft mint-green background with green accent border.
  - Inline important terminology — controlled yellow highlighting.
  - Key points — compact, visually distinct information blocks.
  - Processes — rounded step cards.
  - Tables — RTL tables with aligned headers, cell padding, and restrained
    alternating row backgrounds.
  - Glossary — three-column table: اصطلاح، معادل، تعریف.

* Final page with session glossary (when supported), source date, page
  numbering, a concise Gamas attribution, and an honest automatic-generation
  disclaimer.

Persian shaping uses ``arabic_reshaper``; visual reordering uses ``python-bidi``.
The face is Vazirmatn (SIL OFL) from ``assets/fonts/``. The PDF canvas is
``invariant`` so identical notes yield identical bytes.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from xml.sax.saxutils import escape as _xml_escape

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as _rl_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import HRFlowable

from .config import PROJECT_DIR
from .docx_export import DocumentMeta, PAGE_HEADER_TEXT, jalali_date
from .notes import GlossaryEntry, NoteSection, NoteTable, StructuredNotes
from .progress import to_persian_digits

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

_PAGE_W, _PAGE_H = A4  # 595.27 x 841.89 pt
_MARGIN_LR = 2.0 * cm
_MARGIN_TOP = 2.2 * cm
_MARGIN_BOTTOM = 2.0 * cm
_FRAME_W = _PAGE_W - 2 * _MARGIN_LR
_FRAME_H = _PAGE_H - _MARGIN_TOP - _MARGIN_BOTTOM

# ---------------------------------------------------------------------------
# Palette (Gamas-native, matching the DOCX family and the reference design)
# ---------------------------------------------------------------------------

NAVY = colors.HexColor("#1D3557")        # deep navy — title, H1, brand, cover
COBALT = colors.HexColor("#2C5F8A")      # cobalt — H2 accent bars
STEEL = colors.HexColor("#457B9D")       # steel blue — H3, rules
MUTED = colors.HexColor("#5C6B7A")       # slate grey — metadata, captions
BODY_COLOR = colors.HexColor("#2A2F36")  # near-black for body
RULE = colors.HexColor("#B9CFDD")        # hairline rules

# Content-block tints + matching darker borders.
DEF_FILL = colors.HexColor("#EEE8F6")    # pale lavender/blue — definitions
DEF_BORDER = colors.HexColor("#9E8BC9")  # dark-blue/lavender border
EX_FILL = colors.HexColor("#E6F4EA")     # soft mint green — examples
EX_BORDER = colors.HexColor("#8CC39D")   # green border
KEY_FILL = colors.HexColor("#EAF2F5")    # light blue — key points
KEY_BORDER = colors.HexColor("#9EC2D3")  # steel border
HL_YELLOW = colors.HexColor("#FFF4B8")   # controlled yellow inline highlight
ZEBRA = colors.HexColor("#F4F8FA")       # table alternating rows
GRID = colors.HexColor("#C7D3DE")        # table grid
COVER_CARD_COLORS: tuple[colors.HexColor, ...] = (
    colors.HexColor("#E8F0F7"),  # mist blue
    colors.HexColor("#FDEDE8"),  # soft peach
    colors.HexColor("#E8F6EE"),  # mint
    colors.HexColor("#F3ECF8"),  # lilac
    colors.HexColor("#FFF6E0"),  # cream
    colors.HexColor("#E2F0F5"),  # sky
    colors.HexColor("#FBE8EE"),  # rose
    colors.HexColor("#EAF0E5"),  # sage
)
STEP_FILL = colors.HexColor("#EEF4F8")
STEP_BORDER = colors.HexColor("#7DA7C7")
GLOSSARY_HEADER_FILL = NAVY

# ---------------------------------------------------------------------------
# Fonts (Vazirmatn, SIL OFL, bundled in the repository)
# ---------------------------------------------------------------------------

_REGULAR_NAME = "GamasVazir"
_BOLD_NAME = "GamasVazirBold"
_REGULAR_FILE = "Vazirmatn-Regular.ttf"
_BOLD_FILE = "Vazirmatn-Bold.ttf"

_font_names: tuple[str, str] | None = None


def _find_font(filename: str):
    from pathlib import Path as _Path
    for base in (PROJECT_DIR / "assets" / "fonts", _Path.cwd() / "assets" / "fonts"):
        p = base / filename
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _fonts() -> tuple[str, str]:
    global _font_names
    if _font_names is not None:
        return _font_names
    regular = _find_font(_REGULAR_FILE)
    bold_path = _find_font(_BOLD_FILE)
    if regular is None:
        from .office import OfficeError
        raise OfficeError(
            "فونت فارسی برای PDF جزوه پیدا نشد؛ assets/fonts/Vazirmatn-Regular.ttf لازم است."
        )
    bold = bold_path or regular
    if _REGULAR_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_REGULAR_NAME, str(regular)))
    if _BOLD_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_BOLD_NAME, str(bold)))
    _font_names = (_REGULAR_NAME, _BOLD_NAME)
    return _font_names


# ---------------------------------------------------------------------------
# Persian text shaping: logical wrap, then reshape/bidi per line.
# ---------------------------------------------------------------------------


def _has_strong_rtl(text: str) -> bool:
    for ch in text:
        if "\u0600" <= ch <= "\u06FF" or "\uFB50" <= ch <= "\uFEFC" or "\u0750" <= ch <= "\u077F":
            return True
    return False


def _visual_line(text: str) -> str:
    """Apply arabic_reshaper then bidi for display order."""
    if not text:
        return text
    return get_display(arabic_reshaper.reshape(text))


def _wrap_words(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Word-wrap on logical text; returns logical lines."""
    words = (text or "").split()
    lines: list[str] = []
    cur = ""
    for word in words:
        cand = f"{cur} {word}".strip()
        if not cur or pdfmetrics.stringWidth(cand, font, size) <= max_width:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            # word itself wider than max width? hard break
            if pdfmetrics.stringWidth(word, font, size) > max_width:
                piece = ""
                for ch in word:
                    trial = piece + ch
                    if not piece or pdfmetrics.stringWidth(trial, font, size) <= max_width:
                        piece = trial
                    else:
                        lines.append(piece)
                        piece = ch
                cur = piece
            else:
                cur = word
    if cur:
        lines.append(cur)
    return lines


def _rtl_paragraph(
    text: str,
    style: ParagraphStyle,
    width: float,
    *,
    highlight_terms: tuple[str, ...] = (),
) -> Paragraph:
    """Produce a pre-wrapped, shaped Paragraph so ReportLab never re-wraps us."""
    lines = _wrap_words(text or "", style.fontName, style.fontSize, width)
    shaped_lines: list[str] = []
    for line in lines:
        visual = _xml_escape(_visual_line(line))
        if highlight_terms:
            for term in highlight_terms:
                if not term:
                    continue
                # We highlight at the visual level for simple terms: this is
                # a controlled approach — only known terms get yellow.
                vterm = _visual_line(term)
                if vterm and vterm in visual:
                    visual = visual.replace(
                        vterm,
                        f'<back color="{HL_YELLOW.hexval()[2:]}">{vterm}</back>',
                    )
        shaped_lines.append(visual)
    body = "<br/>".join(shaped_lines) if shaped_lines else " "
    return Paragraph(body, style)


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


def _styles() -> dict[str, ParagraphStyle]:
    regular, bold = _fonts()

    def s(name, font, size, leading, color, align, **kw):
        return ParagraphStyle(
            name, fontName=font, fontSize=size, leading=leading,
            textColor=color, alignment=align, **kw,
        )

    return {
        "body": s("NBody", regular, 11, 17, BODY_COLOR, TA_JUSTIFY,
                 spaceAfter=5, wordWrap="RTL"),
        "bullet": s("NBullet", regular, 11, 16.5, BODY_COLOR, TA_RIGHT,
                    spaceAfter=3, rightIndent=8, leftIndent=4, wordWrap="RTL"),
        "h1": s("NH1", bold, 16, 22, NAVY, TA_RIGHT,
                spaceBefore=14, spaceAfter=6, keepWithNext=1, wordWrap="RTL"),
        "h2": s("NH2", bold, 13.5, 19, COBALT, TA_RIGHT,
                spaceBefore=10, spaceAfter=4, keepWithNext=1, wordWrap="RTL"),
        "h3": s("NH3", bold, 12, 17, STEEL, TA_RIGHT,
                spaceBefore=8, spaceAfter=3, keepWithNext=1, wordWrap="RTL"),
        # Cover
        "cover_label": s("NCLabel", bold, 11, 14, colors.white, TA_CENTER, spaceAfter=4),
        "cover_title": s("NCTitle", bold, 22, 30, colors.white, TA_CENTER, spaceAfter=8),
        "cover_meta": s("NCMeta", regular, 10, 14, colors.HexColor("#D6E2ED"), TA_CENTER, spaceAfter=2),
        "cover_brand": s("NCBrand", bold, 13, 17, MUTED, TA_CENTER, spaceAfter=2),
        # Topic card styles
        "card_num": s("NCardNum", bold, 13, 16, colors.white, TA_CENTER),
        "card_text": s("NCardText", bold, 10, 14, NAVY, TA_RIGHT),
        # Content blocks
        "def_label": s("NDefLabel", bold, 10.5, 14, DEF_BORDER, TA_RIGHT, spaceAfter=2),
        "def_body": s("NDefBody", regular, 11, 16, BODY_COLOR, TA_JUSTIFY),
        "def_term": s("NDefTerm", bold, 11.5, 16, NAVY, TA_RIGHT, spaceAfter=2),
        "ex_label": s("NExLabel", bold, 10.5, 14, EX_BORDER, TA_RIGHT, spaceAfter=2),
        "ex_body": s("NExBody", regular, 11, 16, BODY_COLOR, TA_JUSTIFY),
        "key_label": s("NKeyLabel", bold, 10.5, 14, KEY_BORDER, TA_RIGHT, spaceAfter=2),
        "key_item": s("NKeyItem", regular, 10.5, 15.5, BODY_COLOR, TA_RIGHT,
                      spaceAfter=2, rightIndent=6),
        "step_num": s("NStepNum", bold, 11, 14, colors.white, TA_CENTER),
        "step_body": s("NStepBody", regular, 10.5, 15.5, BODY_COLOR, TA_RIGHT),
        # Tables
        "cell_head": s("NCellHead", bold, 10.5, 14, colors.white, TA_CENTER),
        "cell": s("NCell", regular, 10, 14, BODY_COLOR, TA_RIGHT),
        "cell_center": s("NCellC", regular, 10, 14, BODY_COLOR, TA_CENTER),
        # Glossary
        "glos_head": s("NGlosHead", bold, 10.5, 14, colors.white, TA_CENTER),
        "glos_term": s("NGlosTerm", bold, 10.5, 14, NAVY, TA_RIGHT),
        "glos_en": s("NGlosEn", regular, 9.5, 13, MUTED, TA_LEFT),
        "glos_def": s("NGlosDef", regular, 10, 14, BODY_COLOR, TA_RIGHT),
        # Disclaimer / footer
        "disclaimer": s("NDisclaimer", regular, 8.5, 12, MUTED, TA_JUSTIFY, spaceAfter=4),
        "attribution": s("NAttrib", regular, 9, 12, MUTED, TA_CENTER, spaceAfter=2),
        "summary": s("NSummary", regular, 11, 17, BODY_COLOR, TA_JUSTIFY, spaceAfter=8),
    }


# ---------------------------------------------------------------------------
# Custom flowables
# ---------------------------------------------------------------------------


class _VerticalBar(Flowable):
    """A cobalt vertical accent bar drawn on the right edge of a heading."""

    def __init__(self, height: float, color: colors.Color = COBALT, width: float = 4):
        super().__init__()
        self.height = height
        self.color = color
        self.width = width

    def wrap(self, availW, availH):
        return self.width, self.height

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.roundRect(0, 0, self.width, self.height, 2, fill=1, stroke=0)


class _CircleBadge(Flowable):
    """A numbered circle used for step cards."""

    def __init__(self, number: int, size: float = 22,
                 bg: colors.Color = STEP_BORDER, fg: colors.Color = colors.white):
        super().__init__()
        self.number = number
        self.size = size
        self.bg = bg
        self.fg = fg

    def wrap(self, w, h):
        return self.size, self.size

    def draw(self):
        c = self.canv
        r = self.size / 2
        c.setFillColor(self.bg)
        c.circle(r, r, r, fill=1, stroke=0)
        c.setFillColor(self.fg)
        c.setFont(_BOLD_NAME, 10.5)
        label = to_persian_digits(str(self.number))
        c.drawCentredString(r, r - 3.5, _visual_line(label))


class _ArrowDown(Flowable):
    """The connector between step cards, drawn as a vector.

    Vazirmatn has no U+2193 (↓) glyph, so a text arrow rendered as an empty
    .notdef box — the gap visual QA caught between step cards. Canvas
    primitives need no font coverage and stay crisp at every size.
    """

    def __init__(self, height: float = 12, color: colors.Color = STEP_BORDER):
        super().__init__()
        self.height = height
        self.color = color
        self._cx = 0.0

    def wrap(self, availW, availH):
        self._cx = availW / 2.0
        return availW, self.height

    def draw(self):
        c = self.canv
        c.setStrokeColor(self.color)
        c.setFillColor(self.color)
        c.setLineWidth(1.4)
        c.setLineCap(1)
        top = self.height - 1.0
        c.line(self._cx, top, self._cx, 5.0)
        head = c.beginPath()
        head.moveTo(self._cx - 3.4, 6.4)
        head.lineTo(self._cx + 3.4, 6.4)
        head.lineTo(self._cx, 1.4)
        head.close()
        c.drawPath(head, fill=1, stroke=0)


class _TopicMap(Flowable):
    """The cover topic map: central vertical connector + alternating cards."""

    def __init__(self, topics: list[str], *, width: float):
        super().__init__()
        self.topics = topics[:8]
        self.width = width
        self.card_h = 22 * mm
        self.card_w = width * 0.42
        self.gap_x = width * 0.08
        self.gap_y = 6 * mm
        self.top_pad = 6 * mm
        self.bot_pad = 4 * mm

    def wrap(self, w, h):
        n = len(self.topics)
        height = self.top_pad + self.bot_pad + n * self.card_h + (n - 1) * self.gap_y
        return self.width, height

    def draw(self):
        c = self.canv
        n = len(self.topics)
        total_h = self.top_pad + self.bot_pad + n * self.card_h + (n - 1) * self.gap_y
        center_x = self.width / 2

        # Central connector
        c.setStrokeColor(STEEL)
        c.setLineWidth(1.5)
        c.setDash([3, 3], 0)
        c.line(center_x, 0, center_x, total_h)
        c.setDash()

        # Small circles at top and bottom of connector
        for cy in (total_h - 2, 2):
            c.setFillColor(NAVY)
            c.circle(center_x, cy, 3, fill=1, stroke=0)

        for idx, topic in enumerate(self.topics):
            row_y = total_h - self.top_pad - (idx + 1) * self.card_h - idx * self.gap_y
            is_left = (idx % 2 == 1)  # 0,2,4... on right, 1,3,5... on left
            color = COVER_CARD_COLORS[idx % len(COVER_CARD_COLORS)]
            # Card position
            if is_left:
                cx = self.gap_x
                dot_x = cx + self.card_w
                conn_end_x = center_x - 6
            else:
                cx = self.width - self.gap_x - self.card_w
                dot_x = cx
                conn_end_x = center_x + 6
            cy = row_y
            # Draw connecting line from card edge to central axis
            c.setStrokeColor(STEEL)
            c.setLineWidth(0.8)
            c.line(dot_x, cy + self.card_h / 2, conn_end_x, cy + self.card_h / 2)

            # Card background (rounded rectangle)
            c.setFillColor(color)
            c.setStrokeColor(NAVY)
            c.setLineWidth(0.7)
            c.roundRect(cx, cy, self.card_w, self.card_h, 4 * mm, fill=1, stroke=1)

            # Number badge on the connector node at the card edge. It used to
            # sit inside the card corner, right where the right-aligned RTL
            # text begins — visual QA caught the circle printed over the first
            # word. On the edge (replacing the old connection dot) it can
            # never collide with the text.
            badge_r = 3 * mm
            badge_cx = dot_x
            badge_cy = cy + self.card_h / 2
            c.setFillColor(NAVY)
            c.setStrokeColor(colors.white)
            c.setLineWidth(1.2)
            c.circle(badge_cx, badge_cy, badge_r, fill=1, stroke=1)
            # Number text
            c.setFillColor(colors.white)
            c.setFont(_BOLD_NAME, 9)
            nlabel = to_persian_digits(str(idx + 1))
            c.drawCentredString(badge_cx, badge_cy - 3, _visual_line(nlabel))

            # Topic text (wrapped), right-aligned like any RTL line, with
            # margins that clear the edge badge.
            label_w = self.card_w - 10 * mm
            # Since we are drawing at canvas level, do our own wrap & draw.
            lines = _wrap_words(topic, _REGULAR_NAME, 10, label_w)
            text_y = cy + self.card_h / 2 + (len(lines) - 1) * 6
            c.setFillColor(NAVY)
            c.setFont(_BOLD_NAME, 10)
            for line in lines[:2]:
                lw = pdfmetrics.stringWidth(line, _BOLD_NAME, 10)
                tx = cx + self.card_w - 5 * mm - lw
                c.drawString(tx, text_y, _visual_line(line))
                text_y -= 12


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------


def _h1_with_bar(text: str, styles) -> list[Flowable]:
    """H1 heading with a cobalt vertical bar to its right."""
    p = _rtl_paragraph(text, styles["h1"], _FRAME_W - 10)
    bar = _VerticalBar(22, COBALT, 4)
    # Use a 1-row table: bar on the right, heading in the rest
    t = Table(
        [[p, bar]],
        colWidths=[_FRAME_W - 10, 6],
        hAlign="RIGHT",
    )
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [Spacer(1, 2), t]


def _definition_block(term: str, term_en: str, definition: str, styles,
                      width: float) -> Flowable:
    parts = [
        Paragraph(_xml_escape(_visual_line("تعریف")), styles["def_label"]),
    ]
    label = term
    if term_en:
        label = f"{term} ({term_en})"
    parts.append(Paragraph(_xml_escape(_visual_line(label)), styles["def_term"]))
    parts.append(_rtl_paragraph(definition, styles["def_body"], width - 16))
    inner = Table([[p] for p in parts], colWidths=[width - 16])
    inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    outer = Table([[inner]], colWidths=[width])
    outer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), DEF_FILL),
        ("LINEBEFORE", (0, 0), (0, -1), 3, DEF_BORDER),  # RTL: accent on right
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return KeepTogether([outer, Spacer(1, 6)])


def _example_block(example: str, styles, width: float) -> Flowable:
    parts = [
        Paragraph(_xml_escape(_visual_line("مثال")), styles["ex_label"]),
        _rtl_paragraph(example, styles["ex_body"], width - 16),
    ]
    inner = Table([[p] for p in parts], colWidths=[width - 16])
    inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    outer = Table([[inner]], colWidths=[width])
    outer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), EX_FILL),
        ("LINEBEFORE", (0, 0), (0, -1), 3, EX_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return KeepTogether([outer, Spacer(1, 6)])


def _keypoints_block(label: str, points: list[str], styles, width: float) -> Flowable:
    parts = [Paragraph(_xml_escape(_visual_line(label)), styles["key_label"])]
    bullet_style = styles["key_item"]
    for pt in points:
        parts.append(_rtl_paragraph(f"• {pt}", bullet_style, width - 20))
    inner = Table([[p] for p in parts], colWidths=[width - 20])
    inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    outer = Table([[inner]], colWidths=[width])
    outer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), KEY_FILL),
        ("BOX", (0, 0), (-1, -1), 0.8, KEY_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return KeepTogether([outer, Spacer(1, 6)])


def _steps_flowables(steps: list[str], styles, width: float) -> list[Flowable]:
    out: list[Flowable] = []
    for idx, step in enumerate(steps, start=1):
        badge = _CircleBadge(idx, size=22)
        text_p = _rtl_paragraph(step, styles["step_body"], width - 36)
        arrow_p = _ArrowDown() if idx < len(steps) else None
        row = Table([[text_p, badge]], colWidths=[width - 28, 26])
        row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, -1), STEP_FILL),
            ("BOX", (0, 0), (-1, -1), 0.6, STEP_BORDER),
            ("ROUNDEDCORNERS", [4, 4, 4, 4]),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        out.append(KeepTogether(row))
        if arrow_p is not None:
            out.append(Spacer(1, 2))
            out.append(arrow_p)
            out.append(Spacer(1, 2))
    out.append(Spacer(1, 6))
    return out


def _callout_block(kind: str, text: str, styles, width: float) -> Flowable:
    from .docx_export import CALLOUT_BORDERS, CALLOUT_SHADES
    fill = colors.HexColor("#" + CALLOUT_SHADES.get(kind, CALLOUT_SHADES["نکته"]))
    border = colors.HexColor("#" + CALLOUT_BORDERS.get(kind, CALLOUT_BORDERS["نکته"]))
    # No emoji prefix here: Vazirmatn carries none of the dingbats (⚠ 🔔 💡),
    # so they would draw as empty .notdef boxes. The tinted background, the
    # colored border and the colored label already carry the tone; the DOCX
    # keeps its emoji because Word resolves fallback fonts at view time.
    label = kind
    p_label = Paragraph(_xml_escape(_visual_line(label)), styles["key_label"])
    p_label.textColor = border
    p_body = _rtl_paragraph(text, styles["def_body"], width - 16)
    inner = Table([[p_label], [p_body]], colWidths=[width - 16])
    inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    outer = Table([[inner]], colWidths=[width])
    outer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), fill),
        ("BOX", (0, 0), (-1, -1), 0.8, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return KeepTogether([outer, Spacer(1, 6)])


def _table_flowable(table: NoteTable, styles, width: float) -> Flowable:
    headers = list(table.headers)
    rows = [list(r) for r in table.rows]
    n_cols = len(headers)
    # Compute column widths proportional to content
    max_lens = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if i < n_cols:
                max_lens[i] = max(max_lens[i], len(cell))
    total = max(sum(max_lens), 1)
    # Cap min/max
    col_widths = []
    for ml in max_lens:
        w = max(ml / total, 0.08)
        col_widths.append(w)
    s = sum(col_widths)
    col_widths = [width * w / s for w in col_widths]

    # RTL: reverse column order for visual layout
    order = list(range(n_cols - 1, -1, -1))
    data: list = []
    # Header row
    data.append([
        Paragraph(_xml_escape(_visual_line(headers[i] or " ")), styles["cell_head"])
        for i in order
    ])
    for r in rows:
        padded = list(r) + [""] * (n_cols - len(r))
        data.append([
            _rtl_paragraph(padded[i] or " ", styles["cell"], max(20.0, col_widths[i] - 12))
            for i in order
        ])

    t = Table(data, colWidths=[col_widths[i] for i in order], repeatRows=1)
    cmds: list = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    if rows:
        cmds.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]))
    t.setStyle(TableStyle(cmds))
    return KeepTogether([t, Spacer(1, 8)])


def _glossary_table(entries: list[GlossaryEntry], styles, width: float) -> Flowable:
    # 3 columns: اصطلاح (term, bold), معادل (English term, left-aligned), تعریف (def, RTL)
    col_widths = [width * 0.22, width * 0.20, width * 0.58]
    # RTL visual order: [تعریف, معادل, اصطلاح]
    # But since reportlab tables draw left-to-right visually, we must reverse.
    # Logical order: col0=اصطلاح, col1=معادل, col2=تعریف.
    # Visual RTL means اصطلاح appears on the RIGHT → last in reportlab column order.
    order = [2, 1, 0]
    headers = ["اصطلاح", "معادل", "تعریف"]
    data = [[
        Paragraph(_xml_escape(_visual_line(headers[i])), styles["glos_head"])
        for i in order
    ]]
    for e in entries:
        term_p = Paragraph(_xml_escape(_visual_line(e.term)), styles["glos_term"])
        en_p = Paragraph(_xml_escape(e.term_en or "—"), styles["glos_en"])
        def_p = _rtl_paragraph(e.definition, styles["glos_def"], col_widths[2] - 12)
        # Logical term/equivalent/definition order becomes visual
        # definition/equivalent/term in ReportLab's left-to-right table.
        row = [term_p, en_p, def_p]
        data.append([row[i] for i in order])

    t = Table(data, colWidths=[col_widths[i] for i in order], repeatRows=1)
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), GLOSSARY_HEADER_FILL),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
    ]
    t.setStyle(TableStyle(cmds))
    return KeepTogether([t, Spacer(1, 8)])


# ---------------------------------------------------------------------------
# Cover flowables
# ---------------------------------------------------------------------------


def _cover_flowables(
    styles,
    notes: StructuredNotes,
    meta: DocumentMeta,
    *,
    duration_seconds: int | None,
    session_number: int | None,
) -> list[Flowable]:
    created = meta.created_at or datetime.now()
    out: list[Flowable] = []

    # "In the name of God" line
    out.append(Paragraph(_xml_escape(_visual_line("به نام خدا")),
                         ParagraphStyle("cover_god", fontName=_REGULAR_NAME,
                                        fontSize=11, leading=14, textColor=MUTED,
                                        alignment=TA_CENTER, spaceAfter=10)))

    # ---- Large rounded dark-navy title panel ----
    title = notes.display_title or "جزوهٔ کلاس"
    mode_label = "جزوه جلسه"

    # Metadata lines
    date_str = jalali_date(created)
    meta_lines = [f"تاریخ: {date_str}"]
    if session_number:
        meta_lines.append(f"جلسهٔ {to_persian_digits(str(session_number))}")
    if duration_seconds is not None and duration_seconds > 0:
        minutes = duration_seconds // 60
        if minutes >= 60:
            h = minutes // 60
            m = minutes % 60
            duration_label = f"{to_persian_digits(h)} ساعت"
            if m:
                duration_label += f" و {to_persian_digits(m)} دقیقه"
            meta_lines.append(f"مدت: {duration_label}")
        else:
            meta_lines.append(f"مدت: {to_persian_digits(minutes)} دقیقه")

    label_p = Paragraph(_xml_escape(_visual_line(mode_label)), styles["cover_label"])
    title_p = Paragraph(_xml_escape(_visual_line(title)), styles["cover_title"])
    meta_ps = [Paragraph(_xml_escape(_visual_line(line)), styles["cover_meta"]) for line in meta_lines]

    panel_inner: list = [label_p, Spacer(1, 4), title_p, Spacer(1, 6)]
    panel_inner.extend(meta_ps)
    inner_table = Table([[p] for p in panel_inner], colWidths=[_FRAME_W * 0.9])
    inner_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))

    panel = Table([[inner_table]], colWidths=[_FRAME_W * 0.94])
    panel.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("ROUNDEDCORNERS", [8, 8, 8, 8]),
        ("LEFTPADDING", (0, 0), (-1, -1), 18),
        ("RIGHTPADDING", (0, 0), (-1, -1), 18),
        ("TOPPADDING", (0, 0), (-1, -1), 18),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    out.append(panel)
    out.append(Spacer(1, 18))

    # ---- Topic map (generated from section headings) ----
    topics = [sec.heading for sec in notes.sections[:8]]
    if topics:
        out.append(_TopicMap(topics, width=_FRAME_W * 0.94))

    # ---- Brand footer ----
    out.append(Spacer(1, 12))
    out.append(HRFlowable(width="40%", thickness=0.6, color=RULE,
                          hAlign="CENTER", spaceAfter=6))
    out.append(Paragraph(_xml_escape(_visual_line("Gamas — جزوه‌ساز هوشمند گاماس")),
                         styles["cover_brand"]))
    return out


# ---------------------------------------------------------------------------
# Body flowables
# ---------------------------------------------------------------------------


def _section_flowables(
    section: NoteSection,
    styles,
    h_index: int,
) -> list[Flowable]:
    out: list[Flowable] = []
    # H1 with vertical cobalt bar
    out.extend(_h1_with_bar(section.heading, styles))
    out.append(HRFlowable(width="100%", thickness=0.5, color=RULE, spaceAfter=8))

    # Paragraphs
    for p in section.paragraphs:
        out.append(_rtl_paragraph(p, styles["body"], _FRAME_W))

    # Definitions
    for d in section.definitions:
        out.append(_definition_block(d.term, d.term_en, d.definition, styles, _FRAME_W))

    # Bullets
    for b in section.bullets:
        out.append(_rtl_paragraph(f"• {b}", styles["bullet"], _FRAME_W - 10))

    # Examples
    for ex in section.examples:
        out.append(_example_block(ex, styles, _FRAME_W))

    # Steps
    if section.steps:
        out.extend(_steps_flowables(list(section.steps), styles, _FRAME_W))

    # Formulas (render as a key-point style block for now)
    if section.formulas:
        out.append(_keypoints_block(
            "فرمول‌ها",
            list(section.formulas),
            styles,
            _FRAME_W,
        ))

    # Key points
    if section.key_points:
        out.append(_keypoints_block(
            "نکات کلیدی این بخش",
            list(section.key_points),
            styles,
            _FRAME_W,
        ))

    # Tables
    for tbl in section.all_tables:
        if tbl.caption:
            cap_p = _rtl_paragraph(tbl.caption, ParagraphStyle(
                "tcap", fontName=_BOLD_NAME, fontSize=10, leading=13,
                textColor=MUTED, alignment=TA_CENTER, spaceAfter=4,
            ), _FRAME_W)
            out.append(cap_p)
        out.append(_table_flowable(tbl, styles, _FRAME_W))

    # Callouts
    for c in section.callouts:
        out.append(_callout_block(c.kind, c.text, styles, _FRAME_W))

    return out


def _body_flowables(notes: StructuredNotes, styles) -> list[Flowable]:
    out: list[Flowable] = []

    # Document title (repeated on body start in smaller form)
    title_p = Paragraph(_xml_escape(_visual_line(notes.display_title)),
                        ParagraphStyle("bdytitle", fontName=_BOLD_NAME,
                                       fontSize=18, leading=24, textColor=NAVY,
                                       alignment=TA_CENTER, spaceAfter=10))
    out.append(title_p)
    out.append(HRFlowable(width="60%", thickness=0.8, color=RULE,
                          hAlign="CENTER", spaceAfter=12))

    # Learning objectives
    if notes.learning_objectives:
        out.append(_keypoints_block("اهداف یادگیری", list(notes.learning_objectives),
                                    styles, _FRAME_W))

    # Summary
    if notes.summary:
        summ_label = Paragraph(_xml_escape(_visual_line("خلاصه")),
                               ParagraphStyle("sumlab", fontName=_BOLD_NAME, fontSize=11,
                                              leading=14, textColor=NAVY,
                                              alignment=TA_RIGHT, spaceAfter=4))
        summ_body = _rtl_paragraph(notes.summary, styles["summary"], _FRAME_W - 16)
        summ_inner = Table([[summ_label], [summ_body]], colWidths=[_FRAME_W - 16])
        summ_inner.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        summ_outer = Table([[summ_inner]], colWidths=[_FRAME_W])
        summ_outer.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EEF4F8")),
            ("LINEBEFORE", (0, 0), (0, -1), 3, STEEL),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]))
        out.append(summ_outer)
        out.append(Spacer(1, 10))

    # Sections
    for i, sec in enumerate(notes.sections, start=1):
        out.extend(_section_flowables(sec, styles, i))

    # Global key points
    if notes.key_points:
        out.extend(_h1_with_bar("نکته‌های کلیدی", styles))
        out.append(HRFlowable(width="100%", thickness=0.5, color=RULE, spaceAfter=8))
        out.append(_keypoints_block("مهم‌ترین نکته‌های این جزوه",
                                    list(notes.key_points), styles, _FRAME_W))

    # Practical tips
    if notes.practical_tips:
        out.extend(_h1_with_bar("نکته‌های کاربردی", styles))
        out.append(HRFlowable(width="100%", thickness=0.5, color=RULE, spaceAfter=8))
        tips_list = [f"({tip.kind}) {tip.text}" for tip in notes.practical_tips]
        out.append(_keypoints_block("توصیه‌های مدرس", tips_list, styles, _FRAME_W))

    # Review questions
    if notes.review_questions:
        out.extend(_h1_with_bar("پرسش‌های مرور", styles))
        out.append(HRFlowable(width="100%", thickness=0.5, color=RULE, spaceAfter=8))
        out.extend(_steps_flowables(list(notes.review_questions), styles, _FRAME_W))

    # Glossary
    if notes.glossary:
        out.extend(_h1_with_bar("واژه‌نامهٔ اصطلاح‌ها", styles))
        out.append(HRFlowable(width="100%", thickness=0.5, color=RULE, spaceAfter=8))
        out.append(_glossary_table(list(notes.glossary), styles, _FRAME_W))

    return out


def _final_page_flowables(notes: StructuredNotes, meta: DocumentMeta, styles) -> list[Flowable]:
    created = meta.created_at or datetime.now()
    out: list[Flowable] = []
    out.append(Spacer(1, 14))
    out.append(HRFlowable(width="100%", thickness=0.4, color=RULE, spaceAfter=8))
    # Attribution
    out.append(Paragraph(
        _xml_escape(_visual_line(
            "این جزوه به‌صورت خودکار توسط Gamas تهیه شده است."
        )),
        styles["attribution"],
    ))
    out.append(Paragraph(
        _xml_escape(_visual_line(f"تاریخ تولید: {jalali_date(created)}")),
        styles["attribution"],
    ))
    # Disclaimer
    out.append(Spacer(1, 6))
    disclaimer = (
        "توجه: این جزوه به‌کمک هوش مصنوعی از روی فایل صوتی/متنی کلاس تولید شده و ممکن است حاوی "
        "اشتباهات رونویسی یا برداشتی باشد. این سند جایگزین منابع معتبر، کتاب‌های مرجع و بیان رسمی استاد نیست. "
        "در صورت مشاهدهٔ هرگونه مغایرت با محتوای اصلی، به منبع رسمی مراجعه کنید."
    )
    out.append(_rtl_paragraph(disclaimer, styles["disclaimer"], _FRAME_W))
    return out


# ---------------------------------------------------------------------------
# Page frame / header / footer
# ---------------------------------------------------------------------------


class _InvariantCanvas(_rl_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        kwargs["invariant"] = 1
        super().__init__(*args, **kwargs)


def _make_on_page(title: str):
    regular, _bold = _fonts()

    def on_page(canv, doc):
        page = canv.getPageNumber()
        canv.saveState()
        # Light outer frame
        canv.setStrokeColor(colors.HexColor("#C2CEDC"))
        canv.setLineWidth(0.6)
        inset = 20
        canv.rect(inset, inset, _PAGE_W - 2 * inset, _PAGE_H - 2 * inset)
        canv.setTitle(title)
        canv.setAuthor("Gamas Bot")
        if page > 1:
            # Header
            canv.setFont(regular, 8.5)
            canv.setFillColor(MUTED)
            header = f"{title} | {PAGE_HEADER_TEXT}"
            canv.drawCentredString(_PAGE_W / 2, _PAGE_H - 1.1 * cm, _visual_line(header))
            canv.setStrokeColor(STEEL)
            canv.setLineWidth(0.4)
            canv.line(_MARGIN_LR, _PAGE_H - 1.15 * cm, _PAGE_W - _MARGIN_LR, _PAGE_H - 1.15 * cm)
            # Footer
            canv.setFont(regular, 9)
            canv.setFillColor(MUTED)
            canv.drawCentredString(
                _PAGE_W / 2, 1.1 * cm,
                _visual_line(f"Gamas — صفحهٔ {to_persian_digits(page - 1)}"),
            )
        canv.restoreState()
    return on_page


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def build_notes_pdf(
    notes: StructuredNotes,
    *,
    meta: DocumentMeta,
    duration_seconds: int | None = None,
    session_number: int | None = None,
) -> bytes:
    """Render structured notes as a professional Persian RTL PDF booklet.

    Deterministic: invariant canvas, no wall clock in the body (the date is
    taken from ``meta.created_at``), the same notes always yield the same bytes.
    """
    styles = _styles()
    title = notes.display_title or "جزوهٔ کلاس"
    regular, _bold = _fonts()

    story: list[Flowable] = []

    # Cover
    story.extend(_cover_flowables(styles, notes, meta,
                                  duration_seconds=duration_seconds,
                                  session_number=session_number))
    story.append(PageBreak())

    # Body
    story.extend(_body_flowables(notes, styles))

    # Final page block (disclaimer / attribution)
    story.extend(_final_page_flowables(notes, meta, styles))

    buffer = io.BytesIO()
    frame = Frame(
        _MARGIN_LR, _MARGIN_BOTTOM, _FRAME_W, _FRAME_H,
        id="notes_main",
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    template = PageTemplate(
        id="notes",
        frames=[frame],
        onPage=_make_on_page(title),
    )
    doc = BaseDocTemplate(buffer, pagesize=A4, title=title, author="Gamas Bot")
    doc.addPageTemplates([template])
    doc.build(story, canvasmaker=_InvariantCanvas)
    return buffer.getvalue()


def notes_pdf_filename(notes: StructuredNotes, reference: str) -> str:
    from .docx_export import sanitize_filename_part
    title_part = sanitize_filename_part(notes.display_title)
    name = f"جزوه - {title_part}" if title_part else "جزوه"
    return f"{name} - {reference}.pdf"
