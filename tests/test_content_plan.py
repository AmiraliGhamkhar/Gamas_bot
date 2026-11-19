"""Offline Phase 2 contract tests; sources and provider replies are synthetic."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from gamas_bot.config import from_env
from gamas_bot.document_model import ContentPlan, NormalizedDocument, plan_from_document
from gamas_bot.llm import LLMResponseError, _parse_json_object
from gamas_bot.note_planning import (
    LLM_PLANNER_MAX_WINDOWS,
    LLM_PLANNER_MIN_CHARS,
    LLM_PLANNER_WINDOW_CHARS,
    maybe_refine_plan,
)
from gamas_bot.notes import build_context, build_source_digest, structure_document
from tests.support import make_settings, sample_notes_json
from tests.test_notes import FakeSession


def test_small_plan_has_extractive_summary_and_one_topic_with_all_paragraphs():
    source = NormalizedDocument.from_source_text(
        "An optical sensor tracks movement.\n\nThe output rises in direct light."
    )
    plan = plan_from_document(source)
    assert plan.planner == "deterministic"
    assert plan.title == plan.document_summary == "An optical sensor tracks movement."
    assert len(plan.topics) == 1
    assert plan.topics[0].source_block_ids == tuple(block.id for block in source.source_blocks)
    assert plan.issues(source) == []


def test_ordered_topics_and_parent_links_without_blank_lines_and_with_numeric_signals():
    text = (
        "# Measurement\r\nA baseline was taken on 2025-11-19 at 12.5 mg.\r\n"
        "## Calibration\r\nFFT stands for Fast Fourier Transform.\r\n"
        "The offset is −7 mV. E = mc²; output was 30 kHz.\r\n"
        "## Verification\r\nکالیبراسیون عبارت است از مقایسه با مرجع. سطح ۵ میلی‌متر است.\r\n"
        "# Appendix\r\nReview the calibration procedure."
    )
    source = NormalizedDocument.from_source_text(text)
    plan = plan_from_document(source)
    assert source.text == text.replace("\r\n", "\n")
    assert source.source_blocks == NormalizedDocument.from_source_text(source.text).source_blocks
    assert [topic.title for topic in plan.topics] == ["Measurement", "Calibration", "Verification", "Appendix"]
    first, second, third, fourth = plan.topics
    assert (first.parent_id, second.parent_id, third.parent_id, fourth.parent_id) == (None, first.id, first.id, None)
    assert first.important_numbers == ("2025-11-19", "12.5 mg")
    assert set(first.required_dimensions) == {"dates", "numbers", "units", "terminology"}
    assert second.important_numbers == ("−7 mV", "30 kHz")
    assert {"abbreviations", "formulas", "definitions", "units"} <= set(second.required_dimensions)
    assert any("E = mc²" in fact for fact in second.important_facts)
    assert {"FFT", "کالیبراسیون"} <= set(plan.global_terminology)
    assert third.important_numbers == ("۵ میلی‌متر",)
    assert "numbers" not in fourth.required_dimensions
    assert "formulas" not in fourth.required_dimensions
    assert plan.validate(source) is plan
    assert ContentPlan.from_json(plan.to_json()).validate(source) == plan
    assert plan.to_payload()["topics"][0]["id"] == first.id


def test_unmarked_heading_lines_and_explicit_term_emphasis():
    source = NormalizedDocument.from_source_text(
        "نورشناسی\nدر این آزمایش از **پراش نور** استفاده می‌شود.\n\n"
        "Interference\nTwo light waves can overlap."
    )
    plan = plan_from_document(source)
    assert [topic.title for topic in plan.topics] == ["نورشناسی", "Interference"]
    assert "پراش نور" in plan.topics[0].critical_terms


def test_repeat_and_exact_duplicate_are_distinguished_without_dropping_ranges():
    source = NormalizedDocument.from_source_text(
        "## Calibration\nCheck the 12 mg reference.\n\n"
        "## Calibration\nCheck the 12 mg reference.\n\n"
        "## Calibration\nAn alternate reference uses 24 mg."
    )
    plan = plan_from_document(source)
    first, repeat, expanded = plan.topics
    assert repeat.duplicate_of == repeat.repeated_title_of == first.id
    assert expanded.repeated_title_of == first.id and expanded.duplicate_of is None
    assert expanded.important_numbers == ("24 mg",)
    assert plan.coverage_requirements == tuple(block.id for block in source.source_blocks)
    assert plan.validate(source) is plan


def _very_large_source() -> NormalizedDocument:
    unit = "Calibration maps sensor readings to a physical scale. " * 15
    return NormalizedDocument.from_source_text("\n\n".join(unit + f"Synthetic example {index}." for index in range(320)) + "\n\nEnd of synthetic lesson: 198 kHz.")


def test_long_weak_plan_preserves_the_last_range_and_all_source_blocks():
    source = _very_large_source()
    assert len(source.text) >= LLM_PLANNER_MIN_CHARS
    plan = plan_from_document(source)
    ranges = [item for topic in plan.topics for item in topic.source_ranges]
    assert len(plan.topics) > 1
    assert "198 kHz" in plan.topics[-1].important_numbers
    assert tuple(block_id for item in ranges for block_id in item.block_ids) == plan.coverage_requirements
    assert "".join(source.text[item.start_offset:item.end_offset] for item in ranges) == source.text
    assert plan == plan_from_document(source)


def test_context_projects_the_full_plan_without_mutating_or_truncating_it():
    chunks = [f"## Topic {i} of the illustration\nA synthetic line about optics." for i in range(250)]
    source = NormalizedDocument.from_source_text("\n\n".join(chunks))
    plan = plan_from_document(source)
    context = build_context(chunks, plan=plan, source_ranges=source.chunk_ranges(chunks))
    assert context.plan is plan
    assert context.title == "Topic 0 of the illustration"
    assert context.topic_for(1, len(chunks)) == "Topic 0 of the illustration"
    assert "249" not in build_source_digest(context)
    assert len(context.topics) == len(plan.topics) == 250
    assert plan.topics[-1].title == "Topic 249 of the illustration"


def test_live_chunk_map_uses_the_plan_without_adding_provider_requests():
    body = "Repeated synthetic evidence describes a sample. " * 36
    source = NormalizedDocument.from_source_text(f"# Optical sensors\n{body}\n\n# Mirrors\n{body}")
    session = FakeSession([sample_notes_json()] * 30)
    settings = make_settings(note_chunk_chars=700, note_compile=False, note_concurrency=1, avalai_api_key="avalai-test-key")
    notes = asyncio.run(structure_document(source, settings, session))
    assert 2 < len(session.calls) == len(notes.input_ranges)
    second = session.calls[1]["json"]["messages"]
    assert "موضوع همین بخش: Optical sensors" in second[1]["content"]
    assert dict(plan_from_document(source).metadata)["refined_windows"] == "0"


@pytest.mark.parametrize("defect", ["duplicate_id", "unknown_block", "unknown_parent", "cycle", "range_gap",
                                   "out_of_bounds", "missing_description", "unsupported_dimension"])
def test_canonical_validation_rejects_invalid_contract(defect):
    source = NormalizedDocument.from_source_text("# First\nOne line.\n## Second\nAnother line.")
    plan = plan_from_document(source)
    first, second = plan.topics
    if defect == "duplicate_id":
        second = replace(second, topic_id=first.id)
    elif defect == "unknown_block":
        # Coordinated trust-boundary forgery: only the actual document can
        # reveal that all three matching copies of this block ID are wrong.
        second = replace(second, unit_ids=("missing",), source_ranges=(replace(second.source_ranges[0], block_ids=("missing",)),))
        plan = replace(plan, coverage_requirements=(first.unit_ids[0], "missing"))
    elif defect == "unknown_parent":
        second = replace(second, parent_id="missing")
    elif defect == "cycle":
        first = replace(first, parent_id=second.id)
    elif defect == "range_gap":
        second = replace(second, source_ranges=(replace(second.source_ranges[0], start_offset=1),))
    elif defect == "out_of_bounds":
        second = replace(second, source_ranges=(replace(second.source_ranges[0], end_offset=len(source.text) + 1),))
    elif defect == "missing_description":
        second = replace(second, description=" ")
    elif defect == "unsupported_dimension":
        second = replace(second, required_dimensions=("formulas",))
    bad = replace(plan, topics=(first, second))
    with pytest.raises(ValueError):
        bad.validate(source)
    if defect not in {"unknown_block", "out_of_bounds"}:
        with pytest.raises(ValueError):
            ContentPlan.from_json(bad.to_json())


@pytest.mark.parametrize("missing", ["source_ranges", "source_block_ids", "important_facts", "required_dimensions"])
def test_canonical_json_requires_explicit_fields_even_empty_arrays(missing):
    source = NormalizedDocument.from_source_text("# Test\nSynthetic text.")
    payload = plan_from_document(source).to_payload()
    del payload["topics"][0][missing]
    with pytest.raises(ValueError, match="missing canonical topic field"):
        ContentPlan.from_payload(payload)


def test_planner_setting_defaults_off_and_parses_opt_in(monkeypatch):
    monkeypatch.delenv("NOTE_LLM_PLANNER", raising=False)
    assert from_env(env_file="/nonexistent").note_llm_planner is False
    monkeypatch.setenv("NOTE_LLM_PLANNER", "true")
    assert from_env(env_file="/nonexistent").note_llm_planner is True


@pytest.mark.parametrize("scenario", ["default_off", "small", "medium", "large_usable", "oversized_block"])
def test_optional_planner_never_calls_provider_outside_all_gates(scenario):
    if scenario == "default_off":
        source = _very_large_source()
    elif scenario == "large_usable":
        body = "Image sensors convert light into electrical signals. " * 5000
        source = NormalizedDocument.from_source_text(f"# Optics\n{body}\n\n## Sensors\n{body}")
    elif scenario == "oversized_block":
        source = NormalizedDocument.from_source_text("Image sensors convert light into electrical signals. " * 5000)
    else:
        text = "Image sensors convert light into electrical signals. " * (30 if scenario == "small" else 2000)
        source = NormalizedDocument.from_source_text(text + "\n\nOther examples discuss lenses.")
    settings = make_settings() if scenario == "default_off" else make_settings(note_llm_planner=True)
    plan = plan_from_document(source)
    with patch("gamas_bot.llm.complete_json", new_callable=AsyncMock) as provider:
        assert asyncio.run(maybe_refine_plan(plan, source, settings, FakeSession([]))) is plan
        provider.assert_not_awaited()


def test_optional_planning_is_local_bounded_and_only_accepts_grounded_partitions():
    source = _very_large_source()
    plan = plan_from_document(source)
    seen_windows = []

    async def answer(*args, **request):
        assert request["task"] == "BOOKLET_PLAN"
        assert request["strict_json"] is True
        assert len(request["user"]) <= LLM_PLANNER_WINDOW_CHARS
        blocks = json.loads(request["user"])["blocks"]
        assert blocks not in seen_windows
        seen_windows.append(blocks)
        if len(seen_windows) == 1:
            return {"topics": [
                {"title": "sensor readings", "start_block": 1, "end_block": 1},
                {"title": "physical scale", "start_block": 2, "end_block": len(blocks)},
            ]}
        if len(seen_windows) == 2:
            return {"topics": [{"title": "Unrelated cosmic recipe", "start_block": 1, "end_block": len(blocks)}]}
        raise LLMResponseError("synthetic invalid JSON response")

    with patch("gamas_bot.llm.complete_json", side_effect=answer) as provider:
        result = asyncio.run(maybe_refine_plan(plan, source, make_settings(note_llm_planner=True), FakeSession([])))
    assert provider.await_count == LLM_PLANNER_MAX_WINDOWS
    assert result.planner == "llm"
    assert dict(result.metadata)["refined_windows"] == "1"
    assert result.coverage_requirements == plan.coverage_requirements
    assert [topic.title for topic in result.topics[:3]] == ["sensor readings", "physical scale", plan.topics[1].title]
    assert len(result.topics) == len(plan.topics) + 1
    assert ContentPlan.from_json(result.to_json()).validate(source) == result


def test_invalid_local_hierarchy_and_block_ranges_retain_the_deterministic_plan():
    source = _very_large_source()
    plan = plan_from_document(source)
    answers = [
        {"topics": [{"title": "Calibration", "start_block": 1, "end_block": 1, "parent_id": "self"}]},
        {"topics": [{"title": "Calibration", "start_block": 1, "end_block": 1}]},
        {"topics": [{"title": "Calibration", "start_block": 1, "end_block": 999}]},
    ]
    with patch("gamas_bot.llm.complete_json", side_effect=answers):
        assert asyncio.run(maybe_refine_plan(plan, source, make_settings(note_llm_planner=True), FakeSession([]))) is plan


@pytest.mark.parametrize("raw", [
    '```json\n{"topics": []}\n```',
    'explanation {"topics": []}',
    '[{"topics": []}]',
    '{"topics": [], "topics": []}',
    '{"topics": NaN}',
])
def test_local_planner_requires_a_complete_strict_json_object(raw):
    assert _parse_json_object(' {"topics": []} ', strict=True) == {"topics": []}
    with pytest.raises(LLMResponseError):
        _parse_json_object(raw, strict=True)
