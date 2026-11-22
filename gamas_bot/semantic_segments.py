"""Events and exact source intervals for topic-first booklet mapping."""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from .document_model import PLAN_SCHEMA_VERSION, ContentPlan, NormalizedDocument, SourceRange
from .transcript import sentence_spans, starts_a_topic


@dataclass(frozen=True, slots=True)
class SemanticUnit:
    topic_id: str
    title: str
    parent_id: str | None
    source_range: SourceRange
    # Optional orientation, never part of this unit's evidence or coverage.
    preceding_context: SourceRange | None = None
    # Ordered topic IDs/titles when consecutive small topics are packed into
    # one provider request. Empty tuple means a single topic (topic_id/title).
    packed_topic_ids: tuple[str, ...] = ()
    packed_titles: tuple[str, ...] = ()

    @property
    def covered_topic_ids(self) -> tuple[str, ...]:
        return self.packed_topic_ids or (self.topic_id,)

    @property
    def covered_titles(self) -> tuple[str, ...]:
        return self.packed_titles or (self.title,)


def pack_semantic_units(
    source: SemanticSource,
    units: Sequence[SemanticUnit],
    *,
    target_chars: int,
    fits: Callable[[str], bool] | None = None,
) -> list[SemanticUnit]:
    """Pack consecutive small units in source order up to ``target_chars``.

    Preserves exact contiguous source offsets and every ``source_block_id``
    while keeping the request count bounded (typically 1–3 calls for sources up
    to ~50k chars instead of one request per page, slide, or short topic).
    """
    if target_chars <= 0 or len(units) <= 1:
        return list(units)

    def flush(group: list[SemanticUnit]) -> SemanticUnit:
        if len(group) == 1:
            return group[0]
        first, last = group[0], group[-1]
        block_ids = tuple(
            dict.fromkeys(bid for unit in group for bid in unit.source_range.block_ids)
        )
        topic_ids = tuple(
            dict.fromkeys(tid for unit in group for tid in unit.covered_topic_ids)
        )
        titles = tuple(
            dict.fromkeys(title for unit in group for title in unit.covered_titles)
        )
        return SemanticUnit(
            topic_id=first.topic_id,
            title=first.title,
            parent_id=first.parent_id,
            source_range=SourceRange(
                first.source_range.start_offset,
                last.source_range.end_offset,
                block_ids,
            ),
            preceding_context=first.preceding_context,
            packed_topic_ids=topic_ids if len(topic_ids) > 1 else (),
            packed_titles=titles if len(topic_ids) > 1 else (),
        )

    packed: list[SemanticUnit] = []
    group: list[SemanticUnit] = [units[0]]
    for unit in units[1:]:
        start = group[0].source_range.start_offset
        prev_end = group[-1].source_range.end_offset
        end = unit.source_range.end_offset
        contiguous = unit.source_range.start_offset == prev_end
        within_target = end - start <= target_chars
        if contiguous and within_target and (fits is None or fits(source.text[start:end])):
            group.append(unit)
        else:
            packed.append(flush(group))
            group = [unit]
    packed.append(flush(group))
    return packed


@dataclass(frozen=True, slots=True)
class SemanticRecovery:
    topic_id: str
    reason: str
    original: SourceRange
    replacements: tuple[SourceRange, ...]


def build_semantic_topics(document: NormalizedDocument, plan: ContentPlan) -> list[SemanticUnit]:
    """Use the complete ordered plan, with no size-based packing across topics.

    Normalized blocks retain page, slide and section identity. A parent's own
    source runs before its children; child source is not repeated in the parent.
    """
    plan.validate(document)
    if plan.schema_version != PLAN_SCHEMA_VERSION or any(not topic.source_ranges for topic in plan.topics):
        raise ValueError("semantic segmentation requires a range-backed plan")
    units = [SemanticUnit(
        topic.id, topic.title, topic.parent_id,
        SourceRange(topic.source_ranges[0].start_offset, topic.source_ranges[-1].end_offset, topic.source_block_ids),
    ) for topic in plan.topics]

    # The presentation adapter emits a structural document label before the
    # first slide. Attach this label to the first slide, not a paid empty
    # lesson. Never coalesce a preamble that contains actual source prose.
    if document.source_type == "pptx" and len(units) > 1:
        wrapper, first_slide = units[:2]
        prefix = document.text[:wrapper.source_range.end_offset].strip()
        if first_slide.title.startswith("اسلاید ") and prefix in {
            "## متن اسلایدها", "## متن اسلایدها و گفتار ارائه به تفکیک اسلاید",
        }:
            first = replace(first_slide, source_range=SourceRange(
                0, first_slide.source_range.end_offset,
                wrapper.source_range.block_ids + first_slide.source_range.block_ids,
            ))
            units = [replace(unit, parent_id=None) if unit.parent_id == wrapper.topic_id else unit
                     for unit in [first, *units[2:]]]
    return units


_PARAGRAPH = re.compile(r"\n[ \t]*\n+")
_LINE = re.compile(r"\n")
_SPACE = re.compile(r"\s+")


class SemanticSource:
    """Indexes the canonical source once so subdivisions preserve actual block IDs."""

    def __init__(self, document: NormalizedDocument) -> None:
        self.text = document.text
        self.blocks = document.source_blocks
        self.blocks_by_id = {block.id: block for block in self.blocks}
        self.locators_by_id = {unit.unit_id: unit.locator for unit in document.units if unit.locator}
        self.starts = tuple(block.start_offset for block in self.blocks)
        self.ends = tuple(block.end_offset for block in self.blocks)

    def span(self, start: int, end: int) -> SourceRange:
        if not 0 <= start < end <= len(self.text):
            raise ValueError("semantic source range is out of bounds")
        left = bisect_right(self.ends, start)
        right = bisect_left(self.starts, end)
        return SourceRange(start, end, tuple(block.id for block in self.blocks[left:right]))

    def body(self, unit: SemanticUnit) -> str:
        span = unit.source_range
        return self.text[span.start_offset:span.end_offset]

    def _cut(self, start: int, end: int, target: int, *, minimum: int) -> int:
        """Rightmost strong boundary near the fitted limit, then a finer cut."""
        left = bisect_left(self.starts, minimum)
        right = bisect_right(self.starts, target)
        at_blocks = [block.start_offset for block in self.blocks[left:right]]
        strong = [block.start_offset for block in self.blocks[left:right]
                  if block.page_number is not None or block.slide_number is not None or block.kind == "section"
                  or starts_a_topic(block.text)]
        text = self.text[start:target + 1]
        # For a single oversized page/block, subtopics can still be separated
        # by paragraphs, heading/list lines, sentences, then token fragments.
        candidates = (
            strong,
            at_blocks,
            (start + match.end() for match in _PARAGRAPH.finditer(text)),
            (start + match.end() for match in _LINE.finditer(text)),
            (start + finish for _, finish in sentence_spans(text)),
            (start + match.end() for match in _SPACE.finditer(text)),
        )
        for cuts in candidates:
            best = max((cut for cut in cuts if minimum <= cut <= target and start < cut < end), default=0)
            if best:
                return best
        return target

    def subunit(self, unit: SemanticUnit, start: int, end: int, overlap_chars: int) -> SemanticUnit:
        origin = unit.source_range.start_offset
        context = unit.preceding_context if start == origin else None
        if overlap_chars and start > origin:
            context_start = max(origin, start - overlap_chars)
            # Avoid beginning the orientation excerpt mid-word when possible.
            if context_start > origin and not self.text[context_start - 1].isspace():
                match = _SPACE.search(self.text, context_start, start)
                if match:
                    context_start = match.end()
            if context_start < start:
                context = self.span(context_start, start)
        return replace(unit, source_range=self.span(start, end), preceding_context=context)

    def fit(
        self, units: Sequence[SemanticUnit], fits: Callable[[str], bool], *,
        overlap_chars: int = 0, max_probe_chars: int | None = None,
    ) -> list[SemanticUnit]:
        """Keep fitting topics intact; partition only oversized ones."""
        result: list[SemanticUnit] = []
        for unit in units:
            start, end = unit.source_range.start_offset, unit.source_range.end_offset
            while start < end:
                if (max_probe_chars is None or end - start <= max_probe_chars) and fits(self.text[start:end]):
                    result.append(unit if start == unit.source_range.start_offset else self.subunit(unit, start, end, overlap_chars))
                    break
                lo, hi = start, min(end, start + max_probe_chars + 1) if max_probe_chars is not None else end
                while hi - lo > 1:
                    middle = (lo + hi) // 2
                    if fits(self.text[start:middle]):
                        lo = middle
                    else:
                        hi = middle
                if lo == start:
                    raise ValueError("request has no capacity for a source character")
                cut = self._cut(start, end, lo, minimum=start + max(1, (lo - start) // 2))
                result.append(self.subunit(unit, start, cut, overlap_chars))
                start = cut
        return result

    def bisect(self, unit: SemanticUnit, *, overlap_chars: int = 0) -> tuple[SemanticUnit, ...]:
        """Split an affected unit after a typed provider overflow, not its siblings."""
        start, end = unit.source_range.start_offset, unit.source_range.end_offset
        if end - start < 2:
            return ()
        middle = (start + end) // 2
        # Prefer balanced recovery; an isolated opening header is not enough.
        minimum = start + max(1, (end - start) // 3)
        maximum = min(end - 1, end - (end - start) // 3)
        cut = self._cut(start, end, maximum, minimum=minimum)
        if cut == maximum and not self.text[cut - 1].isspace():
            cut = self._cut(start, end, middle, minimum=start + 1)
            if cut < minimum:
                cut = middle
        return self.subunit(unit, start, cut, overlap_chars), self.subunit(unit, cut, end, overlap_chars)
