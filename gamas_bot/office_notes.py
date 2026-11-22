"""Document-source booklets and genuinely compact revision notes.

This adapter reuses the extracted-document chunks, the established full-note
map/reduce pipeline, the shared evidence audit, and the existing Word/PDF
renderers. Compact notes deliberately have their own prompt, response schema,
validator, and renderer; they are not the full booklet with a different label.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Iterable

from .config import Settings
from .doc_extract import DocChunk, DocSection, chunk_sections
from .learning import MAX_MODEL_INPUT_CHARS
from .llm import LLMError, complete_json
from .document_model import NormalizedDocument, SourceBlock
from .notes import (
    MAX_SOURCE_LOCATORS_PER_SECTION,
    QUALITY_RECOVERED,
    NoteQualityError,
    NoteSection,
    StructuredNotes,
    StructuringError,
    compile_will_run,
    enforce_final_acceptance,
    ensure_structured_booklet,
    gate_notes,
    structurally_valid_booklet,
    structure_document,
)
from .notes_evidence import SignalAudit, audit_notes, content_words, extract_signals, split_sentences

logger = logging.getLogger(__name__)

#: Pipeline-stage reporter, mirroring :mod:`gamas_bot.notes`'s contract: called
#: with ``"validating"``/``"compiling"`` so the caller can show the evidence
#: gate and the final editorial pass on its progress bar.
StageCallback = Callable[[str], Awaitable[None]]
#: Called when an accepted booklet still omits source facts (the reason).
IncompleteCallback = Callable[[str], Awaitable[None]]
#: Per-chunk reporter for the compact pipeline: ``(done, total)``.
ChunkCallback = Callable[[int, int], Awaitable[None]]


async def _notify(callback: Any, *args: Any) -> None:
    """Invoke a progress/stage callback without ever failing the note build."""
    if callback is None:
        return
    try:
        await callback(*args)
    except Exception:  # noqa: BLE001 - reporting is never a note failure
        logger.warning("office note progress callback failed", exc_info=True)

#: Source material sent to the compact-only prompt in one request. The bound
#: keeps the prompt small even when an extracted file contains many pages.
# Keep compact-note requests large enough to avoid many tiny calls while staying
# well inside the configured context budget and cPanel memory ceiling.
MAX_COMPACT_INPUT_CHARS = 20_000
MAX_COMPACT_POINTS_PER_CHUNK = 32
MAX_COMPACT_POINT_CHARS = 6_000
#: Non-note Office exports still use a separate total source safety limit.
MAX_OFFICE_NOTE_SOURCE_CHARS = 250_000
#: A compact result that retains more than this share of source characters is
#: not a useful revision sheet; it is rejected rather than mislabelled.
MAX_COMPACT_SOURCE_RATIO = 0.75
MIN_SOURCE_CHARS_FOR_RATIO = 500

_LOCATOR_LABEL_PATTERN = (
    r"(?:صفحهٔ\s*[0-9۰-۹٠-٩]+|اسلاید\s*[0-9۰-۹٠-٩]+|"
    r"بخش\s+(?:[0-9۰-۹٠-٩]+|«[^\]\n]{1,60}»))"
    r"(?:\s+\([0-9۰-۹٠-٩]+/[0-9۰-۹٠-٩]+\))*"
)
_LOCATOR_LINE = re.compile(rf"(?m)^\[({_LOCATOR_LABEL_PATTERN})\]\s*(?:\n|$)")
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")

COMPACT_NOTE_SYSTEM_PROMPT = (
    "تو دستیار آموزشی فارسی گاماس هستی و فقط «یادداشت مرور سریع» می‌سازی، نه جزوهٔ کامل.\n"
    "از متن داخل <source> فقط نکته‌های کوتاه و مستقیمِ قابل مرور استخراج کن.\n"
    "قواعد سخت: هیچ واقعیت، عدد، واحد، فرمول، اصطلاح تخصصی، شرط، استثنا، هشدار یا رابطه‌ای نساز، "
    "تبدیل، گرد یا حذف نکن. نکته‌ها باید به متن منبع متکی باشند؛ توضیح آموزشی یا مثال تازه ننویس.\n"
    "این خروجی عمداً فشرده و فقط شامل یک عنوان و حداکثر ۳۲ نکتهٔ بولت‌دار است؛ پاراگراف توضیحی، "
    "مقدمه، واژه‌نامه، مثال تازه، جدول و پرسش تولید نکن. هر نکته را تا جای ممکن با عبارت منبع بنویس.\n"
    "اگر متن منبع مکان‌یاب‌هایی مانند [صفحهٔ ۲] دارد، فقط برچسب کامل و عیناً موجود را در "
    "source_locators بیاور؛ هیچ شماره یا مکان‌یاب تازه نساز.\n"
    "اگر فشرده‌سازی بدون حذف واقعیت فنی ممکن نیست، تمام نکته‌های لازم برای حفظ واقعیت‌ها را نگه دار؛ "
    "اعتبار منبع از کوتاهی مهم‌تر است.\n"
    "پاسخ فقط یک شیء JSON مطابق schema باشد و متن بیرون از JSON نداشته باشد."
)

COMPACT_NOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "heading": {"type": "string"},
        "points": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_COMPACT_POINTS_PER_CHUNK,
        },
        "source_locators": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_SOURCE_LOCATORS_PER_SECTION,
        },
    },
    "required": ["heading", "points", "source_locators"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class OfficeSourceChunk:
    """One bounded piece of extracted text plus only its real source labels."""

    text: str
    locators: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompactSection:
    """One terse revision section; it has no full-booklet prose fields."""

    heading: str
    points: tuple[str, ...]
    source_locators: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompactNotes:
    """A compact-only document model and renderer input."""

    title: str
    sections: tuple[CompactSection, ...]

    @property
    def point_text(self) -> str:
        return "\n".join(point for section in self.sections for point in section.points)

    def to_markdown(self) -> str:
        """Render a genuinely compact, bullet-only revision sheet."""
        parts = [f"# {self.title} — مرور سریع"]
        for section in self.sections:
            parts.append(f"## {section.heading}")
            if section.source_locators:
                parts.append("منبع: " + "، ".join(section.source_locators))
            parts.extend(f"- {point}" for point in section.points)
        return "\n\n".join(parts)

    def as_structured_notes(self) -> StructuredNotes:
        """Adapt compact bullets for the shared evidence validator only."""
        return StructuredNotes(
            title=self.title,
            summary="",
            sections=tuple(
                NoteSection(
                    heading=section.heading,
                    bullets=section.points,
                    source_locators=section.source_locators,
                )
                for section in self.sections
            ),
        )


def _locator_labels(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(" ".join(match.group(1).split()) for match in _LOCATOR_LINE.finditer(text)))


def _attached_locator_matches(chunk: OfficeSourceChunk) -> list[re.Match[str]]:
    """Recognize the first attached marker per label, not literal repeats in the body."""
    expected = set(chunk.locators)
    matches = []
    for match in _LOCATOR_LINE.finditer(chunk.text):
        label = match.group(1).strip()
        if label in expected:
            expected.remove(label)
            matches.append(match)
    return matches


def _strip_chunk_locators(chunk: OfficeSourceChunk) -> str:
    """Remove only attached markers; identical later lines remain source text."""
    parts = []
    cursor = 0
    for match in _attached_locator_matches(chunk):
        parts.append(chunk.text[cursor:match.start()])
        cursor = match.end()
    parts.append(chunk.text[cursor:])
    return "".join(parts).strip()


def source_text(chunks: Iterable[OfficeSourceChunk], *, include_locators: bool = True) -> str:
    """Join bounded source pieces without changing their order or content."""
    parts = [
        chunk.text.strip() if include_locators else _strip_chunk_locators(chunk)
        for chunk in chunks
    ]
    return "\n\n".join(part for part in parts if part.strip())


def locators_in_chunks(chunks: Iterable[OfficeSourceChunk]) -> tuple[str, ...]:
    """The exact locator labels attached by the cached extraction."""
    labels = [label for chunk in chunks for label in chunk.locators]
    return tuple(dict.fromkeys(labels))


def _split_plain_text(text: str, *, max_chars: int) -> list[str]:
    """Bound unlocated text without inserting markers that could alter content."""
    source = str(text or "")
    parts: list[str] = []
    start = 0
    while start < len(source):
        end = min(len(source), start + max_chars)
        if end < len(source):
            window_start = start + max_chars // 2
            candidates = (
                source.rfind("\n\n", window_start, end),
                source.rfind("\n", window_start, end),
                source.rfind(" ", window_start, end),
            )
            boundary = max(candidates)
            if boundary > start:
                end = boundary
        part = source[start:end].strip()
        if part:
            parts.append(part)
        start = end
        while start < len(source) and source[start].isspace():
            start += 1
    return parts


def office_source_chunks(
    text: str,
    cached: list[DocChunk] | None = None,
    *,
    max_chars: int = MAX_MODEL_INPUT_CHARS,
) -> list[OfficeSourceChunk]:
    """Use cached page/slide locators, or preserve raw text without fake ones."""
    source_chunks: list[OfficeSourceChunk] = []
    if cached:
        for chunk in cached:
            expected = tuple(" ".join(str(value).split())[:120] for value in chunk.sources)
            expected_set = set(expected)
            labels = tuple(
                label for label in _locator_labels(chunk.text) if label in expected_set
            )
            source_chunks.append(OfficeSourceChunk(chunk.text, tuple(dict.fromkeys(labels))))
    else:
        # Do not use the locator-tagged document chunker for pasted text: an
        # internal [part] marker could collide with a literal user line.
        source_chunks = [
            OfficeSourceChunk(part)
            for part in _split_plain_text(text, max_chars=max_chars)
        ]
    return _rechunk_source_chunks(source_chunks, max_chars=max_chars)


def _rechunk_source_chunks(
    chunks: Iterable[OfficeSourceChunk], *, max_chars: int
) -> list[OfficeSourceChunk]:
    """Split at the original source labels so note calls stay bounded."""
    bounded: list[OfficeSourceChunk] = []
    for chunk in chunks:
        if len(chunk.text) <= max_chars and len(chunk.locators) <= MAX_SOURCE_LOCATORS_PER_SECTION:
            bounded.append(chunk)
            continue
        if not chunk.locators:
            # A locator-shaped line in user-pasted text is ordinary content.
            bounded.extend(
                OfficeSourceChunk(part)
                for part in _split_plain_text(chunk.text, max_chars=max_chars)
            )
            continue
        matches = _attached_locator_matches(chunk)
        if not matches:
            # Preserve trusted locator metadata if an older extraction cache
            # stored the sources separately from the text marker.
            bounded.extend(
                OfficeSourceChunk(part, chunk.locators)
                for part in _split_plain_text(chunk.text, max_chars=max_chars)
            )
            continue
        if chunk.text[:matches[0].start()].strip():
            bounded.extend(
                OfficeSourceChunk(part)
                for part in _split_plain_text(
                    chunk.text[:matches[0].start()], max_chars=max_chars
                )
            )
        sections: list[DocSection] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(chunk.text)
            body = chunk.text[match.end():end].strip()
            if body:
                sections.append(DocSection(match.group(1).strip(), "", body))
        # Keep every locator while respecting the compact schema's per-section
        # locator bound; never trim a tail of page/slide references.
        for start in range(0, len(sections), MAX_SOURCE_LOCATORS_PER_SECTION):
            group = sections[start:start + MAX_SOURCE_LOCATORS_PER_SECTION]
            bounded.extend(
                OfficeSourceChunk(part.text, part.sources)
                for part in chunk_sections(group, max_chars=max_chars)
            )
    return [chunk for chunk in bounded if chunk.text.strip()]


def _source_heading(text: str, index: int) -> str:
    """A source heading if present, otherwise a neutral ordinal label."""
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        heading = _MARKDOWN_HEADING.match(candidate)
        if heading:
            return heading.group(1).strip()[:120]
        if len(candidate) <= 90 and not candidate.endswith((".", "؟", "!", "؛")):
            return candidate[:120]
        break
    return f"بخش {index}"


def basic_source_booklet(
    chunks: list[OfficeSourceChunk], *, title: str
) -> StructuredNotes:
    """A no-provider booklet that keeps every extracted source word verbatim."""
    sections: list[NoteSection] = []
    for index, chunk in enumerate(chunks, start=1):
        content = _strip_chunk_locators(chunk)
        if not content:
            continue
        locators = chunk.locators
        heading = _source_heading(content, index)
        paragraphs = tuple(
            part.strip()
            for part in re.split(r"\n\s*\n", content)
            if part.strip()
        )
        sections.append(
            NoteSection(
                heading=heading,
                paragraphs=paragraphs or (content,),
                source_locators=tuple(dict.fromkeys(locators))[:MAX_SOURCE_LOCATORS_PER_SECTION],
            )
        )
    if not sections:
        raise StructuringError("متن قابل استفاده‌ای برای ساخت جزوهٔ منبع‌محور پیدا نشد.")
    review_source = next(
        (paragraph for section in reversed(sections) for paragraph in reversed(section.paragraphs) if paragraph),
        "",
    )
    return StructuredNotes(
        title=title or "جزوهٔ منبع‌محور",
        sections=tuple(sections),
        final_review=(review_source,) if review_source else (),
    )


def _locator_source_map(chunks: Iterable[OfficeSourceChunk]) -> dict[str, str]:
    """Map each exact cached locator to only the text under that marker."""
    fragments: dict[str, list[str]] = {}
    for chunk in chunks:
        if not chunk.locators:
            continue
        matches = _attached_locator_matches(chunk)
        if matches:
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(chunk.text)
                body = chunk.text[match.end():end].strip()
                if body:
                    label = " ".join(match.group(1).split())
                    fragments.setdefault(label, []).append(body)
            continue
        body = _strip_chunk_locators(chunk)
        if body:
            for locator in chunk.locators:
                label = " ".join(locator.split())
                if label:
                    fragments.setdefault(label, []).append(body)
    return {label: "\n\n".join(parts) for label, parts in fragments.items()}


def _audit_meets_office_floor(audit: SignalAudit, *, compact: bool = False) -> bool:
    """Enforce zero fabrications, numeric preservation, and realistic signal recall."""
    if audit.fabricated:
        return False
    if audit.number_coverage < 0.98:
        return False
    min_coverage = 1.0 if audit.total < 4 else (0.85 if compact else 0.95)
    return audit.coverage >= min_coverage


def _valid_section_locators(locators: tuple[str, ...], allowed: set[str]) -> bool:
    """Require genuine non-empty locators when source has locators, none otherwise."""
    if not allowed:
        return len(locators) == 0
    return (
        bool(locators)
        and len(locators) <= MAX_SOURCE_LOCATORS_PER_SECTION
        and set(locators) <= allowed
    )


def _full_result_is_grounded(
    notes: StructuredNotes,
    source: str,
    allowed_locators: tuple[str, ...],
    locator_sources: dict[str, str],
) -> bool:
    try:
        notes = ensure_structured_booklet(notes, source)
    except StructuringError:
        return False
    if not notes.has_content or not notes.sections:
        return False
    if notes.verification_report is None:
        # Compatibility for callers returning a legacy report-less booklet.
        notes, report = gate_notes(notes, source, strict=True, mode="full")
        try:
            enforce_final_acceptance(notes, report, mode="full", stage="office_final")
        except NoteQualityError:
            return False
        if not _audit_meets_office_floor(report.audit, compact=False):
            logger.warning("Office full booklet rejected by evidence gate %s", report.audit.summary())
            return False
    allowed_set = set(allowed_locators)
    for section in notes.sections:
        missing_verified_locators = (notes.verification_report is not None
                                     and not section.source_locators and bool(section.source_block_ids))
        if not missing_verified_locators and not _valid_section_locators(section.source_locators, allowed_set):
            logger.warning("Office full booklet rejected invalid section source locators")
            return False
        if section.source_locators and notes.verification_report is None:
            # Modern reports already check the exact block/range and its
            # locator. The legacy coarse audit must use the public payload;
            # stringifying a Section would scan its internal provenance IDs.
            locator_source = "\n\n".join(
                locator_sources.get(locator, "") for locator in section.source_locators
            )
            if locator_source and audit_notes(locator_source, StructuredNotes(sections=(section,))).fabricated:
                logger.warning("Office full booklet section failed its locator-local audit")
                return False
    return True


def _normalize_office_source(
    chunks: Iterable[OfficeSourceChunk], title: str, source_type: str, document_id: str | None,
) -> NormalizedDocument:
    blocks: list[SourceBlock] = []
    for chunk in chunks:
        text = chunk.text.strip()
        if not text:
            continue
        markers = _attached_locator_matches(OfficeSourceChunk(text, chunk.locators))
        if not markers:
            parsed = NormalizedDocument.from_source_text(text, source_type=source_type)
            for idx, subblock in enumerate(parsed.source_blocks):
                prefix = "\n\n" if blocks and idx == 0 else ""
                blocks.append(SourceBlock(
                    "", subblock.kind, prefix + subblock.text, 0, 0,
                    page_number=subblock.page_number,
                    slide_number=subblock.slide_number,
                ))
            continue
        starts = sorted({0, *(match.start() for match in markers), len(text)})
        labels = {match.start(): match.group(1) for match in markers}
        for start, end in zip(starts, starts[1:], strict=False):
            label = labels.get(start, "")
            match = re.match(r"(صفحهٔ|اسلاید)\s*(\d+)", label)
            kind = "paragraph" if match is None else "page" if match[1] == "صفحهٔ" else "slide"
            number = int(match[2]) if match is not None else None
            blocks.append(SourceBlock(
                "", kind, ("\n\n" if blocks and start == 0 else "") + text[start:end], 0, 0,
                page_number=number if kind == "page" else None,
                slide_number=number if kind == "slide" else None,
            ))
    return NormalizedDocument.from_blocks(blocks, source_type=source_type, title=title, document_id=document_id)


async def generate_full_booklet(
    chunks: list[OfficeSourceChunk],
    *,
    title: str,
    settings: Settings,
    session: Any,
    on_stage: StageCallback | None = None,
    on_incomplete: IncompleteCallback | None = None,
    source_type: str = "txt",
    document_id: str | None = None,
) -> tuple[StructuredNotes, bool]:
    """Run the existing full booklet pipeline, with a complete safe fallback.

    ``on_stage`` is forwarded to the map-reduce pipeline, so the caller's
    progress bar shows the same evidence gate («اعتبارسنجی») and editorial
    compile («ویرایش نهایی») stages the lecture pipeline shows; the callback
    never changes the outcome. ``on_incomplete`` is forwarded as well: an
    accepted booklet whose coverage is imperfect is delivered and reported,
    exactly like the audio flow.
    """
    source = source_text(chunks, include_locators=True)
    fallback = basic_source_booklet(chunks, title=title)
    fallback = ensure_structured_booklet(fallback, source)
    if getattr(settings, "note_strict_grounding", False):
        fallback, fallback_report = gate_notes(fallback, source, strict=True, mode="full")
        # The extractive fallback is still a booklet path: it must clear the same
        # final contract before any caller can render or deliver it.
        fallback = enforce_final_acceptance(
            fallback, fallback_report, mode="full", stage="office_fallback"
        )
    if not settings.note_configured or session is None:
        # Same visible contract as the model path: the deterministic booklet
        # also passes the evidence gate, and the editor-assembled fallback is
        # the deterministic analogue of the final editorial pass. When strict
        # grounding is off the pipeline below would skip those stages, so the
        # fallback must not claim them either.
        if getattr(settings, "note_strict_grounding", False):
            await _notify(on_stage, "validating")
        if compile_will_run(fallback, settings, mode="full", presentation=False):
            await _notify(on_stage, "compiling")
        return fallback, True
    locator_sources = _locator_source_map(chunks)
    allowed = tuple(dict.fromkeys((*locators_in_chunks(chunks), *locator_sources)))
    try:
        generated = await structure_document(
            _normalize_office_source(chunks, title, source_type, document_id),
            settings,
            session,
            mode="full",
            source_locators=True,
            on_stage=on_stage,
            on_incomplete=on_incomplete,
        )
    except (LLMError, StructuringError) as exc:
        logger.warning("Office full booklet using source fallback error=%s", type(exc).__name__)
        return fallback, True
    if not getattr(settings, "note_strict_grounding", False):
        try:
            normalized = ensure_structured_booklet(generated, source)
        except StructuringError:
            return fallback, True
        if structurally_valid_booklet(normalized) and normalized.has_content:
            return normalized, (normalized.quality_state == QUALITY_RECOVERED)
        return fallback, True
    if not _full_result_is_grounded(generated, source, allowed, locator_sources):
        return fallback, True
    if generated.verification_report is not None:
        return generated, False  # render the same structured artifact that was verified
    gated, report = gate_notes(generated, source, strict=True, mode="full")
    if not gated.has_content or not _audit_meets_office_floor(report.audit, compact=False):
        logger.warning("Office full booklet using source fallback after final audit")
        return fallback, True
    return gated, False


def compact_notes_response_schema() -> dict[str, Any]:
    """A separate JSON schema with compact-only fields."""
    return COMPACT_NOTE_SCHEMA


def _parse_compact_payload(
    payload: object,
    *,
    source: OfficeSourceChunk,
    index: int,
) -> CompactSection | None:
    if not isinstance(payload, dict):
        return None
    raw_points = payload.get("points")
    if not isinstance(raw_points, (list, tuple)) or not raw_points:
        return None
    if len(raw_points) > MAX_COMPACT_POINTS_PER_CHUNK:
        return None
    points: list[str] = []
    for raw in raw_points:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_COMPACT_POINT_CHARS:
            return None
        points.append(" ".join(raw.split()))
    if len("\n".join(points)) > max(1, len(source.text)):
        return None
    allowed = set(source.locators)
    raw_locators = payload.get("source_locators")
    if not isinstance(raw_locators, (list, tuple)):
        return None
    locators = tuple(dict.fromkeys(str(value).strip() for value in raw_locators if str(value).strip()))
    if not _valid_section_locators(locators, allowed):
        return None
    raw_heading = payload.get("heading")
    if not isinstance(raw_heading, str) or len(raw_heading) > 120:
        return None
    heading = " ".join(raw_heading.split())
    body = _strip_chunk_locators(source)
    if heading and heading not in body:
        heading = _source_heading(body, index)
    if not heading:
        heading = _source_heading(body, index)
    candidate = CompactSection(heading, tuple(points), locators)
    adapted = CompactNotes("", (candidate,)).as_structured_notes()
    report = audit_notes(source.text, adapted)
    if not _audit_meets_office_floor(report, compact=True):
        return None
    return candidate


def _extractive_compact_section(
    source: OfficeSourceChunk, *, index: int
) -> CompactSection:
    """Select source sentences carrying hard facts, plus one topic sentence."""
    body = _strip_chunk_locators(source)
    sentences = split_sentences(body)
    if not sentences:
        sentences = tuple(line.strip() for line in body.splitlines() if line.strip())
    if not sentences and body:
        sentences = (body,)
    selected: set[int] = set()
    source_signals = extract_signals(body)
    covered = set()
    for position, sentence in enumerate(sentences):
        signals = set(extract_signals(sentence))
        if signals:
            selected.add(position)
            covered.update(signals)
    # Keep every sentence needed to retain a source number or technical term.
    if source_signals - covered:
        for position, sentence in enumerate(sentences):
            if extract_signals(sentence) & (source_signals - covered):
                selected.add(position)
                covered.update(extract_signals(sentence))
    if sentences:
        selected.add(0)
        richest = max(
            range(len(sentences)),
            key=lambda position: len(content_words(sentences[position])),
        )
        selected.add(richest)
    points = tuple(sentences[position] for position in sorted(selected))
    return CompactSection(
        heading=_source_heading(body, index),
        points=points or (body,),
        source_locators=tuple(dict.fromkeys(source.locators)),
    )


def _compact_section_grounded(section: CompactSection, source: OfficeSourceChunk) -> bool:
    if not section.points or any(not point.strip() for point in section.points):
        return False
    if not _valid_section_locators(section.source_locators, set(source.locators)):
        return False
    adapted = CompactNotes("", (section,)).as_structured_notes()
    if section.source_locators:
        locator_map = _locator_source_map((source,))
        locator_source = "\n\n".join(
            locator_map.get(locator, "") for locator in section.source_locators
        )
        if locator_source and audit_notes(locator_source, adapted).fabricated:
            return False
    report = audit_notes(source.text, adapted)
    return _audit_meets_office_floor(report, compact=True)


def _compact_prompt(source: OfficeSourceChunk, title: str, index: int) -> str:
    return (
        f"عنوان اصلی منبع: {title}\n"
        f"شمارهٔ بخش فنی برای حفظ ترتیب: {index}\n\n"
        "<source>\n"
        f"{source.text}\n"
        "</source>\n\n"
        "خروجی دقیقاً یک شیء JSON با کلیدهای heading، points و source_locators باشد. "
        f"حداکثر {MAX_COMPACT_POINTS_PER_CHUNK} نکتهٔ کوتاه؛ همهٔ عددها و اصطلاح‌های فنی موجود را حفظ کن."
    )


async def generate_compact_notes(
    chunks: list[OfficeSourceChunk],
    *,
    title: str,
    settings: Settings,
    session: Any,
    on_stage: StageCallback | None = None,
    on_chunk: ChunkCallback | None = None,
) -> tuple[CompactNotes, bool]:
    """Build compact notes with a dedicated prompt/schema or extractive fallback.

    ``on_chunk(done, total)`` reports each finished source chunk — in the
    model path as answers arrive, in the extractive path as sections are
    selected — and ``on_stage("validating")`` marks the final whole-document
    evidence audit, so the caller's progress bar mirrors the full booklet's.
    """
    if not chunks:
        raise StructuringError("متن قابل استفاده‌ای برای یادداشت مرور سریع پیدا نشد.")
    chunks = _rechunk_source_chunks(chunks, max_chars=MAX_COMPACT_INPUT_CHARS)
    source_length = sum(len(_strip_chunk_locators(chunk)) for chunk in chunks)
    fallback_sections: list[CompactSection] = []
    provider_active = settings.note_configured and session is not None
    for index, chunk in enumerate(chunks, start=1):
        fallback_sections.append(_extractive_compact_section(chunk, index=index))
        # The AI path reports each chunk from ``make_one`` as answers arrive;
        # without a provider the deterministic selection *is* the work.
        if not provider_active:
            await _notify(on_chunk, index, len(chunks))
    fallback = CompactNotes(title or "مرور سریع", tuple(fallback_sections))
    if not provider_active:
        result = fallback
        used_fallback = True
    else:
        concurrency = max(1, min(8, int(settings.note_concurrency or 1)))
        semaphore = asyncio.Semaphore(concurrency)
        completed = 0

        async def make_one(index: int, source: OfficeSourceChunk) -> tuple[CompactSection, bool]:
            nonlocal completed
            deterministic = _extractive_compact_section(source, index=index)
            async with semaphore:
                try:
                    payload = await complete_json(
                        session,
                        settings,
                        system=COMPACT_NOTE_SYSTEM_PROMPT,
                        user=_compact_prompt(source, title, index),
                        schema=compact_notes_response_schema(),
                        task="COMPACT_NOTES",
                    )
                except LLMError as exc:
                    logger.warning(
                        "Office compact chunk using extractive fallback index=%d error=%s",
                        index,
                        type(exc).__name__,
                    )
                    completed += 1
                    await _notify(on_chunk, completed, len(chunks))
                    return deterministic, True
            parsed = _parse_compact_payload(payload, source=source, index=index)
            if parsed is None or not _compact_section_grounded(parsed, source):
                logger.warning("Office compact chunk using extractive fallback index=%d validation=failed", index)
                completed += 1
                await _notify(on_chunk, completed, len(chunks))
                return deterministic, True
            completed += 1
            await _notify(on_chunk, completed, len(chunks))
            return parsed, False

        results = await asyncio.gather(
            *(make_one(index, chunk) for index, chunk in enumerate(chunks, start=1))
        )
        result = CompactNotes(
            title or "مرور سریع",
            tuple(section for section, _used_fallback in results),
        )
        used_fallback = any(was_fallback for _section, was_fallback in results)
    if not result.sections or not result.point_text.strip():
        raise StructuringError("یادداشت مرور سریع از متن منبع ساخته نشد.")
    if source_length >= MIN_SOURCE_CHARS_FOR_RATIO and len(result.point_text) > source_length * MAX_COMPACT_SOURCE_RATIO:
        logger.warning("Office compact output rejected as insufficiently compact")
        raise StructuringError(
            "این متن جزئیات فنی متراکمی دارد و نمی‌توان آن را بدون حذف اطلاعات مهم "
            "به‌طور ایمن فشرده کرد؛ نسخهٔ کامل یا فایل اصلی را دریافت کنید."
        )
    # One final whole-document audit catches interactions between chunks as
    # well as any model output that slipped through a per-chunk check.
    await _notify(on_stage, "validating")
    adapted = result.as_structured_notes()
    complete_source = source_text(chunks, include_locators=True)
    report = audit_notes(complete_source, adapted)
    if not _audit_meets_office_floor(report, compact=True):
        logger.warning("Office compact output failed final evidence audit; using extractive fallback")
        result = fallback
        used_fallback = True
        report = audit_notes(complete_source, result.as_structured_notes())
        if not _audit_meets_office_floor(report, compact=True):
            raise StructuringError("یادداشت فشرده نتوانست عددها و اصطلاح‌های منبع را کامل حفظ کند.")
    if source_length >= MIN_SOURCE_CHARS_FOR_RATIO and len(result.point_text) > source_length * MAX_COMPACT_SOURCE_RATIO:
        raise StructuringError(
            "متن منبع آن‌قدر متراکم است که فشرده‌سازی ایمن ممکن نیست؛ نسخهٔ کامل را انتخاب کنید."
        )
    return result, used_fallback
