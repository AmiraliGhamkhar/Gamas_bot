"""Deterministic content-preservation QA for generated lecture notes.

The checker compares the *text of the merged notes* against the source chunks
with cheap regular-expression matching — no embeddings, no second LLM pass.
Its job is to make information loss *visible and loggable*, never to rewrite
content: the notes are returned unchanged, and the report is attached to the
job log so a misbehaving prompt or model shows up in operations.

What it detects:

* numbers with optional units/dosages present in a source chunk but missing
  from the notes (5 mg, 50 mg, 500 mg, 5 mL, 0.5 mL, SpO2 95%, BP 120/80,
  HR 80 bpm, mmHg, mg/dL, mcg, μg, kg/m², °C, %, ...);
* English technical/medical terms (length >= 3 letters) that disappeared;
* per-chunk section coverage (a chunk producing no section at all).

It never invents or restores information, and it never blocks delivery: the
worst outcome is a WARNING line in the log with a bounded count of findings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # structural import only; avoids an import cycle at runtime
    from .structuring import StructuredNotes

logger = logging.getLogger(__name__)

#: How many findings are listed per category in one log record.
MAX_REPORTED_FINDINGS = 8

# ---------------------------------------------------------------------------
# Extraction patterns
# ---------------------------------------------------------------------------

#: Numbers optionally followed by a unit. Persian and Latin digits both match;
#: decimal comma/point and thousands separators are handled.
_NUMBER = r"(?:\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)"

#: Unit spellings that commonly ride along with lecture numbers. Latin only:
#: units are technical tokens the model must keep verbatim.
_UNITS = (
    r"mg|mcg|μg|ug|mL|L\b|g\b|kg\b|mmol|mol\b|mmHg|kPa|bpm|b\.p\.m\.|meq|mEq|iu|IU"
    r"|mg/dL|mg/dl|g/dL|mmol/L|ng/mL|pg/mL|μmol|kg/m²|kg/m2|cm\b|mm\b|m²|m2|km/h"
    r"|°C|°F|kcal|kJ|Hz\b|kHz\b|MHz\b|ms\b|s\b|min\b|h\b|%\b|%|درصد|میلی‌گرم|میلی‌لیتر"
    r"|میلی‌مول|کیلوگرم|سانتی‌متر|میلی‌متر جیوه|میلی‌متر"
)

#: Persian unit spellings mapped onto their canonical Latin symbol.  The keys
#: are compared *after* dropping spaces and ZWNJ, so ``میلی گرم``,
#: ``میلی‌گرم`` and ``میلیگرم`` all resolve to the same key.  Without this a
#: Persian transcript ("۵۰۰ میلی‌گرم") would look "missing" from notes that
#: correctly preserved it as "500 mg".
_UNIT_ALIASES = {
    "میلیگرم": "mg",
    "میلیلیتر": "ml",
    "میلیمول": "mmol",
    "میکروگرم": "mcg",
    "میکرولیتر": "µl",
    "کیلوگرم": "kg",
    "گرم": "g",
    "سانتیمتر": "cm",
    "میلیمتر": "mm",
    "میلیمترجیوه": "mmhg",
    "متر": "m",
    "لیتر": "l",
    "درجه": "°c",
    "درصد": "%",
    "دقیقه": "min",
    "ساعت": "h",
}

#: Arabic/Persian letter range used to decide whether a passage is a Persian
#: lecture at all (see :func:`_extract_terms`).
#: URLs are stripped before term extraction: "https", "python" or "tutorial"
#: inside https://docs.python.org/3/tutorial are address fragments, not
#: terminology the notes are expected to repeat.
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

_RTL_RANGE = re.compile(
    "[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]"
)

_NUMBER_WITH_UNIT = re.compile(
    rf"(?<![A-Za-z0-9])({_NUMBER})\s*({_UNITS})(?![A-Za-z0-9])", re.IGNORECASE
)

#: Bare percentages ("95%", "۹۵ درصد") — the % may sit before the number in
#: Persian typography (٪۹۵) as well as after it.
_PERCENT = re.compile(
    rf"(?<![A-Za-z0-9])({_NUMBER})\s*(?:%|٪)|(?:%|٪)\s*({_NUMBER})(?![A-Za-z0-9])"
    rf"|\b({_NUMBER})\s+درصد"
)

#: Blood-pressure pairs ("BP 120/80", "120/80 mmHg").
_BP_PAIR = re.compile(r"(?<![\d/])(\d{2,3})/(\d{2,3})(?![\d/])")

#: English technical/medical words and abbreviations, minimum 3 letters so
#: stopword noise ("and", "the", "was") stays out. Acronyms of any length that
#: contain a digit or are all-caps (HbA1c, MRI, SpO2, AKI) also match.
_ENGLISH_TERM = re.compile(
    r"(?<![A-Za-z])(?:(?=[A-Za-z]*\d)[A-Za-z]{2,}[0-9][A-Za-z0-9]*"
    r"|[A-Z]{2,}[a-z0-9]*|[A-Za-z]{4,})(?![A-Za-z])"
)

#: Tiny English function words that are noise even inside Persian sentences.
_TERM_STOPWORDS = frozenset(
    {
        "that", "this", "with", "from", "have", "has", "had", "was", "were",
        "are", "will", "would", "there", "their", "then", "than", "when",
        "which", "what", "about", "into", "also", "some", "such", "each",
        "them", "they", "been", "being", "very", "more", "most", "only",
        "over", "after", "before", "between", "both", "other", "these",
        "those", "your", "you", "our", "can", "may", "not", "but", "and",
        "for", "the", "its", "his", "her", "him", "she", "who", "why", "how",
        "all", "any", "one", "two", "per", "via", "due", "use", "used",
        "using", "new", "own", "same", "so", "if", "it", "is", "as", "at",
        "by", "an", "or", "we", "he", "do", "does", "did", "done", "get",
        "got", "make", "made", "take", "taken", "see", "seen", "well", "way",
        "part", "like", "just", "now", "even", "here", "still", "much",
        "many", "each", "every", "next", "last", "first", "second", "third",
    }
)


@dataclass(frozen=True, slots=True)
class SourceSignal:
    """Numbers/terms extracted from one source chunk."""

    chunk_index: int
    numbers: frozenset[str]
    terms: frozenset[str]


@dataclass(frozen=True, slots=True)
class NoteQAReport:
    """Deterministic coverage report; findings are informational only."""

    source_numbers: int = 0
    preserved_numbers: int = 0
    source_terms: int = 0
    preserved_terms: int = 0
    missing_numbers: tuple[str, ...] = ()
    missing_terms: tuple[str, ...] = ()
    uncovered_chunks: tuple[int, ...] = ()
    notes_text_chars: int = 0
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings or self.uncovered_chunks)


def _normalize_digits(value: str) -> str:
    """Persian/Arabic digits -> Latin so comparisons are script-independent."""
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return value.translate(table)


def _canon_number(value: str) -> str:
    """Canonical form for comparisons: Latin digits, no thousands separators."""
    value = _normalize_digits(value).replace(",", "")
    return value.rstrip(".").rstrip(",")


def _canon_unit(unit: str) -> str:
    """Canonical unit symbol: lowercase, alias-resolved, whitespace-free.

    ``500 mg``, ``۵۰۰ میلی‌گرم`` and ``۵۰۰ میلی گرم`` therefore all collapse to
    the token ``500 mg`` and compare equal.
    """
    key = re.sub("[\\s\u200c]", "", unit).casefold()
    return _UNIT_ALIASES.get(key, key)


def _extract_numbers(text: str) -> set[str]:
    """Numbers-with-units, percentages and BP pairs, canonically."""
    found: set[str] = set()
    normalized = _normalize_digits(text)
    for match in _NUMBER_WITH_UNIT.finditer(normalized):
        number, unit = match.group(1), match.group(2)
        found.add(f"{_canon_number(number)} {_canon_unit(unit)}")
    for match in _PERCENT.finditer(normalized):
        number = match.group(1) or match.group(2) or match.group(3)
        if number:
            found.add(f"{_canon_number(number)} %")
    for match in _BP_PAIR.finditer(normalized):
        found.add(f"{match.group(1)}/{match.group(2)}")
    return found


def _extract_terms(text: str) -> set[str]:
    """English technical terms/abbreviations, case-preserved for reporting.

    Ordinary lowercase English words are only counted inside a Persian passage
    (an inline technical term such as "Metformin" in a Persian lecture).  A
    chunk that is pure English prose is an STT artefact — or an English lecture
    — and its common words would drown the real signals, so only acronyms
    (all-caps, or mixed-case with digits such as ``HbA1c``) are kept there.
    """
    persian_context = bool(_RTL_RANGE.search(text))
    found: set[str] = set()
    for match in _ENGLISH_TERM.finditer(_URL.sub(" ", text)):
        term = match.group(0)
        if len(term) < 3:
            continue
        if term.casefold() in _TERM_STOPWORDS:
            continue
        if term.islower():
            # Lowercase: ordinary prose word unless it is real jargon (5+)
            # inside a Persian sentence.
            if not persian_context or len(term) < 5:
                continue
        found.add(term)
    return found


def notes_text(notes: StructuredNotes) -> str:
    """Every user-visible string of the notes, joined for QA comparisons."""
    parts: list[str] = [notes.title, notes.summary]
    for section in notes.sections:
        parts.append(section.heading)
        parts.extend(section.paragraphs)
        parts.extend(section.bullets)
        for definition in section.definitions:
            parts.append(definition.term)
            parts.append(definition.term_en)
            parts.append(definition.definition)
        parts.extend(section.examples)
        parts.extend(section.steps)
        parts.extend(section.formulas)
        parts.extend(section.key_points)
        if section.table is not None:
            parts.extend(section.table.headers)
            for row in section.table.rows:
                parts.extend(row)
        for callout in section.callouts:
            parts.append(callout.text)
    parts.extend(notes.key_points)
    for entry in notes.glossary:
        parts.append(entry.term)
        parts.append(entry.definition)
    return "\n".join(part for part in parts if part)


def run_note_qa(notes: StructuredNotes, source_chunks: list[str]) -> NoteQAReport:
    """Compare the notes against their source chunks deterministically.

    Purely observational: the notes are never modified and the return value
    is only used for logging/metrics.
    """
    rendered = notes_text(notes)
    rendered_numbers = _extract_numbers(rendered)
    rendered_terms = _extract_terms(rendered)
    rendered_terms_folded = {term.casefold() for term in rendered_terms}

    missing_numbers: set[str] = set()
    missing_terms: set[str] = set()
    uncovered: list[int] = []
    source_numbers: set[str] = set()
    source_terms: set[str] = set()

    for index, chunk in enumerate(source_chunks, start=1):
        chunk_numbers = _extract_numbers(chunk)
        chunk_terms = _extract_terms(chunk)
        source_numbers |= chunk_numbers
        source_terms |= chunk_terms
        if not chunk_numbers and not chunk_terms:
            continue
        missing_here_numbers = chunk_numbers - rendered_numbers
        missing_here_terms = {
            term for term in chunk_terms if term.casefold() not in rendered_terms_folded
        }
        missing_numbers |= missing_here_numbers
        missing_terms |= missing_here_terms
        # A chunk whose every number *and* term is absent from the notes is
        # likely under-covered (or genuinely filler); report it once.
        if chunk_numbers and missing_here_numbers == chunk_numbers and (
            not chunk_terms or missing_here_terms == chunk_terms
        ):
            uncovered.append(index)

    report = NoteQAReport(
        source_numbers=len(source_numbers),
        preserved_numbers=len(source_numbers - missing_numbers),
        source_terms=len(source_terms),
        preserved_terms=len(source_terms - missing_terms),
        missing_numbers=tuple(sorted(missing_numbers)[:MAX_REPORTED_FINDINGS]),
        missing_terms=tuple(sorted(missing_terms)[:MAX_REPORTED_FINDINGS]),
        uncovered_chunks=tuple(uncovered[:MAX_REPORTED_FINDINGS]),
        notes_text_chars=len(rendered),
    )

    findings: list[str] = []
    if report.missing_numbers:
        findings.append(
            "numbers missing from notes: " + ", ".join(report.missing_numbers)
        )
    if report.missing_terms:
        findings.append(
            "terms missing from notes: " + ", ".join(report.missing_terms)
        )
    if report.uncovered_chunks:
        findings.append(
            "chunks with no preserved signal: "
            + ", ".join(str(index) for index in report.uncovered_chunks)
        )
    report = NoteQAReport(
        source_numbers=report.source_numbers,
        preserved_numbers=report.preserved_numbers,
        source_terms=report.source_terms,
        preserved_terms=report.preserved_terms,
        missing_numbers=report.missing_numbers,
        missing_terms=report.missing_terms,
        uncovered_chunks=report.uncovered_chunks,
        notes_text_chars=report.notes_text_chars,
        findings=tuple(findings),
    )

    if report.has_findings:
        logger.warning(
            "Note QA coverage gaps (informational; notes delivered unchanged): %s",
            " | ".join(findings),
        )
    else:
        logger.info("Note QA coverage complete: no gaps detected")
    return report
