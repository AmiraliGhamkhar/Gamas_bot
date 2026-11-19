"""Release regressions: source partition, retention and an unconfigured startup."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from gamas_bot.config import check_diagnostics, from_env, note_routes
from gamas_bot.document_model import NormalizedDocument, plan_from_document
from gamas_bot.semantic_segments import SemanticSource, build_semantic_topics
from scripts.booklet_regression_bench import MISSING, REPEAT, TABLE, TOPICS, lesson, run


def test_long_nested_source_has_no_holes_or_body_filtering_under_tight_budgets():
    source = lesson(1500)
    assert len(source) > 250_000
    document = NormalizedDocument.from_source_text(source)
    assert document.text == source
    plan = plan_from_document(document)
    plan.validate(document)
    assert any(topic.parent_id for topic in plan.topics)
    semantic = SemanticSource(document)
    units = build_semantic_topics(document, plan)
    fitted = semantic.fit(units, lambda body: len(body) <= 6400, overlap_chars=120, max_probe_chars=6400)
    assert len(fitted) > len(units)
    cursor = 0
    bodies = []
    for unit in fitted:
        span = unit.source_range
        assert span.start_offset == cursor < span.end_offset
        scene = semantic.body(unit)
        assert 0 < len(scene) <= 6400
        assert scene == source[span.start_offset:span.end_offset]
        if unit.preceding_context is not None:
            assert unit.preceding_context.end_offset <= span.start_offset
        cursor = span.end_offset
        bodies.append(scene)
    assert cursor == len(source)
    reconstructed = "".join(bodies)
    assert reconstructed == source
    assert reconstructed.count(REPEAT) == 4500
    assert all(anchor in reconstructed for anchor in (MISSING, TABLE, TOPICS[-1][3][-1]))
    assert "یادداشت غیرآموزشی" in reconstructed


def test_scripted_generation_and_synthesis_preserve_fixture_key():
    result = asyncio.run(run(repeats=45, jobs=1, compile_enabled=True, fault=False))
    assert result["stages"]["BOOKLET_COMPILE"]["requests"] == 1
    assert result["repair_jobs"] == 0
    score, = result["quality"]
    assert (score["topics"], score["facts"], score["definitions"], score["table_rows"]) == (3, 9, 3, 2)
    assert score["measurement_sentences"] == score["procedure_and_explanations"] == 3
    assert score["renderable"] and score["review_prompt"]


def test_absent_env_does_not_enable_paid_booklet_stages_or_legacy_providers(tmp_path):
    with patch.dict(os.environ, {}, clear=True):
        settings = from_env(env_file=str(tmp_path / "missing.env"))
    assert not settings.note_compile and not settings.note_llm_planner and not settings.note_completeness_fill
    assert settings.note_enable_deterministic_fallback and settings.note_max_repair_attempts == 2
    assert not settings.note_configured and note_routes(settings) == ()
    diagnostics = check_diagnostics(settings)
    assert diagnostics  # missing required deployment config is diagnosed, not used
