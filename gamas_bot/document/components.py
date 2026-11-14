"""Semantic document components: the vocabulary both renderers share.

A block is never "a light blue box": it is a *warning*, a *high-yield note*, a
*teacher note*, a *definition*. Each component names its fill, frame tone,
label and accent once, and the Word and PDF renderers both read that spec, so
the same content reads the same way in both deliverables.

Nothing here knows about python-docx or ReportLab.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..progress import to_persian_digits
from .theme import THEME


@dataclass(frozen=True, slots=True)
class BlockStyle:
    """One callout/card family."""

    fill: str
    border: str
    label: str
    accent: str
    text: str
    marker: str = ""


@dataclass(frozen=True, slots=True)
class TableStyle:
    """Content-table dress: navy header, soft grid, cool zebra rows."""

    header_fill: str
    header_text: str
    grid: str
    zebra: str
    cell_text: str
    caption: str


@dataclass(frozen=True, slots=True)
class HeadingStyle:
    """One heading level: colour, rule accent and banner treatment."""

    color: str
    accent: str
    banner: bool = False


_P = THEME.palette

#: Heading hierarchy: a navy chapter banner, teal subtopic, quiet navy detail.
HEADINGS: dict[int, HeadingStyle] = {
    1: HeadingStyle(color=_P.navy_900, accent=_P.cyan_500, banner=True),
    2: HeadingStyle(color=_P.teal_700, accent=_P.teal_400),
    3: HeadingStyle(color=_P.navy_700, accent=_P.line),
}

#: The three model-authored callout kinds. Red stays reserved for «هشدار».
CALLOUTS: dict[str, BlockStyle] = {
    "نکته": BlockStyle(_P.note_fill, _P.note_border, "نکته", _P.teal_700, _P.ink),
    "هشدار": BlockStyle(_P.warn_fill, _P.warn_border, "هشدار", _P.danger, _P.ink),
    "یادآوری": BlockStyle(_P.tip_fill, _P.tip_border, "یادآوری", _P.warning, _P.ink),
}

#: Practical tips are the lecturer's own advice (``PracticalTip``), which is
#: why they may be labelled as exam-oriented: the label is source-attributed.
#: «آزمونی» is the high-yield navy/cyan card — used sparingly, never for prose.
TIPS: dict[str, BlockStyle] = {
    "آزمونی": BlockStyle(_P.navy_900, _P.teal_500, "نکتهٔ آزمونی", _P.cyan_500, _P.on_dark),
    "کاربردی": BlockStyle(_P.note_fill, _P.note_border, "کاربرد", _P.teal_700, _P.ink),
    "هشدار": BlockStyle(_P.warn_fill, _P.warn_border, "هشدار", _P.danger, _P.ink),
}
TIP_DEFAULT = BlockStyle(_P.tip_fill, _P.tip_border, "توصیهٔ مدرس", _P.warning, _P.ink)

DEFINITION = BlockStyle(_P.definition_fill, _P.definition_border, "تعریف", _P.navy_700, _P.ink)
EXAMPLE = BlockStyle(_P.example_fill, _P.example_border, "مثال", _P.success, _P.ink)
FORMULA = BlockStyle(_P.formula_fill, _P.formula_border, "فرمول", _P.teal_700, _P.ink)
KEY_POINTS = BlockStyle(_P.key_fill, _P.key_border, "نکته‌های کلیدی", _P.teal_700, _P.ink)
SUMMARY = BlockStyle(_P.summary_fill, _P.summary_border, "خلاصه", _P.navy_800, _P.ink)
QUOTE = BlockStyle(_P.quote_fill, _P.quote_border, "", _P.navy_800, _P.ink)
HIGH_YIELD = BlockStyle(_P.navy_900, _P.teal_500, "نکات پرتکرار", _P.cyan_500, _P.on_dark)
TEACHER = BlockStyle(_P.tip_fill, _P.tip_border, "توصیهٔ مدرس", _P.warning, _P.ink)
GUIDE = BlockStyle(_P.cream_100, _P.line, "راهنمای استفاده", _P.teal_700, _P.ink)
GLANCE = BlockStyle(_P.cream_50, _P.line, "در یک نگاه", _P.navy_800, _P.ink)
SOURCE = BlockStyle(_P.cream_50, _P.line, "منبع", _P.muted, _P.muted)
EDITORIAL = BlockStyle(_P.cream_100, _P.line, "یادداشت ویرایشی", _P.muted, _P.muted)

TABLES = TableStyle(
    header_fill=_P.navy_900,
    header_text=_P.on_accent,
    grid=_P.line,
    zebra=_P.zebra,
    cell_text=_P.body,
    caption=_P.muted,
)


def callout_style(kind: str) -> BlockStyle:
    """The style for a model-authored callout kind (informational default)."""
    return CALLOUTS.get(kind) or CALLOUTS["نکته"]


def tip_style(kind: str) -> BlockStyle:
    """The style for a lecturer-attributed practical tip."""
    return TIPS.get(kind) or TIP_DEFAULT


def chapter_number(index: int) -> str:
    """Zero-padded Persian chapter number: ``۳`` → ``۰۳``."""
    number = max(1, int(index))
    return to_persian_digits(f"{number:02d}")


#: The booklet's own section labels. Editorial furniture, never scientific
#: content: these strings are fixed and are not produced by the model.
LABELS: dict[str, str] = {
    "guide": "راهنمای استفاده",
    "glance": "در یک نگاه",
    "objectives": "اهداف یادگیری",
    "summary": "خلاصه",
    "key_points": "نکته‌های کلیدی",
    "tips": "نکته‌های کاربردی",
    "review": "پرسش‌های مرور",
    "speakers": "گویندگان و زمان‌بندی",
    "glossary": "واژه‌نامهٔ اصطلاح‌ها",
    "toc": "فهرست مطالب",
    "content_map": "نقشهٔ محتوا",
    "about": "دربارهٔ این جزوه",
    "chapter": "فصل",
}

__all__ = [
    "CALLOUTS",
    "DEFINITION",
    "EDITORIAL",
    "EXAMPLE",
    "FORMULA",
    "GLANCE",
    "GUIDE",
    "HEADINGS",
    "HIGH_YIELD",
    "KEY_POINTS",
    "LABELS",
    "QUOTE",
    "SOURCE",
    "SUMMARY",
    "TABLES",
    "TEACHER",
    "TIPS",
    "BlockStyle",
    "HeadingStyle",
    "TableStyle",
    "callout_style",
    "chapter_number",
    "tip_style",
]
