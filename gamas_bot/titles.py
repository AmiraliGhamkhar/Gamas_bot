"""Short, human-readable titles for the library and the documents behind it.

The library is a *list of names*, not a list of files: a model-written lesson
title can be a full sentence («جزوهٔ جامع فیزیولوژی قلب و عروق برای آزمون پایان
ترم»), and a raw Telegram caption carries a reference code («📖 جزوهٔ Word —
GMS-000123»). Both are too long for a button (``button_label`` caps at 64 UTF-16
units) and unreadable in a list, so every artifact that reaches the library is
named with this module instead.

Rules that make the result predictable:

* **2 to 4 words** — the product contract for a library title. A shorter name
  is widened with the kind word («جزوه قلب»), a longer one is cut on a word
  boundary, and a dangling connector («… قلب و») is dropped rather than kept.
* **The user's words survive** — nothing is translated or re-worded; only
  decoration (emoji, quotes, Markdown, brackets), internal reference codes
  (``GMS-000123``, ``OFF-1A2B``) and surrounding whitespace are removed.
  ZWNJ is preserved (P1): it carries Persian morphology inside a word.
* **Deterministic** (R14) — the same input always yields the same title, so a
  booklet's cover, filename, caption and library row cannot disagree.

The same title is used for the booklet itself, which is why the function is
pure and lives outside ``bot.py``: the Telegram layer only orchestrates.
"""

from __future__ import annotations

import re
import unicodedata

from .textnorm import normalize_display

#: The product contract: a library title is two to four words.
MIN_TITLE_WORDS = 2
MAX_TITLE_WORDS = 4
#: A button label must stay comfortably under Telegram's 64-unit cap (R13).
MAX_TITLE_CHARS = 60

#: Decoration that is never part of a name: emoji/pictographs, arrows, quotes,
#: brackets, Markdown emphasis, separators. ZWNJ (U+200C) is deliberately absent.
_DECORATION = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoji, pictographs
    "\u2190-\u27BF"  # arrows, dingbats, enclosed symbols
    "\u2B00-\u2BFF"  # extra arrows/stars
    "\uFE0F\u200D"  # variation selector, zero-width joiner
    "\u2022\u00B7\u2027"  # bullets
    "\u00AB\u00BB\u201C\u201D\u2018\u2019\u2039\u203A"
    "\"'`"
    "\\[\\]{}()<>«»|/\\\\*#_~"
    "\u2013\u2014"  # en/em dash (the caption separator)
    "]+"
)

#: Upload extensions; a file type is not part of a name.
_FILE_EXTENSION = re.compile(
    r"\.(?:docx?|xlsx?|pptx?|pdf|txt|md|markdown|csv|tsv|rtf|m4a|mp3|ogg|oga|opus|wav"
    r"|mp4|mkv|mov|webm|aac|flac|jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)

#: Word separators inside one token: a filename's extension and the dash/underscore
#: joiners of an upload name. Persian joins words with ZWNJ, never with these.
_WORD_SEPARATORS = re.compile(r"[-_.]+")

#: Internal reference codes the pipeline prints in captions and filenames.
_REFERENCE_CODE = re.compile(
    r"^(?:GMS|OFF|DOC|SUB|TKT|QRZ|GRD|ADM)[-\s]?[0-9A-Z-]*$", re.IGNORECASE
)

#: Words that only say *what kind* of artifact this is. They are dropped from
#: the front while at least two real words remain, so «جزوهٔ فیزیولوژی قلب»
#: becomes «فیزیولوژی قلب» but «جزوهٔ کلاس» is left intact.
_KIND_WORDS = frozenset({"جزوه", "جزوهٔ", "فایل", "سند", "متن", "درس"})

#: A trailing connector means the title was cut mid-phrase.
_CONNECTORS = frozenset({"و", "یا", "از", "با", "به", "در", "بر", "که", "تا", "برای"})

#: Fallbacks per artifact kind; the first word also widens a one-word title.
FALLBACK_BOOKLET = "جزوهٔ درسی"
FALLBACK_TRANSCRIPT = "متن گفتار"
FALLBACK_OFFICE = "سند آزاد"
FALLBACK_DOCUMENT = "سند درسی"


def _tokens(text: str) -> list[str]:
    """Split a candidate into meaningful words, dropping codes and decoration."""
    # ``normalize_display`` folds Arabic letter variants but keeps ZWNJ/newlines
    # (P1); NFKC only removes cosmetic compatibility forms such as «ﻯ».
    cleaned = normalize_display(unicodedata.normalize("NFKC", str(text or "")))
    cleaned = _FILE_EXTENSION.sub(" ", cleaned)
    cleaned = _DECORATION.sub(" ", cleaned)
    words: list[str] = []
    parts: list[str] = []
    for raw in cleaned.split():
        if _REFERENCE_CODE.match(raw):
            continue
        # A filename is not a name: «lecture-3-cardio.m4a» is three words once
        # the extension and the separators are gone.
        parts.extend(_WORD_SEPARATORS.split(raw))
    previous_kept = -1
    for index, part in enumerate(parts):
        word = part.strip().strip(".,:؛،…")
        if not word or _REFERENCE_CODE.match(word):
            continue
        if not any(char.isalpha() for char in word):
            # A number is part of a name only when it belongs to the phrase
            # («فصل ۳», «درس 7»), never when it is a date fragment or an
            # upload timestamp («voice_2026_11_01»).
            if index != previous_kept + 1 or not word.isdigit() or len(word) > 2:
                continue
        if word not in words:
            words.append(word)
        previous_kept = index
    return words


def _trim(words: list[str], max_words: int) -> list[str]:
    """Cut to ``max_words`` without leaving a dangling connector behind."""
    trimmed = list(words[:max_words])
    while len(trimmed) > 1 and trimmed[-1] in _CONNECTORS:
        trimmed.pop()
    return trimmed


def _cap_chars(text: str) -> str:
    """Bound the rendered title on a word boundary, never mid-word."""
    if len(text) <= MAX_TITLE_CHARS:
        return text
    window = text[:MAX_TITLE_CHARS]
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).rstrip()


def short_title(
    *candidates: str | None,
    fallback: str = FALLBACK_BOOKLET,
    max_words: int = MAX_TITLE_WORDS,
) -> str:
    """A two-to-four-word title, taking the first usable candidate.

    Candidates are tried in order (the model's title, then the upload name, then
    any already-short label). The first one that yields at least one real word
    wins, so a missing model title can never rename a document into the generic
    fallback while a usable name exists.
    """
    limit = max(MIN_TITLE_WORDS, min(MAX_TITLE_WORDS, int(max_words)))
    lead = next(iter(_tokens(fallback)), "جزوه")
    for candidate in candidates:
        words = _tokens(str(candidate or ""))
        if not words:
            continue
        # «جزوهٔ فیزیولوژی قلب» -> «فیزیولوژی قلب»: the kind word is implied by
        # where the item is shown (📖 in the library, Word in the caption). It is
        # only dropped while at least two real words remain.
        while len(words) - 1 >= MIN_TITLE_WORDS and words[0] in _KIND_WORDS:
            words.pop(0)
        words = _trim(words, limit)
        if len(words) < MIN_TITLE_WORDS:
            # A single word is widened with the kind word rather than padded
            # with a filler the user never wrote: «قلب» -> «جزوه قلب».
            words = [lead, *words]
        return _cap_chars(" ".join(_trim(words, limit)))
    return _cap_chars(" ".join(_trim(list(_tokens(fallback)), limit))) or fallback


def word_count(title: str) -> int:
    """The number of named words in a title (separators such as «—» are not words)."""
    return sum(
        1 for token in str(title or "").split() if any(ch.isalpha() for ch in token)
    )
