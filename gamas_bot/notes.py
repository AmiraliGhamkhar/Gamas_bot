"""Note generation: the note model, the prompts, and a map-reduce pipeline.

This replaces v2's ``structuring`` + ``editorial`` + ``qa`` stack (≈6,100
lines) with one module and a fixed call budget:

* **one map call per transcript chunk** — run concurrently (bounded by
  ``NOTE_CONCURRENCY``), each with a deterministic position/context block so
  nothing is lost at chunk edges;
* **one optional reduce call** — the editorial compile that merges the chunk
  notes into a coherent booklet. If it fails, the deterministic merge is
  delivered instead;
* **no repair passes, no QA-driven re-asks, no truncation re-sends.** A bad
  answer degrades to the raw transcript — which the user already holds,
  because the transcript is delivered before note generation starts.

The note dataclasses, the Persian prompts and the conservative deterministic
merge are ported from v2 nearly verbatim: they encode the content-preservation
contract (numbers, units, English terms and examples are never dropped) that
the raw-text priority is built on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, replace

import aiohttp

from .config import NOTE_MODES, Settings
from .llm import LLMContextOverflowError, LLMError, complete_json
from .progress import to_persian_digits
from .textnorm import normalize_for_compare
from .transcript import split_transcript

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 20000
VALID_CALLOUT_KINDS = ("نکته", "هشدار", "یادآوری")

#: A merge below this size is not worth an editorial compile call.
COMPILE_MIN_NOTES_CHARS = 400
#: Hard bound on the compile request; a larger merged booklet is delivered as
#: the deterministic merge rather than re-sent as one oversized request.
COMPILE_MAX_CHARS = 48000
#: The compile result must keep at least this share of the merged sections.
COMPILE_MIN_SECTION_RATIO = 0.5
#: The neighbour window handed to a chunk as context only.
NEIGHBOUR_CONTEXT_CHARS = 260
#: Characters of each chunk opening used for the deterministic topic digest.
CONTEXT_HEAD_CHARS = 220
#: Hard bound on the deterministic digest.
CONTEXT_DIGEST_MAX_CHARS = 6000

#: Conservative characters-per-token ratio for a Persian/English lecture.
#: Persian tokenizes less efficiently than English on every mainstream
#: tokenizer, and an over-estimate produces a request the provider rejects, so
#: this is deliberately low (real Persian text is usually closer to 2.5–3).
CHARS_PER_TOKEN = 2.0
#: Prompt scaffolding that is neither the system prompt nor the transcript
#: (position markers, the JSON envelope, provider boilerplate).
PROMPT_RESERVE_TOKENS = 512
#: The smallest request still worth sending, matching the ``NOTE_CHUNK_CHARS``
#: lower bound in the configuration.
MIN_REQUEST_CHARS = 2000
MIN_REQUEST_TOKENS = 1000
#: How many times one oversized request may be halved before the step gives up
#: (1 → 2 → 4 requests, each cut at a paragraph/sentence boundary).
MAX_CONTEXT_SPLITS = 2


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
        return cls(headers, rows) if rows else None


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
            or self.table is not None
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
        section = cls(
            heading=heading,
            paragraphs=tuple(_string_list(payload.get("paragraphs"))),
            bullets=tuple(_string_list(payload.get("bullets"))),
            definitions=definitions,
            examples=tuple(_string_list(payload.get("examples"))),
            steps=tuple(_string_list(payload.get("steps"))),
            formulas=tuple(_string_list(payload.get("formulas"))),
            key_points=tuple(_string_list(payload.get("key_points"))),
            table=NoteTable.from_payload(payload.get("table")),
            callouts=callouts,
        )
        return section if section.has_content else None


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    term: str
    definition: str

    @classmethod
    def from_payload(cls, payload: object) -> "GlossaryEntry | None":
        if not isinstance(payload, dict):
            return None
        term = _bounded_text(payload.get("term"), MAX_TITLE_CHARS)
        definition = _bounded_text(payload.get("definition"))
        return cls(term, definition) if term and definition else None


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
        )

    def to_payload(self) -> dict:
        return {
            "title": self.title,
            "summary": self.summary,
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
                    "table": (
                        {"headers": section.table.headers, "rows": section.table.rows}
                        if section.table
                        else None
                    ),
                    "callouts": [
                        {"kind": callout.kind, "text": callout.text}
                        for callout in section.callouts
                    ],
                }
                for section in self.sections
            ],
            "key_points": list(self.key_points),
            "review_questions": list(self.review_questions),
            "glossary": [
                {"term": entry.term, "definition": entry.definition}
                for entry in self.glossary
            ],
        }

    def to_json(self) -> str:
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
            if section.table is not None:
                header = "| " + " | ".join(section.table.headers) + " |"
                separator = (
                    "|"
                    + "|".join(
                        "-" * max(len(header) + 2, 3) for header in section.table.headers
                    )
                    + "|"
                )
                rows = "\n".join(
                    "| " + " | ".join(row) + " |" for row in section.table.rows
                )
                parts.append(f"{header}\n{separator}\n{rows}")
        if self.key_points:
            parts.append("## نکته‌های کلیدی")
            parts.extend(f"- {point}" for point in self.key_points)
        if self.review_questions:
            parts.append("## پرسش‌های مرور")
            parts.extend(
                f"{index}. {question}"
                for index, question in enumerate(self.review_questions, start=1)
            )
        if self.glossary:
            parts.append("## واژه‌نامه")
            parts.extend(f"- **{entry.term}:** {entry.definition}" for entry in self.glossary)
        return "\n\n".join(parts)

    @classmethod
    def from_payload(cls, payload: object) -> "StructuredNotes":
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
        notes = cls(
            title=_bounded_text(payload.get("title"), MAX_TITLE_CHARS),
            summary=_bounded_text(payload.get("summary")),
            sections=sections,
            key_points=tuple(_string_list(payload.get("key_points"))),
            glossary=glossary,
            learning_objectives=tuple(_string_list(payload.get("learning_objectives"))),
            review_questions=tuple(_string_list(payload.get("review_questions"))),
        )
        if not notes.has_content:
            raise StructuringError("ساختار جزوهٔ دریافتی خالی بود.")
        return notes


# ===========================================================================
# Backend-artifact scrubbing (ported verbatim: the patterns are the contract)
# ===========================================================================

_BACKEND_NAMES = (
    "OpenAI", "GPT", "ChatGPT", "Gemini", "Google", "Anthropic", "Claude",
    "Groq", "Mistral", "Cohere", "Deepgram", "Speechmatics", "Whisper",
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
        "ها", "یک", "یا", "تا", "را", "هم", "می", "شود", "است", "the", "of",
        "and", "a", "an", "to", "in", "on", "for", "with", "part", "section",
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
    words_a = heading_topic_words(first)
    words_b = heading_topic_words(second)
    if not words_a or not words_b:
        return False
    shared = words_a & words_b
    if len(shared) < MIN_SHARED_TOPIC_WORDS:
        return False
    return len(shared) / len(words_a | words_b) >= threshold


# ===========================================================================
# Prompts (ported verbatim — the content-preservation contract)
# ===========================================================================

MODE_RULES = {
    "full": (
        "- حالت خروجی: FULL. شما «گردآورندهٔ جزوهٔ درس» هستید، نه خلاصه‌ساز. مطلبی را که گوینده برای یادگیری لازم می‌داند حذف نکنید.\n"
        "- هر تعریف، توضیح، دلیل، مکانیزم، مثال، روند گام‌به‌گام و مقایسه را کامل بیاورید؛ جزوه باید جایگزین قابل‌اتکای حضار در کلاس باشد.\n"
        "- تکرارهایی که برای تأکید یا روشن‌شدن موضوع به‌کار رفته‌اند را نگه دارید؛ فقط تکرارهای سرهم و عین‌هم را یک بار بنویسید.\n"
        "- توضیح‌های مفصل گوینده را در همان بخشِ موضوعی، چند پاراگراف کامل بنویسید؛ یک توضیح چندجمله‌ای را به یک خط خلاصه فشرده نکنید.\n"
    ),
    "standard": (
        "- حالت خروجی: STANDARD. میان حفظ کامل مطالب و روانی جزوه تعادل برقرار کنید؛ توضیح‌های اصلی و مثال‌های مهم را نگه دارید و فقط پرگویی‌های آشکار را حذف کنید.\n"
    ),
    "summary": (
        "- حالت خروجی: SUMMARY. جزوهٔ کوتاه و فشرده بنویسید؛ فقط موضوع‌های اصلی، تعریف‌های کلیدی و اعداد مهم را بیاورید. مثال‌های فرعی و توضیح‌های طولانی را مختصر کنید.\n"
    ),
}

_CONTENT_RULES = (
    "\nقواعد حفظ محتوا — مهم‌ترین بخش دستور است:\n"
    "- فقط بر پایهٔ متن داده‌شده بنویسید؛ اطلاعات، فرمول، تعریف یا نتیجهٔ تازه نسازید. اگر بخشی نامفهوم است، آن را حدس نزنید و همان‌قدر که فهمیده‌اید بنویسید.\n"
    "- هیچ عدد، واحد، درصد، دوز دارو، مقدار آزمایشگاهی یا علامت اختصاری را حذف یا تغییر ندهید؛ صورت دقیق آن‌ها را عیناً بیاورید.\n"
    "- اصطلاح‌های تخصصی و عبارت‌های انگلیسی (نام دارو، دستگاه، مفهوم علمی و مخفف‌ها) را به همان شکل انگلیسی و بدون ترجمهٔ اجباری داخل متن فارسی حفظ کنید؛ ترجمهٔ فارسی رایج را می‌توانید در پرانتز بیاورید.\n"
    "- تعریف‌ها را در آرایهٔ definitions بیاورید (term، term_en اختیاری، definition)؛ مثال‌ها را در examples؛ روند یا دستورالعمل گام‌به‌گام را در steps؛ فرمول‌ها و معادله‌ها را با صورت دقیق‌شان در formulas بنویسید.\n"
    "- توضیح‌های مهم گوینده را به‌جای یک خط کوتاه، پاراگراف کامل بنویسید؛ جزوه باید درس را بدون شنیدن صدا قابل فهم کند.\n"
    "- نکته‌های کلیدی، یادآوری‌ها و هشدارهای مهم را در callouts یا key_points جداگانه برجسته کنید.\n"
    "- مطالب را با عنوان‌های بخش کوتاه و گویا مرتب کنید و ترتیب منطقی متن اصلی را نگه دارید.\n"
    "- اگر چند مورد قابل مقایسه یا دسته‌بندی وجود دارد، از table استفاده کنید؛ جدول را بی‌دلیل به کار نبرید.\n"
    "- فقط حذف‌های مجاز: پرگویی بی‌محتوا، اصطلاح‌های گفتاری تصادفی، نویزِ پیاده‌سازی صدا و تکرار عین‌هم. هیچ توضیح آموزشی را به‌خاطر کوتاهی حذف نکنید.\n"
)

_SEMANTIC_RULES = (
    "\nقرارداد معنایی — چه چیزی باید در جزوه حاضر باشد:\n"
    "- شما ویرایشگر جزوه هستید، نه خلاصه‌ساز: هر واحد آموزشی که در متن منبع آمده باید در جزوه هم بیاید. "
    "واحد آموزشی یعنی: تعریف، توضیح، مکانیزم و چرایی، مثال، مثال نقض، روند گام‌به‌گام، مقایسه، "
    "هشدار، استثنا، فرمول، عدد و اندازه‌گیری، تاریخ، نام‌های خاص، اصطلاح انگلیسی، مخفف، و نتیجه‌ای "
    "که گوینده صریحاً بیان کرده است.\n"
    "- فقط این چهار چیز را می‌توانید حذف کنید: پرگویی و مکث، گفت‌وگوی حاشیه‌ای بی‌محتوا، نویزِ آشکار "
    "پیاده‌سازی صدا، و تکرار عیناً لفظ‌به‌لفظ.\n"
    "- این‌ها را هرگز حذف نکنید: توضیح آموزشی، مثال، زمینه‌ای که گفته برای فهم آن لازم است، جزئیات "
    "عددی، اصطلاح تخصصی؛ و هرگز یک توضیح چندجمله‌ای را فقط برای کوتاه‌شدن جزوه با یک جملهٔ کوتاه "
    "جایگزین نکنید.\n"
    "- اگر نکته‌ای را با کلمات خودتان می‌نویسید، باید دقیقاً همان معنا و همان روابط متن اصلی باشد؛ "
    "هیچ ادعا، آمار، مرجع یا توصیهٔ تازه‌ای اضافه نکنید.\n"
)

_STYLE_RULES = (
    "\nقواعد نگارش (لحن جزوهٔ یک استاد):\n"
    "- مفهوم را معرفی کنید، اگر گوینده دلیل یا ضرورتش را گفته همان را توضیح دهید، بعد مکانیزم و "
    "جزئیات را بیاورید، بعد پیوند آن را با مفهوم پیشین نشان دهید و در پایان — فقط اگر گوینده گفته — "
    "یک نتیجهٔ کوتاه بنویسید.\n"
    "- هر بخش باید در همان جملهٔ اول بگوید موضوعش چیست و چگونه به مطلب قبلی وصل می‌شود؛ شروع بخش‌ها "
    "را قالبی و یکسان تکرار نکنید.\n"
    "- توضیح‌ها را در پاراگراف کامل و روان بنویسید؛ هر جمله را به یک بولت تبدیل نکنید. "
    "بولت را فقط برای فهرست‌های واقعی (اقلام هم‌رده، مراحل، ویژگی‌ها) به کار ببرید.\n"
    "- عبارت‌های کلیشه‌ای مثل «نکتهٔ مهم»، «در ادامه»، «همان‌طور که گفته شد» را بی‌دلیل تکرار نکنید؛ "
    "فقط وقتی خواندن را روان‌تر می‌کنند به کار ببرید.\n"
    "- جزئیات را ناگهانی و بی‌مقدمه نیاورید؛ اگر گوینده اول دلیل یا زمینه را گفته، همان ترتیب را حفظ کنید.\n"
    "- ادعای علمی، آمار، مرجع، توصیهٔ درمانی یا مثال تازه از خود اضافه نکنید؛ هرچه در متن نیست، نوشته نمی‌شود.\n"
    "- اگر بخشی از متن مبهم یا ناقص است، همان ابهام را حفظ کنید؛ معنای احتمالی را حدس نزنید و آن را "
    "«بهتر» یا «منطقی‌تر» بازنویسی نکنید.\n"
    "- اصطلاح تخصصی و انگلیسی را داخل جملهٔ فارسی به همان صورت اصلی بنویسید.\n"
)

_TRANSITION_RULES = (
    "\nقواعد پیوند و انسجام:\n"
    "- روابط متن را با همان کلمات ربط منبع حفظ کنید: علت و معلول (چون، زیرا، بنابراین، در نتیجه)، "
    "تضاد (اما، در مقابل، برخلاف)، مثال (برای نمونه، برای مثال)، ترتیب زمانی (نخست، اول، سپس، "
    "در پایان).\n"
    "- اگر دو مفهوم پشت‌سرهم به یک موضوع واحد تعلق دارند، آن‌ها را در یک بخش با یک گذر طبیعی بنویسید؛ "
    "اگر گوینده به موضوع تازه‌ای رفته، بخش تازه بسازید و دو موضوع بی‌ربط را در یک بخش قاطی نکنید.\n"
    "- ترتیب موضوع‌ها را جابه‌جا نکنید و از خودتان گذار تازه نسازید.\n"
    "- اگر موضوعی ادامهٔ موضوع قبلی است، پیوند را با یک جملهٔ کوتاه نشان دهید؛ اگر موضوع تازه است، "
    "بخش تازه بسازید و دو موضوع را در یک بخش قاطی نکنید.\n"
)

_JSON_RULES = (
    "\nقواعد خروجی — مطلق‌اند و استثنا ندارند:\n"
    "۱) پاسخ فقط و فقط یک شیء JSON معتبر است. هیچ متن، عنوان، توضیح، علامت نقل‌قول بلوکی یا خنده‌کد (code fence) قبل یا بعد از آن ننویسید.\n"
    "۲) ساختار دقیق JSON این است:\n"
    "{\n"
    "  \"title\": \"عنوان کوتاه جزوه\",\n"
    "  \"summary\": \"خلاصهٔ چند جمله‌ایِ موضوع و هدف جزوه\",\n"
    "  \"learning_objectives\": [\"هدف یادگیری برگرفته از متن (اختیاری)\"],\n"
    "  \"sections\": [\n"
    "    {\n"
    "      \"heading\": \"عنوان بخش\",\n"
    "      \"paragraphs\": [\"پاراگراف توضیحی\"],\n"
    "      \"bullets\": [\"مورد فهرستی\"],\n"
    "      \"definitions\": [{\"term\": \"اصطلاح\", \"term_en\": \"English term\", \"definition\": \"تعریف کامل\"}],\n"
    "      \"examples\": [\"مثال کامل همراه با توضیح\"],\n"
    "      \"steps\": [\"گام ۱ …\", \"گام ۲ …\"],\n"
    "      \"formulas\": [\"صورت دقیق فرمول\"],\n"
    "      \"key_points\": [\"نکتهٔ کلیدی همین بخش\"],\n"
    "      \"table\": {\"headers\": [\"ستون ۱\", \"ستون ۲\"], \"rows\": [[\"مقدار\", \"مقدار\"]]},\n"
    "      \"callouts\": [{\"kind\": \"نکته\", \"text\": \"متن برجسته\"}]\n"
    "    }\n"
    "  ],\n"
    "  \"key_points\": [\"نکته‌های کلیدی کل جزوه\"],\n"
    "  \"review_questions\": [\"پرسش مرور برگرفته از متن (اختیاری)\"],\n"
    "  \"glossary\": [{\"term\": \"اصطلاح\", \"definition\": \"تعریف کوتاه\"}]\n"
    "}\n"
    "۳) هر کلید اختیاری است؛ اگر محتوایی برایش ندارید آن را حذف کنید یا آرایه/رشتهٔ خالی بدهید. کلید تازه‌ای از خودتان نسازید.\n"
    "۳-۱) «learning_objectives» و «review_questions» را فقط وقتی بنویسید که خودِ متن صریحاً پشتوانهٔ آن‌ها باشد؛ "
    "هدف یا پرسشی که در متن نیست، نسازید.\n"
    "۴) «sections» را خالی نگذارید؛ دست‌کم یک بخش با عنوان معنادار و محتوای واقعی بسازید.\n"
    "۵) در callouts مقدار kind فقط یکی از این سه باشد: «نکته»، «هشدار» یا «یادآوری».\n"
    "۶) JSON باید بدون خطا قابل خواندن باشد: از نقل‌قول دوگانه استفاده کنید، کامای اضافی نگذارید و خط جدید داخل رشته‌ها را با \\n بنویسید.\n"
)

_CHUNKED_CONTENT_RULES = (
    "\nقواعد نوشتن یک «بخش» از یک درس بلند:\n"
    "- شما فقط همین بخش را می‌نویسید؛ بخش‌های دیگر را ویرایشگران دیگر می‌نویسند. "
    "فهرست موضوع‌های بالا نقشهٔ راه است، نه متنی که باید بازنویسی شود.\n"
    "- خط «موضوع همین بخش» از همان فهرست برداشته شده و مسئولیت همین بخش است: آن موضوع را "
    "کامل پوشش دهید و موضوع‌های بخش‌های دیگر را در جزوهٔ خود تکرار نکنید.\n"
    "- مقدمه، معرفی و جمع‌بندی کل درس را دوباره ننویسید؛ مستقیم سر موضوع همین بخش بروید.\n"
    "- اگر این بخش وسط یک موضوعِ نیمه‌تمام شروع می‌شود (متن با ادامهٔ توضیح قبلی آغاز شده)، "
    "اولین بخش خود را با عیناً همان عنوان بخش پیشین بنویسید — نه عنوان تازه و نه عنوان با "
    "کلمهٔ «ادامه» — چون جزوهٔ نهایی این دو را یک بخش می‌داند.\n"
    "- کلیدهای «summary» و «key_points» و «glossary» سطح کل درس را فقط زمانی پر کنید که درس تک‌بخشی است؛ "
    "در درس چندبخشی، نکته‌های کلیدی را در «key_points» همان بخش بگذارید و بقیه را خالی بگذارید.\n"
    "- انتهای بخش را طوری بنویسید که به موضوع بعدی وصل شود (بدون افزودن ادعأ تازه).\n"
    "- اصطلاح‌های تخصصی و نام‌های انگلیسی را عیناً مثل بقیهٔ بخش‌ها بنویسید.\n"
)

_PRESENTATION_CONTENT_RULES = (
    "\nقواعد ویژهٔ ارائه:\n"
    "- ورودی شامل متن اسلایدها (با شمارهٔ اسلاید و یادداشت گوینده) و متن پیاده‌سازی‌شدهٔ صدای ارائه است.\n"
    "- ترتیب بخش‌ها را از ترتیب اسلایدها بگیرید و توضیح‌های صوتی هر اسلاید را زیر همان موضوع ادغام کنید.\n"
    "- اگر صدا مطلبی فراتر از متن اسلاید دارد، آن را به‌عنوان توضیح کامل‌کننده بیاورید؛ اسلاید و صدا هر دو را پوشش دهید، نه فقط یکی را.\n"
    "- یادداشت گویندهٔ هر اسلاید جزو محتوای آموزشی است؛ آن را حذف نکنید.\n"
    "- تعریف‌ها را در definitions، مثال‌ها را در examples، روند گام‌به‌گام را در steps و فرمول‌ها را در formulas هر بخش بیاورید.\n"
    "- نکته‌های کلیدی، یادآوری‌ها و هشدارهای مهم گوینده را در callouts یا key_points جداگانه برجسته کنید.\n"
    "- اگر چند مورد قابل مقایسه یا دسته‌بندی وجود دارد، از table استفاده کنید؛ جدول را بی‌دلیل به کار نبرید.\n"
    "- برای فرمول‌ها و اصطلاح‌های تخصصی، صورت اصلی را حفظ کنید و متن را به فارسی روان بنویسید.\n"
)


def build_system_prompt(mode: str = "full", *, context_block: str = "") -> str:
    """The Persian system prompt for one note mode (lecture-to-notes compiler)."""
    mode_rule = MODE_RULES.get(mode, MODE_RULES["full"])
    prompt = (
        "شما دستیار آموزشی فارسی «گاماس» هستید و نقش شما «گردآورندهٔ جزوهٔ درس» است، نه خلاصه‌ساز. "
        "ورودی شما متن پیاده‌سازی‌شدهٔ خام یک کلاس درسی است و خروجی شما باید یک جزوهٔ ساختارمند، کامل و "
        "روان فارسی باشد که دانشجو بتواند جای شنیدن صدا، آن را بخواند و درس را بفهمد.\n\n"
        + _JSON_RULES
        + "\n\nقواعد محتوا:\n"
        + mode_rule
        + _SEMANTIC_RULES
        + _CONTENT_RULES
        + _STYLE_RULES
        + _TRANSITION_RULES
    )
    if context_block:
        prompt += "\n\n" + _CHUNKED_CONTENT_RULES + "\n" + context_block
    return prompt


def build_presentation_system_prompt(mode: str = "full", *, context_block: str = "") -> str:
    """The Persian system prompt for presentation material in one note mode."""
    mode_rule = MODE_RULES.get(mode, MODE_RULES["full"])
    prompt = (
        "شما دستیار آموزشی فارسی «گاماس» هستید. ورودی شما محتوای یک فایل ارائهٔ درسی (PowerPoint) است — "
        "شامل متن اسلایدها، یادداشت‌های گوینده و متن پیاده‌سازی‌شدهٔ صدای ضبط‌شدهٔ همان ارائه — و خروجی شما "
        "یک جزوهٔ ساختارمند و کامل فارسی است؛ نقش شما «گردآورندهٔ جزوهٔ درس» است که محتوای ارائه را منظم و "
        "کامل نگه می‌دارد، نه خلاصه‌سازی که حذف می‌کند.\n"
        + _JSON_RULES
        + "\n\nقواعد محتوا:\n"
        + mode_rule
        + _SEMANTIC_RULES
        + _PRESENTATION_CONTENT_RULES
        + _STYLE_RULES
        + _TRANSITION_RULES
    )
    if context_block:
        prompt += "\n\n" + _CHUNKED_CONTENT_RULES + "\n" + context_block
    return prompt


TRANSCRIPT_PROMPT = "متن پیاده‌سازی‌شدهٔ خام:\n\n"

COMPILE_SYSTEM_PROMPT = (
    "شما ویراستار نهایی جزوهٔ فارسی «گاماس» هستید. ورودی شما «زمینهٔ کلی درس» و «جزوهٔ فعلی» "
    "به شکل JSON است؛ جزوهٔ فعلی از چند بخش جداگانه ساخته شده و ممکن است ناپیوسته یا تکراری باشد. "
    "خروجی شما یک جزوهٔ واحد و روان است، نه خلاصه‌ای از جزوه.\n\n"
    "کار شما:\n"
    "- بخش‌هایی که به یک موضوع واحد تعلق دارند را در یک بخش منسجم ادغام کنید؛ ترتیب منطقی متن را نگه دارید.\n"
    "- عنوان بخش‌ها را از «موضوع‌های درس» بردارید و برای هر موضوع، دقیقاً یک بخش بسازید.\n"
    "- تکرارهای عینی و تیترهای تکراری را یک‌بار بنویسید، ولی هیچ توضیح، تعریف، مثال، مرحله، مقایسه، هشدار، "
    "استثنا، فرمول، عدد، واحد، تاریخ یا اصطلاح تازه‌ای را به‌خاطر کوتاه‌شدن حذف نکنید. دو جمله که یک "
    "مفهوم را با کلمات متفاوت می‌گویند، تکرار عینی نیستند و هر دو می‌مانند؛ فقط تکرار لفظ‌به‌لفظ را یکی کنید.\n"
    "- گذارهای طبیعی را فقط بر پایهٔ روابطی که در متن هست بازسازی کنید. ادعای تازه نسازید.\n"
    "- به‌جای جمله‌های بریده‌بریده و فهرست‌های بی‌جا، پاراگراف‌های کامل و روان بنویسید.\n"
    "- یک صورت یکدست برای هر اصطلاح انتخاب کنید و در همه‌جا همان را بنویسید.\n"
    "- تعریف‌ها را در definitions، مثال‌ها را در examples، مراحل را در steps، فرمول‌ها را در formulas، "
    "جدول‌ها را در table و هشدارها را در callouts نگه دارید.\n"
    "- خلاصهٔ کل درس را در summary و مهم‌ترین نکته‌های کل درس را در key_points بنویسید.\n"
    "- فقط اگر همین متن مدرک روشنی برای «اهداف یادگیری» یا «پرسش‌های مرور» دارد، آن‌ها را کوتاه و مستقیماً "
    "برگرفته از متن اضافه کنید؛ در غیر این صورت این دو کلید را نیاورید.\n"
    "- اطلاعات تازه اضافه نکنید. اگر چیزی در ورودی نیست، حدس نزنید.\n\n"
    "ساختار خروجی — فقط یک شیء JSON معتبر، بدون هیچ متن اضافه، با همان ساختار ورودی؛ "
    "«sections» هرگز خالی نمی‌شود و در callouts مقدار kind فقط «نکته»، «هشدار» یا «یادآوری» است."
)


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


def _table_rows_as_bullets(table: NoteTable) -> tuple[str, ...]:
    bullets: list[str] = []
    for row in table.rows:
        pairs = [
            f"{header}: {value}"
            for header, value in zip(table.headers, row, strict=False)
            if value.strip()
        ]
        if pairs:
            bullets.append("؛ ".join(pairs))
    return tuple(bullets)


def _combine_sections(first: NoteSection, second: NoteSection) -> NoteSection:
    if first.table is None:
        table = second.table
        extra_bullets: tuple[str, ...] = ()
    elif second.table is None:
        table = first.table
        extra_bullets = ()
    elif [_compare_key(header) for header in first.table.headers] == [
        _compare_key(header) for header in second.table.headers
    ]:
        table = NoteTable(
            list(first.table.headers),
            _dedupe_table_rows(
                [list(row) for row in first.table.rows] + [list(row) for row in second.table.rows]
            ),
        )
        extra_bullets = ()
    else:
        table = first.table
        extra_bullets = _table_rows_as_bullets(second.table)
    return replace(
        first,
        paragraphs=_dedupe_by_key(first.paragraphs + second.paragraphs, _compare_key),
        bullets=_dedupe_by_key(first.bullets + second.bullets + extra_bullets, _compare_key),
        definitions=_dedupe_definitions(first.definitions + second.definitions),
        examples=_dedupe_by_key(first.examples + second.examples, _compare_key),
        steps=_dedupe_by_key(first.steps + second.steps, _compare_key),
        formulas=_dedupe_by_key(first.formulas + second.formulas, _compare_key),
        key_points=_dedupe_by_key(first.key_points + second.key_points, _compare_key),
        callouts=_dedupe_callouts(first.callouts + second.callouts),
        table=table,
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
    if section.table is not None:
        headers = "|".join(_compare_key(header) for header in section.table.headers)
        keys.extend(
            headers + "||" + "|".join(_compare_key(cell) for cell in row)
            for row in section.table.rows
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
    for position, section in enumerate(sections):
        paragraphs = _dedupe_by_key(section.paragraphs, _compare_key)
        bullets = _dedupe_by_key(section.bullets, _compare_key)
        paragraphs = tuple(
            value for value in paragraphs if _compare_key(value) not in seen_text
        )
        bullets = tuple(value for value in bullets if _compare_key(value) not in seen_text)
        seen_text.update(_compare_key(value) for value in paragraphs)
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
        if merged and _same_logical_section(
            merged[-1], cleaned, across_chunk=position in chunk_starts
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
        note_mode=next(
            (item.note_mode for item in notes if item.note_mode in NOTE_MODES), "full"
        ),
        learning_objectives=_dedupe_by_key(
            [item for note in notes for item in note.learning_objectives], _compare_key
        ),
        review_questions=_dedupe_by_key(
            [item for note in notes for item in note.review_questions], _compare_key
        ),
    )
    if not merged.has_content:
        raise StructuringError("ساختار جزوهٔ دریافتی خالی بود.")
    return merged


# ===========================================================================
# JSON parsing
# ===========================================================================


def _load_json_payload(payload: dict) -> StructuredNotes:
    try:
        return StructuredNotes.from_payload(payload)
    except (KeyError, TypeError) as exc:
        raise StructuringError("پاسخ سرویس تولید جزوه ساختار معتبری نداشت.") from exc


# ===========================================================================
# Deterministic lecture context (no LLM call: chunk openings)
# ===========================================================================


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


def build_context(chunks: list[str]) -> LectureContext:
    """Topics = the first sentence of each chunk, bounded.

    This is the v3 simplification of v2's orientation LLM call: the opening of
    a chunk states its topic, so the digest is deterministic, free and instant.
    """
    topics: list[str] = []
    budget = CONTEXT_DIGEST_MAX_CHARS
    for chunk in chunks:
        opening = chunk.strip()[:CONTEXT_HEAD_CHARS]
        first_sentence = opening
        match = _SENTENCE_SPAN.search(opening)
        if match and match.end() >= 10:
            first_sentence = opening[: match.end()]
        first_sentence = " ".join(first_sentence.split())
        if not first_sentence:
            continue
        if len(first_sentence) > budget:
            break
        topics.append(first_sentence)
        budget -= len(first_sentence) + 2
    return LectureContext(title="", topics=tuple(topics))


def _chunk_prefix(index: int, total: int) -> str:
    """Positional context prepended to every chunk sent to the model."""
    if total <= 1:
        return ""
    if index == 1:
        return (
            f"[بخش {to_persian_digits(index)} از {to_persian_digits(total)} این درس — "
            "ادامهٔ درس در بخش بعدی می‌آید]\n\n"
        )
    if index == total:
        return (
            f"[بخش {to_persian_digits(index)} از {to_persian_digits(total)} این درس — "
            "این آخرین بخش درس است]\n\n"
        )
    return (
        f"[بخش {to_persian_digits(index)} از {to_persian_digits(total)} این درس — "
        "این بخش ادامهٔ بخش قبل است و ادامهٔ آن در بخش بعد می‌آید]\n\n"
    )


def context_block_for(
    context: LectureContext, documents: list[str], index: int
) -> str:
    """The context block for part ``index``: position, topic, neighbours.

    The neighbour windows are short sentences so a boundary sentence can be
    continued rather than restarted; they are never long enough to be noted
    again.
    """
    total = len(documents)
    lines: list[str] = []
    if context.topics:
        listed = "، ".join(
            f"({to_persian_digits(position)}) {topic}"
            for position, topic in enumerate(context.topics, start=1)
        )
        lines.append("موضوع‌های درس به ترتیب: " + listed)
    topic = context.topic_for(index, total)
    if topic:
        lines.append(
            f"موضوع همین بخش: «{topic}» — فقط همین موضوع را بنویسید و عنوان بخش‌های "
            "خود را از این موضوع بردارید."
        )
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
# Compile (reduce) pass
# ===========================================================================


def compact_notes_payload(notes: StructuredNotes) -> dict:
    """The notes as a compact payload: empty fields are omitted."""
    payload: dict = {}
    if notes.title:
        payload["title"] = notes.title
    if notes.summary:
        payload["summary"] = notes.summary
    if notes.learning_objectives:
        payload["learning_objectives"] = list(notes.learning_objectives)
    sections: list[dict] = []
    for section in notes.sections:
        item: dict = {"heading": section.heading}
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
        if section.table is not None:
            item["table"] = {
                "headers": list(section.table.headers),
                "rows": [list(row) for row in section.table.rows],
            }
        if section.callouts:
            item["callouts"] = [{"kind": c.kind, "text": c.text} for c in section.callouts]
        sections.append(item)
    payload["sections"] = sections
    if notes.key_points:
        payload["key_points"] = list(notes.key_points)
    if notes.review_questions:
        payload["review_questions"] = list(notes.review_questions)
    if notes.glossary:
        payload["glossary"] = [
            {"term": e.term, "definition": e.definition} for e in notes.glossary
        ]
    return payload


def build_compile_document(context: LectureContext, notes: StructuredNotes) -> str:
    """The editorial compile input: topic digest + the merged notes."""
    parts: list[str] = []
    if context.topics:
        listed = "، ".join(
            f"({to_persian_digits(position)}) {topic}"
            for position, topic in enumerate(context.topics, start=1)
        )
        parts.append("### زمینهٔ کلی درس\nموضوع‌های درس به ترتیب: " + listed)
    parts.append(
        "### جزوهٔ فعلی (ساختهٔ بخش‌های جداگانه)\n"
        + json.dumps(compact_notes_payload(notes), ensure_ascii=False)
    )
    parts.append(
        "### دستور\n"
        "این بخش‌ها را به یک متن واحد و روان تبدیل کنید. هیچ مطلبی را حذف نکنید؛ "
        "فقط تکرارهای لفظ‌به‌لفظ را یک‌بار بنویسید، بخش‌های هم‌موضوع را ادغام کنید، گذارها را درست کنید و "
        "یکدستی اصطلاح‌ها را برقرار کنید.\n"
        "- ترتیب موضوع‌های درس را نگه دارید؛ هر موضوع یک بار و در جای خودش بیاید.\n"
        "- اگر جمله‌ای از بخش‌ها بریده یا ناقص است، آن را با همان اطلاعاتِ همین جزوه کامل و روان کنید؛ "
        "هیچ اطلاع تازه‌ای از خودتان اضافه نکنید.\n"
        "- خروجی فقط JSON با همان ساختار است و «sections» هرگز خالی نمی‌شود."
    )
    return "\n\n".join(parts)


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
    return True


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
    never grown.
    """
    reserve_characters = len(system_prompt) + max(0, extra_reserve_chars)
    reserve_tokens = int(reserve_characters / CHARS_PER_TOKEN) + PROMPT_RESERVE_TOKENS
    available_tokens = max(
        MIN_REQUEST_TOKENS,
        settings.note_context_tokens - settings.note_max_output_tokens - reserve_tokens,
    )
    context_budget = max(MIN_REQUEST_CHARS, int(available_tokens * CHARS_PER_TOKEN))
    # ``NOTE_CHUNK_CHARS`` may be smaller than the window allows; it is never grown.
    return max(1, min(settings.note_chunk_chars, context_budget))


async def _structure_piece(
    session: aiohttp.ClientSession,
    settings: Settings,
    document: str,
    *,
    system_prompt: str,
    mode: str,
    semaphore: asyncio.Semaphore,
    depth: int = 0,
) -> StructuredNotes:
    """One note request, with bounded recovery from an oversized input.

    A context overflow is the one provider error that more input can fix, so the
    input is cut at a paragraph/sentence boundary and the halves are requested
    instead. The depth is bounded and an input with no safe cut point is given
    up: an unchanged oversized request is never re-sent.
    """
    try:
        async with semaphore:
            payload = await complete_json(
                session,
                settings,
                system=system_prompt,
                user=TRANSCRIPT_PROMPT + document,
            )
        return _load_json_payload(payload)
    except LLMContextOverflowError as exc:
        if depth >= MAX_CONTEXT_SPLITS:
            raise
        pieces = split_transcript(document, max(1, len(document) // 2))
        if len(pieces) < 2:
            raise
        logger.warning(
            "note request exceeded the model context; splitting into %d part(s) depth=%d (%s)",
            len(pieces), depth + 1, exc,
        )
        parts = await asyncio.gather(
            *(
                _structure_piece(
                    session, settings, piece,
                    system_prompt=system_prompt, mode=mode,
                    semaphore=semaphore, depth=depth + 1,
                )
                for piece in pieces
            )
        )
        return merge_structured_notes(list(parts))


async def structure_document(
    document: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str = "full",
    presentation: bool = False,
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
    * a failed compile falls back to the deterministic merge.
    """
    document = document.strip()
    if not document:
        raise StructuringError("متن ورودی خالی بود.")
    base_system = (
        build_presentation_system_prompt(mode)
        if presentation
        else build_system_prompt(mode)
    )
    semaphore = asyncio.Semaphore(max(1, settings.note_concurrency))
    started = time.monotonic()
    chunks = split_transcript(document, effective_chunk_chars(settings, system_prompt=base_system))
    chunking_elapsed = time.monotonic() - started
    logger.info("note_chunking_complete chunks=%d chunk_chars=%d elapsed_ms=%.0f",
                len(chunks), effective_chunk_chars(settings, system_prompt=base_system),
                chunking_elapsed * 1000)
    if not chunks:
        raise StructuringError("متن ورودی خالی بود.")

    if len(chunks) == 1:
        return await _structure_piece(
            session, settings, chunks[0],
            system_prompt=base_system, mode=mode, semaphore=semaphore,
        )

    # A chunked request also carries the topic digest and the neighbour windows,
    # so the document is re-cut with that material reserved for the prompt.
    chunks = split_transcript(
        document,
        effective_chunk_chars(
            settings, system_prompt=base_system, extra_reserve_chars=CONTEXT_DIGEST_MAX_CHARS
        ),
    )
    if len(chunks) == 1:
        return await _structure_piece(
            session, settings, chunks[0],
            system_prompt=base_system, mode=mode, semaphore=semaphore,
        )

    context = build_context(chunks)

    async def map_chunk(index: int, chunk: str) -> StructuredNotes:
        block = context_block_for(context, chunks, index)
        system = (
            build_presentation_system_prompt(mode, context_block=block)
            if presentation
            else build_system_prompt(mode, context_block=block)
        )
        prefixed = _chunk_prefix(index, len(chunks)) + chunk
        return await _structure_piece(
            session, settings, prefixed,
            system_prompt=system, mode=mode, semaphore=semaphore,
        )

    chunk_notes = await asyncio.gather(
        *(map_chunk(index, chunk) for index, chunk in enumerate(chunks, start=1))
    )
    map_elapsed = time.monotonic() - started
    logger.info("note_map_complete chunks=%d elapsed_seconds=%.1f",
                len(chunks), map_elapsed)
    merged = merge_structured_notes(list(chunk_notes))
    merge_elapsed = time.monotonic() - started - map_elapsed
    logger.info("note_merge_complete sections=%d elapsed_seconds=%.1f",
                len(merged.sections), merge_elapsed)

    merged_chars = len(json.dumps(compact_notes_payload(merged), ensure_ascii=False))
    if (
        not settings.note_compile
        or merged_chars < COMPILE_MIN_NOTES_CHARS
        or merged_chars > COMPILE_MAX_CHARS
    ):
        return merged

    try:
        payload = await complete_json(
            session,
            settings,
            system=COMPILE_SYSTEM_PROMPT,
            user=build_compile_document(context, merged),
        )
        compiled = _load_json_payload(payload)
    except LLMError as exc:
        logger.warning("compile pass failed (%s); delivering deterministic merge", exc)
        return merged
    compile_elapsed = time.monotonic() - started - map_elapsed - merge_elapsed
    logger.info("note_compile_complete elapsed_seconds=%.1f sections=%d",
                compile_elapsed, len(compiled.sections))
    if compile_accepts(compiled, merged):
        return compiled
    return merged


def build_presentation_document(outline: str, transcript: str) -> str:
    """Combine slide text and narration transcript into one prompt payload."""
    sections: list[str] = []
    if outline.strip():
        sections.append("## متن اسلایدها\n\n" + outline.strip())
    if transcript.strip():
        sections.append("## متن پیاده‌سازی‌شدهٔ صدای ارائه\n\n" + transcript.strip())
    return "\n\n".join(sections)


async def structure_transcript(
    text: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str = "full",
) -> StructuredNotes:
    """Transcript text (the cleaned LLM document) → structured notes."""
    return await structure_document(text, settings, session, mode=mode, presentation=False)


async def structure_presentation(
    outline: str,
    transcript: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    mode: str = "full",
) -> StructuredNotes:
    """Slide outline + narration transcript → structured notes."""
    document = build_presentation_document(outline, transcript)
    return await structure_document(
        document, settings, session, mode=mode, presentation=True
    )
