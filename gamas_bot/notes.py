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
* **at most one bounded repair pass**, only when the evidence gate asks for it
  (see ``NOTE_QUALITY_GATE``); an oversized repair request carries only the
  sections that hold the lost facts;
* **no QA-driven re-asks and no truncation re-sends.** A bad answer degrades to
  the raw transcript — which the user already holds, because the transcript is
  delivered before note generation starts.

The note dataclasses, the Persian prompts and the conservative deterministic
merge are ported from v2 nearly verbatim: they encode the content-preservation
contract (numbers, units, English terms and examples are never dropped) that
the raw-text priority is built on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace

import aiohttp

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
    CHUNK_CONTEXT_RESERVE_CHARS,
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
    build_chunk_message,
    build_compile_system_prompt,
    build_edit_message,
    build_system_prompt,
    build_presentation_system_prompt,
    edit_system_prompt,
    notes_system_prompt,
)

from .config import Settings, resolve_note_mode
from .llm import (
    LLMContextOverflowError,
    LLMError,
    LLMOutputBudgetError,
    complete_json,
)
from .notes_evidence import (
    NoteQualityReport,
    SignalAudit,
    audit_notes,
    build_source_index,
    content_words,
    coverage_thresholds,
    evidence_windows,
    extract_signals,
    sentence_coverage,
    split_sentences,
    validate_table,
    validate_tip,
)
from .progress import to_persian_digits
from .textnorm import normalize_for_compare
from .transcript import split_transcript

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 20000
MAX_SOURCE_LOCATORS_PER_SECTION = 100
VALID_CALLOUT_KINDS = ("نکته", "هشدار", "یادآوری")

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
#: The compile result must keep at least this share of the merged sections.
COMPILE_MIN_SECTION_RATIO = 0.5
#: Minimum body-character retention when compiling substantive sections.
COMPILE_MIN_CONTENT_RATIO = 0.35
#: Average body characters per section above which prose-loss gating applies.
COMPILE_SUBSTANTIVE_SECTION_CHARS = 400
#: The deterministic digest/neighbour bounds. They live in the prompt module
#: because the chunk-context reserve is derived from them; ``notes`` re-exports
#: them so existing callers keep one import site.
#: (see ``gamas_bot._prompt_block``: CONTEXT_HEAD_CHARS,
#: PRESENTATION_CHUNK_TOPIC_MAX_CHARS, CONTEXT_DIGEST_MAX_CHARS,
#: NEIGHBOUR_CONTEXT_CHARS)

#: Conservative characters-per-token ratio for a Persian/English lecture.
#: Persian tokenizes less efficiently than English on every mainstream
#: tokenizer, and an over-estimate produces a request the provider rejects, so
#: this is deliberately low (real Persian text is usually closer to 2.5–3).
CHARS_PER_TOKEN = 2.0
#: Prompt scaffolding that is neither the system prompt nor the transcript
#: (position markers, the JSON envelope, provider boilerplate).
PROMPT_RESERVE_TOKENS = 512
#: How many times one oversized request may be halved before the step gives up
#: (1 → 2 → 4 requests, each cut at a paragraph/sentence boundary).
MAX_CONTEXT_SPLITS = 2
#: Hard ceiling on map requests for one booklet. A tiny window (or a tiny
#: ``NOTE_CHUNK_CHARS``) otherwise turns a long lecture into hundreds of paid
#: requests; the caller then delivers the raw transcript it already has.
MAX_NOTE_CHUNKS = 200

#: How many editorial levels a booklet above the compile budget may cost. A
#: merged booklet that does not fit one request is compiled in whole-section
#: groups (level 1); the only deeper level that can help is one final merge of
#: those groups, which is attempted *only* when the compile actually shortened
#: the booklet enough to fit. Re-grouping an over-budget document cannot shrink
#: it, so no further level is ever attempted.
MAX_COMPILE_LEVELS = 2

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


class StructuringError(RuntimeError):
    """Note generation failed; the caller degrades to the raw transcript."""


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


def _normalise_tables(payload: dict) -> tuple[NoteTable | None, tuple[NoteTable, ...]]:
    """Read ``table`` (legacy) and ``tables`` (v4) into primary + extras.

    The primary table is ``table`` when present, else the first entry of
    ``tables``. ``tables`` keeps every table that is not the primary one, so a
    renderer that knows only ``table`` still shows the first table and the v4
    renderer shows all of them. Identical tables are emitted once.
    """
    primary = NoteTable.from_payload(payload.get("table"))
    extra: list[NoteTable] = []
    seen: set[tuple[str, ...]] = set()
    if primary is not None:
        seen.add(primary.signature())
    raw_tables = payload.get("tables")
    if isinstance(raw_tables, dict):
        # A model that returns one table under ``tables`` means a list of one.
        raw_tables = [raw_tables]
    for item in raw_tables if isinstance(raw_tables, (list, tuple)) else ():
        table = NoteTable.from_payload(item)
        if table is None or table.signature() in seen:
            continue
        seen.add(table.signature())
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
class StructuredNotes:
    """The validated note structure the LLM must produce as strict JSON."""

    title: str = ""
    summary: str = ""
    sections: tuple[NoteSection, ...] = ()
    key_points: tuple[str, ...] = ()
    glossary: tuple[GlossaryEntry, ...] = ()
    note_mode: str = "full"
    learning_objectives: tuple[str, ...] = ()
    review_questions: tuple[str, ...] = ()
    #: The lecturer's own practical advice, surfaced with its source basis.
    practical_tips: tuple[PracticalTip, ...] = ()
    #: Anonymous speaker labels actually resolved for this class (Persian
    #: defaults or the owner's chosen names). Never inferred from the model:
    #: the pipeline fills this from the stored speaker turns.
    speakers: tuple[str, ...] = ()
    #: Timestamped chapter markers derived from paragraph timings, e.g.
    #: «[۰۲:۳۰] مقدمهٔ درس». Purely source-derived, never generated.
    timeline: tuple[str, ...] = ()

    @property
    def display_title(self) -> str:
        return self.title or "جزوهٔ کلاس"

    @property
    def has_content(self) -> bool:
        return bool(
            self.summary
            or self.sections
            or self.key_points
            or self.glossary
            or self.learning_objectives
            or self.review_questions
            or self.practical_tips
        )

    def to_payload(self) -> dict:
        return {
            "title": self.title,
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
            "review_questions": list(self.review_questions),
            "glossary": [
                {"term": entry.term, "term_en": entry.term_en, "definition": entry.definition}
                for entry in self.glossary
            ],
            "speakers": list(self.speakers),
            "timeline": list(self.timeline),
        }

    def to_json(self) -> str:
        if _orjson is not None:
            return _orjson.dumps(self.to_payload()).decode("utf-8")
        return json.dumps(self.to_payload(), ensure_ascii=False)

    def to_markdown(self) -> str:
        """Render as the Markdown subset the Telegram renderer understands."""
        parts: list[str] = [f"# {self.display_title}"]
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
        if self.review_questions:
            parts.append("## پرسش‌های مرور")
            parts.extend(
                f"{index}. {question}"
                for index, question in enumerate(self.review_questions, start=1)
            )
        if self.glossary:
            parts.append("## واژه\u200cنامه")
            parts.extend(
                f"- **{entry.term}"
                + (f" ({entry.term_en})" if entry.term_en else "")
                + f":** {entry.definition}"
                for entry in self.glossary
            )
        if self.speakers or self.timeline:
            parts.append("## گویندگان و زمان\u200cبندی")
            parts.extend(f"- {line}" for line in self.speakers)
            parts.extend(f"- {line}" for line in self.timeline)
        return "\n\n".join(parts)

    @classmethod
    def from_payload(
        cls, payload: object
    ) -> "StructuredNotes":
        """Validate and normalise a model-supplied JSON payload."""
        if not isinstance(payload, dict):
            raise StructuringError("پاسخ سرویس تولید جزوه یک شیء JSON نبود.")
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
        notes = cls(
            title=_bounded_text(payload.get("title"), MAX_TITLE_CHARS),
            summary=_bounded_text(payload.get("summary")),
            sections=sections,
            key_points=tuple(_string_list(payload.get("key_points"))),
            glossary=glossary,
            note_mode=note_mode,
            learning_objectives=tuple(_string_list(payload.get("learning_objectives"))),
            review_questions=tuple(_string_list(payload.get("review_questions"))),
            practical_tips=practical_tips,
            speakers=tuple(_string_list(payload.get("speakers")))[:12],
            timeline=tuple(_string_list(payload.get("timeline")))[:40],
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
    """A JSON schema for the note document the prompts already describe.

    This is a *shape* constraint, not a content one: every field is optional
    except the top-level ``sections`` array (a booklet without sections is the
    failure the prompt itself forbids), and the string arrays are the ones
    ``_load_json_payload`` reads. Providers that enforce it cannot answer with
    prose, which is what removes the second "valid JSON only" request from the
    failure path.
    """
    text_array = {"type": "array", "items": {"type": "string"}}
    section = {
        "type": "object",
        "properties": {
            "heading": {"type": "string"},
            "source_locators": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_SOURCE_LOCATORS_PER_SECTION,
            },
            "paragraphs": text_array,
            "bullets": text_array,
            "definitions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "term": {"type": "string"},
                        "term_en": {"type": "string"},
                        "definition": {"type": "string"},
                    },
                },
            },
            "examples": text_array,
            "steps": text_array,
            "formulas": text_array,
            "key_points": text_array,
            "tables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "caption": {"type": "string"},
                        "headers": text_array,
                        "rows": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
            "callouts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "text": {"type": "string"},
                    },
                },
            },
        },
        "required": ["heading"],
    }
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
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
                },
            },
            "key_points": text_array,
            "review_questions": text_array,
            "glossary": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "term": {"type": "string"},
                        "definition": {"type": "string"},
                    },
                },
            },
        },
        "required": ["sections"],
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


def _dedupe_definitions(definitions: tuple[NoteDefinition, ...]) -> tuple[NoteDefinition, ...]:
    by_term: dict[str, NoteDefinition] = {}
    order: list[str] = []
    for entry in definitions:
        term_key = _compare_key(entry.term)
        if not term_key:
            continue
        existing = by_term.get(term_key)
        if existing is None:
            by_term[term_key] = entry
            order.append(term_key)
        elif len(entry.definition) > len(existing.definition):
            by_term[term_key] = entry
    return tuple(by_term[term] for term in order)


def _dedupe_callouts(callouts: tuple[NoteCallout, ...]) -> tuple[NoteCallout, ...]:
    seen: set[tuple[str, str]] = set()
    result: list[NoteCallout] = []
    for callout in callouts:
        identity = (callout.kind, _compare_key(callout.text))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(callout)
    return tuple(result)


def _dedupe_table_rows(rows: list[list[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    result: list[list[str]] = []
    for row in rows:
        identity = tuple(_compare_key(cell) for cell in row)
        if identity in seen:
            continue
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
    primary: NoteTable | None = None
    extras: list[NoteTable] = []
    for table in tables:
        if primary is None:
            primary = table
            continue
        if table.signature() == primary.signature():
            primary = NoteTable(
                list(primary.headers),
                _dedupe_table_rows([list(row) for row in primary.rows] + [list(row) for row in table.rows]),
                primary.caption or table.caption,
            )
            continue
        for index, existing in enumerate(extras):
            if existing.signature() == table.signature():
                extras[index] = NoteTable(
                    list(existing.headers),
                    _dedupe_table_rows(
                        [list(row) for row in existing.rows] + [list(row) for row in table.rows]
                    ),
                    existing.caption or table.caption,
                )
                break
        else:
            extras.append(table)
    return primary, tuple(extras)


def _combine_sections(first: NoteSection, second: NoteSection) -> NoteSection:
    table, tables = _merge_table_group(first.all_tables + second.all_tables)
    return replace(
        first,
        paragraphs=_dedupe_by_key(first.paragraphs + second.paragraphs, _compare_key),
        bullets=_dedupe_by_key(first.bullets + second.bullets, _compare_key),
        definitions=_dedupe_definitions(first.definitions + second.definitions),
        examples=_dedupe_by_key(first.examples + second.examples, _compare_key),
        steps=_dedupe_by_key(first.steps + second.steps, _compare_key),
        formulas=_dedupe_by_key(first.formulas + second.formulas, _compare_key),
        key_points=_dedupe_by_key(first.key_points + second.key_points, _compare_key),
        callouts=_dedupe_callouts(first.callouts + second.callouts),
        table=table,
        tables=tables,
        source_locators=tuple(
            dict.fromkeys(first.source_locators + second.source_locators)
        )[:MAX_SOURCE_LOCATORS_PER_SECTION],
    )


def _block_keys(section: NoteSection) -> tuple[str, ...]:
    keys: list[str] = []
    keys.extend("p|" + _compare_key(value) for value in section.paragraphs if value)
    keys.extend("b|" + _compare_key(value) for value in section.bullets if value)
    keys.extend(
        "d|" + _compare_key(entry.term) + "|" + _compare_key(entry.definition)
        for entry in section.definitions
    )
    keys.extend("e|" + _compare_key(value) for value in section.examples if value)
    keys.extend("s|" + _compare_key(value) for value in section.steps if value)
    keys.extend("f|" + _compare_key(value) for value in section.formulas if value)
    keys.extend("k|" + _compare_key(value) for value in section.key_points if value)
    keys.extend("c|" + callout.kind + "|" + _compare_key(callout.text) for callout in section.callouts)
    for table in section.all_tables:
        headers = "|".join(_compare_key(header) for header in table.headers)
        keys.extend(
            headers + "||" + "|".join(_compare_key(cell) for cell in row)
            for row in table.rows
        )
    return tuple(key for key in keys if not key.endswith("|"))


def _same_logical_section(first: NoteSection, second: NoteSection, *, across_chunk: bool) -> bool:
    if not heading_topic_key(first.heading) and not heading_topic_key(second.heading):
        return False
    if headings_share_a_topic(first.heading, second.heading):
        return True
    return across_chunk and headings_overlap(first.heading, second.heading)


def _merge_sections(
    sections: list[NoteSection],
    *,
    chunk_starts: frozenset[int] = frozenset(),
) -> tuple[NoteSection, ...]:
    """Merge sections additively, in order, with conservative de-duplication."""
    merged: list[NoteSection] = []
    seen_text: set[str] = set()
    seen_blocks: set[str] = set()
    pending_chunk_boundary = False
    for position, section in enumerate(sections):
        if position in chunk_starts:
            pending_chunk_boundary = True
        paragraphs = _dedupe_by_key(section.paragraphs, _compare_key)
        bullets = _dedupe_by_key(section.bullets, _compare_key)
        paragraphs = tuple(
            value for value in paragraphs if _compare_key(value) not in seen_text
        )
        seen_text.update(_compare_key(value) for value in paragraphs)
        bullets = tuple(value for value in bullets if _compare_key(value) not in seen_text)
        seen_text.update(_compare_key(value) for value in bullets)
        cleaned = replace(
            section,
            paragraphs=paragraphs,
            bullets=bullets,
            definitions=_dedupe_definitions(section.definitions),
            examples=_dedupe_by_key(section.examples, _compare_key),
            steps=_dedupe_by_key(section.steps, _compare_key),
            formulas=_dedupe_by_key(section.formulas, _compare_key),
            key_points=_dedupe_by_key(section.key_points, _compare_key),
            callouts=_dedupe_callouts(section.callouts),
        )
        if not cleaned.has_content:
            continue
        keys = _block_keys(cleaned)
        if keys and seen_blocks and all(key in seen_blocks for key in keys):
            continue
        seen_blocks.update(keys)
        across_chunk = pending_chunk_boundary
        pending_chunk_boundary = False
        if merged and _same_logical_section(
            merged[-1], cleaned, across_chunk=across_chunk
        ):
            merged[-1] = _combine_sections(merged[-1], cleaned)
        else:
            merged.append(cleaned)
    return tuple(merged)


_SENTENCE_SPAN = re.compile(r"(?:[!?؟…]+|؛|(?<!\d)\.(?!\d))[»)\]”\"']*")


def _merge_summaries(notes: list[StructuredNotes], *, limit: int = 1200) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for item in notes:
        if not item.summary:
            continue
        start = 0
        for match in _SENTENCE_SPAN.finditer(item.summary):
            text = item.summary[start : match.end()].strip()
            start = match.end()
            if not text:
                continue
            identity = _compare_key(text)
            if identity in seen:
                continue
            seen.add(identity)
            parts.append(text)
        tail = item.summary[start:].strip()
        if tail:
            identity = _compare_key(tail)
            if identity not in seen:
                seen.add(identity)
                parts.append(tail)
    return " ".join(parts)[:limit].rstrip()


def _dedupe_glossary(notes: list[StructuredNotes]) -> tuple[GlossaryEntry, ...]:
    by_term: dict[str, GlossaryEntry] = {}
    term_order: list[str] = []
    for item in notes:
        for entry in item.glossary:
            key = _compare_key(entry.term)
            if not key:
                continue
            if key not in by_term:
                by_term[key] = entry
                term_order.append(key)
            elif len(entry.definition) > len(by_term[key].definition):
                by_term[key] = GlossaryEntry(entry.term, entry.definition)
    return tuple(by_term[term] for term in term_order)


def _dedupe_tips(tips: Sequence[PracticalTip]) -> tuple[PracticalTip, ...]:
    """One copy of each tip; a longer basis wins because it is better evidence."""
    by_text: dict[str, PracticalTip] = {}
    order: list[str] = []
    for tip in tips:
        key = _compare_key(tip.text)
        if not key:
            continue
        if key not in by_text:
            by_text[key] = tip
            order.append(key)
        elif len(tip.basis) > len(by_text[key].basis):
            by_text[key] = tip
    return tuple(by_text[key] for key in order)


def merge_structured_notes(notes: list[StructuredNotes]) -> StructuredNotes:
    """Combine per-chunk notes into one document, preserving order."""
    if not notes:
        raise StructuringError("پاسخ سرویس تولید جزوه خالی بود.")
    if len(notes) == 1:
        return notes[0]
    chunk_starts: set[int] = set()
    cursor = 0
    for item in notes[:-1]:
        cursor += len(item.sections)
        chunk_starts.add(cursor)
    merged = StructuredNotes(
        title=next((item.title for item in notes if item.title), ""),
        summary=_merge_summaries(notes),
        sections=_merge_sections(
            [section for item in notes for section in item.sections],
            chunk_starts=frozenset(chunk_starts),
        ),
        key_points=_dedupe_by_key(
            [point for item in notes for point in item.key_points], _compare_key
        ),
        glossary=_dedupe_glossary(notes),
        # CR-3: every merged booklet is FULL, regardless of legacy stored values.
        note_mode="full",
        learning_objectives=_dedupe_by_key(
            [item for note in notes for item in note.learning_objectives], _compare_key
        ),
        review_questions=_dedupe_by_key(
            [item for note in notes for item in note.review_questions], _compare_key
        ),
        practical_tips=_dedupe_tips([tip for note in notes for tip in note.practical_tips]),
    )
    if not merged.has_content:
        raise StructuringError("ساختار جزوهٔ دریافتی خالی بود.")
    return merged


# ===========================================================================
# JSON parsing
# ===========================================================================


def _load_json_payload(payload: dict, *, mode: str = "full") -> StructuredNotes:
    try:
        return StructuredNotes.from_payload(payload)
    except (KeyError, TypeError) as exc:
        raise StructuringError("پاسخ سرویس تولید جزوه ساختار معتبری نداشت.") from exc


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
    """A compact orientation built deterministically from the chunk openings."""

    title: str = ""
    topics: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.title or self.topics)

    def topic_for(self, index: int, total: int) -> str:
        if 0 < index <= len(self.topics):
            return self.topics[index - 1]
        return ""


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


def build_context(chunks: list[str]) -> LectureContext:
    """Topics = slide headings in each chunk (for decks) or the opening sentence.

    This is the v3 simplification of v2's orientation LLM call: the slide
    headings or opening of a chunk state its topics, so the digest is
    deterministic, free and instant.
    """
    topics: list[str] = []
    used_chars = 0
    for chunk in chunks:
        topic = _chunk_topic_summary(chunk)
        if not topic:
            continue
        position = to_persian_digits(len(topics) + 1)
        rendered_topic = f"({position}) {topic}"
        separator_chars = 2 if topics else 0  # the Persian comma and following space
        addition_chars = separator_chars + len(rendered_topic)
        if used_chars + addition_chars > CONTEXT_DIGEST_MAX_CHARS:
            break
        topics.append(topic)
        used_chars += addition_chars
    return LectureContext(title="", topics=tuple(topics))


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
    if context.topics:
        listed = "، ".join(
            f"({to_persian_digits(position)}) {topic}"
            for position, topic in enumerate(context.topics, start=1)
        )
        lines.append("موضوع‌های درس به ترتیب: " + listed)
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

#: Minimum share of source instructional units the draft must cover.
COMPLETENESS_THRESHOLD = 0.60
#: A source unit counts as covered when at least this share of its content words
#: appear in the draft (or its topic clearly overlaps a section heading).
COMPLETENESS_UNIT_RATIO = 0.5
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
        # final one-page «جمع‌بندی» is rendered from summary / key_points
        "end_summary": bool(notes.summary.strip()) or bool(notes.key_points),
        # English-term glossary is rendered from glossary or section definitions
        "glossary": bool(notes.glossary) or any(s.definitions for s in sections),
    }


def _drafted_text(notes: StructuredNotes) -> str:
    parts: list[str] = [notes.title, notes.summary]
    parts.extend(notes.learning_objectives)
    parts.extend(notes.key_points)
    parts.extend(notes.review_questions)
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
    """Fill all detected coverage gaps in one grounded, bounded request.

    Failure is non-fatal: retain the audited draft and never spin up one request
    per missing fact. The repaired payload is locally grounded before merging.
    """
    report = assess_completeness(source, notes)
    if report.meets_threshold:
        return notes
    gaps = report.missing_units[:MAX_COVERAGE_GAPS]
    if not gaps:
        # Skeleton-only shortfall: an LLM would have to invent structure. Keep
        # the grounded draft; never fabricate a glossary/summary to pass a gate.
        logger.info("note completeness %s (skeleton-only shortfall; no fill)", report.summary())
        return notes
    message = build_fill_document(gaps, notes, source)
    try:
        if semaphore is not None:
            async with semaphore:
                payload = await complete_json(
                    session, settings,
                    system=COVERAGE_FILL_SYSTEM_PROMPT,
                    user=message,
                    schema=_response_schema(settings),
                    task="BOOKLET_REPAIR",
                )
        else:
            payload = await complete_json(
                session, settings,
                system=COVERAGE_FILL_SYSTEM_PROMPT,
                user=message,
                schema=_response_schema(settings),
                task="BOOKLET_REPAIR",
            )
        filled = _load_json_payload(payload)
    except Exception as exc:  # noqa: BLE001 - preserve the audited draft
        logger.warning("coverage fill request failed: %s", type(exc).__name__)
        return notes
    if not filled.has_content:
        return notes
    if getattr(settings, "note_strict_grounding", False):
        filled, filled_report = gate_notes(
            filled, source, strict=True, mode=settings.note_mode
        )
        if filled_report.audit.fabricated:
            filled, _ = scrub_fabricated_numbers(filled, filled_report.audit)
            filled, filled_report = gate_notes(
                filled, source, strict=True, mode=settings.note_mode
            )
        if not filled.has_content or filled_report.audit.fabricated:
            logger.warning("coverage fill rejected by grounding gate")
            return notes
    merged = merge_structured_notes([notes, filled])
    logger.info("note completeness after fill: %s", assess_completeness(source, merged).summary())
    return merged


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
    if notes.summary:
        payload["summary"] = notes.summary
    if notes.learning_objectives:
        payload["learning_objectives"] = list(notes.learning_objectives)
    sections: list[dict] = [section_payload(section) for section in notes.sections]
    payload["sections"] = sections
    if notes.key_points:
        payload["key_points"] = list(notes.key_points)
    if notes.review_questions:
        payload["review_questions"] = list(notes.review_questions)
    if notes.practical_tips:
        payload["practical_tips"] = [
            {"text": tip.text, "basis": tip.basis, "kind": tip.kind}
            for tip in notes.practical_tips
        ]
    if notes.glossary:
        payload["glossary"] = [
            {"term": e.term, "definition": e.definition} for e in notes.glossary
        ]
    return payload


def build_compile_document(context: LectureContext, notes: StructuredNotes) -> str:
    """The editorial compile input: the source digest, then the merged notes.

    The order is the template order of the booklet agent: the transcript-like
    material first, the artifact being edited last, so the mutable scaffolding
    stays a stable prefix and the model reads the draft immediately before it
    starts writing. The edit instructions live in the system prompt; the
    per-request "leave these out / put these back" material is appended by the
    repair builder.
    """
    return build_edit_message(build_source_digest(context), _notes_draft_json(notes))


def build_source_digest(context: LectureContext) -> str:
    """The source side of the editorial request: the lesson's topic map.

    The compile request does not re-send the whole transcript (it would blow
    the budget); it sends the deterministic topic map of the lesson, which is
    what the merged draft is edited *against*.
    """
    if not context.topics:
        return "موضوع‌های درس به ترتیب در دسترس نیست."
    listed = "، ".join(
        f"({to_persian_digits(position)}) {topic}"
        for position, topic in enumerate(context.topics, start=1)
    )
    return "موضوع‌های درس به ترتیب: " + listed


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
    return total


def compile_accepts(compiled: StructuredNotes, merged: StructuredNotes) -> bool:
    """The deterministic acceptance gate for the compile answer.

    Restructuring may tighten prose and merge sections, but a result that guts
    the booklet is rejected and the deterministic merge is delivered instead.
    """
    if not compiled.has_content or not compiled.sections:
        return False
    if not merged.sections:
        return True
    ratio = len(compiled.sections) / len(merged.sections)
    if ratio < COMPILE_MIN_SECTION_RATIO:
        logger.warning(
            "compile rejected: section ratio %.2f (%d -> %d)",
            ratio, len(merged.sections), len(compiled.sections),
        )
        return False
    merged_structured = _notes_structured_item_count(merged)
    if merged_structured >= 2:
        compiled_structured = _notes_structured_item_count(compiled)
        structured_ratio = compiled_structured / merged_structured
        if structured_ratio < COMPILE_MIN_SECTION_RATIO:
            logger.warning(
                "compile rejected: structured item ratio %.2f (%d -> %d)",
                structured_ratio, merged_structured, compiled_structured,
            )
            return False
    merged_chars = _notes_body_chars(merged)
    if merged_chars / len(merged.sections) >= COMPILE_SUBSTANTIVE_SECTION_CHARS:
        compiled_chars = _notes_body_chars(compiled)
        char_ratio = compiled_chars / merged_chars if merged_chars else 1.0
        if char_ratio < COMPILE_MIN_CONTENT_RATIO:
            logger.warning(
                "compile rejected: body char ratio %.2f (%d -> %d)",
                char_ratio, merged_chars, compiled_chars,
            )
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
    source_index = build_source_index(source)
    source_words = source_index.masked_words
    tables_seen = tables_accepted = tips_seen = tips_accepted = 0
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
        cleaned_section = replace(
            section,
            table=accepted_tables[0] if accepted_tables else None,
            tables=tuple(accepted_tables[1:]),
        )
        if cleaned_section.has_content:
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
        practical_tips=tuple(tips[:MAX_PRACTICAL_TIPS]),
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
        dropped_cells=tuple(dict.fromkeys(dropped_cells)),
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
        or audit.number_coverage < number_floor
        or audit.term_coverage < term_floor
        or report.coverage.ratio < 0.75
    )


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
) -> str:
    """The one repair request: the notes, what was lost, and where it came from.

    Only the *missing* facts and the source windows around them are added to
    the request — never the whole transcript — so restoring them is copying a
    sentence the model can see, not recalling one it cannot.

    ``sections`` restricts the request to a bounded subset of the booklet (used
    when the whole booklet does not fit the repair budget). The answer then
    carries only those sections and is merged back by heading.

    Unlike the compile request this one does not carry the whole source: only
    the lost units and the source sentence under each of them travel here, so
    the request stays inside the repair budget however long the lecture is.
    The draft is last, immediately before the model's own output.
    """
    targeted = sections is not None
    booklet = _subset_notes(notes, sections) if sections is not None else notes
    draft = (
        DRAFT_HEADING + "\n" + json.dumps(compact_notes_payload(booklet), ensure_ascii=False)
    )
    parts: list[str] = []
    audit = report.audit
    if audit.missing:
        windows = evidence_windows(
            source, audit.missing[:MAX_REPAIR_SIGNALS], radius=REPAIR_WINDOW_CHARS
        )
        lines = [
            "### موارد جامانده از جزوه (هر مورد را با جملهٔ زیرش، در جای درست خودش برگردانید)"
        ]
        for signal, window in windows:
            lines.append(f"- «{signal.label}» — جملهٔ منبع: {window}")
        for signal in audit.missing[:MAX_REPAIR_SIGNALS]:
            if not any(signal is item for item, _ in windows):
                lines.append(f"- «{signal.label}» — شمارهٔ منبع پیدا نشد؛ فقط اگر در جزوه هست بازگردانید.")
        parts.append("\n".join(lines))
    if audit.fabricated:
        labels = "، ".join(f"«{signal.label}»" for signal in audit.fabricated[:MAX_REPAIR_SIGNALS])
        parts.append(
            "### مواردی که در متن منبع نیست\n"
            + labels
            + "\nاین موارد را از جزوه حذف کنید (یا اگر صورت درست‌شان در متن است، با همان صورت بنویسید). "
            "چیز دیگری اضافه نکنید."
        )
    number_floor, term_floor = coverage_thresholds(resolve_note_mode(mode))
    scope = (
        "فقط همین بخش‌ها را (به همان ترتیب و با همان عنوان) برگردانید؛ "
        "بخش دیگری از جزوه در این درخواست نیست.\n"
        if targeted
        else ""
    )
    parts.append(
        "### دستور\n"
        + scope
        + "همان جزوه را با همان ساختار JSON برگردانید و فقط این دو کار را بکنید:\n"
        f"- موارد جامانده را در بخش مناسب برگردانید (پوشش اعداد باید دست‌کم {number_floor:.0%} و "
        f"پوشش اصطلاح‌ها دست‌کم {term_floor:.0%} باشد).\n"
        "- مواردی را که در منبع نیست حذف کنید.\n"
        "هیچ جملهٔ تازه، عدد تازه، تعریف تازه یا نکتهٔ تازه‌ای نسازید؛ هیچ بخشی را حذف نکنید."
    )
    parts.append(draft)
    return "\n\n".join(parts)


_SCRUB_SENTENCE = re.compile(r"(?<=[.!?؟…])\s+")


def _scrub_text(text: str, fabricated_values: frozenset[str]) -> tuple[str, int]:
    """Drop the sentences that assert a number the source does not contain.

    A fabricated number cannot be "corrected" without inventing the right one,
    and it cannot be shipped as fact. The smallest honest unit is the sentence:
    it is removed whole (never truncated), and the rest of the paragraph is
    kept in order. Removing a sentence can cost a real fact, which the audit
    then reports as missing — an omission is recoverable and visible, an
    invented dosage is neither.
    """
    if not text or not fabricated_values:
        return text, 0
    kept: list[str] = []
    removed = 0
    for sentence in _SCRUB_SENTENCE.split(text):
        if not sentence.strip():
            continue
        numbers = extract_signals(sentence, mask=False)
        if any(
            signal.kind == "number" and signal.value in fabricated_values
            for signal in numbers
        ):
            removed += 1
            continue
        kept.append(sentence.strip())
    return " ".join(kept), removed


def scrub_fabricated_numbers(
    notes: StructuredNotes, audit: SignalAudit
) -> tuple[StructuredNotes, int]:
    """Remove every sentence that states a number absent from the source."""
    values = frozenset(
        signal.value for signal in audit.fabricated if signal.kind == "number"
    )
    if not values:
        return notes, 0
    removed = 0

    def scrub(value: str) -> str:
        nonlocal removed
        cleaned, count = _scrub_text(value, values)
        removed += count
        return cleaned

    def scrub_tuple(values_in: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = (scrub(item) for item in values_in)
        return tuple(item for item in cleaned if item.strip())

    sections: list[NoteSection] = []
    for section in notes.sections:
        definitions = []
        for entry in section.definitions:
            definition = scrub(entry.definition)
            if definition.strip():
                definitions.append(replace(entry, definition=definition))
        callouts = []
        for callout in section.callouts:
            text = scrub(callout.text)
            if text.strip():
                callouts.append(replace(callout, text=text))
        tables = []
        for table in section.all_tables:
            rows = []
            for row in table.rows:
                cells = [scrub(cell) for cell in row]
                if any(cell.strip() for cell in cells):
                    rows.append(cells)
            if rows:
                tables.append(replace(table, rows=rows))
        cleaned = replace(
            section,
            heading=scrub(section.heading),
            paragraphs=scrub_tuple(section.paragraphs),
            bullets=scrub_tuple(section.bullets),
            definitions=tuple(definitions),
            examples=scrub_tuple(section.examples),
            steps=scrub_tuple(section.steps),
            formulas=scrub_tuple(section.formulas),
            key_points=scrub_tuple(section.key_points),
            callouts=tuple(callouts),
            table=tables[0] if tables else None,
            tables=tuple(tables[1:]),
        )
        if cleaned.has_content:
            sections.append(cleaned)
    tips = []
    for tip in notes.practical_tips:
        text = scrub(tip.text)
        if text.strip():
            tips.append(replace(tip, text=text))
    glossary = []
    for glossary_entry in notes.glossary:
        definition = scrub(glossary_entry.definition)
        if definition.strip():
            glossary.append(replace(glossary_entry, definition=definition))
    scrubbed = replace(
        notes,
        title=scrub(notes.title),
        summary=scrub(notes.summary),
        sections=tuple(sections),
        key_points=scrub_tuple(notes.key_points),
        learning_objectives=scrub_tuple(notes.learning_objectives),
        review_questions=scrub_tuple(notes.review_questions),
        practical_tips=tuple(tips),
        glossary=tuple(glossary),
    )
    if removed:
        logger.warning("note scrub removed %d sentence(s) with an unsupported number", removed)
    return scrubbed, removed


def repair_improves(candidate: NoteQualityReport, current: NoteQualityReport) -> bool:
    """A repair is accepted only if it is grounded and loses no ground."""
    if candidate.audit.fabricated:
        return False
    if candidate.audit.number_coverage < current.audit.number_coverage:
        return False
    if candidate.audit.term_coverage < current.audit.term_coverage - 0.05:
        return False
    if candidate.coverage.ratio < current.coverage.ratio - 0.05:
        return False
    return candidate.audit.coverage > current.audit.coverage or candidate.coverage.ratio > current.coverage.ratio


# ===========================================================================
# The map-reduce pipeline
# ===========================================================================


def effective_chunk_chars(
    settings: Settings, *, system_prompt: str, extra_reserve_chars: int = 0
) -> int:
    """The character budget of one note request, derived from the context window.

    A character count says nothing about whether a request fits: the expected
    output, the system prompt and the contextual material all live in the same
    window. This subtracts them from ``NOTE_CONTEXT_TOKENS`` and converts what is
    left with a deliberately conservative ratio, so a model with a smaller window
    receives smaller chunks instead of a request the provider rejects.
    ``NOTE_CHUNK_CHARS`` remains an upper bound — a smaller configured chunk is
    never grown. The budget is never floored to a minimum that would exceed the
    model's configured window.
    """
    reserve_characters = len(system_prompt) + max(0, extra_reserve_chars)
    reserve_tokens = math.ceil(reserve_characters / CHARS_PER_TOKEN) + PROMPT_RESERVE_TOKENS
    available_tokens = (
        settings.note_context_tokens - settings.note_max_output_tokens - reserve_tokens
    )
    if available_tokens <= 0:
        raise StructuringError(
            "پنجرهٔ زمینهٔ مدل پس از کسر دستور جزوه و سقف پاسخ، فضایی برای متن ورودی ندارد؛ "
            "NOTE_MAX_OUTPUT_TOKENS را کم کنید یا NOTE_CONTEXT_TOKENS را افزایش دهید."
        )
    context_budget = max(1, int(available_tokens * CHARS_PER_TOKEN))
    # ``NOTE_CHUNK_CHARS`` may be smaller than the window allows; it is never grown.
    return max(1, min(settings.note_chunk_chars, context_budget))


def effective_compile_max_chars(
    settings: Settings, *, mode: str = "full", presentation: bool = False
) -> int:
    """The character budget of the merged notes in one compile request.

    The compile (reduce) request lives in the same window as every other note
    request: its system prompt, the topic digest wrapped around the merged
    notes and the reserved output all compete with the notes themselves. The
    budget is therefore derived with the same math as
    :func:`effective_chunk_chars` (minus the ``NOTE_CHUNK_CHARS`` cap, which
    sizes map chunks, not the reduce input) instead of the former fixed
    character bound, which silently disabled the editorial pass for every
    booklet above it however large the model's window was.

    ``NOTE_COMPILE_MAX_CHARS`` (> 0) is an additional operator cap; it cannot
    enlarge the request beyond the context-derived budget. The correct system
    prompt for the mode and source type is reserved, as is the bounded topic
    digest. The derived budget is also bounded by ``COMPILE_HARD_MAX_CHARS``, so
    a very large configured window cannot turn the reduce step into one
    unbounded request. A merged booklet above this budget is compiled in whole
    section groups instead of being skipped (see :func:`_compile_pass`).
    """
    reserve_characters = len(edit_system_prompt()) + EDIT_MESSAGE_RESERVE_CHARS
    reserve_tokens = math.ceil(reserve_characters / CHARS_PER_TOKEN) + PROMPT_RESERVE_TOKENS
    available_tokens = (
        settings.note_context_tokens - settings.note_max_output_tokens - reserve_tokens
    )
    if available_tokens <= 0:
        return 0
    derived = min(int(available_tokens * CHARS_PER_TOKEN), COMPILE_HARD_MAX_CHARS)
    if settings.note_compile_max_chars > 0:
        return min(settings.note_compile_max_chars, derived)
    return derived


#: Hard ceiling on concurrent note requests. More in-flight requests than a
#: provider will serve only converts a serial queue into 429s.
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


async def _structure_piece(
    session: aiohttp.ClientSession,
    settings: Settings,
    document: str,
    *,
    system_prompt: str,
    mode: str,
    semaphore: asyncio.Semaphore,
    depth: int = 0,
    presentation: bool = False,
    render: Callable[[str], str],
) -> StructuredNotes:
    """One note request, with bounded recovery from an oversized or dense input.

    ``document`` is raw source text, never a rendered message: ``render`` turns
    it into the user message (see ``build_chunk_message``) so that a split can
    re-render both halves with the same header and topic instead of cutting a
    message in two.

    A context overflow or output-budget truncation is the provider failure that
    smaller input chunks can fix, so the input is cut at a slide/paragraph/
    sentence boundary and the halves are requested instead. The depth is
    bounded and an input with no safe cut point is given up: an unchanged
    oversized request is never re-sent.
    """
    try:
        async with semaphore:
            payload = await complete_json(
                session,
                settings,
                system=system_prompt,
                user=render(document),
                schema=_response_schema(settings),
                task="BOOKLET_GENERATION",
            )
        return _load_json_payload(payload, mode=mode)
    except (LLMContextOverflowError, LLMOutputBudgetError) as exc:
        if depth >= MAX_CONTEXT_SPLITS:
            raise
        splitter = split_presentation_document if presentation else split_transcript
        half_budget = max(1, len(document) // 2)
        if presentation:
            first_marker = _SLIDE_TOPIC_HEADING_RE.search(document)
            if first_marker and first_marker.start() > 0:
                balanced = (len(document) + first_marker.start()) // 2 + 2
                candidate = splitter(document, balanced)
                pieces = candidate if len(candidate) >= 2 else splitter(document, half_budget)
            else:
                pieces = splitter(document, half_budget)
        else:
            pieces = splitter(document, half_budget)
        if len(pieces) < 2:
            raise
        logger.warning(
            "note request exceeded model budget; splitting into %d part(s) depth=%d (%s)",
            len(pieces), depth + 1, exc,
        )
        tasks = [
            asyncio.ensure_future(
                _structure_piece(
                    session, settings, piece,
                    system_prompt=system_prompt, mode=mode,
                    semaphore=semaphore, depth=depth + 1,
                    presentation=presentation, render=render,
                )
            )
            for piece in pieces
        ]
        try:
            parts = await asyncio.gather(*tasks)
        except BaseException:
            # ``gather`` propagates immediately but leaves the siblings running:
            # they would keep the semaphore, the HTTP session and the provider
            # quota busy for a result nobody reads.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return merge_structured_notes(list(parts))


async def _audit_and_repair(
    notes: StructuredNotes,
    source: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str,
    presentation: bool,
    semaphore: asyncio.Semaphore,
    source_locators: bool = False,
) -> tuple[StructuredNotes, NoteQualityReport]:
    """Gate the notes, then make at most one bounded repair request.

    The gate is deterministic and free. The repair is one request that carries
    only the missing facts and their source windows; it is skipped when the
    request would not fit the configured budget, when the operator chose
    ``warn`` mode, or when nothing is actually wrong.

    In ``warn`` mode the notes are returned **unchanged**: the audit still runs
    and is logged, so the operator can read what the gate would have dropped on
    real lectures before it is allowed to drop anything.
    """
    if not settings.note_strict_grounding:
        # Defensive: every caller already skips this step when grounding is off.
        return notes, gate_notes(notes, source, strict=False, mode=mode)[1]
    if settings.note_quality_gate != "repair":
        observed, report = gate_notes(notes, source, strict=False, mode=mode)
        # ``warn`` ships exactly what the gate would have removed, so the log
        # line is the only trace of a real fidelity problem: it is a warning.
        logger.warning(
            "note quality gate=warn: nothing changed; %s", report.summary()
        )
        return observed, report
    gated, report = gate_notes(notes, source, strict=True, mode=mode)
    if notes_need_repair(report, mode=mode):
        gated, report = await _repair_once(
            gated, report, source, settings, session,
            mode=mode, presentation=presentation, semaphore=semaphore,
            source_locators=source_locators,
        )
    if report.audit.fabricated:
        scrubbed, removed = scrub_fabricated_numbers(gated, report.audit)
        if removed:
            gated, report = gate_notes(scrubbed, source, strict=True, mode=mode)
    return gated, report


def repair_request_document(
    notes: StructuredNotes,
    report: NoteQualityReport,
    source: str,
    *,
    budget: int,
    mode: str = "full",
) -> tuple[str, Sequence[NoteSection]] | None:
    """The repair request, whole if it fits, otherwise targeted at sections.

    Returns ``(document, sections)`` where ``sections`` is the repaired subset
    (the whole booklet in the fitting case), or ``None`` when not even one
    section fits the budget — a request that cannot fit is never sent.

    A booklet above the budget used to skip the repair pass *silently*, so a
    multi-hour lecture shipped with whatever the first pass produced. Only the
    sections that hold the lost facts are re-sent now, and the answer is merged
    back by heading: the rest of the booklet is delivered exactly as audited.
    """
    if budget <= 0:
        return None
    whole = build_repair_document(notes, report, source, mode=mode)
    if len(whole) <= budget:
        return whole, notes.sections
    chosen = repair_target_indexes(notes, report, source)
    while chosen:
        sections = [notes.sections[index] for index in sorted(chosen)]
        document = build_repair_document(
            notes, report, source, mode=mode, sections=sections
        )
        if len(document) <= budget:
            logger.info(
                "repair pass targeted: whole_chars=%d request_chars=%d budget=%d sections=%d/%d",
                len(whole), len(document), budget, len(sections), len(notes.sections),
            )
            return document, sections
        # Drop the least relevant section and try again: a section is never
        # split, and a request over the budget would be rejected anyway.
        chosen.pop()
    logger.warning(
        "repair skipped: request_chars=%d budget=%d", len(whole), budget
    )
    return None


def merge_repaired_sections(
    notes: StructuredNotes, repaired: Sequence[NoteSection]
) -> tuple[StructuredNotes, int]:
    """Put the repaired sections back where they came from, by heading.

    Headings are the only stable identity across the request (the model may
    reflow a paragraph, never the section it belongs to). A section the model
    returns that matches nothing is not merged — inventing a section is exactly
    the failure this merge must not amplify. Returns the notes and the number of
    sections that could not be placed.
    """
    indexes: dict[str, int] = {}
    for index, section in enumerate(notes.sections):
        key = heading_topic_key(section.heading)
        if key and key not in indexes:
            indexes[key] = index
    merged = list(notes.sections)
    placed: set[int] = set()
    unplaced = 0
    for section in repaired:
        match = indexes.get(heading_topic_key(section.heading))
        if match is None:
            # A reworded heading still refers to one topic: accept the unique
            # unplaced section it clearly overlaps, never an ambiguous one.
            candidates = [
                position
                for position, original in enumerate(notes.sections)
                if position not in placed
                and headings_overlap(section.heading, original.heading)
            ]
            match = candidates[0] if len(candidates) == 1 else None
        if match is None or match in placed:
            unplaced += 1
            continue
        merged[match] = section
        placed.add(match)
    return replace(notes, sections=tuple(merged)), unplaced


async def _repair_once(
    notes: StructuredNotes,
    report: NoteQualityReport,
    source: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str,
    presentation: bool,
    semaphore: asyncio.Semaphore,
    source_locators: bool = False,
) -> tuple[StructuredNotes, NoteQualityReport]:
    """One bounded repair request; the caller keeps its state on any failure."""
    budget = effective_compile_max_chars(settings, mode=mode, presentation=presentation)
    request = repair_request_document(notes, report, source, budget=budget, mode=mode)
    if request is None:
        return notes, report
    repair_document, repaired_sections = request
    targeted = len(repaired_sections) < len(notes.sections)
    _ = source_locators
    system_prompt = edit_system_prompt()
    try:
        async with semaphore:
            payload = await complete_json(
                session,
                settings,
                system=system_prompt,
                user=repair_document,
                schema=_response_schema(settings),
                task="BOOKLET_REPAIR",
            )
        candidate = _load_json_payload(payload, mode=mode)
    except (LLMError, StructuringError) as exc:
        logger.warning("repair pass failed (%s); keeping the audited notes", type(exc).__name__)
        return notes, report
    if targeted:
        candidate, unplaced = merge_repaired_sections(notes, candidate.sections)
        if unplaced:
            logger.warning(
                "repair returned %d section(s) with no place in the booklet; ignored",
                unplaced,
            )
    candidate, candidate_report = gate_notes(candidate, source, strict=True, mode=mode)
    if repair_improves(candidate_report, report):
        logger.info("repair accepted %s", candidate_report.summary())
        return candidate, candidate_report
    logger.info("repair rejected %s", candidate_report.summary())
    return notes, report


async def _compile_once(
    session: aiohttp.ClientSession,
    settings: Settings,
    context: LectureContext,
    notes: StructuredNotes,
    *,
    mode: str,
    presentation: bool,
    budget: int,
    source_locators: bool = False,
) -> StructuredNotes | None:
    """One editorial compile request, or ``None`` when it cannot be sent.

    The deterministic acceptance gate is applied here, so every caller (the
    whole-booklet path and each group of a grouped compile) gets the same
    "restructure, never gut" guarantee.
    """
    document = build_compile_document(context, notes)
    if budget <= 0 or len(document) > budget:
        logger.warning(
            "compile skipped: request_chars=%d budget=%d sections=%d",
            len(document), budget, len(notes.sections),
        )
        return None
    # One static editorial prompt for every source kind (see
    # ``_prompt_block.edit_system_prompt``): the digest, the draft and the
    # locator contract all live in the user message.
    _ = source_locators
    try:
        payload = await complete_json(
            session,
            settings,
            system=edit_system_prompt(),
            user=document,
            schema=_response_schema(settings),
            task="BOOKLET_COMPILE",
            prefer_provider=getattr(settings, "note_compile_provider", ""),
        )
        compiled = _load_json_payload(payload, mode=mode)
    except (LLMError, StructuringError) as exc:
        logger.warning("compile pass failed (%s); delivering deterministic merge", type(exc).__name__)
        return None
    if not compile_accepts(compiled, notes):
        return None
    return compiled


def compile_groups(
    notes: StructuredNotes, context: LectureContext, *, budget: int
) -> list[StructuredNotes]:
    """Partition a booklet into contiguous whole-section groups that fit.

    A section is never split: it is the unit the acceptance gate and the merge
    both reason about. A section that cannot fit on its own is returned as a
    single-section group, which the caller keeps verbatim (with a warning)
    rather than dropping content it cannot re-send.
    """
    if budget <= 0:
        return []
    base = len(build_compile_document(context, StructuredNotes()))
    groups: list[StructuredNotes] = []
    current: list[NoteSection] = []
    current_chars = base
    for section in notes.sections:
        cost = len(json.dumps(section_payload(section), ensure_ascii=False))
        extra = cost + (2 if current else 0)  # ", " between array items
        if current and current_chars + extra > budget:
            groups.append(StructuredNotes(sections=tuple(current)))
            current = [section]
            current_chars = base + cost
            continue
        current.append(section)
        current_chars += extra
    if current:
        groups.append(StructuredNotes(sections=tuple(current)))
    return groups


def _gate_compiled(
    compiled: StructuredNotes | None,
    baseline_notes: StructuredNotes,
    baseline_report: NoteQualityReport | None,
    document: str,
    settings: Settings,
    *,
    mode: str,
) -> StructuredNotes | None:
    """The editorial answer, unless it trades completeness for fluency."""
    if compiled is None:
        return None
    if not settings.note_strict_grounding:
        return compiled
    compiled, compiled_report = gate_notes(compiled, document, strict=True, mode=mode)
    baseline = baseline_report
    if baseline is None:
        _, baseline = gate_notes(baseline_notes, document, strict=True, mode=mode)
    # An answer that invents a fact, or that covers less of the source than the
    # text it replaced, is rejected even if its shape looks better.
    if compiled_report.audit.fabricated:
        logger.warning("compiled notes rejected: they assert facts absent from the source")
        return None
    if compiled_report.audit.coverage + 0.02 < baseline.audit.coverage:
        logger.warning(
            "compiled notes rejected: coverage %.2f < %.2f",
            compiled_report.audit.coverage, baseline.audit.coverage,
        )
        return None
    if compiled_report.coverage.ratio + 0.05 < baseline.coverage.ratio:
        logger.warning(
            "compiled notes rejected: sentence coverage %.2f < %.2f",
            compiled_report.coverage.ratio, baseline.coverage.ratio,
        )
        return None
    return compiled


async def _compile_in_groups(
    merged: StructuredNotes,
    context: LectureContext,
    document: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str,
    presentation: bool,
    budget: int,
    semaphore: asyncio.Semaphore | None = None,
    source_locators: bool = False,
) -> StructuredNotes:
    """Level 1: compile whole-section groups concurrently; keep any group that fails."""
    groups = compile_groups(merged, context, budget=budget)
    logger.warning(
        "compile pass grouped: merged_chars=%d budget=%d groups=%d",
        len(json.dumps(compact_notes_payload(merged), ensure_ascii=False)),
        budget,
        len(groups),
    )
    sem = semaphore or asyncio.Semaphore(max(1, settings.note_concurrency))

    async def compile_group(group: StructuredNotes) -> StructuredNotes | None:
        async with sem:
            return await _compile_once(
                session, settings, context, group,
                mode=mode, presentation=presentation, budget=budget,
                source_locators=source_locators,
            )

    tasks = [asyncio.create_task(compile_group(group)) for group in groups]
    try:
        compiled_groups_list = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    sections: list[NoteSection] = []
    compiled_groups = 0
    for group, compiled in zip(groups, compiled_groups_list, strict=True):
        if compiled is None:
            sections.extend(group.sections)
            continue
        if settings.note_strict_grounding:
            _, group_report = gate_notes(compiled, document, strict=True, mode=mode)
            if group_report.audit.fabricated:
                logger.warning(
                    "compiled group rejected: it asserts facts absent from the source"
                )
                sections.extend(group.sections)
                continue
        sections.extend(compiled.sections)
        compiled_groups += 1
    if not compiled_groups:
        return merged
    assembled = replace(merged, sections=tuple(sections))
    if not assembled.has_content:
        return merged
    logger.info(
        "compile pass grouped: compiled=%d/%d sections=%d",
        compiled_groups, len(groups), len(assembled.sections),
    )
    return assembled


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

    One predicate for two callers that must never disagree: ``_compile_pass``
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
    """Whether the editorial compile will produce a different booklet."""
    return compile_skip_reason(notes, settings, mode=mode, presentation=presentation) is None


async def _compile_pass(
    merged: StructuredNotes,
    merged_report: NoteQualityReport | None,
    document: str,
    context: LectureContext,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str,
    presentation: bool,
    semaphore: asyncio.Semaphore | None = None,
    source_locators: bool = False,
) -> StructuredNotes:
    """The editorial pass: one request when it fits, whole-section groups when not.

    The size-driven skip used to be silent, which made a 3-hour lecture look
    exactly like an unedited merge. It is now logged, and the booklet is
    compiled in groups so the pass still runs; a booklet that fits one request
    keeps the original single-request behaviour and cost.
    """
    skip = compile_skip_reason(merged, settings, mode=mode, presentation=presentation)
    if skip == "no-budget":
        # A zero budget means the configured window cannot hold the merged
        # booklet plus the compile prompt. Skipping silently made a
        # misconfiguration look like a normal, unedited merge.
        logger.warning(
            "compile pass disabled: NOTE_CONTEXT_TOKENS leaves no room for the "
            "merged booklet (merged_chars=%d, NOTE_MAX_OUTPUT_TOKENS=%d)",
            _merged_chars(merged), settings.note_max_output_tokens,
        )
        return merged
    if skip is not None:
        return merged
    merged_chars = _merged_chars(merged)
    budget = effective_compile_max_chars(
        settings, mode=mode, presentation=presentation
    )
    if budget <= 0:
        # A zero budget means the configured window cannot hold the merged
        # booklet plus the compile prompt. Skipping silently made a
        # misconfiguration look like a normal, unedited merge.
        logger.warning(
            "compile pass disabled: NOTE_CONTEXT_TOKENS leaves no room for the "
            "merged booklet (merged_chars=%d, NOTE_MAX_OUTPUT_TOKENS=%d)",
            merged_chars, settings.note_max_output_tokens,
        )
        return merged
    if merged_chars <= budget:
        compiled = await _compile_once(
            session, settings, context, merged,
            mode=mode, presentation=presentation, budget=budget,
            source_locators=source_locators,
        )
        accepted = _gate_compiled(
            compiled, merged, merged_report, document, settings, mode=mode
        )
        return accepted if accepted is not None else merged

    assembled = await _compile_in_groups(
        merged, context, document, settings, session,
        mode=mode, presentation=presentation, budget=budget,
        semaphore=semaphore,
        source_locators=source_locators,
    )
    accepted = _gate_compiled(
        assembled if assembled is not merged else None,
        merged, merged_report, document, settings, mode=mode,
    )
    if accepted is None:
        return merged
    # Level 2 (the last one): a final merge of the compiled groups, only when
    # the compile shortened the booklet enough that it now fits one request.
    if MAX_COMPILE_LEVELS >= 2:
        assembled_chars = len(build_compile_document(context, accepted))
        if assembled_chars > budget:
            logger.info(
                "compile pass level 2 skipped: assembled_chars=%d budget=%d",
                assembled_chars, budget,
            )
            return accepted
        final = await _compile_once(
            session, settings, context, accepted,
            mode=mode, presentation=presentation, budget=budget,
            source_locators=source_locators,
        )
        merged_final = _gate_compiled(
            final, merged, merged_report, document, settings, mode=mode
        )
        if merged_final is not None:
            return merged_final
    return accepted


#: Called with the deterministic merge before the editorial compile runs, so
#: the caller can deliver it while the compile is still in flight.
MergedCallback = Callable[["StructuredNotes"], Awaitable[None]]


async def structure_document(
    document: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str = "full",
    presentation: bool = False,
    on_merged: MergedCallback | None = None,
    source_locators: bool = False,
) -> StructuredNotes:
    """Map-reduce a prepared LLM document into one structured booklet.

    * one chunk → one request;
    * several chunks → one request per chunk (concurrent, bounded) plus one
      editorial compile request;
    * the chunk budget comes from the model's context window
      (:func:`effective_chunk_chars`), not from a character count alone;
    * an oversized input is split at a safe boundary and retried (bounded), and
      an overflowing chunk fails over to the next provider instead;
    * a failed chunk fails the whole step (the caller delivers the raw
      transcript, which the user already holds);
    * a failed compile falls back to the deterministic merge;
    * ``on_merged`` receives that merge before the compile starts, which is what
      lets the caller deliver a booklet while the editorial pass still runs.
    """
    document = document.strip()
    if not document:
        raise StructuringError("متن ورودی خالی بود.")
    resolved_mode = resolve_note_mode(mode)
    # One static prompt for every source kind: the source kind and the source
    # locators are carried by the user message, never by this string. A prompt
    # that changed with the source kind could not be prefix-cached, and a job
    # that mixes audio and slides would pay for two different prefixes.
    base_system = notes_system_prompt()
    splitter = split_presentation_document if presentation else split_transcript
    chunk_chars = effective_chunk_chars(settings, system_prompt=base_system)
    chunks = splitter(document, chunk_chars)
    if not chunks:
        raise StructuringError("متن ورودی خالی بود.")
    semaphore = _note_semaphore(settings, len(chunks))

    async def finalize(result: StructuredNotes) -> StructuredNotes:
        """CR-4: optionally run the bounded coverage-gap fill pass."""
        if getattr(settings, "note_completeness_fill", False) and not settings.note_strict_grounding:
            return await fill_coverage_gaps(
                session, settings, document, result, semaphore=semaphore
            )
        return result

    # A presentation says so in the user message: its source is slides plus
    # narration, not a transcript. The rest of the template is shared.
    source_header = PRESENTATION_PROMPT.rstrip("\n") if presentation else ""

    def single_render(text: str) -> str:
        """The user message of a one-part request."""
        return build_chunk_message(text, index=1, total=1, header=source_header)

    async def finish_single(chars_used: int) -> StructuredNotes:
        """One chunk: a single note request, then the evidence gate.

        Shared by both sizing passes so the short-lecture path cannot drift
        between the first split and the re-split that reserves the orientation
        context.
        """
        logger.info(
            "note document sizing kind=%s chars=%d chunk_chars=%d chunks=1 context_digest=false",
            "presentation" if presentation else "transcript",
            len(document),
            chars_used,
        )
        single = await _structure_piece(
            session, settings,
            chunks[0],
            system_prompt=base_system, mode=resolved_mode, semaphore=semaphore,
            presentation=presentation, render=single_render,
        )
        if not settings.note_strict_grounding:
            return single
        return (
            await _audit_and_repair(
                single, document, settings, session,
                mode=resolved_mode, presentation=presentation, semaphore=semaphore,
                source_locators=source_locators,
            )
        )[0]

    if len(chunks) == 1:
        return await finalize(await finish_single(chunk_chars))

    # A chunked request also carries the topic digest and the neighbour windows.
    # Reserve their worst-case size when it fits; for a narrow model window,
    # drop this optional orientation rather than exceed the configured context.
    include_context = True
    try:
        contextual_chunk_chars = effective_chunk_chars(
            settings, system_prompt=base_system, extra_reserve_chars=CHUNK_CONTEXT_RESERVE_CHARS
        )
    except StructuringError:
        include_context = False
        contextual_chunk_chars = chunk_chars
    chunks = splitter(document, contextual_chunk_chars)
    if len(chunks) > MAX_NOTE_CHUNKS:
        raise StructuringError(
            f"این متن به {len(chunks)} بخش تقسیم می‌شود که از سقف {MAX_NOTE_CHUNKS} "
            "بخش بیشتر است؛ NOTE_CONTEXT_TOKENS یا NOTE_CHUNK_CHARS را افزایش دهید."
        )
    semaphore = _note_semaphore(settings, len(chunks))
    if len(chunks) == 1:
        return await finalize(await finish_single(contextual_chunk_chars))

    context = build_context(chunks) if include_context else LectureContext()
    logger.info(
        "note document sizing kind=%s chars=%d chunk_chars=%d chunks=%d context_digest=%s",
        "presentation" if presentation else "transcript",
        len(document),
        contextual_chunk_chars,
        len(chunks),
        include_context,
    )

    async def map_chunk(index: int, chunk: str) -> StructuredNotes:
        block = context_block_for(context, chunks, index) if include_context else ""
        topic = context.topic_for(index, len(chunks)) if include_context else ""
        # ``base_system`` is byte-identical for every chunk, pass and job, so
        # the provider can serve it from its prefix cache; only the user
        # message differs. See ``build_chunk_message``.
        def render(text: str) -> str:
            return build_chunk_message(
                text, index=index, total=len(chunks), topic=topic,
                context_block=block, header=source_header,
            )

        return await _structure_piece(
            session, settings, chunk,
            system_prompt=base_system, mode=resolved_mode, semaphore=semaphore,
            presentation=presentation, render=render,
        )

    # Cancel the siblings when one chunk fails: ``gather`` propagates the first
    # error but leaves the other provider requests running against a job that
    # has already been failed and released (audit F-17).
    tasks = [
        asyncio.create_task(map_chunk(index, chunk))
        for index, chunk in enumerate(chunks, start=1)
    ]
    try:
        chunk_notes = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    merged = merge_structured_notes(list(chunk_notes))
    merged_report: NoteQualityReport | None = None
    if settings.note_strict_grounding:
        merged, merged_report = await _audit_and_repair(
            merged, document, settings, session,
            mode=resolved_mode, presentation=presentation, semaphore=semaphore,
            source_locators=source_locators,
        )
    if on_merged is not None and compile_will_run(
        merged, settings, mode=resolved_mode, presentation=presentation
    ):
        # Deliberately after the repair: the provisional booklet is the same
        # audited content the compile starts from, never the raw merge. Skipped
        # when the compile will not run, because the final booklet would then be
        # this exact file.
        try:
            await on_merged(merged)
        except Exception:  # noqa: BLE001 - delivery is never a note failure
            logger.warning("progressive booklet delivery failed", exc_info=True)

    return await finalize(await _compile_pass(
        merged, merged_report, document, context, settings, session,
        mode=resolved_mode, presentation=presentation, semaphore=semaphore,
        source_locators=source_locators,
    ))


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


def _slide_blocks(text: str) -> tuple[dict[int, tuple[str, str]], list[int]]:
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
            title = prior_title or title
            body = "\n\n".join(part for part in (prior_body, body) if part)
        else:
            order.append(number)
        blocks[number] = (title, body)
    return blocks, order


def _slide_narration_blocks(text: str) -> tuple[dict[int, str], str, bool]:
    matches = sorted(
        [*(_SLIDE_NARRATION_RE.finditer(text)), *(_UNASSIGNED_NARRATION_RE.finditer(text))],
        key=lambda match: match.start(),
    )
    if not matches:
        return {}, "", False
    assigned: dict[int, str] = {}
    unassigned: list[str] = []
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
    slide_blocks, slide_order = _slide_blocks(outline)
    narration, unassigned, has_markers = _slide_narration_blocks(transcript)
    if has_markers:
        sections: list[str] = []
        if outline.strip() and not slide_blocks:
            sections.append("## متن اسلایدها\n\n" + outline.strip())
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


def split_presentation_document(text: str, max_chars: int) -> list[str]:
    """Pack complete slide blocks into chunks, splitting only an oversized slide.

    This preserves slide boundaries when practical, so the note model receives
    each slide's text beside its aligned narration instead of a generic text
    chunk that crosses slide attribution boundaries.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    text = text.strip()
    if not text:
        return []
    headings = list(_PRESENTATION_BLOCK_RE.finditer(text))
    if not headings:
        return split_transcript(text, max_chars)

    preamble = text[:headings[0].start()].strip()
    blocks = [
        text[match.start():(headings[index + 1].start() if index + 1 < len(headings) else len(text))].strip()
        for index, match in enumerate(headings)
    ]
    chunks: list[str] = []
    if preamble:
        if blocks and len(preamble) + 2 < max_chars:
            blocks[0] = f"{preamble}\n\n{blocks[0]}"
        else:
            chunks.extend(split_transcript(preamble, max_chars))
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
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
                    chunks.append(prefix + piece)
                else:
                    for continuation in split_transcript(piece, piece_budget):
                        chunks.append(prefix + continuation)
            else:
                chunks.append(piece)
    flush()
    return chunks


async def structure_transcript(
    text: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str = "full",
    on_merged: MergedCallback | None = None,
) -> StructuredNotes:
    """Transcript text (the cleaned LLM document) → structured notes."""
    return await structure_document(
        text, settings, session, mode=mode, presentation=False, on_merged=on_merged
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
    )
