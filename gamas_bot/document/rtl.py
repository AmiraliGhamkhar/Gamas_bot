"""Persian text preparation shared by the booklet renderers.

Two different jobs, deliberately kept apart:

* **Word** stores logical Unicode order and lets the reader resolve BiDi. It
  never touches this module's visual helpers; :mod:`gamas_bot.bidi` splits its
  runs instead.
* **PDF** (ReportLab) has no BiDi engine and no contextual shaping. A line must
  be measured and wrapped in *logical* order, then shaped and reordered once,
  as a finished line. Shaping a whole paragraph before wrapping wraps on the
  reversed string and breaks phrases mid-line.

``wrap_logical`` is generic over the measuring function so the same wrapping
rules serve any canvas or font.
"""

from __future__ import annotations

from collections.abc import Callable

import arabic_reshaper
from bidi.algorithm import get_display

from ..bidi import is_rtl_dominant

Measure = Callable[[str], float]

#: Characters that may never start a wrapped line (Persian punctuation and
#: closing marks). A line break before them leaves an orphaned mark.
NO_LINE_START = "،؛؟!:»)]}٪,.%;"


def visual(text: str) -> str:
    """One logical line → display-order string with contextual glyph forms.

    The base direction is pinned rather than auto-detected. A Persian sentence
    that opens with a Latin term ("eGFR را پیش از …") is RTL text, but a bidi
    implementation that guesses from the first strong character picks LTR and
    mirrors the clause, pushing "را" to the wrong end of the line.
    """
    if not text:
        return text
    base = "R" if is_rtl_dominant(text) else "L"
    return get_display(arabic_reshaper.reshape(text), base_dir=base)


def visual_ltr(text: str) -> str:
    """Display order for a line whose base direction is left-to-right.

    Formulas and technical lines are LTR, but may contain a Persian term. This
    shapes the Persian (contextual forms) and resolves the BiDi order with an
    LTR base, so the formula reads left-to-right and the Persian inside it stays
    joined — instead of a reversed formula or disconnected letters.
    """
    if not text:
        return text
    return get_display(arabic_reshaper.reshape(text), base_dir="L")


def wrap_logical(text: str, measure: Measure, max_width: float) -> list[str]:
    """Word-wrap ``text`` in logical order.

    ``measure`` returns the rendered width of a candidate line for the font and
    size in use. A single token wider than the line is hard-broken, so a long
    URL can never escape the frame.
    """
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or measure(candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    wrapped: list[str] = []
    for line in lines:
        if measure(line) <= max_width:
            wrapped.append(line)
            continue
        # Only an over-long left-to-right token (a URL, an identifier, a
        # chemical name) is broken: overflowing the frame is worse than a break
        # there. Persian and Arabic words are never split — the break detaches
        # the final letter onto the next line, which reads as a typo (e.g.
        # "سیتاگلیپتی" then "ن").
        if not line.isascii():
            wrapped.append(line)
            continue
        piece = ""
        for char in line:
            trial = f"{piece}{char}"
            if not piece or measure(trial) <= max_width:
                piece = trial
            else:
                wrapped.append(piece)
                piece = char
        if piece:
            wrapped.append(piece)
    return wrapped


def visual_lines(lines: list[str]) -> list[str]:
    """Shape and reorder finished logical lines."""
    return [visual(line) for line in lines]


def label_kind(kind: str) -> str:
    """The printable label for a callout kind."""
    return (kind or "").strip()


__all__ = [
    "NO_LINE_START",
    "Measure",
    "is_rtl_dominant",
    "label_kind",
    "visual",
    "visual_ltr",
    "visual_lines",
    "wrap_logical",
]
