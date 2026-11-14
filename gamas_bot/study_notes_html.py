"""Deterministic renderer for the offline HTML study booklet.

The note model never writes HTML: it produces one validated
:class:`~gamas_bot.notes.StructuredNotes` document per job, and this module
turns that document into one self-contained, right-to-left HTML file:

* **no network at all** — the stylesheet is inline, the two Vazirmatn faces
  (SIL OFL, ``assets/fonts``) are embedded as ``data:`` URIs, there is no
  script, and a ``Content-Security-Policy`` meta pins ``default-src 'none'``;
* **no validation pipeline** — the renderer produces the document from a typed
  model, so the only technical check left is the minimal envelope/size check in
  :mod:`gamas_bot.artifact_validation`;
* **academic reading layout** — cover, table of contents, learning objectives,
  the booklet sections, key points, summary, quick review, important terms and
  a final review. Every block is rendered only when the source notes actually
  contain it; no section is invented here.

The transcript metadata (speaker names, chapter timestamps) is never rendered:
a study booklet must read like prepared course material, not like a transcript.
"""

from __future__ import annotations

import base64
import html
import re
from functools import lru_cache
from typing import Final

from .docx_export import sanitize_filename_part
from .notes import NoteSection, NoteTable, StructuredNotes
from .pdf_common import BOLD_FILE, REGULAR_FILE, find_font
from .progress import to_persian_digits


class StudyNotesRenderError(RuntimeError):
    """The booklet cannot be rendered on this host (no readable content)."""


#: Persian labels of the rendered blocks. Kept together so the wording is one
#: reviewable list instead of scattered literals.
BRAND_LATIN: Final = "GAMAS"
BRAND_PERSIAN: Final = "گاماس"
KICKER: Final = "جزوهٔ مطالعهٔ آفلاین"
DOC_TITLE_SUFFIX: Final = "جزوهٔ مطالعهٔ گاماس"
TOC_TITLE: Final = "فهرست مطالب"
OBJECTIVES_TITLE: Final = "اهداف یادگیری"
OBJECTIVES_INTRO: Final = "پس از مطالعهٔ این جزوه انتظار می‌رود این موارد را بفهمید:"
KEY_POINTS_TITLE: Final = "نکات کلیدی"
SUMMARY_TITLE: Final = "جمع‌بندی"
QUICK_REVIEW_TITLE: Final = "مرور سریع"
QUICK_REVIEW_INTRO: Final = "این بخش برای مرور نهایی و شب امتحان است؛ جزئیات در متن اصلی آمده است."
QUICK_REVIEW_POINTS: Final = "نکته‌های پرمحور"
TERMS_TITLE: Final = "واژه‌های مهم"
TERMS_HEADERS: Final = ("اصطلاح", "معادل انگلیسی", "توضیح کوتاه")
FINAL_REVIEW_TITLE: Final = "مرور نهایی"
FINAL_REVIEW_INTRO: Final = "چه چیزی را باید به خاطر بسپارید:"
DEFINITION_LABEL: Final = "تعریف"
EXAMPLE_LABEL: Final = "مثال"
STEP_LABEL: Final = "مراحل"
FORMULA_LABEL: Final = "فرمول"
SECTION_KEY_POINTS_LABEL: Final = "نکته‌های مهم این بخش"
FOOTER_NOTE: Final = "این فایل یک جزوهٔ مطالعهٔ آفلاین است و بدون اینترنت باز می‌شود."
FOOTER_SOURCE: Final = "تهیه‌شده در گاماس"

#: Callout kinds of :data:`gamas_bot.notes.VALID_CALLOUT_KINDS` → CSS class.
_CALLOUT_CLASS: Final = {"نکته": "note", "هشدار": "warn", "یادآوری": "remind"}

_WHITESPACE = re.compile(r"\s+")
_UNSAFE_SECTION_ID = re.compile(r"[^a-z0-9-]+")


def _text(value: str) -> str:
    """Escaped, whitespace-normalised text safe for any HTML text position."""
    return html.escape(" ".join(str(value or "").split()), quote=True)


def _multiline(value: str) -> str:
    """Escaped text with paragraph breaks preserved as separate paragraphs."""
    parts = [part.strip() for part in re.split(r"\n\s*\n", str(value or "")) if part.strip()]
    if not parts:
        return ""
    return "\n".join(f"<p>{_text(part)}</p>" for part in parts)


@lru_cache(maxsize=4)
def _font_face(filename: str, weight: int) -> str:
    """An ``@font-face`` block embedding a bundled face, or ``""`` if absent.

    Embedding the face is what makes the booklet typographically correct on a
    device that does not have Vazirmatn installed, without any download. A host
    that ships without the font files still gets a valid document; the system
    font stack below takes over.
    """
    path = find_font(filename)
    if path is None:
        return ""
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return (
        "@font-face{font-family:'Vazirmatn';font-style:normal;"
        f"font-weight:{weight};font-display:swap;"
        f"src:url(data:font/ttf;base64,{data}) format('truetype');}}"
    )


def _stylesheet() -> str:
    faces = _font_face(REGULAR_FILE, 400) + _font_face(BOLD_FILE, 700)
    return _CSS.replace("/*FACES*/", faces)


_CSS: Final = """/*FACES*/
:root{
  --ink:#0b1f33;--ink-2:#14304a;--ink-3:#274a63;
  --paper:#f4f8fb;--card:#ffffff;--line:#dce8f1;--line-2:#eaf2f8;
  --teal:#0f8f96;--teal-2:#12a7a0;--cyan:#27c1c9;--teal-soft:#e8f6f6;
  --amber:#a86412;--amber-soft:#fdf3e3;--muted:#5d7387;
  --radius:16px;--shadow:0 10px 30px rgba(11,31,51,.07);
}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;padding:0 0 40px;background:var(--paper);color:var(--ink);
  font-family:"Vazirmatn","Tahoma","Segoe UI",Arial,sans-serif;
  font-size:17px;line-height:1.95;letter-spacing:0;
}
a{color:var(--teal);text-decoration:none}
a:hover{text-decoration:underline}
.page{max-width:900px;margin:0 auto;padding:0 18px}

/* ---------------------------------------------------------------- cover */
.hero{
  background:
    radial-gradient(1100px 340px at 100% 0,rgba(39,193,201,.20),transparent 62%),
    linear-gradient(160deg,#0b1f33 0%,#102c44 58%,#123a4b 100%);
  color:#f6fbfd;padding:40px 0 64px;
}
.hero .page{position:relative}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:30px}
.brand-mark{
  font-size:14px;font-weight:700;letter-spacing:.32em;color:#bfeef1;
  border:1px solid rgba(191,238,241,.45);border-radius:999px;padding:6px 14px;
}
.brand-fa{font-size:15px;color:#8fb3c8}
.kicker{margin:0 0 10px;font-size:15px;color:#8fd6da;letter-spacing:.02em}
.hero h1{
  margin:0 0 14px;font-size:clamp(28px,6vw,44px);line-height:1.38;font-weight:700;
  color:#ffffff;
}
.subtitle{margin:0;max-width:62ch;font-size:17px;color:#c9dde8;line-height:1.9}
.meta{display:flex;flex-wrap:wrap;gap:10px;margin-top:26px}
.chip{
  font-size:14px;color:#d8edf2;background:rgba(255,255,255,.08);
  border:1px solid rgba(216,237,242,.22);border-radius:999px;padding:7px 14px;
}
.chip-accent{background:rgba(39,193,201,.16);border-color:rgba(39,193,201,.45);color:#d6f7f8}

/* ---------------------------------------------------------------- toc */
.toc{
  position:relative;margin:-38px 0 30px;background:var(--card);border:1px solid var(--line);
  border-radius:var(--radius);box-shadow:var(--shadow);padding:22px 22px 18px;
}
.toc h2{margin:0 0 14px;font-size:19px;color:var(--ink)}
.toc ol{
  margin:0;padding:0;list-style:none;display:grid;gap:8px;
  grid-template-columns:1fr;
}
.toc li{border-bottom:1px dashed var(--line);padding-bottom:8px}
.toc li:last-child{border-bottom:0;padding-bottom:0}
.toc a{
  display:flex;gap:10px;align-items:baseline;color:var(--ink-2);font-size:16px;
  padding:4px 0;
}
.toc a:hover{color:var(--teal)}
.toc .num{
  flex:0 0 auto;min-width:24px;height:24px;border-radius:8px;background:var(--teal-soft);
  color:var(--teal);font-size:13px;font-weight:700;display:inline-flex;
  align-items:center;justify-content:center;
}

/* ------------------------------------------------------------- blocks */
main{display:block}
.card{
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:22px;margin:0 0 22px;
}
.card > h2{
  margin:0 0 14px;font-size:22px;line-height:1.5;color:var(--ink);
  padding-bottom:10px;border-bottom:2px solid var(--teal-soft);
}
.card > h2 .badge{
  display:inline-block;background:var(--teal);color:#fff;border-radius:9px;
  font-size:14px;font-weight:700;padding:1px 10px;margin-inline-end:10px;
  vertical-align:middle;
}
.lead{color:var(--muted);font-size:15px;margin:0 0 16px}
p{margin:0 0 14px}
p:last-child{margin-bottom:0}
ul,ol{margin:0 0 16px;padding-inline-start:22px}
li{margin:0 0 8px}
li:last-child{margin-bottom:0}
strong{font-weight:700;color:var(--ink)}
.defterm{font-weight:700;color:var(--ink-2)}
.en{direction:ltr;unicode-bidi:isolate;font-family:"Segoe UI",Arial,sans-serif;font-size:.94em;color:var(--muted)}

/* definitions */
.defs{display:grid;gap:12px;margin:0 0 18px}
.def{
  background:var(--teal-soft);border:1px solid #cfe9ea;
  border-inline-start:4px solid var(--teal);border-radius:12px;padding:14px 16px;
}
.def .defterm{display:block;margin-bottom:4px}
.def p{margin:0;font-size:16px}

/* sub-blocks */
.block{margin:0 0 20px}
.block:last-child{margin-bottom:0}
.block-title{
  font-size:15px;font-weight:700;color:var(--teal);margin:0 0 10px;
  display:flex;align-items:center;gap:8px;
}
.block-title::before{
  content:"";width:6px;height:6px;border-radius:2px;background:var(--teal);
}
.steps{counter-reset:step;list-style:none;padding:0}
.steps li{
  counter-increment:step;position:relative;padding-inline-start:38px;margin-bottom:10px;
}
.steps li::before{
  content:counter(step);position:absolute;inset-inline-start:0;top:2px;
  width:26px;height:26px;border-radius:50%;background:var(--ink);
  color:#fff;font-size:13px;font-weight:700;display:flex;
  align-items:center;justify-content:center;
}
.example{
  background:#f7fafc;border:1px dashed #c9d9e5;border-radius:12px;
  padding:12px 16px;
}
.example .block-title{color:var(--ink-3)}
.formula{
  background:#0f2637;color:#e6f6f7;border-radius:12px;padding:14px 16px;
  font-family:"Segoe UI",Arial,monospace;font-size:16px;direction:ltr;
  unicode-bidi:isolate;text-align:left;overflow-x:auto;
}
.keylist{list-style:none;padding:0;margin:0}
.keylist li{
  position:relative;padding-inline-start:26px;margin-bottom:10px;font-size:16px;
}
.keylist li::before{
  content:"";position:absolute;inset-inline-start:4px;top:.72em;
  width:8px;height:8px;border-radius:2px;background:var(--teal-2);
}

/* callouts */
.callout{border-radius:12px;padding:14px 16px;margin:0 0 16px;border:1px solid}
.callout .block-title{margin-bottom:6px}
.callout.note{background:var(--teal-soft);border-color:#cfe9ea}
.callout.warn{background:var(--amber-soft);border-color:#f0dcc0}
.callout.warn .block-title{color:var(--amber)}
.callout.remind{background:#eef3f8;border-color:#d7e3ec}
.callout.remind .block-title{color:var(--ink-3)}

/* tables */
.table-wrap{overflow-x:auto;min-width:0;margin:0 0 18px;border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;min-width:420px;font-size:15px;background:#fff}
caption{
  caption-side:top;text-align:start;font-size:14px;color:var(--muted);
  padding:10px 14px 0;
}
th,td{padding:11px 12px;text-align:start;vertical-align:top;border-bottom:1px solid var(--line-2)}
thead th{background:#eef6f9;color:var(--ink-2);font-weight:700;white-space:nowrap}
tbody tr:nth-child(even) td{background:#fafcfd}
tbody tr:last-child td{border-bottom:0}

/* summary + quick review */
.summary{background:#0f2637;color:#e9f5f9;border:0}
.summary > h2{color:#ffffff;border-bottom-color:rgba(39,193,201,.4)}
.summary p{color:#d9e9f1}
.summary strong{color:#ffffff}
.grid2{display:grid;grid-template-columns:minmax(0,1fr);gap:16px}
.grid2 > *{min-width:0}
.cheat{
  background:#f7fafc;border:1px solid var(--line);border-radius:12px;padding:16px;
}
.cheat h3{margin:0 0 10px;font-size:16px;color:var(--ink-2)}
.muted{color:var(--muted);font-size:14px}

/* footer */
footer{
  background:linear-gradient(160deg,#0b1f33,#103044);color:#cfe2ea;
  margin-top:34px;padding:26px 0;
}
footer .page{display:flex;flex-direction:column;gap:6px}
footer .fbrand{color:#ffffff;font-weight:700;letter-spacing:.18em;font-size:14px}
footer .fnote{font-size:14px;color:#9fbccd}
footer .fsource{font-size:13px;color:#7f9fb2}

/* print */
@media print{
  body{background:#fff;font-size:12pt}
  .hero{background:#fff;color:#000;padding:0 0 12pt;border-bottom:2px solid #000}
  .hero h1,.subtitle,.kicker,.brand-mark,.brand-fa,.chip{color:#000;border-color:#000;background:none}
  .toc{box-shadow:none;border:0;margin:0 0 12pt}
  .card{box-shadow:none;border:1px solid #ccc;break-inside:avoid}
  .summary{background:#fff;color:#000;border:1px solid #ccc}
  .summary p,.summary > h2{color:#000}
  .table-wrap{overflow:visible}
  table{min-width:0}
  footer{background:#fff;color:#000}
  footer .fbrand,footer .fnote,footer .fsource{color:#000}
}

/* mobile-first refinements */
@media (max-width:560px){
  body{font-size:16px;line-height:1.9}
  .page{padding:0 14px}
  .hero{padding:28px 0 52px}
  .hero h1{font-size:27px}
  .subtitle{font-size:15.5px}
  .toc{padding:16px}
  .card{padding:17px;border-radius:14px}
  .card > h2{font-size:19px}
  .toc a{font-size:15px}
  table{font-size:14px;min-width:360px}
  th,td{padding:9px 10px}
}
@media (min-width:760px){
  .toc ol{grid-template-columns:1fr 1fr;column-gap:26px}
  .grid2{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
}
"""


def _section_number(index: int) -> str:
    return to_persian_digits(index)


def _definitions(section: NoteSection) -> str:
    if not section.definitions:
        return ""
    cards = []
    for definition in section.definitions:
        term = _text(definition.term)
        if definition.term_en:
            term += f' <span class="en">({_text(definition.term_en)})</span>'
        cards.append(
            '<div class="def">'
            f'<span class="defterm">{term}</span>'
            f"<p>{_text(definition.definition)}</p></div>"
        )
    return (
        f'<div class="block"><p class="block-title">{DEFINITION_LABEL}</p>'
        f'<div class="defs">{"".join(cards)}</div></div>'
    )


def _table(table: NoteTable, *, css_class: str = "") -> str:
    if not table.headers:
        return ""
    head = "".join(f"<th>{_text(cell)}</th>" for cell in table.headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_text(cell)}</td>" for cell in row) + "</tr>"
        for row in table.rows
    )
    caption = f"<caption>{_text(table.caption)}</caption>" if table.caption else ""
    extra = f" {css_class}" if css_class else ""
    return (
        f'<div class="table-wrap{extra}"><table>{caption}'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _section_body(section: NoteSection) -> str:
    blocks: list[str] = []
    for paragraph in section.paragraphs:
        body = _multiline(paragraph)
        if body:
            blocks.append(f'<div class="block">{body}</div>')
    if section.bullets:
        items = "".join(f"<li>{_text(item)}</li>" for item in section.bullets)
        blocks.append(f'<div class="block"><ul>{items}</ul></div>')
    definitions = _definitions(section)
    if definitions:
        blocks.append(definitions)
    if section.steps:
        items = "".join(f"<li>{_text(item)}</li>" for item in section.steps)
        blocks.append(
            f'<div class="block"><p class="block-title">{STEP_LABEL}</p>'
            f'<ol class="steps">{items}</ol></div>'
        )
    if section.formulas:
        items = "".join(
            f'<div class="formula">{_text(formula)}</div>' for formula in section.formulas
        )
        blocks.append(
            f'<div class="block"><p class="block-title">{FORMULA_LABEL}</p>'
            f"{items}</div>"
        )
    for table in section.all_tables:
        rendered = _table(table)
        if rendered:
            blocks.append(f'<div class="block">{rendered}</div>')
    if section.examples:
        items = "".join(f"<li>{_text(example)}</li>" for example in section.examples)
        blocks.append(
            f'<div class="block example"><p class="block-title">{EXAMPLE_LABEL}</p>'
            f"<ul>{items}</ul></div>"
        )
    for callout in section.callouts:
        css = _CALLOUT_CLASS.get(callout.kind, "note")
        blocks.append(
            f'<div class="callout {css}"><p class="block-title">{_text(callout.kind)}</p>'
            f"<p>{_text(callout.text)}</p></div>"
        )
    if section.key_points:
        items = "".join(f"<li>{_text(point)}</li>" for point in section.key_points)
        blocks.append(
            f'<div class="block"><p class="block-title">{SECTION_KEY_POINTS_LABEL}</p>'
            f'<ul class="keylist">{items}</ul></div>'
        )
    return "".join(blocks)


def _section_html(index: int, section: NoteSection) -> str:
    heading = _text(section.heading)
    return (
        f'<article class="card" id="s{index}">'
        f'<h2><span class="badge">{_section_number(index)}</span>{heading}</h2>'
        f"{_section_body(section)}</article>"
    )


def _objectives(notes: StructuredNotes) -> str:
    if not notes.learning_objectives:
        return ""
    items = "".join(f"<li>{_text(item)}</li>" for item in notes.learning_objectives)
    return (
        '<section class="card" id="objectives">'
        f"<h2>{OBJECTIVES_TITLE}</h2>"
        f'<p class="lead">{OBJECTIVES_INTRO}</p>'
        f'<ul class="keylist">{items}</ul></section>'
    )


def _key_points(notes: StructuredNotes) -> str:
    if not notes.key_points:
        return ""
    items = "".join(f"<li>{_text(point)}</li>" for point in notes.key_points)
    return (
        '<section class="card" id="key-points">'
        f"<h2>{KEY_POINTS_TITLE}</h2>"
        f'<ul class="keylist">{items}</ul></section>'
    )


def _summary(notes: StructuredNotes) -> str:
    body = _multiline(notes.summary)
    if not body:
        return ""
    return (
        '<section class="card summary" id="summary">'
        f"<h2>{SUMMARY_TITLE}</h2>{body}</section>"
    )


def _quick_review(notes: StructuredNotes) -> str:
    """Compact revision view built only from the document's own high-yield items."""
    if not notes.key_points and not notes.glossary:
        return ""
    columns: list[str] = []
    if notes.key_points:
        items = "".join(f"<li>{_text(point)}</li>" for point in notes.key_points)
        columns.append(
            f'<div class="cheat"><h3>{QUICK_REVIEW_POINTS}</h3>'
            f'<ul class="keylist">{items}</ul></div>'
        )
    if notes.glossary:
        rows = "".join(
            f"<tr><td>{_text(entry.term)}</td><td>{_text(entry.definition)}</td></tr>"
            for entry in notes.glossary
        )
        columns.append(
            '<div class="cheat"><h3>'
            + TERMS_TITLE
            + '</h3><div class="table-wrap"><table><thead><tr>'
            + f"<th>{TERMS_HEADERS[0]}</th><th>{TERMS_HEADERS[2]}</th>"
            + f"</tr></thead><tbody>{rows}</tbody></table></div></div>"
        )
    return (
        '<section class="card" id="quick-review">'
        f"<h2>{QUICK_REVIEW_TITLE}</h2>"
        f'<p class="lead">{QUICK_REVIEW_INTRO}</p>'
        f'<div class="grid2">{"".join(columns)}</div></section>'
    )


def _terms(notes: StructuredNotes) -> str:
    if not notes.glossary:
        return ""
    rows = "".join(
        "<tr>"
        f"<td><span class=\"defterm\">{_text(entry.term)}</span></td>"
        f"<td>{_text(entry.term_en)}</td>"
        f"<td>{_text(entry.definition)}</td>"
        "</tr>"
        for entry in notes.glossary
    )
    head = "".join(f"<th>{_text(label)}</th>" for label in TERMS_HEADERS)
    return (
        '<section class="card" id="terms">'
        f"<h2>{TERMS_TITLE}</h2>"
        '<div class="table-wrap"><table>'
        f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div></section>"
    )


def _final_review(notes: StructuredNotes) -> str:
    """A short "what to remember" list; never a quiz and never a new claim."""
    recall: tuple[str, ...] = notes.learning_objectives or notes.key_points[:4]
    if not recall:
        return ""
    items = "".join(f"<li>{_text(item)}</li>" for item in recall[:8])
    return (
        '<section class="card" id="final-review">'
        f"<h2>{FINAL_REVIEW_TITLE}</h2>"
        f'<p class="lead">{FINAL_REVIEW_INTRO}</p>'
        f'<ul class="keylist">{items}</ul></section>'
    )


def _toc(notes: StructuredNotes) -> str:
    entries: list[tuple[str, str]] = []
    if notes.learning_objectives:
        entries.append(("#objectives", OBJECTIVES_TITLE))
    for index, section in enumerate(notes.sections, start=1):
        heading = " ".join(section.heading.split()) or f"بخش {index}"
        entries.append((f"#s{index}", heading))
    if notes.key_points:
        entries.append(("#key-points", KEY_POINTS_TITLE))
    if notes.summary:
        entries.append(("#summary", SUMMARY_TITLE))
    if notes.key_points or notes.glossary:
        entries.append(("#quick-review", QUICK_REVIEW_TITLE))
    if notes.glossary:
        entries.append(("#terms", TERMS_TITLE))
    if notes.learning_objectives or notes.key_points:
        entries.append(("#final-review", FINAL_REVIEW_TITLE))
    if not entries:
        return ""
    items = "".join(
        f'<li><a href="{href}"><span class="num">{to_persian_digits(position)}</span>'
        f"<span>{_text(label)}</span></a></li>"
        for position, (href, label) in enumerate(entries, start=1)
    )
    return f'<nav class="toc" aria-label="{TOC_TITLE}"><h2>{TOC_TITLE}</h2><ol>{items}</ol></nav>'


def render_study_notes_html(notes: StructuredNotes) -> str:
    """One complete, self-contained, offline HTML document for the booklet."""
    if not notes.has_content:
        raise StudyNotesRenderError("the booklet has no content to render")
    title = " ".join(notes.display_title.split())
    if not title:
        raise StudyNotesRenderError("the booklet has no title")
    subtitle = " ".join(str(notes.summary or "").split())
    if len(subtitle) > 220:
        subtitle = subtitle[:217].rstrip() + "…"
    chips = [
        f"{to_persian_digits(len(notes.sections))} بخش",
        f"{to_persian_digits(len(notes.key_points))} نکتهٔ کلیدی",
        f"{to_persian_digits(len(notes.glossary))} واژه",
    ]
    meta = "".join(
        f'<span class="chip{" chip-accent" if position == 0 else ""}">{_text(chip)}</span>'
        for position, chip in enumerate(chips)
        if position != 1 or notes.key_points
        if position != 2 or notes.glossary
    )
    sections = "".join(
        _section_html(index, section)
        for index, section in enumerate(notes.sections, start=1)
    )
    toc = _toc(notes)
    body = "".join(
        (
            _objectives(notes),
            sections,
            _key_points(notes),
            _summary(notes),
            _quick_review(notes),
            _terms(notes),
            _final_review(notes),
        )
    )
    subtitle_html = f'<p class="subtitle">{_text(subtitle)}</p>' if subtitle else ""
    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; base-uri 'none'; form-action 'none'">
<meta name="generator" content="Gamas">
<meta name="referrer" content="no-referrer">
<title>{_text(title)} — {DOC_TITLE_SUFFIX}</title>
<style>
{_stylesheet()}
</style>
</head>
<body>
<a id="top"></a>
<header class="hero">
  <div class="page">
    <div class="brand">
      <span class="brand-mark">{BRAND_LATIN}</span>
      <span class="brand-fa">{BRAND_PERSIAN}</span>
    </div>
    <p class="kicker">{KICKER}</p>
    <h1>{_text(title)}</h1>
    {subtitle_html}
    <div class="meta">
      {meta}
      <span class="chip">قابل مطالعه بدون اینترنت</span>
    </div>
  </div>
</header>
<div class="page">
{toc}
<main>
{body}
</main>
</div>
<footer>
  <div class="page">
    <span class="fbrand">{BRAND_LATIN}</span>
    <span class="fnote">{FOOTER_NOTE}</span>
    <span class="fsource">{FOOTER_SOURCE}</span>
  </div>
</footer>
</body>
</html>
"""


def study_notes_filename(title: str) -> str:
    """A clean Persian/English ``.html`` filename for the delivered booklet.

    Deliberately free of the conversation reference, internal ids, timestamps
    and speaker labels: the user keeps this file on their device.
    """
    topic = sanitize_filename_part(title)
    part = re.sub(r"\s+", "_", topic).strip("_") or "study"
    return f"Gamas_{part}_notes.html"
