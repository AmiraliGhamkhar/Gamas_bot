"""Modern Typst-powered scientific booklet PDF renderer.

Compiles a validated :class:`~gamas_bot.notes.StructuredNotes` instance into an
A4 Persian RTL academic booklet using the self-contained :mod:`typst` engine
(HarfBuzz native shaping, real clickable Table of Contents with page numbers,
multi-page repeating-header comparative tables, formula cards, professor
emphasis highlights, and practice Q&A blocks).

Every colour, spacing step, font size, and page measurement is pulled from
:mod:`gamas_bot.document` so the Typst PDF, ReportLab fallback, and Word
document share one design system.

Compilation is deterministic (``timestamp=0`` and bundled ``Vazirmatn`` only)
and uses zero system binaries or external network resources.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Sequence

from .config import PROJECT_DIR
from .document import THEME, components as design
from .document.assets import booklet_text, optional as optional_asset, topic_for
from .document.layout import pdf_frame
from .document.rtl import parse_emphasis_spans, strip_emphasis_markers
from .notes import NoteSection, NoteTable, PracticeQuestion, StructuredNotes
from .progress import to_persian_digits
from .textnorm import normalize_display

if TYPE_CHECKING:
    from .docx_export import DocumentMeta

logger = logging.getLogger(__name__)

#: Quoted verbatim from the Gamas editorial cover; never reworded.
QUOTE_TEXT = "دانش اگر در ثریا باشد مردمی از سرزمین پارس به آن دست خواهند یافت"

_FONT_DIR = PROJECT_DIR / "assets" / "fonts"

#: Typst markup characters that must be escaped in user/LLM plain-text spans.
_TYPST_ESCAPE_RE = re.compile(r'([\\#$*_<>@~`\[\]])')

#: Standalone parenthetical English/Latin scientific terms inside Persian prose
#: (e.g. "(Insulin Resistance)", "(Metformin)"), never function-call parens
#: like "O(n log n)" or "T(n/2)".
_LATIN_PAREN_RE = re.compile(
    r"(?<![A-Za-z0-9_])\(([A-Za-z][A-Za-z0-9\s\-/,.:+%=<>&;′'°α-ωΑ-Ω]{1,80})\)"
)

#: Heuristic for mostly-LTR formulas / equations (e.g. "BMI = weight / height^2").
_PERSIAN_CHAR_RE = re.compile(r"[\u0600-\u06FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_LATIN_OR_MATH_RE = re.compile(r"[A-Za-z0-9=+\-×÷*/^√∑∫<>≤≥≈≠%]")


def typst_available() -> bool:
    """Return True when the ``typst`` wheel is importable and fonts exist."""
    try:
        import typst  # noqa: F401
    except ImportError:
        return False
    return (_FONT_DIR / "Vazirmatn-Regular.ttf").is_file()


def _clean_text(value: str) -> str:
    """Normalize Persian text and collapse stray control characters."""
    text = normalize_display(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _escape_typst(text: str) -> str:
    """Escape Typst markup tokens while preserving newlines as soft breaks."""
    escaped = _TYPST_ESCAPE_RE.sub(r"\\\1", text)
    # Double slashes in URLs/paths can start comments if unescaped; escape //.
    escaped = escaped.replace("//", r"\/\/")
    return escaped


def _escape_typst_string(text: str) -> str:
    """Escape a value for placement inside a Typst double-quoted string literal."""
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _format_plain_segment(
    text: str,
    *,
    style_latin: bool = True,
    latin_color: str = "c-teal-700",
) -> str:
    """Render a non-emphasized segment, styling parenthetical English terms."""
    if not text:
        return ""
    if not style_latin or not _LATIN_PAREN_RE.search(text):
        return _escape_typst(text)

    parts: list[str] = []
    cursor = 0
    for match in _LATIN_PAREN_RE.finditer(text):
        if match.start() > cursor:
            parts.append(_escape_typst(text[cursor : match.start()]))
        inner = _escape_typst(match.group(1).strip())
        # Explicit LTR isolate prevents RTL parenthesis-flipping bugs around
        # English scientific terms while highlighting them cleanly.
        parts.append(
            f'#text(dir: ltr, fill: {latin_color}, weight: "medium")[({inner})]'
        )
        cursor = match.end()
    if cursor < len(text):
        parts.append(_escape_typst(text[cursor:]))
    return "".join(parts)


def _typst_inline(
    text: str,
    *,
    highlight_emphasis: bool = True,
    style_latin: bool = True,
    emphasis_color: str = "c-navy-900",
    latin_color: str = "c-teal-700",
) -> str:
    """Convert raw text with optional ``**...**`` spans into safe Typst markup."""
    cleaned = _clean_text(text)
    if not cleaned:
        return ""
    spans = parse_emphasis_spans(cleaned)
    out: list[str] = []
    for chunk, is_bold in spans:
        if not chunk:
            continue
        # Preserve intentional line breaks inside a paragraph/formula.
        lines = chunk.split("\n")
        formatted_lines = [
            _format_plain_segment(
                line,
                style_latin=style_latin,
                latin_color=emphasis_color if is_bold else latin_color,
            )
            for line in lines
        ]
        body = " \\\n".join(formatted_lines)
        if not is_bold:
            out.append(body)
        elif highlight_emphasis:
            out.append(
                f'#gamas-em[{body}]'
            )
        else:
            out.append(
                f'#text(weight: "bold", fill: {emphasis_color})[{body}]'
            )
    return "".join(out)


def _asset_typst_path(key: str) -> str | None:
    """Resolve a semantic booklet asset to a Typst root-relative POSIX path."""
    asset = optional_asset(key)
    if asset is None:
        return None
    try:
        rel = asset.path.resolve().relative_to(PROJECT_DIR.resolve())
    except ValueError:
        logger.warning("booklet asset %s is outside PROJECT_DIR; skipping in Typst", key)
        return None
    return "/" + rel.as_posix()


def _is_ltr_formula(formula: str) -> bool:
    """Return True when a formula is predominantly mathematical/Latin."""
    cleaned = strip_emphasis_markers(formula)
    persian_count = len(_PERSIAN_CHAR_RE.findall(cleaned))
    latin_math_count = len(_LATIN_OR_MATH_RE.findall(cleaned))
    return latin_math_count > 0 and persian_count <= max(2, latin_math_count // 4)


def _column_fractions(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[float]:
    """Compute balanced fractional column weights from column content lengths."""
    width = max(1, len(headers))
    if width == 1:
        return [1.0]
    scores: list[float] = []
    for col in range(width):
        header_len = len(strip_emphasis_markers(headers[col]))
        cell_lens = [
            len(strip_emphasis_markers(row[col]))
            for row in rows
            if col < len(row)
        ]
        typical = sorted(cell_lens)[len(cell_lens) // 2] if cell_lens else 0
        longest = max(cell_lens, default=0)
        # Weight typical length heavily while allowing a modest boost for header/max.
        score = max(6.0, header_len * 0.85, typical * 0.75 + longest * 0.25)
        scores.append(min(score, 68.0))
    total = sum(scores) or float(width)
    # Clamp each column between 12% and 55% so narrow columns stay legible.
    min_share = min(0.14, 0.75 / width)
    max_share = max(0.52, 1.35 / width)
    clamped = [max(min_share, min(max_share, s / total)) for s in scores]
    norm = sum(clamped) or 1.0
    return [round((c / norm) * width, 3) for c in clamped]


def _preamble(title: str) -> str:
    """Emit Typst palette tokens, page setup, headers/footers, and components."""
    pal = THEME.palette
    typ = THEME.type
    spc = THEME.space
    frame = pdf_frame()
    safe_title = _escape_typst_string(strip_emphasis_markers(_clean_text(title))[:54])
    doc_title = _escape_typst_string(strip_emphasis_markers(_clean_text(title)) or "جزوهٔ کلاس")

    callout_lines: list[str] = []
    for kind, spec in design.CALLOUTS.items():
        badge_map = {
            "تأکید استاد": "تأکید ویژهٔ استاد",
            "هشدار": "هشدار",
            "نکته": "نکته",
            "یادآوری": "یادآوری",
        }
        badge = badge_map.get(kind, kind)
        callout_lines.append(
            f'  "{_escape_typst_string(kind)}": ('
            f'fill: rgb("{spec.fill}"), '
            f'border: rgb("{spec.border}"), '
            f'title-color: rgb("{spec.accent}"), '
            f'body-color: rgb("{spec.text}"), '
            f'label: "{_escape_typst_string(badge)}"),'
        )
    callouts_dict = "\n".join(callout_lines)

    tip_lines: list[str] = []
    for kind, spec in design.TIPS.items():
        tip_lines.append(
            f'  "{_escape_typst_string(kind)}": ('
            f'fill: rgb("{spec.fill}"), '
            f'border: rgb("{spec.border}"), '
            f'title-color: rgb("{spec.accent}"), '
            f'body-color: rgb("{spec.text}")),'
        )
    tips_dict = "\n".join(tip_lines)

    return f'''#set document(title: "{doc_title}", author: "Gamas Bot")

// --- Design Tokens (synced with gamas_bot.document.theme.THEME) ---
#let c-navy-950 = rgb("{pal.navy_950}")
#let c-navy-900 = rgb("{pal.navy_900}")
#let c-navy-800 = rgb("{pal.navy_800}")
#let c-navy-700 = rgb("{pal.navy_700}")
#let c-teal-700 = rgb("{pal.teal_700}")
#let c-teal-600 = rgb("{pal.teal_600}")
#let c-teal-500 = rgb("{pal.teal_500}")
#let c-teal-400 = rgb("{pal.teal_400}")
#let c-cyan-500 = rgb("{pal.cyan_500}")
#let c-cyan-300 = rgb("{pal.cyan_300}")
#let c-cream-50 = rgb("{pal.cream_50}")
#let c-cream-100 = rgb("{pal.cream_100}")
#let c-paper = rgb("{pal.paper}")
#let c-ink = rgb("{pal.ink}")
#let c-body = rgb("{pal.body}")
#let c-muted = rgb("{pal.muted}")
#let c-on-dark = rgb("{pal.on_dark}")
#let c-line = rgb("{pal.line}")
#let c-border = rgb("{pal.border}")
#let c-zebra = rgb("{pal.zebra}")
#let c-danger = rgb("{pal.danger}")
#let c-warning = rgb("{pal.warning}")
#let c-success = rgb("{pal.success}")
#let c-highlight = rgb("{pal.highlight}")
#let c-def-fill = rgb("{design.DEFINITION.fill}")
#let c-def-border = rgb("{design.DEFINITION.border}")
#let c-ex-fill = rgb("{design.EXAMPLE.fill}")
#let c-ex-border = rgb("{design.EXAMPLE.border}")
#let c-key-fill = rgb("{design.KEY_POINTS.fill}")
#let c-key-border = rgb("{design.KEY_POINTS.border}")
#let c-sum-fill = rgb("{design.SUMMARY.fill}")
#let c-sum-border = rgb("{design.SUMMARY.border}")
#let c-form-fill = rgb("{design.FORMULA.fill}")
#let c-form-border = rgb("{design.FORMULA.border}")
#let c-quote-fill = rgb("{design.QUOTE.fill}")
#let c-quote-border = rgb("{design.QUOTE.border}")
#let c-qa-fill = rgb("{design.PRACTICE_QA.fill}")
#let c-qa-border = rgb("{design.PRACTICE_QA.border}")
#let c-pit-fill = rgb("{pal.warn_fill}")
#let c-pit-border = rgb("{pal.warn_border}")
#let c-obj-fill = rgb("{pal.note_fill}")
#let c-obj-border = rgb("{pal.note_border}")
#let c-glance-fill = rgb("{design.GLANCE.fill}")
#let c-glance-border = rgb("{design.GLANCE.border}")

#let callout-specs = (
{callouts_dict}
)

#let tip-specs = (
{tips_dict}
)

#let fa-digits(val) = {{
  let s = str(val)
  s.replace("0", "۰").replace("1", "۱").replace("2", "۲").replace("3", "۳").replace("4", "۴").replace("5", "۵").replace("6", "۶").replace("7", "۷").replace("8", "۸").replace("9", "۹")
}}

#let ecg-rule(w: 92pt, col: c-teal-400) = {{
  curve(
    stroke: 0.85pt + col,
    curve.move((0pt, 4pt)),
    curve.line((w * 0.34, 4pt)),
    curve.line((w * 0.39, 1.5pt)),
    curve.line((w * 0.44, 7.5pt)),
    curve.line((w * 0.49, 0.5pt)),
    curve.line((w * 0.54, 5.2pt)),
    curve.line((w * 0.59, 4pt)),
    curve.line((w, 4pt)),
  )
}}

#set text(
  font: "Vazirmatn",
  size: {typ.body - 0.4:.2f}pt,
  lang: "fa",
  region: "ir",
  dir: rtl,
  fill: c-body,
)

#set par(
  justify: true,
  leading: 0.78em,
  spacing: 0.72em,
)

// Professor-emphasized inline highlight: wraps cleanly across line breaks.
#let gamas-em(body) = highlight(
  fill: c-highlight,
  top-edge: "ascender",
  bottom-edge: "descender",
  extent: 1.5pt,
  radius: 2.5pt,
)[#text(weight: "bold", fill: c-navy-900)[#body]]

#let gamas-dot(col, size: 5pt) = box(
  baseline: -1pt,
  rect(width: size, height: size, radius: 1.2pt, fill: col),
)

// --- Cover background (Page 1 only) ---
#let cover-bg = {{
  let pw = {frame.page_width:.2f}pt
  let ph = {frame.page_height:.2f}pt
  let fi = {frame.frame_inset:.2f}pt
  place(top + left, rect(width: pw, height: ph, fill: c-cream-50, stroke: none))
  // Soft editorial teal/cyan decorative sweeps behind upper cover artwork.
  place(top + left, dy: ph * 0.24, curve(
    stroke: 1.05pt + c-teal-400,
    curve.move((0pt, 32pt)),
    curve.cubic((pw * 0.30, -14pt), (pw * 0.65, 56pt), (pw, 8pt)),
  ))
  place(top + left, dy: ph * 0.285, curve(
    stroke: 0.7pt + c-cyan-500,
    curve.move((0pt, 30pt)),
    curve.cubic((pw * 0.32, -8pt), (pw * 0.66, 52pt), (pw, 12pt)),
  ))
  // Outer navy frame + inner cyan hairline.
  place(top + left, dx: fi, dy: fi, rect(
    width: pw - 2 * fi,
    height: ph - 2 * fi,
    stroke: 1.2pt + c-navy-900,
    fill: none,
  ))
  place(top + left, dx: fi + 5pt, dy: fi + 5pt, rect(
    width: pw - 2 * (fi + 5pt),
    height: ph - 2 * (fi + 5pt),
    stroke: 0.35pt + c-cyan-500,
    fill: none,
  ))
}}

// --- Body page background (subtle frame border) ---
#let body-bg = {{
  let pw = {frame.page_width:.2f}pt
  let ph = {frame.page_height:.2f}pt
  let fi = {frame.frame_inset:.2f}pt
  place(top + left, dx: fi, dy: fi, rect(
    width: pw - 2 * fi,
    height: ph - 2 * fi,
    stroke: 0.55pt + c-line,
    fill: none,
  ))
}}

// --- Body running header & footer (kept strictly inside QA header/footer bands) ---
#let body-header = {{
  set text(size: 8.2pt)
  grid(
    columns: (1fr, auto),
    align: (right + bottom, left + bottom),
    text(fill: c-muted)[{safe_title}],
    text(dir: ltr, weight: "bold", fill: c-teal-600, tracking: 0.8pt)[GAMAS],
  )
  v(-5pt)
  line(length: 100%, stroke: 0.55pt + c-cyan-500)
}}

#let body-footer = context {{
  let pg = counter(page).get().first()
  set text(size: 8.2pt, fill: c-muted)
  line(length: 100%, stroke: 0.45pt + c-line)
  v(-4pt)
  grid(
    columns: (1fr, auto, 1fr),
    align: (right + horizon, center + horizon, left + horizon),
    [#ecg-rule(w: 44pt, col: c-teal-400)],
    [GAMAS · صفحهٔ #fa-digits(pg)],
    [],
  )
}}

#set page(
  width: {frame.page_width:.2f}pt,
  height: {frame.page_height:.2f}pt,
  margin: (
    left: {frame.margin_lr:.2f}pt,
    right: {frame.margin_lr:.2f}pt,
    top: {frame.margin_top:.2f}pt,
    bottom: {frame.margin_bottom:.2f}pt,
  ),
  header-ascent: 16pt,
  footer-descent: 15pt,
  background: body-bg,
  header: body-header,
  footer: body-footer,
)

// --- Styled Outline (Table of Contents) ---
#show outline.entry: it => {{
  let loc = it.element.location()
  let pg = counter(page).at(loc).first()
  link(loc, block(
    width: 100%,
    below: 4pt,
    inset: (x: 4pt, y: 4pt),
    stroke: (bottom: 0.4pt + c-line),
  )[
    #grid(
      columns: (1fr, auto),
      gutter: 10pt,
      align: (right + horizon, left + horizon),
      text(weight: "bold", fill: c-navy-900, size: 9.6pt)[#it.element.body],
      box(
        fill: c-key-fill,
        stroke: 0.5pt + c-teal-400,
        inset: (x: 7pt, y: 2pt),
        radius: 3pt,
      )[#text(weight: "bold", fill: c-teal-700, size: 8.5pt)[صفحهٔ #fa-digits(pg)]],
    )
  ])
}}

// --- Headings (invisible visual override: components supply styled banners) ---
#show heading.where(level: 1): it => {{
  // Rendered via gamas-chapter / gamas-section-banner helpers below.
  it
}}

// --- Editorial Components ---
#let gamas-display-heading(label) = block(sticky: true, above: {spc.md:.1f}pt, below: 6pt)[
  #stack(
    dir: ttb,
    spacing: 3pt,
    text(size: {typ.h2:.1f}pt, weight: "bold", fill: c-navy-900)[#label],
    line(length: 48pt, stroke: 1.8pt + c-teal-500),
  )
]

#let gamas-subheading(label) = block(sticky: true, above: 9pt, below: 5pt)[
  #grid(
    columns: (auto, 1fr),
    gutter: 6pt,
    align: (right + horizon, right + horizon),
    rect(width: 4pt, height: 12pt, radius: 1.5pt, fill: c-teal-500),
    text(size: {typ.h3:.1f}pt, weight: "bold", fill: c-navy-800)[#label],
  )
]

#let gamas-chapter(chapter-num, title-body, outline-text, locators: none) = {{
  block(sticky: true, above: 13pt, below: 7pt, breakable: false)[
    #if chapter-num != none [
      #box(
        fill: c-teal-600,
        inset: (x: 8pt, y: 2.5pt),
        radius: (top: 4pt, bottom: 0pt),
      )[#text(size: 8.2pt, weight: "bold", fill: c-on-dark)[فصل #chapter-num]]
      #v(-1pt)
    ]
    #block(
      width: 100%,
      fill: c-navy-900,
      stroke: (right: 4pt + c-cyan-500),
      radius: (left: 5pt, bottom-right: 5pt, top-right: if chapter-num != none {{ 0pt }} else {{ 5pt }}),
      inset: (x: 12pt, y: 8.5pt),
    )[
      #show heading.where(level: 1): it => text(
        size: {typ.banner:.1f}pt,
        weight: "bold",
        fill: c-on-dark,
      )[#title-body]
      #heading(level: 1, outlined: true)[#outline-text]
    ]
    #v(4pt)
    #align(center)[#ecg-rule(w: 96pt, col: c-teal-400)]
    #if locators != none [
      #v(2pt)
      #text(size: 8.2pt, fill: c-muted)[#locators]
    ]
  ]
}}

#let gamas-card(fill-col, border-col, body, pad-x: 11pt, pad-y: 8.5pt, breakable: true) = block(
  width: 100%,
  breakable: breakable,
  above: 4pt,
  below: 7pt,
  fill: fill-col,
  stroke: (
    right: 3.4pt + border-col,
    left: 0.45pt + c-line,
    top: 0.45pt + c-line,
    bottom: 0.45pt + c-line,
  ),
  radius: (left: 4.5pt),
  inset: (x: pad-x, y: pad-y),
)[#body]

#let gamas-definition(term, term-en: none, body) = block(
  width: 100%,
  breakable: false,
  above: 5pt,
  below: 7pt,
  fill: c-def-fill,
  stroke: (
    right: 3.5pt + c-def-border,
    left: 0.45pt + c-line,
    top: 0.45pt + c-line,
    bottom: 0.45pt + c-line,
  ),
  radius: (left: 5pt),
  inset: (x: 11pt, y: 8pt),
)[
  #grid(
    columns: if term-en != none {{ (1fr, auto) }} else {{ (1fr,) }},
    gutter: 8pt,
    align: (right + horizon, left + horizon),
    text(weight: "bold", fill: c-navy-900, size: 10.4pt)[#term],
    ..if term-en != none {{
      (box(
        fill: white,
        stroke: 0.6pt + c-def-border,
        inset: (x: 7pt, y: 2pt),
        radius: 3.5pt,
      )[#text(dir: ltr, size: 8.6pt, weight: "bold", fill: c-teal-700)[#term-en]],)
    }} else {{ () }}
  )
  #v(4pt)
  #text(fill: c-body, size: 10pt)[#body]
]

#let gamas-formula(body, is-ltr: false) = block(
  width: 100%,
  breakable: false,
  above: 5pt,
  below: 7pt,
  fill: c-form-fill,
  stroke: 0.85pt + c-form-border,
  radius: 5pt,
  inset: (x: 12pt, y: 8.5pt),
)[
  #align(right)[
    #box(
      fill: white,
      stroke: 0.45pt + c-form-border,
      inset: (x: 6pt, y: 1.5pt),
      radius: 3pt,
    )[#text(size: 7.8pt, weight: "bold", fill: c-teal-700)[فرمول / رابطهٔ علمی]]
  ]
  #v(4pt)
  #if is-ltr [
    #block(width: 100%)[
      #set text(dir: ltr)
      #set par(justify: false)
      #align(center)[#text(weight: "bold", size: 10.6pt, fill: c-navy-900)[#body]]
    ]
  ] else [
    #align(center)[#text(weight: "bold", size: 10.4pt, fill: c-navy-900)[#body]]
  ]
]

#let gamas-example(num, body) = block(
  width: 100%,
  breakable: true,
  above: 4pt,
  below: 7pt,
  fill: c-ex-fill,
  stroke: (
    right: 3.4pt + c-ex-border,
    left: 0.45pt + c-line,
    top: 0.45pt + c-line,
    bottom: 0.45pt + c-line,
  ),
  radius: (left: 4.5pt),
  inset: (x: 11pt, y: 8pt),
)[
  #box(
    fill: c-ex-border,
    inset: (x: 6.5pt, y: 1.8pt),
    radius: 3pt,
  )[#text(size: 8pt, weight: "bold", fill: white)[مثال #fa-digits(num)]]
  #v(4pt)
  #text(fill: c-ink, size: 10pt)[#body]
]

#let gamas-callout(kind, body) = {{
  let spec = callout-specs.at(kind, default: callout-specs.at("نکته"))
  block(
    width: 100%,
    breakable: true,
    above: 5pt,
    below: 7pt,
    fill: spec.fill,
    stroke: (
      right: 3.8pt + spec.border,
      left: 0.5pt + spec.border,
      top: 0.5pt + spec.border,
      bottom: 0.5pt + spec.border,
    ),
    radius: (left: 5pt),
    inset: (x: 11pt, y: 8.5pt),
  )[
    #grid(
      columns: (auto, 1fr),
      gutter: 5pt,
      align: (right + horizon, right + horizon),
      gamas-dot(spec.border, size: 6pt),
      text(weight: "bold", size: 9.4pt, fill: spec.title-color)[#spec.label],
    )
    #v(3pt)
    #text(fill: spec.body-color, size: 10pt)[#body]
  ]
}}

#let gamas-tip(kind, body) = {{
  let spec = tip-specs.at(kind, default: tip-specs.at("کاربردی"))
  block(
    width: 100%,
    breakable: true,
    above: 4pt,
    below: 6pt,
    fill: spec.fill,
    stroke: (
      right: 3.4pt + spec.border,
      left: 0.45pt + c-line,
      top: 0.45pt + c-line,
      bottom: 0.45pt + c-line,
    ),
    radius: (left: 4.5pt),
    inset: (x: 11pt, y: 8pt),
  )[
    #box(
      fill: spec.border,
      inset: (x: 6.5pt, y: 1.8pt),
      radius: 3pt,
    )[#text(size: 8pt, weight: "bold", fill: white)[نکتهٔ #kind]]
    #v(3.5pt)
    #text(fill: spec.body-color, size: 10pt)[#body]
  ]
}}

#let gamas-high-yield(title, items, fill-col: c-key-fill, border-col: c-key-border, title-col: c-teal-700) = block(
  width: 100%,
  breakable: true,
  above: 5pt,
  below: 8pt,
  fill: fill-col,
  stroke: (
    right: 3.5pt + border-col,
    left: 0.45pt + c-line,
    top: 0.45pt + c-line,
    bottom: 0.45pt + c-line,
  ),
  radius: (left: 5pt),
  inset: (x: 11pt, y: 8.5pt),
)[
  #text(weight: "bold", size: 9.8pt, fill: title-col)[#title]
  #v(4pt)
  #for item in items [
    #grid(
      columns: (auto, 1fr),
      gutter: 6.5pt,
      align: (right + top, right + top),
      box(baseline: -2.5pt, rect(width: 4.5pt, height: 4.5pt, radius: 1pt, fill: border-col)),
      text(size: 9.9pt, fill: c-ink)[#item],
    )
    #v(2.5pt)
  ]
]

#let gamas-steps(items) = block(width: 100%, above: 4pt, below: 8pt)[
  #for (idx, item) in items.enumerate() [
    #block(
      width: 100%,
      breakable: false,
      below: 4.5pt,
      fill: if calc.rem(idx, 2) == 0 {{ c-cream-50 }} else {{ white }},
      stroke: 0.45pt + c-line,
      radius: 4.5pt,
      inset: (x: 9pt, y: 6.5pt),
    )[
      #grid(
        columns: (auto, 1fr),
        gutter: 8pt,
        align: (center + horizon, right + horizon),
        box(
          width: 20pt,
          height: 20pt,
          radius: 10pt,
          fill: c-navy-900,
        )[#align(center + horizon)[#text(size: 8.8pt, weight: "bold", fill: white)[#fa-digits(idx + 1)]]],
        text(size: 10pt, fill: c-body)[#item],
      )
    ]
  ]
]

#let gamas-qa(num, kind, question, answer: none) = block(
  width: 100%,
  breakable: false,
  above: 5pt,
  below: 7pt,
  fill: white,
  stroke: 0.65pt + c-qa-border,
  radius: 5pt,
  clip: true,
)[
  #block(
    width: 100%,
    fill: c-qa-fill,
    stroke: (bottom: 0.5pt + c-qa-border),
    inset: (x: 11pt, y: 7.5pt),
  )[
    #grid(
      columns: (auto, 1fr, auto),
      gutter: 7pt,
      align: (right + top, right + top, left + top),
      box(
        fill: c-navy-900,
        inset: (x: 6pt, y: 1.8pt),
        radius: 3pt,
      )[#text(size: 8.2pt, weight: "bold", fill: white)[پرسش #fa-digits(num)]],
      text(weight: "bold", size: 10pt, fill: c-navy-900)[#question],
      ..if kind != "" {{
        (box(
          fill: white,
          stroke: 0.5pt + c-qa-border,
          inset: (x: 5.5pt, y: 1.5pt),
          radius: 3pt,
        )[#text(size: 7.8pt, weight: "bold", fill: c-teal-700)[#kind]],)
      }} else {{ () }}
    )
  ]
  #if answer != none [
    #block(
      width: 100%,
      fill: white,
      inset: (x: 11pt, y: 7.5pt),
    )[
      #text(weight: "bold", size: 9.2pt, fill: c-teal-700)[پاسخ: ]
      #text(size: 9.8pt, fill: c-body)[#answer]
    ]
  ]
]
'''


def _cover_markup(notes: StructuredNotes) -> str:
    """Render Page 1 editorial cover matching the Gamas visual identity."""
    topic = topic_for(booklet_text(notes))
    monogram = _asset_typst_path("brand/monogram")
    hero = _asset_typst_path(topic.cover)

    title_markup = _typst_inline(
        notes.display_title,
        highlight_emphasis=False,
        style_latin=False,
        emphasis_color="c-on-dark",
    )
    subtitle_markup = (
        _typst_inline(
            notes.subtitle,
            highlight_emphasis=False,
            style_latin=False,
            emphasis_color="c-cyan-300",
        )
        if notes.subtitle
        else ""
    )

    stats: list[tuple[str, str]] = []
    if notes.sections:
        stats.append((to_persian_digits(len(notes.sections)), "فصل آموزشی"))
    term_count = len(notes.glossary) + sum(len(s.definitions) for s in notes.sections)
    if term_count:
        stats.append((to_persian_digits(term_count), "اصطلاح علمی"))
    table_count = sum(len(s.all_tables) for s in notes.sections)
    qa_count = len(notes.practice_questions) + len(notes.review_questions)
    if table_count:
        stats.append((to_persian_digits(table_count), "جدول مقایسه‌ای"))
    elif qa_count:
        stats.append((to_persian_digits(qa_count), "پرسش تمرینی"))

    parts: list[str] = [
        "#page(background: cover-bg, header: none, footer: none)[",
        "  #set par(justify: false)",
        '  #align(center)[#text(size: 11.5pt, fill: c-navy-800, weight: "bold")[به نام خدا]]',
        "  #v(6pt)",
    ]
    if monogram:
        parts.append(f'  #align(center)[#image("{monogram}", width: 2.1cm, height: 2.1cm)]')
        parts.append("  #v(3pt)")
    parts.extend([
        '  #align(center)[#text(dir: ltr, size: 17pt, weight: "bold", fill: c-navy-900, tracking: 2pt)[GAMAS]]',
        "  #v(-4pt)",
        '  #align(center)[#text(dir: ltr, size: 7.6pt, weight: "bold", fill: c-teal-600, tracking: 1.4pt)[SMART STUDY ASSISTANT]]',
        "  #v(8pt)",
        "  #align(center)[#block(",
        "    width: 94%,",
        "    fill: c-navy-900,",
        "    clip: true,",
        "    radius: 9pt,",
        "    inset: (x: 18pt, y: 15pt),",
        "  )[",
        "    #align(center)[#rect(width: 64pt, height: 2.6pt, radius: 1.3pt, fill: c-cyan-500)]",
        "    #v(8pt)",
        f'    #align(center)[#box(fill: c-teal-600, inset: (x: 10pt, y: 2.5pt), radius: 10pt)[#text(size: 8.6pt, weight: "bold", fill: white)[جزوهٔ علمی · {_escape_typst(topic.label)}]]]',
        "    #v(7pt)",
        f'    #align(center)[#text(size: 18pt, weight: "bold", fill: c-on-dark)[{title_markup}]]',
    ])
    if subtitle_markup:
        parts.append("    #v(5pt)")
        parts.append(
            f'    #align(center)[#text(size: 10.6pt, fill: c-cyan-300)[{subtitle_markup}]]'
        )
    parts.append("  ]]")

    if stats:
        stat_cells = ", ".join(
            f'box(fill: white, stroke: 0.6pt + c-line, radius: 5pt, inset: (x: 10pt, y: 5pt))['
            f'#text(weight: "bold", size: 10.5pt, fill: c-teal-700)[{_escape_typst(val)}] '
            f'#text(size: 8.5pt, fill: c-muted)[{_escape_typst(lbl)}]]'
            for val, lbl in stats[:3]
        )
        parts.append("  #v(7pt)")
        parts.append(
            f"  #align(center)[#grid(columns: ({', '.join(['auto'] * len(stats[:3]))}), gutter: 8pt, {stat_cells})]"
        )

    if hero:
        parts.append("  #v(8pt)")
        parts.append(
            f'  #align(center)[#box(radius: 6pt, clip: true, stroke: 0.6pt + c-line)[#image("{hero}", width: 92%)]]'
        )

    parts.extend([
        "  #v(1fr)",
        "  #block(",
        "    width: 100%,",
        "    fill: c-quote-fill,",
        "    stroke: (right: 3.2pt + c-quote-border, left: 0.4pt + c-line, top: 0.4pt + c-line, bottom: 0.4pt + c-line),",
        "    radius: (left: 4.5pt),",
        "    inset: (x: 12pt, y: 7.5pt),",
        f'  )[#align(center)[#text(size: 9.6pt, fill: c-navy-800)[«{_escape_typst(QUOTE_TEXT)}»]]]',
        "  #v(6pt)",
        '  #align(center)[#text(size: 8.6pt, fill: c-muted)[Gamas · دستیار مطالعهٔ هوشمند]]',
        "]",
        "#counter(page).update(1)",
    ])
    return "\n".join(parts)


def _table_markup(table: NoteTable) -> str:
    """Render a comparative table with repeating navy header and zebra rows."""
    headers = list(table.headers) or [""]
    width = len(headers)
    rows = [list(row) + [""] * (width - len(row)) for row in table.rows]
    weights = _column_fractions(headers, rows)
    col_spec = ", ".join(f"{w}fr" for w in weights)

    out: list[str] = ["\n#block(width: 100%, above: 6pt, below: 9pt)["]
    if table.caption:
        cap = _typst_inline(table.caption, highlight_emphasis=False)
        out.append(
            "  #block(width: 100%, sticky: true, fill: c-cream-100, "
            "stroke: (bottom: 1pt + c-cyan-500, top: 0.45pt + c-line, left: 0.45pt + c-line, right: 0.45pt + c-line), "
            f'radius: (top: 4pt), inset: (x: 9pt, y: 5pt))[#text(weight: "bold", size: 9.2pt, fill: c-navy-900)[{cap}]]'
        )
        out.append("  #v(-1pt)")

    out.extend([
        "  #set par(justify: false, leading: 0.62em)",
        "  #table(",
        f"    columns: ({col_spec}),",
        "    align: right + horizon,",
        "    inset: (x: 7pt, y: 5.5pt),",
        "    stroke: 0.45pt + c-line,",
        "    fill: (_, y) => if y == 0 { c-navy-900 } else if calc.rem(y, 2) == 0 { c-zebra } else { white },",
        "    table.header(",
    ])
    for header in headers:
        h_text = _typst_inline(
            header or " ",
            highlight_emphasis=False,
            style_latin=False,
            emphasis_color="c-on-dark",
        )
        out.append(f'      text(weight: "bold", size: 9.2pt, fill: c-on-dark)[{h_text}],')
    out.append("    ),")

    for row in rows:
        for col_idx, cell in enumerate(row[:width]):
            c_text = _typst_inline(cell or " ", highlight_emphasis=True)
            if col_idx == 0:
                out.append(f'    text(weight: "bold", size: 9.1pt, fill: c-navy-900)[{c_text}],')
            else:
                out.append(f"    text(size: 9.1pt, fill: c-body)[{c_text}],")
    out.append("  )")
    out.append("]")
    return "\n".join(out)


def _section_markup(section: NoteSection, index: int) -> str:
    """Render one structured chapter with all educational blocks."""
    ch_num = to_persian_digits(index)
    heading_plain = strip_emphasis_markers(_clean_text(section.heading))
    heading_body = _typst_inline(
        section.heading,
        highlight_emphasis=False,
        style_latin=False,
        emphasis_color="c-on-dark",
    )
    outline_entry = _escape_typst(f"فصل {ch_num}: {heading_plain}")
    locators = (
        f'"منبع: {_escape_typst_string("، ".join(section.source_locators))}"'
        if section.source_locators
        else "none"
    )
    parts: list[str] = [
        f'#gamas-chapter("{ch_num}", [{heading_body}], [{outline_entry}], locators: {locators})'
    ]

    for paragraph in section.paragraphs:
        body = _typst_inline(paragraph)
        if body:
            parts.append(f"{body}\n")

    for definition in section.definitions:
        term = _typst_inline(definition.term, highlight_emphasis=False, style_latin=False)
        body = _typst_inline(definition.definition)
        if definition.term_en:
            en_esc = _escape_typst(_clean_text(definition.term_en))
            parts.append(f"#gamas-definition([{term}], term-en: [{en_esc}], [{body}])")
        else:
            parts.append(f"#gamas-definition([{term}], [{body}])")

    if section.bullets:
        parts.append("#block(width: 100%, above: 3pt, below: 6pt)[")
        for bullet in section.bullets:
            b_text = _typst_inline(bullet)
            parts.append(
                "  #grid(columns: (auto, 1fr), gutter: 6pt, align: (right + top, right + top), "
                f'text(weight: "bold", fill: c-teal-600)[•], [{b_text}])'
            )
            parts.append("  #v(2.5pt)")
        parts.append("]")

    if section.steps:
        step_items = ", ".join(f"[{_typst_inline(step)}]" for step in section.steps)
        parts.append('#gamas-subheading("مراحل و روند انجام")')
        parts.append(f"#gamas-steps(({step_items},))")

    if section.formulas:
        parts.append('#gamas-subheading("فرمول‌ها و روابط علمی")')
        for formula in section.formulas:
            is_ltr = _is_ltr_formula(formula)
            f_text = _typst_inline(formula, highlight_emphasis=False, style_latin=not is_ltr)
            ltr_flag = "true" if is_ltr else "false"
            parts.append(f"#gamas-formula([{f_text}], is-ltr: {ltr_flag})")

    if section.examples:
        parts.append('#gamas-subheading("مثال‌های کاربردی")')
        for ex_idx, example in enumerate(section.examples, start=1):
            parts.append(f"#gamas-example({ex_idx}, [{_typst_inline(example)}])")

    for callout in section.callouts:
        kind_esc = _escape_typst_string(callout.kind)
        parts.append(f'#gamas-callout("{kind_esc}", [{_typst_inline(callout.text)}])')

    for table in section.all_tables:
        parts.append(_table_markup(table))

    if section.key_points:
        kp_items = ", ".join(f"[{_typst_inline(kp)}]" for kp in section.key_points)
        parts.append(
            f'#gamas-high-yield("نکته‌های کلیدی این فصل", ({kp_items},))'
        )

    return "\n".join(parts)


def _glossary_markup(notes: StructuredNotes) -> str:
    """Render the bilingual Persian/English scientific glossary table."""
    out: list[str] = [
        f'#gamas-chapter(none, [{_escape_typst(design.LABELS["glossary"])}], [{_escape_typst(design.LABELS["glossary"])}])',
        "#set par(justify: false, leading: 0.65em)",
        "#table(",
        "  columns: (1.15fr, 1.85fr),",
        "  align: right + horizon,",
        "  inset: (x: 8pt, y: 6pt),",
        "  stroke: 0.45pt + c-line,",
        "  fill: (_, y) => if y == 0 { c-navy-900 } else if calc.rem(y, 2) == 0 { c-zebra } else { white },",
        "  table.header(",
        '    text(weight: "bold", size: 9.4pt, fill: c-on-dark)[اصطلاح علمی (فارسی / English)],',
        '    text(weight: "bold", size: 9.4pt, fill: c-on-dark)[تعریف و توضیح],',
        "  ),",
    ]
    for entry in notes.glossary:
        term = _typst_inline(entry.term, highlight_emphasis=False, style_latin=False)
        defn = _typst_inline(entry.definition)
        if entry.term_en:
            en_esc = _escape_typst(_clean_text(entry.term_en))
            term_cell = (
                f'stack(dir: ttb, spacing: 3pt, '
                f'text(weight: "bold", size: 9.4pt, fill: c-navy-900)[{term}], '
                f'text(dir: ltr, size: 8.4pt, weight: "medium", fill: c-teal-700)[{en_esc}])'
            )
        else:
            term_cell = f'text(weight: "bold", size: 9.4pt, fill: c-navy-900)[{term}]'
        out.append(f"  {term_cell},")
        out.append(f"  text(size: 9.2pt, fill: c-body)[{defn}],")
    out.append(")")
    return "\n".join(out)


def _practice_questions_markup(questions: Sequence[PracticeQuestion]) -> str:
    """Render course practice/exam questions with their answers."""
    label = _escape_typst(design.LABELS["practice_questions"])
    parts = [f"#gamas-chapter(none, [{label}], [{label}])"]
    for idx, qa in enumerate(questions, start=1):
        q_text = _typst_inline(qa.question, highlight_emphasis=False)
        kind = _escape_typst_string(qa.kind)
        if qa.answer:
            a_text = _typst_inline(qa.answer)
            parts.append(f'#gamas-qa({idx}, "{kind}", [{q_text}], answer: [{a_text}])')
        else:
            parts.append(f'#gamas-qa({idx}, "{kind}", [{q_text}])')
    return "\n".join(parts)


def render_typst_source(notes: StructuredNotes) -> str:
    """Build the complete Typst source document for ``notes``."""
    parts: list[str] = [_preamble(notes.display_title), _cover_markup(notes)]

    # --- Opening Overview Spread: TOC + At a Glance + Objectives + Summary ---
    if len(notes.sections) >= 2 or notes.glossary or notes.practice_questions:
        parts.append(f'#gamas-display-heading("{_escape_typst_string(design.LABELS["toc"])}")')
        parts.append("#outline(title: none, depth: 1)")
        parts.append("#v(8pt)")

    if notes.learning_objectives:
        obj_items = ", ".join(f"[{_typst_inline(item)}]" for item in notes.learning_objectives)
        parts.append(f'#gamas-display-heading("{_escape_typst_string(design.LABELS["objectives"])}")')
        parts.append(
            f'#gamas-high-yield("آنچه در این جزوه می‌آموزید", ({obj_items},), '
            "fill-col: c-obj-fill, border-col: c-obj-border, title-col: c-teal-700)"
        )

    if notes.summary:
        parts.append(f'#gamas-display-heading("{_escape_typst_string(design.LABELS["summary"])}")')
        parts.append(
            f"#gamas-card(c-sum-fill, c-sum-border, [{_typst_inline(notes.summary)}])"
        )

    # --- Main Chapters ---
    for index, section in enumerate(notes.sections, start=1):
        parts.append(_section_markup(section, index))

    # --- Closing Synthesis & Exam-Prep Sections ---
    if notes.key_points:
        kp_label = _escape_typst(design.LABELS["key_points"])
        kp_items = ", ".join(f"[{_typst_inline(kp)}]" for kp in notes.key_points)
        parts.append(f"#gamas-chapter(none, [{kp_label}], [{kp_label}])")
        parts.append(f'#gamas-high-yield("مهم‌ترین نکته‌های کلیدی جزوه", ({kp_items},))')

    if notes.practical_tips:
        tips_label = _escape_typst(design.LABELS["tips"])
        parts.append(f"#gamas-chapter(none, [{tips_label}], [{tips_label}])")
        for tip in notes.practical_tips:
            kind_esc = _escape_typst_string(tip.kind)
            is_dark = tip.kind == "آزمونی"
            tip_body = _typst_inline(
                tip.text,
                highlight_emphasis=not is_dark,
                emphasis_color="c-cyan-300" if is_dark else "c-navy-900",
                latin_color="c-cyan-300" if is_dark else "c-teal-700",
            )
            parts.append(f'#gamas-tip("{kind_esc}", [{tip_body}])')

    if notes.practice_questions:
        parts.append(_practice_questions_markup(notes.practice_questions))

    if notes.review_questions:
        rev_label = _escape_typst(design.LABELS["review"])
        parts.append(f"#gamas-chapter(none, [{rev_label}], [{rev_label}])")
        rev_items = ", ".join(f"[{_typst_inline(q)}]" for q in notes.review_questions)
        parts.append(f"#gamas-steps(({rev_items},))")

    if notes.common_mistakes:
        pit_label = _escape_typst(design.LABELS["common_mistakes"])
        pit_items = ", ".join(f"[{_typst_inline(item)}]" for item in notes.common_mistakes)
        parts.append(f"#gamas-chapter(none, [{pit_label}], [{pit_label}])")
        parts.append(
            f'#gamas-high-yield("خطاها، هشدارها و دام‌های رایج", ({pit_items},), '
            "fill-col: c-pit-fill, border-col: c-pit-border, title-col: c-danger)"
        )

    if notes.glossary:
        parts.append(_glossary_markup(notes))

    if notes.final_review:
        fr_label = _escape_typst(design.LABELS["final_review"])
        fr_items = ", ".join(f"[{_typst_inline(item)}]" for item in notes.final_review)
        parts.append(f"#gamas-chapter(none, [{fr_label}], [{fr_label}])")
        parts.append(
            f'#gamas-high-yield("نکته‌های ضروری برای مرور پایانی", ({fr_items},))'
        )

    return "\n\n".join(parts) + "\n"


def build_notes_pdf_typst(
    notes: StructuredNotes,
    *,
    meta: DocumentMeta | None = None,
    duration_seconds: int | None = None,
    session_number: int | None = None,
) -> bytes:
    """Compile ``notes`` to a deterministic A4 Persian PDF via ``typst``.

    Operational arguments (``meta``, ``duration_seconds``, ``session_number``)
    are accepted for signature parity with :func:`gamas_bot.notes_pdf.build_notes_pdf`
    and deliberately never rendered into the educational booklet.
    """
    _ = (meta, duration_seconds, session_number)
    import typst

    source = render_typst_source(notes)
    compile_fn: Any = typst.compile
    pdf_bytes = compile_fn(
        source.encode("utf-8"),
        root=str(PROJECT_DIR),
        font_paths=[str(_FONT_DIR)],
        ignore_system_fonts=True,
        timestamp=0,
    )
    if isinstance(pdf_bytes, list):  # pragma: no cover - PDF format always returns bytes
        return b"".join(pdf_bytes)
    return bytes(pdf_bytes)


__all__ = [
    "QUOTE_TEXT",
    "build_notes_pdf_typst",
    "render_typst_source",
    "typst_available",
]
