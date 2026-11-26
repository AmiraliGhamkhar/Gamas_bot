"""Focused regressions for booklet request efficiency and telemetry safety.

The provider is scripted offline: these tests measure request *shape*, never
model quality or a provider invoice.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from gamas_bot import llm, notes as n
from gamas_bot.booklet_telemetry import (
    BookletTelemetry,
    current_job_id,
    enter_job_scope,
    exit_job_scope,
    telemetry_scope,
)
from gamas_bot.booklet_verification import _deterministic_pre_gate, verify_and_repair
from gamas_bot.document_model import NormalizedDocument, SourceBlock, plan_from_document
from gamas_bot.llm import LLMContextOverflowError, LLMError
from gamas_bot.semantic_segments import SemanticSource, build_semantic_topics, pack_semantic_units
from scripts.booklet_regression_bench import lesson
from tests.support import make_settings
from tests.test_notes import FakeSession, _FakeResponse


def _settings(tmp_path: Path, **overrides):
    params = {"db_path": tmp_path / "cost.sqlite", "avalai_api_key": "fake-key",
              "note_ttfb_timeout": 0, "note_compile": False}
    params.update(overrides)
    return make_settings(**params)


def _source_document(text: str) -> NormalizedDocument:
    return NormalizedDocument.from_source_text(text, source_type="audio", kind="transcript")


async def _run(source: str | NormalizedDocument, settings, *, on_task=None):
    """Run the booklet pipeline with a scripted, source-grounded provider."""
    tasks: list[tuple[str, str]] = []

    async def provider(_session, _settings, *, task, user, **_kwargs):
        tasks.append((task, user))
        if on_task is not None:
            reply = on_task(task, user, len([name for name, _ in tasks if name == task]))
            if reply is not None:
                return reply
        if task == "BOOKLET_GENERATION":
            quote = next((line.strip() for line in reversed(user.splitlines())
                          if line.strip() and not line.startswith(("{", "###"))), "متن درس")
            return {"title": "مرور درس", "sections": [
                {"heading": "بخش اصلی", "paragraphs": [quote]},
            ], "final_review": [quote]}
        raise AssertionError(f"unexpected LLM task {task}")

    with patch.object(n, "complete_json", side_effect=provider):
        notes = await n.structure_document(source, settings, object())
    return notes, tasks


def test_short_medium_and_long_transcripts_keep_full_source_coverage(tmp_path):
    short = "دمای water برابر 25 °C ثبت شد و سپس تبخیر آغاز شد."
    medium = "\n\n".join((
        "# اندازه", "دمای water برابر 25 °C ثبت شد.",
        "# رنگ", "تغییر رنگ نمونه با نور مشخص شد.",
        "# جریان", "طول مسیر جریان برابر 32 cm است.",
    ))
    long_source = lesson(240)
    for label, source, chunk_chars in (
        ("short", short, 64_000), ("medium", medium, 64_000), ("long", long_source, 6_000),
    ):
        notes, tasks = asyncio.run(_run(
            _source_document(source), _settings(tmp_path, note_chunk_chars=chunk_chars),
        ))
        maps = [user for task, user in tasks if task == "BOOKLET_GENERATION"]
        assert maps, label
        if label == "short":
            assert len(maps) == 1
        elif label == "medium":
            assert 1 <= len(maps) <= 3
        else:
            assert len(maps) > 1
        assert "".join(source[r.start_offset:r.end_offset] for r in notes.input_ranges) == source, label
        assert notes.sections and notes.verification_report is not None, label


def test_multiple_units_pack_without_losing_source_blocks(tmp_path):
    document = NormalizedDocument.from_blocks([
        SourceBlock("", "page", f"[صفحهٔ {index}]\nتوضیح کوتاه دربارهٔ اندازه‌گیری نمونه.\n",
                    0, 0, page_number=index) for index in range(1, 9)
    ], source_type="pdf", title="اندازه‌گیری")
    plan = plan_from_document(document)
    units = build_semantic_topics(document, plan)
    source = SemanticSource(document)
    packed = pack_semantic_units(source, units, target_chars=10_000,
                                 fits=lambda body: len(body) <= 10_000)
    assert len(units) >= 8
    assert len(packed) < len(units)
    assert [topic_id for unit in packed for topic_id in unit.covered_topic_ids] == [
        topic.id for topic in plan.topics
    ]
    assert "".join(source.body(unit) for unit in packed) == document.text
    telemetry = BookletTelemetry(job_id="packing")
    with telemetry_scope(telemetry):
        asyncio.run(_run(document, _settings(tmp_path, note_pack_target_chars=20_000)))
    assert telemetry.semantic_unit_count >= 8
    assert 0 < telemetry.packed_unit_count < telemetry.semantic_unit_count


def test_context_overflow_splits_only_the_affected_unit(tmp_path):
    first = "اندازه: طول نمونه برابر ۵ mm است."
    second = "رنگ: تغییر رنگ نمونه با نور مشخص شد."
    source = _source_document(f"# اندازه\n{first}\n\n# رنگ\n{second}")

    def reply(task, user, count):
        if task == "BOOKLET_GENERATION" and count == 1:
            raise LLMContextOverflowError("synthetic overflow")
        return None

    notes, tasks = asyncio.run(_run(source, _settings(tmp_path, note_chunk_chars=200), on_task=reply))
    maps = [user for task, user in tasks if task == "BOOKLET_GENERATION"]
    assert len(maps) >= 3  # one failed unit plus its source-preserving leaves
    assert any(first in user for user in maps)
    assert any(second in user for user in maps)
    assert any(event.reason == "context_overflow" for event in notes.semantic_recoveries)
    assert "".join(source.text[r.start_offset:r.end_offset] for r in notes.input_ranges) == source.text


def test_failed_map_unit_recovers_locally_without_regenerating_siblings(tmp_path):
    first = "اندازه: طول نمونه برابر ۵ mm است."
    second = "رنگ: تغییر رنگ نمونه با نور مشخص شد."
    source = _source_document(f"# اندازه\n{first}\n\n# رنگ\n{second}")

    async def provider(_session, _settings, *, task, user, **_kwargs):
        assert task == "BOOKLET_GENERATION"
        if "اندازه:" in user:
            raise LLMError("synthetic provider failure")
        return {"title": "درس", "sections": [{"heading": "رنگ", "paragraphs": [second]}],
                "final_review": [second]}

    settings = _settings(tmp_path)
    with patch.object(n, "complete_json", side_effect=provider):
        notes = asyncio.run(n.structure_document(source, settings, object()))
    assert len(notes.failed_sections) == 1 and notes.failed_sections[0].recovered
    assert any(second in section.paragraphs for section in notes.sections)
    assert "".join(source.text[r.start_offset:r.end_offset] for r in notes.input_ranges) == source.text


def test_deterministic_quality_failure_uses_repair_not_semantic_verification(tmp_path):
    measured = "طول نمونه برابر ۵ mm است."
    source = _source_document(measured)
    tasks: list[str] = []

    async def provider(_session, _settings, *, task, user, **_kwargs):
        tasks.append(task)
        if task == "BOOKLET_GENERATION":
            return {"title": "اندازه", "sections": [{"heading": "اندازه", "paragraphs": [
                "طول نمونه برابر ۹ mm است."
            ]}], "final_review": [measured]}
        if task == "BOOKLET_REPAIR":
            return {"sections": [{"heading": "اندازه", "paragraphs": [measured]}]}
        raise AssertionError(f"unexpected deterministic task {task}")

    settings = _settings(tmp_path, note_strict_grounding=True)
    with patch.object(n, "complete_json", side_effect=provider):
        notes = asyncio.run(n.structure_document(source, settings, object()))
    assert "BOOKLET_REPAIR" in tasks
    assert "BOOKLET_VERIFY" not in tasks
    assert notes.sections[0].paragraphs[0] == measured


def test_semantic_verification_is_triggered_only_for_ambiguous_claims(tmp_path):
    evidence = "دارو فقط همراه غذا مصرف شود تا از عارضه گوارشی جلوگیری شود."
    claim = "برای جلوگیری از عارضه گوارشی دارو باید فقط با غذا مصرف شود."
    source = _source_document(evidence)
    tasks: list[str] = []

    async def provider(_session, _settings, *, task, user, **_kwargs):
        tasks.append(task)
        if task == "BOOKLET_GENERATION":
            return {"title": "درمان", "sections": [{"heading": "درمان", "paragraphs": [claim]}],
                    "final_review": [evidence]}
        if task == "BOOKLET_VERIFY":
            assert claim in user and evidence in user
            return {"verdicts": [{"id": "q1", "verdict": "supported"}]}
        raise AssertionError(f"unexpected task {task}")

    settings = _settings(tmp_path, note_strict_grounding=True)
    with patch.object(n, "complete_json", side_effect=provider):
        asyncio.run(n.structure_document(source, settings, object()))
    assert tasks.count("BOOKLET_VERIFY") == 1
    assert "BOOKLET_REPAIR" not in tasks


def test_targeted_repair_leaves_unaffected_sections_untouched(tmp_path):
    first = "اندازه: طول نمونه برابر ۵ mm است."
    second = "رنگ: تغییر رنگ نمونه با نور مشخص شد."
    source = _source_document(f"# اندازه\n{first}\n\n# رنگ\n{second}")
    repaired: list[str] = []

    async def provider(_session, _settings, *, task, user, **_kwargs):
        if task == "BOOKLET_GENERATION":
            if "رنگ:" in user:
                return {"title": "درس", "sections": [{"heading": "رنگ", "paragraphs": [second]}],
                        "final_review": [first]}
            return {"title": "درس", "sections": [
                {"heading": "اندازه", "paragraphs": ["اندازه: طول نمونه برابر ۹ mm است."]},
            ], "final_review": [first]}
        if task == "BOOKLET_REPAIR":
            repaired.append(user)
            assert "۹ mm" in user and first in user
            assert second not in user
            return {"sections": [{"heading": "اندازه", "paragraphs": [first]}]}
        raise AssertionError(f"unexpected task {task}")

    settings = _settings(tmp_path, note_strict_grounding=True, note_chunk_chars=120)
    with patch.object(n, "complete_json", side_effect=provider):
        notes = asyncio.run(n.structure_document(source, settings, object()))
    assert len(repaired) == 1
    assert notes.sections[0].paragraphs == (first,)
    assert any(second in section.paragraphs for section in notes.sections)


def test_compile_skipped_when_merge_needs_no_editorial_correction(tmp_path):
    # Long enough that the old unconditional compile gate would have run.
    text = ("اندازه: طول نمونه برابر ۵ mm است. "
            + "توضیح علمی دربارهٔ اندازه\u200cگیری نمونه در آزمایشگاه ثبت شد. " * 12).strip()
    source = _source_document(text)
    with patch.object(n, "COMPILE_MIN_NOTES_CHARS", 0):
        notes, tasks = asyncio.run(_run(source, _settings(tmp_path, note_compile=True)))
    assert [task for task, _ in tasks] == ["BOOKLET_GENERATION"]
    assert notes.sections and text in notes.sections[0].paragraphs[0]
    assert notes.verification_report is not None and notes.verification_report.passed


def test_compile_required_when_deterministic_signal_present(tmp_path):
    first = "طول نمونه برابر ۵ mm است."
    second = "طول نمونه برابر ۷ mm است."
    source = _source_document(f"# اندازه\n{first}\n\n# اندازهٔ دوم\n{second}")

    def reply(task, user, count):
        if task == "BOOKLET_GENERATION":
            text = first if "۵ mm" in user else second
            return {"title": "درس", "sections": [{"heading": "اندازه", "paragraphs": [text]}],
                    "final_review": [text]}
        if task == "BOOKLET_COMPILE":
            return {"deduplicate": [{"id": "s0001", "field": "paragraphs"}]}
        return None

    with patch.object(n, "COMPILE_MIN_NOTES_CHARS", 0):
        notes, tasks = asyncio.run(_run(
            source, _settings(tmp_path, note_compile=True, note_chunk_chars=120), on_task=reply,
        ))
    assert "BOOKLET_COMPILE" in [task for task, _ in tasks]
    assert any(conflict.kind == "potential_value_conflict" for conflict in notes.merge_conflicts)


def test_deterministic_pre_gate_skips_semantic_route_for_malformed_booklet(tmp_path):
    evidence = "دارو فقط همراه غذا مصرف شود تا از عارضه گوارشی جلوگیری شود."
    claim = "برای جلوگیری از عارضه گوارشی دارو باید فقط با غذا مصرف شود."
    source = _source_document(evidence)
    plan = plan_from_document(source)
    topic = plan.topics[0]
    section = n.NoteSection(heading="درمان", paragraphs=(claim,), section_id="duplicate-id",
                            topic_id=topic.id, source_block_ids=topic.unit_ids)
    draft = n.StructuredNotes(title="درمان", sections=(section, section), final_review=(evidence,))
    assert _deterministic_pre_gate(draft) == "duplicate_identifier"
    writer = n.SectionWriter(_settings(tmp_path, note_strict_grounding=True), object())
    with patch.object(n, "complete_json", side_effect=AssertionError("no LLM for malformed output")):
        result = asyncio.run(verify_and_repair(draft, source, plan, writer))
    assert result.verification_report is not None
    assert any(issue.code == "duplicate_identifier" for issue in result.verification_report.issues)


def test_telemetry_reports_all_job_counters_and_survives_broken_metrics(tmp_path):
    job_token = enter_job_scope(77)
    assert current_job_id() == "77"
    exit_job_scope(job_token)
    assert current_job_id() == ""
    source = _source_document("اندازه: طول نمونه برابر ۵ mm است.")
    telemetry = BookletTelemetry(job_id="42", source_chars=7)
    with telemetry_scope(telemetry):
        notes, tasks = asyncio.run(_run(source, _settings(tmp_path)))
        telemetry.record_provider_tokens(120, 30)
        telemetry.record_wire("BOOKLET_GENERATION", 2)
    row = telemetry.snapshot()
    assert row["job_id"] == "42" and row["source_chars"] == len(source.text)
    assert row["estimated_source_tokens"] > 0 and row["estimated_input_tokens"] > 0
    assert row["map_requests"] == len([task for task, _ in tasks if task == "BOOKLET_GENERATION"])
    assert row["total_llm_requests"] == len(tasks)
    assert row["input_tokens"] == 120 and row["output_tokens"] == 30
    assert row["map_retries"] >= 1 and row["wire_attempts"] >= 2
    assert row["final_booklet_chars"] == len(notes.to_json())
    assert row["final_booklet_sections"] == len(notes.sections)
    assert row["quality_state"] == notes.quality_state
    assert row["planner_requests"] == row["synthesis_requests"] == 0
    assert row["semantic_verification_requests"] == row["repair_requests"] == 0

    with patch("gamas_bot.booklet_telemetry.estimate_tokens", side_effect=RuntimeError("broken")), \
            patch("gamas_bot.booklet_telemetry.logger.info", side_effect=RuntimeError("broken")), \
            patch("gamas_bot.booklet_telemetry.current_telemetry", side_effect=RuntimeError("broken")):
        broken, _ = asyncio.run(_run(source, _settings(tmp_path)))
    assert broken.sections and broken.verification_report is not None


def test_job_reference_reaches_the_telemetry_log(tmp_path, caplog):
    source = _source_document("اندازه: طول نمونه برابر ۵ mm است.")
    job_token = enter_job_scope(12345)
    try:
        with caplog.at_level("INFO", logger="gamas_bot.booklet_telemetry"):
            asyncio.run(_run(source, _settings(tmp_path)))
    finally:
        exit_job_scope(job_token)
    rows = [row.message for row in caplog.records if row.name == "gamas_bot.booklet_telemetry"]
    assert rows and "job=12345" in rows[-1]
    assert "map=1" in rows[-1] and "booklet_sections=" in rows[-1]


def test_telemetry_records_actual_provider_usage_and_retries(tmp_path):
    settings = make_settings(db_path=tmp_path / "telemetry.sqlite", avalai_api_key="fake-key",
                             note_ttfb_timeout=0)
    payload = {
        "choices": [{"message": {"content": json.dumps({"sections": []})}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 45},
    }
    telemetry = BookletTelemetry(job_id="usage")
    with telemetry_scope(telemetry):
        asyncio.run(llm.complete_json(FakeSession([_FakeResponse(200, payload)]), settings,
                                      system="system", user="user", task="BOOKLET_GENERATION"))
    row = telemetry.snapshot()
    assert row["input_tokens"] == 123 and row["output_tokens"] == 45
    assert row["wire_attempts"] == 1 and row["map_retries"] == 0
    assert row["map_requests"] == 0  # a direct LLM call has no pipeline call site
