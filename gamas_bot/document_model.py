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
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Iterable

from .doc_extract import DocSection, ExtractedDoc
from .notes import NoteSection, StructuredNotes
from .textnorm import normalize_display

if TYPE_CHECKING:  # pragma: no cover - typing only
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

    def render(self) -> str:
        """Heading + body, matching :meth:`doc_extract.DocSection.render`."""
        heading = self.heading.strip()
        body = self.text.strip()
        if heading and body and heading not in body:
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
        )


#: How an extraction source type maps to the kind of unit it produces.
_SOURCE_TYPE_UNIT_KIND = {
    "pdf": "page",
    "pptx": "slide",
    "docx": "section",
    "image": "page",
}


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

    @property
    def text(self) -> str:
        """The complete normalized source, units joined in order."""
        rendered = (unit.render() for unit in self.units)
        return "\n\n".join(text for text in rendered if text.strip())

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
        order, with its locator and heading intact. Extraction already cleaned
        the text; ``normalize_display`` is idempotent, so applying it here
        only guarantees the canonical normal form.
        """
        kind = _SOURCE_TYPE_UNIT_KIND.get(extracted.source_type, "section")
        units = tuple(
            SourceUnit(
                ordinal=index,
                unit_id=f"u{index + 1:04d}",
                text=normalize_display(section.text),
                locator=section.locator,
                heading=normalize_display(section.heading),
                heading_level=section.heading_level,
                kind=kind,
            )
            for index, section in enumerate(extracted.sections)
        )
        full_text = "\n\n".join(
            rendered for rendered in (unit.render() for unit in units) if rendered.strip()
        )
        return cls(
            units=units,
            title=title,
            language=language or detect_language(full_text),
            source_type=extracted.source_type or "text",
            page_count=int(extracted.page_count),
            document_id=document_id,
            metadata=tuple(metadata),
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
    def from_transcript(
        cls,
        transcript: "TranscriptDocument",
        *,
        title: str = "",
        document_id: str = "",
        provenance: str = "audio>transcribe",
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
        return cls.from_text(
            transcript.llm_document(),
            source_type="transcript",
            title=title,
            document_id=document_id,
            metadata=metadata,
            provenance=provenance,
        )

    def to_extracted(self) -> ExtractedDoc:
        """Back-compat adapter: the extraction shape existing callers consume."""
        return ExtractedDoc(
            self.source_type,
            self.page_count,
            tuple(
                DocSection(
                    unit.locator, unit.heading, unit.text, unit.heading_level
                )
                for unit in self.units
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "language": self.language,
            "source_type": self.source_type,
            "page_count": self.page_count,
            "provenance": self.provenance,
            "metadata": _pairs_to_dict(self.metadata),
            "units": [unit.to_payload() for unit in self.units],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)

    @classmethod
    def from_payload(cls, payload: object) -> "NormalizedDocument":
        if not isinstance(payload, dict):
            raise ValueError("NormalizedDocument payload must be a JSON object")
        units = tuple(
            unit
            for unit in (SourceUnit.from_payload(item) for item in payload.get("units") or [])
            if unit is not None
        )
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

    def to_payload(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "purpose": self.purpose,
            "unit_ids": list(self.unit_ids),
            "locators": list(self.locators),
            "concepts": list(self.concepts),
            "depth": self.depth,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "PlanTopic | None":
        if not isinstance(payload, dict):
            return None
        topic_id = str(payload.get("topic_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        if not topic_id or not title:
            return None
        depth = str(payload.get("depth") or DEFAULT_PLAN_DEPTH).strip().lower()
        if depth not in PLAN_DEPTHS:
            depth = DEFAULT_PLAN_DEPTH
        return cls(
            topic_id=topic_id,
            title=title,
            purpose=str(payload.get("purpose") or ""),
            unit_ids=tuple(str(item) for item in payload.get("unit_ids") or () if str(item)),
            locators=tuple(str(item) for item in payload.get("locators") or () if str(item)),
            concepts=tuple(str(item) for item in payload.get("concepts") or () if str(item)),
            depth=depth,
            depends_on=tuple(
                str(item) for item in payload.get("depends_on") or () if str(item)
            ),
        )


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

    def topic(self, topic_id: str) -> PlanTopic | None:
        for topic in self.topics:
            if topic.topic_id == topic_id:
                return topic
        return None

    def issues(self, document: NormalizedDocument | None = None) -> list[str]:
        """Integrity problems, reported (not raised) for the caller to handle."""
        problems: list[str] = []
        seen: set[str] = set()
        for topic in self.topics:
            if topic.topic_id in seen:
                problems.append(f"duplicate topic id: {topic.topic_id}")
            seen.add(topic.topic_id)
            if not topic.title.strip():
                problems.append(f"topic {topic.topic_id} has no title")
            for dependency in topic.depends_on:
                if dependency not in seen and self.topic(dependency) is None:
                    problems.append(
                        f"topic {topic.topic_id} depends on unknown topic {dependency}"
                    )
        if document is not None:
            known_units = {unit.unit_id for unit in document.units}
            for topic in self.topics:
                for unit_id in topic.unit_ids:
                    if unit_id not in known_units:
                        problems.append(
                            f"topic {topic.topic_id} covers unknown unit {unit_id}"
                        )
        return problems

    def to_payload(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "language": self.language,
            "topics": [topic.to_payload() for topic in self.topics],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)

    @classmethod
    def from_payload(cls, payload: object) -> "ContentPlan":
        if not isinstance(payload, dict):
            raise ValueError("ContentPlan payload must be a JSON object")
        topics = tuple(
            topic
            for topic in (PlanTopic.from_payload(item) for item in payload.get("topics") or [])
            if topic is not None
        )
        return cls(
            document_id=str(payload.get("document_id") or ""),
            language=str(payload.get("language") or LANGUAGE_UNDETERMINED),
            topics=topics,
        )

    @classmethod
    def from_json(cls, payload: str) -> "ContentPlan":
        return cls.from_payload(json.loads(payload))


def plan_from_document(
    document: NormalizedDocument, *, depth: str = DEFAULT_PLAN_DEPTH
) -> ContentPlan:
    """Deterministic structural default plan: one ordered topic per unit.

    This is **not** the global planner. It exists so the plan layer is real,
    testable and provenance-complete today: every source unit is covered by
    exactly one topic, topics follow source order, and each topic depends on
    its predecessor (a linear chain). A later planning stage replaces this
    with semantic grouping; the representation does not change.
    """
    if depth not in PLAN_DEPTHS:
        depth = DEFAULT_PLAN_DEPTH
    topics: list[PlanTopic] = []
    previous_id = ""
    for index, unit in enumerate(document.units):
        topic_id = f"t{index + 1:03d}"
        title = unit.heading or unit.locator or f"بخش {index + 1}"
        topics.append(
            PlanTopic(
                topic_id=topic_id,
                title=title,
                purpose="source coverage",
                unit_ids=(unit.unit_id,),
                locators=(unit.locator,) if unit.locator else (),
                depth=depth,
                depends_on=(previous_id,) if previous_id else (),
            )
        )
        previous_id = topic_id
    return ContentPlan(
        document_id=document.stable_id,
        language=document.language,
        topics=tuple(topics),
    )


# ===========================================================================
# Layer 3 — StructuredSection
# ===========================================================================


def _note_section_payload(section: NoteSection) -> dict[str, Any]:
    """Serialize one section through the established StructuredNotes contract.

    ``StructuredNotes.to_payload`` owns the section mapping; reusing it (via a
    single-section wrapper) means this module can never drift from the JSON
    shape the renderers and validators already trust.
    """
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
