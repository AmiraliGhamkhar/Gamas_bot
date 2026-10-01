"""Deterministic Persian text normalization for rendering and comparison.

Two Persian keyboards and two STT engines do not agree on characters that
*look* identical to a reader but are different code points to a renderer:

* Arabic KAF ``ك`` U+0643 vs Persian KEHEH ``ک`` U+06A9
* Arabic YEH ``ي`` U+064A and ALEF MAKSURA ``ى`` U+0649 vs
  Persian FARSI YEH ``ی`` U+06CC
* Arabic TEH MARBUTA ``ة`` U+0629 vs Persian HEH ``ه`` U+0647

The Persian Language Table (IANA/IRNIC, fa-IR 1.0) documents exactly this
mapping: an Arabic keyboard's ``ك``/``ي`` are *transformed* into U+06A9/U+06CC
because they are the same letters in Persian. Leaving them mixed in one
document is visible to a Persian reader and defeats text search, so they are
folded here — deterministically, before anything is rendered.

What this module deliberately does **not** touch, because damaging it would
break the very content the bot exists to preserve:

* **ZWNJ** (U+200C) is the carrier of Persian morphology (``می‌شود``, ``نمی‌کند``)
  and is kept exactly. It is never removed, and a space is never inserted in
  its place; doing either would corrupt compound words.
* **Latin/technical tokens** (``HbA1c``, ``COVID-19``, ``mg/dL``, URLs,
  e-mails, formulas) pass through byte-for-byte.
* **Digits** are not rewritten here. A dosage must keep the digits the
  lecturer said; digit-shape conversion is a presentation decision and lives
  in the renderer, not here.
* Arabic diacritics (harakat) are *not* stripped: they can be meaningful, and
  STT does not reliably produce them, so removing them is not our business.

The function is idempotent — normalising twice equals normalising once — which
is what makes it safe to call on both the transcript and the rendered notes.
"""

from __future__ import annotations

import re
import unicodedata

#: Arabic code points that have a distinct Persian counterpart. Taken from the
#: IANA/IRNIC Persian Language Table (fa-IR 1.0) plus the standard Persian
#: orthographic equivalences. Only *letter* substitutions appear here; digits
#: and punctuation are intentionally excluded.
_PERSIAN_LETTERS = {
    "ك": "ک",  # ARABIC KAF -> KEHEH
    "ڪ": "ک",  # SWASH KAF -> KEHEH
    "ي": "ی",  # ARABIC YEH -> FARSI YEH
    "ى": "ی",  # ALEF MAKSURA -> FARSI YEH
    "ﻱ": "ی",  # ARABIC YEH isolated form
    "ﻲ": "ی",  # ARABIC YEH final form
    "ﻳ": "ی",  # ARABIC YEH initial form
    "ﻴ": "ی",  # ARABIC YEH medial form
    "ﮎ": "ک",  # KEHEH swash
    "ة": "ه",  # TEH MARBUTA -> HEH
}

#: Arabic-Indic digits (٠-٩) and the extended forms (۰-۹) are *not* folded
#: here; see the module docstring.
_PERSIAN_DIGIT_TRANSLATION = {
    0x0660: "0", 0x0661: "1", 0x0662: "2", 0x0663: "3", 0x0664: "4",
    0x0665: "5", 0x0666: "6", 0x0667: "7", 0x0668: "8", 0x0669: "9",
    0x06F0: "0", 0x06F1: "1", 0x06F2: "2", 0x06F3: "3", 0x06F4: "4",
    0x06F5: "5", 0x06F6: "6", 0x06F7: "7", 0x06F8: "8", 0x06F9: "9",
}

#: Ranges that only need letter substitution; a cheap pre-filter so a string
#: with no Arabic-script characters at all is returned unchanged and fast.
_ARABIC_SCRIPT = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

#: Runs of *horizontal* whitespace collapse to one ordinary space. Newline,
#: carriage return and tab are deliberately excluded: they are structure the
#: caller owns (paragraph and line boundaries), and a renderer must not
#: silently reflow a transcript into a single run. ZWNJ is not whitespace and
#: can never match.
_WHITESPACE_RUN = re.compile(r"[    -   　]+")


def contains_arabic_script(text: str) -> bool:
    """True when ``text`` has at least one Arabic-script character."""
    return bool(_ARABIC_SCRIPT.search(text))


def normalize_digits(text: str) -> str:
    """Convert Arabic-Indic/Extended-Arabic digits to ASCII, and nothing else.

    ``translate`` is used instead of per-character ``int()`` handling so the
    mapping is a single, auditable table. Latin digits are left alone, which
    is what keeps ``mg/dL`` and ``120/80`` intact.
    """
    if not text:
        return text
    return text.translate(_PERSIAN_DIGIT_TRANSLATION)


def normalize_persian(text: str, *, digits: bool = False) -> str:
    """Return ``text`` with Persian letter equivalences applied.

    Idempotent and deterministic. ``digits=True`` additionally normalises
    Arabic-Indic digits to ASCII, which the QA comparison layer wants but the
    renderer does not need.

    Only Persian *letters* are substituted, so English terms, identifiers,
    URLs, formulas and numeric values are preserved byte-for-byte. ZWNJ is
    never removed or replaced by a space.
    """
    if not text or not contains_arabic_script(text):
        return text
    # A string of Arabic-script letters can only contain characters that are
    # safe to map; a single ``str.translate`` keeps this to one pass.
    result = text.translate(
        {ord(key): value for key, value in _PERSIAN_LETTERS.items()}
    )
    if digits:
        result = normalize_digits(result)
    return result


def normalize_whitespace(text: str) -> str:
    """Collapse horizontal whitespace runs to a single space.

    Newlines, tabs and carriage returns are preserved exactly: they are the
    caller's paragraph structure, and collapsing them would join two lines
    into one run in the rendered document. ZWNJ, RTL/LTR marks and all other
    non-whitespace content also survive untouched.

    This is deliberately not ``" ".join(text.split())``, which would delete
    ZWNJ (the carrier of Persian compound-word morphology) and reflow newlines.
    """
    if not text:
        return text
    return _WHITESPACE_RUN.sub(" ", text)


def normalize_display(text: str) -> str:
    """The normal form used for rendering: letters + horizontal whitespace.

    This is the single entry point the DOCX renderer and the Telegram text
    renderer should use. It is idempotent, so applying it on an already
    normalised string is a no-op and cannot be applied twice by accident. Line
    breaks and tabs are preserved; only Persian letter variants and repeated
    horizontal spaces change.
    """
    if not text:
        return text
    return normalize_whitespace(normalize_persian(text, digits=False))


def normalize_for_compare(text: str) -> str:
    """Aggressive, lossy normal form used only for equality/search comparison.

    Additionally folds digits, removes ZWNJ and collapses case, so that
    ``۵۰۰ میلی گرم`` and ``500 mg`` can be compared for *equality* without
    either losing information. Never use this to produce user-visible text —
    it would strip the ZWNJ that carries Persian morphology.
    """
    if not text:
        return text
    folded = normalize_whitespace(normalize_persian(text, digits=True))
    folded = folded.replace("‌", "").replace("‍", "").strip()
    return unicodedata.normalize("NFKC", folded).casefold()
