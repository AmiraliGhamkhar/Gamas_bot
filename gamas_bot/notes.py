"""Note generation: the note model, the prompts, and a map-reduce pipeline.

This replaces v2's ``structuring`` + ``editorial`` + ``qa`` stack (≈6,100
lines) with one module and a fixed call budget:

* **one map call per transcript chunk** — run concurrently (bounded by
  ``NOTE_CONCURRENCY``), each with a deterministic position/context block so
  nothing is lost at chunk edges;
* **one optional reduce pass** — the editorial compile that merges the chunk
  notes into a coherent booklet. A booklet too large for one request is
  compiled in groups of whole sections (bounded levels); if the pass fails, the
  deterministic merge is delivered instead;
* **bounded command-driven repair**, ordered by critical faithfulness,
  coverage, and structural cleanup (the conservative default is two calls;
  ``NOTE_MAX_REPAIR_ATTEMPTS`` may permit the third structural pass). Oversized
  requests carry only affected sections and source evidence; an unsupported
  span that cannot be repaired is deterministically removed and re-audited;
* **isolated map outcomes and deterministic recovery.** A failed provider or
  malformed chunk never discards successful chunks; an exhausted chain yields
  a source-preserving ``StructuredNotes`` fallback whenever source exists.

The note dataclasses, the Persian prompts and the conservative deterministic
merge are ported from v2 nearly verbatim: they encode the content-preservation
contract (numbers, units, English terms and examples are never dropped) that
the raw-text priority is built on.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field as dataclass_field, replace
from typing import TYPE_CHECKING

import aiohttp

from .document_model import ContentPlan, NormalizedDocument, PlannedTopic, SourceBlock, SourceRange, normalize_source_text, plan_from_document

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - optional fast-path
    _orjson = None  # type: ignore[assignment]

# The public prompt surface of the note pipeline. It is re-exported here (not
# only imported) because callers, benchmarks and tests address it through this
# module: ``build_system_prompt``, ``build_compile_system_prompt``,
# ``MODE_RULES``, ``COMPILE_SYSTEM_PROMPT`` and the message builders. The text
# itself lives in ``_prompt_block`` so that it cannot be assembled differently
# per call site — see that module's docstring for the cache contract.
from ._prompt_block import (  # noqa: F401  (re-exported public prompt surface)
    COMPILE_SYSTEM_PROMPT,
    CONTEXT_DIGEST_MAX_CHARS,
    CONTEXT_HEAD_CHARS,
    COVERAGE_FILL_RULES,
    DRAFT_HEADING,
    MODE_RULES,
    NEIGHBOUR_CONTEXT_CHARS,
    PRESENTATION_CHUNK_TOPIC_MAX_CHARS,
    PRESENTATION_PROMPT,
    SOURCE_DIGEST_HEADING,
    SINGLE_CHUNK_HEADER,
    build_chunk_message,
    build_compile_system_prompt,
    build_edit_message,
    build_system_prompt,
    build_presentation_system_prompt,
    edit_system_prompt,
    notes_system_prompt,
    section_writer_system_prompt,
    synthesis_system_prompt,
)

from .config import Settings, resolve_note_mode
from .llm import (
    LLMContextOverflowError,
    LLMError,
    LLMOutputBudgetError,
    booklet_input_budget,
    complete_json,
)
from .llm_quota import estimate_tokens
from .note_planning import maybe_refine_plan
from .notes_evidence import (
    MIN_CLAIM_SUPPORT,
    NoteQualityReport,
    Signal,
    SignalAudit,
    audit_notes,
    classify_claim_risk,
    build_source_index,
    content_words,
    coverage_thresholds,
    evidence_windows,
    extract_number_occurrences,
    extract_signals,
    extract_term_occurrences,
    mask_structural,
    prose_claim_evidence,
    sentence_coverage,
    split_sentences,
    validate_table,
    validate_tip,
)
from .progress import to_persian_digits
from .semantic_segments import SemanticRecovery, SemanticSource, SemanticUnit, build_semantic_topics
from .textnorm import normalize_for_compare
from .transcript import split_transcript

if TYPE_CHECKING:
    from .booklet_verification import VerificationReport

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 20000
MAX_SOURCE_LOCATORS_PER_SECTION = 100
VALID_CALLOUT_KINDS = ("نکته", "هشدار", "یادآوری", "تأکید استاد")
PRACTICE_QUESTION_KINDS = ("مفهومی", "آزمونی", "تشریحی", "محاسباتی")

# These fields belong to transcription/operational records, never to the
# educational booklet contract.  Keeping the deny-list next to the canonical
# model makes both the permissive parser and strict provider schemas fail
# closed when a provider attempts to smuggle transcript metadata into a note.
FORBIDDEN_BOOKLET_METADATA_FIELDS = frozenset(
    {
        "speaker",
        "speakers",
        "speaker_id",
        "speaker_name",
        "speaker_turn",
        "speaker_turns",
        "timeline",
        "timestamp",
        "timestamps",
        "start_time",
        "end_time",
        "start",
        "end",
    }
)


def _contains_forbidden_booklet_metadata(value: object) -> bool:
    """Return whether a structured booklet payload carries transcript metadata."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_BOOKLET_METADATA_FIELDS:
                return True
            if _contains_forbidden_booklet_metadata(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_booklet_metadata(item) for item in value)
    return False

#: A merge below this size is not worth an editorial compile call.
COMPILE_MIN_NOTES_CHARS = 400
#: Worst-case size of the editorial message scaffolding (headings, separators,
#: the digest wrapper and the JSON envelope) that is neither the system prompt
#: nor the booklet body. The compile budget reserves it explicitly instead of
#: assuming the topic digest is the only other material in the window.
EDIT_MESSAGE_RESERVE_CHARS = 4096
#: Absolute ceiling on the compile request, however large the configured
#: window is: a larger merged booklet is delivered as the deterministic merge
#: rather than re-sent as one enormous request. The operative budget below
#: that ceiling is derived from the context window per request (see
#: :func:`effective_compile_max_chars`); ``NOTE_COMPILE_MAX_CHARS`` can further
#: lower that budget when an operator sets it explicitly.
COMPILE_HARD_MAX_CHARS = 160000
#: The deterministic digest/neighbour bounds. They live in the prompt module
#: because the chunk-context reserve is derived from them; ``notes`` re-exports
#: them so existing callers keep one import site.
#: (see ``gamas_bot._prompt_block``: CONTEXT_HEAD_CHARS,
#: PRESENTATION_CHUNK_TOPIC_MAX_CHARS, CONTEXT_DIGEST_MAX_CHARS,
#: NEIGHBOUR_CONTEXT_CHARS)

#: How many times one oversized request may be halved before the step gives up
#: (1 → 2 → 4 requests, each cut at a paragraph/sentence boundary).
MAX_CONTEXT_SPLITS = 2
#: Bound paid map work per booklet. Above this, recover from the complete
#: source locally (or fail explicitly if deterministic fallback is disabled).
MAX_NOTE_CHUNKS = 200

#: Hard ceiling on practical tips in one booklet; a long lecture may contain
#: more advice than a study document should surface at once.
MAX_PRACTICAL_TIPS = 8
#: How many sections one targeted repair request may carry when the whole
#: booklet does not fit the repair budget. The lost facts live in a handful of
#: sections, and the rest of the booklet is delivered unchanged and uncharged.
MAX_REPAIR_SECTIONS = 6
#: How many missing signals one repair request may carry. The windows are
#: bounded, so the repair request stays far below the compile budget.
MAX_REPAIR_SIGNALS = 40
#: Source characters of context handed to the repair pass per missing signal.
REPAIR_WINDOW_CHARS = 160
#: Absolute hard cap on node-repair requests, independent of provider failover.
MAX_NOTE_REPAIR_ATTEMPTS = 3

# Normalized, command-driven repair codes. They deliberately describe an
# operation rather than a generic "repair the booklet" request, which makes a
# bounded edit auditable and lets the repair prompt carry only the affected
# sections and source windows.
REPAIR_UNSUPPORTED_CLAIM = "REPAIR_UNSUPPORTED_CLAIM"
REPAIR_MISSING_NUMBER = "REPAIR_MISSING_NUMBER"
REPAIR_MISSING_TERM = "REPAIR_MISSING_TERM"
REPAIR_MISSING_CONTENT = "REPAIR_MISSING_CONTENT"
REPAIR_WRONG_NUMBER = "REPAIR_WRONG_NUMBER"
REPAIR_WRONG_TERM = "REPAIR_WRONG_TERM"
REPAIR_STRUCTURE = "REPAIR_STRUCTURE"
REPAIR_DUPLICATE = "REPAIR_DUPLICATE"
REPAIR_TABLE = "REPAIR_TABLE"
REPAIR_LOCATOR = "REPAIR_LOCATOR"
REPAIR_COVERAGE = "REPAIR_COVERAGE"
REPAIR_FORMAT = "REPAIR_FORMAT"

REPAIR_CRITICAL_CODES = frozenset(
    {REPAIR_UNSUPPORTED_CLAIM, REPAIR_WRONG_NUMBER, REPAIR_WRONG_TERM}
)
REPAIR_COVERAGE_CODES = frozenset(
    {REPAIR_MISSING_NUMBER, REPAIR_MISSING_TERM, REPAIR_MISSING_CONTENT, REPAIR_COVERAGE}
)
REPAIR_STRUCTURAL_CODES = frozenset(
    {REPAIR_STRUCTURE, REPAIR_DUPLICATE, REPAIR_TABLE, REPAIR_LOCATOR, REPAIR_FORMAT}
)

QUALITY_VERIFIED = "VERIFIED"
QUALITY_REPAIRED = "REPAIRED"
QUALITY_DEGRADED = "DEGRADED"
QUALITY_RECOVERED = "RECOVERED"


@dataclass(frozen=True, slots=True)
class RepairIssue:
    """One source-grounded, minimal edit command for a booklet artifact.

    Values are carried to the repair provider but never logged verbatim. The
    metadata is a normalized tuple so instances remain immutable and safe to
    pass through the bounded repair pipeline.
    """

    code: str
    severity: str
    section_id: str = ""
    field: str = ""
    current_text: str = ""
    source_evidence: str = ""
    action: str = ""
    source_signal_metadata: tuple[tuple[str, str], ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "severity": self.severity,
            "section_id": self.section_id,
            "field": self.field,
            "current_text": self.current_text,
            "source_evidence": self.source_evidence,
            "action": self.action,
        }
        if self.source_signal_metadata:
            payload["source_signal_metadata"] = dict(self.source_signal_metadata)
        return payload


class StructuringError(RuntimeError):
    """Note generation failed; the caller degrades to the raw transcript."""


class NoteQualityError(StructuringError):
    """The finished booklet still fails a hard evidence requirement.

    Raised after the bounded repair and scrub passes, so the caller keeps the
    transcript, reports a partial result and never presents an unsupported
    number or term as a delivered booklet.
    """

    def __init__(self, message: str, *, reason: str, fabricated: int = 0) -> None:
        super().__init__(message)
        self.reason = reason
        self.fabricated = fabricated


# ===========================================================================
# Note model
# ===========================================================================


@dataclass(frozen=True, slots=True)
class NoteCallout:
    kind: str
    text: str

    @classmethod
    def from_payload(cls, payload: object) -> "NoteCallout | None":
        if not isinstance(payload, dict):
            return None
        text = _bounded_text(payload.get("text"))
        if not text:
            return None
        kind = _bounded_text(payload.get("kind"), 20)
        if kind not in VALID_CALLOUT_KINDS:
            kind = "نکته"
        return cls(kind, text)


@dataclass(frozen=True, slots=True)
class NoteTable:
    headers: list[str]
    rows: list[list[str]]
    caption: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> "NoteTable | None":
        if not isinstance(payload, dict):
            return None
        headers = _string_list(payload.get("headers"))
        raw_rows = payload.get("rows")
        if not headers or not isinstance(raw_rows, (list, tuple)):
            return None
        rows: list[list[str]] = []
        for raw_row in raw_rows:
            if isinstance(raw_row, (list, tuple)):
                cells = [_bounded_text(cell, 4000) for cell in raw_row]
            else:
                cells = [_bounded_text(raw_row, 4000)]
            if any(cell for cell in cells):
                cells = (cells + [""] * len(headers))[: len(headers)]
                rows.append(cells)
        if not rows:
            return None
        caption = _bounded_text(payload.get("caption"), MAX_TITLE_CHARS)
        return cls(headers, rows, caption)

    def signature(self) -> tuple[str, ...]:
        """Column identity, used to merge the same table across chunks."""
        return tuple(_compare_key(header) for header in self.headers)


#: The kinds a practical tip may carry. The label is the lecturer's stance,
#: which the evidence layer can check against the basis sentence.
TIP_KINDS = ("کاربردی", "آزمونی", "هشدار")


@dataclass(frozen=True, slots=True)
class PracticalTip:
    """A surfaced piece of the lecturer's own advice.

    ``basis`` is the verbatim source phrase the tip was derived from. It is
    never rendered: it exists so :func:`gamas_bot.notes_evidence.validate_tip`
    can prove the tip is not new content. A tip without a basis is dropped.
    """

    text: str
    basis: str = ""
    kind: str = "کاربردی"

    @classmethod
    def from_payload(cls, payload: object) -> "PracticalTip | None":
        if not isinstance(payload, dict):
            return None
        text = _bounded_text(payload.get("text"))
        if not text:
            return None
        kind = _bounded_text(payload.get("kind"), 20)
        if kind not in TIP_KINDS:
            kind = "کاربردی"
        return cls(text, _bounded_text(payload.get("basis")), kind)


@dataclass(frozen=True, slots=True)
class NoteDefinition:
    term: str
    definition: str
    term_en: str = ""


    @classmethod
    def from_payload(cls, payload: object) -> "NoteDefinition | None":
        if not isinstance(payload, dict):
            return None
        term = _bounded_text(payload.get("term"), MAX_TITLE_CHARS)
        definition = _bounded_text(payload.get("definition"))
        if not term or not definition:
            return None
        return cls(term, definition, _bounded_text(payload.get("term_en"), MAX_TITLE_CHARS))


def _table_identity(table: NoteTable) -> tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]]:
    return table.caption, tuple(table.headers), tuple(tuple(row) for row in table.rows)


def _normalise_tables(payload: dict) -> tuple[NoteTable | None, tuple[NoteTable, ...]]:
    """Read ``table`` (legacy) and ``tables`` (v4) into primary + extras.

    The primary table is ``table`` when present, else the first entry of
    ``tables``. ``tables`` keeps every table that is not the primary one, so a
    renderer that knows only ``table`` still shows the first table and the v4
    renderer shows all of them. Identical tables are emitted once.
    """
    primary = NoteTable.from_payload(payload.get("table"))
    extra: list[NoteTable] = []
    seen = {_table_identity(primary)} if primary is not None else set()
    raw_tables = payload.get("tables")
    if isinstance(raw_tables, dict):
        # A model that returns one table under ``tables`` means a list of one.
        raw_tables = [raw_tables]
    for item in raw_tables if isinstance(raw_tables, (list, tuple)) else ():
        table = NoteTable.from_payload(item)
        if table is None or _table_identity(table) in seen:
            continue
        seen.add(_table_identity(table))
        if primary is None:
            primary = table
        else:
            extra.append(table)
    return primary, tuple(extra)


@dataclass(frozen=True, slots=True)
class NoteSection:
    heading: str
    paragraphs: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()
    definitions: tuple[NoteDefinition, ...] = ()
    examples: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    formulas: tuple[str, ...] = ()
    key_points: tuple[str, ...] = ()
    table: NoteTable | None = None
    callouts: tuple[NoteCallout, ...] = ()
    #: Additional tables beyond ``table``. The primary table stays in ``table``
    #: so every existing renderer and the legacy payload keep working; a model
    #: answer with ``tables`` is normalised into both fields.
    tables: tuple[NoteTable, ...] = ()
    #: Exact page/slide/section labels from an extracted source. Populated only
    #: by document-note generation; audio-note output remains unchanged.
    source_locators: tuple[str, ...] = ()
    # Generation provenance (not provider/stored JSON or renderer fields).
    section_id: str = dataclass_field(default="", compare=False, repr=False)
    topic_id: str = dataclass_field(default="", compare=False, repr=False)
    parent_topic_id: str | None = dataclass_field(default=None, compare=False, repr=False)
    source_block_ids: tuple[str, ...] = dataclass_field(default=(), compare=False, repr=False)


    @property
    def all_tables(self) -> tuple[NoteTable, ...]:
        primary = (self.table,) if self.table is not None else ()
        return primary + tuple(self.tables)

    @property
    def has_content(self) -> bool:
        return bool(
            self.paragraphs
            or self.bullets
            or self.definitions
            or self.examples
            or self.steps
            or self.formulas
            or self.key_points
            or self.all_tables
            or self.callouts
        )

    @classmethod
    def from_payload(cls, payload: object, fallback_index: int) -> "NoteSection | None":
        if not isinstance(payload, dict):
            return None
        heading = _bounded_text(payload.get("heading"), MAX_TITLE_CHARS)
        if not heading:
            heading = f"بخش {fallback_index}"
        callouts = tuple(
            filter(None, (NoteCallout.from_payload(item) for item in payload.get("callouts") or []))
        )
        definitions = tuple(
            filter(None, (NoteDefinition.from_payload(item) for item in payload.get("definitions") or []))
        )
        primary, extra = _normalise_tables(payload)
        section = cls(
            heading=heading,
            paragraphs=tuple(_string_list(payload.get("paragraphs"))),
            bullets=tuple(_string_list(payload.get("bullets"))),
            definitions=definitions,
            examples=tuple(_string_list(payload.get("examples"))),
            steps=tuple(_string_list(payload.get("steps"))),
            formulas=tuple(_string_list(payload.get("formulas"))),
            key_points=tuple(_string_list(payload.get("key_points"))),
            table=primary,
            callouts=callouts,
            tables=extra,
            source_locators=tuple(
                dict.fromkeys(
                    " ".join(value.split())[:120]
                    for value in _string_list(payload.get("source_locators"))
                    if value.strip()
                )
            )[:MAX_SOURCE_LOCATORS_PER_SECTION],
        )
        return section if section.has_content else None


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    term: str
    definition: str
    term_en: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> "GlossaryEntry | None":
        if not isinstance(payload, dict):
            return None
        term = _bounded_text(payload.get("term"), MAX_TITLE_CHARS)
        definition = _bounded_text(payload.get("definition"))
        if not term or not definition:
            return None
        return cls(term, definition, _bounded_text(payload.get("term_en"), MAX_TITLE_CHARS))


@dataclass(frozen=True, slots=True)
class PracticeQuestion:
    """A course-related study question paired with a concise scientific answer."""

    question: str
    answer: str = ""
    kind: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> "PracticeQuestion | None":
        if isinstance(payload, str):
            text = _bounded_text(payload)
            return cls(text) if text else None
        if not isinstance(payload, dict):
            return None
        question = _bounded_text(payload.get("question") or payload.get("q"))
        if not question:
            return None
        answer = _bounded_text(payload.get("answer") or payload.get("a"))
        kind = _bounded_text(payload.get("kind"), 24)
        if kind and kind not in PRACTICE_QUESTION_KINDS:
            kind = ""
        return cls(question=question, answer=answer, kind=kind)


@dataclass(frozen=True, slots=True)
class MergeConflict:
    """A conflict surfaced without choosing a winner or dropping either value."""

    kind: str
    identifiers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StructuredNotes:
    """The validated note structure the LLM must produce as strict JSON."""

    title: str = ""
    subtitle: str = ""
    summary: str = ""
    sections: tuple[NoteSection, ...] = ()
    key_points: tuple[str, ...] = ()
    glossary: tuple[GlossaryEntry, ...] = ()
    note_mode: str = "full"
    learning_objectives: tuple[str, ...] = ()
    review_questions: tuple[str, ...] = ()
    #: Course-related practice/exam questions paired with concise answers.
    practice_questions: tuple[PracticeQuestion, ...] = ()
    #: The lecturer's own practical advice, surfaced with its source basis.
    practical_tips: tuple[PracticalTip, ...] = ()
    #: Only source-supported pitfalls belong here; this is never a generic
    #: study-skills list.
    common_mistakes: tuple[str, ...] = ()
    #: A closing high-value recall list. It is derived from source-backed
    #: content and must exist before a full booklet is accepted.
    final_review: tuple[str, ...] = ()
    #: Internal outcome only. It intentionally is not serialized into the
    #: established StructuredNotes JSON contract or renderer input.
    quality_state: str = QUALITY_VERIFIED
    #: Input provenance, not a claim that each range appears in the output.
    #: Kept out of the provider/stored JSON and renderer contract.
    input_ranges: tuple[SourceRange, ...] = dataclass_field(default=(), compare=False, repr=False)
    semantic_recoveries: tuple[SemanticRecovery, ...] = dataclass_field(default=(), compare=False, repr=False)
    failed_sections: tuple[FailedSection, ...] = dataclass_field(default=(), compare=False, repr=False)
    merge_conflicts: tuple[MergeConflict, ...] = dataclass_field(default=(), compare=False, repr=False)
    verification_report: VerificationReport | None = dataclass_field(default=None, compare=False, repr=False)

    @property
    def display_title(self) -> str:
        return self.title or "جزوهٔ کلاس"

    @property
    def has_content(self) -> bool:
        return bool(
            self.subtitle
            or self.summary
            or self.sections
            or self.key_points
            or self.glossary
            or self.learning_objectives
            or self.review_questions
            or self.practice_questions
            or self.practical_tips
            or self.common_mistakes
            or self.final_review
        )

    def to_payload(self) -> dict:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "summary": self.summary,
            # CR-3: the field stays for backward compatibility with archived
            # booklets, but is always emitted as "full" (legacy values normalise).
            "note_mode": "full",
            "learning_objectives": list(self.learning_objectives),
            "sections": [
                {
                    "heading": section.heading,
                    "paragraphs": list(section.paragraphs),
                    "bullets": list(section.bullets),
                    "definitions": [
                        {
                            "term": definition.term,
                            "term_en": definition.term_en,
                            "definition": definition.definition,
                        }
                        for definition in section.definitions
                    ],
                    "examples": list(section.examples),
                    "steps": list(section.steps),
                    "formulas": list(section.formulas),
                    "key_points": list(section.key_points),
                    "tables": [
                        {
                            "caption": table.caption,
                            "headers": list(table.headers),
                            "rows": [list(row) for row in table.rows],
                        }
                        for table in section.all_tables
                    ],
                    "callouts": [
                        {"kind": callout.kind, "text": callout.text}
                        for callout in section.callouts
                    ],
                    "source_locators": list(section.source_locators),
                }
                for section in self.sections
            ],
            "practical_tips": [
                {"text": tip.text, "basis": tip.basis, "kind": tip.kind}
                for tip in self.practical_tips
            ],
            "key_points": list(self.key_points),
            "practice_questions": [
                {"question": item.question, "answer": item.answer, "kind": item.kind}
                for item in self.practice_questions
            ],
            "review_questions": list(self.review_questions),
            "common_mistakes": list(self.common_mistakes),
            "final_review": list(self.final_review),
            "glossary": [
                {"term": entry.term, "term_en": entry.term_en, "definition": entry.definition}
                for entry in self.glossary
            ],
        }

    def to_json(self) -> str:
        if _orjson is not None:
            return _orjson.dumps(self.to_payload()).decode("utf-8")
        return json.dumps(self.to_payload(), ensure_ascii=False)

    def to_markdown(self) -> str:
        """Render as the Markdown subset the Telegram renderer understands."""
        parts: list[str] = [f"# {self.display_title}"]
        if self.subtitle:
            parts.append(f"**{self.subtitle}**")
        if self.summary:
            parts.append(f"**{self.summary}**")
        if self.learning_objectives:
            parts.append("**اهداف یادگیری**")
            parts.extend(f"- {objective}" for objective in self.learning_objectives)
        for section in self.sections:
            parts.append(f"## {section.heading}")
            if section.source_locators:
                parts.append("منبع: " + "، ".join(section.source_locators))
            parts.extend(section.paragraphs)
            if section.definitions:
                parts.append("**تعریف‌ها**")
                parts.extend(
                    f"- **{entry.term}**"
                    + (f" ({entry.term_en})" if entry.term_en else "")
                    + f": {entry.definition}"
                    for entry in section.definitions
                )
            parts.extend(f"- {bullet}" for bullet in section.bullets)
            if section.examples:
                parts.append("**مثال‌ها**")
                parts.extend(f"- {example}" for example in section.examples)
            if section.steps:
                parts.append("**مراحل انجام**")
                parts.extend(
                    f"{index}. {step}" for index, step in enumerate(section.steps, start=1)
                )
            if section.formulas:
                parts.append("**فرمول‌ها**")
                parts.extend(f"- {formula}" for formula in section.formulas)
            if section.key_points:
                parts.append("**نکته‌های کلیدی این بخش**")
                parts.extend(f"- {point}" for point in section.key_points)
            for callout in section.callouts:
                parts.append(f"**{callout.kind}:** {callout.text}")
            for table in section.all_tables:
                if table.caption:
                    parts.append(f"**{table.caption}**")
                header = "| " + " | ".join(table.headers) + " |"
                separator = (
                    "|"
                    + "|".join("-" * max(len(header) + 2, 3) for header in table.headers)
                    + "|"
                )
                rows = "\n".join("| " + " | ".join(row) + " |" for row in table.rows)
                parts.append(f"{header}\n{separator}\n{rows}")
        if self.key_points:
            parts.append("## نکته‌های کلیدی")
            parts.extend(f"- {point}" for point in self.key_points)
        if self.practical_tips:
            parts.append("## نکته‌های کاربردی")
            parts.extend(f"- **{tip.kind}:** {tip.text}" for tip in self.practical_tips)
        if self.practice_questions:
            parts.append("## پرسش‌ها و تمرین‌های درس")
            for index, qa in enumerate(self.practice_questions, start=1):
                prefix = f" ({qa.kind})" if qa.kind else ""
                parts.append(f"{index}.{prefix} {qa.question}")
                if qa.answer:
                    parts.append(f"پاسخ: {qa.answer}")
        if self.review_questions:
            parts.append("## پرسش‌های مرور")
            parts.extend(
                f"{index}. {question}"
                for index, question in enumerate(self.review_questions, start=1)
            )
        if self.common_mistakes:
            parts.append("## خطاها و دام‌های رایج")
            parts.extend(f"- {item}" for item in self.common_mistakes)
        if self.glossary:
            parts.append("## واژه\u200cنامه")
            parts.extend(
                f"- **{entry.term}"
                + (f" ({entry.term_en})" if entry.term_en else "")
                + f":** {entry.definition}"
                for entry in self.glossary
            )
        if self.final_review:
            parts.append("## مرور نهایی")
            parts.extend(f"- {item}" for item in self.final_review)
        return "\n\n".join(parts)

    @classmethod
    def from_payload(
        cls, payload: object, *, provider_reply: bool = False
    ) -> "StructuredNotes":
        """Validate and normalise a model-supplied JSON payload."""
        if not isinstance(payload, dict):
            raise StructuringError("پاسخ سرویس تولید جزوه یک شیء JSON نبود.")
        if _contains_forbidden_booklet_metadata(payload):
            raise StructuringError("پاسخ جزوه شامل فرادادهٔ ممنوعِ گفتار یا زمان بود.")
        raw_sections = payload.get("sections")
        sections = tuple(
            filter(
                None,
                (
                    NoteSection.from_payload(item, index)
                    for index, item in enumerate(
                        raw_sections if isinstance(raw_sections, (list, tuple)) else [],
                        start=1,
                    )
                ),
            )
        )
        raw_glossary = payload.get("glossary")
        glossary_items = raw_glossary if isinstance(raw_glossary, (list, tuple)) else []
        glossary = tuple(
            filter(None, (GlossaryEntry.from_payload(item) for item in glossary_items))
        )
        # CR-3: FULL is the only mode. Any legacy compression-mode value or
        # junk in an archived payload is normalised to "full" and never crashes
        # parsing. ``default_mode`` is kept for a compatible signature.
        note_mode = "full"
        raw_tips = payload.get("practical_tips")
        practical_tips = tuple(
            filter(
                None,
                (
                    PracticalTip.from_payload(item)
                    for item in (raw_tips if isinstance(raw_tips, (list, tuple)) else [])
                ),
            )
        )
        raw_practice = payload.get("practice_questions")
        practice_list: list[PracticeQuestion] = [
            parsed
            for item in (raw_practice if isinstance(raw_practice, (list, tuple)) else [])
            if (parsed := PracticeQuestion.from_payload(item)) is not None
        ]
        raw_review = payload.get("review_questions")
        review_list: list[str] = []
        if isinstance(raw_review, (list, tuple)):
            for item in raw_review:
                if isinstance(item, dict):
                    parsed_q = PracticeQuestion.from_payload(item)
                    if parsed_q is not None:
                        practice_list.append(parsed_q)
                else:
                    cleaned = _bounded_text(item)
                    if cleaned:
                        review_list.append(cleaned)
        notes = cls(
            title=_bounded_text(payload.get("title"), MAX_TITLE_CHARS),
            subtitle=_bounded_text(payload.get("subtitle"), MAX_TITLE_CHARS),
            summary=(_bounded_text(payload.get("summary")) if provider_reply else
                     scrub_backend_artifacts(str(payload.get("summary") or ""))),
            sections=sections,
            key_points=tuple(_string_list(payload.get("key_points"))),
            glossary=glossary,
            note_mode=note_mode,
            learning_objectives=tuple(_string_list(payload.get("learning_objectives"))),
            review_questions=tuple(review_list),
            practice_questions=tuple(practice_list),
            practical_tips=practical_tips,
            common_mistakes=tuple(_string_list(payload.get("common_mistakes"))),
            final_review=tuple(_string_list(payload.get("final_review"))),
        )
        if not notes.has_content:
            raise StructuringError("ساختار جزوهٔ دریافتی خالی بود.")
        return notes


# ===========================================================================
# Backend-artifact scrubbing (ported verbatim: the patterns are the contract)
# ===========================================================================

_BACKEND_NAMES = (
    "OpenAI", "GPT", "ChatGPT", "Gemini", "Google", "Anthropic", "Claude",
    "Mistral", "Cohere", "Deepgram", "Speechmatics", "Whisper",
    "OpenRouter", "Cloudflare", "NVIDIA", "Hugging Face", "HuggingFace",
)
_BACKEND_NAME_ALT = "|".join(re.escape(name) for name in _BACKEND_NAMES)

_KEY_PREFIX = re.compile(
    r"\b(?:sk|sk-ant|ghp|gho|github_pat|hf|gsk|nvapi|xox[baprs])[_-][A-Za-z0-9_-]{8,}\b"
    r"|\bAIza[A-Za-z0-9_-]{12,}\b"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b"
)
_REQUEST_ID = re.compile(
    r"\b(?:chatcmpl-[A-Za-z0-9]{6,}|req_[A-Za-z0-9]{6,}|resp_[A-Za-z0-9]{6,}"
    r"|cmpl-[A-Za-z0-9]{6,}|msg_[A-Za-z0-9]{6,})\b"
    r"|\bx-request-id\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_KEY_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|authorization)\b\s*[\"']?\s*[:=]\s*[\"']?[^\s\"']{6,}",
    re.IGNORECASE,
)
_SELF_REFERENCE = re.compile(
    r"\b(?:powered|generated|created|provided|developed|served|hosted)\s+by\s+(?:"
    + _BACKEND_NAME_ALT + r")\b[\w\s.-]{0,24}"
    r"|\b(?:" + _BACKEND_NAME_ALT + r")\s+(?:API|api|model|endpoint|service|provider|LLM|engine|servers?)\b"
    r"|(?:ساخته|تولید|ارائه|آماده)\s*شده\s*(?:توسط|با)\s+(?:" + _BACKEND_NAME_ALT + r")\b"
    r"|(?:توسط|با)\s+(?:" + _BACKEND_NAME_ALT + r")\s+(?:API|api|سرویس)\b",
    re.IGNORECASE,
)
_AI_DISCLAIMER = re.compile(
    r"\bas\s+an?\s+AI(?:\s+(?:assistant|language\s+model|model|chatbot|bot))?\b"
    r"(?=[,؛.]?\s*(?:I\b|I['’]|my\b|we\b|our\b))"
    r"|\bas\s+a\s+language\s+model\b(?=[,؛.]?\s*(?:I\b|I['’]|my\b|we\b|our\b))"
    r"|^\s*as\s+an?\s+AI(?:\s+(?:assistant|language\s+model|model|chatbot|bot))?"
    r"(?:\s*[:،؛]|\s*$)"
    r"|\bI['’]?m\s+(?:just\s+)?an?\s+AI(?:\s+(?:assistant|language\s+model|model))?\b"
    r"|\bI\s+am\s+(?:just\s+)?an?\s+AI(?:\s+(?:assistant|language\s+model|model))?\b"
    r"|من\s+یک\s+هوش\s+مصنوعی\s*(?:هستم|نیستم)?"
    r"|من\s+یک\s+مدل\s+زبانی\s+(?:هستم|نیستم)?",
    re.IGNORECASE,
)
_META_PHRASE = re.compile(
    r"\b(?:system\s+prompt|instruction\s+prompt|prompt\s+template"
    r"|response[_-]?format|json[_-]?schema|json[_-]?object\s+mode"
    r"|additionalProperties|prompt[_-]?tokens|completion[_-]?tokens|total[_-]?tokens"
    r"|finish[_-]?reason|x-ratelimit[\w-]*)\b\s*[:=]?\s*[\"']?[^\s\"',}]{0,40}",
    re.IGNORECASE,
)
_DIAGNOSTIC_TOKEN = re.compile(
    r"HTTP\s*/?\s*\d{3}\b|status[_ ]?code|retry[- ]?after|rate[- ]?limit|quota"
    r"|too many requests|billing|overloaded|server error|internal error"
    r"|\b429\b|\b500\b|\b502\b|\b503\b|\b504\b|\b408\b",
    re.IGNORECASE,
)
_REDACTION = "•••"
_STATUS_PHRASE = re.compile(
    r"(?:HTTP\s*/?\s*|status(?:_|\s)?code\s*[:=]?\s*|error\s*(?:code)?\s*[:=]?\s*|code\s*[:=]?\s*)"
    r"(?:429|500|502|503|504|408)\b"
    r"(?:\s+(?:Too Many Requests|Bad Gateway|Service Unavailable|Gateway Timeout|Request Timeout|Internal Server Error))?",
    re.IGNORECASE,
)


def _scrub_line(line: str) -> tuple[str, int]:
    hits = 0
    for pattern in (_KEY_PREFIX, _REQUEST_ID, _KEY_ASSIGNMENT, _SELF_REFERENCE, _AI_DISCLAIMER, _META_PHRASE):
        line, count = pattern.subn(_REDACTION, line)
        hits += count
    markers = len(_DIAGNOSTIC_TOKEN.findall(line))
    if markers >= 2:
        return "", hits + markers
    scrubbed, count = _STATUS_PHRASE.subn(_REDACTION, line)
    return scrubbed, hits + count


def scrub_backend_artifacts(text: str) -> str:
    """Remove backend plumbing a model may have echoed into its answer.

    Conservative by design: bare provider names survive (a networking lecture
    may legitimately mention Cloudflare or NVIDIA); only self-references,
    key/id-shaped tokens, diagnostic clusters and meta phrases are removed.
    """
    if not text:
        return text
    cleaned_lines = []
    for line in str(text).splitlines():
        cleaned, _ = _scrub_line(line)
        cleaned_lines.append(cleaned)
    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _bounded_text(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    return scrub_backend_artifacts(str(value if value is not None else ""))[:limit]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_bounded_text(item) for item in value if _bounded_text(item)]


# ===========================================================================
# Heading topic identity (the merge's definition of "the same topic")
# ===========================================================================

MIN_SHARED_TOPIC_WORDS = 2

#: Combining marks that trail a continuation word: the Persian ezafe
#: («ادامهٔ …» = «ادامه» + U+0654), harakat and ZWNJ.
_CONTINUATION_MARKS = "\u064b-\u065f\u200c"

_CONTINUATION_PREFIX = re.compile(
    r"^(?:\(?\s*(?:ادامه|دنباله|تکمله|بخش\s+(?:بعد|دوم|۲|2)|part\s*(?:2|ii)|cont(?:inued)?)"
    r"\s*\)?[\s.:،\-—»" + _CONTINUATION_MARKS + r"]*)+",
    re.IGNORECASE,
)
_CONTINUATION_SUFFIX = re.compile(
    r"[\s(«\[:،\-—" + _CONTINUATION_MARKS + r"]*(?:ادامه|دنباله|تکمله|بخش\s+بعد|continued|cont\.?|part\s+(?:2|ii))[\s)»\]:،\-—.]*$",
    re.IGNORECASE,
)
_HEADING_STOPWORDS = frozenset(
    {
        "و", "در", "به", "از", "با", "بر", "برای", "که", "این", "آن", "های",
        "ها", "یک", "یا", "تا", "را", "هم", "می", "شود", "است", "اسلاید",
        "the", "of", "and", "a", "an", "to", "in", "on", "for", "with", "part",
        "section", "slide",
    }
)


def _compare_key(value: str) -> str:
    """Whitespace/Persian-variant-insensitive identity for duplicate detection."""
    return re.sub(r"\s+", " ", normalize_for_compare(value)).strip()


def heading_topic_key(heading: str) -> str:
    """Topic identity of a heading, with continuation markers removed.

    «مقدمه», «ادامهٔ مقدمه», «مقدمه (ادامه)» are the same topic written three
    ways; the merge needs exactly one definition of that identity.
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
    key = heading_topic_key(heading) or _compare_key(heading)
    words = re.findall(r"[^\W_]+", key, re.UNICODE)
    return frozenset(
        word
        for word in words
        if word not in _HEADING_STOPWORDS and (len(word) > 1 or word.isdigit())
    )


def _heading_numbers(heading: str) -> tuple[str, ...]:
    key = heading_topic_key(heading) or _compare_key(heading)
    return tuple(re.findall(r"\d+", key))


def headings_share_a_topic(first: str, second: str, *, minimum: int = MIN_SHARED_TOPIC_WORDS) -> bool:
    """Do two headings clearly describe one topic? (conservative)"""
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
    """A looser topic test, used *only* across a chunk boundary."""
    nums_a = _heading_numbers(first)
    nums_b = _heading_numbers(second)
    if nums_a and nums_b and nums_a != nums_b:
        return False
    words_a = heading_topic_words(first)
    words_b = heading_topic_words(second)
    if not words_a or not words_b:
        return False
    shared = words_a & words_b
    if len(shared) < MIN_SHARED_TOPIC_WORDS:
        return False
    return len(shared) / len(words_a | words_b) >= threshold


# ===========================================================================
# Prompt-facing helpers and the response schema
# ===========================================================================

#: CR-3: FULL is the single source of truth, and the note-generation ``mode``
#: parameter is kept on the public builders for backward compatibility.
#:
#: The prompt text itself lives in :mod:`gamas_bot._prompt_block`: one static
#: agent prompt, one static editorial prompt, and the two user-message
#: builders. It is deliberately not assembled here any more — the assembled
#: form differed per chunk (context block, source locators) and per source
#: kind, which made every request of a job an uncached prompt on a provider
#: that caches by prefix. See that module for the contract itself.

def notes_response_schema() -> dict:
    """Strict JSON Schema for the canonical educational booklet contract.

    The provider-facing shape deliberately has no transcript identity, turn or
    timing fields.  Optional instructional structures remain optional because
    a source cannot justify every kind of table, formula, example or warning.
    """
    text_array = {"type": "array", "items": {"type": "string"}}
    definition = {
        "type": "object",
        "properties": {
            "term": {"type": "string"},
            "term_en": {"type": "string"},
            "definition": {"type": "string"},
        },
        "additionalProperties": False,
    }
    table = {
        "type": "object",
        "properties": {
            "caption": {"type": "string"},
            "headers": text_array,
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        },
        "additionalProperties": False,
    }
    section = {
        "type": "object",
        "properties": {
            "heading": {"type": "string"},
            "source_locators": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_SOURCE_LOCATORS_PER_SECTION},
            "paragraphs": text_array,
            "bullets": text_array,
            "definitions": {"type": "array", "items": definition},
            "examples": text_array,
            "steps": text_array,
            "formulas": text_array,
            "key_points": text_array,
            "tables": {"type": "array", "items": table},
            "callouts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"kind": {"type": "string"}, "text": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
        },
        "required": ["heading"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "subtitle": {"type": "string"},
            "summary": {"type": "string"},
            "learning_objectives": text_array,
            "sections": {"type": "array", "items": section},
            "practical_tips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "basis": {"type": "string"},
                        "kind": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "key_points": text_array,
            "practice_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "answer": {"type": "string"},
                        "kind": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "review_questions": text_array,
            "common_mistakes": text_array,
            "final_review": text_array,
            "glossary": {"type": "array", "items": definition},
        },
        "required": ["sections"],
        "additionalProperties": False,
    }

def _response_schema(settings: Settings) -> dict | None:
    """The schema to send, or ``None`` when structured output is off."""
    return notes_response_schema() if getattr(settings, "note_structured_output", False) else None


# ===========================================================================
# Deterministic merge (ported verbatim: conservative, no re-summarising)
# ===========================================================================


def _dedupe_by_key(values, key) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        identity = key(value)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(value)
    return tuple(result)


def _literal_key(value: str) -> str:
    # Case, superscripts, punctuation and units are semantically significant.
    return unicodedata.normalize("NFC", value).strip()


def _dedupe_definitions(definitions: tuple[NoteDefinition, ...]) -> tuple[NoteDefinition, ...]:
    seen: set[tuple[str, str, str]] = set()
    result: list[NoteDefinition] = []
    for entry in definitions:
        key = (_literal_key(entry.term), _literal_key(entry.definition), _literal_key(entry.term_en))
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return tuple(result)


def _dedupe_callouts(callouts: tuple[NoteCallout, ...]) -> tuple[NoteCallout, ...]:
    seen: set[tuple[str, str]] = set()
    result: list[NoteCallout] = []
    for callout in callouts:
        identity = (callout.kind, _literal_key(callout.text))
        if identity not in seen:
            seen.add(identity)
            result.append(callout)
    return tuple(result)


def _dedupe_table_rows(rows: list[list[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    result: list[list[str]] = []
    for row in rows:
        identity = tuple(_literal_key(cell) for cell in row)
        if identity not in seen:
            seen.add(identity)
            result.append(row)
    return result


def _merge_table_group(
    tables: Sequence[NoteTable],
) -> tuple[NoteTable | None, tuple[NoteTable, ...]]:
    """Merge tables that share a column signature, keep distinct ones apart.

    Two chunks that describe the same comparison produce two tables with the
    same headers; their rows belong together. Two genuinely different tables
    in one section are kept as separate tables instead of being flattened into
    bullets — a table is the shape the lecturer's comparison actually has.
    """
    combined: list[NoteTable] = []
    for table in tables:
        headers = tuple(map(_literal_key, table.headers))
        caption = _literal_key(table.caption)
        for index, current in enumerate(combined):
            if (headers == tuple(map(_literal_key, current.headers))
                    and caption == _literal_key(current.caption)):
                combined[index] = NoteTable(
                    list(current.headers),
                    _dedupe_table_rows([list(row) for row in (*current.rows, *table.rows)]),
                    current.caption,
                )
                break
        else:
            combined.append(table)
    return (combined[0], tuple(combined[1:])) if combined else (None, ())


def _combine_sections(first: NoteSection, second: NoteSection) -> NoteSection:
    table, tables = _merge_table_group(first.all_tables + second.all_tables)
    return replace(
        first,
        paragraphs=_dedupe_by_key(first.paragraphs + second.paragraphs, _literal_key),
        bullets=_dedupe_by_key(first.bullets + second.bullets, _literal_key),
        definitions=_dedupe_definitions(first.definitions + second.definitions),
        examples=_dedupe_by_key(first.examples + second.examples, _literal_key),
        steps=first.steps if first.steps == second.steps else first.steps + second.steps,
        formulas=_dedupe_by_key(first.formulas + second.formulas, _literal_key),
        key_points=_dedupe_by_key(first.key_points + second.key_points, _literal_key),
        callouts=_dedupe_callouts(first.callouts + second.callouts),
        table=table,
        tables=tables,
        source_block_ids=tuple(dict.fromkeys(first.source_block_ids + second.source_block_ids)),
        source_locators=tuple(
            dict.fromkeys(first.source_locators + second.source_locators)
        ),
    )


_LITERAL_CONT_PREFIX = re.compile(r"^(?:ادامه[ٔ‌ی]*|دنباله[ٔ‌ی]*|continued|cont\.)(?=[\s:：،—-]|$)[\s:：،—-]*", re.I)
_LITERAL_CONT_SUFFIX = re.compile(r"\s*[(«](?:ادامه|دنباله|continued|cont\.)[)»]$", re.I)


def _literal_continuation_title(text: str) -> str:
    return _LITERAL_CONT_SUFFIX.sub("", _LITERAL_CONT_PREFIX.sub("", _literal_key(text)))


def _same_logical_section(first: NoteSection, second: NoteSection) -> bool:
    if (first.section_id != second.section_id or first.topic_id != second.topic_id
            or first.parent_topic_id != second.parent_topic_id):
        return False
    if first.steps and second.steps and first.steps != second.steps:
        return False  # do not turn separate procedures into one sequence
    if len(set(first.source_locators + second.source_locators)) > MAX_SOURCE_LOCATORS_PER_SECTION:
        return False  # keep distinct structures rather than clipping references
    if _literal_key(first.heading) == _literal_key(second.heading):
        return True
    # Only a literal continuation marker is structural. Similar headings do
    # not establish that two sections or subjects are interchangeable.
    a, b = map(_literal_continuation_title, (first.heading, second.heading))
    return bool(a and a == b) or (not second.topic_id and not b and bool(a))


def _merge_sections(sections: list[NoteSection], conflicts: list[MergeConflict]) -> tuple[NoteSection, ...]:
    """Combine adjacent equivalent sections, keeping source order and references."""
    merged: list[NoteSection] = []
    explicit = {section.section_id for section in sections if section.section_id}
    seen_ids: dict[str, NoteSection] = {}
    conflicting_ids: set[str] = set()
    parent_by_topic: dict[str, str | None] = {}
    for section in sections:
        if not section.has_content:
            continue
        topic = section.topic_id
        if topic and topic in parent_by_topic and parent_by_topic[topic] != section.parent_topic_id:
            conflicts.append(MergeConflict("topic_parent", (topic,)))
        elif topic:
            parent_by_topic[topic] = section.parent_topic_id
        identity = section.section_id
        if identity and identity in seen_ids:
            original = seen_ids[identity]
            same = (original == replace(section, source_locators=original.source_locators) and original.topic_id == topic
                    and original.parent_topic_id == section.parent_topic_id)
            if not same:
                # Do not let aliases obscure two different sections. Preserve
                # both, allocate a deterministic ID, report the collision.
                counter = 2
                while f"{identity}~{counter}" in explicit:
                    counter += 1
                alias = f"{identity}~{counter}"
                explicit.add(alias)
                conflicting_ids.update((identity, alias))
                conflicts.append(MergeConflict("section_id", (identity, alias)))
                section = replace(section, section_id=alias)
            elif merged and merged[-1].section_id == identity and _same_logical_section(merged[-1], section):
                merged[-1] = _combine_sections(merged[-1], section)
                continue
            else:
                # An explicitly repeated ID away from its original position is
                # still kept in source order with its own record.
                counter = 2
                while f"{identity}~{counter}" in explicit:
                    counter += 1
                alias = f"{identity}~{counter}"
                explicit.add(alias)
                section = replace(section, section_id=alias)
        elif identity:
            seen_ids[identity] = section
        if (merged and merged[-1].section_id not in conflicting_ids and section.section_id not in conflicting_ids
                and _same_logical_section(merged[-1], section)):
            merged[-1] = _combine_sections(merged[-1], section)
        else:
            merged.append(section)
    return tuple(merged)


_SYNTHESIS_NUMBER = re.compile(r"(?<![\w.])[+−-]?\d+(?:[.٫]\d+)?(?![\d.])")


def _section_reference(section: NoteSection, position: int) -> str:
    return section.section_id or f"@position:{position}"


def _potential_value_conflicts(sections: Sequence[NoteSection]) -> list[MergeConflict]:
    """Find literal value variants, not semantic contradictions or a winner."""
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    conflicts: list[MergeConflict] = []
    for index, section in enumerate(sections, 1):
        ref = _section_reference(section, index)
        facts = (*section.paragraphs, *section.bullets, *section.key_points, *section.formulas)
        for fact in facts:
            text = _literal_key(fact.rstrip("."))
            numbers = tuple(_SYNTHESIS_NUMBER.finditer(text))
            if len(numbers) != 1:
                continue
            match = numbers[0]
            left, right = text[:match.start()], text[match.end():]
            if not (left.strip() and right.strip()):
                continue
            value = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in match[0])
            value = value.replace("٫", ".").replace("−", "-").lstrip("+")
            if "." in value:
                value = value.rstrip("0").rstrip(".")
            first_value, first_ref = seen.setdefault((left, right), (value, ref))
            if first_value != value:
                conflicts.append(MergeConflict("potential_value_conflict", (first_ref, ref)))
    return conflicts


def _merge_summaries(notes: list[StructuredNotes]) -> str:
    # Whole summaries only: matching sentences can play different roles in
    # their respective explanations, and punctuation is not a safe split.
    return " ".join(_dedupe_by_key([item.summary for item in notes if item.summary], _literal_key))


def _dedupe_glossary(notes: list[StructuredNotes]) -> tuple[GlossaryEntry, ...]:
    by_key: dict[tuple[str, str, str], GlossaryEntry] = {}
    for note in notes:
        for entry in note.glossary:
            key = (_literal_key(entry.term), _literal_key(entry.definition), _literal_key(entry.term_en))
            by_key.setdefault(key, entry)
    return tuple(by_key.values())


def _dedupe_practice_questions(questions: Sequence[PracticeQuestion]) -> tuple[PracticeQuestion, ...]:
    by_key: dict[tuple[str, str, str], PracticeQuestion] = {}
    for item in questions:
        key = (_literal_key(item.question), _literal_key(item.answer), item.kind)
        by_key.setdefault(key, item)
    return tuple(by_key.values())


def _dedupe_tips(tips: Sequence[PracticalTip]) -> tuple[PracticalTip, ...]:
    by_key: dict[tuple[str, str, str], PracticalTip] = {}
    for tip in tips:
        key = (_literal_key(tip.text), _literal_key(tip.basis), tip.kind)
        by_key.setdefault(key, tip)
    return tuple(by_key.values())


def merge_structured_notes(notes: list[StructuredNotes]) -> StructuredNotes:
    """Combine validated pieces in source order without paraphrasing their contents."""
    if not notes:
        raise StructuringError("پاسخ سرویس تولید جزوه خالی بود.")
    conflicts = [conflict for item in notes for conflict in item.merge_conflicts
                 if conflict.kind != "potential_value_conflict"]
    sections = _merge_sections([section for item in notes for section in item.sections], conflicts)
    conflicts.extend(_potential_value_conflicts(sections))
    if len(notes) == 1:
        if not notes[0].has_content:
            raise StructuringError("ساختار جزوهٔ دریافتی خالی بود.")
        only = notes[0]
        diagnostics = tuple(dict.fromkeys(conflicts))
        glossary = _dedupe_glossary(notes)
        if glossary == only.glossary and diagnostics == only.merge_conflicts and len(sections) == len(only.sections) and all(
            new is old for new, old in zip(sections, only.sections, strict=True)
        ):
            return only
        return replace(only, sections=sections, merge_conflicts=diagnostics, glossary=glossary)
    quality = max((item.quality_state for item in notes), key=lambda state: {
        QUALITY_VERIFIED: 0, QUALITY_REPAIRED: 1, QUALITY_DEGRADED: 2, QUALITY_RECOVERED: 3,
    }.get(state, 0))
    merged = StructuredNotes(
        quality_state=quality,
        title=next((item.title for item in notes if item.title), ""),
        subtitle=next((item.subtitle for item in notes if item.subtitle), ""),
        summary=_merge_summaries(notes),
        sections=sections,
        merge_conflicts=tuple(dict.fromkeys(conflicts)),
        input_ranges=tuple(dict.fromkeys(span for item in notes for span in item.input_ranges)),
        semantic_recoveries=tuple(dict.fromkeys(event for item in notes for event in item.semantic_recoveries)),
        failed_sections=tuple(dict.fromkeys(failure for item in notes for failure in item.failed_sections)),
        key_points=_dedupe_by_key(
            [point for item in notes for point in item.key_points], _literal_key
        ),
        glossary=_dedupe_glossary(notes),
        # CR-3: every merged booklet is FULL, regardless of legacy stored values.
        note_mode="full",
        learning_objectives=_dedupe_by_key(
            [item for note in notes for item in note.learning_objectives], _literal_key
        ),
        review_questions=_dedupe_by_key(
            [item for note in notes for item in note.review_questions], _literal_key
        ),
        practice_questions=_dedupe_practice_questions(
            [item for note in notes for item in note.practice_questions]
        ),
        practical_tips=_dedupe_tips([tip for note in notes for tip in note.practical_tips]),
        common_mistakes=_dedupe_by_key(
            [item for note in notes for item in note.common_mistakes], _literal_key
        ),
        final_review=_dedupe_by_key(
            [item for note in notes for item in note.final_review], _literal_key
        ),
    )
    if not merged.has_content:
        raise StructuringError("ساختار جزوهٔ دریافتی خالی بود.")
    return merged


# ===========================================================================
# JSON parsing
# ===========================================================================


def _load_json_payload(payload: dict, *, mode: str = "full") -> StructuredNotes:
    try:
        notes = StructuredNotes.from_payload(payload, provider_reply=True)
    except (KeyError, TypeError) as exc:
        raise StructuringError("پاسخ سرویس تولید جزوه ساختار معتبری نداشت.") from exc
    if not notes.sections:
        raise StructuringError("ساختار پاسخ جزوه بخش آموزشیِ قابل‌خواندن نداشت.")
    return notes


# ===========================================================================
# Deterministic lecture context (no LLM call: chunk openings)
# ===========================================================================

_SLIDE_TOPIC_HEADING_RE = re.compile(
    r"(?m)^###\s+((?:ادامهٔ\s+)?اسلاید\s+[0-9۰-۹٠-٩]+(?:\s*[—–-]\s*[^\n]+)?|"
    r"(?:ادامهٔ\s+)?گفتار بدون نگاشت مطمئن به اسلاید)"
)
_LEADING_WRAPPER_HEADINGS_RE = re.compile(r"^(?:\s*#{1,6}\s+[^\n]+\n+)+")


@dataclass(frozen=True, slots=True)
class LectureContext:
    """Bounded chunk orientation, separate from the complete content plan."""

    title: str = ""
    topics: tuple[str, ...] = ()
    plan: ContentPlan | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.title or self.topics)

    def topic_for(self, index: int, total: int) -> str:
        if 0 < index <= len(self.topics):
            return self.topics[index - 1]
        return ""


_SENTENCE_SPAN = re.compile(r"(?:[!?؟…]+|؛|(?<!\d)\.(?!\d))[»)\]”\"']*")


def _chunk_topic_summary(chunk: str) -> str:
    """Summarize the topics of one chunk from its slide headings or opening sentence."""
    slide_headings = [
        " ".join(match.split())
        for match in _SLIDE_TOPIC_HEADING_RE.findall(chunk)
        if match.strip()
    ]
    slide_headings = list(dict.fromkeys(slide_headings))
    if slide_headings:
        summary = "؛ ".join(slide_headings)
        if len(summary) > PRESENTATION_CHUNK_TOPIC_MAX_CHARS:
            summary = summary[:PRESENTATION_CHUNK_TOPIC_MAX_CHARS].rsplit("؛", 1)[0] + "؛ …"
        return summary
    stripped = chunk.strip()
    unwrapped = _LEADING_WRAPPER_HEADINGS_RE.sub("", stripped).strip() or stripped
    opening = unwrapped[:CONTEXT_HEAD_CHARS]
    first_sentence = opening
    match = _SENTENCE_SPAN.search(opening)
    if match and match.end() >= 10:
        first_sentence = opening[: match.end()]
    return " ".join(first_sentence.split())


def build_context(
    chunks: list[str], *, plan: ContentPlan | None = None,
    source_ranges: Sequence[SourceRange] = (),
) -> LectureContext:
    """Project a full source plan onto the existing positional prompt digest.

    Callers without source ranges keep the original slide/opening hints.
    Prompt limits never truncate or change the canonical plan.
    """
    if plan is not None and len(source_ranges) != len(chunks):
        raise ValueError("context source ranges must align with chunks")
    by_block = {
        block_id: topic.title for topic in (plan.topics if plan is not None else ())
        for block_id in topic.source_block_ids
    }
    topics: list[str] = []
    used_chars = 0
    for index, chunk in enumerate(chunks):
        topic = ""
        if plan is not None:
            titles = dict.fromkeys(by_block[block_id] for block_id in source_ranges[index].block_ids if block_id in by_block)
            for title in titles:
                addition = ("؛ " if topic else "") + title
                if len(topic) + len(addition) > PRESENTATION_CHUNK_TOPIC_MAX_CHARS:
                    break
                topic += addition
        if not topic:
            topic = _chunk_topic_summary(chunk)
        if not topic:
            # Keep one slot per input chunk: topic_for(index) is positional.
            topics.append("")
            continue
        position = to_persian_digits(len(topics) + 1)
        rendered_topic = f"({position}) {topic}"
        separator_chars = 2 if used_chars else 0  # the Persian comma and following space
        addition_chars = separator_chars + len(rendered_topic)
        if used_chars + addition_chars > CONTEXT_DIGEST_MAX_CHARS:
            # Preserve positional alignment even after the digest size cap.
            topics.append("")
            continue
        topics.append(topic)
        used_chars += addition_chars
    return LectureContext(title=plan.title if plan is not None else "", topics=tuple(topics), plan=plan)


def context_block_for(
    context: LectureContext, documents: list[str], index: int
) -> str:
    """The orientation block for part ``index``: the topic map and neighbours.

    The topic of *this* part is not repeated here — the chunk template appends
    it after the text (``build_chunk_message``), so it appears exactly once in
    the request. The neighbour windows are short sentences so a boundary
    sentence can be continued rather than restarted; they are never long enough
    to be noted again.
    """
    total = len(documents)
    lines: list[str] = []
    listed_topics = [
        f"({to_persian_digits(position)}) {topic}"
        for position, topic in enumerate(context.topics, start=1)
        if topic
    ]
    if listed_topics:
        lines.append("موضوع‌های درس به ترتیب: " + "، ".join(listed_topics))
    if index > 1:
        previous_tail = documents[index - 2].strip()[-NEIGHBOUR_CONTEXT_CHARS:].strip()
        if previous_tail:
            lines.append("چند جملهٔ پایانی بخش پیشین (فقط برای پیوند): … " + previous_tail)
    if index < total:
        next_head = documents[index].strip()[:NEIGHBOUR_CONTEXT_CHARS].strip()
        if next_head:
            lines.append("چند جملهٔ آغاز بخش بعدی (فقط برای پیوند): " + next_head + " …")
    return "\n".join(lines)


# ===========================================================================
# Completeness validator & coverage-gap fill (CR-4)
# ===========================================================================

# A 60% source match is a partial summary, not a complete booklet. These
# floors operate on instructional units and hard signals, never character
# similarity; a source-preserving fallback remains available after bounded repair.
COMPLETENESS_THRESHOLD = 0.85
#: A source unit counts as covered when a substantial share of its content words
#: appears in the draft (or its topic clearly overlaps a section heading).
COMPLETENESS_UNIT_RATIO = 0.65
FINAL_SENTENCE_COVERAGE_FLOOR = 0.85
#: Hard bound on targeted fill requests per booklet (a cost/loop guard).
MAX_COVERAGE_GAPS = 6
#: Characters of source text handed to one targeted fill request.
COVERAGE_GAP_CHARS = 700

#: Skeleton blocks that must all be present for a booklet to be "complete".
#: Structural blocks (cover, objectives, TOC, sections, key points, end summary,
#: glossary) are mandatory; the instructional block types (formulas, tables,
#: ...) are reported but optional because not every lecture contains all of them.
REQUIRED_SKELETON_BLOCKS: tuple[str, ...] = (
    "cover_title", "objectives", "toc", "sections",
    "key_points", "end_summary", "glossary",
)


@dataclass(frozen=True, slots=True)
class CompletenessReport:
    """A pure, deterministic assessment of a booklet against its source."""

    total_units: int
    covered_units: int
    coverage: float
    threshold: float
    skeleton: dict
    skeleton_complete: bool
    missing_units: tuple[str, ...]

    @property
    def meets_threshold(self) -> bool:
        return self.skeleton_complete and self.coverage >= self.threshold

    def summary(self) -> str:
        return (
            f"units={self.covered_units}/{self.total_units} "
            f"coverage={self.coverage:.2f} threshold={self.threshold:.2f} "
            f"skeleton={'ok' if self.skeleton_complete else 'incomplete'} "
            f"gaps={len(self.missing_units)}"
        )


# Presentation extractors preserve their own structure so the model can orient
# itself, but these labels are not lesson facts and must never become required
# booklet content. Keep the actual slide/narration prose intact; only explicit
# source mechanics (speaker identity and semester/session labels) are removed.
_SOURCE_METADATA_LINE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:گوینده|speaker|نیم[\s‌-]*سال)\s*[:：]",
    re.IGNORECASE,
)
_SPEAKER_NOTE_PREFIX = re.compile(r"^(\s*)یادداشت\s+گوینده\s*[:：]\s*", re.IGNORECASE)
_SLIDE_PREFIX = re.compile(
    r"^(\s*)#{1,6}\s*اسلاید\s*[0-9۰-۹٠-٩]+\s*[—–-]?\s*", re.IGNORECASE
)
_LESSON_LABEL_PREFIX = re.compile(r"^(\s*)[-*•]\s*درس\s*[:：]\s*", re.IGNORECASE)


def instructional_source_text(source: str) -> str:
    """Return source prose eligible for booklet grounding and fallback output.

    Operational presentation labels remain in the archived source/transcript;
    this projection only prevents them from becoming educational requirements or
    visible booklet text. It intentionally does not remove ordinary dates,
    numbers or speaker *content* embedded in lesson prose.
    """
    lines: list[str] = []
    for line in str(source or "").splitlines():
        if _SOURCE_METADATA_LINE.match(line):
            continue
        line = _SPEAKER_NOTE_PREFIX.sub(r"\1", line)
        line = _SLIDE_PREFIX.sub(r"\1", line)
        line = _LESSON_LABEL_PREFIX.sub(r"\1", line)
        lines.append(line)
    return "\n".join(lines).strip()


def booklet_skeleton(notes: StructuredNotes) -> dict[str, bool]:
    """Report which skeleton blocks are present in a structured booklet."""
    sections = list(notes.sections)
    headings = [section.heading.strip() for section in sections if section.heading.strip()]
    return {
        # cover + lesson title
        "cover_title": bool(notes.title.strip()),
        # overall lesson objective
        "objectives": bool(notes.learning_objectives) or bool(notes.summary.strip()),
        # table of contents (derived from section headings by the exporter)
        "toc": bool(headings),
        # logical sections in source order
        "sections": bool(sections),
        "definitions": any(section.definitions for section in sections),
        "examples": any(section.examples for section in sections),
        "steps": any(section.steps for section in sections),
        "formulas": any(section.formulas for section in sections),
        "tables": any(section.all_tables for section in sections),
        "callouts": any(section.callouts for section in sections),
        "key_points": bool(notes.key_points) or any(s.key_points for s in sections),
        # A closing review is a real canonical field, not a renderer duplicate.
        "end_summary": bool(notes.final_review),
        # English-term glossary is rendered from glossary or section definitions
        "glossary": bool(notes.glossary) or any(s.definitions for s in sections),
    }


def _drafted_text(notes: StructuredNotes) -> str:
    parts: list[str] = [notes.title, notes.subtitle, notes.summary]
    parts.extend(notes.learning_objectives)
    parts.extend(notes.key_points)
    parts.extend(notes.review_questions)
    parts.extend(f"{qa.question} {qa.answer}" for qa in notes.practice_questions)
    parts.extend(notes.common_mistakes)
    parts.extend(notes.final_review)
    parts.extend(f"{tip.text} {tip.basis}" for tip in notes.practical_tips)
    parts.extend(f"{entry.term} {entry.definition}" for entry in notes.glossary)
    parts.extend(section_plain_text(section) for section in notes.sections)
    return "\n".join(part for part in parts if part)


def _unit_is_covered(
    unit: str,
    drafted_words: frozenset[str],
    section_headings: list[str],
    *,
    unit_ratio: float,
) -> bool:
    words = content_words(unit)
    if not words:
        return True
    if len(words & drafted_words) / len(words) >= unit_ratio:
        return True
    # Reuse the topic-overlap helper: a section heading that clearly shares the
    # unit's topic still counts as coverage under heavy (grounded) paraphrase.
    lead = " ".join(unit.split()[:12])
    return any(headings_overlap(lead, heading) for heading in section_headings)


def _coverage_inputs(notes: StructuredNotes, unit_ratio: float):
    drafted_words = content_words(_drafted_text(notes))
    section_headings = [s.heading for s in notes.sections if s.heading.strip()]
    def covered(unit: str) -> bool:
        return _unit_is_covered(unit, drafted_words, section_headings, unit_ratio=unit_ratio)
    return covered


def find_coverage_gaps(
    source: str,
    notes: StructuredNotes,
    *,
    max_gaps: int = MAX_COVERAGE_GAPS,
    unit_ratio: float = COMPLETENESS_UNIT_RATIO,
) -> tuple[str, ...]:
    """Source instructional units that the drafted sections do not represent."""
    source = instructional_source_text(source)
    covered = _coverage_inputs(notes, unit_ratio)
    gaps: list[str] = []
    for unit in split_sentences(source):
        if not covered(unit):
            gaps.append(unit)
        if len(gaps) >= max_gaps * 3:  # bounded detection, still informative
            break
    return tuple(gaps)


def assess_completeness(
    source: str,
    notes: StructuredNotes,
    *,
    threshold: float = COMPLETENESS_THRESHOLD,
    unit_ratio: float = COMPLETENESS_UNIT_RATIO,
) -> CompletenessReport:
    """Validate a draft's skeleton and source coverage (pure, no LLM)."""
    source = instructional_source_text(source)
    units = split_sentences(source)
    covered = _coverage_inputs(notes, unit_ratio)
    missing = tuple(unit for unit in units if not covered(unit))
    covered_count = len(units) - len(missing)
    coverage = 1.0 if not units else covered_count / len(units)
    skeleton = booklet_skeleton(notes)
    skeleton_complete = all(skeleton[name] for name in REQUIRED_SKELETON_BLOCKS)
    return CompletenessReport(
        total_units=len(units),
        covered_units=covered_count,
        coverage=coverage,
        threshold=threshold,
        skeleton=skeleton,
        skeleton_complete=skeleton_complete,
        missing_units=missing,
    )


def meets_completeness_threshold(
    report: CompletenessReport, *, threshold: float | None = None
) -> bool:
    """True when both the skeleton and the coverage floor are satisfied."""
    floor = report.threshold if threshold is None else threshold
    return report.skeleton_complete and report.coverage >= floor


#: System prompt for one bounded, strictly grounded coverage-fill request. It is
#: the static booklet contract plus the fill rules, so the fill request shares
#: the cached prefix of every other note request.
COVERAGE_FILL_SYSTEM_PROMPT = notes_system_prompt() + COVERAGE_FILL_RULES

def build_fill_document(gaps, notes: StructuredNotes, source: str = "") -> str:
    """Targeted repair request with only source excerpts relevant to the gaps."""
    selected_gaps = list(gaps)[:MAX_COVERAGE_GAPS]
    units = "\n".join(
        "- " + str(gap)[:COVERAGE_GAP_CHARS] for gap in selected_gaps
    )
    excerpts: list[str] = []
    for gap in selected_gaps:
        words = [word for word in re.findall(r"[\w-]{4,}", str(gap)) if word]
        match_at = next((source.casefold().find(word.casefold()) for word in words
                         if source.casefold().find(word.casefold()) >= 0), -1)
        if match_at >= 0:
            excerpt = source[max(0, match_at - 250):match_at + 650].strip()
            if excerpt and excerpt not in excerpts:
                excerpts.append(excerpt)
    source_excerpt = "\n\n".join(excerpts[:MAX_COVERAGE_GAPS])[:8_000]
    headings = "، ".join(s.heading for s in notes.sections if s.heading.strip())[:600]
    return (
        "### واحد آموزشی جاافتاده\n" + units + "\n\n"
        "### گزیده‌های مرتبط از متن اصلی\n" + (source_excerpt or "(برای این شکاف گزیدهٔ متنی پیدا نشد)") + "\n\n"
        "### عنوان بخش‌های فعلی جزوه\n" + (headings or "(هنوز بخشی نیست)") + "\n\n"
        "### دستور\n"
        "فقط همین واحد را — همان‌گونه که در متن منبع آمده — در قالب JSON جزوه برگردان تا به "
        "بخش‌ها افزوده شود. هیچ مطلب تازه‌ای اضافه نکن."
    )


async def fill_coverage_gaps(
    session: aiohttp.ClientSession,
    settings: Settings,
    source: str,
    notes: StructuredNotes,
    *,
    semaphore: asyncio.Semaphore | None = None,
) -> StructuredNotes:
    """Compatibility entry point; coverage now uses the same per-node cap."""
    from .booklet_verification import verify_and_repair

    document = NormalizedDocument.from_source_text(source)
    plan = plan_from_document(document)
    if len(plan.topics) == 1 and not any(section.topic_id for section in notes.sections):
        topic = plan.topics[0]
        notes = replace(notes, sections=tuple(replace(
            section, topic_id=topic.id, source_block_ids=topic.unit_ids,
        ) for section in notes.sections))
    router = SectionWriter(replace(settings, note_completeness_fill=True), session, semaphore=semaphore)
    return await verify_and_repair(notes, document, plan, router)


# ===========================================================================
# Compile (reduce) pass
# ===========================================================================


def section_payload(section: NoteSection) -> dict:
    """One section as the compact payload: empty fields are omitted."""
    item: dict = {"heading": section.heading}
    if section.source_locators:
        item["source_locators"] = list(section.source_locators)
    if section.paragraphs:
        item["paragraphs"] = list(section.paragraphs)
    if section.bullets:
        item["bullets"] = list(section.bullets)
    if section.definitions:
        item["definitions"] = [
            {"term": d.term, "term_en": d.term_en, "definition": d.definition}
            for d in section.definitions
        ]
    if section.examples:
        item["examples"] = list(section.examples)
    if section.steps:
        item["steps"] = list(section.steps)
    if section.formulas:
        item["formulas"] = list(section.formulas)
    if section.key_points:
        item["key_points"] = list(section.key_points)
    if section.all_tables:
        item["tables"] = [
            {
                "caption": table.caption,
                "headers": list(table.headers),
                "rows": [list(row) for row in table.rows],
            }
            for table in section.all_tables
        ]
    if section.callouts:
        item["callouts"] = [{"kind": c.kind, "text": c.text} for c in section.callouts]
    return item


def section_plain_text(section: NoteSection) -> str:
    """All searchable text of a section, for deterministic relevance tests."""
    parts: list[str] = [section.heading]
    parts.extend(section.paragraphs)
    parts.extend(section.bullets)
    parts.extend(f"{d.term} {d.term_en} {d.definition}" for d in section.definitions)
    parts.extend(section.examples)
    parts.extend(section.steps)
    parts.extend(section.formulas)
    parts.extend(section.key_points)
    for table in section.all_tables:
        if table.caption:
            parts.append(table.caption)
        parts.append(" ".join(table.headers))
        parts.extend(" ".join(row) for row in table.rows)
    parts.extend(callout.text for callout in section.callouts)
    return "\n".join(part for part in parts if part)


def compact_notes_payload(notes: StructuredNotes) -> dict:
    """The notes as a compact payload: empty fields are omitted."""
    payload: dict = {}
    if notes.title:
        payload["title"] = notes.title
    if notes.subtitle:
        payload["subtitle"] = notes.subtitle
    if notes.summary:
        payload["summary"] = notes.summary
    if notes.learning_objectives:
        payload["learning_objectives"] = list(notes.learning_objectives)
    sections: list[dict] = [section_payload(section) for section in notes.sections]
    payload["sections"] = sections
    if notes.key_points:
        payload["key_points"] = list(notes.key_points)
    if notes.practice_questions:
        payload["practice_questions"] = [
            {"question": qa.question, "answer": qa.answer, "kind": qa.kind}
            for qa in notes.practice_questions
        ]
    if notes.review_questions:
        payload["review_questions"] = list(notes.review_questions)
    if notes.common_mistakes:
        payload["common_mistakes"] = list(notes.common_mistakes)
    if notes.final_review:
        payload["final_review"] = list(notes.final_review)
    if notes.practical_tips:
        payload["practical_tips"] = [
            {"text": tip.text, "basis": tip.basis, "kind": tip.kind}
            for tip in notes.practical_tips
        ]
    if notes.glossary:
        payload["glossary"] = [
            {"term": e.term, "term_en": e.term_en, "definition": e.definition}
            if e.term_en
            else {"term": e.term, "definition": e.definition}
            for e in notes.glossary
        ]
    return payload


def build_compile_document(context: LectureContext, notes: StructuredNotes) -> str:
    """Legacy draft envelope retained for callers sizing targeted repair."""
    return build_edit_message(build_source_digest(context), _notes_draft_json(notes))


def build_source_digest(context: LectureContext) -> str:
    """The editorial orientation map; it is not source evidence.

    The compile request does not re-send the whole transcript (it would blow
    the budget); it sends the deterministic topic map for ordering only. The
    editorial prompt explicitly forbids treating it as evidence for new facts.
    """
    listed_topics = [
        f"({to_persian_digits(position)}) {topic}"
        for position, topic in enumerate(context.topics, start=1)
        if topic
    ]
    if not listed_topics:
        return "موضوع‌های درس به ترتیب در دسترس نیست."
    return "موضوع‌های درس به ترتیب: " + "، ".join(listed_topics)


def _notes_draft_json(notes: StructuredNotes) -> str:
    return json.dumps(compact_notes_payload(notes), ensure_ascii=False)


def _notes_structured_item_count(notes: StructuredNotes) -> int:
    return (
        sum(
            len(section.definitions)
            + len(section.formulas)
            + len(section.steps)
            + len(section.examples)
            + len(section.all_tables)
            + len(section.callouts)
            for section in notes.sections
        )
        + len(notes.practical_tips)
        + len(notes.practice_questions)
    )


def _notes_body_chars(notes: StructuredNotes) -> int:
    total = 0
    for section in notes.sections:
        total += sum(len(p) for p in section.paragraphs)
        total += sum(len(b) for b in section.bullets)
        total += sum(len(d.term) + len(d.definition) for d in section.definitions)
        total += sum(len(e) for e in section.examples)
        total += sum(len(s) for s in section.steps)
        total += sum(len(f) for f in section.formulas)
        total += sum(len(k) for k in section.key_points)
        total += sum(len(c.text) for c in section.callouts)
        for table in section.all_tables:
            total += sum(len(h) for h in table.headers)
            total += sum(len(cell) for row in table.rows for cell in row)
    total += sum(len(tip.text) + len(tip.basis) for tip in notes.practical_tips)
    total += sum(len(qa.question) + len(qa.answer) for qa in notes.practice_questions)
    return total


def compile_accepts(compiled: StructuredNotes, merged: StructuredNotes) -> bool:
    """Check educational payload and owning references, not a size/shape proxy.

    Additive excerpts and annotations are validated separately by the edit
    interpreter. Original ordered bodies, steps, tables and booklet metadata
    cannot be replaced, even by a candidate with the same apparent coverage.
    """
    if not compiled.has_content or len(compiled.sections) != len(merged.sections):
        return False
    if replace(compiled, sections=(), title=merged.title, quality_state=merged.quality_state) != replace(merged, sections=()):
        return False
    if (compiled.input_ranges != merged.input_ranges or compiled.semantic_recoveries != merged.semantic_recoveries
            or compiled.failed_sections != merged.failed_sections or compiled.merge_conflicts != merged.merge_conflicts):
        return False

    def identity(section: NoteSection) -> tuple:
        return (section.section_id, section.topic_id, section.parent_topic_id,
                tuple(dict.fromkeys(section.paragraphs)), tuple(dict.fromkeys(section.bullets)),
                tuple(section.definitions), tuple(section.examples),
                tuple(section.steps), tuple(section.formulas))

    by_body: dict[tuple, list[NoteSection]] = {}
    for section in compiled.sections:
        by_body.setdefault(identity(section), []).append(section)
    for original in merged.sections:
        candidates = by_body.get(identity(original), [])
        for index, candidate in enumerate(candidates):
            if (set(original.key_points) <= set(candidate.key_points)
                    and set(original.callouts) <= set(candidate.callouts)
                    and all(table in candidate.all_tables for table in original.all_tables)
                    and set(original.source_locators) <= set(candidate.source_locators)
                    and set(original.source_block_ids) <= set(candidate.source_block_ids)):
                candidates.pop(index)
                break
        else:
            return False
    return True


# ===========================================================================
# Grounding gate and one-pass repair
# ===========================================================================


def _supported_by_source(text: str, source_words: frozenset[str], *, minimum: float) -> bool:
    """Whether a model-written sentence is backed by the source's vocabulary.

    Used for the two fields that invite outside knowledge —
    ``learning_objectives`` and ``review_questions``. A field whose content
    words are largely absent from the lecture is not "derived from the text";
    it is the model's own study advice, and the strict policy drops it.
    """
    words = content_words(text)
    if not words:
        return False
    return len(words & source_words) / len(words) >= minimum


def gate_notes(
    notes: StructuredNotes,
    source: str,
    *,
    strict: bool = True,
    mode: str = "full",
) -> tuple[StructuredNotes, NoteQualityReport]:
    """Drop what the source cannot support, then audit what remains.

    * a table with an untraceable numeric cell — or with more than a quarter of
      its text cells untraceable — is removed (it is never "fixed");
    * a practical tip without a verbatim basis is removed;
    * objectives and review questions without source vocabulary are removed;
    * the surviving document is audited for omissions and fabrications.

    Dropping is deliberately preferred to rewriting: a table or tip that fails
    the check carries the exact failure the check exists for, and the prose
    still states the fact (the audit reports it as missing if it does not).

    With ``strict=False`` the validators still run and the counters still say
    what *would* be dropped, but nothing is removed and no objective or review
    question is filtered. That is the ``warn`` mode: an audit of the real
    deployment without any effect on what the user receives.
    """
    resolved_mode = resolve_note_mode(mode)
    source = instructional_source_text(source)
    source_index = build_source_index(source)
    source_words = source_index.masked_words
    tables_seen = tables_accepted = tips_seen = tips_accepted = 0
    locators_rejected = 0
    dropped_cells: list[str] = []
    sections: list[NoteSection] = []
    for section in notes.sections:
        accepted_tables: list[NoteTable] = []
        for table in section.all_tables:
            tables_seen += 1
            verdict = validate_table(table, source, source_index=source_index)
            if not verdict.accepted:
                dropped_cells.extend(verdict.failed_cells)
                logger.info(
                    "note table%s reason=%s cells=%d",
                    " dropped" if strict else " would be dropped",
                    verdict.reason,
                    len(verdict.failed_cells),
                )
                if strict:
                    continue
            else:
                tables_accepted += 1
            accepted_tables.append(table)
        locators = tuple(
            locator for locator in section.source_locators
            if normalize_for_compare(locator) in source_index.compare_source
        )
        locators_rejected += len(section.source_locators) - len(locators)
        cleaned_section = replace(
            section,
            table=accepted_tables[0] if accepted_tables else None,
            tables=tuple(accepted_tables[1:]),
            source_locators=locators if strict else section.source_locators,
        )
        if cleaned_section.has_content or not strict:
            sections.append(cleaned_section)

    tips: list[PracticalTip] = []
    for tip in notes.practical_tips:
        tips_seen += 1
        if not validate_tip(tip, source, source_index=source_index):
            logger.info(
                "note tip%s: basis not found in the source",
                " dropped" if strict else " would be dropped",
            )
            if strict:
                continue
        else:
            tips_accepted += 1
        tips.append(tip)

    objectives = notes.learning_objectives
    review_questions = notes.review_questions
    if strict:
        objectives = tuple(
            item
            for item in objectives
            if _supported_by_source(item, source_words, minimum=0.7)
        )
        review_questions = tuple(
            item
            for item in review_questions
            if _supported_by_source(item, source_words, minimum=0.6)
        )

    gated = replace(
        notes,
        sections=tuple(sections),
        # The model emits its tips in importance order; the cap keeps the first
        # ones. Sorting by ``len(basis)`` (the old behaviour) promoted tips with
        # the longest justification, not the most useful advice.
        practical_tips=tuple(tips[:MAX_PRACTICAL_TIPS] if strict else tips),
        learning_objectives=objectives,
        review_questions=review_questions,
    )
    audit = audit_notes(source, gated, source_index=source_index)
    report = NoteQualityReport(
        audit=audit,
        coverage=sentence_coverage(source, gated, source_index=source_index),
        tables_seen=tables_seen,
        tables_accepted=tables_accepted,
        tables_rejected=tables_seen - tables_accepted,
        tips_seen=tips_seen,
        tips_accepted=tips_accepted,
        tips_rejected=tips_seen - tips_accepted,
        objectives_dropped=len(notes.learning_objectives) - len(objectives),
        review_questions_dropped=len(notes.review_questions) - len(review_questions),
        locators_rejected=locators_rejected,
        dropped_cells=tuple(dict.fromkeys(dropped_cells)),
        # The signal audit only sees numbers, dates, Latin terms and URLs; this
        # is what an invented Persian-only sentence has to pass as well.
        unsupported_claims=prose_claim_evidence(
            source, gated, source_index=source_index
        ),
    )
    logger.info("note quality %s mode=%s", report.summary(), resolved_mode)
    return gated, report


def notes_need_repair(report: NoteQualityReport, *, mode: str = "full") -> bool:
    """Whether a bounded repair request is justified.

    A fabrication always is (faithfulness is a hard gate). Missing numbers are
    a defect below the mode's recall floor, not a matter of taste. Prose
    coverage is a soft signal: it only asks for repair when it is clearly low,
    so a legitimately paraphrased booklet is not sent again.
    """
    number_floor, term_floor = coverage_thresholds(resolve_note_mode(mode))
    audit = report.audit
    return bool(
        audit.fabricated
        or report.unsupported_claims
        or audit.number_coverage < number_floor
        or audit.term_coverage < term_floor
        or report.coverage.ratio < FINAL_SENTENCE_COVERAGE_FLOOR
    )


def safety_failure(report: NoteQualityReport) -> str | None:
    """Return an unsupported-claim failure that cannot wait for coverage fill."""
    if report.audit.fabricated:
        return "unsupported_facts"
    if report.unsupported_claims:
        return "unsupported_claims"
    return None


def acceptance_failure(report: NoteQualityReport) -> str | None:
    """Return the critical grounding/completeness defect, if any.

    The final gate is intentionally stricter than a repair hint: a delivery
    cannot claim to be a complete booklet while it has lost a source number,
    technical term, or a material share of instructional units.  Callers use a
    bounded repair first and then the source-preserving fallback rather than
    silently delivering a partial synthesis.
    """
    if safety := safety_failure(report):
        return safety
    number_floor, term_floor = coverage_thresholds("full")
    if report.audit.number_coverage < number_floor:
        return "missing_numbers"
    if report.audit.term_coverage < term_floor:
        return "missing_terms"
    if report.coverage.ratio < FINAL_SENTENCE_COVERAGE_FLOOR:
        return "low_instructional_coverage"
    return None


_BOOKLET_TIMELINE_MARKER = re.compile(
    r"(?:\[|\(|در\s+(?:دقیقه|ثانیه|زمان))\s*[0-9۰-۹٠-٩]{1,3}\s*[:٫]\s*[0-9۰-۹٠-٩]{2}"
)
_BOOKLET_SPEAKER_MARKER = re.compile(
    r"(?:یادداشت\s+گوینده\s*[:：]|(?:گوینده|speaker)\s*[:：]|"
    r"گوینده(?:ٔ|\s+)\s*[0-9۰-۹٠-٩]+|speaker\s*\d+|"
    r"(?:استاد|سخنران|گوینده)\s+(?:گفت|ذکر کرد|بیان کرد|اشاره کرد))",
    re.IGNORECASE,
)
_BOOKLET_SOURCE_METADATA_MARKER = re.compile(
    r"(?:نیم[\s‌-]*سال\s*[:：]|اسلاید\s*[0-9۰-۹٠-٩]+)",
    re.IGNORECASE,
)


def booklet_output_violations(notes: StructuredNotes) -> tuple[str, ...]:
    """Detect transcript/timeline leakage in educational content, not STT data."""
    if any(name in FORBIDDEN_BOOKLET_METADATA_FIELDS for name in notes.__dataclass_fields__):
        return ("forbidden_booklet_field",)
    violations: list[str] = []
    text = json.dumps(notes.to_payload(), ensure_ascii=False)
    if _BOOKLET_TIMELINE_MARKER.search(text):
        violations.append("timeline_marker")
    if _BOOKLET_SPEAKER_MARKER.search(text):
        violations.append("speaker_reference")
    if _BOOKLET_SOURCE_METADATA_MARKER.search(text):
        violations.append("source_metadata_reference")
    return tuple(dict.fromkeys(violations))


def structurally_valid_booklet(notes: StructuredNotes) -> bool:
    """Mandatory schema/content check shared by renderers and final acceptance."""
    if not notes.title.strip() or normalize_for_compare(notes.title) in _GENERIC_BOOKLET_TITLES:
        return False
    if not notes.sections or not notes.has_content or not notes.final_review:
        return False
    if booklet_output_violations(notes):
        return False
    for section in notes.sections:
        if not section.heading.strip():
            return False
        if not section_plain_text(replace(section, heading="")).strip():
            return False
    return True


def completeness_failure(report: NoteQualityReport, *, mode: str = "full") -> str | None:
    """A user-facing PARTIAL reason, never a hard acceptance rejection.

    Recall floors and lexical sentence coverage are diagnostic only. They may
    trigger a bounded repair or a partial-result notice, but do not make a safe
    structured booklet disappear.
    """
    _ = resolve_note_mode(mode)  # retained for API compatibility
    audit = report.audit
    if any(signal.kind == "number" for signal in audit.missing):
        return "missing_numbers"
    if audit.missing:
        return "missing_terms"
    if report.coverage.ratio < FINAL_SENTENCE_COVERAGE_FLOOR:
        return "low_instructional_coverage"
    return None


def enforce_final_acceptance(
    notes: StructuredNotes,
    report: NoteQualityReport,
    *,
    mode: str = "full",
    stage: str = "final",
    allow_incomplete: bool = False,
    check_grounding: bool = True,
) -> StructuredNotes:
    """Return *notes*, or raise :class:`NoteQualityError`.

    This is the final strict gate for an accepted synthesis baseline. Optional
    editorial edits must subsequently preserve the artifact and pass the same checks.
    ``allow_incomplete`` permits safe partial delivery; completeness still has
    a separate report. ``stage`` is diagnostic only.
    """
    reason = ((safety_failure(report) if allow_incomplete else acceptance_failure(report))
              if check_grounding else None)
    if reason is None and not structurally_valid_booklet(notes):
        violations = booklet_output_violations(notes)
        reason = violations[0] if violations else "invalid_booklet_structure"
    if reason is None:
        return notes
    logger.warning(
        "note acceptance rejected stage=%s reason=%s fabricated=%d missing=%d mode=%s",
        stage,
        reason,
        len(report.audit.fabricated),
        len(report.audit.missing),
        resolve_note_mode(mode),
    )
    raise NoteQualityError(
        "جزوهٔ ساخته‌شده با متن منبع راستی‌آزمایی نشد؛ تحویل داده نمی‌شود و "
        "متن خام درس در دسترس است.",
        reason=reason,
        fabricated=len(report.audit.fabricated),
    )


def _iter_section_text_fields(section: NoteSection, section_index: int):
    """Yield displayable section fields with stable paths for issue reports."""
    yield section_index, f"sections[{section_index}].heading", section.heading
    for field_name in (
        "paragraphs", "bullets", "examples", "steps", "formulas", "key_points",
        "source_locators",
    ):
        for item_index, value in enumerate(getattr(section, field_name)):
            yield section_index, f"sections[{section_index}].{field_name}[{item_index}]", value
    for item_index, definition in enumerate(section.definitions):
        yield section_index, f"sections[{section_index}].definitions[{item_index}].term", definition.term
        yield section_index, f"sections[{section_index}].definitions[{item_index}].definition", definition.definition
    for table_index, table in enumerate(section.all_tables):
        for header_index, value in enumerate(table.headers):
            yield section_index, f"sections[{section_index}].tables[{table_index}].headers[{header_index}]", value
        for row_index, row in enumerate(table.rows):
            for cell_index, value in enumerate(row):
                yield section_index, f"sections[{section_index}].tables[{table_index}].rows[{row_index}][{cell_index}]", value
    for item_index, callout in enumerate(section.callouts):
        yield section_index, f"sections[{section_index}].callouts[{item_index}].text", callout.text


def _signal_matches(candidate: object, expected: object) -> bool:
    return (
        getattr(candidate, "kind", "") == getattr(expected, "kind", "")
        and getattr(candidate, "value", "") == getattr(expected, "value", "")
        and (
            not getattr(candidate, "unit", "")
            or not getattr(expected, "unit", "")
            or getattr(candidate, "unit", "") == getattr(expected, "unit", "")
        )
    )


def _signal_occurrence(notes: StructuredNotes, signal: object) -> tuple[int, str, str]:
    """Locate the first artifact field that asserts *signal*, if any."""
    for section_index, field, text in (
        item
        for index, section in enumerate(notes.sections)
        for item in _iter_section_text_fields(section, index)
    ):
        if any(_signal_matches(found, signal) for found in extract_signals(text, mask=False)):
            return section_index, field, text[:1_000]
    return -1, "", ""


def _section_for_evidence(notes: StructuredNotes, evidence: str) -> int:
    """Choose the least-surprising section for a source-only addition."""
    words = content_words(evidence)
    if not words or not notes.sections:
        return -1
    scores = [len(words & content_words(section_plain_text(section))) for section in notes.sections]
    best = max(scores, default=0)
    return scores.index(best) if best else -1


def build_repair_issues(
    notes: StructuredNotes,
    report: NoteQualityReport,
    source: str,
    *,
    limit: int = MAX_REPAIR_SIGNALS,
) -> tuple[RepairIssue, ...]:
    """Normalize evidence findings into minimal, issue-specific repair commands.

    The returned data intentionally includes the offending span and its source
    window. It is used for provider input only; logs report codes and counts,
    never source or artifact text.
    """
    issues: list[RepairIssue] = []
    audit = report.audit
    evidence_by_signal = {
        (signal.kind, signal.value, signal.unit): window
        for signal, window in evidence_windows(
            source, [*audit.missing, *audit.fabricated], radius=REPAIR_WINDOW_CHARS
        )
    }
    for signal in audit.fabricated:
        section_index, field, current = _signal_occurrence(notes, signal)
        code = REPAIR_WRONG_NUMBER if signal.kind == "number" else REPAIR_WRONG_TERM
        risk = classify_claim_risk(current or signal.label)
        issues.append(
            RepairIssue(
                code=code,
                severity="critical",
                section_id=f"s{section_index + 1}" if section_index >= 0 else "",
                field=field,
                current_text=current or signal.label,
                source_evidence=evidence_by_signal.get((signal.kind, signal.value, signal.unit), ""),
                action="REMOVE_OR_REWRITE_FROM_SOURCE",
                source_signal_metadata=(
                    ("kind", signal.kind), ("label", signal.label),
                    ("value", signal.value), ("unit", signal.unit), ("risk", risk),
                ),
            )
        )
    for claim in sorted(report.unsupported_claims, key=lambda item: item.risk != "high"):
        source_evidence = source[claim.span[0]:claim.span[1]].strip() if claim.span[1] else ""
        issues.append(
            RepairIssue(
                code=REPAIR_UNSUPPORTED_CLAIM,
                severity="critical",
                section_id=f"s{claim.section_index + 1}" if claim.section_index >= 0 else "",
                field=claim.field,
                current_text=claim.claim[:1_000],
                source_evidence=source_evidence[:1_000],
                action="REMOVE_OR_REWRITE_FROM_SOURCE",
                source_signal_metadata=(
                    ("shared_ratio", f"{claim.shared_ratio:.3f}"), ("risk", claim.risk)
                ),
            )
        )
    for signal in audit.missing:
        code = REPAIR_MISSING_NUMBER if signal.kind == "number" else REPAIR_MISSING_TERM
        evidence = evidence_by_signal.get((signal.kind, signal.value, signal.unit), "")
        section_index = _section_for_evidence(notes, evidence)
        issues.append(
            RepairIssue(
                code=code,
                severity="high",
                section_id=f"s{section_index + 1}" if section_index >= 0 else "",
                field="",
                current_text="",
                source_evidence=evidence,
                action="ADD_FROM_SOURCE",
                source_signal_metadata=(
                    ("kind", signal.kind), ("label", signal.label),
                    ("value", signal.value), ("unit", signal.unit),
                ),
            )
        )
    # Signal coverage has priority. Sentence gaps are supplemental and are
    # capped so repair never becomes an unbounded source resend.
    if report.coverage.ratio < FINAL_SENTENCE_COVERAGE_FLOOR:
        for gap in find_coverage_gaps(source, notes, max_gaps=3):
            section_index = _section_for_evidence(notes, gap)
            issues.append(
                RepairIssue(
                    code=REPAIR_MISSING_CONTENT,
                    severity="medium",
                    section_id=f"s{section_index + 1}" if section_index >= 0 else "",
                    field="",
                    source_evidence=gap[:COVERAGE_GAP_CHARS],
                    action="ADD_FROM_SOURCE",
                )
            )
    if report.tables_rejected:
        issues.append(
            RepairIssue(
                code=REPAIR_TABLE,
                severity="medium",
                current_text="; ".join(report.dropped_cells[:4]),
                action="KEEP_REMOVED_UNLESS_SOURCE_SUPPORTS_A_VALID_TABLE",
            )
        )
    if report.locators_rejected:
        issues.append(
            RepairIssue(
                code=REPAIR_LOCATOR,
                severity="low",
                action="REMOVE_UNSUPPORTED_LOCATORS",
                source_signal_metadata=(("rejected", str(report.locators_rejected)),),
            )
        )
    # Preserve priority order while collapsing duplicate commands for the same
    # field/signal. A single command may carry several equivalent detector
    # observations but should never cause duplicate provider edits.
    unique: list[RepairIssue] = []
    seen: set[tuple[str, str, str, str]] = set()
    for issue in issues:
        key = (issue.code, issue.section_id, issue.field, issue.current_text)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    # High-risk medical assertions lead the bounded request, irrespective of
    # how many lower-risk term/coverage findings appeared before them.
    unique.sort(
        key=lambda issue: dict(issue.source_signal_metadata).get("risk") != "high"
    )
    return tuple(unique[:max(1, limit)])


def _repair_issues_for_pass(issues: Sequence[RepairIssue], pass_name: str) -> tuple[RepairIssue, ...]:
    if pass_name == "claim_repair":
        allowed = REPAIR_CRITICAL_CODES
    elif pass_name == "coverage_repair":
        allowed = REPAIR_COVERAGE_CODES
    else:
        allowed = REPAIR_STRUCTURAL_CODES
    return tuple(issue for issue in issues if issue.code in allowed)


def repair_target_indexes(
    notes: StructuredNotes,
    report: NoteQualityReport,
    source: str,
    *,
    limit: int = MAX_REPAIR_SECTIONS,
) -> list[int]:
    """Which sections a bounded repair request should carry, best first.

    A booklet above the repair budget cannot be re-sent whole, but the lost
    facts belong to particular sections. Sections whose text overlaps the source
    window of a missing or fabricated signal score highest (that is where the
    fact belongs); the remaining slots are filled in document order so a
    booklet-wide pattern is not hidden behind the first few sections. The caller
    presents the chosen sections in document order.
    """
    total = len(notes.sections)
    if total <= limit:
        return list(range(total))
    # Each section's words are computed once: the booklet can hold hundreds of
    # sections and every signal window would otherwise re-tokenize all of them.
    section_words = [
        content_words(section_plain_text(section)) for section in notes.sections
    ]
    scores = [0] * total
    signals = [*report.audit.missing, *report.audit.fabricated]
    for _signal, window in evidence_windows(source, signals, radius=REPAIR_WINDOW_CHARS):
        window_words = content_words(window)
        if not window_words:
            continue
        for index, words in enumerate(section_words):
            overlap = len(window_words & words)
            if overlap:
                scores[index] += overlap
    if not any(scores):
        # No signal locates itself in the booklet: spread the bounded request
        # across it instead of repairing only its opening sections, which is
        # where a booklet-wide pattern (a repeated term, a recurring unit) would
        # never be found.
        step = total / limit
        return [min(total - 1, int(position * step)) for position in range(limit)]
    return sorted(range(total), key=lambda index: (-scores[index], index))[:limit]


def _subset_notes(notes: StructuredNotes, sections: Sequence[NoteSection]) -> StructuredNotes:
    """The notes limited to ``sections`` (global fields kept: they are cheap)."""
    return replace(notes, sections=tuple(sections))


def build_repair_document(
    notes: StructuredNotes,
    report: NoteQualityReport,
    source: str,
    *,
    mode: str = "full",
    sections: Sequence[NoteSection] | None = None,
    issues: Sequence[RepairIssue] | None = None,
    pass_name: str = "claim_repair",
) -> str:
    """Build a bounded, command-driven edit request, never a generic rewrite.

    The provider receives only the affected section JSON, normalized issue
    records, and the sentence/window that supports each requested change. Valid
    sections and valid spans are explicitly immutable. When no correct source
    replacement is visible, the command requires removal rather than a guess.
    """
    selected_issues = tuple(issues or build_repair_issues(notes, report, source))
    selected_issues = selected_issues[:MAX_REPAIR_SIGNALS]
    targeted = sections is not None
    booklet = _subset_notes(notes, sections) if sections is not None else notes
    draft = DRAFT_HEADING + "\n" + json.dumps(compact_notes_payload(booklet), ensure_ascii=False)
    issue_json = json.dumps(
        [issue.to_payload() for issue in selected_issues], ensure_ascii=False, separators=(",", ":")
    )
    scope = (
        "فقط همین بخش‌ها را، به همان ترتیب و با همان عنوان، برگردانید؛ بخش‌های دیگر "
        "در این درخواست وجود ندارند و نباید ساخته شوند.\n"
        if targeted
        else ""
    )
    number_floor, term_floor = coverage_thresholds(resolve_note_mode(mode))
    issue_labels: list[str] = []
    if any(issue.code in REPAIR_COVERAGE_CODES for issue in selected_issues):
        issue_labels.append("### موارد جامانده از جزوه: فقط با شاهد منبعِ همان مسئله برگردانید.")
    if any(issue.code in REPAIR_CRITICAL_CODES for issue in selected_issues):
        issue_labels.append("### مواردی که در متن منبع نیست: حذف کنید یا فقط با شاهد درست بازنویسی کنید.")
    if any(dict(issue.source_signal_metadata).get("risk") == "high" for issue in selected_issues):
        issue_labels.append(
            "### اولویت ایمنی پزشکی: برای دوز، درمان، تشخیص یا آستانهٔ بالینی "
            "بدون شاهد دقیق فقط حذف کنید؛ مقدار یا توصیهٔ جایگزین نسازید."
        )
    return "\n\n".join(
        tuple(issue_labels) + (
            "### فرمان تعمیر\n"
            f"مرحله: {pass_name}\n"
            + scope
            + "شما جزوهٔ تازه تولید نمی‌کنید؛ فقط همین پیش‌نویس را ویرایش می‌کنید. "
            "کمترین بازهٔ لازم را تغییر دهید، همهٔ بخش‌ها و جمله‌های معتبر را حفظ کنید، "
            "ترتیب منبع را تغییر ندهید و فقط از شواهد زیر استفاده کنید. اگر جایگزین درست "
            "از شواهد پیدا نیست، ادعای بی‌پشتوانه را حذف کنید؛ هرگز حدس نزنید.\n"
            f"پوشش اعداد/اصطلاح‌ها فقط در صورت داشتن شاهد منبع می‌تواند به {number_floor:.0%}/"
            f"{term_floor:.0%} برسد.",
            "### مسئله‌های دقیق تعمیر (JSON)\n" + issue_json,
            "### قرارداد خروجی\n"
            "فقط یک شیء JSON معتبر با همان schema پیش‌نویس برگردانید. هیچ بخش، عدد، اصطلاح، "
            "مرجع، دوز، تاریخ یا جملهٔ تازه‌ای که در شواهد نیست اضافه نکنید.",
            draft,
        )
    )


_SCRUB_SENTENCE = re.compile(r"(?<=[.!?؟…])\s+")
_SCRUB_LOCAL_CLAUSE = re.compile(
    r"(?<=[،,؛;:])\s+|\s+(?=(?:و|اما|ولی|بلکه|سپس|همچنین|and|but|then)\s+)",
    re.IGNORECASE,
)
_SCRUB_LEADING_CONNECTOR = re.compile(
    r"^(?:و|اما|ولی|بلکه|سپس|همچنین|and|but|then)\s+", re.IGNORECASE
)


def _scrub_occurrence_matches(
    occurrence: Signal,
    rejected: frozenset[Signal | str],
) -> bool:
    for item in rejected:
        if isinstance(item, Signal):
            if (
                item.kind == occurrence.kind
                and item.value == occurrence.value
                and (item.kind != "number" or item.unit == occurrence.unit)
            ):
                return True
        elif item == occurrence.value:
            # Compatibility for older callers that supplied a value-only set.
            return True
    return False


def _clause_has_source_support(
    clause: str, source_sentence_words: Sequence[frozenset[str]]
) -> bool:
    words = content_words(clause)
    if not words:
        return True
    if not source_sentence_words:
        return False
    return max(
        (len(words & source_words) / len(words) for source_words in source_sentence_words),
        default=0.0,
    ) >= MIN_CLAIM_SUPPORT


def _remove_local_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    if not spans:
        return text
    chars = list(text)
    for start, end in sorted(set(spans), reverse=True):
        for index in range(max(0, start), min(len(chars), end)):
            if chars[index] not in "\r\n":
                chars[index] = " "
    cleaned = "".join(chars)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\(\s*\)|\[\s*\]", "", cleaned)
    cleaned = re.sub(r"\s+([،,؛;:])", r"\1", cleaned)
    cleaned = re.sub(r"([،,؛;:])\s*([،,؛;:])", r"\1", cleaned)
    return cleaned.strip()


def _scrub_text(
    text: str,
    *,
    fabricated_numbers: frozenset[Signal | str] = frozenset(),
    fabricated_terms: frozenset[Signal | str] = frozenset(),
    unsupported_claims: frozenset[str] = frozenset(),
    source: str = "",
) -> tuple[str, int]:
    """Remove only the unsupported local fact/claim; retain adjacent content.

    Numeric and term matchers now target exact occurrence spans (including
    Persian number words and units). Independent clauses are salvaged locally;
    only a high-risk medical clause is removed as a unit when it contains an
    unsupported assertion. Claim-level removal is separately checked against
    source clauses so one unsupported clause cannot erase a supported neighbor.
    """
    if not text or not (fabricated_numbers or fabricated_terms or unsupported_claims):
        return text, 0
    source_sentence_words = [
        content_words(sentence) for sentence in split_sentences(source)
    ]
    if not source_sentence_words and source:
        source_sentence_words = [content_words(source)]
    normalized_claims = {
        " ".join(normalize_for_compare(claim).split())
        for claim in unsupported_claims
        if claim.strip()
    }
    kept_sentences: list[str] = []
    removed = 0
    for sentence in _SCRUB_SENTENCE.split(text):
        stripped = sentence.strip()
        if not stripped:
            continue
        normalized_sentence = " ".join(normalize_for_compare(stripped).split())
        sentence_is_unsupported = any(
            claim and (normalized_sentence == claim or claim in normalized_sentence)
            for claim in normalized_claims
        )
        clauses = [part.strip() for part in _SCRUB_LOCAL_CLAUSE.split(stripped) if part.strip()]
        if not clauses:
            continue
        kept_clauses: list[str] = []
        prior_clause_removed = False
        for clause in clauses:
            if sentence_is_unsupported and source:
                if not _clause_has_source_support(clause, source_sentence_words):
                    removed += 1
                    prior_clause_removed = True
                    continue
            elif sentence_is_unsupported:
                removed += 1
                prior_clause_removed = True
                continue

            number_hits = [
                occurrence
                for occurrence in extract_number_occurrences(clause, mask=False)
                if _scrub_occurrence_matches(occurrence.signal, fabricated_numbers)
            ]
            term_hits = [
                occurrence
                for occurrence in extract_term_occurrences(clause)
                if _scrub_occurrence_matches(occurrence.signal, fabricated_terms)
            ]
            if (number_hits or term_hits) and classify_claim_risk(clause) == "high":
                # For dose/treatment/diagnosis/threshold assertions, keeping the
                # surrounding imperative or condition without its value can
                # itself be unsafe. The clause split protects neighboring facts.
                removed += 1
                prior_clause_removed = True
                continue
            spans = [
                (occurrence.start, occurrence.end)
                for occurrence in (*number_hits, *term_hits)
            ]
            cleaned = _remove_local_spans(clause, spans)
            removed += len(set(spans))
            if not cleaned:
                prior_clause_removed = True
                continue
            if prior_clause_removed:
                cleaned = _SCRUB_LEADING_CONNECTOR.sub("", cleaned).strip()
            kept_clauses.append(cleaned)
            prior_clause_removed = False
        if kept_clauses:
            # A removed clause can leave a separator hanging at the join point;
            # trim only that punctuation and preserve the remaining words.
            kept_clauses[-1] = kept_clauses[-1].rstrip(" ،,؛;:")
            sentence_result = " ".join(part for part in kept_clauses if part).strip()
            if sentence_result:
                kept_sentences.append(sentence_result)
    if removed == 0:
        return text, 0
    return " ".join(kept_sentences), removed


def scrub_unsupported_content(
    notes: StructuredNotes, report: NoteQualityReport, *, source: str = ""
) -> tuple[StructuredNotes, int]:
    """Deterministically remove source-disproved claims without gutting a draft.

    This is the final safe recovery after a targeted repair fails or cannot
    establish a replacement. It handles fabricated numbers, invented terms and
    prose claims, unlike the former number-only scrub.
    """
    fabricated_numbers = frozenset(
        signal for signal in report.audit.fabricated if signal.kind == "number"
    )
    fabricated_terms = frozenset(
        signal for signal in report.audit.fabricated if signal.kind in ("term", "url")
    )
    unsupported_claims = frozenset(
        normalize_for_compare(claim.claim)
        for claim in report.unsupported_claims
        if claim.claim.strip()
    )
    if not (fabricated_numbers or fabricated_terms or unsupported_claims):
        return notes, 0
    removed = 0

    def scrub(value: str) -> str:
        nonlocal removed
        cleaned, count = _scrub_text(
            value,
            fabricated_numbers=fabricated_numbers,
            fabricated_terms=fabricated_terms,
            unsupported_claims=unsupported_claims,
            source=source,
        )
        removed += count
        return cleaned

    def scrub_tuple(values_in: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value for value in (scrub(item) for item in values_in) if value.strip())

    sections: list[NoteSection] = []
    for section_index, section in enumerate(notes.sections, start=1):
        definitions_list: list[NoteDefinition] = []
        for entry in section.definitions:
            cleaned_term = scrub(entry.term)
            cleaned_definition = scrub(entry.definition)
            if cleaned_term.strip() and cleaned_definition.strip():
                definitions_list.append(
                    replace(entry, term=cleaned_term, definition=cleaned_definition)
                )
        definitions = tuple(definitions_list)
        callouts_list: list[NoteCallout] = []
        for callout in section.callouts:
            cleaned_callout = scrub(callout.text)
            if cleaned_callout.strip():
                callouts_list.append(replace(callout, text=cleaned_callout))
        callouts = tuple(callouts_list)
        tables: list[NoteTable] = []
        for table in section.all_tables:
            headers = [scrub(header) for header in table.headers]
            if not all(header.strip() for header in headers):
                continue
            rows = [
                [scrub(cell) for cell in row]
                for row in table.rows
            ]
            rows = [row for row in rows if any(cell.strip() for cell in row)]
            if rows:
                tables.append(
                    replace(table, caption=scrub(table.caption), headers=headers, rows=rows)
                )
        cleaned = replace(
            section,
            heading=scrub(section.heading) or f"بخش {to_persian_digits(section_index)}",
            paragraphs=scrub_tuple(section.paragraphs),
            bullets=scrub_tuple(section.bullets),
            definitions=definitions,
            examples=scrub_tuple(section.examples),
            steps=scrub_tuple(section.steps),
            formulas=scrub_tuple(section.formulas),
            key_points=scrub_tuple(section.key_points),
            callouts=callouts,
            table=tables[0] if tables else None,
            tables=tuple(tables[1:]),
        )
        if cleaned.has_content:
            sections.append(cleaned)
    tips_list: list[PracticalTip] = []
    for tip in notes.practical_tips:
        cleaned_tip = scrub(tip.text)
        cleaned_basis = scrub(tip.basis)
        if cleaned_tip.strip() and cleaned_basis.strip():
            tips_list.append(replace(tip, text=cleaned_tip, basis=cleaned_basis))
    tips = tuple(tips_list)
    glossary_list: list[GlossaryEntry] = []
    for glossary_entry in notes.glossary:
        cleaned_glossary_term = scrub(glossary_entry.term)
        cleaned_glossary = scrub(glossary_entry.definition)
        if cleaned_glossary_term.strip() and cleaned_glossary.strip():
            glossary_list.append(
                replace(
                    glossary_entry,
                    term=cleaned_glossary_term,
                    definition=cleaned_glossary,
                )
            )
    glossary = tuple(glossary_list)
    practice_list: list[PracticeQuestion] = []
    for qa in notes.practice_questions:
        cleaned_q = scrub(qa.question)
        cleaned_a = scrub(qa.answer)
        if cleaned_q.strip():
            practice_list.append(replace(qa, question=cleaned_q, answer=cleaned_a))
    scrubbed = replace(
        notes,
        title=scrub(notes.title),
        subtitle=scrub(notes.subtitle),
        summary=scrub(notes.summary),
        sections=tuple(sections),
        key_points=scrub_tuple(notes.key_points),
        learning_objectives=scrub_tuple(notes.learning_objectives),
        review_questions=scrub_tuple(notes.review_questions),
        practice_questions=tuple(practice_list),
        common_mistakes=scrub_tuple(notes.common_mistakes),
        final_review=scrub_tuple(notes.final_review),
        practical_tips=tips,
        glossary=glossary,
        quality_state=QUALITY_DEGRADED,
    )
    if removed:
        logger.warning("note deterministic scrub removed=%d source-disproved span(s)", removed)
    return scrubbed, removed


def scrub_fabricated_numbers(
    notes: StructuredNotes, audit: SignalAudit
) -> tuple[StructuredNotes, int]:
    """Backward-compatible number-only scrub used by older call sites/tests."""
    report = NoteQualityReport(audit=audit, coverage=sentence_coverage("", notes))
    return scrub_unsupported_content(notes, report)

def _critical_issue_count(report: NoteQualityReport) -> int:
    return len(report.audit.fabricated) + len(report.unsupported_claims)


def _repair_priority(report: NoteQualityReport) -> tuple[int, int, int, int, int, int]:
    """Lexicographic quality: faithfulness dominates recall and presentation."""
    return (
        -_critical_issue_count(report),
        -len(report.audit.fabricated),
        -len(report.unsupported_claims),
        -len(report.audit.missing),
        int(round(report.audit.number_coverage * 10_000)),
        int(round(report.audit.term_coverage * 10_000)),
    )


def repair_improves(candidate: NoteQualityReport, current: NoteQualityReport) -> bool:
    """Accept a safer artifact even when raw coverage does not rise.

    A repair that removes an invented dose or unsupported sentence is strictly
    better than its predecessor at unchanged coverage. Conversely, a candidate
    that creates any *new* critical issue is never accepted. Small recall loss
    is tolerated only when it buys a higher-priority faithfulness correction;
    the final bounded fill pass may restore source-backed omissions later.
    """
    current_critical = _critical_issue_count(current)
    candidate_critical = _critical_issue_count(candidate)
    if candidate_critical > current_critical:
        return False
    if candidate_critical < current_critical:
        return True
    if candidate.audit.fabricated or candidate.unsupported_claims:
        # No priority win, and a hard failure remains: do not swap a known bad
        # artifact for a differently bad one.
        return False
    if candidate.audit.number_coverage + 1e-9 < current.audit.number_coverage:
        return False
    if candidate.audit.term_coverage + 1e-9 < current.audit.term_coverage - 0.05:
        return False
    if candidate.coverage.ratio + 0.05 < current.coverage.ratio:
        return False
    return _repair_priority(candidate) > _repair_priority(current) or (
        candidate.coverage.ratio > current.coverage.ratio + 1e-9
    )


_FALLBACK_LOCATOR = re.compile(r"\[([^\]\n]{1,120})\]")
_FALLBACK_HEADING = re.compile(r"^\s*#{1,6}\s+([^\n]{1,200})", re.MULTILINE)
_GENERIC_BOOKLET_TITLES = frozenset(
    normalize_for_compare(value)
    for value in ("جزوه", "جزوهٔ منبع", "جزوهٔ کلاس", "درس", "عنوان", "عنوان جزوه", "untitled")
)


def is_usable_transcript(source: str) -> bool:
    """Whether source has any readable substance for a source-only booklet.

    Keep this deliberately permissive: short, valid spoken passages must not be
    rejected by a completeness heuristic. Strip only pure locator/structural
    scaffolding, then require at least one word or hard source signal.
    """
    raw = (source or "").strip()
    if not raw:
        return False
    material = _FALLBACK_LOCATOR.sub(" ", raw)
    material = mask_structural(material)
    material = re.sub(
        r"(?:متن پیاده.?سازی.?شده.? صدای ارائه|گفتار بدون نگاشت مطمئن به اسلاید)",
        " ", material, flags=re.IGNORECASE,
    )
    folded = normalize_for_compare(material)
    return bool(content_words(folded) or extract_signals(material))


def _fallback_title(source: str) -> str:
    """Derive a short source-only title; never invent a subject label."""
    for match in _FALLBACK_HEADING.finditer(source):
        candidate = match.group(1).strip()
        if not candidate or re.search(r"متن پیاده.?سازی|گفتار بدون نگاشت", candidate):
            continue
        candidate = re.sub(r"^(?:ادامهٔ\s+)?اسلاید\s+[0-9۰-۹٠-٩]+\s*[—–-]?\s*", "", candidate)
        candidate = candidate.strip(" —–-:#")
        if candidate:
            return f"جزوهٔ {candidate[:80]}"
    first = next((item.strip() for item in split_sentences(source) if item.strip()), "")
    if not first:
        first = next((line.strip() for line in source.splitlines() if line.strip()), "")
    first = re.sub(r"^\s*#{1,6}\s*", "", first)
    first = re.sub(r"\[[^\]\n]{1,120}\]", "", first).strip()
    words = first.split()[:9]
    subject = " ".join(words).strip(" .،؛:؟!?—–-")
    return f"جزوهٔ {subject}" if subject else "جزوهٔ متن آموزشی"


def _fallback_piece_boundaries(text: str, *, maximum: int = MAX_TEXT_CHARS) -> list[str]:
    """Split only at source whitespace/sentence boundaries without dropping text."""
    source = text.strip()
    if not source:
        return []
    pieces: list[str] = []
    cursor = 0
    while cursor < len(source):
        end = min(len(source), cursor + maximum)
        if end < len(source):
            boundary = max(
                source.rfind("\n\n", cursor + maximum // 2, end),
                source.rfind("\n", cursor + maximum // 2, end),
                source.rfind(". ", cursor + maximum // 2, end),
                source.rfind("؟", cursor + maximum // 2, end),
                source.rfind("!", cursor + maximum // 2, end),
                source.rfind(" ", cursor + maximum // 2, end),
            )
            if boundary > cursor:
                end = boundary + (1 if source[boundary:boundary + 2] == ". " else 0)
        part = source[cursor:end].strip()
        if part:
            pieces.append(part)
        cursor = end
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
    return pieces


def deterministic_fallback_notes(
    source: str,
    *,
    title: str = "",
    section_offset: int = 0,
) -> StructuredNotes:
    """Build a renderer-safe, source-preserving booklet with no LLM inference.

    This is deliberately *not* a summary: every non-empty source paragraph is
    retained in source order. Existing source headings are reused; otherwise a
    neutral Persian ``بخش N`` heading is created. Exact bracket locators remain
    both in the content and section metadata, so the fallback never invents a
    citation while still being usable in the DOCX/PDF renderers.
    """
    raw = instructional_source_text(source)
    if not is_usable_transcript(raw):
        raise StructuringError("متن پیاده‌سازی‌شده برای ساخت جزوه محتوای قابل‌استفاده‌ای نداشت.")
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n+", raw):
        paragraphs.extend(_fallback_piece_boundaries(block))
    if not paragraphs:
        raise StructuringError("متن ورودی قابل‌استفاده نبود.")
    sections: list[NoteSection] = []
    pending: list[str] = []
    pending_chars = 0
    current_heading = ""

    def flush() -> None:
        nonlocal pending, pending_chars, current_heading
        if not pending:
            return
        number = section_offset + len(sections) + 1
        heading = current_heading or f"بخش {to_persian_digits(number)}"
        locators = tuple(
            dict.fromkeys(
                " ".join(match.group(1).split())
                for paragraph in pending
                for match in _FALLBACK_LOCATOR.finditer(paragraph)
            )
        )[:MAX_SOURCE_LOCATORS_PER_SECTION]
        sections.append(
            NoteSection(heading=heading, paragraphs=tuple(pending), source_locators=locators)
        )
        pending = []
        pending_chars = 0
        current_heading = ""

    for paragraph in paragraphs:
        match = _FALLBACK_HEADING.match(paragraph)
        paragraph_heading = match.group(1).strip() if match else ""
        # Keep a real Markdown heading as both a deterministic heading and in
        # the source text when it has no following content.
        if paragraph_heading and pending:
            flush()
        if paragraph_heading:
            current_heading = paragraph_heading
        if pending and pending_chars + len(paragraph) + 2 > MAX_TEXT_CHARS:
            flush()
        pending.append(paragraph)
        pending_chars += len(paragraph) + 2
    flush()
    if not sections:
        raise StructuringError("متن ورودی قابل‌استفاده نبود.")
    # The fallback is intentionally source-preserving rather than pretending to
    # be a synthesized study guide. Its closing review is a verbatim source
    # passage so the canonical rendering contract remains complete without an
    # invented recap.
    review_source = next(
        (paragraph for section in reversed(sections) for paragraph in reversed(section.paragraphs) if paragraph),
        "",
    )
    return StructuredNotes(
        title=title.strip() or _fallback_title(raw),
        sections=tuple(sections),
        final_review=(review_source,) if review_source else (),
        quality_state=QUALITY_RECOVERED,
    )


def ensure_structured_booklet(notes: StructuredNotes, source: str) -> StructuredNotes:
    """Repair mandatory booklet structure locally, preserving source content.

    A malformed/empty model shape is replaced with a source-preserving outline;
    optional sections are never invented. The function is intentionally local
    and deterministic so schema/format problems do not require another provider
    call or turn a usable transcript into a failed job.
    """
    if not is_usable_transcript(source):
        raise StructuringError("متن پیاده‌سازی‌شده برای ساخت جزوه محتوای قابل‌استفاده‌ای نداشت.")
    sections: list[NoteSection] = []
    changed = False
    for index, section in enumerate(notes.sections, start=1):
        body = section_plain_text(replace(section, heading="")).strip()
        if not section.has_content or not body:
            changed = True
            continue
        heading = section.heading.strip() or f"بخش {to_persian_digits(index)}"
        if heading != section.heading:
            changed = True
        sections.append(replace(section, heading=heading))
    if not sections:
        return deterministic_fallback_notes(source)

    title = notes.title.strip()
    if not title or normalize_for_compare(title) in _GENERIC_BOOKLET_TITLES:
        title = _fallback_title(source)
        changed = True

    # A final review is mandatory for a delivered full booklet. When a provider
    # omits it, reuse already source-grounded material rather than fabricate a
    # new recap or spend another whole-booklet request.
    final_review = tuple(item for item in notes.final_review if item.strip())
    if not final_review:
        candidates = list(notes.key_points)
        if not candidates:
            candidates = [point for section in sections for point in section.key_points]
        if not candidates:
            candidates = [
                value
                for section in reversed(sections)
                for value in (*section.paragraphs, *section.bullets)
                if value.strip()
            ]
        final_review = tuple(dict.fromkeys(candidates[:8]))
        changed = True
    if changed and notes.quality_state == QUALITY_VERIFIED:
        state = QUALITY_REPAIRED
    else:
        state = notes.quality_state
    return replace(
        notes,
        title=title,
        sections=tuple(sections),
        final_review=final_review,
        quality_state=state,
    )


# ===========================================================================
# The map-reduce pipeline
# ===========================================================================


def effective_chunk_chars(
    settings: Settings, *, system_prompt: str, extra_reserve_chars: int = 0
) -> int:
    """Transport guardrail; the semantic plan, not this number, chooses topics."""
    tokens = booklet_input_budget(
        settings, system=system_prompt, user_metadata=" " * max(0, extra_reserve_chars),
        schema=_response_schema(settings), task="BOOKLET_GENERATION",
    )
    if tokens <= 0:
        raise StructuringError(
            "پنجرهٔ زمینهٔ مدل پس از کسر دستور جزوه، ساختار و سقف پاسخ، فضایی برای متن ورودی ندارد."
        )
    return max(1, min(settings.note_chunk_chars, int(tokens * settings.llm_token_estimation_chars_per_token)))


def effective_compile_max_chars(
    settings: Settings, *, mode: str = "full", presentation: bool = False
) -> int:
    """Editorial character ceiling; route dispatch also checks actual token budgets."""
    from .booklet_synthesis import synthesis_schema

    available_tokens = booklet_input_budget(
        settings, system=synthesis_system_prompt(), user_metadata="",
        schema=synthesis_schema() if settings.note_structured_output else None, task="BOOKLET_COMPILE",
    )
    if available_tokens <= 0:
        return 0
    derived = min(int(available_tokens * settings.llm_token_estimation_chars_per_token), COMPILE_HARD_MAX_CHARS)
    if settings.note_compile_max_chars > 0:
        return min(settings.note_compile_max_chars, derived)
    return derived


MAX_NOTE_CONCURRENCY = 8


def _note_semaphore(settings: Settings, chunks: int) -> asyncio.Semaphore:
    """Bound map concurrency by the setting *and* the number of chunks.

    A semaphore of ``NOTE_CONCURRENCY`` over two chunks is two idle slots, and
    over a hundred chunks it is a rate-limit generator; the useful limit is the
    smaller of the configured value and the actual work, capped at
    :data:`MAX_NOTE_CONCURRENCY`.
    """
    configured = max(1, int(getattr(settings, "note_concurrency", 1) or 1))
    return asyncio.Semaphore(max(1, min(configured, MAX_NOTE_CONCURRENCY, max(1, chunks))))


@dataclass(frozen=True, slots=True)
class FailedSection:
    topic_id: str
    source_range: SourceRange
    error_type: str
    recovered: bool = False


_SectionKey = tuple[str, int, int]


def _section_key(unit: SemanticUnit) -> _SectionKey:
    return unit.topic_id, unit.source_range.start_offset, unit.source_range.end_offset


@dataclass(slots=True)
class SectionCheckpoint:
    """Job-local progress. Serialize via a caller-owned, trusted checkpoint store.

    It is not a booklet artifact. Only verified response shapes are retained;
    local fallback and failed provider replies are not cached as successes.
    """

    fingerprint: str = ""
    plan: ContentPlan | None = None
    completed: dict[_SectionKey, StructuredNotes] = dataclass_field(default_factory=dict)
    split_reasons: dict[_SectionKey, str] = dataclass_field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "version": 1, "fingerprint": self.fingerprint,
            "plan": self.plan.to_payload() if self.plan is not None else None,
            "completed": [[list(key), notes.to_payload()] for key, notes in sorted(self.completed.items())],
            "splits": [[list(key), reason] for key, reason in sorted(self.split_reasons.items())],
        }

    @classmethod
    def from_payload(cls, payload: object) -> SectionCheckpoint:
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("fingerprint"), str):
            raise ValueError("invalid section checkpoint")
        plan = ContentPlan.from_payload(payload["plan"]) if payload.get("plan") is not None else None
        checkpoint = cls(payload["fingerprint"], plan)
        for field in ("completed", "splits"):
            items = payload.get(field)
            if not isinstance(items, list):
                raise ValueError("invalid section checkpoint entries")
            seen: set[_SectionKey] = set()
            for item in items:
                if not isinstance(item, list) or len(item) != 2:
                    raise ValueError("invalid section checkpoint entry")
                key, data = item
                if (not isinstance(key, list) or len(key) != 3 or not isinstance(key[0], str)
                        or type(key[1]) is not int or type(key[2]) is not int or not 0 <= key[1] < key[2]):
                    raise ValueError("invalid section checkpoint range key")
                identity = (key[0], key[1], key[2])
                if identity in seen:
                    raise ValueError("duplicate section checkpoint range key")
                seen.add(identity)
                if field == "completed" and isinstance(data, dict):
                    checkpoint.completed[identity] = _load_json_payload(data)
                elif field == "splits" and data in ("context_overflow", "output_truncation"):
                    checkpoint.split_reasons[identity] = data
                else:
                    raise ValueError("invalid section checkpoint entry value")
        if checkpoint.completed.keys() & checkpoint.split_reasons.keys():
            raise ValueError("completed section cannot also require splitting")
        if not checkpoint.fingerprint and (checkpoint.completed or checkpoint.split_reasons):
            raise ValueError("unbound section checkpoint")
        return checkpoint


class SectionGenerationError(StructuringError):
    """An incomplete map can be resumed, but cannot be delivered as a booklet."""

    def __init__(self, failures: tuple[FailedSection, ...], checkpoint: SectionCheckpoint) -> None:
        super().__init__("یک یا چند موضوع ساخته نشد؛ جزوهٔ ناقص به‌عنوان خروجی کامل تحویل داده نمی‌شود.")
        self.failures = failures
        self.checkpoint = checkpoint


CheckpointCallback = Callable[[SectionCheckpoint], Awaitable[None]]


_SECTION_LOCATOR_LINE = re.compile(
    r"(?m)^[ \t]*\[((?:صفحهٔ|اسلاید|بخش|page|slide|section)[ \t]+[^\]\n]{1,100})\][ \t]*$", re.I,
)


def _section_provenance(notes: StructuredNotes, source: SemanticSource, unit: SemanticUnit) -> StructuredNotes:
    # Bind to real blocks, even if an oversized page was split after its
    # locator line. Bracket delimiters are input syntax, not the stored label.
    refs = unit.source_range.block_ids
    locators = dict.fromkeys(
        " ".join(label.strip("[]").split())
        for block_id in refs
        for label in ((source.locators_by_id[block_id],) if block_id in source.locators_by_id else
                      _SECTION_LOCATOR_LINE.findall(source.blocks_by_id[block_id].text))
    )
    allowed = tuple(locators)[:MAX_SOURCE_LOCATORS_PER_SECTION]
    return replace(notes, input_ranges=(unit.source_range,), sections=tuple(replace(
        section, source_locators=allowed, topic_id=unit.topic_id, parent_topic_id=unit.parent_id,
        section_id=f"{unit.topic_id}:{unit.source_range.start_offset}:{unit.source_range.end_offset}:{index}",
        source_block_ids=refs,
    ) for index, section in enumerate(notes.sections, 1)))


def _section_prompt_metadata(topic: PlannedTopic, plan: ContentPlan, position: int, *, strict: bool) -> str:
    # The plan projection is deliberately structural. No other topic's prose,
    # examples, numbers, or source text is included in this writer's request.
    return "موضوع و طرح (فقط ساختار): " + json.dumps({
        "topic": {"id": topic.id, "title": topic.title, "parent_id": topic.parent_id,
                  "dimensions_if_evidenced": topic.required_dimensions},
        "plan": {"position": position, "topic_count": len(plan.topics),
                 "previous_id": plan.topics[position - 2].id if position > 1 else None,
                 "next_id": plan.topics[position].id if position < len(plan.topics) else None},
        "strict_grounding": strict,
    }, ensure_ascii=False, separators=(",", ":"))


class SectionWriter:
    """Bind topic-local generation to the existing provider router and semaphore."""

    def __init__(
        self, settings: Settings, session: aiohttp.ClientSession, *, mode: str = "full",
        semaphore: asyncio.Semaphore | None = None, checkpoint: SectionCheckpoint | None = None,
        on_checkpoint: CheckpointCallback | None = None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.mode = mode
        self.semaphore = semaphore or _note_semaphore(settings, settings.note_concurrency)
        self.checkpoint = checkpoint if checkpoint is not None else SectionCheckpoint()
        self.recoveries: list[SemanticRecovery] = []
        self.failures: list[FailedSection] = []
        self._binding: tuple[NormalizedDocument, ContentPlan] | None = None
        self._on_checkpoint = on_checkpoint
        self._checkpoint_lock = asyncio.Lock()

    async def save_checkpoint(self) -> None:
        if self._on_checkpoint is not None:
            async with self._checkpoint_lock:
                try:
                    await self._on_checkpoint(self.checkpoint)
                except Exception as exc:
                    logger.warning("section checkpoint store failed error=%s", type(exc).__name__)

    def bind(self, document: NormalizedDocument, plan: ContentPlan, units: Sequence[SemanticUnit] = ()) -> None:
        if self._binding is not None and self._binding[0] is document and self._binding[1] is plan:
            return
        digest = hashlib.sha256(document.text.encode("utf-8", errors="surrogatepass"))
        digest.update(plan.to_json().encode("utf-8", errors="surrogatepass"))
        digest.update(section_writer_system_prompt().encode("utf-8"))
        digest.update(str((self.mode, self.settings.note_strict_grounding)).encode("ascii"))
        for unit in units:
            digest.update(repr(_section_key(unit)).encode("utf-8"))
        fingerprint = digest.hexdigest()
        if self.checkpoint.fingerprint and self.checkpoint.fingerprint != fingerprint:
            raise ValueError("section checkpoint belongs to a different source or plan")
        self.checkpoint.fingerprint = fingerprint
        self.checkpoint.plan = plan
        self._binding = (document, plan)

    def failure(self, source: SemanticSource, unit: SemanticUnit, exc: Exception) -> StructuredNotes:
        recovered: StructuredNotes | None = None
        if self.settings.note_enable_deterministic_fallback:
            try:
                recovered = deterministic_fallback_notes(source.body(unit))
            except StructuringError:
                pass
        failure = FailedSection(unit.topic_id, unit.source_range, type(exc).__name__, recovered is not None)
        self.failures.append(failure)
        logger.warning("section writer topic=%s range=%d:%d outcome=%s error=%s", unit.topic_id,
                       unit.source_range.start_offset, unit.source_range.end_offset,
                       "FALLBACK" if recovered is not None else "FAILED", failure.error_type)
        if recovered is None:
            raise SectionGenerationError((failure,), self.checkpoint) from exc
        return _section_provenance(recovered, source, unit)

    async def write_unit(
        self, topic: PlannedTopic, source: SemanticSource, unit: SemanticUnit, plan: ContentPlan,
        *, position: int, index: int, total: int, header: str = "", standalone: bool = False,
    ) -> StructuredNotes:
        metadata = _section_prompt_metadata(topic, plan, position, strict=self.settings.note_strict_grounding)

        def render(part: SemanticUnit) -> str:
            body = source.body(part)
            if total == 1:
                return build_chunk_message(body, index=1, total=1, header=(header or SINGLE_CHUNK_HEADER) + "\n" + metadata)
            return build_chunk_message(body, index=index, total=total, topic=topic.title,
                                       context_block=metadata, header=header)

        try:
            result = await _structure_piece(
                self.session, self.settings, source, unit, system_prompt=section_writer_system_prompt(),
                mode=self.mode, semaphore=self.semaphore, render=render, recovery_events=self.recoveries,
                writer=self, standalone=standalone,
            )
        except SectionGenerationError:
            raise
        except (LLMError, StructuringError) as exc:
            # A caller-supplied/legacy piece implementation may raise before
            # handing off to the normal leaf failure handler.
            return self.failure(source, unit, exc)
        return result if result.input_ranges else _section_provenance(result, source, unit)


async def generate_section(
    topic: PlannedTopic, document: NormalizedDocument, plan: ContentPlan, router: SectionWriter,
) -> NoteSection:
    """Write one planned topic, using the same mapper as booklet generation.

    The booklet path retains rich metadata and separate nested sections; the
    one-section adapter combines their typed content for single-topic callers.
    """
    if plan.topic(topic.id) != topic:
        raise ValueError("planned topic is not part of the supplied plan")
    units = build_semantic_topics(document, plan)
    unit = next((unit for unit in units if unit.topic_id == topic.id), None)
    if unit is None:
        raise ValueError("topic is not an independent semantic unit")
    router.bind(document, plan)
    source = SemanticSource(document)
    position = plan.topics.index(topic) + 1
    metadata = _section_prompt_metadata(topic, plan, position, strict=router.settings.note_strict_grounding)
    budget = effective_chunk_chars(router.settings, system_prompt=section_writer_system_prompt(),
                                   extra_reserve_chars=len(metadata) + 200)
    units = source.fit([unit], lambda body: len(body) <= budget, max_probe_chars=budget)
    if len(units) > MAX_NOTE_CHUNKS:
        notes = router.failure(source, unit, StructuringError("موضوع از سقف درخواست‌های بخش بیشتر است."))
    else:
        results = await asyncio.gather(*(router.write_unit(topic, source, part, plan, position=position, index=index,
                                                           total=len(units), standalone=True) for index, part in enumerate(units, 1)),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        notes = merge_structured_notes([result for result in results if isinstance(result, StructuredNotes)])
    section = notes.sections[0]
    for following in notes.sections[1:]:
        section = _combine_sections(section, following)
    return section


async def _structure_piece(
    session: aiohttp.ClientSession,
    settings: Settings,
    source: SemanticSource,
    unit: SemanticUnit,
    *,
    system_prompt: str,
    mode: str,
    semaphore: asyncio.Semaphore,
    render: Callable[[SemanticUnit], str],
    recovery_events: list[SemanticRecovery],
    overlap_chars: int = 0,
    depth: int = 0,
    writer: SectionWriter | None = None,
    standalone: bool = False,
) -> StructuredNotes:
    """Generate one unit; overflow replaces just its exact owned interval.

    A truncated response cannot prove which claims or sections finished. Never
    stitch JSON fragments: request smaller independent source sections and
    validate every response through the same StructuredNotes loader.
    """
    key = _section_key(unit)
    checkpoint = writer.checkpoint if writer is not None else None
    if checkpoint is not None and key in checkpoint.completed:
        return _section_provenance(checkpoint.completed[key], source, unit)
    reason = checkpoint.split_reasons.get(key) if checkpoint is not None else None
    if reason is None:
        try:
            async with semaphore:
                payload = await complete_json(
                    session, settings, system=system_prompt, user=render(unit),
                    schema=_response_schema(settings), task="BOOKLET_GENERATION", adaptive_capacity=True,
                )
            notes = _load_json_payload(payload, mode=mode)
            if standalone and settings.note_strict_grounding:
                notes, report = gate_notes(notes, source.body(unit), strict=True, mode=mode)
                if (issue := safety_failure(report)) or not notes.sections:
                    raise NoteQualityError("بخش مستقل شاهد کافی ندارد.", reason=issue or "invalid_structure")
            elif standalone and not settings.note_strict_grounding:
                # Direct mode retains lexical enrichment, but cannot establish
                # unsupported measurements or high-risk clinical instructions.
                audit = audit_notes(source.body(unit), notes)
                clinical = classify_claim_risk(notes.to_json()) == "high"
                if any(signal.kind == "number" and (signal.unit or clinical) for signal in audit.fabricated):
                    raise NoteQualityError("اندازه‌گیری بدون شاهد در بخش.", reason="unsupported_facts")
                if (clinical and any(
                        claim.risk == "high" for claim in prose_claim_evidence(source.body(unit), notes))):
                    raise NoteQualityError("دستور بالینی بدون شاهد در بخش.", reason="unsupported_claims")
            if checkpoint is not None:
                checkpoint.completed[key] = notes
                assert writer is not None
                await writer.save_checkpoint()
            return _section_provenance(notes, source, unit)
        except (LLMContextOverflowError, LLMOutputBudgetError) as exc:
            if depth >= MAX_CONTEXT_SPLITS or unit.source_range.end_offset - unit.source_range.start_offset < 2:
                if writer is not None:
                    return writer.failure(source, unit, exc)
                raise
            reason = "context_overflow" if isinstance(exc, LLMContextOverflowError) else "output_truncation"
            if checkpoint is not None:
                checkpoint.split_reasons[key] = reason
                assert writer is not None
                await writer.save_checkpoint()
        except (LLMError, StructuringError) as exc:
            if writer is not None:
                return writer.failure(source, unit, exc)
            raise
    children = source.bisect(unit, overlap_chars=overlap_chars)
    if not children or depth >= MAX_CONTEXT_SPLITS:
        raise StructuringError("بازهٔ نامعتبر در بازیابی بخش.")
    event = SemanticRecovery(unit.topic_id, reason, unit.source_range, tuple(child.source_range for child in children))
    recovery_events.append(event)
    logger.info("section writer split reason=%s topic=%s range=%d:%d depth=%d", reason, unit.topic_id,
                unit.source_range.start_offset, unit.source_range.end_offset, depth + 1)
    outcomes = await asyncio.gather(*(_structure_piece(
        session, settings, source, child, system_prompt=system_prompt, mode=mode, semaphore=semaphore,
        render=render, recovery_events=recovery_events, overlap_chars=overlap_chars, depth=depth + 1, writer=writer, standalone=standalone,
    ) for child in children), return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    parts = [item for item in outcomes if isinstance(item, StructuredNotes)]
    return replace(merge_structured_notes(parts), input_ranges=tuple(span for part in parts for span in part.input_ranges),
                   quality_state=QUALITY_RECOVERED if any(part.quality_state == QUALITY_RECOVERED for part in parts)
                   else parts[0].quality_state)


def _merged_chars(notes: StructuredNotes) -> int:
    """Serialized size of the merged booklet, the compile budget's unit."""
    return len(json.dumps(compact_notes_payload(notes), ensure_ascii=False))


def compile_skip_reason(
    notes: StructuredNotes,
    settings: Settings,
    *,
    mode: str,
    presentation: bool,
) -> str | None:
    """Why the editorial pass will not run, or ``None`` when it will.

    One predicate for two callers that must never disagree: ``synthesize_booklet``
    decides whether to send the request, and the progressive-booklet callback
    decides whether a *draft* is worth delivering. When the compile is skipped
    the pass returns the merge unchanged, so a draft would be byte-identical to
    the booklet the user is about to receive — a second file that says nothing
    new.
    """
    if not settings.note_compile:
        return "disabled"
    if _merged_chars(notes) < COMPILE_MIN_NOTES_CHARS:
        return "small"
    if effective_compile_max_chars(settings, mode=mode, presentation=presentation) <= 0:
        return "no-budget"
    return None


def compile_will_run(
    notes: StructuredNotes,
    settings: Settings,
    *,
    mode: str,
    presentation: bool,
) -> bool:
    """Whether the booklet is eligible for the optional editorial pass."""
    return compile_skip_reason(notes, settings, mode=mode, presentation=presentation) is None


async def synthesize_booklet(
    sections: StructuredNotes | Sequence[NoteSection], plan: ContentPlan,
    document: NormalizedDocument, router: SectionWriter,
) -> StructuredNotes:
    """Apply opt-in editorial edits to a deterministic booklet, not a rewrite."""
    from .booklet_synthesis import synthesize_booklet as edit_booklet

    return await edit_booklet(sections, plan, document, router)


#: Called with the deterministic merge before the editorial compile runs, so
#: the caller can deliver it while the compile is still in flight.
MergedCallback = Callable[["StructuredNotes"], Awaitable[None]]

#: Called with ``"validating"`` and ``"compiling"`` when the pipeline enters the
#: evidence gate and the editorial pass. It lets the job layer report a real
#: stage instead of one opaque "generating notes" window; a failure inside the
#: callback is logged and never fails the booklet.
StageCallback = Callable[[str], Awaitable[None]]

#: Called with ``"missing_numbers"`` or ``"missing_terms"`` when a booklet is
#: accepted but still omits source facts, so the job reports a partial result
#: instead of claiming a complete, verified booklet.
IncompleteCallback = Callable[[str], Awaitable[None]]


async def _structure_booklet(
    normalized: NormalizedDocument,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str = "full",
    presentation: bool = False,
    on_merged: MergedCallback | None = None,
    source_locators: bool = False,
    on_stage: StageCallback | None = None,
    section_checkpoint: SectionCheckpoint | None = None,
    on_section_checkpoint: CheckpointCallback | None = None,
) -> StructuredNotes:
    """Map-reduce a prepared LLM document into one structured booklet.

    * the normalized plan owns topic/page/slide boundaries; only oversized
      topics are subdivided to fit the configured route budgets;
    * one map request per fitted unit (bounded concurrency), followed by the
      existing deterministic merge and optional editorial pass;
    * typed context/output exhaustion replaces only the affected range after
      provider failover, with bounded source-preserving recovery;
    * failed coverage recovers locally or fails the booklet instead of
      delivering only the successful subset;
    * a failed compile falls back to the deterministic merge;
    * ``on_merged`` receives that merge before the compile starts, which is what
      lets the caller deliver a booklet while the editorial pass still runs.
    """
    document = normalized.text
    if not document:
        raise StructuringError("متن ورودی خالی بود.")
    resolved_mode = resolve_note_mode(mode)
    async def notify(stage: str) -> None:
        """Report a pipeline stage without ever failing the booklet."""
        if on_stage is None:
            return
        try:
            await on_stage(stage)
        except Exception:  # noqa: BLE001 - reporting is never a note failure
            logger.warning("note stage callback failed stage=%s", stage, exc_info=True)

    # One static prompt for every source kind: the source kind and the source
    # locators are carried by the user message, never by this string. A prompt
    # that changed with the source kind could not be prefix-cached, and a job
    # that mixes audio and slides would pay for two different prefixes.
    base_system = section_writer_system_prompt()
    source_header = PRESENTATION_PROMPT.rstrip("\n") if presentation else ""
    source = SemanticSource(normalized)
    plan = (section_checkpoint.plan if section_checkpoint is not None else None)
    if plan is None:
        plan = await asyncio.to_thread(plan_from_document, normalized)
    schema = _response_schema(settings)
    ratio = settings.llm_token_estimation_chars_per_token

    def fit_plan(active_plan: ContentPlan) -> list[SemanticUnit]:
        topics = build_semantic_topics(normalized, active_plan)
        longest = max((_section_prompt_metadata(topic, active_plan, n, strict=settings.note_strict_grounding)
                       for n, topic in enumerate(active_plan.topics, 1)), key=len, default="")
        metadata = build_chunk_message("", index=MAX_NOTE_CHUNKS, total=MAX_NOTE_CHUNKS,
                                       context_block=longest, topic=" " * 120, header=source_header)
        # The single-part form can use a slightly different label.
        body_budget = booklet_input_budget(settings, system=base_system, user_metadata=metadata + " " * 64,
                                           schema=schema, task="BOOKLET_GENERATION")
        if body_budget <= 0:
            raise StructuringError("پنجرهٔ زمینهٔ مدل پس از کسر بودجهٔ پاسخ، فضایی برای محتوای منبع ندارد.")
        max_chars = max(1, min(settings.note_chunk_chars, int(body_budget * ratio)))
        return source.fit(topics, lambda text: len(text) <= max_chars and
                          estimate_tokens(text, chars_per_token=ratio) <= body_budget, max_probe_chars=max_chars)

    units = await asyncio.to_thread(fit_plan, plan)
    if not units:
        raise StructuringError("متن ورودی خالی بود.")
    if 1 < len(units) <= MAX_NOTE_CHUNKS and (section_checkpoint is None or not section_checkpoint.fingerprint):
        refined = await maybe_refine_plan(plan, normalized, settings, session)
        if refined is not plan:
            plan = refined
            units = await asyncio.to_thread(fit_plan, plan)
    input_ranges = tuple(unit.source_range for unit in units)
    semaphore = _note_semaphore(settings, len(units))
    writer = SectionWriter(settings, session, mode=resolved_mode, semaphore=semaphore, checkpoint=section_checkpoint,
                           on_checkpoint=on_section_checkpoint)
    await asyncio.to_thread(writer.bind, normalized, plan, units)
    by_topic = {topic.id: (position, topic) for position, topic in enumerate(plan.topics, 1)}

    def sorted_failures() -> tuple[FailedSection, ...]:
        return tuple(sorted(writer.failures, key=lambda failure: failure.source_range.start_offset))

    def with_provenance(notes: StructuredNotes) -> StructuredNotes:
        return replace(notes, input_ranges=input_ranges, failed_sections=sorted_failures(), semantic_recoveries=tuple(sorted(
            writer.recoveries, key=lambda event: (event.original.start_offset, -event.original.end_offset, event.reason),
        )))

    async def write(index: int, unit: SemanticUnit) -> StructuredNotes:
        position, topic = by_topic[unit.topic_id]
        try:
            result = await writer.write_unit(topic, source, unit, plan, position=position, index=index,
                                             total=len(units), header=source_header)
            logger.info("section writer topic=%s outcome=%s", unit.topic_id, result.quality_state)
            return result
        except SectionGenerationError:
            raise
        except Exception as exc:
            logger.warning("section writer unexpected error topic=%s type=%s", unit.topic_id, type(exc).__name__)
            return writer.failure(source, unit, exc)

    from .booklet_verification import verify_and_repair

    async def finish_and_synthesize(draft: StructuredNotes, *, allow_repair: bool = True) -> StructuredNotes:
        await notify("validating")

        async def edit(baseline: StructuredNotes) -> StructuredNotes:
            if (writer.failures or baseline.quality_state == QUALITY_RECOVERED
                    or not any(section.section_id for section in baseline.sections)
                    or not compile_will_run(baseline, settings, mode=resolved_mode, presentation=presentation)):
                return baseline
            if on_merged is not None:
                try:
                    await on_merged(with_provenance(baseline))
                except Exception:  # noqa: BLE001 - delivery is never a note failure
                    logger.warning("progressive booklet delivery failed", exc_info=True)
            await notify("compiling")
            return await synthesize_booklet(with_provenance(baseline), plan, normalized, writer)

        verified = await verify_and_repair(with_provenance(draft), normalized, plan, writer,
                                           allow_repair=allow_repair and not writer.failures,
                                           after_repair=edit if allow_repair else None)
        return with_provenance(verified)

    if len(units) == 1:
        single = merge_structured_notes([await write(1, units[0])])
        input_ranges = single.input_ranges
        return await finish_and_synthesize(single)

    if len(units) > MAX_NOTE_CHUNKS:
        if not settings.note_enable_deterministic_fallback:
            raise StructuringError(
                f"این متن به {len(units)} بخش تقسیم می‌شود که از سقف {MAX_NOTE_CHUNKS} "
                "درخواست جزوه بیشتر است؛ متن خام دست‌نخورده می‌ماند."
            )
        logger.warning("note map request cap exceeded chunks=%d outcome=source_preserving_fallback", len(units))
        # Do not ask the model to fill or compile this budget-driven recovery.
        # Use the same deterministic structure/evidence acceptance instead.
        return await finish_and_synthesize(deterministic_fallback_notes(document), allow_repair=False)
    # Gather in plan/range order. The provider semaphore limits both initial
    # requests and recursive leaves; one failure never cancels another writer.
    outcomes = await asyncio.gather(*(write(index, unit) for index, unit in enumerate(units, 1)),
                                   return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException) and not isinstance(outcome, SectionGenerationError):
            raise outcome
    if any(isinstance(outcome, SectionGenerationError) for outcome in outcomes):
        raise SectionGenerationError(sorted_failures(), writer.checkpoint)
    pieces = [outcome for outcome in outcomes if isinstance(outcome, StructuredNotes)]
    input_ranges = tuple(span for piece in pieces for span in piece.input_ranges)
    merged = merge_structured_notes(pieces)
    return await finish_and_synthesize(merged)


async def structure_document(
    document: str | NormalizedDocument,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str = "full",
    presentation: bool = False,
    on_merged: MergedCallback | None = None,
    source_locators: bool = False,
    on_stage: StageCallback | None = None,
    on_incomplete: IncompleteCallback | None = None,
    section_checkpoint: SectionCheckpoint | None = None,
    on_section_checkpoint: CheckpointCallback | None = None,
) -> StructuredNotes:
    """Produce a booklet, then report whether it is complete.

    The retained artifact carries its verification report. A safe but
    incomplete booklet can be delivered; strict jobs also receive a short
    ``on_incomplete`` reason without any source text.
    """
    normalized = document if isinstance(document, NormalizedDocument) else (
        _normalized_presentation(document) if presentation
        else NormalizedDocument.from_source_text(document, source_type="txt")
    )
    notes = await _structure_booklet(
        normalized, settings, session,
        mode=mode, presentation=presentation, on_merged=on_merged,
        source_locators=source_locators, on_stage=on_stage, section_checkpoint=section_checkpoint,
        on_section_checkpoint=on_section_checkpoint,
    )
    notes = ensure_structured_booklet(notes, normalized.text)
    if not structurally_valid_booklet(notes):
        raise StructuringError("جزوهٔ تولیدشده ساختار لازم برای رندر نداشت.")
    report = notes.verification_report
    if on_incomplete is None or report is None or report.passed or not settings.note_strict_grounding:
        return notes
    codes = {issue.code for issue in report.issues}
    recovery_codes = {"source_only_fallback", "source_node_fallback", "removed_unverified_content"}
    if "source_only_fallback" in codes or (notes.quality_state == QUALITY_RECOVERED and codes <= recovery_codes):
        return notes  # recovery is already reported via quality_state
    if codes & {"missing_number", "missing_date"}:
        reason = "missing_numbers"
    elif "missing_term" in codes:
        reason = "missing_terms"
    elif "missing_topic" in codes:
        reason = "missing_topic"
    else:
        reason = "verification_incomplete"
    logger.warning("note delivered incomplete reason=%s issue_count=%d", reason, len(report.issues))
    try:
        await on_incomplete(reason)
    except Exception:  # noqa: BLE001 - reporting is never a note failure
        logger.warning("note incomplete callback failed reason=%s", reason, exc_info=True)
    return notes


_SLIDE_NUMBER_CHARS = "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩"
_SLIDE_NUMBER_ASCII = "01234567890123456789"
_SLIDE_NUMBER_TRANSLATION = str.maketrans(_SLIDE_NUMBER_CHARS, _SLIDE_NUMBER_ASCII)
_SLIDE_OUTLINE_RE = re.compile(
    r"(?m)^###\s+(?:ادامهٔ\s+)?اسلاید\s+([0-9۰-۹٠-٩]+)(?:\s*[—–-]\s*(.*))?\s*$"
)
_SLIDE_NARRATION_RE = re.compile(
    r"(?m)^###\s+گفتار مرتبط با اسلاید\s+([0-9۰-۹٠-٩]+)\s*$"
)
_UNASSIGNED_NARRATION_RE = re.compile(
    r"(?m)^###\s+(?:ادامهٔ\s+)?گفتار بدون نگاشت مطمئن به اسلاید.*$"
)
_UNALIGNED_SECTION_RE = re.compile(
    r"(?m)^##\s+متن پیاده‌سازی‌شدهٔ صدای ارائه[^\n]*$"
)
_PRESENTATION_BLOCK_RE = re.compile(
    r"(?m)^(?:"
    r"##\s+متن پیاده‌سازی‌شدهٔ صدای ارائه[^\n]*(?:\n+\s*###\s+(?:ادامهٔ\s+)?گفتار بدون نگاشت مطمئن به اسلاید)?"
    r"|###\s+(?:(?:ادامهٔ\s+)?اسلاید\s+[0-9۰-۹٠-٩]+|(?:ادامهٔ\s+)?گفتار بدون نگاشت مطمئن به اسلاید)"
    r")"
)


def _number_from_heading(value: str) -> int:
    return int(value.translate(_SLIDE_NUMBER_TRANSLATION))


def _slide_blocks(text: str) -> tuple[dict[int, tuple[str, str]], list[int], str]:
    matches = list(_SLIDE_OUTLINE_RE.finditer(text))
    blocks: dict[int, tuple[str, str]] = {}
    order: list[int] = []
    for index, match in enumerate(matches):
        number = _number_from_heading(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = (match.group(2) or "").strip()
        body = text[match.end():end].strip()
        if number in blocks:
            prior_title, prior_body = blocks[number]
            extra_title = title if prior_title and title != prior_title else ""
            title = prior_title or title
            body = "\n\n".join(part for part in (prior_body, extra_title, body) if part)
        else:
            order.append(number)
        blocks[number] = (title, body)
    return blocks, order, text[:matches[0].start()].strip() if matches else text.strip()


def _slide_narration_blocks(text: str) -> tuple[dict[int, str], str, bool]:
    matches = sorted(
        [*(_SLIDE_NARRATION_RE.finditer(text)), *(_UNASSIGNED_NARRATION_RE.finditer(text))],
        key=lambda match: match.start(),
    )
    if not matches:
        return {}, "", False
    assigned: dict[int, str] = {}
    preamble = text[:matches[0].start()].strip()
    unassigned: list[str] = [preamble] if preamble else []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        if match.re is _SLIDE_NARRATION_RE:
            number = _number_from_heading(match.group(1))
            previous = assigned.get(number, "")
            assigned[number] = "\n\n".join(part for part in (previous, body) if part)
        else:
            if body:
                unassigned.append(body)
    return assigned, "\n\n".join(unassigned), bool(assigned)


def build_presentation_document(outline: str, transcript: str) -> str:
    """Build presentation input grouped by slide when narration timings allow it.

    Marked, timestamp-aligned narration is joined to its slide text. Plain or
    unaligned provider output stays in a clearly separate section; it is never
    heuristically assigned to a slide.
    """
    outline, transcript = normalize_source_text(outline), normalize_source_text(transcript)
    slide_blocks, slide_order, outline_preamble = _slide_blocks(outline)
    narration, unassigned, has_markers = _slide_narration_blocks(transcript)
    if has_markers:
        sections: list[str] = []
        if outline_preamble:
            sections.append("## متن اسلایدها\n\n" + outline_preamble)
        sections.append("## متن اسلایدها و گفتار ارائه به تفکیک اسلاید")
        order = list(dict.fromkeys([*slide_order, *sorted(narration)]))
        for number in order:
            title, slide_body = slide_blocks.get(number, ("", ""))
            header = f"### اسلاید {to_persian_digits(number)}"
            if title:
                header += f" — {title}"
            block = [header]
            if slide_body:
                block.append("متن اسلاید و یادداشت‌ها:\n\n" + slide_body)
            if narration.get(number):
                block.append("گفتار مرتبط با این اسلاید:\n\n" + narration[number])
            sections.append("\n\n".join(block))
        if unassigned:
            sections.append(
                "## متن پیاده‌سازی‌شدهٔ صدای ارائه (بدون نگاشت زمانی مطمئن)"
            )
            sections.append(
                "### گفتار بدون نگاشت مطمئن به اسلاید\n\n" + unassigned
            )
        return "\n\n".join(sections)

    sections = []
    if outline.strip():
        sections.append("## متن اسلایدها\n\n" + outline.strip())
    if transcript.strip():
        sections.append("## متن پیاده‌سازی‌شدهٔ صدای ارائه\n\n" + transcript.strip())
    return "\n\n".join(sections)


def _continuation_prefix_for_block(block: str, max_chars: int) -> str:
    """Build a bounded continuation heading for an oversized presentation block."""
    slide_match = _SLIDE_OUTLINE_RE.search(block)
    if slide_match is not None:
        number = to_persian_digits(_number_from_heading(slide_match.group(1)))
        title = (slide_match.group(2) or "").strip()
        short_prefix = f"### ادامهٔ اسلاید {number}\n\n"
        if title:
            with_title = f"### ادامهٔ اسلاید {number} — {title}\n\n"
            if len(with_title) <= max_chars // 3:
                return with_title
        return short_prefix if len(short_prefix) <= max_chars // 2 else ""
    if _UNASSIGNED_NARRATION_RE.search(block) or _UNALIGNED_SECTION_RE.search(block):
        unassigned_prefix = "### ادامهٔ گفتار بدون نگاشت مطمئن به اسلاید\n\n"
        return unassigned_prefix if len(unassigned_prefix) <= max_chars // 2 else ""
    return ""


def _presentation_pieces(text: str, max_chars: int) -> list[tuple[str, str]]:
    """Pack slide blocks as (prompt text, source body) pairs."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    text = text.strip()
    if not text:
        return []
    headings = list(_PRESENTATION_BLOCK_RE.finditer(text))
    if not headings:
        return [(piece, piece) for piece in split_transcript(text, max_chars)]

    preamble = text[:headings[0].start()].strip()
    blocks = [
        text[match.start():(headings[index + 1].start() if index + 1 < len(headings) else len(text))].strip()
        for index, match in enumerate(headings)
    ]
    chunks: list[tuple[str, str]] = []
    if preamble:
        if blocks and len(preamble) + 2 < max_chars:
            blocks[0] = f"{preamble}\n\n{blocks[0]}"
        else:
            chunks.extend((part, part) for part in split_transcript(preamble, max_chars))
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append((current.strip(), current.strip()))
        current = ""

    for block in blocks:
        candidate = "\n\n".join(part for part in (current, block) if part)
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            flush()
        if len(block) <= max_chars:
            current = block
            continue

        prefix = _continuation_prefix_for_block(block, max_chars)
        piece_budget = max(1, max_chars - len(prefix))
        pieces = split_transcript(block, piece_budget) if prefix else split_transcript(block, max_chars)
        for index, piece in enumerate(pieces):
            if index and prefix:
                if len(prefix) + len(piece) <= max_chars:
                    chunks.append((prefix + piece, piece))
                else:
                    for continuation in split_transcript(piece, piece_budget):
                        chunks.append((prefix + continuation, continuation))
            else:
                chunks.append((piece, piece))
    flush()
    return chunks


def split_presentation_document(text: str, max_chars: int) -> list[str]:
    """Pack slide blocks; continuation headings are prompt-only annotations."""
    return [text for text, _body in _presentation_pieces(text, max_chars)]


def _normalized_presentation(text: str) -> NormalizedDocument:
    headings = list(_PRESENTATION_BLOCK_RE.finditer(text))
    starts = sorted({0, *(match.start() for match in headings), len(text)})
    blocks = []
    for start, end in zip(starts, starts[1:], strict=False):
        body = text[start:end]
        marker = _SLIDE_OUTLINE_RE.match(body)
        number = _number_from_heading(marker.group(1)) if marker is not None else None
        blocks.append(SourceBlock(
            "", "slide" if number is not None else "paragraph", body, start, end,
            slide_number=number,
        ))
    return NormalizedDocument.from_blocks(blocks, source_type="pptx")


async def structure_transcript(
    text: str | NormalizedDocument,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str = "full",
    on_merged: MergedCallback | None = None,
    on_stage: StageCallback | None = None,
    on_incomplete: IncompleteCallback | None = None,
    section_checkpoint: SectionCheckpoint | None = None,
    on_section_checkpoint: CheckpointCallback | None = None,
) -> StructuredNotes:
    """Transcript text (the cleaned LLM document) → structured notes."""
    return await structure_document(
        (text if isinstance(text, NormalizedDocument)
         else NormalizedDocument.from_source_text(text, source_type="audio", kind="transcript")),
        settings, session, mode=mode, presentation=False,
        on_merged=on_merged, on_stage=on_stage, on_incomplete=on_incomplete,
        section_checkpoint=section_checkpoint, on_section_checkpoint=on_section_checkpoint,
    )


async def structure_presentation(
    outline: str,
    transcript: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str = "full",
    document: str | None = None,
    on_merged: MergedCallback | None = None,
    on_stage: StageCallback | None = None,
    on_incomplete: IncompleteCallback | None = None,
    section_checkpoint: SectionCheckpoint | None = None,
    on_section_checkpoint: CheckpointCallback | None = None,
) -> StructuredNotes:
    """Slide outline + narration transcript → structured notes."""
    resolved_document = (
        document
        if document is not None
        else build_presentation_document(outline, transcript)
    )
    return await structure_document(
        resolved_document,
        settings,
        session,
        mode=mode,
        presentation=True,
        on_merged=on_merged,
        on_stage=on_stage,
        on_incomplete=on_incomplete, section_checkpoint=section_checkpoint,
        on_section_checkpoint=on_section_checkpoint,
    )
