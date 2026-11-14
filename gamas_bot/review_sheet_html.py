"""Deterministic renderer for the offline interactive review sheet.

The model never writes HTML, CSS or JavaScript. This module turns a validated
:class:`~gamas_bot.review_sheet.ReviewSheetPlan` into one self-contained file
built from the template in ``assets/review_sheet/skeleton.html``:

* the skeleton's CSS and JavaScript are used byte-for-byte. The only edits are
  the two Google Fonts ``<link>`` lines (removed, because the sheet must work
  offline) and one ``@font-face`` block placed at the top of the same
  ``<style>`` element, which embeds the project's Vazirmatn faces (SIL OFL,
  ``assets/fonts``) as ``data:`` URIs;
* only the six placeholders ``{{TITLE_TAG}}``, ``{{KICKER}}``, ``{{H1}}``,
  ``{{MINUTES_FA}}``, ``{{SECTIONS}}`` and ``{{FOOTER}}`` are filled, in a single
  pass so inserted text is never scanned for placeholders again;
* section markup follows the component catalogue of the review-sheet template
  specification (``.sec``, ``.sh``, ``.key``, ``.trap``, ``.tip``, ``.card``,
  ``.tw``, ``details``). Every dynamic value is HTML-escaped; Latin runs are
  wrapped in ``<e>`` and digits are Persian outside it;
* the Content-Security-Policy pins the one inline script by SHA-256. The one
  inline style is allowed inline because the template sets section colours
  with ``style=`` attributes; ``default-src 'none'`` still blocks every network
  request. The validator in :mod:`gamas_bot.artifact_validation` re-checks all of
  this on the rendered bytes.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from .config import PROJECT_DIR
from .docx_export import sanitize_filename_part
from .notes import QUALITY_VERIFIED
from .pdf_common import BOLD_FILE, REGULAR_FILE, find_font
from .progress import to_persian_digits
from .review_sheet import ReviewSection, ReviewSheetPlan, ReviewUnit


class ReviewSheetRenderError(RuntimeError):
    """The sheet cannot be rendered on this host (missing font or template)."""


SKELETON_NAME: Final = "skeleton.html"
#: Placeholders the skeleton exposes; anything else in the template is a bug.
PLACEHOLDERS: Final = frozenset({"TITLE_TAG", "KICKER", "H1", "MINUTES_FA", "SECTIONS", "FOOTER"})

#: Every class name the template and the catalogue use. The validator rejects others.
ALLOWED_CLASSES: Final = frozenset(
    {
        # page chrome from the skeleton
        "wrap", "hero", "clock", "tbar", "legend", "chip", "sticky", "ok",
        # section chrome and the component catalogue (section 5 of the spec)
        "sec", "quiz", "sh", "ic", "done", "key", "trap", "tip", "card", "tw", "g2",
        "pill", "mono", "zone", "z1", "z2", "nd", "ar", "row3", "gray", "spec", "chain",
        "lad", "sw", "arrow", "fig", "eq",
    }
)

#: Section colour tokens in the template's rotation order (spec section 2).
COLOUR_ROTATION: Final = ("pri", "sec", "blu", "carb", "tl", "rx", "pk")
#: The self-test is neutral (spec section 3).
QUIZ_STYLE: Final = "--c:var(--ink);--cs:var(--line)"
#: Section header and trap/tip labels.
_LABEL_DEFINITION: Final = "تعریف"
_LABEL_FORMULA: Final = "فرمول"
_LABEL_KEY: Final = "نکتهٔ محوری"
_LABEL_EXAMPLE: Final = "مثال"
_LABEL_WARNING: Final = "هشدار"
_LABEL_TIP: Final = "یادت باشد"
_LABEL_TRAP: Final = "دام امتحانی"
_DONE_TEXT: Final = "✓ این بخش را مرور کردم"
_QUIZ_ICON: Final = "🧪"
_QUIZ_HINT: Final = "اول جواب را در ذهنت بساز، بعد باز کن"
_QUALITY_NOTE: Final = {
    "REPAIRED": "وضعیت تولید: اصلاح‌شده؛ محتوا را با متن جزوه تطبیق دهید.",
    "RECOVERED": "وضعیت تولید: بازیابی‌شده؛ محتوا را با متن جزوه تطبیق دهید.",
}

_GOOGLE_FONT_LINES: Final = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n',
    '<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800&display=swap" rel="stylesheet">\n',
)
_STYLE_OPEN: Final = "<style>\n"
_STYLE_CLOSE: Final = "\n</style>"
_SCRIPT_OPEN: Final = "<script>"
_SCRIPT_CLOSE: Final = "</script>"
_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
#: Latin runs (words, codes, formulas) that must be wrapped in ``<e>``.
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9\-]*(?:[ \t]+[A-Za-z][A-Za-z0-9\-]*)*")
_NAV_FORBIDDEN = re.compile(r"[<>&\"'`]")


@dataclass(frozen=True, slots=True)
class SkeletonParts:
    """The skeleton split into the parts the renderer and validator both need."""

    template: str
    #: The skeleton's own CSS, exactly as written in the file.
    style_body: str
    #: The skeleton's own script, exactly as written in the file.
    script_body: str


def _digest(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


def script_hash(script_body: str) -> str:
    """The CSP source expression for an inline script block."""
    return "'" + _digest(script_body) + "'"


def _skeleton_path() -> Path:
    for base in (PROJECT_DIR / "assets" / "review_sheet", Path.cwd() / "assets" / "review_sheet"):
        path = base / SKELETON_NAME
        if path.is_file():
            return path
    raise ReviewSheetRenderError(f"review sheet template {SKELETON_NAME} not found")


@lru_cache(maxsize=1)
def load_skeleton() -> SkeletonParts:
    """Read the template and check the structure the renderer depends on."""
    text = _skeleton_path().read_text(encoding="utf-8")
    for line in _GOOGLE_FONT_LINES:
        if text.count(line) != 1:
            raise ReviewSheetRenderError("review sheet template no longer matches the expected head")
    if text.count(_STYLE_OPEN) != 1 or text.count(_STYLE_CLOSE) != 1:
        raise ReviewSheetRenderError("review sheet template must contain exactly one <style> block")
    if text.count(_SCRIPT_OPEN) != 1 or text.count(_SCRIPT_CLOSE) != 1:
        raise ReviewSheetRenderError("review sheet template must contain exactly one <script> block")
    found = set(_PLACEHOLDER.findall(text))
    if found != PLACEHOLDERS:
        raise ReviewSheetRenderError(f"review sheet template placeholders differ: {sorted(found)}")
    style_start = text.index(_STYLE_OPEN) + len(_STYLE_OPEN)
    style_body = text[style_start : text.index(_STYLE_CLOSE)]
    script_start = text.index(_SCRIPT_OPEN) + len(_SCRIPT_OPEN)
    script_body = text[script_start : text.index(_SCRIPT_CLOSE)]
    template = text
    for line in _GOOGLE_FONT_LINES:
        template = template.replace(line, "")
    return SkeletonParts(template=template, style_body=style_body, script_body=script_body)


def _font_face(weight: int, filename: str) -> str:
    path = find_font(filename)
    if path is None:
        raise ReviewSheetRenderError(f"font file {filename} not found")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return (
        "@font-face{font-family:'Vazirmatn';font-style:normal;font-weight:"
        f"{weight};font-display:block;"
        f"src:url(data:font/ttf;base64,{encoded}) format('truetype');}}"
    )


def content_security_policy(script_body: str) -> str:
    """The CSP: one pinned script, inline styles, no network, data fonts only."""
    return (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        f"script-src {script_hash(script_body)}; "
        "font-src data:; img-src data:; "
        "base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'"
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _clean(value: object) -> str:
    """Single-line text without braces (braces would look like placeholders)."""
    return " ".join(str(value).replace("{", " ").replace("}", " ").split())


def _persian(segment: str) -> str:
    return _esc(to_persian_digits(segment))


def _inline(value: object) -> str:
    """Escaped text: Latin runs in ``<e>`` (LTR isolate), digits in Persian outside it."""
    raw = _clean(value)
    parts: list[str] = []
    position = 0
    for match in _LATIN_RUN.finditer(raw):
        parts.append(_persian(raw[position : match.start()]))
        parts.append(f"<e>{_esc(match.group(0))}</e>")
        position = match.end()
    parts.append(_persian(raw[position:]))
    return "".join(parts)


def _nav_label(value: object) -> str:
    """A short label safe for ``data-n`` and for the script's ``innerHTML`` chip."""
    cleaned = " ".join(_NAV_FORBIDDEN.sub(" ", _clean(value)).split())[:24].rstrip()
    return cleaned or "بخش"


def _colour_style(index: int) -> str:
    token = COLOUR_ROTATION[(index - 1) % len(COLOUR_ROTATION)]
    return f"--c:var(--{token});--cs:var(--{token}-s)"


def _unit_source(section: ReviewSection, evidence: tuple[str, ...], fallback: int) -> int:
    by_ref = {unit.ref: unit for unit in section.units}
    for ref in evidence:
        if ref in by_ref:
            return by_ref[ref].section_index
    return fallback


_LIST_KINDS: Final = frozenset({"bullet", "step", "key_point"})
_STRUCTURED_MARKERS: Final = ('class="key"', 'class="card"', 'class="tw"', 'class="trap"', 'class="tip"')


def _table_html(unit: ReviewUnit) -> str:
    head, *body = unit.table
    header = "".join(f"<th>{_inline(cell)}</th>" for cell in head)
    rows = "".join("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>" for row in body)
    return f'<div class="tw"><table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table></div>'


def _section_components(section: ReviewSection) -> str:
    """Catalogue components for a section, in source order, then its traps."""
    important = frozenset(section.important_refs)
    parts: list[str] = []
    items: list[str] = []

    def flush_list() -> None:
        if items:
            parts.append(f'<div class="card"><ul>{"".join(items)}</ul></div>')
            items.clear()

    for unit in section.units:
        if unit.ref in important and unit.kind not in {"table"}:
            flush_list()
            parts.append(f'<div class="key"><b>{_LABEL_KEY}</b>{_inline(unit.text)}</div>')
            continue
        if unit.kind in _LIST_KINDS:
            items.append(f"<li>{_inline(unit.text)}</li>")
            continue
        flush_list()
        if unit.kind == "table" and unit.table:
            parts.append(_table_html(unit))
        elif unit.kind == "callout":
            if unit.label.strip().lower() in {"warning", "caution"}:
                parts.append(f'<div class="trap"><b>{_LABEL_WARNING}</b>{_inline(unit.text)}</div>')
            else:
                parts.append(f'<div class="tip"><b>{_LABEL_TIP}</b>{_inline(unit.text)}</div>')
        elif unit.kind == "definition":
            parts.append(f'<div class="key"><b>{_LABEL_DEFINITION}</b>{_inline(unit.text)}</div>')
        elif unit.kind == "formula":
            parts.append(f'<div class="key"><b>{_LABEL_FORMULA}</b>{_inline(unit.text)}</div>')
        elif unit.kind == "example":
            parts.append(f'<div class="card"><h3>{_LABEL_EXAMPLE}</h3><p>{_inline(unit.text)}</p></div>')
        else:
            parts.append(f"<p>{_inline(unit.text)}</p>")
    flush_list()

    for trap in section.exam_traps:
        source = _unit_source(section, trap.source_evidence, section.source_section)
        parts.append(
            f'<div class="trap"><b>{_LABEL_TRAP}</b>{_inline(trap.explanation)}'
            f"<small>(منبع: بخش {to_persian_digits(source)})</small></div>"
        )
    if not any(marker in part for part in parts for marker in _STRUCTURED_MARKERS):
        parts = [f'<div class="card">{"".join(parts) or "<p>—</p>"}</div>']
    return "".join(parts)


def _section_html(index: int, section: ReviewSection) -> str:
    minutes = int(section.minutes)
    return (
        f'<section class="sec" id="s{index}" data-t="{minutes}" '
        f'data-n="{_esc(_nav_label(section.nav_label or section.heading))}" '
        f'style="{_colour_style(index)}">'
        f'<div class="sh"><span class="ic">{_esc(section.icon)}</span><div>'
        f"<h2>{_inline(section.heading)}</h2>"
        f"<small>⏱ {to_persian_digits(minutes)} دقیقه</small></div>"
        '<a href="#top">↑</a></div>'
        f"{_section_components(section)}"
        f'<button class="done">{_DONE_TEXT}</button></section>'
    )


def _quiz_html(index: int, plan: ReviewSheetPlan) -> str:
    minutes = int(plan.self_test_minutes)
    questions = "".join(
        f"<details><summary>{to_persian_digits(number)}. {_inline(item.question)}</summary>"
        f"<p>{_inline(item.answer)}</p></details>"
        for number, item in enumerate(plan.self_test, start=1)
    )
    pairs = ""
    if plan.frequent_pairs:
        rows = "".join(
            f"<tr><td>{_inline(pair.left_concept)}</td><td>{_inline(pair.right_concept)}</td>"
            f"<td>{_inline(pair.actual_distinction)}</td></tr>"
            for pair in plan.frequent_pairs
        )
        pairs = (
            "<h3>جفت‌های پرتکرار</h3>"
            '<div class="tw"><table><thead><tr><th>مفهوم اول</th><th>مفهوم دوم</th><th>تفاوت</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
    return (
        f'<section class="sec quiz" id="s{index}" data-t="{minutes}" data-n="خودآزمایی" style="{QUIZ_STYLE}">'
        f'<div class="sh"><span class="ic">{_QUIZ_ICON}</span><div><h2>خودآزمایی</h2>'
        f"<small>⏱ {to_persian_digits(minutes)} دقیقه؛ {_QUIZ_HINT}</small></div>"
        '<a href="#top">↑</a></div>'
        f"{questions}{pairs}"
        f'<button class="done">{_DONE_TEXT}</button></section>'
    )


def render_review_sheet_html(plan: ReviewSheetPlan) -> str:
    """One complete, self-contained HTML document for a validated plan."""
    skeleton = load_skeleton()
    title = _clean(plan.title) or "جزوهٔ کلاس"
    duration = int(plan.duration_minutes)
    sections = [_section_html(index, section) for index, section in enumerate(plan.sections, start=1)]
    sections.append(_quiz_html(len(plan.sections) + 1, plan))
    footer = _inline(f"منبع: {title}")
    note = _QUALITY_NOTE.get(plan.quality_state, "") if plan.quality_state != QUALITY_VERIFIED else ""
    if note:
        footer += f" · {_esc(note)}"
    values = {
        "TITLE_TAG": _esc(f"مرور {to_persian_digits(duration)} دقیقه‌ای · {title}"),
        "KICKER": _esc("جزوهٔ مرور امتحانی"),
        "H1": _inline(title),
        "MINUTES_FA": to_persian_digits(duration),
        "SECTIONS": "\n".join(sections),
        "FOOTER": footer,
    }
    # Fonts go at the top of the skeleton's own <style> block; its CSS follows verbatim.
    font_css = _font_face(400, REGULAR_FILE) + _font_face(700, BOLD_FILE)
    template = skeleton.template.replace(_STYLE_OPEN, _STYLE_OPEN + font_css + "\n", 1)
    # The policy is added to <head> only; it pins the skeleton's script by hash.
    if template.count("<title>") != 1:
        raise ReviewSheetRenderError("review sheet template must contain exactly one <title>")
    csp = content_security_policy(skeleton.script_body)
    meta = f'<meta http-equiv="Content-Security-Policy" content="{_esc(csp)}">\n'
    template = template.replace("<title>", meta + "<title>", 1)
    # One pass: inserted values are never scanned for placeholders.
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def review_sheet_filename(title: str, reference: str) -> str:
    """Persian ``.html`` filename, mirroring the PDF and DOCX naming scheme."""
    title_part = sanitize_filename_part(title)
    name = f"جزوه مرور تعاملی - {title_part}" if title_part else "جزوه مرور تعاملی"
    return f"{name} - {reference}.html"
