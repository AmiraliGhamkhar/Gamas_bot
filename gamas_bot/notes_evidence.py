"""Deterministic evidence checks for generated notes.

The note prompt asks the model to *restructure* a lecture without adding or
removing content. This module is what makes that contract checkable without a
model, a network call or a new dependency:

* :func:`extract_signals` turns a text into the finite set of hard facts a note
  may not invent or lose — numbers with their unit, full dates, Latin terms,
  URLs;
* :func:`audit_notes` compares the generated notes against the source and
  reports *omissions* and *fabrications* separately (recall and precision);
* :func:`validate_table` checks every proposed table cell against the source;
* :func:`validate_tip` requires a verbatim source basis for every practical
  tip;
* :func:`sentence_coverage` is a cheap sentence-level completeness proxy;
* :func:`prose_claim_evidence` verifies individual prose claims against source
  sentences and reports the unsupported ones with their best source span.

Why sets and not counts
-----------------------
:func:`gamas_bot.transcript.fingerprint` counts content units, which catches
loss but not *substitution*: ``12.5`` → ``12.3`` keeps every count identical.
A note may only be checked by comparing the actual values.

Why the unit table exists
-------------------------
The source is spoken Persian and the notes are written Persian:
``۵۰۰ میلی‌گرم`` becomes ``500 mg`` — the same fact, the same value. Without a
unit alias table every legitimate rewrite would look like a fabrication. The
table is deliberately small and auditable; extend it from real lectures, never
guess. Units are compared with a prefix rule as well (``ml`` and ``ml/min``
describe the same measurement when the note keeps the rest in words).

Standard-equivalent policy
--------------------------
An English term that the source does not contain is allowed in exactly these
bounded places, because those are the places a note *names* an equivalent:

* a definition's ``term_en`` field;
* a glossary term;
* a Latin token inside a short parenthetical (no digits, at most five words)
  immediately following a term, or inside a short table cell.

Anything else — an English sentence in a paragraph, an invented acronym, a new
reference — is reported as a fabrication. Numbers are never exempt: a number
that is not in the source is always a fabrication, whatever the field.

Every function here is pure and deterministic. The module never logs user text
and never talks to the network.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, is_dataclass
from functools import lru_cache
from typing import Iterable, Iterator, Sequence

from .textnorm import normalize_digits, normalize_for_compare, normalize_persian

# ===========================================================================
# Units
# ===========================================================================

#: Canonical unit -> spellings seen in Persian lectures and in written notes.
#: The first spelling is the canonical one; the rest are aliases. Extend from
#: real transcripts (a new alias is a fact about the language, not a guess).
UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "mg": ("mg", "میلی‌گرم", "میلی گرم", "میلیگرم", "میلی‌گرمی"),
    "mcg": ("mcg", "µg", "μg", "میکروگرم", "میکرو گرم"),
    "g": ("g", "gr", "گرم"),
    "kg": ("kg", "کیلوگرم", "کیلو گرم"),
    "ml": ("ml", "mL", "میلی‌لیتر", "میلی لیتر", "سی‌سی", "سی سی"),
    "l": ("l", "L", "لیتر"),
    "dl": ("dl", "dL", "دسی‌لیتر", "دسی لیتر"),
    "mg/dl": ("mg/dL", "mg/dl", "میلی‌گرم بر دسی‌لیتر", "میلی گرم بر دسی لیتر"),
    "g/dl": ("g/dL", "g/dl", "گرم بر دسی‌لیتر", "گرم بر دسی لیتر"),
    "ml/min": ("ml/min", "mL/min", "میلی‌لیتر بر دقیقه", "میلی لیتر بر دقیقه"),
    "mmol/l": ("mmol/L", "mmol/l", "میلی‌مول بر لیتر", "میلی مول بر لیتر"),
    "mmhg": ("mmHg", "mm Hg", "میلی‌متر جیوه", "میلی متر جیوه"),
    "bpm": ("bpm", "بار در دقیقه", "ضربه در دقیقه"),
    "%": ("%", "٪", "درصد"),
    "°c": ("°C", "°c", "درجهٔ سانتی‌گراد", "درجه سانتی‌گراد", "درجه"),
    "m2": ("m²", "m2", "متر مربع"),
    "cm": ("cm", "سانتی‌متر", "سانتی متر"),
    "mm": ("mm", "میلی‌متر", "میلی متر"),
    "s": ("s", "sec", "ثانیه"),
    "min": ("min", "دقیقه"),
    "h": ("h", "hr", "ساعت"),
    "kb": ("KB", "kb", "کیلوبایت"),
    "mb": ("MB", "mb", "مگابایت"),
    "gb": ("GB", "gb", "گیگابایت"),
    "ms": ("ms", "میلی‌ثانیه", "میلی ثانیه"),
    "day": ("day", "روز", "روزه"),
    "week": ("week", "هفته", "هفتگی"),
    "month": ("month", "ماه", "ماهه"),
    "year": ("year", "yr", "سال", "ساله"),
    "count": ("count", "نفر", "مورد", "بار"),
}

#: Every known unit spelling, folded, for the term scanner to skip.
_UNIT_TOKENS: frozenset[str] = frozenset(
    alias.casefold() for aliases in UNIT_ALIASES.values() for alias in aliases
)

_UNIT_SPELLINGS: dict[str, str] = {
    alias.casefold(): canonical
    for canonical, aliases in UNIT_ALIASES.items()
    for alias in aliases
}


def _unit_pattern(tokens: Iterable[str] = _UNIT_TOKENS) -> str:
    """One alternation of unit aliases, longest first, whitespace-tolerant."""
    parts: list[str] = []
    for alias in sorted(tokens, key=len, reverse=True):
        pieces = [re.escape(piece) for piece in alias.split()]
        parts.append(r"[\s\u200c]*".join(pieces))
    return "|".join(parts)


_UNIT_ALTERNATION = _unit_pattern()

#: Digits of either Persian/Arabic-Indic shape are folded before extraction.
#: ``(?<![A-Za-z0-9])``/``(?![A-Za-z0-9])`` keep a digit that is part of a term
#: (``HbA1c``, ``SGLT2``) out of the numeric signals.
_NUMBER = r"\d+(?:[.,]\d+)?(?:\s*/\s*\d+(?:[.,]\d+)?)?"
_NUMBER_UNIT = re.compile(
    rf"(?<![A-Za-z0-9])(?P<num>{_NUMBER})\s*(?:(?P<unit>{_UNIT_ALTERNATION}))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)
#: A Latin token that contains a digit (``HbA1c``, ``SGLT2``, ``COVID-19``) is
#: one term, not a term plus a number.
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]*")

#: Function words only: a real term is never one of these.
_LATIN_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can",
        "do", "does", "for", "from", "had", "has", "have", "if", "in", "into",
        "is", "it", "its", "may", "not", "of", "on", "or", "that", "the",
        "their", "then", "there", "these", "they", "this", "to", "was", "were",
        "will", "with", "you", "your",
    }
)

#: Structural numbering: a slide/page/figure/step number is not a content number.
_STRUCTURAL_NUMBER = re.compile(
    r"(?:[Ss]lide|[Pp]age|اسلاید|صفحه(?:ٔ|\s+ی)?|برگ|بخش|گام|مرحله|جدول|شکل|نمودار|فصل|درس|جلسه|تمرین|پرسش|سوال|سؤال)"
    r"\s*[‌ ]?\s*[0-9۰-۹٠-٩]+"
    r"(?:\s*(?:از|از کل)\s*[0-9۰-۹٠-٩]+)?"
    r"(?:\s*\(\s*[0-9۰-۹٠-٩]+\s*/\s*[0-9۰-۹٠-٩]+\s*\))?"
)
_CHUNK_MARKER = re.compile(r"\[[^\]\n]{0,120}?(?:بخش|part)[^\]\n]{0,120}\]", re.IGNORECASE)
#: A full date is one fact. Its separators carry no meaning (``۱۴۰۲/۰۵/۱۲`` and
#: ``1402-05-12`` are the same day), while the components do: a changed month or
#: year is still an unsupported claim.
_DATE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<date>\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})"
    r"(?![A-Za-z0-9])"
)
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_LIST_ORDINAL = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+", re.MULTILINE)

#: A parenthetical immediately after a term is an equivalence position.
_EQUIVALENCE_PAREN = re.compile(r"[\u0600-\u06FF\w][^\n()]{0,40}?\(([^()\n]{1,80})\)")

#: Persian/Arabic decimal and thousands separators.
_NUMBER_FIXUPS = str.maketrans({"٫": ".", "٬": "", "／": "/"})
# Occurrence spans must continue to index the original text. Preserve the
# thousands separator as a one-character comma here; `_canonical_value` folds
# it after matching. The general extraction normalizer above may still drop it.
_NUMBER_SPAN_FIXUPS = str.maketrans({"٫": ".", "٬": ",", "／": "/"})

#: A parenthetical may not be longer than this many words to count as an
#: equivalence position; anything longer is prose, not a term.
_MAX_EQUIVALENT_WORDS = 5

#: Standard equivalents: a Latin term and the Persian spelling the lecturer
#: may have used aloud. When the Persian form is in the source, the English
#: form is a *translation of the same fact*, not new knowledge. This is the
#: narrow, auditable version of "standard equivalents": extend it from real
#: transcripts, and never use it to justify an unsupported claim.
STANDARD_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "python": ("پایتون",),
    "java": ("جاوا",),
    "javascript": ("جاوااسکریپت", "جاوا اسکریپت"),
    "linux": ("لینوکس",),
    "windows": ("ویندوز",),
    "git": ("گیت",),
    "sql": ("اس‌کیوال", "اس کیوال", "سی‌کوئل"),
    "algorithm": ("الگوریتم",),
    "database": ("دیتابیس", "پایگاه داده"),
    # Avoid broad Persian words such as «شما» / «اصلی» as aliases: they occur
    # in ordinary prose and would incorrectly ground an unrelated term.
    "schema": ("اسکیما",),
    "relation": ("رابطه‌ای",),
    "tuple": ("چندتایی", "تاپل"),
    "attribute": ("صفت", "ویژگی"),
    "index": ("ایندکس", "نمایه"),
    "primary": ("کلید اصلی",),
    "foreign": ("کلید خارجی",),
    "wimp": ("ویمپ",),
    "fitts": ("فیتس",),
    "nielsen": ("نیلسن",),
    "metformin": ("متفورمین",),
    "dapagliflozin": ("داپاگلیفلوزین",),
    "hba1c": ("اچ‌بی‌ای‌وان‌سی", "هموگلوبین گلیکوزیله"),
    "egfr": ("ای‌جی‌اف‌آر", "نرخ فیلتراسیون گلومرولی"),
    # Common, unambiguous spoken forms of abbreviations in Persian lectures.
    "api": ("ای‌پی‌آی", "رابط برنامه‌نویسی کاربردی"),
    "cpu": ("سی‌پی‌یو", "پردازندهٔ مرکزی"),
    "gpu": ("جی‌پی‌یو", "پردازندهٔ گرافیکی"),
    "covid-19": ("کووید-۱۹", "کووید ۱۹"),
}


def _comparison_phrase(text: str) -> str:
    """Normalize punctuation and spacing for terminology alias comparison."""
    folded = normalize_for_compare(text or "")
    folded = re.sub(r"[^\w]+", " ", folded, flags=re.UNICODE)
    return re.sub(r"\s+", " ", folded).strip()


_NORMALIZED_STANDARD_EQUIVALENTS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (token, tuple(_comparison_phrase(item) for item in spellings))
    for token, spellings in STANDARD_EQUIVALENTS.items()
)

# Persian number words are recognized only when they directly qualify an
# explicit unit/quantity marker. This avoids treating the common article «یک»
# as a numeric fact in every sentence.
_NUMBER_WORD_VALUES: dict[str, int] = {
    "صفر": 0,
    "یک": 1, "يك": 1, "یه": 1, "یك": 1,
    "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "شیش": 6,
    "هفت": 7, "هشت": 8, "نه": 9, "ده": 10,
    "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14, "پانزده": 15,
    "شانزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19,
    "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60,
    "هفتاد": 70, "هشتاد": 80, "نود": 90,
    "صد": 100, "یکصد": 100, "دویست": 200, "سیصد": 300,
    "چهارصد": 400, "پانصد": 500, "ششصد": 600, "هفتصد": 700,
    "هشتصد": 800, "نهصد": 900,
    "هزار": 1_000, "میلیون": 1_000_000, "میلیارد": 1_000_000_000,
}
_NUMBER_WORD_ATOM = "|".join(
    re.escape(word) for word in sorted(_NUMBER_WORD_VALUES, key=len, reverse=True)
)
# Large year phrases («هزار سال») are common rhetorical/historical periods in
# Persian humanities text, not measurements. Keep year aliases for digit values,
# but do not turn number words followed by «سال» into hard numeric signals.
_NUMBER_WORD_UNIT_TOKENS = frozenset(
    alias for alias in _UNIT_TOKENS if _UNIT_SPELLINGS.get(alias) != "year"
)
_NUMBER_WORD_UNIT_ALTERNATION = _unit_pattern(_NUMBER_WORD_UNIT_TOKENS)
_NUMBER_WORD_UNIT = re.compile(
    rf"(?<![\w])(?P<num>(?:{_NUMBER_WORD_ATOM})"
    rf"(?:(?:[\s\u200c]*و[\s\u200c]*|[\s\u200c]+)(?:{_NUMBER_WORD_ATOM})){{0,5}})"
    rf"[\s\u200c]*(?P<unit>{_NUMBER_WORD_UNIT_ALTERNATION})(?![\w])",
    re.UNICODE | re.IGNORECASE,
)
_NUMBER_WORD_TOKEN = re.compile(rf"(?<![\w])(?:{_NUMBER_WORD_ATOM})(?![\w])", re.UNICODE)
_NORMALIZED_NUMBER_WORD_VALUES = {
    normalize_for_compare(word): value for word, value in _NUMBER_WORD_VALUES.items()
}

_HORIZONTAL_SPACES = re.compile(r"[ \t]+")
_UNIT_SPACE_COLLAPSE = re.compile(r"[\s\u200c]+")
_CONTENT_WORD_TOKEN = re.compile(r"[0-9a-z][0-9a-z_+#.\-]*|[\u0600-\u06FF]{3,}")


# ===========================================================================
# Signals
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Signal:
    """One hard fact of the source: a number, a term or a URL.

    ``value`` is canonical: digits folded, decimal point normalised, Latin
    case folded. ``unit`` is canonical or empty. Two signals are the same fact
    when their values match and their units are compatible (one side may omit
    the unit — ``48`` and ``48 ml`` refer to the same value).
    """

    kind: str  # "number" | "term" | "url"
    value: str
    unit: str = ""

    @property
    def label(self) -> str:
        if self.kind != "number" or not self.unit:
            return self.value
        return f"{self.value} {self.unit}"


@dataclass(frozen=True, slots=True)
class SignalOccurrence:
    """A normalized signal and its exact span in the original text."""

    signal: Signal
    start: int
    end: int


def units_compatible(first: str, second: str) -> bool:
    """Whether two canonical units describe the same measurement.

    Equal units match; a missing unit matches anything (the note may write the
    unit in words); and a compound unit is compatible with its leading or
    trailing component, so ``48 ml بر دقیقه`` matches ``48 ml/min``. ``mmHg``
    and ``mm`` are deliberately *not* compatible.
    """
    if not first or not second or first == second:
        return True
    if first.startswith(second + "/") or second.startswith(first + "/"):
        return True
    if first.endswith("/" + second) or second.endswith("/" + first):
        return True
    return False


@dataclass(frozen=True, slots=True)
class TableVerdict:
    """The outcome of checking one proposed table against the source."""

    accepted: bool
    numeric_total: int = 0
    numeric_failed: tuple[str, ...] = ()
    text_total: int = 0
    text_failed: tuple[str, ...] = ()
    reason: str = ""

    @property
    def failed_cells(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.numeric_failed + self.text_failed))


@dataclass(frozen=True, slots=True)
class SignalAudit:
    """Omissions and fabrications of a generated document, separated."""

    source_numbers: tuple[Signal, ...] = ()
    source_terms: tuple[Signal, ...] = ()
    covered_numbers: int = 0
    covered_terms: int = 0
    missing: tuple[Signal, ...] = ()
    fabricated: tuple[Signal, ...] = ()
    equivalent_additions: tuple[str, ...] = ()
    note_signal_total: int = 0

    @property
    def number_total(self) -> int:
        return len(self.source_numbers)

    @property
    def term_total(self) -> int:
        return len(self.source_terms)

    @property
    def total(self) -> int:
        return self.number_total + self.term_total

    @property
    def number_coverage(self) -> float:
        if not self.number_total:
            return 1.0
        return self.covered_numbers / self.number_total

    @property
    def term_coverage(self) -> float:
        if not self.term_total:
            return 1.0
        return self.covered_terms / self.term_total

    @property
    def coverage(self) -> float:
        if not self.total:
            return 1.0
        return (self.covered_numbers + self.covered_terms) / self.total

    @property
    def precision(self) -> float:
        if not self.note_signal_total:
            return 1.0
        return (self.note_signal_total - len(self.fabricated)) / self.note_signal_total

    @property
    def grounded(self) -> bool:
        return not self.fabricated

    def summary(self) -> str:
        """A log-safe one-line description (counts only, never text)."""
        return (
            f"signals={self.total} numbers={self.number_total} terms={self.term_total} "
            f"coverage={self.coverage:.2f} number_coverage={self.number_coverage:.2f} "
            f"term_coverage={self.term_coverage:.2f} missing={len(self.missing)} "
            f"fabricated={len(self.fabricated)} "
            f"equivalents={len(self.equivalent_additions)}"
        )


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """A sentence-level completeness proxy (see :func:`sentence_coverage`)."""

    sentences: int
    matched: int
    ratio: float


# ===========================================================================
# Normalisation helpers
# ===========================================================================


def _fold(text: str) -> str:
    """Digits, decimal separators and whitespace, without touching letters."""
    folded = normalize_persian(text or "", digits=True)
    folded = folded.translate(_NUMBER_FIXUPS)
    return _HORIZONTAL_SPACES.sub(" ", folded)


def _canonical_value(raw: str) -> str:
    """Canonical numeric text: ``۱۲٫۵``/``12,5`` -> ``12.5``; ``1,000`` -> ``1000``."""
    value = raw.replace(" ", "")
    if "," in value and "." not in value:
        head, _, tail = value.rpartition(",")
        # ``1,000`` is a thousands separator; ``6,5`` is a decimal comma.
        value = head + ("" if len(tail) == 3 and head else ".") + tail
    value = value.replace(",", ".")
    if value.endswith("0") and "." in value:
        value = value.rstrip("0").rstrip(".")
    return value


def _canonical_date(raw: str) -> str:
    """One canonical date value: ``۱۴۰۲/۵/۱۲`` -> ``1402-05-12``."""
    parts = [part for part in re.split(r"[/-]", raw) if part]
    return "-".join(part.zfill(2) if len(part) <= 2 else part for part in parts)


def _canonical_unit(raw: str) -> str:
    if not raw:
        return ""
    key = _UNIT_SPACE_COLLAPSE.sub(" ", raw.strip().casefold())
    canonical = _UNIT_SPELLINGS.get(key, key)
    # Count words are quantity markers, not measurement units; retain the
    # longstanding `Signal(number, value)` shape for values such as «۱٬۰۰۰ نفر».
    return "" if canonical == "count" else canonical


def _parse_number_words(raw: str) -> int | None:
    """Parse a bounded Persian number phrase such as «هفتاد و پنج».

    The caller only invokes this for a phrase immediately followed by an
    explicit unit, so ordinary «یک» in prose is not elevated into a numeric
    assertion. Unknown/mixed phrases fail closed instead of guessing.
    """
    words = [
        _NORMALIZED_NUMBER_WORD_VALUES.get(normalize_for_compare(match.group(0)))
        for match in _NUMBER_WORD_TOKEN.finditer(raw)
    ]
    if not words or any(value is None for value in words):
        return None
    total = 0
    current = 0
    for value in words:
        assert value is not None
        if value < 100:
            current += value
        elif value < 1_000:
            current = (current or 1) * value
        else:
            total += (current or 1) * value
            current = 0
    return total + current


def mask_structural(text: str) -> str:
    """Remove numbering that is layout, not content.

    Slide/figure/step numbers, chunk markers (``[بخش ۱ از ۳ …]``), Markdown
    heading marks and list ordinals are removed before signals are extracted.
    A presentation's ``### اسلاید ۳`` heading must not become the number ``3``,
    while the *semester* ``۱۴۰۵`` beside it is content and stays.
    """
    if not text:
        return ""
    masked = _MARKDOWN_HEADING.sub(" ", text)
    masked = _CHUNK_MARKER.sub(" ", masked)
    masked = _STRUCTURAL_NUMBER.sub(" ", masked)
    masked = _LIST_ORDINAL.sub(" ", masked)
    return masked


# ===========================================================================
# Extraction
# ===========================================================================


def _iter_urls(text: str) -> Iterator[tuple[int, int, str]]:
    for match in _URL.finditer(text):
        raw = match.group(0).rstrip(".,;،؛:!?؟»)]}")
        yield match.start(), match.start() + len(raw), raw


def _blanked(source: str, ranges: Sequence[tuple[int, int]]) -> str:
    """Replace spans by spaces, keeping offsets and newlines intact."""
    if not ranges:
        return source
    chars = list(source)
    for start, end in ranges:
        for index in range(max(0, start), min(len(chars), end)):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def extract_signals(text: str, *, mask: bool = True) -> frozenset[Signal]:
    """Every hard fact in ``text``, as a set.

    Order matters: URLs are removed first (a path segment is not a number),
    then Latin terms (so ``HbA1c``'s digit is not a number), then numbers with
    their optional unit. Unit words themselves are not term signals.
    """
    if not text:
        return frozenset()
    # Fold digits and decimal separators *before* the structural masks and the
    # number regex: ``۱۲٫۵`` is one number, and ``۱٬۰۰۰`` is a thousand.
    folded = _fold(text)
    working = mask_structural(folded) if mask else folded
    signals: set[Signal] = set()

    # 1. URLs are atomic.
    url_spans: list[tuple[int, int]] = []
    for start, end, raw in _iter_urls(working):
        signals.add(Signal("url", raw.casefold()))
        url_spans.append((start, end))
    without_urls = _blanked(working, url_spans)

    # 2. Dates, before the number scanner can split them into components.
    date_spans: list[tuple[int, int]] = []
    for match in _DATE.finditer(without_urls):
        signals.add(Signal("number", _canonical_date(match.group("date"))))
        date_spans.append((match.start(), match.end()))
    without_dates = _blanked(without_urls, date_spans)

    # 3. Latin terms, including the ones that carry a digit.
    term_spans: list[tuple[int, int]] = []
    for match in _LATIN_TOKEN.finditer(without_dates):
        token = match.group(0).strip(".-+")
        if not token:
            continue
        folded = token.casefold()
        if folded in _UNIT_TOKENS or folded in _LATIN_STOPWORDS:
            continue
        if len(folded) < 2:
            continue
        signals.add(Signal("term", folded))
        term_spans.append((match.start(), match.start() + len(token)))
    without_terms = _blanked(without_dates, term_spans)

    # 4. Numeric digits with their optional unit.
    for match in _NUMBER_UNIT.finditer(without_terms):
        value = _canonical_value(normalize_digits(match.group("num")))
        if not value:
            continue
        unit = _canonical_unit(match.group("unit") or "")
        signals.add(Signal("number", value, unit))

    # 5. Persian number words only when they immediately qualify a known unit
    # or quantity marker (e.g. «پنجاه میلی‌گرم», «سه روز»). The exact value is
    # normalized, so it remains a hard signal and an unsupported value is still
    # detected even when the two sides use different number representations.
    for match in _NUMBER_WORD_UNIT.finditer(without_terms):
        number_value = _parse_number_words(match.group("num"))
        if number_value is None:
            continue
        unit = _canonical_unit(match.group("unit") or "")
        signals.add(Signal("number", str(number_value), unit))
    return frozenset(signals)


def _offset_preserving_structural_mask(text: str) -> str:
    """Mask layout numbers without changing offsets needed by local scrubbing."""
    normalized = normalize_digits(normalize_persian(text or "")).translate(_NUMBER_SPAN_FIXUPS)
    ranges = [
        match.span()
        for pattern in (_MARKDOWN_HEADING, _CHUNK_MARKER, _STRUCTURAL_NUMBER, _LIST_ORDINAL)
        for match in pattern.finditer(normalized)
    ]
    return _blanked(normalized, ranges)


def extract_number_occurrences(
    text: str, *, mask: bool = True
) -> tuple[SignalOccurrence, ...]:
    """Return exact numeric spans, including date and number-word forms.

    Matching uses the same canonical values and units as :func:`extract_signals`.
    The normalized text is length-preserving (character folding only), so spans
    map back to the original string and a scrubber can edit only the unsupported
    claim rather than deleting its whole sentence.
    """
    if not text:
        return ()
    working = (
        _offset_preserving_structural_mask(text)
        if mask
        else normalize_digits(normalize_persian(text)).translate(_NUMBER_SPAN_FIXUPS)
    )
    ranges: list[tuple[int, int]] = []
    for match in _URL.finditer(working):
        ranges.append(match.span())
    without_urls = _blanked(working, ranges)

    occurrences: list[SignalOccurrence] = []
    date_ranges: list[tuple[int, int]] = []
    for match in _DATE.finditer(without_urls):
        occurrences.append(
            SignalOccurrence(
                Signal("number", _canonical_date(match.group("date"))),
                match.start(), match.end(),
            )
        )
        date_ranges.append(match.span())
    without_dates = _blanked(without_urls, date_ranges)
    term_ranges: list[tuple[int, int]] = []
    for match in _LATIN_TOKEN.finditer(without_dates):
        token = match.group(0).strip(".-+")
        folded_token = token.casefold()
        if folded_token in _UNIT_TOKENS or folded_token in _LATIN_STOPWORDS:
            continue
        if len(folded_token) >= 2:
            term_ranges.append((match.start(), match.start() + len(token)))
    without_terms = _blanked(without_dates, term_ranges)

    for match in _NUMBER_UNIT.finditer(without_terms):
        value = _canonical_value(match.group("num"))
        unit = _canonical_unit(match.group("unit") or "")
        occurrences.append(
            SignalOccurrence(Signal("number", value, unit), match.start(), match.end())
        )
    for match in _NUMBER_WORD_UNIT.finditer(without_terms):
        number_value = _parse_number_words(match.group("num"))
        if number_value is None:
            continue
        unit = _canonical_unit(match.group("unit") or "")
        occurrences.append(
            SignalOccurrence(
                Signal("number", str(number_value), unit), match.start(), match.end()
            )
        )
    return tuple(sorted(occurrences, key=lambda item: (item.start, item.end)))


def extract_term_occurrences(text: str) -> tuple[SignalOccurrence, ...]:
    """Return the exact URL/Latin-term spans used by the signal audit.

    Offsets are measured in the original text. Persian letter normalization
    replaces characters one-for-one, so it does not shift the spans; URLs remain
    atomic and are not rescanned as terms.
    """
    if not text:
        return ()
    working = normalize_persian(text)
    url_occurrences: list[SignalOccurrence] = []
    url_ranges: list[tuple[int, int]] = []
    for start, end, raw in _iter_urls(working):
        url_occurrences.append(SignalOccurrence(Signal("url", raw.casefold()), start, end))
        url_ranges.append((start, end))
    without_urls = _blanked(working, url_ranges)
    occurrences = list(url_occurrences)
    for match in _LATIN_TOKEN.finditer(without_urls):
        token = match.group(0).strip(".-+")
        folded = token.casefold()
        if len(folded) < 2 or folded in _UNIT_TOKENS or folded in _LATIN_STOPWORDS:
            continue
        occurrences.append(
            SignalOccurrence(Signal("term", folded), match.start(), match.start() + len(token))
        )
    return tuple(sorted(occurrences, key=lambda item: (item.start, item.end)))


#: CR-3: FULL is the only note mode, so there is a single recall floor. Numbers
#: are the hard facts of a lecture (0.95); terms are held to the same floor.
#: ``mode`` is accepted on :func:`coverage_thresholds` for backward-compatible
#: callers but no longer selects between different floors.
NUMBER_COVERAGE_FLOORS = {"full": 0.95}
TERM_COVERAGE_FLOORS = {"full": 0.95}


def coverage_thresholds(mode: str) -> tuple[float, float]:
    """The ``(number, term)`` recall floors; always the FULL floors."""
    key = mode if mode in NUMBER_COVERAGE_FLOORS else "full"
    return NUMBER_COVERAGE_FLOORS[key], TERM_COVERAGE_FLOORS[key]


def _numbers(signals: Iterable[Signal]) -> tuple[Signal, ...]:
    return tuple(signal for signal in signals if signal.kind == "number")


def _terms(signals: Iterable[Signal]) -> tuple[Signal, ...]:
    return tuple(signal for signal in signals if signal.kind in ("term", "url"))


def _build_signal_lookup(signals: Iterable[Signal]) -> dict[tuple[str, str], tuple[Signal, ...]]:
    """Index signals by ``(kind, value)`` for O(1) compatibility lookups."""
    grouped: dict[tuple[str, str], list[Signal]] = {}
    for signal in signals:
        grouped.setdefault((signal.kind, signal.value), []).append(signal)
    return {key: tuple(items) for key, items in grouped.items()}


def _covers_lookup(
    signal: Signal, lookup: dict[tuple[str, str], tuple[Signal, ...]]
) -> bool:
    bucket = lookup.get((signal.kind, signal.value))
    if not bucket:
        return False
    if signal.kind != "number":
        return True
    return any(units_compatible(signal.unit, candidate.unit) for candidate in bucket)


@dataclass(frozen=True, slots=True)
class SourceEvidenceIndex:
    """Pre-computed source representations so evidence checks avoid redundant passes."""

    source: str
    masked_folded: str
    masked_folded_lower: str
    masked_raw: str
    compare_source: str
    compare_masked: str
    signals: frozenset[Signal]
    numbers: tuple[Signal, ...]
    terms: tuple[Signal, ...]
    number_lookup: dict[tuple[str, str], tuple[Signal, ...]]
    term_lookup: dict[tuple[str, str], tuple[Signal, ...]]
    masked_words: frozenset[str]
    raw_sentences: tuple[tuple[str, frozenset[str]], ...]
    masked_sentences: tuple[tuple[str, frozenset[str]], ...]
    standard_equivalent_tokens: frozenset[str]


@lru_cache(maxsize=16)
def build_source_index(source: str) -> SourceEvidenceIndex:
    """Build and cache the pre-computed evidence index for *source*."""
    folded = _fold(source)
    masked_folded = mask_structural(folded)
    masked_raw = mask_structural(source)
    compare_source = normalize_for_compare(source)
    compare_masked = normalize_for_compare(masked_raw)
    signals = extract_signals(source)
    numbers = _numbers(signals)
    terms = _terms(signals)
    masked_words = content_words(masked_raw)
    raw_sentences = tuple(
        (sentence, content_words(sentence))
        for sentence in split_sentences(source)
    )
    masked_sentences = tuple(
        (sentence, content_words(sentence))
        for sentence in split_sentences(masked_raw)
    )
    normalized_source = _comparison_phrase(source)
    standard_equivalent_tokens = (
        frozenset(
            token
            for token, norm_spellings in _NORMALIZED_STANDARD_EQUIVALENTS
            if any(_phrase_contains(normalized_source, item) for item in norm_spellings)
        )
        if normalized_source
        else frozenset()
    )
    return SourceEvidenceIndex(
        source=source,
        masked_folded=masked_folded,
        masked_folded_lower=masked_folded.casefold(),
        masked_raw=masked_raw,
        compare_source=compare_source,
        compare_masked=compare_masked,
        signals=signals,
        numbers=numbers,
        terms=terms,
        number_lookup=_build_signal_lookup(numbers),
        term_lookup=_build_signal_lookup(terms),
        masked_words=masked_words,
        raw_sentences=raw_sentences,
        masked_sentences=masked_sentences,
        standard_equivalent_tokens=standard_equivalent_tokens,
    )


# ===========================================================================
# Notes audit
# ===========================================================================


_METADATA_KEYS: frozenset[str] = frozenset({"note_mode", "source_locators"})


def _payload_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in _METADATA_KEYS:
                continue
            yield from _payload_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _payload_strings(item)


def _payload_of(notes: object) -> object:
    """The note payload without non-prose metadata.

    ``note_mode`` and ``source_locators`` are producer/citation metadata rather
    than prose assertions, and a bare dataclass section (e.g. ``NoteSection``)
    is wrapped under ``sections`` so both text extraction and equivalence-slot
    checks inspect it identically to a full ``StructuredNotes`` payload.
    """
    if hasattr(notes, "to_payload"):
        payload: object = notes.to_payload()
    elif is_dataclass(notes) and not isinstance(notes, type):
        raw_dc = asdict(notes)
        payload = {"sections": [raw_dc]} if "sections" not in raw_dc and "heading" in raw_dc else raw_dc
    else:
        payload = notes
    if isinstance(payload, dict) and any(k in payload for k in _METADATA_KEYS):
        payload = {key: value for key, value in payload.items() if key not in _METADATA_KEYS}
    return payload


def _notes_text(notes: object) -> str:
    """All user-visible text of a note model, in one corpus."""
    return "\n".join(_payload_strings(_payload_of(notes)))


def _is_equivalence_paren(content: str) -> bool:
    """A parenthetical is an equivalence position only when it is term-like."""
    stripped = content.strip()
    if not stripped or any(char.isdigit() for char in stripped):
        return False
    return len(stripped.split()) <= _MAX_EQUIVALENT_WORDS


def _table_cells(table: object) -> Iterator[str]:
    for header in getattr(table, "headers", ()) or ():
        yield str(header)
    for row in getattr(table, "rows", ()) or ():
        for cell in row:
            yield str(cell)


def _collect_tokens(text: object, allowed: set[str]) -> None:
    for token in _LATIN_TOKEN.findall(str(text or "")):
        allowed.add(token.casefold())


def _collect_table(table: object, allowed: set[str]) -> None:
    if isinstance(table, dict):
        for header in table.get("headers") or ():
            _collect_tokens(header, allowed)
        for row in table.get("rows") or ():
            for cell in row if isinstance(row, (list, tuple)) else (row,):
                _collect_tokens(cell, allowed)
        return
    for cell in _table_cells(table):
        _collect_tokens(cell, allowed)


def _phrase_contains(text: str, phrase: str) -> bool:
    """Boundary-aware phrase matching after Persian/punctuation normalization."""
    if not text or not phrase:
        return False
    return bool(re.search(rf"(?<![\w]){re.escape(phrase)}(?![\w])", text, re.UNICODE))


def _term_aliases(term: str) -> tuple[str, ...]:
    """Known Persian/English forms for a Latin term; no open-ended inference."""
    normalized = _comparison_phrase(term)
    aliases: list[str] = []
    for token, spellings in _NORMALIZED_STANDARD_EQUIVALENTS:
        if normalized == _comparison_phrase(token):
            aliases.extend(spellings)
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def _term_matches_text(term: str, text: str) -> bool:
    """Exact or explicitly registered alias match in a normalized corpus."""
    normalized_term = _comparison_phrase(term)
    if _phrase_contains(text, normalized_term):
        return True
    return any(_phrase_contains(text, alias) for alias in _term_aliases(term))


def _term_is_in_source(signal: Signal, source: str, source_index: SourceEvidenceIndex) -> bool:
    if _covers_lookup(signal, source_index.term_lookup):
        return True
    return signal.kind == "term" and _term_matches_text(signal.value, _comparison_phrase(source))


def _term_is_in_notes(
    signal: Signal,
    notes_lookup: dict[tuple[str, str], tuple[Signal, ...]],
    notes_text: str,
) -> bool:
    if _covers_lookup(signal, notes_lookup):
        return True
    return signal.kind == "term" and _term_matches_text(signal.value, notes_text)


def _equivalent_additions(
    notes: object,
    source: str = "",
    *,
    source_index: SourceEvidenceIndex | None = None,
) -> frozenset[str]:
    """Latin tokens in equivalence positions (see the module docstring).

    ``term_en`` fields, glossary terms, short parenthetical expansions, table
    cells and code examples are the places a note is allowed to name a
    standard English equivalent or write spoken code in real syntax — the
    lecturer says "print in parentheses", the note writes ``print(...)``.

    Paragraph prose is *not* an equivalence position: an English sentence
    inside a section is exactly what this check must catch.
    """
    allowed: set[str] = set()
    payload = _payload_of(notes)
    if not isinstance(payload, dict):
        for text in _payload_strings(payload):
            for match in _EQUIVALENCE_PAREN.finditer(text):
                if _is_equivalence_paren(match.group(1)):
                    _collect_tokens(match.group(1), allowed)
        return frozenset(allowed)

    for section in payload.get("sections") or ():
        if not isinstance(section, dict):
            continue
        for definition in section.get("definitions") or ():
            if isinstance(definition, dict):
                _collect_tokens(definition.get("term_en"), allowed)
                _collect_tokens(definition.get("term"), allowed)
        for table in section.get("tables") or ():
            _collect_table(table, allowed)
        _collect_table(section.get("table"), allowed)
        for field_name in ("examples", "formulas"):
            for item in section.get(field_name) or ():
                _collect_tokens(item, allowed)
    for entry in payload.get("glossary") or ():
        if isinstance(entry, dict):
            _collect_tokens(entry.get("term"), allowed)
    for text in _payload_strings(payload):
        for match in _EQUIVALENCE_PAREN.finditer(text):
            if _is_equivalence_paren(match.group(1)):
                _collect_tokens(match.group(1), allowed)
    if source_index is not None:
        allowed.update(source_index.standard_equivalent_tokens)
    elif source:
        allowed.update(build_source_index(source).standard_equivalent_tokens)
    return frozenset(allowed)


def audit_notes(
    source: str,
    notes: object,
    *,
    source_index: SourceEvidenceIndex | None = None,
) -> SignalAudit:
    """Compare the source's hard facts with the ones the notes assert.

    ``notes`` may be a :class:`gamas_bot.notes.StructuredNotes` or any object
    with the same ``to_payload`` (the tests use plain dicts). Omissions and
    fabrications are reported separately: the first is a completeness defect,
    the second a faithfulness defect, and they need different repairs.
    """
    idx = source_index if source_index is not None else build_source_index(source)
    note_text = _notes_text(notes)
    normalized_notes = _comparison_phrase(note_text)
    normalized_source = _comparison_phrase(source)
    note_signals = extract_signals(note_text)

    source_numbers = idx.numbers
    source_terms = idx.terms
    note_numbers = _numbers(note_signals)
    note_terms = _terms(note_signals)
    note_number_lookup = _build_signal_lookup(note_numbers)
    note_term_lookup = _build_signal_lookup(note_terms)
    allowed = _equivalent_additions(notes, source, source_index=idx)

    missing_numbers = tuple(
        signal
        for signal in source_numbers
        if not _covers_lookup(signal, note_number_lookup)
    )
    missing_terms = tuple(
        signal
        for signal in source_terms
        if not _term_is_in_notes(signal, note_term_lookup, normalized_notes)
    )
    fabricated_numbers = tuple(
        signal
        for signal in note_numbers
        if not _covers_lookup(signal, idx.number_lookup)
    )
    fabricated_terms = tuple(
        signal
        for signal in note_terms
        if signal.value not in allowed
        and not _term_is_in_source(signal, normalized_source, idx)
    )
    return SignalAudit(
        source_numbers=source_numbers,
        source_terms=source_terms,
        covered_numbers=len(source_numbers) - len(missing_numbers),
        covered_terms=len(source_terms) - len(missing_terms),
        missing=tuple(sorted(missing_numbers + missing_terms, key=lambda s: s.label)),
        fabricated=tuple(sorted(fabricated_numbers + fabricated_terms, key=lambda s: s.label)),
        equivalent_additions=tuple(sorted(allowed)),
        note_signal_total=len(note_numbers) + len(note_terms),
    )


def evidence_windows(
    source: str,
    signals: Sequence[Signal],
    *,
    radius: int = 160,
    source_index: SourceEvidenceIndex | None = None,
) -> tuple[tuple[Signal, str], ...]:
    """The source window in which each signal occurs.

    Used by the repair pass: the model receives the fact it lost and the exact
    text it came from, so restoring it is copying, not inventing.
    """
    if not signals or not source:
        return ()
    idx = source_index if source_index is not None else build_source_index(source)
    source_numbers = extract_number_occurrences(source)
    source_lower = source.casefold()
    windows: list[tuple[Signal, str]] = []
    for signal in signals:
        if signal.kind == "number":
            occurrence = next(
                (
                    item for item in source_numbers
                    if item.signal.value == signal.value
                    and units_compatible(item.signal.unit, signal.unit)
                ),
                None,
            )
            if occurrence is None:
                continue
            index, end_index = occurrence.start, occurrence.end
        else:
            index = source_lower.find(signal.value.casefold())
            if index < 0:
                # Persian aliases are not signal terms themselves, but keep the
                # cached normalized text as a fallback for legacy callers.
                index = idx.masked_folded_lower.find(signal.value.casefold())
                if index < 0:
                    continue
                end_index = index + len(signal.value)
            else:
                end_index = index + len(signal.value)
        start = max(0, index - radius)
        end = min(len(source), end_index + radius)
        windows.append((signal, " ".join(source[start:end].split()).strip()))
    return tuple(windows)


# ===========================================================================
# Sentence coverage
# ===========================================================================

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟…])\s+|\n{2,}")
_PERSIAN_STOPWORDS = frozenset(
    {
        "و", "در", "به", "از", "با", "بر", "برای", "که", "این", "آن", "های",
        "ها", "یک", "یا", "تا", "را", "هم", "می", "شود", "است", "بود", "شد",
        "شده", "می‌شود", "میشود", "می‌شوند", "میشوند", "می‌کند", "میکند",
        "می‌کنند", "میکنند", "کرد", "کند", "دارد", "داریم", "بین", "روی", "اگر",
        "اما", "نیز", "هر", "چه", "چون", "زیرا", "بنابراین", "پس", "ما", "شما",
        "آنها", "وی", "او", "من", "خود", "دیگر", "بسیار", "خیلی", "طور",
    }
)
_NORMALIZED_PERSIAN_STOPWORDS = frozenset(
    normalize_for_compare(word) for word in _PERSIAN_STOPWORDS
)
_PERSIAN_PLURAL_SUFFIXES = ("هایشان", "هایمان", "هایتان", "هایی", "های", "ها")


def _stem_content_word(word: str) -> str:
    """Fold common Persian plurals and English plural endings conservatively."""
    if any(0x0600 <= ord(char) <= 0x06FF for char in word):
        for suffix in _PERSIAN_PLURAL_SUFFIXES:
            if word.endswith(suffix) and len(word) > len(suffix) + 2:
                return word[:-len(suffix)]
        return word
    if len(word) > 5:
        if word.endswith("ies"):
            return word[:-3] + "y"
        if word.endswith("s") and not word.endswith(("ss", "us", "is")):
            return word[:-1]
    return word


def content_words(text: str) -> frozenset[str]:
    """Meaning-bearing words after Unicode, spacing and light inflection folding."""
    folded = normalize_for_compare(text or "")
    # Use an explicit unit boundary for number words, then compare the same
    # canonical number as a digit-form note. Other number words remain ordinary
    # prose so «یک» is not treated as an assertion by itself.
    for match in tuple(_NUMBER_WORD_UNIT.finditer(folded)):
        number = _parse_number_words(match.group("num"))
        if number is not None:
            folded = folded.replace(match.group(0), f" {number} ", 1)
    words = (_stem_content_word(word) for word in _CONTENT_WORD_TOKEN.findall(folded))
    return frozenset(
        word
        for word in words
        if word
        and word not in _NORMALIZED_PERSIAN_STOPWORDS
        and word not in _LATIN_STOPWORDS
    )


def split_sentences(text: str) -> tuple[str, ...]:
    """Deterministic sentence split for Persian prose and transcripts."""
    if not text:
        return ()
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part and part.strip()]
    return tuple(part for part in parts if len(part) >= 12)


def sentence_coverage(
    source: str,
    notes: object,
    *,
    minimum_ratio: float = 0.6,
    source_index: SourceEvidenceIndex | None = None,
) -> CoverageReport:
    """The share of source sentences with most of their words in the notes.

    This is a *proxy*: it cannot see paraphrase beyond shared vocabulary, so it
    is used to trigger a repair and to log, never to reject a booklet on its
    own.
    """
    idx = source_index if source_index is not None else build_source_index(source)
    sentence_pairs = idx.masked_sentences
    if not sentence_pairs:
        return CoverageReport(0, 0, 1.0)
    note_words = content_words(_notes_text(notes))
    matched = 0
    for _sentence, words in sentence_pairs:
        if not words:
            matched += 1  # nothing to lose
            continue
        shared = len(words & note_words)
        if shared / len(words) >= minimum_ratio:
            matched += 1
    return CoverageReport(len(sentence_pairs), matched, matched / len(sentence_pairs))


# ===========================================================================
# Claim-level evidence
# ===========================================================================

#: A claim with fewer content words than this is a label, not an assertion.
MIN_CLAIM_WORDS = 6
#: A claim whose best source sentence shares less than this share of its content
#: words has no evidence at all. Deliberately low: this is not a similarity
#: score used to grade paraphrase, it is the floor below which nothing in the
#: source supports the sentence. Numeric and term grounding stay strict and
#: independent (`audit_notes`).
MIN_CLAIM_SUPPORT = 0.2

_HIGH_RISK_MEDICAL_MARKERS = (
    "دوز", "میلی گرم", "میلیگرم", "میکروگرم", "mg", "mcg", "mg dl", "mmhg",
    "آستانه", "حد تشخیص", "حد طبیعی", "قند خون", "فشار خون", "مقدار مجاز",
    "تجویز", "مصرف", "باید مصرف", "نباید مصرف", "درمان", "تشخیص", "تزریق",
    "قطع دارو", "شروع دارو", "افزایش دوز", "کاهش دوز", "منع مصرف", "عوارض جانبی",
    "dose", "dosage", "prescribe", "prescription", "diagnos", "treat",
    "administer", "inject", "take", "should take", "must take", "medication",
    "drug", "contraindication", "side effect", "normal range", "target range",
    "threshold", "cutoff", "increase the dose", "decrease the dose",
    "start the medication", "stop the medication",
)


def classify_claim_risk(text: str) -> str:
    """Conservative deterministic risk label for an unsupported prose claim."""
    folded = _comparison_phrase(text)
    if any(_phrase_contains(folded, _comparison_phrase(marker)) for marker in _HIGH_RISK_MEDICAL_MARKERS):
        return "high"
    return "medium"


#: Sentences that talk about the document rather than about the subject. They
#: carry no claim to verify, so they must never be reported as unsupported.
_META_SENTENCE_MARKERS = (
    "در ادامه",
    "در این بخش",
    "این بخش",
    "به بررسی",
    "بررسی می‌کنیم",
    "بررسی میکنیم",
    "خواهیم دید",
    "مشاهده می‌کنیم",
    "مشاهده میکنیم",
    "در این درس",
    "در این جلسه",
    "خلاصهٔ",
    "خلاصه ی",
    "فهرست",
    "این مطلب",
    "همان‌طور که",
)


@dataclass(frozen=True, slots=True)
class ClaimEvidence:
    """One prose claim with no supporting source sentence.

    Only offsets and a ratio are kept: the report is logged, and a log line must
    never quote the lecture or the booklet. ``span`` points at the sentence in
    the source that came closest, so an operator can inspect it against the
    stored transcript.
    """

    span: tuple[int, int]
    shared_ratio: float
    #: The exact asserted sentence and artifact path are retained for a
    #: targeted repair request. They are never written to logs.
    claim: str = ""
    section_index: int = -1
    field: str = ""
    risk: str = "medium"


def _claim_sentences(notes: object) -> Iterator[tuple[str, int, str]]:
    """Assertive prose plus exact artifact paths for claim-level repair.

    Short headings and locator labels remain labels, not factual claims. Long
    prose-bearing labels are still inspected along with their displayed fields:
    an invented warning, definition, table caption/cell, tip or global summary
    must not evade the final acceptance gate merely by living outside
    ``paragraphs``.
    """
    payload = _payload_of(notes)
    if not isinstance(payload, dict):
        return

    def sentences(value: object, section_index: int, field: str):
        for sentence in split_sentences(str(value or "")):
            yield sentence, section_index, field

    for field_name in ("title", "summary"):
        yield from sentences(payload.get(field_name), -1, field_name)
    for field_name in ("key_points", "learning_objectives", "review_questions"):
        for item_index, item in enumerate(payload.get(field_name) or ()):
            yield from sentences(item, -1, f"{field_name}[{item_index}]")
    for item_index, item in enumerate(payload.get("glossary") or ()):
        if isinstance(item, dict):
            yield from sentences(item.get("definition"), -1, f"glossary[{item_index}].definition")
    for item_index, item in enumerate(payload.get("practical_tips") or ()):
        if isinstance(item, dict):
            yield from sentences(item.get("text"), -1, f"practical_tips[{item_index}].text")
    for section_index, section in enumerate(payload.get("sections") or ()):
        if not isinstance(section, dict):
            continue
        yield from sentences(section.get("heading"), section_index, f"sections[{section_index}].heading")
        for field_name in ("paragraphs", "bullets", "examples", "steps", "formulas", "key_points"):
            for item_index, item in enumerate(section.get(field_name) or ()):
                yield from sentences(item, section_index, f"sections[{section_index}].{field_name}[{item_index}]")
        for item_index, item in enumerate(section.get("definitions") or ()):
            if isinstance(item, dict):
                yield from sentences(
                    item.get("term"), section_index,
                    f"sections[{section_index}].definitions[{item_index}].term",
                )
                yield from sentences(
                    item.get("definition"), section_index,
                    f"sections[{section_index}].definitions[{item_index}].definition",
                )
        for item_index, item in enumerate(section.get("callouts") or ()):
            if isinstance(item, dict):
                yield from sentences(
                    item.get("text"), section_index,
                    f"sections[{section_index}].callouts[{item_index}].text",
                )
        for table_index, table in enumerate(section.get("tables") or ()):
            if not isinstance(table, dict):
                continue
            yield from sentences(
                table.get("caption"), section_index,
                f"sections[{section_index}].tables[{table_index}].caption",
            )
            for header_index, header in enumerate(table.get("headers") or ()):
                yield from sentences(
                    header, section_index,
                    f"sections[{section_index}].tables[{table_index}].headers[{header_index}]",
                )
            for row_index, row in enumerate(table.get("rows") or ()):
                if isinstance(row, (list, tuple)):
                    for cell_index, cell in enumerate(row):
                        yield from sentences(
                            cell, section_index,
                            f"sections[{section_index}].tables[{table_index}].rows[{row_index}][{cell_index}]",
                        )


def _is_meta_sentence(sentence: str) -> bool:
    folded = normalize_for_compare(sentence)
    return any(
        marker and marker in folded
        for marker in (normalize_for_compare(item) for item in _META_SENTENCE_MARKERS)
    )


def _source_sentence_spans(source: str) -> tuple[tuple[str, tuple[int, int]], ...]:
    """Each source sentence with its character span, in document order."""
    spans: list[tuple[str, tuple[int, int]]] = []
    cursor = 0
    for sentence in split_sentences(source):
        position = source.find(sentence, cursor)
        if position < 0:
            continue
        spans.append((sentence, (position, position + len(sentence))))
        cursor = position + len(sentence)
    return tuple(spans)


def prose_claim_evidence(
    source: str,
    notes: object,
    *,
    minimum_ratio: float = MIN_CLAIM_SUPPORT,
    source_index: SourceEvidenceIndex | None = None,
) -> tuple[ClaimEvidence, ...]:
    """Prose claims the source does not support, with their closest span.

    This is the prose counterpart of :func:`audit_notes`: the signal audit can
    only see numbers, dates, Latin terms and URLs, so an invented Persian-only
    statement used to pass it untouched. Every assertive sentence is compared
    with the source sentences and must share at least ``minimum_ratio`` of its
    content words with one of them. The rule is lexical on purpose — it never
    claims semantic entailment, it only refuses a sentence that has *no*
    vocabulary in common with the lecture it is supposed to summarise.

    Returns only unsupported claims; an empty tuple means every checked
    sentence found source vocabulary.
    """
    if not source:
        return ()
    _ = source_index  # the span needs the raw text, not the folded index
    claims = tuple(_claim_sentences(notes))
    if not claims:
        return ()
    spans = _source_sentence_spans(source)
    if not spans:
        return ()
    prepared = [
        (content_words(sentence), span) for sentence, span in spans
    ]
    prepared = [(words, span) for words, span in prepared if words]
    if not prepared:
        return ()
    unsupported: list[ClaimEvidence] = []
    for claim, section_index, field_path in claims:
        words = content_words(claim)
        if len(words) < MIN_CLAIM_WORDS or _is_meta_sentence(claim):
            continue
        best_ratio = 0.0
        best_span = (0, 0)
        for source_words, span in prepared:
            ratio = len(words & source_words) / len(words)
            if ratio > best_ratio:
                best_ratio = ratio
                best_span = span
                if best_ratio >= minimum_ratio:
                    break
        if best_ratio < minimum_ratio:
            unsupported.append(
                ClaimEvidence(
                    span=best_span,
                    shared_ratio=best_ratio,
                    claim=claim,
                    section_index=section_index,
                    field=field_path,
                    risk=classify_claim_risk(claim),
                )
            )
    return tuple(unsupported)


# ===========================================================================
# Tables
# ===========================================================================

#: A cell of at most this many words may be a label rather than a sentence.
_SHORT_CELL_WORDS = 8
#: A gloss is a Persian explanation of a row's source term (``INSERT`` -> «درج»).
#: It is allowed when the row is anchored in the source and the gloss is short.
_MAX_GLOSS_WORDS = 3


def _cell_grounded(cell: str, source_words: frozenset[str], source_text: str) -> bool:
    """A cell is grounded when its content traces to the source.

    Short labels may be re-ordered or re-cased; longer cells must share their
    content words with the source. Latin tokens are exempt as standard
    equivalents, but a long all-Latin cell is prose (or an invented sentence)
    and is not exempt. Numbers are checked by :func:`validate_table` first.
    """
    text = (cell or "").strip()
    if not text:
        return True
    folded = normalize_for_compare(text)
    if folded and folded in source_text:
        return True
    latin = {token.casefold() for token in _LATIN_TOKEN.findall(text)}
    words = content_words(text) - latin
    if not words:
        return len(latin) <= _MAX_EQUIVALENT_WORDS
    shared = len(words & source_words)
    if len(words) <= _SHORT_CELL_WORDS:
        return shared / len(words) >= 0.75
    return shared / len(words) >= 0.6


def validate_table(
    table: object,
    source: str,
    *,
    source_index: SourceEvidenceIndex | None = None,
) -> TableVerdict:
    """Validate one proposed table: numbers hard, text soft.

    * a numeric cell must exist in the source as the same value (unit-aware);
    * a text cell must trace its content words to the source;
    * any numeric failure rejects the table, because an invented value in a
      table is exactly the failure this check exists for;
    * more than a quarter of failed text cells rejects it too.

    A rejected table is dropped, never "fixed": its values become missing
    signals for the repair pass.
    """
    headers = [str(header) for header in getattr(table, "headers", ()) or ()]
    rows = [[str(cell) for cell in row] for row in getattr(table, "rows", ()) or ()]
    if not headers or not rows:
        return TableVerdict(False, reason="empty")
    if len(headers) > 8 or len(rows) > 40:
        return TableVerdict(False, reason="size")
    if len(headers) < 2 or len(rows) < 2:
        return TableVerdict(False, reason="size")

    idx = source_index if source_index is not None else build_source_index(source)
    source_number_lookup = idx.number_lookup
    source_words = idx.masked_words
    source_text = idx.compare_masked

    numeric_total = 0
    numeric_failed: list[str] = []
    text_total = 0
    text_failed: list[str] = []
    for row in rows:
        # A row anchored in the source may gloss its own term: ``INSERT`` with
        # the Persian meaning «درج» is an equivalence, not new knowledge. The
        # gloss must be short and the row must contain a grounded cell.
        row_anchored = any(
            _cell_grounded(str(cell or ""), source_words, source_text)
            and bool(content_words(str(cell or "")))
            for cell in row
        )
        for cell in row:
            numbers = _numbers(extract_signals(cell, mask=False))
            if numbers:
                numeric_total += len(numbers)
                for signal in numbers:
                    if not _covers_lookup(signal, source_number_lookup):
                        numeric_failed.append(signal.label)
                continue
            text_total += 1
            if _cell_grounded(cell, source_words, source_text):
                continue
            if row_anchored and len(content_words(cell)) <= _MAX_GLOSS_WORDS:
                continue
            text_failed.append(cell)
    for header in headers:
        for signal in _numbers(extract_signals(header, mask=False)):
            numeric_total += 1
            if not _covers_lookup(signal, source_number_lookup):
                numeric_failed.append(signal.label)

    if numeric_failed:
        return TableVerdict(
            False, numeric_total, tuple(numeric_failed), text_total, tuple(text_failed), "number"
        )
    if text_total and len(text_failed) / text_total > 0.25:
        return TableVerdict(False, numeric_total, (), text_total, tuple(text_failed), "text")
    return TableVerdict(True, numeric_total, (), text_total, ())


# ===========================================================================
# Practical tips
# ===========================================================================

#: A basis shorter than this is not evidence; it is a word.
MIN_TIP_BASIS_WORDS = 5
#: A tip longer than this is advice the model wrote, not advice it surfaced.
MAX_TIP_CHARS = 400


def validate_tip(
    tip: object,
    source: str,
    *,
    source_index: SourceEvidenceIndex | None = None,
) -> bool:
    """A tip is valid only when its ``basis`` is verifiably in the source.

    The check accepts a verbatim substring (after normalisation) or a sentence
    with 85% of the basis's content words — ASR mangles a word here and there,
    and a legitimate quote must survive that.
    """
    text = str(getattr(tip, "text", "") or "").strip()
    basis = str(getattr(tip, "basis", "") or "").strip()
    if not text or not basis:
        return False
    if len(basis.split()) < MIN_TIP_BASIS_WORDS:
        return False
    if len(text) > MAX_TIP_CHARS:
        return False
    idx = source_index if source_index is not None else build_source_index(source)
    folded_source = idx.compare_source
    folded_basis = normalize_for_compare(basis)
    if folded_basis and folded_basis in folded_source:
        return True
    basis_words = content_words(basis)
    if not basis_words:
        return False
    for _sentence, words in idx.raw_sentences:
        if not words:
            continue
        if len(basis_words & words) / len(basis_words) >= 0.85:
            return True
    return False


#: Advice markers a practical tip must be supported by (the lecturer's words).
_ADVICE_MARKERS = (
    "باید", "نباید", "حتما", "حتماً", "توصیه", "دقت کنید", "توجه کنید",
    "مراقب", "خطر", "هشدار", "در آزمون", "امتحان", "پرهیز", "اجتناب",
    "recommend", "should", "must", "avoid", "caution", "warning",
)


def advice_sentences(source: str) -> tuple[str, ...]:
    """Sentences in which the lecturer gives advice, a warning or exam emphasis.

    The list is the *candidate pool* for practical tips and the hint the repair
    pass receives. If it is empty, the correct output has no tips.
    """
    found: list[str] = []
    for sentence in split_sentences(source):
        lowered = sentence.casefold()
        if any(marker.casefold() in lowered for marker in _ADVICE_MARKERS):
            found.append(" ".join(sentence.split()))
    return tuple(found)


# ===========================================================================
# Report
# ===========================================================================


@dataclass(frozen=True, slots=True)
class NoteQualityReport:
    """Everything the pipeline (and the quality log) needs to know."""

    audit: SignalAudit
    coverage: CoverageReport
    tables_seen: int = 0
    tables_accepted: int = 0
    tables_rejected: int = 0
    tips_seen: int = 0
    tips_accepted: int = 0
    tips_rejected: int = 0
    objectives_dropped: int = 0
    review_questions_dropped: int = 0
    #: Locators absent from the source were removed by the strict gate.
    locators_rejected: int = 0
    dropped_cells: tuple[str, ...] = field(default_factory=tuple)
    #: Prose claims with no supporting source sentence (see
    #: :func:`prose_claim_evidence`). Values stay in memory solely to build a
    #: bounded repair command and are never emitted in logs.
    unsupported_claims: tuple[ClaimEvidence, ...] = field(default_factory=tuple)

    @property
    def passes(self) -> bool:
        return self.audit.grounded and not self.unsupported_claims

    def summary(self) -> str:
        return (
            self.audit.summary()
            + f" sentence_coverage={self.coverage.ratio:.2f}"
            + f" tables={self.tables_accepted}/{self.tables_seen}"
            + f" tips={self.tips_accepted}/{self.tips_seen}"
            + f" objectives_dropped={self.objectives_dropped}"
            + f" review_questions_dropped={self.review_questions_dropped}"
            + f" locators_rejected={self.locators_rejected}"
            + f" unsupported_claims={len(self.unsupported_claims)}"
        )
