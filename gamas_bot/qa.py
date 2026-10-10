"""Deterministic content-preservation QA for generated lecture notes.

The checker compares the *text of the merged notes* against the source chunks
with cheap regular-expression matching plus the content-unit layer in
:mod:`gamas_bot.units` — no embeddings and no vector store. Its job is to make
information loss *visible and loggable*, never to rewrite content: the notes
are returned unchanged, and the report is attached to the job log so a
misbehaving prompt or model shows up in operations.

What it detects:

* numbers with optional units/dosages present in a source chunk but missing
  from the notes (5 mg, 50 mg, 500 mg, 5 mL, 0.5 mL, SpO2 95%, BP 120/80,
  HR 80 bpm, mmHg, mg/dL, mcg, μg, kg/m², °C, %, ...);
* English technical/medical terms (length >= 3 letters) that disappeared;
* per-chunk section coverage (a chunk producing no section at all);
* **semantic completeness** — the fraction of the source's educational
  content units (definitions, examples, procedures, warnings, comparisons,
  explanations) that survived. Signal-level checks alone are blind to the
  worst failure mode: a document can keep every number and term while
  deleting the explanation that made them meaningful;
* **added content** — numbers with units/percentages/BP pairs, and strong
  technical tokens (acronyms, digit-bearing terms such as ``HbA1c``), that the
  notes state but no source chunk contains. This is the deterministic
  fingerprint of a *hallucinated* fact (a fabricated dosage, lab value or
  statistic). The check is deliberately conservative — a token is only
  "unsupported" when it is absent from the source *and* shares no digit run
  with it — so a formatting difference (``۱۲۰/۸۰`` vs ``120/80``) is never
  mistaken for an invention;
* length/compression facts, reported as evidence and deliberately *not*
  treated as a failure on their own, because a lecture can legitimately be
  tightened a long way without losing information.

Note: a repair pass (a second, targeted provider call) exists in
:mod:`gamas_bot.structuring` and is gated on this report. It is off by default
when coverage is healthy, so the normal path is one call per chunk.

It never invents or restores information, and it never blocks delivery: the
worst outcome is a WARNING line in the log with a bounded count of findings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # structural import only; avoids an import cycle at runtime
    from .structuring import StructuredNotes

from .textnorm import normalize_digits, normalize_for_compare

logger = logging.getLogger(__name__)

#: How many findings are listed per category in one log record.
MAX_REPORTED_FINDINGS = 8

#: Structural diagnostics. They are *observational*: a repeated heading or a
#: bullet-only section is reported, never silently rewritten or deleted, and
#: they never gate delivery. Repair still reacts only to real content loss.
#: A section shorter than this (visible characters) is "excessively short".
MIN_SECTION_CHARS = 120
#: A section that is only a list is "fragmented prose" at this many bullets.
MIN_BULLETS_FOR_FRAGMENT = 3
#: A heading longer than this, or one that ends with sentence punctuation, is
#: not a heading in the academic sense: it is a sentence that was promoted.
MAX_HEADING_CHARS = 80
#: Minimum number of shared content words before two headings can be judged to
#: describe one topic. Two shared words is the smallest honest signal; below it
#: ("مقدمه" vs "نتیجه") any match is coincidence.
MIN_SHARED_TOPIC_WORDS = 2

#: Continuation markers. Lecture notes of a split topic are titled in practice
#: as «ادامهٔ …» / «(ادامه)» / «… — continued», and a merge that does not
#: recognise them prints one topic as two sections.
_CONTINUATION_PREFIX = re.compile(
    r"^(?:\(?\s*(?:ادامه|دنباله|تکمله|بخش\s+(?:بعد|دوم|۲|2)|part\s*(?:2|ii)|cont(?:inued)?)\s*\)?"
    r"[\s.:،\-—»]*)+",
    re.IGNORECASE,
)
_CONTINUATION_SUFFIX = re.compile(
    r"[\s(«\[:،\-—]*(?:ادامه|دنباله|تکمله|بخش\s+بعد|continued|cont\.?|part\s+(?:2|ii))[\s)»\]:،\-—.]*$",
    re.IGNORECASE,
)

#: The heading :meth:`gamas_bot.structuring.NoteSection.from_payload` generates
#: when a model returns content *without* a heading. Seeing it in a finished
#: booklet means the provider dropped a heading, which is a structural defect
#: worth reporting (it is not something the merge can invent back).
_PLACEHOLDER_HEADING = re.compile(r"^\s*(?:بخش|فصل|قسمت)\s*[\d۰-۹]+\s*$")

#: Relations a lecturer uses to link a paragraph to what came before it. A
#: section that opens with none of them and reuses no vocabulary from the
#: previous section opens a subject of its own: reported, never rewritten.
_RELATION_MARKERS = re.compile(
    r"(?:چون|زیرا|بنابراین|در\s+نتیجه|از\s+این\s+رو|به\s+همین\s+دلیل|به\s+این\s+ترتیب|"
    r"اما|ولی|در\s+مقابل|برخلاف|با\s+این\s+حال|"
    r"برای\s+نمونه|برای\s+مثال|به\s+عنوان\s+مثال|مانند|"
    r"نخست|نخستین|اول|سپس|پس\s+از\s+آن|در\s+پایان|در\s+نهایت|"
    r"همچنین|در\s+ادامه|همان\s+طور\s+که|"
    r"because|therefore|however|in\s+contrast|for\s+example|first|then|finally)",
    re.IGNORECASE,
)

#: Words that carry no topical identity when two paragraphs are compared, so
#: sharing them cannot count as "this opening links to what came before".
_LINK_STOPWORDS = frozenset(
    {
        "است", "هست", "هستند", "شود", "شوند", "شد", "شده", "بود", "بوده", "باشند",
        "نیز", "را", "مورد", "حال", "طور", "بسیار", "خوب", "کم", "زیاد", "دیگر",
        "روی", "بین", "زیر", "کار", "بخش", "درس", "متن", "کند", "کنیم", "دارد",
        "دارند", "دهیم", "گیرد", "می", "های", "این", "آن", "یک", "هم",
        "the", "and", "for", "with", "that", "this", "are", "was", "were", "its",
    }
)

#: Function words that carry no topical identity inside a heading.
_HEADING_STOPWORDS = frozenset(
    {
        "و", "در", "به", "از", "با", "بر", "برای", "که", "این", "آن", "های",
        "ها", "یک", "یا", "تا", "را", "هم", "می", "شود", "است", "the", "of",
        "and", "a", "an", "to", "in", "on", "for", "with", "part", "section",
    }
)

#: Thresholds for the optional repair pass and for the "aggressive compression"
#: finding. The repair pass costs an extra provider call, so it must fire only
#: on unambiguous information loss.
#:
#: ``MIN_COVERAGE`` is the fraction of source numbers+terms that must survive.
#: ``MIN_SEMANTIC_COVERAGE`` is the fraction of educational *content units*
#: (definitions, examples, procedures, …) that must survive; this is the
#: signal that a whole explanation was deleted rather than a number.
#: ``MIN_RATIO`` is the smallest acceptable notes/source character ratio. A
#: low ratio is only *evidence*, never a failure by itself — see
#: :attr:`NoteQAReport.compression_is_concerning`, which additionally requires
#: poor semantic coverage before it complains.
MIN_COVERAGE = 0.75
MIN_SEMANTIC_COVERAGE = 0.70
MIN_RATIO = 0.10
MIN_RATIO_SOURCE_CHARS = 1000

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
        "many", "every", "next", "last", "first", "second", "third",
    }
)


@dataclass(frozen=True, slots=True)
class SourceSignal:
    """Numbers/terms extracted from one source chunk."""

    chunk_index: int
    numbers: frozenset[str]
    terms: frozenset[str]


@dataclass(frozen=True, slots=True)
class NoteStructureReport:
    """Deterministic structure/coherence diagnostics for one note document.

    Everything here is a *signal for the operator* (log line, benchmark
    column): duplicates, repeated headings, empty or fragmentary sections and
    a summary that repeats the body. Nothing in this report deletes content —
    the conservative merge in :mod:`gamas_bot.structuring` owns the only
    automatic de-duplication, and it removes verbatim repeats only.
    """

    total_sections: int = 0
    empty_sections: int = 0
    short_sections: int = 0
    bullet_only_sections: int = 0
    duplicate_paragraphs: int = 0
    duplicate_bullets: int = 0
    repeated_headings: int = 0
    duplicate_key_points: int = 0
    repeated_summary_sentences: int = 0
    #: Adjacent sections whose headings describe one topic: the signature of a
    #: lecture topic that a chunk boundary (or a model) split in two.
    split_topics: int = 0
    #: Sections with content but no prose paragraph — everything was bulleted.
    sections_without_paragraphs: int = 0
    #: Headings that are sentences rather than labels (too long / punctuated).
    sentence_headings: int = 0
    #: Sections whose heading is the placeholder the parser generates when the
    #: model returned content without a heading ("بخش ۳").
    untitled_sections: int = 0
    #: Sections (after the first) whose opening paragraph carries no relation
    #: to what came before. A weak style signal: a section may legitimately
    #: start a subject of its own.
    abrupt_sections: int = 0
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_clean(self) -> bool:
        return not self.findings


def _compare_key(value: str) -> str:
    """Whitespace/Persian-variant-insensitive identity for duplicate detection."""
    return re.sub(r"\s+", " ", normalize_for_compare(value)).strip()


def heading_topic_key(heading: str) -> str:
    """Topic identity of a heading, with continuation markers removed.

    «مقدمه», «ادامهٔ مقدمه», «مقدمه (ادامه)» and «مقدمه — continued» are the
    same topic written four ways. Both the merge (:mod:`gamas_bot.structuring`)
    and the diagnostics below need exactly one definition of that identity, so
    it lives here and is imported, never re-implemented.
    """
    key = _compare_key(heading)
    if not key:
        return ""
    previous = None
    while previous != key:
        previous = key
        key = _CONTINUATION_PREFIX.sub("", key)
        key = _CONTINUATION_SUFFIX.sub("", key)
        key = key.strip(" .:،-—»«()[]")
    return key


def heading_topic_words(heading: str) -> frozenset[str]:
    """Content words of a heading, for the conservative topic comparison.

    Numbers are *kept*: «صورت اول» and «صورت دوم», or «مرحله ۱» and «مرحله ۲»,
    are different sections whose only difference is a digit, and dropping the
    digit would make them look identical.
    """
    key = heading_topic_key(heading) or _compare_key(heading)
    words = re.findall(r"[^\W_]+", key, re.UNICODE)
    return frozenset(
        word
        for word in words
        if word not in _HEADING_STOPWORDS and (len(word) > 1 or word.isdigit())
    )


def headings_share_a_topic(first: str, second: str, *, minimum: int = MIN_SHARED_TOPIC_WORDS) -> bool:
    """Do two headings clearly describe one topic?

    Deliberately conservative, because both callers may act on a ``True``:

    * the same topic identity (continuation markers removed) is enough;
    * identical content-word sets are enough (word order only);
    * otherwise one heading's words must *contain* the other's and share at
      least ``minimum`` words — «نرمال‌سازی و صورت سوم» contains «نرمال‌سازی»,
      while «کلید خارجی» and «ایندکس» share nothing and never match.
    """
    key_a = heading_topic_key(first)
    key_b = heading_topic_key(second)
    if key_a and key_a == key_b:
        return True
    words_a = heading_topic_words(first)
    words_b = heading_topic_words(second)
    if not words_a or not words_b:
        return False
    if words_a == words_b:
        return True
    shared = words_a & words_b
    if len(shared) < minimum:
        return False
    return words_a <= words_b or words_b <= words_a


def headings_overlap(first: str, second: str, *, threshold: float = 0.5) -> bool:
    """A looser topic test, used *only* across a chunk boundary.

    A chunk boundary is the one place where a model provably splits a single
    topic: its last section and the next part's first section are almost never
    two different subjects. The threshold is a Jaccard ratio over content
    words, still requiring two shared words, so a boundary can merge a split
    topic but never two unrelated ones («کلید و رابطه» vs «ایندکس» is 0.0).
    """
    words_a = heading_topic_words(first)
    words_b = heading_topic_words(second)
    if not words_a or not words_b:
        return False
    shared = words_a & words_b
    if len(shared) < MIN_SHARED_TOPIC_WORDS:
        return False
    union = words_a | words_b
    return len(shared) / len(union) >= threshold


#: Tokenizer for the link test: Unicode letters only (Persian and Latin alike),
#: at least three characters. ZWNJ is removed first so «جمع‌بندی» is one word
#: rather than two halves.
_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_ZWNJ = re.compile(r"[\u200c\u200d]")


def _content_words(text: str) -> frozenset[str]:
    """Topical words of a text: no function words, no short tokens."""
    cleaned = _ZWNJ.sub("", text or "")
    return frozenset(
        word.casefold()
        for word in _WORD.findall(cleaned)
        if word.casefold() not in _LINK_STOPWORDS
    )


def _opens_with_a_link(paragraph: str, previous_text: str) -> bool:
    """Does this opening paragraph connect to the text before it?

    Two honest ways to connect: a relation word («چون»، «اما»، «برای نمونه»), or
    shared vocabulary with the previous section. A section that does neither may
    be a legitimate new subject — which is why this is a *reported* diagnostic
    and never a reason to change a single word.
    """
    if _RELATION_MARKERS.search(paragraph or ""):
        return True
    return bool(_content_words(paragraph) & _content_words(previous_text))


def _heading_looks_like_a_sentence(heading: str) -> bool:
    """A heading is a label; a full sentence with a terminator is not."""
    text = _compare_key(heading)
    if not text:
        return False
    if len(text) > MAX_HEADING_CHARS:
        return True
    return text.rstrip().endswith((".", "؟", "?", "!", "…"))


def _section_paragraphs(section) -> int:
    return len(getattr(section, "paragraphs", ()) or ())


def _sections_of(notes) -> list:
    sections = getattr(notes, "sections", ())
    return list(sections) if sections else []


def section_text(section) -> str:
    """Every user-visible string of one section, joined."""
    parts: list[str] = [section.heading]
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
    return "\n".join(part for part in parts if part)


def analyze_structure(notes) -> NoteStructureReport:
    """Count structural defects (duplicates, stubs, fragmentation, repeats)."""
    sections = _sections_of(notes)
    empty_sections = 0
    short_sections = 0
    bullet_only = 0
    duplicate_paragraphs = 0
    duplicate_bullets = 0
    repeated_headings = 0
    duplicate_key_points = 0
    split_topics = 0
    sections_without_paragraphs = 0
    sentence_headings = 0
    untitled_sections = 0
    abrupt_sections = 0

    seen_headings: set[str] = set()
    seen_paragraphs: set[str] = set()
    seen_bullets: set[str] = set()
    seen_points: set[str] = set()
    previous_heading = ""
    previous_body = ""

    for section in sections:
        if not section.has_content:
            empty_sections += 1
            continue
        visible = section_text(section)
        body_length = len(visible) - len(section.heading)
        if body_length < MIN_SECTION_CHARS:
            short_sections += 1
        prose_blocks = (
            len(section.paragraphs)
            + len(section.definitions)
            + len(section.examples)
            + len(section.steps)
            + len(section.formulas)
        )
        if not section.paragraphs and not prose_blocks and len(section.bullets) >= MIN_BULLETS_FOR_FRAGMENT:
            bullet_only += 1
        if not _section_paragraphs(section):
            sections_without_paragraphs += 1
        if _heading_looks_like_a_sentence(section.heading):
            sentence_headings += 1
        if _PLACEHOLDER_HEADING.match(section.heading or ""):
            untitled_sections += 1
        if previous_body and section.paragraphs:
            if not _opens_with_a_link(section.paragraphs[0], previous_body):
                abrupt_sections += 1
        heading_key = _compare_key(section.heading)
        if heading_key:
            if heading_key in seen_headings:
                repeated_headings += 1
            seen_headings.add(heading_key)
        # A split topic is measured on the *topic identity*, so an exact repeat
        # is not double-counted: the heading was already reported as repeated.
        if (
            previous_heading
            and headings_share_a_topic(previous_heading, section.heading)
            and _compare_key(previous_heading) != _compare_key(section.heading)
        ):
            split_topics += 1
        previous_heading = section.heading
        previous_body = visible
        for paragraph in section.paragraphs:
            key = _compare_key(paragraph)
            if key in seen_paragraphs:
                duplicate_paragraphs += 1
            seen_paragraphs.add(key)
        for bullet in section.bullets:
            key = _compare_key(bullet)
            if key in seen_bullets:
                duplicate_bullets += 1
            seen_bullets.add(key)
        for point in section.key_points:
            key = _compare_key(point)
            if key in seen_points:
                duplicate_key_points += 1
            seen_points.add(key)

    for point in getattr(notes, "key_points", ()) or ():
        key = _compare_key(point)
        if key in seen_points:
            duplicate_key_points += 1
        seen_points.add(key)

    # A document-level summary that repeats its own sentences, or that repeats
    # a paragraph of the body, is the classic symptom of "each chunk wrote its
    # own summary" and is worth reporting.
    summary = getattr(notes, "summary", "") or ""
    body_keys = seen_paragraphs | seen_bullets
    repeated_summary = 0
    summary_seen: set[str] = set()
    if summary:
        from .units import split_sentences

        for sentence in split_sentences(summary):
            key = _compare_key(sentence)
            if not key:
                continue
            if key in summary_seen or key in body_keys:
                repeated_summary += 1
            summary_seen.add(key)

    findings: list[str] = []
    if repeated_headings:
        findings.append(f"repeated section headings: {repeated_headings}")
    if split_topics:
        findings.append(
            f"adjacent sections about one split topic: {split_topics}"
        )
    if duplicate_paragraphs:
        findings.append(f"duplicate paragraphs: {duplicate_paragraphs}")
    if duplicate_bullets:
        findings.append(f"duplicate bullets: {duplicate_bullets}")
    if duplicate_key_points:
        findings.append(f"duplicate key points: {duplicate_key_points}")
    if empty_sections:
        findings.append(f"empty sections: {empty_sections}")
    if short_sections:
        findings.append(f"very short sections: {short_sections}")
    if sections_without_paragraphs:
        findings.append(
            f"sections without a prose paragraph: {sections_without_paragraphs}"
        )
    if bullet_only:
        findings.append(f"fragmented sections (bullet-only): {bullet_only}")
    if sentence_headings:
        findings.append(f"headings written as sentences: {sentence_headings}")
    if untitled_sections:
        findings.append(f"sections the model left untitled: {untitled_sections}")
    if abrupt_sections:
        findings.append(
            f"sections that open without a link to the previous one: {abrupt_sections}"
        )
    if repeated_summary:
        findings.append(
            f"summary sentences repeated from the body: {repeated_summary}"
        )

    return NoteStructureReport(
        total_sections=len(sections),
        empty_sections=empty_sections,
        short_sections=short_sections,
        bullet_only_sections=bullet_only,
        duplicate_paragraphs=duplicate_paragraphs,
        duplicate_bullets=duplicate_bullets,
        repeated_headings=repeated_headings,
        duplicate_key_points=duplicate_key_points,
        repeated_summary_sentences=repeated_summary,
        split_topics=split_topics,
        sections_without_paragraphs=sections_without_paragraphs,
        sentence_headings=sentence_headings,
        untitled_sections=untitled_sections,
        abrupt_sections=abrupt_sections,
        findings=tuple(findings),
    )


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
    #: Precision side of the comparison: what the notes state that no source
    #: chunk supports. Reported as the fingerprint of added ("hallucinated")
    #: content; see :func:`run_note_qa`.
    notes_numbers: int = 0
    notes_terms: int = 0
    unsupported_numbers: tuple[str, ...] = ()
    unsupported_terms: tuple[str, ...] = ()
    notes_text_chars: int = 0
    findings: tuple[str, ...] = field(default_factory=tuple)
    # Length / compression facts (added for measurable before/after checks).
    source_chars: int = 0
    total_chunks: int = 0
    covered_chunks: int = 0
    # Semantic completeness, computed by gamas_bot.units.
    total_units: int = 0
    covered_units: int = 0
    #: Unit types of the expected-but-missing content units (bounded, for logs).
    missing_unit_types: tuple[str, ...] = ()
    #: Deterministic structure/coherence diagnostics (duplicates, repeated
    #: headings, stub sections, a summary that repeats the body, ...).
    structure: NoteStructureReport = field(default_factory=NoteStructureReport)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings or self.uncovered_chunks)

    @property
    def compression_ratio(self) -> float:
        """Notes characters per source character (0.0 for an empty source)."""
        if self.source_chars <= 0:
            return 0.0
        return self.notes_text_chars / self.source_chars

    @property
    def coverage(self) -> float:
        """Fraction of the source's numbers+terms that survived into the notes."""
        total = self.source_numbers + self.source_terms
        if not total:
            # Nothing measurable to compare (a Persian-only humanities lecture):
            # coverage is defined as complete rather than unknown.
            return 1.0
        kept = self.preserved_numbers + self.preserved_terms
        return kept / total

    @property
    def chunk_coverage(self) -> float:
        """Fraction of signal-bearing chunks that kept any of their signal."""
        if not self.total_chunks:
            return 1.0
        return self.covered_chunks / self.total_chunks

    @property
    def semantic_coverage(self) -> float:
        """Fraction of the source's educational content units that survived.

        1.0 when the source exposed no comparable unit, so a short or purely
        conversational lecture is never scored as zero.
        """
        if self.total_units <= 0:
            return 1.0
        return self.covered_units / self.total_units

    @property
    def missing_units_count(self) -> int:
        """How many expected content units were not preserved."""
        return max(self.total_units - self.covered_units, 0)

    @property
    def unsupported_count(self) -> int:
        """How many invented values/strong terms the notes state."""
        return len(self.unsupported_numbers) + len(self.unsupported_terms)

    @property
    def has_unsupported_facts(self) -> bool:
        """True when the notes state a measured value the lecture never mentions.

        A *missing* number is information loss that a recall check can see; an
        *added* number cannot be — it is a correctness defect of its own, and
        the one failure mode a student cannot detect by reading. Only the
        conservative numeric signal is strong enough to gate a corrective
        pass; unsupported terms are reported but never used to trigger one.
        """
        return bool(self.unsupported_numbers)

    @property
    def compression_is_concerning(self) -> bool:
        """True when the notes are *both* very short and semantically poor.

        Compression alone is not a defect: removing filler and speech
        artifacts legitimately shrinks a transcript a long way. Only when the
        content units are also missing is the length actually evidence of
        loss. This is why the ratio is never a standalone failure condition.
        """
        if self.source_chars < MIN_RATIO_SOURCE_CHARS:
            return False
        if not 0 < self.compression_ratio < MIN_RATIO:
            return False
        return self.semantic_coverage < MIN_SEMANTIC_COVERAGE

    @property
    def needs_repair(self) -> bool:
        """True when the notes look degraded enough to justify a second pass.

        Reacts only to *unambiguous information loss*: a missing number/unit,
        a signal coverage below the floor, or educational content units
        (definitions, examples, procedures) that did not survive. Length is
        deliberately not a trigger on its own — a short source cannot
        distinguish "compiled" from "summarised", and gating on it previously
        meant the most over-compressed notes were the ones that never fired.

        A very short source never triggers the pass at all.
        """
        if self.source_chars < MIN_RATIO_SOURCE_CHARS:
            return False
        if self.missing_numbers:
            return True
        if self.coverage < MIN_COVERAGE:
            return True
        return self.semantic_coverage < MIN_SEMANTIC_COVERAGE


def _normalize_digits(value: str) -> str:
    """Persian/Arabic digits -> Latin so comparisons are script-independent.

    Delegates to :mod:`gamas_bot.textnorm` so there is exactly one digit table
    in the codebase; QA must agree with the renderer on what ``۵۰۰`` means.
    """
    return normalize_digits(value)


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


#: A run of digits, used to decide whether an apparently unseen value is a
#: genuine invention or merely the same magnitude written differently.
_DIGIT_RUN = re.compile(r"\d+")


def _digit_parts(value: str) -> set[str]:
    """The digit runs of a value, digit-normalised (``۵۰۰`` -> ``500``)."""
    return set(_DIGIT_RUN.findall(_normalize_digits(value)))


def _is_strong_term(term: str) -> bool:
    """A term whose absence from the source is a *strong* invention signal.

    An ordinary lowercase English word is too noisy to accuse: a Persian
    lecture rephrased with a different English word is not proof that a fact
    was invented. An acronym or a digit-bearing token (``MRI``, ``HbA1c``,
    ``COVID19``) that the notes contain but no source chunk does is.
    """
    return any(ch.isdigit() for ch in term) or (term.isupper() and len(term) >= 2)


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
    source_chars = 0
    signal_chunks = 0
    covered_chunks = 0

    for index, chunk in enumerate(source_chunks, start=1):
        source_chars += len(chunk)
        chunk_numbers = _extract_numbers(chunk)
        chunk_terms = _extract_terms(chunk)
        source_numbers |= chunk_numbers
        source_terms |= chunk_terms
        if not chunk_numbers and not chunk_terms:
            continue
        signal_chunks += 1
        missing_here_numbers = chunk_numbers - rendered_numbers
        missing_here_terms = {
            term for term in chunk_terms if term.casefold() not in rendered_terms_folded
        }
        missing_numbers |= missing_here_numbers
        missing_terms |= missing_here_terms
        # A chunk whose every number *and* term is absent from the notes is
        # likely under-covered (or genuinely filler); report it once. A signal
        # the chunk does not carry is vacuously "all absent", so a chunk of
        # only terms is judged on its terms alone.
        numbers_all_missing = not chunk_numbers or missing_here_numbers == chunk_numbers
        terms_all_missing = not chunk_terms or missing_here_terms == chunk_terms
        if numbers_all_missing and terms_all_missing:
            uncovered.append(index)
        else:
            covered_chunks += 1

    # Precision: the deterministic fingerprint of *added* content. A value is
    # only called unsupported when its canonical token is absent from every
    # source chunk *and* none of its digit runs occurs anywhere in the source,
    # so a formatting difference (``۱۲۰/۸۰`` vs ``120/80``, ``۵۰۰ میلی‌گرم``
    # vs ``500 mg``) is never mistaken for an invention. Terms are additionally
    # restricted to strong tokens (acronyms, digit-bearing forms) because an
    # ordinary rephrased English word is not evidence of fabrication.
    source_digits = _digit_parts(" ".join(source_chunks))
    unsupported_numbers = {
        token
        for token in (rendered_numbers - source_numbers)
        if not (_digit_parts(token) & source_digits)
    }
    source_terms_folded = {term.casefold() for term in source_terms}
    unsupported_terms = {
        term
        for term in rendered_terms
        if term.casefold() not in source_terms_folded and _is_strong_term(term)
    }

    # Semantic completeness: the fraction of educational content units
    # (definitions, examples, procedures, warnings, comparisons, explanations)
    # that survived. This is the check that catches a deleted explanation even
    # when every number and term survived.
    from .units import extract_all_units, semantic_coverage

    units = extract_all_units(source_chunks)
    expected_units = [unit for unit in units if unit.is_expectation]
    _, missing_units = semantic_coverage(units, rendered)

    # Structural diagnostics are *separate* from the content findings above:
    # a repeated heading or a stub section is worth reporting and benchmarking
    # (it tells the operator whether merge/compilation did their job) but it is
    # never a reason to run a repair pass, because there is no content to
    # restore and rewriting for style is not an option.
    structure = analyze_structure(notes)

    report = NoteQAReport(
        source_numbers=len(source_numbers),
        preserved_numbers=len(source_numbers - missing_numbers),
        source_terms=len(source_terms),
        preserved_terms=len(source_terms - missing_terms),
        missing_numbers=tuple(sorted(missing_numbers)[:MAX_REPORTED_FINDINGS]),
        missing_terms=tuple(sorted(missing_terms)[:MAX_REPORTED_FINDINGS]),
        uncovered_chunks=tuple(uncovered[:MAX_REPORTED_FINDINGS]),
        notes_numbers=len(rendered_numbers),
        notes_terms=len(rendered_terms),
        unsupported_numbers=tuple(sorted(unsupported_numbers)[:MAX_REPORTED_FINDINGS]),
        unsupported_terms=tuple(sorted(unsupported_terms)[:MAX_REPORTED_FINDINGS]),
        notes_text_chars=len(rendered),
        source_chars=source_chars,
        total_chunks=signal_chunks,
        covered_chunks=covered_chunks,
        total_units=len(expected_units),
        covered_units=len(expected_units) - len(missing_units),
        missing_unit_types=tuple(
            sorted({unit.type for unit in missing_units})[:MAX_REPORTED_FINDINGS]
        ),
        structure=structure,
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
    if report.unsupported_numbers:
        findings.append(
            "invented values in notes (absent from the source): "
            + ", ".join(report.unsupported_numbers)
        )
    if report.unsupported_terms:
        findings.append(
            "terms in notes absent from the source: "
            + ", ".join(report.unsupported_terms)
        )
    if report.missing_units_count:
        findings.append(
            f"missing educational content: {report.missing_units_count} unit(s) of type "
            + ",".join(report.missing_unit_types)
        )
    if report.compression_is_concerning:
        findings.append(
            f"aggressive compression: notes are {report.compression_ratio:.1%} of the source "
            f"with only {report.semantic_coverage:.0%} of content units preserved"
        )
    # ``findings`` is the last field, so build the report once at the end
    # instead of constructing it twice.
    report = replace(report, findings=tuple(findings))

    logger.info(
        "Note QA mode=%s source_chars=%s notes_chars=%s ratio=%.3f coverage=%.2f "
        "semantic=%.2f units=%s/%s chunk_coverage=%.2f numbers=%s/%s terms=%s/%s "
        "invented=%s/%s",
        getattr(notes, "note_mode", "?"),
        report.source_chars,
        report.notes_text_chars,
        report.compression_ratio,
        report.coverage,
        report.semantic_coverage,
        report.covered_units,
        report.total_units,
        report.chunk_coverage,
        report.preserved_numbers,
        report.source_numbers,
        report.preserved_terms,
        report.source_terms,
        report.unsupported_count,
        report.notes_numbers + report.notes_terms,
    )
    if report.has_findings:
        logger.warning(
            "Note QA coverage gaps (informational; notes delivered unchanged): %s",
            " | ".join(findings),
        )
    if not structure.is_clean:
        logger.warning(
            "Note QA structure diagnostics (informational; content untouched): %s",
            " | ".join(structure.findings),
        )
    return report
