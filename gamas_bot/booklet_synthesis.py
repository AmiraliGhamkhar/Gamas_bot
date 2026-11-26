"""Bounded opt-in editorial proposals; never a replacement booklet generator."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import groupby

from . import notes as n
from ._prompt_block import synthesis_system_prompt
from .booklet_telemetry import record_call
from .document_model import ContentPlan, NormalizedDocument
from .llm import LLMContextOverflowError, LLMError, LLMOutputBudgetError

logger = logging.getLogger(__name__)
MAX_SYNTHESIS_REQUESTS = 24
MAX_GROUP_SPLIT_DEPTH = 2
_MAX_EDITS = 24  # per additive edit kind, per request
_MAX_QUOTE_CHARS = 360


def synthesis_schema() -> dict:
    text = {"type": "string"}

    def array(item: dict) -> dict:
        return {"type": "array", "items": item}

    def item(**fields: dict) -> dict:
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}

    return {"type": "object", "additionalProperties": False, "properties": {
        "title": text,
        "order": array(text),
        "headings": array(item(id=text, heading=text)),
        "summaries": array(item(id=text, quotes=array(text))),
        "deduplicate": array(item(id=text, field={"type": "string", "enum": ["paragraphs", "bullets"]})),
        "links": array(item(from_id=text, to_id=text, kind={"type": "string", "enum": ["related", "transition", "potential_conflict"]})),
        "comparisons": array(item(id=text, rows=array(item(id=text, quote=text)))),
    }}


@dataclass(frozen=True, slots=True)
class _Entry:
    id: str  # scoped editor handle; persisted/provenance IDs stay on the section
    section: n.NoteSection
    chapter: str
    ref: str
    links: tuple[tuple[str, str], ...] = ()


def _entries(notes: n.StructuredNotes, plan: ContentPlan) -> list[_Entry]:
    topics = {topic.id: topic for topic in plan.topics}
    roots: dict[str, str] = {}
    for topic in plan.topics:
        roots[topic.id] = roots.get(topic.parent_id or "", topic.id)
    return [_Entry(f"s{index:04d}", section, roots.get(section.topic_id, section.topic_id or f"s{index:04d}"),
                   n._section_reference(section, index))
            for index, section in enumerate(notes.sections, 1) if section.has_content
            and (not section.topic_id or section.topic_id in topics)]


def _quotes(section: n.NoteSection) -> frozenset[str]:
    return frozenset((*section.paragraphs, *section.bullets, *section.key_points, *section.examples))


def _chapter_highlight(section: n.NoteSection) -> str:
    # One complete extract per section, explicitly labelled as a highlight.
    return next((text for text in (*section.key_points, *section.paragraphs, *section.bullets)
                 if 1 <= len(text) <= _MAX_QUOTE_CHARS), "")


def _label(value: object, permitted: Sequence[str]) -> str:
    if (not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= n.MAX_TITLE_CHARS
            or any(char in value for char in "\n\r<>")):
        raise ValueError("invalid heading label")
    if value not in permitted:
        raise ValueError("heading must preserve the original label")
    return value


def _list(payload: dict, name: str) -> list:
    entries = payload.get(name, [])
    if not isinstance(entries, list) or len(entries) > _MAX_EDITS:
        raise ValueError("invalid edit array")
    return entries


def _order_ok(ordered: Sequence[_Entry], plan: ContentPlan) -> bool:
    positions: dict[str, list[int]] = {}
    chapters: dict[str, list[int]] = {}
    for index, entry in enumerate(ordered):
        if entry.section.topic_id:
            positions.setdefault(entry.section.topic_id, []).append(index)
            chapters.setdefault(entry.chapter, []).append(index)
    if any(indices[-1] - indices[0] + 1 != len(indices) for indices in (*positions.values(), *chapters.values())):
        return False
    for topic in plan.topics:
        indices = positions.get(topic.id)
        if not indices:
            continue
        parents = (*topic.depends_on, *((topic.parent_id,) if topic.parent_id else ()))
        if any(parent in positions and positions[parent][-1] > indices[0] for parent in parents):
            return False
    return True


def _preserves_topic_sequence(before: Sequence[_Entry], after: Sequence[_Entry]) -> bool:
    def sequences(items: Sequence[_Entry]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for entry in items:
            if entry.section.topic_id:
                result.setdefault(entry.section.topic_id, []).append(entry.id)
        return result

    return sequences(before) == sequences(after)


def _pair_conflicts(notes: n.StructuredNotes, entries: Sequence[_Entry]) -> set[tuple[str, str]]:
    handles = {entry.ref: entry.id for entry in entries}
    return {(min(handles[left], handles[right]), max(handles[left], handles[right]))
            for conflict in notes.merge_conflicts if conflict.kind == "potential_value_conflict"
            and len(conflict.identifiers) == 2
            for left, right in [conflict.identifiers] if left in handles and right in handles and handles[left] != handles[right]}


def _apply(
    payload: object, basis: n.StructuredNotes, entries: list[_Entry], plan: ContentPlan,
    *, level: str, chapter_highlights: bool, conflicts: set[tuple[str, str]],
) -> tuple[list[_Entry], str]:
    allowed = {"order", "headings", "links"}
    if level in {"whole", "final"}:
        allowed.add("title")
    if level in {"whole", "topic_group"}:
        allowed.update(("summaries", "comparisons", "deduplicate"))
    if chapter_highlights:
        allowed.add("summaries")
    # Strict OpenAI-compatible schema normalization requires the optional
    # properties on the wire with null for omitted edits.
    if not isinstance(payload, dict) or set(payload) - {"title", "order", "headings", "links", "summaries", "comparisons", "deduplicate"}:
        raise ValueError("synthesis must return edits, not a replacement booklet")
    payload = {key: value for key, value in payload.items() if value is not None}
    if set(payload) - allowed:
        raise ValueError("edit is not supported at this stage")
    by_id = {entry.id: entry for entry in entries}

    def target(value: object) -> str:
        if not isinstance(value, str) or value not in by_id:
            raise ValueError("unknown synthesis section ID")
        return value

    order = list(by_id)
    if "order" in payload:
        raw = payload["order"]
        if not isinstance(raw, list) or len(raw) != len(by_id):
            raise ValueError("order must be a complete permutation")
        order = [target(value) for value in raw]
        if (len(set(order)) != len(by_id) or not _order_ok([by_id[key] for key in order], plan)
                or not _preserves_topic_sequence(entries, [by_id[key] for key in order])):
            raise ValueError("invalid order or plan dependency")
    originals = dict(by_id)
    title = basis.title
    if "title" in payload:
        titles = [basis.title, n._literal_continuation_title(basis.title)] if basis.title else [entry.section.heading for entry in entries]
        title = _label(payload["title"], titles)
    seen: set[str] = set()
    for row in _list(payload, "headings"):
        if not isinstance(row, dict) or set(row) != {"id", "heading"}:
            raise ValueError("invalid heading edit")
        key = target(row["id"])
        entry = by_id[key]
        if key in seen:
            raise ValueError("duplicate heading edit")
        seen.add(key)
        label = _label(row["heading"], [entry.section.heading, n._literal_continuation_title(entry.section.heading)])
        by_id[key] = replace(entry, section=replace(entry.section, heading=label))

    for row in _list(payload, "deduplicate"):
        if not isinstance(row, dict) or set(row) != {"id", "field"} or row["field"] not in ("paragraphs", "bullets"):
            raise ValueError("exact repetition edit only supports paragraphs and bullets")
        key = target(row["id"])
        entry = by_id[key]
        section = entry.section
        edited = (replace(section, paragraphs=tuple(dict.fromkeys(section.paragraphs)))
                  if row["field"] == "paragraphs" else replace(section, bullets=tuple(dict.fromkeys(section.bullets))))
        by_id[key] = replace(entry, section=edited)

    def quote(key: str, value: object) -> str:
        section = originals[key].section
        permitted = {_chapter_highlight(section)} if chapter_highlights else _quotes(section)
        if not isinstance(value, str) or not 1 <= len(value) <= _MAX_QUOTE_CHARS or value not in permitted:
            raise ValueError("summary or comparison must quote a complete input item")
        return value

    for row in _list(payload, "summaries"):
        if not isinstance(row, dict) or set(row) != {"id", "quotes"}:
            raise ValueError("invalid summary edit")
        key = target(row["id"])
        raw = row["quotes"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
            raise ValueError("invalid summary excerpt count")
        entry = by_id[key]
        points = tuple(dict.fromkeys((*entry.section.key_points, *(quote(key, text) for text in raw))))
        by_id[key] = replace(entry, section=replace(entry.section, key_points=points))

    for row in _list(payload, "links"):
        if not isinstance(row, dict) or set(row) != {"from_id", "to_id", "kind"}:
            raise ValueError("invalid structural link")
        start, end = target(row["from_id"]), target(row["to_id"])
        kind = row["kind"]
        if start == end or kind not in ("related", "transition", "potential_conflict"):
            raise ValueError("invalid link relation")
        if kind == "potential_conflict" and (min(start, end), max(start, end)) not in conflicts:
            raise ValueError("no input conflict for link")
        entry = by_id[start]
        by_id[start] = replace(entry, links=tuple(dict.fromkeys((*entry.links, (end, kind)))))

    for row in _list(payload, "comparisons"):
        if not isinstance(row, dict) or set(row) != {"id", "rows"}:
            raise ValueError("invalid comparison edit")
        key = target(row["id"])
        raw = row["rows"]
        if not isinstance(raw, list) or not 2 <= len(raw) <= 6:
            raise ValueError("invalid comparison row count")
        entry = by_id[key]
        cells: list[list[str]] = []
        offsets = list(entry.section.source_block_ids)
        locators = list(entry.section.source_locators)
        seen = set()
        for cell in raw:
            if not isinstance(cell, dict) or set(cell) != {"id", "quote"}:
                raise ValueError("invalid comparison row")
            linked = target(cell["id"])
            if linked in seen:
                raise ValueError("comparison repeats a section")
            seen.add(linked)
            peer = originals[linked].section
            # A cross-section quote must not lose its owning source refs.
            offsets.extend(peer.source_block_ids)
            locators.extend(peer.source_locators)
            cells.append([by_id[linked].section.heading, quote(linked, cell["quote"])])
        if key not in seen or len(set(locators)) > n.MAX_SOURCE_LOCATORS_PER_SECTION:
            raise ValueError("comparison provenance exceeds the section contract")
        table = n.NoteTable(["موضوع", "گزارهٔ موجود"], cells, "مقایسهٔ گزاره‌های جزوه")
        tables = entry.section.all_tables
        if table not in tables:
            tables = (*tables, table)
        primary, extra = tables[0], tables[1:]
        by_id[key] = replace(entry, section=replace(entry.section, table=primary, tables=extra,
            source_block_ids=tuple(dict.fromkeys(offsets)), source_locators=tuple(dict.fromkeys(locators))))
    return [by_id[key] for key in order], title


def _split_point(entries: list[_Entry]) -> int:
    middle = len(entries) // 2
    preferred = [index for index in range(1, len(entries))
                 if (entries[index - 1].section.topic_id or entries[index - 1].chapter)
                 != (entries[index].section.topic_id or entries[index].chapter)]
    return min(preferred, key=lambda index: abs(middle - index), default=middle)


class _Editor:
    def __init__(self, basis: n.StructuredNotes, plan: ContentPlan, router: n.SectionWriter, budget: int) -> None:
        self.basis = basis
        self.plan = plan
        self.router = router
        self.budget = budget
        self.schema = synthesis_schema() if router.settings.note_structured_output else None
        self.topics = {topic.id: (index, topic) for index, topic in enumerate(plan.topics)}
        self.attempts = 0
        self.rejected = False

    def message(self, entries: list[_Entry], level: str, *, highlights: bool = False) -> str:
        outline = level in {"chapter", "final"}
        sections = [{"id": item.id, "topic_id": item.section.topic_id, "chapter": item.chapter,
                     "parent_id": item.section.parent_topic_id,
                     **({"heading": item.section.heading} if outline else n.section_payload(item.section))}
                    for item in entries]
        payload: dict = {"level": level, "sections": sections, "potential_conflicts": sorted(_pair_conflicts(self.basis, entries))}
        if highlights:
            for row, entry in zip(sections, entries, strict=True):
                row["highlight"] = _chapter_highlight(entry.section)
        ids = dict.fromkeys(item.section.topic_id for item in entries if item.section.topic_id)
        payload["plan"] = [{"id": key, "position": self.topics[key][0],
                            "depends_on": self.topics[key][1].depends_on} for key in ids]
        if level == "whole":
            payload["metadata"] = n.compact_notes_payload(replace(self.basis, sections=()))
            del payload["metadata"]["sections"]
        elif level == "final":
            payload["title"] = self.basis.title
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    async def request(self, entries: list[_Entry], level: str, *, include_highlights: bool = True) -> tuple[list[_Entry], str] | None:
        highlights = level == "chapter" and include_highlights
        message = self.message(entries, level, highlights=highlights)
        if highlights and len(message) > self.budget:
            message = self.message(entries, level)
            highlights = False  # explicit outline without optional highlight menu
        # Reserve at least one chapter attempt and one global attempt even
        # when the topic groups consume all of their allotted calls.
        reserved = 0 if level == "final" else 1 if level == "chapter" else 2
        cap = MAX_SYNTHESIS_REQUESTS - reserved
        if len(message) > self.budget or self.attempts >= cap:
            self.rejected = True
            logger.info("synthesis kept input level=%s reason=%s sections=%d", level,
                        "capacity" if len(message) > self.budget else "request_cap", len(entries))
            return None
        self.attempts += 1
        try:
            async with self.router.semaphore:
                record_call("BOOKLET_COMPILE", system=synthesis_system_prompt(), user=message,
                            chars_per_token=self.router.settings.llm_token_estimation_chars_per_token)
                reply = await n.complete_json(
                    self.router.session, self.router.settings, system=synthesis_system_prompt(), user=message,
                    schema=self.schema, task="BOOKLET_COMPILE", adaptive_capacity=True, strict_json=True,
                    prefer_provider=self.router.settings.note_compile_provider, sampling_temperature=0.1,
                )
            return _apply(reply, self.basis, entries, self.plan, level=level,
                          chapter_highlights=highlights, conflicts=_pair_conflicts(self.basis, entries))
        except (LLMContextOverflowError, LLMOutputBudgetError):
            raise
        except (LLMError, ValueError) as exc:
            self.rejected = True
            logger.info("synthesis kept input level=%s reason=%s sections=%d", level, type(exc).__name__, len(entries))
            return None

    def groups(self, entries: list[_Entry]) -> list[list[_Entry]]:
        """Pack whole sections, treating a topic boundary as the preferred cut."""
        groups: list[list[_Entry]] = []
        pending: list[_Entry] = []
        for _, topic in groupby(entries, key=lambda entry: entry.section.topic_id or entry.chapter):
            batch = list(topic)
            if len(self.message(pending + batch, "topic_group")) <= self.budget:
                pending.extend(batch)
                continue
            if pending:
                groups.append(pending)
                pending = []
            for entry in batch:
                if pending and len(self.message(pending + [entry], "topic_group")) > self.budget:
                    groups.append(pending)
                    pending = []
                pending.append(entry)
        if pending:
            groups.append(pending)
        return groups

    async def group(self, entries: list[_Entry], depth: int = 0) -> list[_Entry]:
        try:
            edited = await self.request(entries, "topic_group")
            return edited[0] if edited is not None else entries
        except (LLMContextOverflowError, LLMOutputBudgetError) as exc:
            logger.info("synthesis group overflow sections=%d reason=%s", len(entries), type(exc).__name__)
            if depth >= MAX_GROUP_SPLIT_DEPTH or len(entries) < 2:
                self.rejected = True
                return entries
            cut = _split_point(entries)
            left = await self.group(entries[:cut], depth + 1)
            return left + await self.group(entries[cut:], depth + 1)


def _finalize_global_learning_materials(candidate: n.StructuredNotes) -> n.StructuredNotes:
    """Ensure global learning materials are deduplicated without violating preservation."""
    objectives = tuple(dict.fromkeys(item.strip() for item in candidate.learning_objectives if item.strip()))
    final_review = tuple(dict.fromkeys(item.strip() for item in candidate.final_review if item.strip()))
    review_questions = tuple(dict.fromkeys(item.strip() for item in candidate.review_questions if item.strip()))
    return replace(
        candidate,
        learning_objectives=objectives,
        final_review=final_review,
        review_questions=review_questions,
    )


def _candidate(basis: n.StructuredNotes, entries: list[_Entry], title: str, recovered: bool,
               document: NormalizedDocument, router: n.SectionWriter) -> n.StructuredNotes:
    headings = {entry.id: entry.section.heading for entry in entries}
    sections = []
    for entry in entries:
        callouts = list(entry.section.callouts)
        for target, kind in entry.links:
            label = headings.get(target, "").strip()
            if not label:
                continue
            if kind == "potential_conflict":
                text = f"ناهمخوانی احتمالی با «{label}»؛ هر دو مقدار در جزوه حفظ شده‌اند."
            elif kind == "transition":
                text = f"ادامهٔ مطالعه: «{label}»."
            else:
                text = f"برای ارتباط موضوعی، «{label}» را ببینید."
            callouts.append(n.NoteCallout("یادآوری", text))
        sections.append(replace(entry.section, callouts=tuple(dict.fromkeys(callouts))))
    candidate = _finalize_global_learning_materials(
        replace(basis, sections=tuple(sections), title=title,
                quality_state=n.QUALITY_RECOVERED if recovered else basis.quality_state)
    )
    accepted = n.compile_accepts(candidate, basis) and not n.booklet_output_violations(candidate)
    if accepted and router.settings.note_strict_grounding:
        if candidate.to_payload() != basis.to_payload():
            # Non-mutating check: fall back to the prior approved artifact if
            # the additions would be removed or fail the strict evidence gate.
            gated, report = n.gate_notes(candidate, document.text, strict=True, mode=router.mode)
            accepted = gated == candidate and n.acceptance_failure(report) is None
    if not accepted:
        logger.info("synthesis rejected by the preservation/evidence gate; kept deterministic baseline")
        return replace(basis, quality_state=n.QUALITY_RECOVERED)
    return candidate


async def synthesize_booklet(
    sections: n.StructuredNotes | Sequence[n.NoteSection], plan: ContentPlan,
    document: NormalizedDocument, router: n.SectionWriter,
) -> n.StructuredNotes:
    """Edit references to existing content; no response can replace factual bodies."""
    basis = sections if isinstance(sections, n.StructuredNotes) else n.StructuredNotes(sections=tuple(sections))
    skip = n.compile_skip_reason(basis, router.settings, mode=router.mode, presentation=False)
    if skip is not None:
        if skip == "no-budget":
            logger.info("synthesis kept baseline reason=no-route-budget")
        return basis
    plan.validate(document)
    entries = _entries(basis, plan)
    if len(entries) != len(basis.sections) or not entries:
        logger.info("synthesis kept baseline reason=unresolved-section")
        return basis  # never lose an unresolved original section
    budget = n.effective_compile_max_chars(router.settings, mode=router.mode)
    editor = _Editor(basis, plan, router, budget)
    if len(editor.message(entries, "whole")) <= budget:
        try:
            edited = await editor.request(entries, "whole")
        except (LLMContextOverflowError, LLMOutputBudgetError) as exc:
            logger.info("synthesis whole overflow reason=%s; falling back to whole-section groups", type(exc).__name__)
        else:
            if edited is None:
                return replace(basis, quality_state=n.QUALITY_RECOVERED)
            return _candidate(basis, *edited, False, document, router)
        cut = _split_point(entries) if len(entries) > 1 else 0
        groups = [entries[:cut], entries[cut:]] if cut else []
    else:
        groups = editor.groups(entries)
    original_order = [entry.id for entry in entries]
    if not groups:
        # A single overflowed body cannot be cut in the editing stage. A
        # final outline may still rename it, but the original body stays whole.
        editor.rejected = True
    else:
        edited_groups = await asyncio.gather(*(editor.group(group) for group in groups))
        entries = [entry for group in edited_groups for entry in group]
        if not _order_ok(entries, plan):
            by_id = {entry.id: entry for entry in entries}
            entries = [by_id[key] for key in original_order]

    async def outline(group: list[_Entry], level: str) -> tuple[list[_Entry], str]:
        try:
            return await editor.request(group, level) or (group, basis.title)
        except (LLMContextOverflowError, LLMOutputBudgetError) as exc:
            if level == "chapter" and len(editor.message(group, level, highlights=True)) <= editor.budget:
                # Retry on the complete manifest without the optional recap menu.
                try:
                    return await editor.request(group, level, include_highlights=False) or (group, basis.title)
                except (LLMContextOverflowError, LLMOutputBudgetError):
                    pass
            editor.rejected = True
            logger.info("synthesis kept outline level=%s reason=%s sections=%d", level, type(exc).__name__, len(group))
            return group, basis.title

    # Chapter/global levels see an explicit, complete ID manifest rather than
    # a silently shortened version of an over-budget booklet body.
    chapters: list[_Entry] = []
    for _, iterator in groupby(entries, key=lambda item: item.chapter):
        chapter, _ = await outline(list(iterator), "chapter")
        chapters.extend(chapter)
    entries, title = await outline(chapters, "final")
    return _candidate(basis, entries, title, editor.rejected, document, router)
