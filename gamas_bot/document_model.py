"""Canonical intermediate representations for the educational pipeline.

The document pipeline conceptually flows through four stable shapes::

    RAW INPUT
        ↓
    NormalizedDocument   (this module: complete, ordered, addressable source)
        ↓
    ContentPlan          (this module: ordered topics + coverage, no prose)
        ↓
    StructuredSection    (this module: generated section + stable identity)
        ↓
    StructuredNotes      (existing ``gamas_bot.notes`` model, unchanged)

No DOCX, PDF, HTML or Markdown is ever the canonical representation; those are
inputs (extraction) or outputs (rendering) only.

This module is *representation plus small adapters*. It contains no LLM calls,
no rendering and no planner logic: ``plan_from_document`` is a deterministic
structural default that preserves order and complete source coverage until a
real planning stage replaces it, and ``notes_from_sections`` simply assembles
sections into the existing :class:`~gamas_bot.notes.StructuredNotes` model.

Design rules (mirroring the project's existing conventions):

* **Dataclasses, not a new framework.** ``to_payload``/``from_payload`` and
  stdlib-JSON ``to_json``/``from_json`` follow the exact pattern of
  ``doc_extract.chunks_to_json`` and ``StructuredNotes.to_payload``.
* **Never truncate.** No field here clips source text; the complete
  educational source survives every transformation. Model-sized *chunking*
  stays where it belongs (``doc_extract.chunk_sections`` /
  ``learning.chunk_text``), downstream of this representation.
* **Provenance is a chain of ids.** ``SourceUnit.unit_id`` →
  ``PlanTopic.unit_ids`` → ``StructuredSection.topic_id``/``source_refs`` →
  ``NoteSection.source_locators``. Each layer carries the previous layer's
  identifiers so the path source → topic → section → note stays traceable
  without inventing a citation engine the project does not have yet.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, replace
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Iterable

from .textnorm import normalize_display

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .doc_extract import ExtractedDoc
    from .notes import NoteSection, StructuredNotes
    from .transcript import TranscriptDocument

logger = logging.getLogger(__name__)

#: BCP-47-ish language tags this pipeline distinguishes. ``und`` (undetermined)
#: is the honest answer for symbol-only text; detection is a cheap script
#: heuristic, never a classifier with privacy or routing consequences.
LANGUAGE_PERSIAN = "fa"
LANGUAGE_ENGLISH = "en"
LANGUAGE_UNDETERMINED = "und"

#: Expected-depth values a plan topic may carry. Unknown values normalise to
#: ``standard`` (the project's fail-to-default convention, not a hard error).
PLAN_DEPTHS = ("overview", "standard", "detailed")
DEFAULT_PLAN_DEPTH = "standard"

#: Script detection samples a bounded prefix only; the *stored* text is never
#: truncated by this or any other function in the module.
_LANGUAGE_SAMPLE_CHARS = 4_000
_ARABIC_SCRIPT_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> str:
    """Dominant-script heuristic: ``fa`` / ``en`` / ``und``.

    Sampling a prefix keeps the check cheap on multi-megabyte sources; it only
    decides the label and never alters the stored content.
    """
    sample = str(text or "")[:_LANGUAGE_SAMPLE_CHARS]
    arabic = len(_ARABIC_SCRIPT_RE.findall(sample))
    latin = len(_LATIN_RE.findall(sample))
    if not arabic and not latin:
        return LANGUAGE_UNDETERMINED
    return LANGUAGE_PERSIAN if arabic >= latin else LANGUAGE_ENGLISH


def _pairs(payload: object) -> tuple[tuple[str, str], ...]:
    """Read a JSON object back into the immutable key/value tuple form."""
    if not isinstance(payload, dict):
        return ()
    return tuple((str(key), str(value)) for key, value in payload.items())


def _pairs_to_dict(pairs: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return dict(pairs)


# ===========================================================================
# Layer 1 — NormalizedDocument
# ===========================================================================


@dataclass(frozen=True, slots=True)
class SourceBlock:
    id: str
    kind: str
    text: str
    start_offset: int
    end_offset: int
    page_number: int | None = None
    slide_number: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SourceRange:
    start_offset: int
    end_offset: int
    block_ids: tuple[str, ...]


def normalize_source_text(text: str, *, initial: bool = True) -> str:
    """Technical normalization only: CRLF/CR and one leading encoding BOM."""
    return (text.removeprefix("\ufeff") if initial else text).replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True, slots=True)
class SourceUnit:
    """One ordered, addressable slice of a normalized document.

    A unit is the smallest piece provenance can point at: a page, a slide, a
    heading-delimited section or a plain-text block. ``unit_id`` is stable for
    the lifetime of the document (deterministic ordinal-based ids from the
    adapters), and ``text`` always holds the unit's *complete* content.
    """

    ordinal: int
    unit_id: str
    text: str
    #: Human-readable source label («صفحهٔ ۳», «اسلاید ۷»); may be empty.
    locator: str = ""
    heading: str = ""
    heading_level: int | None = None
    #: What one unit means in this document: page / slide / section / text.
    kind: str = "section"
    start_offset: int | None = None
    end_offset: int | None = None
    page_number: int | None = None
    slide_number: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None

    def render(self) -> str:
        """Heading + body, matching :meth:`doc_extract.DocSection.render`."""
        if self.start_offset is not None:
            return self.text  # exact unit: text owns its separators
        heading = self.heading.strip()
        body = self.text.strip()
        if heading and body:
            return f"{heading}\n{body}"
        return body or heading

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ordinal": self.ordinal,
            "unit_id": self.unit_id,
            "text": self.text,
            "locator": self.locator,
            "heading": self.heading,
            "kind": self.kind,
        }
        if self.heading_level is not None:
            payload["heading_level"] = self.heading_level
        for key in ("start_offset", "end_offset", "page_number", "slide_number", "start_ms", "end_ms"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> "SourceUnit | None":
        if not isinstance(payload, dict):
            return None
        unit_id = str(payload.get("unit_id") or "").strip()
        if not unit_id:
            return None
        # Empty-text units are still meaningful extraction boundaries (for
        # example, a heading-only section). Do not silently drop them when
        # restoring a NormalizedDocument.
        raw_text = payload.get("text", "")
        text = "" if raw_text is None else str(raw_text)
        try:
            ordinal = int(payload.get("ordinal", 0))
        except (TypeError, ValueError):
            ordinal = 0
        raw_level = payload.get("heading_level")
        try:
            heading_level = int(raw_level) if raw_level is not None else None
        except (TypeError, ValueError):
            heading_level = None
        return cls(
            ordinal=ordinal,
            unit_id=unit_id,
            text=text,
            locator=str(payload.get("locator") or ""),
            heading=str(payload.get("heading") or ""),
            heading_level=heading_level,
            kind=str(payload.get("kind") or "section"),
            start_offset=payload.get("start_offset"),
            end_offset=payload.get("end_offset"),
            page_number=payload.get("page_number"),
            slide_number=payload.get("slide_number"),
            start_ms=payload.get("start_ms"),
            end_ms=payload.get("end_ms"),
        )


#: How an extraction source type maps to the kind of unit it produces.
_SOURCE_TYPE_UNIT_KIND = {
    "pdf": "page",
    "pptx": "slide",
    "docx": "section",
    "image": "page",
}


def _locator_number(locator: str) -> int | None:
    label = locator.rpartition(" ")[2]
    return int(label) if label.isdecimal() else None


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    """The canonical, complete, ordered source every pipeline stage reads.

    ``units`` preserve source order and boundaries; ``text`` is the full
    normalized educational content; ``metadata`` (flat key/value pairs) and
    ``provenance`` (a short origin chain such as ``upload>extract:pdf``)
    describe where the document came from without embedding raw-input
    internals.
    """

    units: tuple[SourceUnit, ...] = ()
    title: str = ""
    language: str = LANGUAGE_UNDETERMINED
    source_type: str = "text"
    page_count: int = 0
    document_id: str = ""
    metadata: tuple[tuple[str, str], ...] = ()
    provenance: str = ""

    def __post_init__(self) -> None:
        if not any(unit.start_offset is not None or unit.end_offset is not None for unit in self.units):
            return  # historical units use implicit separators and offsets
        cursor = 0
        ids: set[str] = set()
        for unit in self.units:
            end = cursor + len(unit.text)
            if (not unit.unit_id or unit.unit_id in ids or end == cursor
                    or unit.start_offset != cursor or unit.end_offset != end):
                raise ValueError("invalid normalized source block partition")
            ids.add(unit.unit_id)
            cursor = end

    @property
    def text(self) -> str:
        """The complete normalized educational source."""
        if self.units and self.units[0].start_offset is not None:
            return "".join(unit.text for unit in self.units)
        rendered = (unit.render() for unit in self.units)
        return "\n\n".join(text for text in rendered if text.strip())

    @property
    def source_blocks(self) -> tuple[SourceBlock, ...]:
        """Project existing provenance units as an exact partition of `text`."""
        blocks: list[SourceBlock] = []
        cursor = 0
        for unit in self.units:
            body = unit.render()
            if unit.start_offset is None:
                if not body.strip():
                    continue
                body = ("\n\n" if blocks else "") + body
            end = cursor + len(body)
            blocks.append(SourceBlock(
                unit.unit_id, unit.kind, body, cursor, end, unit.page_number,
                unit.slide_number, unit.start_ms, unit.end_ms,
            ))
            cursor = end
        return tuple(blocks)

    @classmethod
    def from_blocks(
        cls, blocks: Iterable[SourceBlock], *, source_type: str, title: str = "",
        document_id: str | None = None, metadata: Iterable[tuple[str, str]] = (),
        language: str | None = None, provenance: str = "",
    ) -> NormalizedDocument:
        """Assign stable unit IDs and offsets to the canonical text stream."""
        parts = [(block, normalize_source_text(block.text, initial=index == 0))
                 for index, block in enumerate(blocks)]
        text = "".join(part for _, part in parts)
        digest = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()
        cursor = 0
        units: list[SourceUnit] = []
        for block, part in parts:
            if not part:
                continue
            end = cursor + len(part)
            units.append(SourceUnit(
                len(units), f"u{digest[:16]}:{cursor}-{end}", part, kind=block.kind,
                start_offset=cursor, end_offset=end, page_number=block.page_number,
                slide_number=block.slide_number, start_ms=block.start_ms, end_ms=block.end_ms,
            ))
            cursor = end
        return cls(
            units=tuple(units), title=title, source_type=source_type, language=language or detect_language(text),
            page_count=1 if units else 0, document_id=document_id or f"doc-{digest[:16]}",
            metadata=tuple(metadata), provenance=provenance or f"text>{source_type}",
        )

    @property
    def stable_id(self) -> str:
        """Explicit id, else a deterministic content-derived one."""
        if self.document_id:
            return self.document_id
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        return f"doc-{digest[:16]}"

    @property
    def metadata_map(self) -> dict[str, str]:
        return _pairs_to_dict(self.metadata)

    def unit(self, unit_id: str) -> SourceUnit | None:
        for unit in self.units:
            if unit.unit_id == unit_id:
                return unit
        return None

    def chunk_ranges(self, bodies: Sequence[str]) -> tuple[SourceRange, ...]:
        """Bind splitter bodies to ordered offsets, ignoring only reflowed whitespace.

        Synthetic headers must be removed by the splitter that added them.
        Reject an inconsistent split rather than submitting a partial document.
        Boundary whitespace belongs to one adjacent range; all ranges together
        reconstruct the canonical source exactly, even for mid-word splits.
        """
        cursor = 0
        block_cursor = 0
        ranges: list[SourceRange] = []
        source = self.text
        blocks = self.source_blocks
        size = len(source)
        for index, body in enumerate(bodies):
            start = cursor
            for char in body:
                if char.isspace():
                    continue
                while cursor < size and source[cursor].isspace():
                    cursor += 1
                if cursor == size or source[cursor] != char:
                    raise ValueError("source split changed or reordered content")
                cursor += 1
            if index == len(bodies) - 1:
                while cursor < size and source[cursor].isspace():
                    cursor += 1
            while (block_cursor < len(blocks)
                   and blocks[block_cursor].end_offset <= start):
                block_cursor += 1
            ids: list[str] = []
            position = block_cursor
            while position < len(blocks) and blocks[position].start_offset < cursor:
                ids.append(blocks[position].id)
                position += 1
            ranges.append(SourceRange(start, cursor, tuple(ids)))
        if cursor != size:
            raise ValueError("source split left an unassigned range")
        return tuple(ranges)


    @classmethod
    def from_extracted(
        cls,
        extracted: ExtractedDoc,
        *,
        title: str = "",
        document_id: str = "",
        metadata: Iterable[tuple[str, str]] = (),
        provenance: str = "",
        language: str | None = None,
    ) -> "NormalizedDocument":
        """Adapt an extraction result without losing a single section.

        Every :class:`~gamas_bot.doc_extract.DocSection` becomes one unit, in
        order, with its locator and heading intact. Extraction uses technical
        normalization only; no lesson text is rewritten here.
        """
        kind = _SOURCE_TYPE_UNIT_KIND.get(extracted.source_type, "section")
        units = tuple(
            SourceUnit(
                ordinal=index,
                unit_id=f"u{index + 1:04d}",
                text=normalize_source_text(section.text, initial=index == 0),
                locator=section.locator,
                heading=normalize_source_text(section.heading, initial=index == 0),
                heading_level=section.heading_level,
                kind=kind,
                page_number=_locator_number(section.locator) if kind == "page" else None,
                slide_number=_locator_number(section.locator) if kind == "slide" else None,
            )
            for index, section in enumerate(extracted.sections)
        )
        full_text = "\n\n".join(
            rendered for rendered in (unit.render() for unit in units) if rendered.strip()
        )
        source_metadata = tuple(metadata)
        if extracted.pages_without_text:
            source_metadata += (("pages_without_text", ",".join(map(str, extracted.pages_without_text))),)
        return cls(
            units=units,
            title=title,
            language=language or detect_language(full_text),
            source_type=extracted.source_type or "text",
            page_count=int(extracted.page_count),
            document_id=document_id,
            metadata=source_metadata,
            provenance=provenance or f"upload>extract:{extracted.source_type}",
        )

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        source_type: str = "text",
        title: str = "",
        locator: str = "",
        document_id: str = "",
        metadata: Iterable[tuple[str, str]] = (),
        provenance: str = "",
        language: str | None = None,
    ) -> "NormalizedDocument":
        """Adapt a raw text source (transcript body, pasted text, …) as-is.

        The text is normalized for display but never clipped: a source longer
        than any model budget stays whole here; chunking is a downstream
        concern of the generation layer.
        """
        normalized = normalize_display(str(text or ""))
        units: tuple[SourceUnit, ...] = ()
        if normalized.strip():
            units = (
                SourceUnit(
                    ordinal=0,
                    unit_id="u0001",
                    text=normalized,
                    locator=locator,
                    kind="text",
                ),
            )
        return cls(
            units=units,
            title=title,
            language=language or detect_language(normalized),
            source_type=source_type,
            page_count=1 if units else 0,
            document_id=document_id,
            metadata=tuple(metadata),
            provenance=provenance or f"text>{source_type}",
        )

    @classmethod
    def from_source_text(
        cls,
        text: str,
        *,
        source_type: str = "text",
        title: str = "",
        locator: str = "",
        document_id: str = "",
        metadata: Iterable[tuple[str, str]] = (),
        provenance: str = "",
        language: str | None = None,
        kind: str = "paragraph",
    ) -> NormalizedDocument:
        """Preserve paragraph boundaries and all non-technical source text."""
        source = str(text or "")
        # Partition before technical normalization (the pattern also recognizes CRLF).
        starts = sorted({
            0,
            *(m.end() for m in re.finditer(r"\r?\n[^\S\n]*\r?\n\s*", source) if m.end() < len(source)),
            *(m.end() for m in re.finditer(r"(?<=[\r\n]) {0,3}(?=#{1,6}[ \t]+\S)", source)),
        })
        normalized = cls.from_blocks(
            (SourceBlock("", kind, source[start:end], start, end)
             for start, end in zip(starts, [*starts[1:], len(source)], strict=True) if end > start),
            source_type=source_type, title=title, document_id=document_id, metadata=metadata,
            language=language, provenance=provenance,
        )
        if locator and normalized.units:
            normalized = replace(normalized, units=tuple(replace(unit, locator=locator) for unit in normalized.units))
        return normalized

    @classmethod
    def from_transcript(
        cls,
        transcript: "TranscriptDocument",
        *,
        title: str = "",
        document_id: str = "",
        provenance: str = "audio>transcribe",
        source_type: str = "transcript",
    ) -> "NormalizedDocument":
        """Adapt a cleaned transcript via its existing LLM-input contract.

        ``llm_document()`` is deliberately free of speaker/timing metadata so
        the booklet model cannot turn conversation mechanics into content;
        the speaker statistics survive here only as document-level metadata.
        """
        metadata: list[tuple[str, str]] = [("speakers", str(transcript.speakers))]
        if transcript.confidence is not None:
            metadata.append(("confidence", f"{transcript.confidence:.3f}"))
        if transcript.diarization_attempted:
            metadata.append(("diarization_attempted", "true"))
        if transcript.overlap_count:
            metadata.append(("overlap_count", str(transcript.overlap_count)))
        blocks: list[SourceBlock] = []
        for paragraph in transcript.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            start, end = paragraph.start, paragraph.end
            blocks.append(SourceBlock(
                "", "transcript", ("\n\n" if blocks else "") + text, 0, 0,
                start_ms=round(start * 1000) if start is not None and math.isfinite(start) else None,
                end_ms=round(end * 1000) if end is not None and math.isfinite(end) else None,
            ))
        return cls.from_blocks(
            blocks, source_type=source_type, title=title, document_id=document_id,
            metadata=metadata, provenance=provenance,
        )

    def to_extracted(self) -> ExtractedDoc:
        """Back-compat adapter: the extraction shape existing callers consume."""
        from .doc_extract import DocSection, ExtractedDoc

        return ExtractedDoc(
            self.source_type,
            self.page_count,
            tuple(
                DocSection(
                    unit.locator, unit.heading, unit.text, unit.heading_level
                )
                for unit in self.units
            ),
            tuple(int(page) for page in self.metadata_map.get("pages_without_text", "").split(",") if page.isdecimal()),
        )

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "document_id": self.document_id,
            "title": self.title,
            "language": self.language,
            "source_type": self.source_type,
            "page_count": self.page_count,
            "provenance": self.provenance,
            "metadata": _pairs_to_dict(self.metadata),
            "units": [unit.to_payload() for unit in self.units],
        }

        if self.units and self.units[0].start_offset is not None:
            payload["text_length"] = self.units[-1].end_offset
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)

    @classmethod
    def from_payload(cls, payload: object) -> "NormalizedDocument":
        if not isinstance(payload, dict):
            raise ValueError("NormalizedDocument payload must be a JSON object")
        raw_units = payload.get("units") or []
        units = tuple(unit for unit in map(SourceUnit.from_payload, raw_units) if unit is not None)
        if "text_length" in payload and (
            len(units) != len(raw_units) or not units or units[-1].end_offset is None
            or units[-1].end_offset != payload["text_length"]
        ):
            raise ValueError("invalid normalized source block partition")
        language = str(payload.get("language") or LANGUAGE_UNDETERMINED)
        return cls(
            units=units,
            title=str(payload.get("title") or ""),
            language=language,
            source_type=str(payload.get("source_type") or "text"),
            page_count=int(payload.get("page_count") or 0),
            document_id=str(payload.get("document_id") or ""),
            metadata=_pairs(payload.get("metadata")),
            provenance=str(payload.get("provenance") or ""),
        )

    @classmethod
    def from_json(cls, payload: str) -> "NormalizedDocument":
        return cls.from_payload(json.loads(payload))


# ===========================================================================
# Layer 2 — ContentPlan
# ===========================================================================


PLAN_SCHEMA_VERSION = 2
_PLAN_DIMENSIONS = {
    "numbers": "important_numbers", "units": "important_numbers", "dates": "important_numbers",
    "terminology": "critical_terms", "abbreviations": "critical_terms",
    "formulas": "important_facts", "definitions": "important_facts",
}


def _legacy_plan_strings(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(item) for item in payload.get(key) or () if str(item))


def _plan_strings(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    values = payload.get(key, [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{key} must be an array of strings")
    return tuple(values)


def _plan_ranges(payload: dict[str, Any]) -> tuple[SourceRange, ...]:
    ranges = payload.get("source_ranges", [])
    if not isinstance(ranges, list):
        raise ValueError("source_ranges must be an array")
    result = []
    for item in ranges:
        if (not isinstance(item, dict) or type(item.get("start_offset")) is not int
                or type(item.get("end_offset")) is not int):
            raise ValueError("invalid source range offsets")
        result.append(SourceRange(item["start_offset"], item["end_offset"], _plan_strings(item, "block_ids")))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PlanTopic:
    """One ordered topic the final educational document should cover.

    This is a planning artifact: it says *what* to cover and *where the
    coverage comes from*, never the prose itself. ``unit_ids`` reference
    :class:`SourceUnit` ids (source coverage); ``depends_on`` references
    other ``topic_id`` values (topic relationships).
    """

    topic_id: str
    title: str
    #: Why this topic belongs in the document (e.g. "source coverage").
    purpose: str = ""
    #: Source coverage: ids of the normalized-document units this topic covers.
    unit_ids: tuple[str, ...] = ()
    #: Human-readable source references (page/slide labels) for later prompts.
    locators: tuple[str, ...] = ()
    #: Important concepts the section must not drop.
    concepts: tuple[str, ...] = ()
    depth: str = DEFAULT_PLAN_DEPTH
    #: Relationships between topics: ids of topics that must come first.
    depends_on: tuple[str, ...] = ()

    description: str = ""
    parent_id: str | None = None
    source_ranges: tuple[SourceRange, ...] = ()
    important_facts: tuple[str, ...] = ()
    critical_terms: tuple[str, ...] = ()
    important_numbers: tuple[str, ...] = ()
    required_dimensions: tuple[str, ...] = ()
    duplicate_of: str | None = None
    repeated_title_of: str | None = None

    @property
    def id(self) -> str:
        return self.topic_id

    @property
    def source_block_ids(self) -> tuple[str, ...]:
        return self.unit_ids

    def to_payload(self, *, canonical: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "topic_id": self.topic_id,
            "title": self.title,
            "purpose": self.purpose,
            "unit_ids": list(self.unit_ids),
            "locators": list(self.locators),
            "concepts": list(self.concepts),
            "depth": self.depth,
            "depends_on": list(self.depends_on),
        }
        if not canonical:
            return result
        result["id"] = result.pop("topic_id")
        result["source_block_ids"] = result.pop("unit_ids")
        result.update({
            "description": self.description,
            "parent_id": self.parent_id,
            "source_ranges": [
                {"start_offset": item.start_offset, "end_offset": item.end_offset, "block_ids": list(item.block_ids)}
                for item in self.source_ranges
            ],
            "important_facts": list(self.important_facts),
            "critical_terms": list(self.critical_terms),
            "important_numbers": list(self.important_numbers),
            "required_dimensions": list(self.required_dimensions),
            "duplicate_of": self.duplicate_of,
            "repeated_title_of": self.repeated_title_of,
        })
        return result

    @classmethod
    def from_payload(cls, payload: object, *, strict: bool = False) -> "PlanTopic | None":
        if not isinstance(payload, dict):
            return None
        id_key = "id" if strict else "topic_id"
        topic_id = str(payload.get(id_key) or "").strip()
        title = str(payload.get("title") or "").strip()
        if not topic_id or not title:
            return None
        depth = str(payload.get("depth") or DEFAULT_PLAN_DEPTH).strip().lower()
        if depth not in PLAN_DEPTHS:
            depth = DEFAULT_PLAN_DEPTH
        if not strict:
            return cls(
                topic_id=topic_id, title=title, purpose=str(payload.get("purpose") or ""),
                unit_ids=_legacy_plan_strings(payload, "unit_ids"), locators=_legacy_plan_strings(payload, "locators"),
                concepts=_legacy_plan_strings(payload, "concepts"), depth=depth,
                depends_on=_legacy_plan_strings(payload, "depends_on"),
            )
        for key in ("id", "title", "description"):
            if not isinstance(payload.get(key), str):
                raise ValueError(f"{key} must be text")
        for key in ("parent_id", "duplicate_of", "repeated_title_of"):
            if payload.get(key) is not None and not isinstance(payload[key], str):
                raise ValueError(f"{key} must be a string or null")
        for key in ("parent_id", "source_block_ids", "source_ranges", "important_facts", "critical_terms",
                    "important_numbers", "required_dimensions"):
            if key not in payload:
                raise ValueError(f"missing canonical topic field: {key}")
        return cls(
            topic_id=topic_id,
            title=title,
            purpose=str(payload.get("purpose") or ""),
            unit_ids=_plan_strings(payload, "source_block_ids"),
            locators=_plan_strings(payload, "locators"),
            concepts=_plan_strings(payload, "concepts"),
            depth=depth,
            depends_on=_plan_strings(payload, "depends_on"),
            description=payload["description"],
            parent_id=payload["parent_id"],
            source_ranges=_plan_ranges(payload),
            important_facts=_plan_strings(payload, "important_facts"),
            critical_terms=_plan_strings(payload, "critical_terms"),
            important_numbers=_plan_strings(payload, "important_numbers"),
            required_dimensions=_plan_strings(payload, "required_dimensions"),
            duplicate_of=payload.get("duplicate_of"),
            repeated_title_of=payload.get("repeated_title_of"),
        )


PlannedTopic = PlanTopic  # Same type; keep the existing public name.


@dataclass(frozen=True, slots=True)
class ContentPlan:
    """The ordered topics a final document should cover, with provenance.

    ``document_id`` chains the plan back to its :class:`NormalizedDocument`;
    topic order *is* the intended document order (the tuple position), so no
    separate rank field is needed.
    """

    document_id: str = ""
    language: str = LANGUAGE_UNDETERMINED
    topics: tuple[PlanTopic, ...] = ()

    title: str = ""
    document_summary: str = ""
    global_terminology: tuple[str, ...] = ()
    coverage_requirements: tuple[str, ...] = ()
    planner: str = "deterministic"
    metadata: tuple[tuple[str, str], ...] = ()
    schema_version: int = 1  # Archived minimal plans remain readable.

    def topic(self, topic_id: str) -> PlanTopic | None:
        for topic in self.topics:
            if topic.topic_id == topic_id:
                return topic
        return None

    def issues(self, document: NormalizedDocument | None = None) -> list[str]:
        """Check references, hierarchy and exact ordered source coverage."""
        problems: list[str] = []
        canonical = self.schema_version == PLAN_SCHEMA_VERSION
        if self.schema_version not in {1, PLAN_SCHEMA_VERSION}:
            problems.append("unsupported ContentPlan version")
        known_topics = {topic.topic_id for topic in self.topics}
        blocks = {block.id: block for block in document.source_blocks} if document is not None else {}
        known_units = {unit.unit_id for unit in document.units} if document is not None else set()
        if self.planner not in {"deterministic", "llm"}:
            problems.append("invalid planner")
        if canonical and document is not None and self.document_id != document.stable_id:
            problems.append("plan references a different document")
        if canonical and (self.topics or self.coverage_requirements):
            if not all(value.strip() for value in (self.title, self.document_id, self.document_summary)):
                problems.append("missing plan title, identity or summary")
        seen: set[str] = set()
        covered: list[str] = []
        cursor = 0
        for topic in self.topics:
            topic_id = topic.topic_id
            if not topic_id.strip() or topic_id in seen:
                problems.append(f"duplicate topic id: {topic_id}" if topic_id in seen else "empty topic id")
            if not topic.title.strip():
                problems.append(f"topic {topic_id} has no title")
            for dependency in topic.depends_on:
                if dependency not in known_topics:
                    problems.append(f"topic {topic_id} depends on unknown topic {dependency}")
                elif dependency not in seen and canonical:
                    problems.append(f"topic {topic_id} has a forward or cyclic dependency")
            for reference in (topic.parent_id, topic.duplicate_of, topic.repeated_title_of):
                if reference is not None and reference not in seen:
                    problems.append(f"topic {topic_id} has an invalid parent or duplicate reference")
            seen.add(topic_id)
            if document is not None:
                for unit_id in topic.unit_ids:
                    if unit_id not in known_units:
                        problems.append(f"topic {topic_id} covers unknown unit {unit_id}")
            if not canonical:
                continue
            if not topic.description.strip() or not topic.unit_ids:
                problems.append(f"topic {topic_id} has no description or source references")
            for dimension in topic.required_dimensions:
                field = _PLAN_DIMENSIONS.get(dimension)
                if not field or not getattr(topic, field):
                    problems.append(f"topic {topic_id} has an unsupported required dimension: {dimension}")
            if tuple(block_id for item in topic.source_ranges for block_id in item.block_ids) != topic.unit_ids:
                problems.append(f"topic {topic_id} range references differ from block ids")
            for item in topic.source_ranges:
                block = blocks.get(item.block_ids[0]) if len(item.block_ids) == 1 else None
                if (len(item.block_ids) != 1 or type(item.start_offset) is not int or type(item.end_offset) is not int
                        or item.start_offset != cursor or item.end_offset <= cursor
                        or (document is not None and (block is None or
                            (block.start_offset, block.end_offset) != (item.start_offset, item.end_offset)))):
                    problems.append(f"topic {topic_id} has an invalid source range")
                cursor = item.end_offset
            covered.extend(topic.unit_ids)
        if canonical:
            if len(set(covered)) != len(covered) or tuple(covered) != self.coverage_requirements:
                problems.append("topic coverage does not match the ordered requirements exactly once")
            if document is not None and (tuple(covered) != tuple(blocks) or cursor != len(document.text)):
                problems.append("plan does not cover the complete source")
        return problems

    def validate(self, document: NormalizedDocument | None = None) -> ContentPlan:
        problems = self.issues(document)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def to_payload(self) -> dict[str, Any]:
        canonical = self.schema_version == PLAN_SCHEMA_VERSION
        result: dict[str, Any] = {
            "document_id": self.document_id,
            "language": self.language,
            "topics": [topic.to_payload(canonical=canonical) for topic in self.topics],
        }
        if not canonical:
            return result
        result.update({
            "title": self.title,
            "document_summary": self.document_summary,
            "global_terminology": list(self.global_terminology),
            "coverage_requirements": list(self.coverage_requirements),
            "planner": self.planner,
            "metadata": _pairs_to_dict(self.metadata),
            "schema_version": self.schema_version,
        })
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)

    @classmethod
    def from_payload(cls, payload: object) -> "ContentPlan":
        if not isinstance(payload, dict):
            raise ValueError("ContentPlan payload must be a JSON object")
        version = payload.get("schema_version", 1)
        if type(version) is not int or version not in {1, PLAN_SCHEMA_VERSION}:
            raise ValueError("unsupported ContentPlan version")
        canonical = version == PLAN_SCHEMA_VERSION
        raw_topics = payload.get("topics")
        if not canonical and raw_topics is None:
            raw_topics = []
        if not isinstance(raw_topics, list):
            raise ValueError("topics must be an array")
        topics = tuple(
            topic for item in raw_topics if (topic := PlanTopic.from_payload(item, strict=canonical)) is not None
        )
        if not canonical:
            return cls(
                document_id=str(payload.get("document_id") or ""),
                language=str(payload.get("language") or LANGUAGE_UNDETERMINED), topics=topics,
            )
        metadata = payload.get("metadata")
        if (len(topics) != len(raw_topics) or any(
            not isinstance(payload.get(key), str) for key in ("document_id", "title", "document_summary", "planner")
        ) or any(key not in payload for key in ("global_terminology", "coverage_requirements"))
                or not isinstance(metadata, dict) or any(not isinstance(value, str) for value in metadata.values())):
            raise ValueError("invalid canonical ContentPlan fields")
        plan = cls(
            document_id=str(payload.get("document_id") or ""),
            language=str(payload.get("language") or LANGUAGE_UNDETERMINED),
            topics=topics,
            title=str(payload.get("title") or ""),
            document_summary=str(payload.get("document_summary") or ""),
            global_terminology=_plan_strings(payload, "global_terminology"),
            coverage_requirements=_plan_strings(payload, "coverage_requirements"),
            planner=str(payload.get("planner", "deterministic")),
            metadata=_pairs(metadata),
            schema_version=version,
        )
        return plan.validate()

    @classmethod
    def from_json(cls, payload: str) -> "ContentPlan":
        return cls.from_payload(json.loads(payload))


def plan_from_document(
    document: NormalizedDocument, *, depth: str = DEFAULT_PLAN_DEPTH
) -> ContentPlan:
    """Build and validate the canonical plan without a model request."""
    from .note_planning import build_deterministic_plan

    return build_deterministic_plan(document, depth=depth)


# ===========================================================================
# Layer 3 — StructuredSection
# ===========================================================================


def _note_section_payload(section: NoteSection) -> dict[str, Any]:
    """Serialize one section through the established StructuredNotes contract.

    ``StructuredNotes.to_payload`` owns the section mapping; reusing it (via a
    single-section wrapper) means this module can never drift from the JSON
    shape the renderers and validators already trust.
    """
    from .notes import StructuredNotes

    payload = StructuredNotes(sections=(section,)).to_payload()
    sections = payload.get("sections")
    return sections[0] if isinstance(sections, list) and sections else {}


@dataclass(frozen=True, slots=True)
class StructuredSection:
    """A generated section with stable identity and provenance references.

    ``body`` *is* the existing :class:`~gamas_bot.notes.NoteSection`, so every
    structured educational element (paragraphs, bullets, definitions, tables,
    callouts, source locators, …) works unchanged. The wrapper adds only what
    the pipeline needs beyond the note model: a stable ``section_id``, an
    explicit ``order``, the ``topic_id`` this section realizes, and
    ``source_refs`` back into the normalized document.
    """

    section_id: str
    order: int
    body: NoteSection
    topic_id: str = ""
    source_refs: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return self.body.heading

    @property
    def locators(self) -> tuple[str, ...]:
        """Human-readable source references, mirroring the note contract."""
        return self.body.source_locators

    @classmethod
    def from_note_section(
        cls,
        section: NoteSection,
        *,
        section_id: str,
        order: int,
        topic_id: str = "",
        source_refs: Iterable[str] = (),
    ) -> "StructuredSection":
        return cls(
            section_id=section_id,
            order=int(order),
            body=section,
            topic_id=topic_id,
            source_refs=tuple(source_refs),
        )

    def to_note_section(self) -> NoteSection:
        """Back-compat adapter: the shape every existing renderer consumes."""
        return self.body

    def to_payload(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "order": self.order,
            "topic_id": self.topic_id,
            "source_refs": list(self.source_refs),
            "body": _note_section_payload(self.body),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)

    @classmethod
    def from_payload(cls, payload: object) -> "StructuredSection | None":
        if not isinstance(payload, dict):
            return None
        section_id = str(payload.get("section_id") or "").strip()
        from .notes import NoteSection

        body = NoteSection.from_payload(payload.get("body"), 0)
        if not section_id or body is None:
            return None
        try:
            order = int(payload.get("order", 0))
        except (TypeError, ValueError):
            order = 0
        return cls(
            section_id=section_id,
            order=order,
            body=body,
            topic_id=str(payload.get("topic_id") or ""),
            source_refs=tuple(
                str(item) for item in payload.get("source_refs") or () if str(item)
            ),
        )

    @classmethod
    def from_json(cls, payload: str) -> "StructuredSection | None":
        try:
            raw = json.loads(payload)
        except (TypeError, ValueError):
            return None
        return cls.from_payload(raw)


def notes_from_sections(
    sections: Iterable[StructuredSection],
    *,
    base: StructuredNotes | None = None,
) -> StructuredNotes:
    """Assemble sections into the existing note model, ordered by ``order``.

    With ``base`` the assembly replaces only ``sections`` (metadata such as
    title, glossary and review lists survive); without it a minimal
    ``StructuredNotes`` is produced. Empty-content sections are dropped,
    matching the note model's own contract.
    """
    from .notes import StructuredNotes

    ordered = sorted(sections, key=lambda section: section.order)
    bodies = tuple(section.body for section in ordered if section.body.has_content)
    if base is None:
        return StructuredNotes(sections=bodies)
    return replace(base, sections=bodies)


# ===========================================================================
# Provenance
# ===========================================================================


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    """One source → topic → section link in the trace."""

    unit_id: str
    locator: str
    topic_id: str
    topic_title: str
    section_id: str
    section_title: str
    order: int


def build_provenance_trace(
    document: NormalizedDocument,
    plan: ContentPlan,
    sections: Iterable[StructuredSection],
) -> tuple[ProvenanceRecord, ...]:
    """Flatten the id chain into one record per (section, source unit) pair.

    A section without explicit ``source_refs`` falls back to its topic's
    coverage, so provenance survives even when a generator only answers the
    plan. Units the trace cannot resolve in the document are skipped (the
    plan's ``issues()`` reports them separately).
    """
    records: list[ProvenanceRecord] = []
    for section in sorted(sections, key=lambda item: item.order):
        topic = plan.topic(section.topic_id)
        unit_ids = section.source_refs or (topic.unit_ids if topic else ())
        for unit_id in unit_ids:
            unit = document.unit(unit_id)
            if unit is None:
                continue
            records.append(
                ProvenanceRecord(
                    unit_id=unit_id,
                    locator=unit.locator,
                    topic_id=section.topic_id,
                    topic_title=topic.title if topic else "",
                    section_id=section.section_id,
                    section_title=section.title,
                    order=section.order,
                )
            )
    return tuple(records)
