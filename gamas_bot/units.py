"""Lightweight, deterministic extraction of educational content units.

The existing QA layer (:mod:`gamas_bot.qa`) counts *atomic signals* — numbers
with units, percentages, blood-pressure pairs, English technical terms. That
catches dosage corruption, but it is blind to the most damaging loss a lecture
note can suffer: a whole explanation, worked example or procedure being deleted
while every number and term survives. "500 mg" is not a definition, and a
document can keep every "500 mg" in the file and still have thrown away why.

This module closes that gap without embeddings, a vector store or a second
model. It segments a source chunk into **content units** — the rhetorical
moves a lecturer actually makes — and gives each one a stable content
fingerprint. Coverage is then a ratio of fingerprints present in the notes.

The design is deliberately pragmatic rather than a parser:

* units are found by *cue phrases* ("تعریف می‌شود", "مثال", "گام اول"), by
  *structural markers* (numbered steps, bullet-like enumerations, formulas),
  and by a *residual* rule for everything else;
* a unit's fingerprint is a bag of its distinctive content words plus its
  numbers, so a paraphrase or a reordered translation still matches, while a
  genuinely different statement does not;
* a unit is only *expected* in the notes when it is substantive. Short
  interjections ("بله", "خوب", "ادامه می‌دهیم") are dropped by a minimum
  content-word threshold, because requiring filler to be preserved would make
  the metric meaningless.

Nothing here rewrites notes. The output is metadata for QA, repair, the
benchmark and debugging — never shown to a Telegram user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .textnorm import normalize_for_compare

# Imported at module scope but only for the numeric helpers below. ``qa`` imports
# this module, so the dependency is one-way (units -> qa); ``qa`` never imports
# ``units`` at import time, so there is no cycle.
from .qa import _extract_numbers

#: Content-unit types, ordered from the most to the least explicit cue. The
#: order matters: a sentence carrying an explicit cue is typed by that cue even
#: if it also contains a weaker marker.
UNIT_TYPES = (
    "definition",
    "example",
    "procedure",
    "warning",
    "exception",
    "comparison",
    "formula",
    "explanation",
    "fact",
    "conclusion",
)

#: Cue phrases per type, in *priority* order. Persian first (the product's
#: primary language), plus the English equivalents a bilingual lecture uses.
#: Matching is on the normalized text, so Arabic letter variants and ZWNJ
#: differences do not matter.
#:
#: Order is significant and is strongest-signal-first. "در نتیجه" must beat the
#: procedure cue "سپس", otherwise a concluding sentence is typed as a step and
#: the repair prompt asks for the wrong thing. Weak, high-frequency sequencing
#: words ("سپس", "پس", "then") therefore live in a *last-resort* group below.
_CUES_STRONG: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "definition",
        (
            "تعریف می‌شود", "تعریف می شود", "تعریف", "یعنی چه", "منظور از",
            "به عبارت دیگر", "که عبارت است از", "یعنی",
            "is defined as", "is called", "refers to", "means that",
        ),
    ),
    (
        "example",
        (
            "به عنوان مثال", "برای مثال", "برای نمونه", "مثال", "نمونه",
            "فرض کنید", "تصور کنید", "در نظر بگیرید",
            "for example", "for instance", "as an example", "say that",
            "imagine", "suppose",
        ),
    ),
    (
        "warning",
        (
            "هشدار دهید", "هشدار", "توجه کنید", "توجه", "مراقب باشید", "مراقب",
            "نکتهٔ مهم", "مهم است که", "یادآوری", "اخطار",
            "warning", "caution", "be careful", "note that",
        ),
    ),
    (
        "exception",
        (
            "استثنا", "به استثنای", "اما اگر", "در صورتی که", "مگر", "خارج از",
            "نباید", "هرگز",
            "except", "unless", "however if", "but if", "never", "do not",
        ),
    ),
    (
        "comparison",
        (
            "در مقایسه با", "در مقابل", "برخلاف", "در حالی که", "تفاوت",
            "شباهت", "بهتر از", "بدتر از",
            "in contrast", "compared to", "unlike", "whereas", "difference",
            "better than", "worse than",
        ),
    ),
    (
        "procedure",
        (
            "اولین قدم", "گام", "مرحلهٔ", "مرحله", "به ترتیب", "روش کار",
            "آلگوریتم", "ابتدا",
            "first we", "the procedure", "algorithm", "step by step",
        ),
    ),
    (
        "conclusion",
        (
            "در نتیجه", "بنابراین", "خلاصه اینکه", "جمع‌بندی", "نتیجه‌گیری",
            "در نهایت",
            "in conclusion", "to conclude", "in summary", "therefore", "thus",
        ),
    ),
    (
        "formula",
        ("فرمول", "به دست می‌آید", "محاسبه", "رابطهٔ"),
    ),
)

#: Last-resort cues. These are common enough in ordinary prose that they only
#: type a sentence when nothing stronger matched.
_CUES_WEAK: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("conclusion", ("پس", "نتیجه", "in short")),
    ("procedure", ("سپس", "then we", "then")),
    ("comparison", ("اما ", "however")),
)

#: Sentence terminators for both scripts. A dot between digits is a decimal, so
#: it must not split a sentence (same rule the chunker already uses).
_SENTENCE_END = re.compile(r"(?:[!?؟…]+|؛|(?<!\d)\.(?!\d))[»\)\]”\"']*")

#: Structural markers: a leading enumerator ("۱." / "اول") or a bullet glyph.
_ENUMERATOR = re.compile(r"^\s*(?:[0-9۰-۹]{1,2}[.)]|[·•\-*–—]|[0-9۰-۹]{1,2}\s+-)\s*")

#: A formula-ish token: digits and operators with a relation sign.
_FORMULAISH = re.compile(r"[=<>≤≥≈±]|\b\d+\s*[+*/^]\s*\d+")

#: Filler / discourse noise that carries no educational content. These are
#: dropped before a unit is considered, so the metric never demands that
#: "بله، خوب، ادامه می‌دهیم" be preserved.
_FILLER = re.compile(
    r"^(?:بله|بلی|خوب|خب|درست|صحیح|حالا|خب پس|بله خوب|ادامه|ادامه می‌دهیم|"
    r"ممنون|تشکر|سلام|شب بخیر|ببینید|ببینیم|گوش کنید|دقت کنید|"
    r"yes|ok|okay|right|so|well|now|let's|let us|alright|right)\b[\s،,.:]*",
    re.IGNORECASE,
)

#: Words carrying no discriminative content in Persian/English note text.
_STOPWORDS = frozenset(
    {
        "را", "از", "به", "با", "در", "را", "که", "این", "آن", "برای", "است",
        "هست", "های", "می", "شود", "شده", "کرد", "کند", "یک", "هم", "یا", "تا",
        "اما", "اگر", "چون", "پس", "هر", "همه", "روی", "بین", "دو", "چند",
        "the", "a", "an", "of", "to", "in", "is", "are", "and", "or", "for",
        "that", "this", "it", "as", "be", "on", "with", "we", "you", "not",
    }
)

#: A unit must have at least this many distinctive content words to be
#: *expected* in the notes. Below it the "unit" is a sentence fragment and
#: requiring it would add noise rather than signal.
MIN_CONTENT_WORDS = 4

#: A fingerprint match only counts when both sides have this many shared
#: content words, so a single shared word cannot fake coverage.
MIN_FINGERPRINT_SHARED = 2

_WORD = re.compile(r"[\w؀-ۿ]+", re.UNICODE)

#: Punctuation is not part of a word. ``normalize_for_compare`` leaves
#: punctuation in place, so "قدیمی،" and "قدیمی" would otherwise be two
#: different words and a perfectly preserved unit would look uncovered.
_TRIM = "،؛؟!.,;:()[]{}«»\"'`…—–-ـ"


def _contains(haystack: str, needle: str) -> bool:
    """Substring match that respects word boundaries.

    A plain ``in`` test produces false positives on Persian compounds: the cue
    ``گام`` (step) occurs inside ``زودهنگام`` (early), which silently retyped a
    conclusion as a procedure step. Arabic script joins with ZWNJ, so a
    ``\\b``-only pattern is not enough either; the explicit lookarounds are.
    """
    pattern = rf"(?<![؀-ۿ\w]){re.escape(needle)}(?![؀-ۿ\w])"
    return re.search(pattern, haystack) is not None


@dataclass(frozen=True, slots=True)
class ContentUnit:
    """One traceable unit of educational content from the source."""

    id: str
    type: str
    text: str
    source_chunk: int
    content_words: frozenset[str] = frozenset()
    numbers: frozenset[str] = frozenset()
    important: bool = False

    @property
    def is_expectation(self) -> bool:
        """True when this unit is substantial enough that the notes owe it."""
        return self.important or len(self.content_words) >= MIN_CONTENT_WORDS


def _content_words(text: str) -> frozenset[str]:
    """Distinctive lowercased content words, stopwords and filler removed.

    Punctuation is stripped from each token so a trailing comma cannot make a
    preserved word look absent.
    """
    normalized = normalize_for_compare(text)
    words = {word.strip(_TRIM) for word in _WORD.findall(normalized)}
    return frozenset(
        word for word in words if len(word) > 2 and word not in _STOPWORDS
    )


def _strip_filler(text: str) -> str:
    """Remove leading discourse noise; a unit is not "بله، HbA1c یعنی ..."."""
    previous = None
    current = text.strip()
    while previous != current:
        previous = current
        current = _FILLER.sub("", current, count=1).strip()
    return current


def _is_real_number(token: str, sentence: str) -> bool:
    """Reject numbers that are a by-product of a larger compound value.

    ``۱۲۰/۸۰ میلی‌متر جیوه`` legitimately yields both ``120/80`` and
    ``80 mmhg``. The second is an artefact of scanning the unit word after the
    second half of the pair: notes that preserved ``120/80 mmHg`` perfectly
    would otherwise be reported as having dropped a number. A token is real
    when its digits do not already appear inside another detected token of the
    same sentence.
    """
    if not token:
        return True
    digits = re.sub(r"[^\d]", "", token)
    if not digits:
        return True
    others = [other for other in _extract_numbers(sentence) if other != token]
    for other in others:
        other_digits = re.sub(r"[^\d]", "", other)
        # Only a *strict* substring of a longer value is an artefact; equal
        # length means a genuinely separate measurement.
        if digits != other_digits and digits in other_digits:
            return False
    return True


def _classify(sentence: str) -> str:
    """Pick the unit type from the strongest cue present in the sentence.

    Strong, unambiguous cues win first; only when none matched do the weak
    high-frequency sequencing words get a chance, and a formula-shaped
    sentence is typed as a formula before any of them.
    """
    normalized = normalize_for_compare(sentence)
    for unit_type, cues in _CUES_STRONG:
        for cue in cues:
            if _contains(normalized, cue):
                return unit_type
    if _FORMULAISH.search(sentence):
        return "formula"
    for unit_type, cues in _CUES_WEAK:
        for cue in cues:
            if _contains(normalized, cue):
                return unit_type
    return "explanation"


def split_sentences(text: str) -> list[str]:
    """Split a chunk into sentences, keeping terminators with their sentence.

    A dot between digits (``7.2``, ``1.000``) is a decimal/thousands separator
    and never ends a sentence, so numeric values are never cut in half.
    """
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        if end <= start:
            continue
        piece = text[start:end].strip()
        if piece:
            sentences.append(piece)
        start = end
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def extract_units(chunk: str, chunk_index: int = 1) -> list[ContentUnit]:
    """Segment one source chunk into traceable educational units.

    ``chunk_index`` is 1-based and becomes the ``source_chunk`` provenance, so
    a unit can always be traced back to the part of the lecture it came from.
    Units below the content threshold are returned with ``important=False`` so
    a caller (QA, debugging) can still see them, but they are never *demanded*
    of the notes.
    """
    units: list[ContentUnit] = []
    position = 0
    for sentence in split_sentences(chunk):
        cleaned = _strip_filler(sentence)
        if not cleaned:
            continue
        unit_type = _classify(cleaned)
        words = _content_words(cleaned)
        # Numbers are carried through verbatim so a unit cannot be considered
        # covered when its dosage was dropped. A number written as an *ordinal
        # position* ("80 میلی‌متر" inside "۱۲۰/۸۰ میلی‌متر جیوه") must not
        # become a separate expectation, so numbers that are fully contained
        # in a larger detected number are discarded as artefacts of the split.
        numbers = frozenset(
            token for token in _extract_numbers(cleaned) if _is_real_number(token, cleaned)
        )
        position += 1
        units.append(
            ContentUnit(
                id=f"chunk{chunk_index}-unit{position}",
                type=unit_type,
                text=cleaned,
                source_chunk=chunk_index,
                content_words=words,
                numbers=numbers,
                important=len(words) >= MIN_CONTENT_WORDS or bool(numbers),
            )
        )
    return units


def extract_all_units(chunks: list[str]) -> list[ContentUnit]:
    """Extract units from every chunk, keeping chunk order."""
    units: list[ContentUnit] = []
    for index, chunk in enumerate(chunks, start=1):
        units.extend(extract_units(chunk, index))
    return units


def unit_is_covered(
    unit: ContentUnit,
    notes_words: frozenset[str],
    notes_numbers: frozenset[str] = frozenset(),
) -> bool:
    """True when the notes preserve the substance of ``unit``.

    Coverage is judged on distinctive content words, not string equality, so a
    reworded or reordered statement still counts as preserved while a different
    statement does not.

    Numbers are checked against a *separate* set: they are extracted as
    canonical tokens (``6.5 %``) and never appear in the word set, so
    comparing them against ``notes_words`` would always fail and silently mark
    every numeric unit as missing. A unit whose numbers are absent is not
    preserved — a definition that kept its words but dropped its dosage is not
    preserved.
    """
    if unit.numbers and not unit.numbers <= notes_numbers:
        return False
    if not unit.content_words:
        # A unit with no distinctive words is covered only if it was purely
        # numeric and that numeric survived.
        return not unit.numbers or bool(unit.numbers <= notes_numbers)
    shared = len(unit.content_words & notes_words)
    return shared >= max(MIN_FINGERPRINT_SHARED, min(len(unit.content_words), 3))


def notes_word_set(text: str) -> frozenset[str]:
    """The comparable vocabulary of a block of note text."""
    return _content_words(text)


def unit_summary(units: list[ContentUnit], limit: int = 6) -> str:
    """A short, human-readable list of uncovered units for the repair prompt.

    Only the *text* of missing units is emitted, never their metadata, and the
    list is bounded so the repair prompt cannot grow without limit.
    """
    return " | ".join(unit.text[:160] for unit in units[:limit])


def semantic_coverage(
    units: list[ContentUnit], notes_text: str
) -> tuple[float, list[ContentUnit]]:
    """Fraction of expected units preserved in the notes, plus those missing.

    Only "expected" units (:attr:`ContentUnit.is_expectation`) count, so filler
    and fragments never dilute the score. A source with no expected unit (a
    very short or purely conversational chunk) yields ``1.0`` — complete by
    definition — rather than a misleading zero.
    """
    expected = [unit for unit in units if unit.is_expectation]
    if not expected:
        return 1.0, []
    notes_words = notes_word_set(notes_text)
    notes_numbers = frozenset(_extract_numbers(notes_text))
    missing = [
        unit
        for unit in expected
        if not unit_is_covered(unit, notes_words, notes_numbers)
    ]
    return (len(expected) - len(missing)) / len(expected), missing
