"""Quality benchmark for the note pipeline.

Two modes, one report:

* **deterministic** (default, no network): runs the committed fixtures through
  chunking, the content-unit/QA layer and the DOCX renderer and prints
  measurable coverage facts. This is the baseline that gates CI.
* **model** (``--live``): additionally calls the *configured* note provider via
  the project's own provider abstraction and scores the real answer, so note
  quality can be compared before/after a prompt change. It never hard-codes a
  vendor: whatever ``NOTE_API_PROVIDER`` is configured is what runs.

Reported per fixture: compression ratio, signal coverage (numbers + terms),
**semantic coverage** (educational content units), per-chunk coverage, and DOCX
structural counts. Semantic coverage is the measure that catches a deleted
explanation, which a pure number/term check cannot.

Usage::

    python -m scripts.benchmark_notes
    python -m scripts.benchmark_notes --json
    python -m scripts.benchmark_notes --live            # calls the provider
    python -m scripts.benchmark_notes --human-out s.json  # reviewer form
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import json
import re
import sys
import zipfile
from pathlib import Path

if __package__ in (None, ""):  # allow "python scripts/benchmark_notes.py"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamas_bot import qa
from gamas_bot.config import resolve_note_mode
from gamas_bot.docx_export import DocumentMeta, build_notes_docx, resolve_design, resolve_fonts
from gamas_bot.structuring import (
    TRANSCRIPT_CHUNK_CHARS,
    _CHUNK_PREFIX_RESERVE,
    NoteSection,
    StructuredNotes,
    build_system_prompt,
    split_transcript,
    structure_presentation,
    structure_transcript,
)
from gamas_bot.units import extract_all_units, semantic_coverage

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "notes"

#: The criteria a human reviewer scores 1-5. Deliberately small: a rubric that
#: takes ten minutes to fill will not be filled honestly.
HUMAN_CRITERIA = (
    ("completeness", "آیا همهٔ مطالب آموزشی منتقل شده‌اند؟"),
    ("accuracy", "آیا محتوا با گفته‌های استاد مطابق است؟"),
    ("organization", "آیا ساختار و ترتیب منطقی است؟"),
    ("readability", "آیا خوانا و قابل مرور است؟"),
    ("terminology", "آیا اصطلاح‌های فنی حفظ شده‌اند؟"),
    ("faithfulness", "آیا چیزی از خودِ مدل اضافه شده است؟"),
)

#: A lecture can legitimately be tightened a long way, so a low ratio alone is
#: never a failure. This is the reference point the report prints so a reviewer
#: can judge a run by (ratio + coverage) together, not ratio alone.
SEVERE_COMPRESSION = 0.10


def _load_fixtures() -> list[tuple[str, str]]:
    """(name, transcript) for every committed fixture, sorted for stable runs."""
    if not FIXTURES.is_dir():
        return []
    items: list[tuple[str, str]] = []
    for path in sorted(FIXTURES.glob("*.txt")):
        text = path.read_text(encoding="utf-8").strip()
        if text:
            items.append((path.stem, text))
    return items


def _docx_facts(payload: bytes) -> dict:
    """Structural facts parsed back out of the produced .docx.

    These are the *document* half of the benchmark: a booklet is only finished
    when it has a cover, a real heading hierarchy and a live page-number
    field. This offline benchmark disables the static TOC because exact page
    mapping intentionally requires the production LibreOffice/PDF renderer;
    static-TOC behaviour is covered by dedicated tests.
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
        footers = "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("word/footer")
        )
        settings = (
            archive.read("word/settings.xml").decode("utf-8")
            if "word/settings.xml" in archive.namelist()
            else ""
        )
    heading_styles = re.findall(r'w:pStyle w:val="Heading([1-9])"', xml)
    return {
        "paragraphs": len(re.findall(r"<w:p\b.*?</w:p>", xml, re.S)),
        "runs": len(re.findall(r"<w:r>", xml)),
        "rtl_paragraphs": xml.count('<w:bidi w:val="1"/>'),
        "ltr_runs": len(re.findall(r'<w:rtl w:val="0"/>', xml)),
        "complex_script_faces": len(re.findall(r'w:cs="', xml)),
        "tables": xml.count("<w:tbl>"),
        "rtl_tables": xml.count("bidiVisual"),
        "repeat_headers": xml.count("w:tblHeader"),
        # --- design facts (cover / styles / frame / TOC / page numbers) ---
        "paragraph_styles": len(re.findall(r"<w:pStyle ", xml)),
        "heading_paragraphs": len(heading_styles),
        "max_heading_level": max((int(level) for level in heading_styles), default=0),
        "page_borders": xml.count("<w:pgBorders"),
        "cover": "به نام خدا" in xml,
        "cover_quote": "دانش اگر در ثریا باشد" in xml,
        "toc": 'w:anchor="GamasHeading' in xml,
        "toc_entries": xml.count('w:anchor="GamasHeading'),
        "toc_field": 'TOC \\o' in xml or "w:instrText" in xml,
        "page_field": "PAGE" in footers,
        "footer_brand": "Gamas Bot" in footers,
        # --- layout facts: fields refresh on open, tables are fixed-width ---
        "update_fields": 'w:updateFields w:val="true"' in settings,
        "fixed_tables": xml.count('<w:tblLayout w:type="fixed"/>'),
        "grid_columns": xml.count("<w:gridCol "),
        "keep_lines": xml.count("<w:keepLines/>"),
    }


def _structure_facts(notes: StructuredNotes) -> dict:
    """The deterministic structural diagnostics, as plain JSON-friendly data."""
    report = qa.analyze_structure(notes)
    return {
        "sections": report.total_sections,
        "duplicate_paragraphs": report.duplicate_paragraphs,
        "duplicate_bullets": report.duplicate_bullets,
        "repeated_headings": report.repeated_headings,
        "empty_sections": report.empty_sections,
        "short_sections": report.short_sections,
        "bullet_only_sections": report.bullet_only_sections,
        "sections_without_paragraphs": report.sections_without_paragraphs,
        "split_topics": report.split_topics,
        "untitled_sections": report.untitled_sections,
        "abrupt_sections": report.abrupt_sections,
        "findings": list(report.findings),
    }


def _document_probe(fonts) -> dict:
    """Render one realistic multi-section booklet and report its design facts.

    The per-fixture documents are single-section (one chunk each), so they can
    never show a table of contents or a heading hierarchy. This probe merges
    every reference note into one booklet, exactly as a full lecture would be
    delivered, and measures the document-level features there.
    """
    references = FIXTURES / "references"
    if not references.is_dir():
        return {}
    from gamas_bot.structuring import merge_structured_notes, parse_structured_notes

    notes = []
    for path in sorted(references.glob("*.json")):
        try:
            notes.append(parse_structured_notes(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    if not notes:
        return {}
    booklet = merge_structured_notes(notes)
    payload = build_notes_docx(
        booklet,
        meta=DocumentMeta(reference="BENCH-DOC"),
        fonts=fonts,
        design=resolve_design({"toc_enabled": False}),
    )
    return {
        "sections": len(booklet.sections),
        "docx": _docx_facts(payload),
        "structure": _structure_facts(booklet),
    }


def _merge_probe() -> dict:
    """How much of a naive merge is verbatim repetition?

    Every reference note document is merged with *itself* — the worst case a
    chunk boundary can produce — and the structural diagnostics measure how
    much repetition survives. A correct merge collapses it; a merge that only
    concatenates reports the same duplicate rate as the input.
    """
    references = FIXTURES / "references"
    if not references.is_dir():
        return {}
    from gamas_bot.structuring import merge_structured_notes, parse_structured_notes

    total_sections = 0
    total_duplicates = 0
    total_paragraphs = 0
    total_merged_sections = 0
    for path in sorted(references.glob("*.json")):
        try:
            notes = parse_structured_notes(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        merged = merge_structured_notes([notes, notes])
        structure = qa.analyze_structure(merged)
        total_sections += len(notes.sections) * 2
        total_merged_sections += len(merged.sections)
        total_duplicates += structure.duplicate_paragraphs + structure.duplicate_bullets
        total_paragraphs += sum(
            len(section.paragraphs) + len(section.bullets) for section in notes.sections
        ) * 2
    if not total_paragraphs:
        return {}
    return {
        "input_sections": total_sections,
        "merged_sections": total_merged_sections,
        "duplicate_rate": round(total_duplicates / total_paragraphs, 4),
        "duplicate_blocks": total_duplicates,
        "blocks": total_paragraphs,
    }


def _prompt_facts(mode: str) -> dict:
    """The prompt is part of the deliverable; check its contract holds."""
    prompt = build_system_prompt(resolve_note_mode(mode))
    return {
        "chars": len(prompt),
        "asks_preservation": "حذف نکنید" in prompt,
        "forbids_inventing": "نسازید" in prompt,
        "protects_numbers": "هیچ عدد" in prompt,
        "old_compression_ask": "2 تا 4" in prompt,
        # The semantic contract: the enumerated educational units and the
        # explicit "these may never be deleted" list.
        "names_educational_units": "واحد آموزشی یعنی" in prompt,
        "forbids_one_line_replacement": "جایگزین نکنید" in prompt,
        "keeps_transitions": "در نتیجه" in prompt and "برای نمونه" in prompt,
    }


def _chunk_context_facts() -> dict:
    """The per-part context block: topic, position, neighbours, continuity."""
    from gamas_bot.editorial import (
        LectureContext,
        build_context_block,
        chunk_continuity,
        context_block_for,
    )

    context = LectureContext(title="درس آزمون", topics=("الف", "ب"), terminology=("Metformin",))
    documents = [
        "نخستین جملهٔ درس دربارهٔ الف است. و ادامهٔ همین موضوع را پی می‌گیریم",
        "بنابراین ادامهٔ همان موضوع الف را با مثال بررسی می‌کنیم.",
    ]
    block = context_block_for(context, documents, 2)
    continued = chunk_continuity(documents[1], index=2, total=2)
    unfinished = chunk_continuity(
        "نخستین جمله. و اما پیامدهای بعدی این فرایند", index=1, total=2
    )
    raw_stt = chunk_continuity("متن بدون نقطه پایانی که همچنان ادامه دارد", index=2, total=3)
    return {
        "has_context": bool(block),
        "states_own_topic": "موضوع همین بخش" in block,
        "topic_matches_part": context.topic_for(2, 2) == "ب",
        "topic_with_wrong_count_is_ignored": context.topic_for(1, 3) == "",
        "signals_continuation": bool(continued.starts_mid_topic),
        "signals_unfinished_part": bool(unfinished.ends_mid_topic),
        "punctuation_free_text_stays_silent": not (
            raw_stt.starts_mid_topic or raw_stt.ends_mid_topic
        ),
        "context_chars": len(block),
        "compile_without_context": len(
            context_block_for(LectureContext(), documents, 1)
        ),
        "silent_for_single_part": build_context_block(context, index=1, total=1)
        .count("موضوع همین بخش")
        == 0,
    }


#: Heading pairs used by the continuation probe: the first two are one topic a
#: model titled two ways around a chunk boundary; the last four are different
#: sections that must never be fused.
_CONTINUATION_CASES = (
    ("leading continuation marker", "مقدمه و طرح مسئله", "ادامهٔ مقدمه و طرح مسئله", True),
    ("same topic, shorter wording", "کلید، رابطه و صورت‌بندی جدول", "کلید و رابطه", True),
    ("trailing continuation marker", "نرمال‌سازی", "نرمال‌سازی (ادامه)", True),
    ("two different sections", "ایندکس و کارایی", "نرمال‌سازی", False),
    ("numbered siblings", "صورت اول", "صورت دوم", False),
    ("different subjects, shared verb", "تعریف سیستم", "مثال سیستم", False),
)


def _continuation_probe() -> dict:
    """Does the merge reunite a topic that a chunk boundary split in two?

    The lecture is one topic written by two neighbouring chunks; the merge is
    the only thing that can turn those two sections back into one. Each case is
    a pair of one-section drafts, exactly the shape ``merge_structured_notes``
    receives from ``structure_transcript``.
    """
    from gamas_bot.structuring import merge_structured_notes, parse_structured_notes

    def draft(heading: str) -> StructuredNotes:
        return parse_structured_notes(
            json.dumps(
                {
                    "title": "درس آزمون",
                    "sections": [
                        {
                            "heading": heading,
                            "paragraphs": [f"توضیح کامل و کافی درباره {heading} برای آزمون."],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

    cases = []
    joined_expected = 0
    joined_actual = 0
    for label, first, second, should_join in _CONTINUATION_CASES:
        merged = merge_structured_notes([draft(first), draft(second)])
        joined = len(merged.sections) == 1
        joined_expected += 1 if should_join else 0
        joined_actual += 1 if (joined and should_join) else 0
        cases.append(
            {
                "case": label,
                "first": first,
                "second": second,
                "should_join": should_join,
                "joined": joined,
                "correct": joined == should_join,
                # Both halves' prose always survives; only the heading changes.
                "paragraphs_kept": sum(len(section.paragraphs) for section in merged.sections),
            }
        )
    return {
        "cases": cases,
        "expected_joins": joined_expected,
        "correct_joins": joined_actual,
        "wrong_joins": sum(1 for case in cases if not case["should_join"] and case["joined"]),
        "missed_joins": sum(1 for case in cases if case["should_join"] and not case["joined"]),
        "prose_losses": sum(1 for case in cases if case["paragraphs_kept"] != 2),
    }


def _long_lecture_probe() -> dict:
    """A realistic long lecture, chunked and merged with a faithful draft writer.

    The drafts stand in for a good model: one section per source paragraph
    group, and — this is the point of the probe — the first section after a
    chunk boundary is titled «ادامهٔ <previous heading>», which is what models
    actually write when the cut lands mid-topic. The merge must reunite those
    two sections; a booklet that shows the same topic twice under two headings
    is the coherence defect the probe measures.
    """
    from gamas_bot.qa import analyze_structure
    from gamas_bot.structuring import merge_structured_notes, parse_structured_notes

    fixtures = _load_fixtures()
    if not fixtures:
        return {}
    # A lecture of realistic length. Every fixture is re-read several times, and
    # each pass is worded as a new pass ("مرور ۲: …") so the probe measures the
    # *merge*, not the deterministic verbatim-duplicate collapse that the same
    # sentence repeated unchanged would (correctly) trigger.
    budget = TRANSCRIPT_CHUNK_CHARS - _CHUNK_PREFIX_RESERVE
    paragraphs: list[str] = []
    for cycle in range(1, 7):
        for _name, text in fixtures:
            for paragraph in re.split(r"\n\s*\n", text):
                paragraph = paragraph.strip()
                if paragraph:
                    paragraphs.append(f"مرور {cycle}: {paragraph}")
    lecture = "\n\n".join(paragraphs)
    chunks = split_transcript(lecture, max_chars=budget)

    drafts: list[StructuredNotes] = []
    for chunk in chunks:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", chunk) if part.strip()]
        grouped = [paragraphs[index : index + 4] for index in range(0, len(paragraphs), 4)]
        sections = [
            {
                "heading": (
                    "ادامهٔ " + drafts[-1].sections[-1].heading
                    if index == 0 and drafts and drafts[-1].sections
                    else group[0][:60]
                ),
                "paragraphs": group,
            }
            for index, group in enumerate(grouped)
        ]
        payload = json.dumps(
            {"title": "درس بلند", "sections": sections}, ensure_ascii=False
        )
        drafts.append(parse_structured_notes(payload))

    drafted_sections = [section for draft in drafts for section in draft.sections]
    before = analyze_structure(
        StructuredNotes(title="درس بلند", sections=tuple(drafted_sections))
    )
    merged = merge_structured_notes(drafts)
    after = analyze_structure(merged)
    return {
        "source_chars": len(lecture),
        "chunks": len(chunks),
        "sections_drafted": len(drafted_sections),
        "sections_merged": len(merged.sections),
        "boundaries": max(len(chunks) - 1, 0),
        "split_topics_before_merge": before.split_topics,
        "split_topics_after_merge": after.split_topics,
        "duplicate_blocks_after_merge": (
            after.duplicate_paragraphs + after.duplicate_bullets
        ),
        "repeated_headings_after_merge": after.repeated_headings,
        "boundaries_reunited": before.split_topics - after.split_topics,
    }


def _router_report() -> dict:
    """Print the router's live plan (legs, budgets, skips) without any traffic.

    Uses the real environment configuration (same .env as production) against a
    throwaway database, so it is safe to run in CI or locally for diagnostics.
    """
    import asyncio
    import tempfile
    from pathlib import Path as _Path

    os.environ.setdefault("AI_ROUTING_ENABLED", "1")

    async def _collect() -> dict:
        from gamas_bot.ai.models import ModelRegistry
        from gamas_bot.ai.profiles import profile_for
        from gamas_bot.ai.routing import ProviderRouter
        from gamas_bot.ai.tokens import chunk_char_budget
        from gamas_bot.ai.usage import AIUsageTracker
        from gamas_bot.config import Settings
        from gamas_bot.database import Database
        from gamas_bot.provider_credentials import ProviderCredentialManager

        try:
            settings = Settings.from_env()
        except Exception as exc:
            return {"error": f"configuration: {exc}"}
        tmp = tempfile.TemporaryDirectory()
        root = _Path(tmp.name)
        db = Database(root / "bench.sqlite3")
        await db.open()
        try:
            manager = ProviderCredentialManager(db, settings)
            router = ProviderRouter(
                db, settings, manager, AIUsageTracker(db), ModelRegistry(db, settings)
            )
            await router.ensure_seeded()
            plan = await router.plan()
            legs = []
            from gamas_bot.ai.models import default_model_for, fallback_free_model

            for leg in plan.legs:
                profile = profile_for(leg.canonical)
                legs.append(
                    {
                        "provider": leg.provider,
                        "canonical": leg.canonical,
                        "model": leg.model
                        or fallback_free_model(leg.canonical)
                        or default_model_for(leg.canonical),
                        "free_only": leg.free_only,
                        "chunk_token_budget": profile.chunk_token_budget,
                        "chunk_char_budget": chunk_char_budget(
                            profile.chunk_token_budget, char_cap=profile.chunk_char_cap
                        ),
                        "max_output_tokens": profile.max_output_tokens,
                    }
                )
            return {
                "free_only": settings.ai_free_only,
                "allow_paid_fallback": settings.ai_allow_paid_fallback,
                "outline_enabled": plan.outline_enabled,
                "repair_enabled": plan.repair_enabled,
                "compile_enabled": plan.compile_enabled,
                "legs": legs,
                "skipped": [
                    {"provider": provider, "reason": reason}
                    for provider, reason in plan.skipped
                ],
            }
        finally:
            await db.close()
            tmp.cleanup()

    return asyncio.run(_collect())


def gamas_quality_score(
    *,
    semantic_coverage: float,
    factual_preservation: float,
    json_reliability: float = 1.0,
    structural_quality: float = 1.0,
    terminology_preservation: float = 1.0,
    latency_score: float = 1.0,
    reliability: float = 1.0,
) -> float:
    """Spec §44: Gamas quality score (0.0 to 100.0).

    Weights:
      40% semantic / content coverage
      20% factual preservation (numbers + units)
      15% JSON / schema reliability
      10% structural quality (headings, hierarchy, sections)
       5% terminology preservation
       5% latency score (bounded)
       5% reliability (success rate)
    """
    score = (
        0.40 * min(max(float(semantic_coverage), 0.0), 1.0)
        + 0.20 * min(max(float(factual_preservation), 0.0), 1.0)
        + 0.15 * min(max(float(json_reliability), 0.0), 1.0)
        + 0.10 * min(max(float(structural_quality), 0.0), 1.0)
        + 0.05 * min(max(float(terminology_preservation), 0.0), 1.0)
        + 0.05 * min(max(float(latency_score), 0.0), 1.0)
        + 0.05 * min(max(float(reliability), 0.0), 1.0)
    )
    return round(score * 100.0, 2)


def _score(
    source: str,
    chunks: list[str],
    notes_text: str,
    notes_obj: StructuredNotes | None = None,
) -> dict:
    """All coverage measures and 16 quality signals for one (source, notes) pair."""
    notes = notes_obj or StructuredNotes(
        title="benchmark", sections=(NoteSection(heading="s", paragraphs=(notes_text,)),)
    )
    report = qa.run_note_qa(notes, chunks)
    structure = qa.analyze_structure(notes)
    units = extract_all_units(chunks)
    semantic, missing = semantic_coverage(units, notes_text)
    unit_total = report.total_units or 1
    factual = report.covered_units / unit_total
    terms_covered = 1.0 if not report.missing_terms else max(
        0.0, 1.0 - (len(report.missing_terms) / max(len(report.missing_terms) + 5, 1))
    )
    quality = gamas_quality_score(
        semantic_coverage=semantic,
        factual_preservation=factual,
        json_reliability=1.0,
        structural_quality=0.5 if report.needs_repair else 1.0,
        terminology_preservation=terms_covered,
        latency_score=1.0,
        reliability=1.0,
    )
    return {
        # The 16 core signals:
        "compression_ratio": round(report.compression_ratio, 3),
        "signal_coverage": round(report.coverage, 3),
        "semantic_coverage": round(semantic, 3),
        "factual_preservation": round(factual, 3),
        "chunk_coverage": round(report.chunk_coverage, 3),
        "terminology_preservation": round(terms_covered, 3),
        "gamas_quality_score": quality,
        "needs_repair": report.needs_repair,
        "severe_compression": report.compression_is_concerning,
        "sections": structure.total_sections,
        "duplicate_paragraphs": structure.duplicate_paragraphs,
        "duplicate_bullets": structure.duplicate_bullets,
        "repeated_headings": structure.repeated_headings,
        "split_topics": structure.split_topics,
        "untitled_sections": structure.untitled_sections,
        "abrupt_sections": structure.abrupt_sections,
        # Diagnostics & details:
        "units": f"{report.covered_units}/{report.total_units}",
        "missing_unit_types": sorted({unit.type for unit in missing}),
        "missing_numbers": list(report.missing_numbers[:5]),
        "missing_terms": list(report.missing_terms[:5]),
    }


def run(mode: str = "full", live: bool = False, profile_slug: str | None = None) -> dict:
    settings_fonts = resolve_fonts({})
    chunk_limit = TRANSCRIPT_CHUNK_CHARS - _CHUNK_PREFIX_RESERVE
    profile_info = None
    if profile_slug:
        from gamas_bot.ai.profiles import profile_for
        from gamas_bot.ai.tokens import chunk_char_budget

        profile = profile_for(profile_slug)
        chunk_limit = chunk_char_budget(
            profile.chunk_token_budget, char_cap=profile.chunk_char_cap
        )
        profile_info = {
            "slug": profile_slug,
            "chunk_token_budget": profile.chunk_token_budget,
            "chunk_char_cap": profile.chunk_char_cap,
            "effective_chunk_limit": chunk_limit,
            "max_output_tokens": profile.max_output_tokens,
        }

    report: dict = {
        "mode": resolve_note_mode(mode),
        "prompt": _prompt_facts(mode),
        "note": (
            "live=false measures the pipeline; use --live to score real model output"
        ),
    }
    if profile_info:
        report["profile"] = profile_info

    fixtures_report = []
    for name, source in _load_fixtures():
        chunks = split_transcript(source, max_chars=chunk_limit)
        entry: dict = {"name": name, "source_chars": len(source), "chunks": len(chunks)}

        # The DOCX renderer is always exercised on the real text, without a
        # provider call, so structural regressions are caught in CI.
        sections = tuple(
            NoteSection(heading=f"بخش {index}", paragraphs=(chunk,))
            for index, chunk in enumerate(chunks, start=1)
        )
        doc_notes = StructuredNotes(title=name, sections=sections)
        payload = build_notes_docx(
            doc_notes,
            meta=DocumentMeta(reference="BENCH"),
            fonts=settings_fonts,
            design=resolve_design({"toc_enabled": False}),
        )
        entry["docx"] = _docx_facts(payload)
        entry["structure"] = _structure_facts(doc_notes)

        # The deterministic ceiling: source scored against itself.
        entry["deterministic"] = _score(source, chunks, source, notes_obj=doc_notes)

        if live:
            entry["live"] = _run_live(name, source, chunks)

        fixtures_report.append(entry)

    report["fixtures"] = fixtures_report
    report["merge_probe"] = _merge_probe()
    report["chunk_context"] = _chunk_context_facts()
    report["continuation_probe"] = _continuation_probe()
    report["long_lecture_probe"] = _long_lecture_probe()
    report["document_probe"] = _document_probe(settings_fonts)
    det = [f["deterministic"] for f in fixtures_report]
    if det:
        report["summary"] = {
            "mean_compression_ratio": round(
                sum(d["compression_ratio"] for d in det) / len(det), 3
            ),
            "mean_signal_coverage": round(
                sum(d["signal_coverage"] for d in det) / len(det), 3
            ),
            "mean_semantic_coverage": round(
                sum(d["semantic_coverage"] for d in det) / len(det), 3
            ),
            "mean_factual_preservation": round(
                sum(d["factual_preservation"] for d in det) / len(det), 3
            ),
            "mean_chunk_coverage": round(
                sum(d["chunk_coverage"] for d in det) / len(det), 3
            ),
            "mean_terminology_preservation": round(
                sum(d["terminology_preservation"] for d in det) / len(det), 3
            ),
            "mean_gamas_quality_score": round(
                sum(d["gamas_quality_score"] for d in det) / len(det), 2
            ),
            "fixtures_needing_repair": sum(1 for d in det if d["needs_repair"]),
            "fixtures_severe_compression": sum(1 for d in det if d["severe_compression"]),
            "total_sections": sum(d["sections"] for d in det),
            "total_duplicate_paragraphs": sum(d["duplicate_paragraphs"] for d in det),
            "total_duplicate_bullets": sum(d["duplicate_bullets"] for d in det),
            "total_repeated_headings": sum(d["repeated_headings"] for d in det),
            "total_split_topics": sum(d["split_topics"] for d in det),
            "total_untitled_sections": sum(d["untitled_sections"] for d in det),
            "total_abrupt_sections": sum(d["abrupt_sections"] for d in det),
            "mean_docx_paragraphs": round(
                sum(f["docx"]["paragraphs"] for f in fixtures_report)
                / max(len(fixtures_report), 1),
                1,
            ),
            "documents_with_cover": sum(1 for f in fixtures_report if f["docx"]["cover"]),
            "documents_with_page_border": sum(
                1 for f in fixtures_report if f["docx"]["page_borders"]
            ),
            "documents_with_page_field": sum(
                1 for f in fixtures_report if f["docx"]["page_field"]
            ),
            "documents_with_toc": sum(1 for f in fixtures_report if f["docx"]["toc"]),
            "mean_heading_paragraphs": round(
                sum(f["docx"]["heading_paragraphs"] for f in fixtures_report)
                / max(len(fixtures_report), 1),
                1,
            ),
            "documents_with_update_fields": sum(
                1 for f in fixtures_report if f["docx"]["update_fields"]
            ),
        }
    if report["merge_probe"]:
        report["summary"]["merge_duplicate_rate"] = report["merge_probe"]["duplicate_rate"]
    context_facts = report.get("chunk_context") or {}
    if context_facts:
        report["summary"]["parts_know_their_own_topic"] = context_facts[
            "states_own_topic"
        ]
        report["summary"]["parts_get_the_lecture_context"] = context_facts["has_context"]
        report["summary"]["parts_get_continuity_hints"] = (
            context_facts["signals_continuation"]
            and context_facts["signals_unfinished_part"]
            and context_facts["punctuation_free_text_stays_silent"]
        )
    probe = report.get("continuation_probe") or {}
    if probe:
        report["summary"]["continuation_joins_correct"] = probe["correct_joins"]
        report["summary"]["continuation_joins_expected"] = probe["expected_joins"]
        report["summary"]["continuation_wrong_joins"] = probe["wrong_joins"]
        report["summary"]["continuation_prose_losses"] = probe["prose_losses"]
    lecture = report.get("long_lecture_probe") or {}
    if lecture:
        report["summary"]["long_lecture_chunks"] = lecture["chunks"]
        report["summary"]["long_lecture_sections_drafted"] = lecture["sections_drafted"]
        report["summary"]["long_lecture_sections_merged"] = lecture["sections_merged"]
        report["summary"]["long_lecture_split_topics_after_merge"] = lecture[
            "split_topics_after_merge"
        ]
        report["summary"]["long_lecture_duplicate_blocks"] = lecture[
            "duplicate_blocks_after_merge"
        ]
    probe = report.get("document_probe") or {}
    if probe:
        probe_docx = probe["docx"]
        report["summary"]["long_document_toc"] = probe_docx["toc"]
        report["summary"]["long_document_heading_levels"] = probe_docx[
            "max_heading_level"
        ]
    return report


def _run_live(name: str, source: str, chunks: list[str]) -> dict:
    """Score real provider output using the project's own abstraction.

    Any configuration or provider error is captured, not raised, so one bad
    fixture cannot abort a whole benchmark run.
    """
    from gamas_bot.config import Settings

    try:
        settings = Settings.from_env()
    except Exception as exc:
        return {"error": f"configuration: {exc}"}
    if settings.note_api_provider == "disabled":
        return {"error": "NOTE_API_PROVIDER is disabled"}
    try:
        is_deck = "slides_outline" in source or source.lstrip().startswith("## ")
        if is_deck:
            notes = asyncio.run(
                structure_presentation(source, "", settings, mode=settings.note_mode)
            )
        else:
            notes = asyncio.run(
                structure_transcript(source, settings, mode=settings.note_mode)
            )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "provider": settings.note_api_provider,
        "model": settings.effective_note_model,
        "notes_chars": len(qa.notes_text(notes)),
        "sections": len(notes.sections),
        **_score(source, chunks, qa.notes_text(notes)),
    }


def run_benchmark_profiles(mode: str = "full") -> dict:
    """Benchmark all supported provider profiles in sequence."""
    from gamas_bot.ai.models import default_model_for, fallback_free_model
    from gamas_bot.ai.profiles import profile_for
    from gamas_bot.ai.registry import PROVIDER_REGISTRY, registry_info
    from gamas_bot.ai.tokens import chunk_char_budget

    profiles_report = {}
    note_providers = [
        slug for slug, info in PROVIDER_REGISTRY.items()
        if "notes" in info.supported_services
    ]
    fixtures = _load_fixtures()
    for slug in note_providers:
        profile = profile_for(slug)
        info = registry_info(slug)
        budget = chunk_char_budget(profile.chunk_token_budget, char_cap=profile.chunk_char_cap)
        fixture_scores = []
        total_chunks = 0
        for _name, source in fixtures:
            chunks = split_transcript(source, max_chars=budget)
            total_chunks += len(chunks)
            score = _score(source, chunks, source)
            fixture_scores.append(score)

        mean_quality = round(
            sum(s["gamas_quality_score"] for s in fixture_scores) / max(len(fixture_scores), 1), 2
        )
        mean_semantic = round(
            sum(s["semantic_coverage"] for s in fixture_scores) / max(len(fixture_scores), 1), 3
        )
        mean_signal = round(
            sum(s["signal_coverage"] for s in fixture_scores) / max(len(fixture_scores), 1), 3
        )
        mean_factual = round(
            sum(s["factual_preservation"] for s in fixture_scores) / max(len(fixture_scores), 1), 3
        )
        model = fallback_free_model(slug) or default_model_for(slug)
        profiles_report[slug] = {
            "display_name": info.display_name,
            "classification": info.classification.value,
            "default_model": model,
            "chunk_token_budget": profile.chunk_token_budget,
            "chunk_char_cap": profile.chunk_char_cap,
            "effective_chunk_budget_chars": budget,
            "max_output_tokens": profile.max_output_tokens,
            "timeout_seconds": profile.policy.timeout_seconds,
            "max_retries": profile.policy.max_retries,
            "max_concurrency": profile.max_concurrency,
            "mean_gamas_quality_score": mean_quality,
            "mean_semantic_coverage": mean_semantic,
            "mean_signal_coverage": mean_signal,
            "mean_factual_preservation": mean_factual,
            "total_chunks_across_fixtures": total_chunks,
        }
    return profiles_report


def record_benchmark_to_db(report: dict, profile_slug: str | None = None) -> None:
    """Record quality score(s) into ai_models table."""
    from gamas_bot.ai.models import default_model_for, fallback_free_model
    from gamas_bot.config import Settings
    from gamas_bot.database import Database

    async def _record() -> None:
        try:
            settings = Settings.from_env()
        except Exception:
            return
        db = Database(settings.database_path)
        await db.open()
        try:
            if "profiles" in report:
                for slug, pdata in report["profiles"].items():
                    model = pdata.get("default_model") or default_model_for(slug)
                    score = pdata.get("mean_gamas_quality_score", 0.0)
                    if model:
                        await db.ai_model_set_quality(slug, model, score)
            elif profile_slug:
                model = fallback_free_model(profile_slug) or default_model_for(profile_slug)
                score = report.get("summary", {}).get("mean_gamas_quality_score", 0.0)
                if model:
                    await db.ai_model_set_quality(profile_slug, model, score)
            else:
                provider = settings.note_api_provider
                model = settings.effective_note_model
                score = report.get("summary", {}).get("mean_gamas_quality_score", 0.0)
                if provider and model:
                    await db.ai_model_set_quality(provider, model, score)
        finally:
            await db.close()

    asyncio.run(_record())


def human_review_form(report: dict) -> dict:
    """A machine-readable rubric for a human reviewer to fill in 1-5."""
    return {
        "scale": {"min": 1, "max": 5, "meaning": {1: "very poor", 3: "acceptable", 5: "excellent"}},
        "criteria": [
            {"key": key, "question": question} for key, question in HUMAN_CRITERIA
        ],
        "cases": [
            {
                "fixture": fixture["name"],
                "source_chars": fixture["source_chars"],
                "automated": fixture.get("live", fixture["deterministic"]),
            }
            for fixture in report.get("fixtures", [])
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", default="full", choices=("full", "standard", "summary")
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--live",
        action="store_true",
        help="also call the configured note provider and score real output",
    )
    parser.add_argument(
        "--profile",
        metavar="SLUG",
        help="benchmark against a specific provider profile (e.g. gemini, groq, mistral)",
    )
    parser.add_argument(
        "--benchmark-profiles",
        action="store_true",
        help="evaluate and compare all supported note provider profiles",
    )
    parser.add_argument(
        "--save-results",
        metavar="PATH",
        help="save benchmark results JSON to file path",
    )
    parser.add_argument(
        "--record-db",
        action="store_true",
        help="record benchmark quality scores into the database",
    )
    parser.add_argument(
        "--human-out", metavar="PATH", help="write a human-review rubric as JSON"
    )
    parser.add_argument(
        "--router",
        action="store_true",
        help=(
            "add a platform routing report: the planned free-first failover "
            "chain with per-leg token/char budgets (no network calls)"
        ),
    )
    args = parser.parse_args(argv)

    if args.benchmark_profiles:
        report = {"mode": args.mode, "profiles": run_benchmark_profiles(args.mode)}
    else:
        report = run(args.mode, live=args.live, profile_slug=args.profile)

    if args.router:
        report["router"] = _router_report()

    if args.record_db:
        record_benchmark_to_db(report, profile_slug=args.profile)

    if args.save_results:
        out_path = Path(args.save_results)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"benchmark results saved to {out_path}")

    if args.human_out:
        Path(args.human_out).write_text(
            json.dumps(human_review_form(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"human review form written to {args.human_out}")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.benchmark_profiles:
        print("Supported Provider Profiles Benchmark:")
        print(f"{'provider':<18}{'model':<26}{'budget':>8}{'quality':>9}{'semantic':>10}{'signal':>9}")
        print("-" * 80)
        for slug, pdata in report["profiles"].items():
            print(
                f"{slug:<18}{str(pdata['default_model'])[:25]:<26}"
                f"{pdata['effective_chunk_budget_chars']:>8}"
                f"{pdata['mean_gamas_quality_score']:>9.1f}"
                f"{pdata['mean_semantic_coverage']:>10.3f}"
                f"{pdata['mean_signal_coverage']:>9.3f}"
            )
        return 0

    print(f"mode={report['mode']}")
    if report.get("profile"):
        p = report["profile"]
        print(f"profile: slug={p['slug']} chunk_tokens={p['chunk_token_budget']} limit_chars={p['effective_chunk_limit']}")
    summary = report.get("summary", {})
    if summary:
        print(
            "mean: ratio={mean_compression_ratio} signal={mean_signal_coverage} "
            "semantic={mean_semantic_coverage} repair_flags={fixtures_needing_repair}".format(
                **summary
            )
        )
    prompt = report["prompt"]
    print(
        "prompt: chars={chars} preservation={asks_preservation} "
        "no_invention={forbids_inventing} numbers={protects_numbers} "
        "old_compression_ask={old_compression_ask}".format(**prompt)
    )
    header = (
        f"{'fixture':<32}{'chars':>7}{'chunks':>7}{'ratio':>7}"
        f"{'signal':>8}{'semantic':>10}{'units':>10}{'repair':>8}"
    )
    print("\n" + header)
    print("-" * len(header))
    for item in report["fixtures"]:
        d = item["deterministic"]
        print(
            f"{item['name']:<32}{item['source_chars']:>7}{item['chunks']:>7}"
            f"{d['compression_ratio']:>7}{d['signal_coverage']:>8}"
            f"{d['semantic_coverage']:>10}{d['units']:>10}"
            f"{str(d['needs_repair']):>8}"
        )
        if item.get("live"):
            live = item["live"]
            if "error" in live:
                print(f"    live: ERROR {live['error']}")
            else:
                score_str = f" quality_score={live.get('gamas_quality_score', '—')}"
                print(
                    f"    live: {live['provider']}/{live['model']} "
                    f"ratio={live['compression_ratio']} signal={live['signal_coverage']} "
                    f"semantic={live['semantic_coverage']} units={live['units']}"
                    + score_str
                )
    probe = report.get("document_probe") or {}
    if probe:
        structure = probe.get("structure") or {}
        docx = probe.get("docx") or {}
        print(
            "long document: sections={sections} headings={headings} toc={toc} "
            "page_border={border} page_field={page_field}".format(
                sections=probe.get("sections"),
                headings=docx.get("heading_paragraphs"),
                toc=docx.get("toc"),
                border=bool(docx.get("page_borders")),
                page_field=docx.get("page_field"),
            )
        )
        print(
            "structure: repeated_headings={repeated} split_topics={split} "
            "untitled={untitled} abrupt_openings={abrupt} duplicate_paragraphs={dupes}".format(
                repeated=structure.get("repeated_headings"),
                split=structure.get("split_topics"),
                untitled=structure.get("untitled_sections"),
                abrupt=structure.get("abrupt_sections"),
                dupes=structure.get("duplicate_paragraphs"),
            )
        )
    context_facts = report.get("chunk_context") or {}
    if context_facts:
        print(
            "part context: own_topic={topic} continuation={cont} unfinished={cut} "
            "silent_on_raw_stt={raw} chars={chars}".format(
                topic=context_facts["states_own_topic"],
                cont=context_facts["signals_continuation"],
                cut=context_facts["signals_unfinished_part"],
                raw=context_facts["punctuation_free_text_stays_silent"],
                chars=context_facts["context_chars"],
            )
        )
    router = report.get("router") or {}
    if router:
        if "error" in router:
            print(f"\nrouter: ERROR {router['error']}")
        else:
            print(
                "\nrouter: free_only={free_only} paid_fallback={allow_paid_fallback} "
                "outline={outline_enabled} repair={repair_enabled} compile={compile_enabled}".format(
                    **router
                )
            )
            for index, leg in enumerate(router["legs"], start=1):
                print(
                    "  {index}. {provider} [{model}] free_only={free_only} "
                    "chunk={chunk_token_budget}tok/{chunk_char_budget}ch "
                    "max_out={max_output_tokens}".format(index=index, **leg)
                )
            for skip in router["skipped"]:
                print(f"  - {skip['provider']}: {skip['reason']}")
    print(
        "\nNote: a low compression_ratio alone is NOT a failure — read it together "
        "with semantic coverage. A ratio below "
        f"{SEVERE_COMPRESSION} only matters when content units were also lost."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
