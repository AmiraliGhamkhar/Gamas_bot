"""The Free Office section's PDF renderer.

A fourth deliverable next to Word, Excel and PowerPoint: the same user text,
read by the same one parser, rendered as a real, right-to-left PDF — and,
like the other three formats, never touched by a model, so the "your words
are never rewritten" promise holds by construction.

Layout rules, mirroring the Word booklet (see :mod:`gamas_bot.docx_export`):

* **One line per paragraph** — the line breaks a user typed survive;
* **the booklet palette** — the same hex family (deep navy, steel blue, the
  cool zebra tint, the soft grid lines);
* **a cover page** — brand, title, mode label, Jalali date, the quotation;
* **a topic list** for long documents — clickable links, no page numbers
  (a page number is never guessed: the same policy as the Word document);
* **a page frame, a running header and a footer page number** on every page
  after the cover, numbered exactly like the Word section's page field
  (the cover is unnumbered; the body restarts at one without a topic list).

Persian is shaped (contextual letter forms) with ``arabic_reshaper`` and
reordered into storage order with ``python-bidi``. The face is Vazirmatn,
bundled under ``assets/fonts/`` (SIL OFL, see the license note there).
Wrapping is done on the *logical* text and only the finished lines are
shaped: shaping a whole paragraph first and wrapping afterwards would wrap
on the reversed (visual) order and break phrases mid-line.

The canvas is ``invariant`` (fixed ID, fixed timestamps), so identical text
always yields identical bytes — the same reproducibility policy as the Word
exporter's normalised zip member timestamps.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as _rl_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import HRFlowable

from .config import PROJECT_DIR
from .docx_export import (
    ACCENT_HEX,
    ACCENT_SOFT_HEX,
    BRAND_NAME,
    BRAND_SUBTITLE,
    COVER_GOD_LINE,
    COVER_QUOTE,
    DEFAULT_BORDER_COLOR,
    PAGE_HEADER_TEXT,
    QUOTE_BORDER,
    QUOTE_SHADE,
    RULE_FILL,
    TABLE_BORDER_COLOR,
    ZEBRA_SHADE,
    DocumentMeta,
    jalali_date,
    resolve_design,
    toc_is_worth_it,
)
from .mdblocks import heading_level
from .office import OfficeDocument, OfficeError
from .progress import to_persian_digits

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Geometry and palette (mirroring docx_export's A4 layout and brand colours)
# ---------------------------------------------------------------------------

_PAGE_W, _PAGE_H = A4
_MARGIN_LR = 2.2 * cm
_MARGIN_TOP = 2.4 * cm
_MARGIN_BOTTOM = 2.2 * cm
_FRAME_W = _PAGE_W - 2 * _MARGIN_LR
_FRAME_H = _PAGE_H - _MARGIN_TOP - _MARGIN_BOTTOM
#: The page frame: 24 pt inside the page edge, a 1 pt line (the booklet's
#: ``w:pgBorders`` equivalent), on every page including the cover.
_FRAME_INSET = 24
#: 4 pt of safety so the pre-wrapped lines never make a Paragraph re-wrap.
_WRAP_WIDTH = _FRAME_W - 4


def _color(value: str) -> colors.Color:
    return colors.HexColor(f"#{value}")


ACCENT = _color(ACCENT_HEX)
ACCENT_SOFT = _color(ACCENT_SOFT_HEX)
MUTED = _color("5C6B7A")
BODY = _color("333333")
ZEBRA = _color(ZEBRA_SHADE)
GRID = _color(TABLE_BORDER_COLOR)
FRAME_BORDER = _color(DEFAULT_BORDER_COLOR)
RULE = _color(RULE_FILL)
QUOTE_FILL = _color(QUOTE_SHADE)
QUOTE_FRAME = _color(QUOTE_BORDER)


# ---------------------------------------------------------------------------
# The Persian face (Vazirmatn, SIL OFL, bundled in the repository)
# ---------------------------------------------------------------------------

_REGULAR_NAME = "GamasVazir"
_BOLD_NAME = "GamasVazirBold"
_REGULAR_FILE = "Vazirmatn-Regular.ttf"
_BOLD_FILE = "Vazirmatn-Bold.ttf"

#: Registered once per process (reportlab rejects a second registration).
_font_names: tuple[str, str] | None = None


def _find_font(filename: str) -> Path | None:
    """The bundled font file, discovered the way the logo is discovered."""
    for base in (PROJECT_DIR / "assets" / "fonts", Path.cwd() / "assets" / "fonts"):
        path = base / filename
        try:
            if path.is_file():
                return path
        except OSError:  # pragma: no cover - defensive
            continue
    return None


def _fonts() -> tuple[str, str]:
    """``(regular, bold)`` registered font names, or an ``OfficeError``."""
    global _font_names
    if _font_names is not None:
        return _font_names
    regular = _find_font(_REGULAR_FILE)
    if regular is None:
        raise OfficeError(
            "فونت فارسی روی سرور پیدا نشد؛ assets/fonts/Vazirmatn-Regular.ttf لازم است."
        )
    bold = _find_font(_BOLD_FILE) or regular
    try:
        if _REGULAR_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(_REGULAR_NAME, str(regular)))
        if _BOLD_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(_BOLD_NAME, str(bold)))
    except Exception as exc:
        raise OfficeError("فونت فارسی خوانده نشد؛ فایل فونت خراب است.") from exc
    _font_names = (_REGULAR_NAME, _BOLD_NAME)
    return _font_names


# ---------------------------------------------------------------------------
# Persian text: shaping and logical-order wrapping
# ---------------------------------------------------------------------------


def _visual(text: str) -> str:
    """One line in storage order: contextual forms, then visual reordering."""
    return get_display(arabic_reshaper.reshape(text))


def _wrap_logical(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Word-wrap in *logical* order, so shaping never sees a broken line.

    ``stringWidth`` sums the same glyph widths in either direction, so the
    candidate line is measured before shaping and only the finished lines
    are shaped. A single token wider than the line (a URL) is hard-broken.
    """
    words = (text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or pdfmetrics.stringWidth(candidate, font, size) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    wrapped: list[str] = []
    for line in lines:
        if pdfmetrics.stringWidth(line, font, size) <= max_width:
            wrapped.append(line)
            continue
        piece = ""
        for char in line:
            trial = f"{piece}{char}"
            if not piece or pdfmetrics.stringWidth(trial, font, size) <= max_width:
                piece = trial
            else:
                wrapped.append(piece)
                piece = char
        if piece:
            wrapped.append(piece)
    return wrapped


def _rtl_flowable(text: str, style: ParagraphStyle, width: float = _WRAP_WIDTH) -> Paragraph:
    """A Paragraph of pre-wrapped, shaped lines that will not re-wrap."""
    lines = _wrap_logical(text or "", style.fontName, style.fontSize, width)
    body = "<br/>".join(_xml_escape(_visual(line)) for line in lines)
    return Paragraph(body or " ", style)


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


def _styles() -> dict[str, ParagraphStyle]:
    regular, bold = _fonts()

    def make(name: str, font: str, size: float, leading: float, color: colors.Color,
             align: int, **kwargs) -> ParagraphStyle:
        return ParagraphStyle(
            name, fontName=font, fontSize=size, leading=leading,
            textColor=color, alignment=align, **kwargs,
        )

    return {
        "body": make("OfficeBody", regular, 11, 15.5, BODY, TA_RIGHT, spaceAfter=6),
        "bullet": make("OfficeBullet", regular, 11, 15.5, BODY, TA_RIGHT,
                       spaceAfter=6, rightIndent=10),
        "h1": make("OfficeH1", bold, 15, 20, ACCENT, TA_RIGHT,
                   spaceBefore=14, spaceAfter=6, keepWithNext=1),
        "h2": make("OfficeH2", bold, 12.5, 17, ACCENT_SOFT, TA_RIGHT,
                   spaceBefore=10, spaceAfter=4, keepWithNext=1),
        "h3": make("OfficeH3", bold, 11.5, 16, ACCENT_SOFT, TA_RIGHT,
                   spaceBefore=8, spaceAfter=3, keepWithNext=1),
        "title": make("OfficeTitle", bold, 24, 30, ACCENT, TA_CENTER, spaceAfter=8),
        "brand": make("OfficeBrand", bold, 34, 40, ACCENT, TA_CENTER),
        "brand_sub": make("OfficeBrandSub", regular, 13, 16, MUTED, TA_CENTER, spaceAfter=2),
        "god": make("OfficeGodLine", regular, 12, 15, MUTED, TA_CENTER, spaceAfter=18),
        "mode": make("OfficeMode", regular, 10.5, 14, ACCENT, TA_CENTER, spaceAfter=2),
        "date": make("OfficeDate", regular, 9.5, 13, MUTED, TA_CENTER, spaceAfter=6),
        "toc_heading": make("OfficeTocHeading", bold, 16, 20, ACCENT, TA_RIGHT, spaceAfter=10),
        "toc": make("OfficeTocEntry", regular, 10.5, 17, ACCENT, TA_RIGHT, underline=1),
        "quote": make("OfficeQuote", regular, 12, 17, ACCENT, TA_CENTER),
        "cell_head": make("OfficeCellHead", bold, 10.5, 14, colors.white, TA_CENTER),
        "cell": make("OfficeCell", regular, 10.5, 14, BODY, TA_RIGHT),
    }


# ---------------------------------------------------------------------------
# Cover page, topic list, body
# ---------------------------------------------------------------------------


class _PageBookmark(Flowable):
    """A zero-sized internal link destination anchored at its position."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        return 0.0, 0.0

    def drawOn(self, canv, x: float, y: float, _sW: float = 0, _sH: float = 0) -> None:
        canv.bookmarkPage(self.name)


def _quote_box(styles: dict[str, ParagraphStyle]) -> Table:
    """The cover quotation: a tinted, framed panel like the Word cover's."""
    table = Table(
        [[Paragraph(_xml_escape(_visual(f"«{COVER_QUOTE}»")), styles["quote"])]],
        colWidths=[_FRAME_W * 0.8],
        hAlign="CENTER",
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), QUOTE_FILL),
        ("GRID", (0, 0), (-1, -1), 0.75, QUOTE_FRAME),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    return table


def _cover_flowables(
    styles: dict[str, ParagraphStyle], title: str, meta: DocumentMeta, mode_label: str
) -> list[Flowable]:
    created = meta.created_at or datetime.now()
    return [
        Spacer(1, 10),
        _rtl_flowable(COVER_GOD_LINE, styles["god"]),
        _rtl_flowable(BRAND_NAME, styles["brand"]),
        _rtl_flowable(BRAND_SUBTITLE, styles["brand_sub"]),
        HRFlowable(width="55%", thickness=1, color=RULE, hAlign="CENTER",
                   spaceBefore=4, spaceAfter=16),
        Spacer(1, 8),
        _rtl_flowable(title, styles["title"], _FRAME_W * 0.8),
        Spacer(1, 10),
        _rtl_flowable(mode_label, styles["mode"]) if mode_label else Spacer(1, 2),
        _rtl_flowable(f"تاریخ: {jalali_date(created)}", styles["date"]),
        HRFlowable(width="55%", thickness=1, color=RULE, hAlign="CENTER",
                   spaceBefore=4, spaceAfter=14),
        Spacer(1, 48),
        Spacer(1, 48),
        Spacer(1, 48),
        _quote_box(styles),
    ]


def _toc_flowables(styles: dict[str, ParagraphStyle], titles: list[str]) -> list[Flowable]:
    """The topic list: clickable links, no page numbers (never guessed)."""
    flowables: list[Flowable] = [
        _rtl_flowable("فهرست مطالب", styles["toc_heading"]),
        HRFlowable(width="100%", thickness=0.5, color=ACCENT_SOFT, spaceAfter=10),
    ]
    for index, title in enumerate(titles, start=1):
        flowables.append(
            Paragraph(
                f'<a href="#gms_h{index}">{_xml_escape(_visual(title))}</a>',
                styles["toc"],
            )
        )
    return flowables


def _table_flowables(styles: dict[str, ParagraphStyle], rows: tuple[tuple[str, ...], ...]) -> list:
    """A grid the user pasted: a real table, first logical column on the right."""
    if not rows:
        return []
    width = max(len(row) for row in rows)
    padded = [list(row) + [""] * (width - len(row)) for row in rows]
    headers, body = padded[0], padded[1:]

    weights: list[int] = []
    for index in range(width):
        longest = len(headers[index])
        for row in body:
            longest = max(longest, len(row[index]))
        weights.append(min(max(longest + 4, 12), 60))
    total = sum(weights)
    col_widths = [_FRAME_W * w / total for w in weights]

    # The RTL equivalent of w:bidiVisual: mirror the column order.
    order = list(range(width - 1, -1, -1))
    table_data: list[list[Paragraph]] = []
    for position, row in enumerate([headers] + body):
        style = styles["cell_head"] if position == 0 else styles["cell"]
        table_data.append([
            _rtl_flowable(row[i] or " ", style, max(20.0, col_widths[i] - 12))
            for i in order
        ])

    table = Table(table_data, colWidths=[col_widths[i] for i in order], repeatRows=1)
    commands: list = [
        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    if body:
        commands.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]))
    table.setStyle(TableStyle(commands))
    return [table]


def _body_flowables(
    styles: dict[str, ParagraphStyle], document: OfficeDocument
) -> list[Flowable]:
    flowables: list[Flowable] = []
    h1_index = 0
    for block in document.blocks:
        if block.kind == "heading":
            level = heading_level(block.level, offset=0)
            if level == 1:
                # Every level-1 heading carries a destination; the topic
                # list (when built) links to them in this order.
                h1_index += 1
                flowables.append(_PageBookmark(f"gms_h{h1_index}"))
            flowables.append(_rtl_flowable(block.text, styles[f"h{level}"]))
        elif block.kind == "bullet":
            flowables.append(_rtl_flowable(f"• {block.text}", styles["bullet"]))
        elif block.kind == "table":
            flowables.extend(_table_flowables(styles, block.rows))
            flowables.append(Spacer(1, 8))
        else:
            flowables.append(_rtl_flowable(block.text, styles["body"]))
    return flowables


# ---------------------------------------------------------------------------
# Page frame, header, footer
# ---------------------------------------------------------------------------


class _InvariantCanvas(_rl_canvas.Canvas):
    """A canvas with a fixed ID and timestamps: identical bytes per input."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs["invariant"] = 1
        super().__init__(*args, **kwargs)


def _page_decorator(title: str, *, with_toc: bool):
    """The ``onPage`` callback: the frame always, the header/footer from page 2.

    The footer number mirrors the Word section's page field: the cover is
    unnumbered, and with no topic list the body restarts at one.
    """
    regular, _bold = _fonts()

    def on_page(canv, _doc) -> None:
        page = canv.getPageNumber()
        canv.saveState()
        canv.setStrokeColor(FRAME_BORDER)
        canv.setLineWidth(1)
        canv.rect(
            _FRAME_INSET, _FRAME_INSET,
            _PAGE_W - 2 * _FRAME_INSET, _PAGE_H - 2 * _FRAME_INSET,
        )
        canv.setTitle(title)
        canv.setAuthor("Gamas Bot")
        if page > 1:
            canv.setFont(regular, 8.5)
            canv.setFillColor(MUTED)
            canv.drawCentredString(
                _PAGE_W / 2, _PAGE_H - 1.2 * cm, _visual(f"{title} | {PAGE_HEADER_TEXT}")
            )
            canv.setStrokeColor(ACCENT_SOFT)
            canv.setLineWidth(0.5)
            rule_y = _PAGE_H - 1.2 * cm - 4
            canv.line(_MARGIN_LR, rule_y, _PAGE_W - _MARGIN_LR, rule_y)
            number = page if with_toc else page - 1
            canv.setFont(regular, 9)
            canv.setFillColor(MUTED)
            canv.drawCentredString(
                _PAGE_W / 2, 1.2 * cm, _visual(f"Gamas Bot — صفحهٔ {to_persian_digits(number)}")
            )
        canv.restoreState()

    return on_page


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def build_office_pdf(
    document: OfficeDocument, *, meta: DocumentMeta, mode_label: str = ""
) -> bytes:
    """Render the user's text as a branded, right-to-left PDF.

    Deterministic by construction: no model is called, the canvas is
    invariant and no wall clock reaches the output, so the same text always
    yields the same bytes.
    """
    styles = _styles()
    title = document.title
    blocks = document.blocks
    heading_blocks = [block for block in blocks if block.kind == "heading"]
    l1_titles = [
        block.text for block in heading_blocks if heading_level(block.level, offset=0) == 1
    ]
    # The same decision the Word engine makes: enough level-1 headings to
    # navigate, or enough body text to need navigation.
    toc_needed = len(l1_titles) >= 2 and toc_is_worth_it(
        len(document.text), len(heading_blocks), resolve_design()
    )

    story: list[Flowable] = _cover_flowables(styles, title, meta, mode_label)
    body = _body_flowables(styles, document)
    if body:
        story.append(PageBreak())
        if toc_needed:
            story.extend(_toc_flowables(styles, l1_titles))
            story.append(PageBreak())
        story.extend(body)

    buffer = io.BytesIO()
    template = PageTemplate(
        id="office",
        frames=[
            Frame(
                _MARGIN_LR, _MARGIN_BOTTOM, _FRAME_W, _FRAME_H,
                id="main", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
            )
        ],
        onPage=_page_decorator(title, with_toc=toc_needed),
    )
    document_template = BaseDocTemplate(buffer, pagesize=A4, title=title, author="Gamas Bot")
    document_template.addPageTemplates([template])
    try:
        # ``build``'s own ``canvasmaker`` argument (not a constructor option)
        # is what instantiates the real canvas.
        document_template.build(story, canvasmaker=_InvariantCanvas)
    except OfficeError:
        raise
    except Exception as exc:
        logger.warning("office PDF build failed: %s", exc)
        raise OfficeError("سند PDF ساخته نشد.") from exc
    return buffer.getvalue()
