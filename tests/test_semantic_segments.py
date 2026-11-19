"""Topic-first map, exact subdivision/provenance, and route capacity (offline)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from gamas_bot.config import DEFAULT_AVALAI_MODEL, note_routes
from gamas_bot.document_model import NormalizedDocument, SourceBlock, plan_from_document
from gamas_bot.llm import LLMContextOverflowError, LLMTransientError, booklet_input_budget, complete_json
from gamas_bot.llm_profiles import ModelCapacity, model_capacity
from gamas_bot.notes import structure_document
from gamas_bot.semantic_segments import SemanticSource, build_semantic_topics
from tests.support import make_settings, sample_notes_json
from tests.test_notes import FakeSession, _FakeResponse, context_overflow_response


def _covered(document, ranges):
    cursor = 0
    slices = []
    for span in ranges:
        assert span.start_offset == cursor
        assert span.start_offset < span.end_offset
        slices.append(document.text[span.start_offset:span.end_offset])
        cursor = span.end_offset
    assert cursor == len(document.text)
    return "".join(slices)


def test_short_nested_topics_remain_separate_with_exact_ranges_even_when_all_fit():
    document = NormalizedDocument.from_source_text("# Parent\nIntro.\n## Child\nDetail.\n# Final\nSummary.")
    plan = plan_from_document(document)
    units = build_semantic_topics(document, plan)
    assert [unit.title for unit in units] == ["Parent", "Child", "Final"]
    assert [unit.parent_id for unit in units] == [None, units[0].topic_id, None]
    assert SemanticSource(document).fit(units, lambda body: len(body) < 1_000) == units
    assert _covered(document, [unit.source_range for unit in units]) == document.text


def test_only_oversized_topic_subdivides_preferring_blocks_and_excluding_overlap():
    document = NormalizedDocument.from_source_text(
        "# Short\nKeep whole.\n# Large\n" + "First subtopic. " * 8 + "\n\n"
        + "Second subtopic, with useful continuity. " * 3 + "\n\n" + "Third subtopic. " * 8 + "\n# End\nKeep whole."
    )
    source = SemanticSource(document)
    roots = build_semantic_topics(document, plan_from_document(document))
    children = source.fit(roots, lambda body: len(body) <= 150, overlap_chars=25, max_probe_chars=150)
    assert children[0] == roots[0] and children[-1] == roots[-1]
    assert len(children) == 5
    assert all(child.topic_id == roots[1].topic_id for child in children[1:-1])
    assert [child.source_range.start_offset for child in children[1:-1]] == [
        block.start_offset for block in document.source_blocks[1:-1]
    ]
    for child in children[2:-1]:
        overlap = child.preceding_context
        assert overlap is not None and overlap.end_offset == child.source_range.start_offset
        assert 0 < overlap.end_offset - overlap.start_offset <= 25
    assert _covered(document, [child.source_range for child in children]) == document.text


@pytest.mark.parametrize("kind,locator", [("page", "page_number"), ("slide", "slide_number")])
def test_physical_pages_or_slides_are_indivisible_until_capacity_requires_it(kind, locator):
    document = NormalizedDocument.from_blocks([
        SourceBlock("", kind, "Page one has an explanation.\n", 0, 0, **{locator: 1}),
        SourceBlock("", kind, "Page two has another explanation.", 0, 0, **{locator: 2}),
    ], source_type="pdf" if kind == "page" else "pptx")
    units = build_semantic_topics(document, plan_from_document(document))
    assert [unit.source_range.block_ids for unit in units] == [(block.id,) for block in document.source_blocks]
    assert _covered(document, [unit.source_range for unit in units]) == document.text


def test_indivisible_word_is_partitioned_without_reordering_or_losing_codepoints():
    document = NormalizedDocument.from_source_text("S" + "آ" * 150 + "TAIL")
    src = SemanticSource(document)
    children = src.fit(build_semantic_topics(document, plan_from_document(document)), lambda body: len(body) <= 35)
    assert len(children) == 5
    assert {span for unit in children for span in unit.source_range.block_ids} == {document.source_blocks[0].id}
    assert _covered(document, [unit.source_range for unit in children]) == document.text


@pytest.mark.parametrize("failure,reason", [
    (context_overflow_response(), "context_overflow"),
    (_FakeResponse(200, {"choices": [{"message": {"content": sample_notes_json("INVALID PARTIAL")},
                                      "finish_reason": "length"}]}), "output_truncation"),
])
def test_provider_recovery_maps_only_failed_topic_and_records_real_child_ranges(failure, reason, tmp_path):
    document = NormalizedDocument.from_source_text(
        "# Alpha\nOpening evidence.\n# Beta\n" + "Middle evidence is preserved. " * 24 + "\n# Gamma\nClosing evidence."
    )
    plan = plan_from_document(document)
    settings = make_settings(avalai_api_key="synthetic-key", db_path=tmp_path / "quota.db", note_compile=False, note_concurrency=1)

    class SelectiveSession(FakeSession):
        count = 0

        def post(self, url, **kwargs):
            user = kwargs["json"]["messages"][1]["content"]
            if user.endswith("موضوع همین بخش: Beta") and not self.count:
                self.count += 1
                self.contents.append(failure)
            else:
                self.contents.append(sample_notes_json())
            return super().post(url, **kwargs)

    session = SelectiveSession([])
    notes = asyncio.run(structure_document(document, settings, session))
    requests = [call["json"]["messages"][1]["content"] for call in session.calls]
    assert sum(r.endswith("موضوع همین بخش: Alpha") for r in requests) == 1
    assert sum(r.endswith("موضوع همین بخش: Gamma") for r in requests) == 1
    assert sum(r.endswith("موضوع همین بخش: Beta") for r in requests) == 3
    event, = notes.semantic_recoveries
    assert event.reason == reason and event.topic_id == plan.topics[1].id
    assert tuple(notes.input_ranges[1:-1]) == event.replacements
    assert _covered(document, notes.input_ranges) == document.text
    assert "INVALID PARTIAL" not in notes.to_json()
    assert "semantic_recoveries" not in notes.to_json()


def test_capability_uses_exact_reference_or_configured_unknown_fallback():
    settings = make_settings(avalai_api_key="synthetic-key", gemini_api_key="synthetic-key",
                             note_unknown_model_context_tokens=30_000, note_unknown_model_max_output_tokens=3_000,
                             note_capacity_safety_tokens=640)
    avalai, gemini = note_routes(settings)[:2]
    assert avalai.model == DEFAULT_AVALAI_MODEL
    assert model_capacity(avalai, settings) == ModelCapacity(
        "avalai", DEFAULT_AVALAI_MODEL, 1_000_000, 393_216, 640,
        "config:checked-in reference, docs/PROVIDERS.md (AvalAI)",
    )
    assert model_capacity(gemini, settings).max_output_tokens == 65_536
    # The same model ID on an undocumented gateway is not the same profile.
    unknown = model_capacity(replace(gemini, model=avalai.model), settings)
    assert (unknown.context_window, unknown.max_output_tokens) == (30_000, 3_000)
    assert unknown.capability_source.startswith("config:NOTE_UNKNOWN_MODEL_")
    assert unknown.available_input_tokens(system_prompt_tokens=2000, request_metadata_tokens=300,
                                          schema_tokens=1000, reserved_output_tokens=3000) == 23_060


def test_schema_request_metadata_and_task_output_each_change_the_budget():
    settings = make_settings(avalai_api_key="synthetic-key", note_structured_output=True,
                             note_compile_max_output_tokens=1024, note_repair_max_output_tokens=512)
    def budget(task, metadata="header", schema=None):
        return booklet_input_budget(settings, system="system instruction", user_metadata=metadata, schema=schema, task=task)

    plain = budget("BOOKLET_GENERATION")
    schema = {"type": "object", "properties": {"value": {"type": "string", "description": "x" * 220}}}
    assert plain > budget("BOOKLET_GENERATION", schema=schema) > budget("BOOKLET_GENERATION", "header" * 40, schema=schema)
    assert budget("BOOKLET_REPAIR") > budget("BOOKLET_COMPILE") > plain


@pytest.mark.parametrize("count", [100, None, 50_000])
def test_gemini_native_count_is_used_before_generation_and_can_fall_back(count, tmp_path):
    settings = make_settings(gemini_api_key="gemini-synthetic", note_providers=("gemini",),
                             note_context_tokens=16_000, db_path=tmp_path / "quota.db")
    counted = _FakeResponse(200, {"totalTokens": count}) if count is not None else _FakeResponse(404, {})
    generated = _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]})
    session = FakeSession([counted, generated])
    request = complete_json(session, settings, system="instructions", user="sample", adaptive_capacity=True)
    if count == 50_000:
        with pytest.raises(LLMContextOverflowError):
            asyncio.run(request)
        assert len(session.calls) == 1
    else:
        assert asyncio.run(request) == {"ok": True}
        assert len(session.calls) == 2
        assert session.calls[1]["url"].endswith(":generateContent")
    assert session.calls[0]["url"].endswith(":countTokens")
    assert session.calls[0]["json"]["generateContentRequest"]["systemInstruction"]["parts"][0]["text"] == "instructions"


def test_mixed_provider_errors_preserve_adaptive_recoverable_capacity_signal(tmp_path):
    settings = make_settings(avalai_api_key="synthetic-key", openrouter_api_key="openrouter-synthetic",
                             db_path=tmp_path / "quota.db")
    paths = []

    async def reply(route, *args, **kwargs):
        paths.append(route.slug)
        if route.slug == "avalai":
            raise LLMTransientError("synthetic transient failure")
        raise LLMContextOverflowError("synthetic capacity failure")

    with patch("gamas_bot.llm._complete_json_on_route", side_effect=reply):
        with pytest.raises(LLMContextOverflowError):
            asyncio.run(complete_json(FakeSession([]), settings, system="S", user="U", adaptive_capacity=True))
    assert paths == ["avalai", "openrouter"]


def test_task_and_unknown_model_clamps_reach_wire(tmp_path):
    settings = make_settings(avalai_api_key="synthetic-key", avalai_model="unknown-test-model",
                             note_unknown_model_max_output_tokens=2400, note_compile_max_output_tokens=1400,
                             db_path=tmp_path / "quota.db")
    session = FakeSession([json.dumps({"okay": True})] * 2)

    async def request():
        for task in ("BOOKLET_GENERATION", "BOOKLET_COMPILE"):
            assert await complete_json(session, settings, system="S", user="U", task=task, adaptive_capacity=True)

    asyncio.run(request())
    assert [call["json"]["max_tokens"] for call in session.calls] == [2400, 1400]
