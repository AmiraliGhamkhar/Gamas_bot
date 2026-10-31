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
from dataclasses import dataclass
from typing import Any, Iterable

from .config import Settings
from .doc_extract import DocChunk, DocSection, chunk_sections
from .learning import MAX_MODEL_INPUT_CHARS
from .llm import LLMError, complete_json
from .notes import (
    MAX_SOURCE_LOCATORS_PER_SECTION,
    NoteSection,
    StructuredNotes,
    StructuringError,
    gate_notes,
    structure_document,
)
from .notes_evidence import audit_notes, content_words, extract_signals, split_sentences

logger = logging.getLogger(__name__)

#: Source material sent to the compact-only prompt in one request. The bound
#: keeps the prompt small even when an extracted file contains many pages.
MAX_COMPACT_INPUT_CHARS = 6_000
MAX_COMPACT_POINTS_PER_CHUNK = 32
MAX_COMPACT_POINT_CHARS = 6_000
#: Refuse note generation above this complete-source ceiling. The original
#: file remains downloadable; no note path may silently discard its tail.
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


def _strip_chunk_locators(chunk: OfficeSourceChunk) -> str:
    """Remove only labels explicitly attached by the document extractor.

    Pasted text may itself contain a line such as ``[صفحهٔ ۲]``. It is source
    text, not metadata, unless the extractor supplied that exact label.
    """
    if not chunk.locators:
        return chunk.text.strip()
    allowed = set(chunk.locators)
    cleaned = _LOCATOR_LINE.sub(
        lambda match: "" if match.group(1).strip() in allowed else match.group(0),
        chunk.text,
    )
    return cleaned.strip()


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
        if not chunk.locators:
            # A locator-shaped line in user-pasted text is ordinary content.
            bounded.extend(
                OfficeSourceChunk(part)
                for part in _split_plain_text(chunk.text, max_chars=max_chars)
            )
            continue
        expected = set(chunk.locators)
        matches = [
            match
            for match in _LOCATOR_LINE.finditer(chunk.text)
            if match.group(1).strip() in expected
        ]
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
    return StructuredNotes(title=title or "جزوهٔ منبع‌محور", sections=tuple(sections))


def _locator_source_map(chunks: Iterable[OfficeSourceChunk]) -> dict[str, str]:
    """Map each exact cached locator to only the text under that marker."""
    fragments: dict[str, list[str]] = {}
    for chunk in chunks:
        if not chunk.locators:
            continue
        expected = set(chunk.locators)
        matches = [
            match
            for match in _LOCATOR_LINE.finditer(chunk.text)
            if match.group(1).strip() in expected
        ]
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


def _full_result_is_grounded(
    notes: StructuredNotes,
    source: str,
    allowed_locators: tuple[str, ...],
    locator_sources: dict[str, str],
) -> bool:
    if not notes.has_content or not notes.sections:
        return False
    _, report = gate_notes(notes, source, strict=True, mode="full")
    if report.audit.fabricated or report.audit.missing:
        logger.warning("Office full booklet rejected by evidence gate %s", report.audit.summary())
        return False
    cited: set[str] = set()
    for section in notes.sections:
        for locator in section.source_locators:
            if locator not in allowed_locators:
                logger.warning("Office full booklet rejected an unknown source locator")
                return False
            cited.add(locator)
        if section.source_locators:
            locator_source = "\n\n".join(
                locator_sources.get(locator, "") for locator in section.source_locators
            )
            if locator_source and audit_notes(locator_source, section).fabricated:
                logger.warning("Office full booklet section failed its locator-local audit")
                return False
        if allowed_locators and not section.source_locators:
            logger.warning("Office full booklet section has no source locator")
            return False
    if allowed_locators and set(allowed_locators) - cited:
        logger.warning("Office full booklet omitted source locators count=%d", len(set(allowed_locators) - cited))
        return False
    return True


async def generate_full_booklet(
    chunks: list[OfficeSourceChunk],
    *,
    title: str,
    settings: Settings,
    session: Any,
) -> tuple[StructuredNotes, bool]:
    """Run the existing full booklet pipeline, with a complete safe fallback."""
    source = source_text(chunks, include_locators=True)
    source_chars = len(source_text(chunks, include_locators=False))
    if source_chars > MAX_OFFICE_NOTE_SOURCE_CHARS:
        raise StructuringError(
            "متن استخراج‌شده از سقف تولید جزوهٔ آزاد بیشتر است؛ فایل اصلی را دریافت کنید "
            "و سند را به چند بخش کوچک‌تر تقسیم و دوباره بفرستید."
        )
    fallback = basic_source_booklet(chunks, title=title)
    if not settings.note_configured or session is None:
        return fallback, True
    locator_sources = _locator_source_map(chunks)
    allowed = tuple(dict.fromkeys((*locators_in_chunks(chunks), *locator_sources)))
    try:
        generated = await structure_document(
            source,
            settings,
            session,
            mode="full",
            source_locators=True,
        )
    except (LLMError, StructuringError) as exc:
        logger.warning("Office full booklet using source fallback error=%s", type(exc).__name__)
        return fallback, True
    if not _full_result_is_grounded(generated, source, allowed, locator_sources):
        return fallback, True
    gated, report = gate_notes(generated, source, strict=True, mode="full")
    if report.audit.fabricated or report.audit.missing or not gated.has_content:
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
    if len(locators) > MAX_SOURCE_LOCATORS_PER_SECTION or any(locator not in allowed for locator in locators):
        return None
    if set(locators) != allowed:
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
    if report.fabricated or report.missing:
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
    if set(section.source_locators) != set(source.locators):
        return False
    adapted = CompactNotes("", (section,)).as_structured_notes()
    report = audit_notes(source.text, adapted)
    return not report.fabricated and not report.missing


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
) -> tuple[CompactNotes, bool]:
    """Build compact notes with a dedicated prompt/schema or extractive fallback."""
    if not chunks:
        raise StructuringError("متن قابل استفاده‌ای برای یادداشت مرور سریع پیدا نشد.")
    chunks = _rechunk_source_chunks(chunks, max_chars=MAX_COMPACT_INPUT_CHARS)
    source_length = sum(len(_strip_chunk_locators(chunk)) for chunk in chunks)
    if source_length > MAX_OFFICE_NOTE_SOURCE_CHARS:
        raise StructuringError(
            "متن استخراج‌شده از سقف تولید یادداشت مرور سریع بیشتر است؛ فایل اصلی را دریافت کنید "
            "و سند را به چند بخش کوچک‌تر تقسیم کنید."
        )
    fallback_sections = tuple(
        _extractive_compact_section(chunk, index=index)
        for index, chunk in enumerate(chunks, start=1)
    )
    fallback = CompactNotes(title or "مرور سریع", fallback_sections)
    if not settings.note_configured or session is None:
        result = fallback
        used_fallback = True
    else:
        concurrency = max(1, min(8, int(settings.note_concurrency or 1)))
        semaphore = asyncio.Semaphore(concurrency)

        async def make_one(index: int, source: OfficeSourceChunk) -> tuple[CompactSection, bool]:
            deterministic = _extractive_compact_section(source, index=index)
            async with semaphore:
                try:
                    payload = await complete_json(
                        session,
                        settings,
                        system=COMPACT_NOTE_SYSTEM_PROMPT,
                        user=_compact_prompt(source, title, index),
                        schema=compact_notes_response_schema(),
                    )
                except LLMError as exc:
                    logger.warning(
                        "Office compact chunk using extractive fallback index=%d error=%s",
                        index,
                        type(exc).__name__,
                    )
                    return deterministic, True
            parsed = _parse_compact_payload(payload, source=source, index=index)
            if parsed is None or not _compact_section_grounded(parsed, source):
                logger.warning("Office compact chunk using extractive fallback index=%d validation=failed", index)
                return deterministic, True
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
    adapted = result.as_structured_notes()
    complete_source = source_text(chunks, include_locators=True)
    report = audit_notes(complete_source, adapted)
    if report.fabricated or report.missing:
        logger.warning("Office compact output failed final evidence audit; using extractive fallback")
        result = fallback
        used_fallback = True
        report = audit_notes(complete_source, result.as_structured_notes())
        if report.fabricated or report.missing:
            raise StructuringError("یادداشت فشرده نتوانست عددها و اصطلاح‌های منبع را کامل حفظ کند.")
    if source_length >= MIN_SOURCE_CHARS_FOR_RATIO and len(result.point_text) > source_length * MAX_COMPACT_SOURCE_RATIO:
        raise StructuringError(
            "متن منبع آن‌قدر متراکم است که فشرده‌سازی ایمن ممکن نیست؛ نسخهٔ کامل را انتخاب کنید."
        )
    return result, used_fallback
