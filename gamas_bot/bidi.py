"""Run-level bidirectional segmentation for mixed Persian/English Word output.

Word does not render mixed-direction text by "reversing strings": it keeps the
logical character order and resolves the visual order with the Unicode
BiDi Algorithm, using the base direction of each *run* (``w:rtl``) inside a
paragraph whose base direction is ``w:bidi`` (ECMA-376 §17.3.2.30 / §17.3.1.6).
Hand-written Word files therefore model a mixed sentence such as

    شبکهٔ عصبی Artificial Neural Network یکی از روش‌های یادگیری ماشین است.

as alternating RTL and LTR runs. This module splits a logical string into
exactly that run sequence. Reversed visual order is never stored in the file —
that would double-apply BiDi in the renderer and corrupt the text.

Segmenation rules (deliberately simple and predictable):

* contiguous Persian/Arabic (and other strong-RTL) characters form one RTL run;
* contiguous Latin letters/digits/technical tokens (URLs, e-mails, "120/80",
  "HbA1c", "500 mg") form one LTR run;
* neutral characters (spaces, punctuation, Persian punctuation, digits,
  parentheses) join the *preceding* run so a Persian sentence never breaks
  into per-word fragments;
* the ``w:rtl`` property alone never changes font selection: the renderer must
  still set ``w:cs`` fonts for RTL runs and ``w:ascii``/``w:hAnsi`` for LTR
  ones, which ``docx_export`` does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Strong RTL characters: Arabic, Persian, Hebrew blocks plus their presentation
#: forms and the ZWNJ carrier used by Persian orthography (می‌شود).
_RTL_CHARS = (
    "\\u0600-\\u06FF\\u0750-\\u077F\\u08A0-\\u08FF\\uFB50-\\uFDFF\\uFE70-\\uFEFF"
    "\\u0590-\\u05FF"
)

_RTL_RE = re.compile(f"[{_RTL_CHARS}]")

#: A whole LTR technical token: Latin letters/digits plus intraword separators
#: that must stay inside the token ("mg/dL", "120/80", "kg/m²", "e-mail",
#: "example.com", "HbA1c", "COVID-19").
_LTR_TOKEN_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+~&'*/-]*@?"  # includes e-mails partially
    r"|[A-Za-z0-9]+(?:[./_+-][A-Za-z0-9]+)+"
)

#: Neutral characters that glue to the previous run instead of opening a new one.
_NEUTRAL_RE = re.compile(
    r"[^" + _RTL_CHARS + r"A-Za-z0-9]"  # everything not RTL and not Latin/digit
)


@dataclass(frozen=True, slots=True)
class TextRun:
    """One directional run: the logical text plus its base direction."""

    text: str
    rtl: bool

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        direction = "RTL" if self.rtl else "LTR"
        return f"TextRun({direction}, {self.text!r})"


def _has_rtl(text: str) -> bool:
    return bool(_RTL_RE.search(text))


def split_direction_runs(text: str) -> list[TextRun]:
    """Split ``text`` into ordered direction runs (never reorders characters).

    Empty input yields an empty list. A string without any strong RTL
    character yields a single LTR run (a pure-Latin sentence must not be
    forced RTL); a string without Latin yields a single RTL run.
    """
    if not text:
        return []
    if not _has_rtl(text):
        return [TextRun(text, False)]
    if not re.search(r"[A-Za-z0-9]", text):
        return [TextRun(text, True)]

    runs: list[TextRun] = []
    buffer = ""
    current_rtl: bool | None = None

    def flush() -> None:
        nonlocal buffer
        if buffer:
            runs.append(TextRun(buffer, bool(current_rtl)))
            buffer = ""

    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if _RTL_RE.match(char):
            if current_rtl is not True:
                flush()
                current_rtl = True
            buffer += char
            index += 1
            continue
        if char.isascii() and (char.isalnum()):
            # Greedily consume the whole LTR technical token.
            match = _LTR_TOKEN_RE.match(text, index)
            token_end = match.end() if match else index + 1
            token = text[index:token_end]
            if current_rtl is not False:
                flush()
                current_rtl = False
            buffer += token
            index = token_end
            continue
        # Neutral character: attach to the current run, or when the text so
        # far starts neutrally, to the first upcoming strong run's direction.
        if current_rtl is None:
            current_rtl = _next_strong_direction(text, index)
            if current_rtl is not None:
                # No flush needed: buffer is still empty here.
                pass
        buffer += char
        index += 1
    flush()
    return _merge_adjacent(runs)


def _next_strong_direction(text: str, index: int) -> bool:
    """Direction of the first strong character at/after ``index`` (RTL default)."""
    for char in text[index:]:
        if _RTL_RE.match(char):
            return True
        if char.isascii() and char.isalnum():
            return False
    return True


def _merge_adjacent(runs: list[TextRun]) -> list[TextRun]:
    """Collapse adjacent runs of the same direction (neutrals glue runs)."""
    merged: list[TextRun] = []
    for run in runs:
        if merged and merged[-1].rtl == run.rtl:
            merged[-1] = TextRun(merged[-1].text + run.text, run.rtl)
        else:
            merged.append(run)
    return merged


def is_rtl_dominant(text: str) -> bool:
    """True when a paragraph's base direction should be right-to-left.

    The heuristic is the classic first-strong-character rule with a Persian
    default: any strong RTL character makes the paragraph RTL (lecture notes
    are Persian-dominant), and a paragraph with only LTR content (a formula,
    a URL list) stays LTR so it reads correctly inside the RTL document.
    """
    if not text:
        return False
    return bool(_RTL_RE.search(text))
