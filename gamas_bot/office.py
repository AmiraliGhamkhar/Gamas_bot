"""The Free Office section: the user's own text becomes a real Office file.

This module owns the *document* side of the section. It reads a piece of
text — typed in the chat, pasted, or uploaded as ``.txt``/``.csv`` — and
produces four deliverables with the same brand and the same right-to-left
rules as the lecture booklet:

* **Word** — handed to :mod:`gamas_bot.docx_export`, which already owns the
  polished RTL booklet (cover page, topic list, page frame, running header,
  footer page field). The Office section reuses that engine instead of
  writing a second one.
* **Excel** — a real ``.xlsx`` with a right-to-left sheet, a navy header row
  that repeats on every page, frozen headings and zebra rows.
* **PowerPoint** — a real ``.pptx`` in 16:9, with a title slide, an agenda,
  one slide per section and a closing slide.
* **PDF** — handed to :mod:`gamas_bot.pdf_export`: cover, optional topic
  list, the same palette, all right-to-left, shaped with Vazirmatn.

Two rules run through the whole module:

* **The user's words are never rewritten.** Structure is *arranged*, not
  invented: the text of a slide bullet is a line of the user's text, and the
  cells of a sheet are the user's lines. The optional model layer in
  :mod:`gamas_bot.office_ai` may only propose an arrangement, and every line
  it proposes is checked against the source before it is used.
* **A document is never lost to a missing dependency.** If the model is not
  configured, the deck falls back to the user's own headings; if the model
  misbehaves, the same fallback runs. The section therefore works on a
  deployment with no note provider at all.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .mdblocks import TextBlock, parse_blocks
from .progress import to_persian_digits
from .textnorm import normalize_display, normalize_for_compare

logger = logging.getLogger(__name__)

#: The four deliverables, in the order the menu shows them.
OFFICE_FORMATS = ("word", "excel", "ppt", "pdf")

#: Human labels (used in captions and messages).
FORMAT_LABELS = {"word": "Word", "excel": "Excel", "ppt": "PowerPoint", "pdf": "PDF"}

#: The extension each format is delivered with.
FORMAT_EXTENSIONS = {"word": ".docx", "excel": ".xlsx", "ppt": ".pptx", "pdf": ".pdf"}

#: The emoji shown on the menu button of each format.
FORMAT_EMOJI = {"word": "📝", "excel": "📊", "ppt": "📖", "pdf": "📕"}

#: The label printed on the cover of a Free Office Word document.
OFFICE_MODE_LABELS = {
    "word": "سند آزاد — Word",
    "excel": "سند آزاد — Excel",
    "ppt": "سند آزاد — PowerPoint",
    "pdf": "سند آزاد — PDF",
}

#: Uploads the section accepts as *content* (a document, not a lecture).
TEXT_FILE_EXTENSIONS = frozenset(
    {".txt", ".text", ".csv", ".tsv", ".md", ".markdown", ".log"}
)

#: The title of a document that has no better one.
DEFAULT_TITLE = "سند"

#: Ceilings that keep one document inside what a reader (and a phone) can use.
MAX_TABLE_ROWS = 2000
MAX_TABLE_COLUMNS = 40
MAX_SLIDES = 40
#: Lines per slide when the text has no headings of its own.
FALLBACK_LINES_PER_SLIDE = 6
#: A table needs at least this many rows (header included) to be a table.
MIN_TABLE_ROWS = 2

#: A cell that is *only* a number becomes a real number in the sheet, so the
#: user can sum it. **ASCII digits only**: ``\d`` would also match Persian
#: digits, and turning «۲۰» into the number 20 would change what the sheet
#: says (Excel would show "20", not "۲۰").
_NUMBER_PATTERN = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")

#: The palette, shared with the Word engine (see ``gamas_bot.docx_export``).
ACCENT_HEX = "1D3557"
ACCENT_SOFT_HEX = "457B9D"
ZEBRA_HEX = "F4F8FA"
GRID_HEX = "C7D3DE"
MUTED_HEX = "5C6B7A"
ON_ACCENT_HEX = "FFFFFF"
BODY_HEX = "333333"

#: The face used for every generated document.
OFFICE_FONT = "Tahoma"


class OfficeError(RuntimeError):
    """A document could not be produced; the message is safe to show."""


@dataclass(frozen=True, slots=True)
class OfficeTable:
    """A table: the header row plus its data rows."""

    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...] = ()

    @property
    def width(self) -> int:
        return len(self.headers)


@dataclass(frozen=True, slots=True)
class OfficeSlide:
    """One slide: a title and the lines under it."""

    title: str
    bullets: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True, slots=True)
class OfficeDocument:
    """The parsed content of one Office request."""

    title: str
    text: str
    blocks: tuple[TextBlock, ...]

    @property
    def tables(self) -> tuple[OfficeTable, ...]:
        """Every table the user's own text already contains."""
        found: list[OfficeTable] = []
        for block in self.blocks:
            if not block.is_table or not block.rows:
                continue
            rows = block.rows[:MAX_TABLE_ROWS]
            found.append(OfficeTable(headers=rows[0], rows=tuple(rows[1:])))
        return tuple(found)

    @property
    def lines(self) -> tuple[str, ...]:
        """Every non-empty line of the text, in order."""
        return tuple(
            block.text
            for block in self.blocks
            if block.kind in ("paragraph", "bullet") and block.text
        )


@dataclass
class OfficeSession:
    """One user's in-flight Free Office request.

    The session is what makes the section forgiving: the content and the
    format may arrive in any order (a user may paste the text first and
    choose the format afterwards, or the other way round), and several
    messages may be collected into one document.

    ``kind`` is ``None`` while the format is still unknown. ``parts`` holds
    the content in the order it arrived, and ``message`` is the Telegram
    message the content came with — the document is delivered as a reply to
    it, so the result stays threaded to the request.
    """

    kind: str | None = None
    parts: list[str] = field(default_factory=list)
    filename: str | None = None
    title: str = ""
    message: Any = None
    updated_at: float = 0.0

    @property
    def text(self) -> str:
        """Everything the user sent, in order."""
        return "\n".join(part for part in self.parts if part.strip())

    def add(self, content: str) -> None:
        """Append one piece of content to the session."""
        if content and content.strip():
            self.parts.append(content)


def detect_title(blocks: list[TextBlock], text: str) -> str:
    """A title for a document that did not bring one.

    The first heading wins. Otherwise the first non-empty line is used, but
    only when it is short enough to be a title — a document whose first line
    is a paragraph gets the neutral default instead of a heading-sized wall of
    text.
    """
    for block in blocks:
        if block.kind == "heading" and block.text:
            return block.text[:120]
    for block in blocks:
        if block.kind in ("paragraph", "bullet") and block.text:
            candidate = block.text.strip()
            if 0 < len(candidate) <= 90:
                return candidate
            break
    for raw_line in text.splitlines():
        candidate = raw_line.strip()
        if candidate:
            return candidate[:120]
    return DEFAULT_TITLE


def build_office_document(text: str, *, title: str = "") -> OfficeDocument:
    """Parse the user's text into the document the renderers work on."""
    cleaned = normalize_display(text or "")
    blocks = parse_blocks(cleaned)
    resolved = (title or "").strip() or detect_title(blocks, cleaned)
    return OfficeDocument(title=resolved[:120], text=cleaned, blocks=tuple(blocks))


# ---------------------------------------------------------------------------
# Deterministic fallbacks (used whenever the model is absent or untrustworthy)
# ---------------------------------------------------------------------------


def fallback_slides(document: OfficeDocument, *, max_slides: int = MAX_SLIDES) -> list[OfficeSlide]:
    """Slides built from the user's own headings — no model involved.

    Each heading starts a slide and the lines under it become its bullets. A
    text without headings is cut into fixed-size slides, so a deck always
    exists for every text.
    """
    slides: list[OfficeSlide] = []
    current_title = document.title or DEFAULT_TITLE
    current_lines: list[str] = []

    def flush() -> None:
        if current_lines:
            slides.append(
                OfficeSlide(
                    title=current_title,
                    bullets=tuple(current_lines),
                    # Keep the exact body in the notes pane too. Using the
                    # accumulated slide lines includes table rows as well as
                    # prose; a separate prose-only list silently lost grids.
                    notes="\n".join(current_lines).strip(),
                )
            )

    has_heading = any(block.kind == "heading" for block in document.blocks)
    for block in document.blocks:
        if block.kind == "heading":
            flush()
            current_title = block.text or current_title
            current_lines = []
            continue
        if block.kind == "table":
            for row in block.rows:
                current_lines.append(" | ".join(cell for cell in row if cell))
            continue
        if block.text:
            current_lines.append(block.text)
    flush()

    if not has_heading and len(current_lines) > 0:
        # A text with no headings of its own would make one crowded slide.
        # It is cut into readable slides instead; the first line is the
        # document's title, so it titles the deck rather than repeating
        # itself as the first bullet.
        lines = list(current_lines)
        title = document.title or DEFAULT_TITLE
        if lines and lines[0].strip() == title.strip():
            lines = lines[1:]
        slides = []
        for index in range(0, len(lines), FALLBACK_LINES_PER_SLIDE):
            chunk = lines[index : index + FALLBACK_LINES_PER_SLIDE]
            slides.append(
                OfficeSlide(
                    title=title if index == 0 else f"بخش {to_persian_digits(index // FALLBACK_LINES_PER_SLIDE + 1)}",
                    bullets=tuple(chunk),
                    notes="\n".join(chunk).strip(),
                )
            )
        if not slides:
            slides = [OfficeSlide(title=title, bullets=(), notes=document.text)]

    if not slides:
        # No headings and no lines: one slide carrying the whole text.
        return [OfficeSlide(title=document.title or DEFAULT_TITLE, bullets=(), notes=document.text)]
    return slides[:max_slides]


def fallback_table(document: OfficeDocument) -> OfficeTable:
    """A one-column sheet built from the user's lines.

    The fallback keeps the content: every line of the text becomes a row, so
    an Excel file is always produced, even when the text was prose.
    """
    lines = document.lines
    if not lines:
        return OfficeTable(headers=("متن",), rows=())
    rows = tuple((line,) for line in lines[:MAX_TABLE_ROWS])
    return OfficeTable(headers=("متن",), rows=rows)


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def _cell_value(text: str):
    """The value written to a cell: a number when the text is only a number."""
    stripped = text.strip()
    if stripped and _NUMBER_PATTERN.match(stripped):
        try:
            return float(stripped) if "." in stripped else int(stripped)
        except ValueError:  # pragma: no cover - the pattern already guarantees it
            return stripped
    return text


def _column_widths(table: OfficeTable) -> list[int]:
    """A width per column, from the widest cell it holds."""
    widths: list[int] = []
    for index in range(table.width):
        longest = 0
        if index < len(table.headers):
            longest = len(table.headers[index])
        for row in table.rows:
            if index < len(row):
                longest = max(longest, len(row[index]))
        widths.append(min(max(longest + 4, 12), 60))
    return widths


def _sheet_name(title: str, index: int, taken: set[str]) -> str:
    """A valid, unique Excel sheet name (31 characters, no ``[]:*?/\\``)."""
    if index == 0:
        candidate = title.strip() or DEFAULT_TITLE
    else:
        candidate = f"بخش {index + 1}"
    candidate = re.sub(r"[\[\]:*?/\\]", " ", candidate).strip() or DEFAULT_TITLE
    candidate = candidate[:31]
    name = candidate
    suffix = 2
    while name.lower() in taken:
        tail = f" ({suffix})"
        name = candidate[: 31 - len(tail)] + tail
        suffix += 1
    taken.add(name.lower())
    return name


def build_office_xlsx(document: OfficeDocument, tables: list[OfficeTable]) -> bytes:
    """Build the ``.xlsx`` file: one sheet per table, right-to-left.

    The sheet is right-to-left (``w:rtl`` on the sheet view), the header row
    is navy on white and repeats at the top of every printed page, the header
    row is frozen, and the body rows are zebra-striped. Numbers that are
    *only* numbers become real numbers, so the user can sum a column.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover - openpyxl is a dependency
        raise OfficeError("کتابخانهٔ ساخت Excel روی سرور نصب نیست.") from exc

    if not tables:
        tables = [fallback_table(document)]

    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.creator = "Gamas Bot"
    workbook.properties.title = document.title

    header_fill = PatternFill("solid", fgColor=ACCENT_HEX)
    zebra_fill = PatternFill("solid", fgColor=ZEBRA_HEX)
    grid = Side(style="thin", color=GRID_HEX)
    border = Border(left=grid, right=grid, top=grid, bottom=grid)
    header_font = Font(name=OFFICE_FONT, bold=True, color=ON_ACCENT_HEX, size=11)
    body_font = Font(name=OFFICE_FONT, size=11)
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    body_alignment = Alignment(horizontal="right", vertical="center", wrap_text=True)

    taken: set[str] = set()
    for index, table in enumerate(tables):
        sheet = workbook.create_sheet(title=_sheet_name(document.title, index, taken))
        sheet.sheet_view.rightToLeft = True
        sheet.sheet_properties.tabColor = ACCENT_HEX
        width = max(1, table.width)

        for column, label in enumerate(table.headers[:width], start=1):
            cell = sheet.cell(row=1, column=column, value=label)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
            cell.border = border
        sheet.row_dimensions[1].height = 26

        for row_index, row in enumerate(table.rows[:MAX_TABLE_ROWS], start=2):
            stripe = (row_index % 2) == 1
            for column in range(1, width + 1):
                value = row[column - 1] if column - 1 < len(row) else ""
                cell = sheet.cell(row=row_index, column=column, value=_cell_value(value))
                cell.font = body_font
                cell.alignment = body_alignment
                cell.border = border
                if stripe:
                    cell.fill = zebra_fill

        for column, width_chars in enumerate(_column_widths(table), start=1):
            sheet.column_dimensions[get_column_letter(column)].width = width_chars
        # The header stays on screen while the body scrolls.
        sheet.freeze_panes = "A2"

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# PowerPoint
# ---------------------------------------------------------------------------

#: 16:9, the default of every modern PowerPoint.
SLIDE_WIDTH_INCHES = 13.333
SLIDE_HEIGHT_INCHES = 7.5


def _hex_rgb(value: str):
    from pptx.dml.color import RGBColor

    return RGBColor.from_string(value)


def _set_run_font(run, *, name: str, size: float, bold: bool, color: str) -> None:
    """Style a run, including the complex-script face Persian text needs.

    ``font.name`` writes ``a:latin`` only. Persian is a complex script, and
    PowerPoint picks the face for it from ``a:cs``; without it the deck is
    rendered in whatever the theme happens to use.
    """
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    font = run.font
    font.size = Pt(size)
    font.bold = bold
    font.name = name
    font.color.rgb = _hex_rgb(color)
    r_pr = run._r.get_or_add_rPr()
    for existing in r_pr.findall(qn("a:cs")):
        r_pr.remove(existing)
    cs = r_pr.makeelement(qn("a:cs"), {"typeface": name})
    latin = r_pr.find(qn("a:latin"))
    if latin is not None:
        latin.addnext(cs)
    else:  # pragma: no cover - a run always has a latin face after the above
        r_pr.append(cs)


def _rtl_paragraph(paragraph, *, align: str = "r", space_after: float = 6) -> None:
    """Mark a paragraph right-to-left and aligned to its logical start edge.

    ``a:pPr/@rtl`` is what makes PowerPoint lay the line out right-to-left
    (the caret, the bullet and the punctuation follow the script); ``algn``
    only places the block.
    """
    p_pr = paragraph._p.get_or_add_pPr()
    p_pr.set("rtl", "1")
    p_pr.set("algn", align)
    if space_after:
        p_pr.set("spcAft", str(int(space_after * 100)))


def _add_textbox(slide, *, left: float, top: float, width: float, height: float):
    from pptx.util import Inches

    shape = slide.shapes.add_textbox(
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    frame = shape.text_frame
    frame.word_wrap = True
    frame.margin_left = 0
    frame.margin_right = 0
    frame.margin_top = 0
    frame.margin_bottom = 0
    return frame


def _write_lines(
    frame,
    lines: list[tuple[str, float, bool, str, str]],
    *,
    align: str = "r",
    line_spacing: float = 1.15,
) -> None:
    """Fill a text frame with ``(text, size, bold, color, align)`` lines."""
    for index, (text, size, bold, color, line_align) in enumerate(lines):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        _rtl_paragraph(paragraph, align=line_align or align)
        paragraph.line_spacing = line_spacing
        run = paragraph.add_run()
        run.text = text
        _set_run_font(run, name=OFFICE_FONT, size=size, bold=bold, color=color)


def _add_rectangle(slide, *, left: float, top: float, width: float, height: float, color: str):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _hex_rgb(color)
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _blank_slide(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    return slide


def _paint_background(slide, color: str) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _hex_rgb(color)


def _add_notes(slide, text: str) -> None:
    """Put the section's full text in the notes pane (verbatim)."""
    if not text:
        return
    try:
        notes_frame = slide.notes_slide.notes_text_frame
    except Exception:  # pragma: no cover - a deck without a notes master
        return
    notes_frame.text = text
    for paragraph in notes_frame.paragraphs:
        _rtl_paragraph(paragraph, align="r")


def build_office_pptx(
    document: OfficeDocument,
    slides: list[OfficeSlide],
    *,
    subtitle: str = "",
    max_slides: int = MAX_SLIDES,
) -> bytes:
    """Build the ``.pptx`` file: title, agenda, one slide per section, closing.

    The deck follows the booklet's palette (deep navy, steel blue) and the
    same right-to-left rules: every paragraph is marked RTL and aligned to
    the right, and Persian runs carry a complex-script face.
    """
    try:
        from pptx import Presentation
        from pptx.util import Inches
    except ImportError as exc:  # pragma: no cover - python-pptx is a dependency
        raise OfficeError("کتابخانهٔ ساخت PowerPoint روی سرور نصب نیست.") from exc

    sections = list(slides)[:max_slides]
    if not sections:
        sections = [OfficeSlide(title=document.title or DEFAULT_TITLE, bullets=())]

    presentation = Presentation()
    presentation.slide_width = Inches(SLIDE_WIDTH_INCHES)
    presentation.slide_height = Inches(SLIDE_HEIGHT_INCHES)

    # --- title slide ------------------------------------------------------
    title_slide = _blank_slide(presentation)
    _paint_background(title_slide, ACCENT_HEX)
    brand = _add_textbox(title_slide, left=1.0, top=0.9, width=11.333, height=0.6)
    _write_lines(brand, [("GAMAS", 14, True, ON_ACCENT_HEX, "ctr")], align="ctr")
    title_frame = _add_textbox(title_slide, left=1.2, top=2.5, width=10.933, height=1.8)
    _write_lines(
        title_frame,
        [(document.title or DEFAULT_TITLE, 40, True, ON_ACCENT_HEX, "ctr")],
        align="ctr",
        line_spacing=1.1,
    )
    _add_rectangle(title_slide, left=5.866, top=4.45, width=2.6, height=0.045, color=ACCENT_SOFT_HEX)
    if subtitle:
        subtitle_frame = _add_textbox(title_slide, left=1.5, top=4.85, width=10.333, height=1.0)
        _write_lines(
            subtitle_frame, [(subtitle, 18, False, "D6E4F0", "ctr")], align="ctr"
        )

    # --- agenda slide -----------------------------------------------------
    agenda_slide = _blank_slide(presentation)
    agenda_frame = _add_textbox(agenda_slide, left=0.9, top=0.8, width=11.5, height=0.9)
    _write_lines(agenda_frame, [("فهرست مطالب", 30, True, ACCENT_HEX, "r")])
    _add_rectangle(agenda_slide, left=0.9, top=1.75, width=11.5, height=0.03, color=ACCENT_SOFT_HEX)
    agenda_items: list[tuple[str, float, bool, str, str]] = []
    from .progress import to_persian_digits

    for index, section in enumerate(sections, start=1):
        agenda_items.append(
            (f"{to_persian_digits(index)}. {section.title}", 18, False, BODY_HEX, "r")
        )
    agenda_body = _add_textbox(agenda_slide, left=0.9, top=2.1, width=11.5, height=4.6)
    _write_lines(agenda_body, agenda_items, line_spacing=1.35)

    # --- one slide per section -------------------------------------------
    for section in sections:
        slide = _blank_slide(presentation)
        heading = _add_textbox(slide, left=0.9, top=0.75, width=11.5, height=1.1)
        _write_lines(heading, [(section.title, 30, True, ACCENT_HEX, "r")])
        _add_rectangle(slide, left=0.9, top=1.85, width=11.5, height=0.03, color=ACCENT_SOFT_HEX)
        bullets = [line for line in section.bullets if line.strip()]
        if bullets:
            body = _add_textbox(slide, left=0.9, top=2.2, width=11.5, height=4.5)
            _write_lines(
                body,
                [(f"▪ {line}", 19, False, BODY_HEX, "r") for line in bullets],
                line_spacing=1.35,
            )
        _add_notes(slide, section.notes)

    # --- closing slide ----------------------------------------------------
    closing = _blank_slide(presentation)
    _paint_background(closing, ACCENT_HEX)
    closing_frame = _add_textbox(closing, left=1.2, top=2.9, width=10.933, height=1.2)
    _write_lines(
        closing_frame, [("با تشکر از توجه شما", 34, True, ON_ACCENT_HEX, "ctr")], align="ctr"
    )
    closing_brand = _add_textbox(closing, left=1.2, top=4.3, width=10.933, height=0.6)
    _write_lines(closing_brand, [("Gamas Bot", 16, False, "D6E4F0", "ctr")], align="ctr")

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Intake helpers
# ---------------------------------------------------------------------------

#: Encodings an uploaded text file may use. UTF-8 first (with and without a
#: BOM), then the Persian Windows encoding, then Latin-1 as a last resort.
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1256", "latin-1")


def read_text_file(path: Path, *, max_bytes: int) -> str:
    """Read an uploaded text file, whatever encoding it was written in.

    A file that cannot be decoded is not an error: the last encoding in the
    list decodes any byte sequence, so a mis-declared file still yields
    readable text instead of a failed job.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise OfficeError("فایل ارسالی خوانده نشد.") from exc
    if size > max_bytes:
        raise OfficeError(
            f"حجم فایل بیش از حد مجاز است (حداکثر {max_bytes // 1024} کیلوبایت)."
        )
    payload = path.read_bytes()
    for encoding in _TEXT_ENCODINGS:
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")  # pragma: no cover - latin-1 always decodes


def office_filename(title: str, kind: str, reference: str) -> str:
    """The delivered file name: ``<title> - <reference>.<ext>``."""
    from .docx_export import sanitize_filename_part

    part = sanitize_filename_part(title) or DEFAULT_TITLE
    extension = FORMAT_EXTENSIONS.get(kind, ".docx")
    return f"{part} - {reference}{extension}"


def office_title_from_filename(filename: str | None) -> str:
    """A title taken from an uploaded file's name, when it has one."""
    if not filename:
        return ""
    stem = Path(filename).stem.strip()
    stem = re.sub(r"[_]+", " ", stem)
    return stem[:120]


def office_datetime_stamp(now: datetime | None = None) -> str:
    """A human stamp for the document metadata (the Jalali date)."""
    from .docx_export import jalali_date

    return jalali_date(now or datetime.now())


def normalized_source(text: str) -> str:
    """The comparison form of a source text (used to validate model output)."""
    return normalize_for_compare(text)
