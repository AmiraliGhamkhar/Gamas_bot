"""Section Writer contracts and isolated checkpoint recovery (synthetic/offline)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from gamas_bot._prompt_block import SECTION_EXPANSION_MATRIX
from gamas_bot.document_model import NormalizedDocument, plan_from_document
from gamas_bot.llm import LLMContextOverflowError, LLMResponseError
from gamas_bot.notes import (
    SectionCheckpoint,
    SectionGenerationError,
    SectionWriter,
    generate_section,
    notes_response_schema,
    structure_document,
)
from tests.support import make_settings
from tests.test_notes import FakeSession, _FakeResponse


def _payload(label, body):
    return {"title": "Synthetic lesson", "sections": [{"heading": label, "paragraphs": [body]}]}


def _topic(user):
    return json.loads(next(line.split(": ", 1)[1] for line in user.splitlines() if line.startswith("موضوع و طرح (فقط ساختار):")))


def test_topic_writer_parallel_is_bounded_ordered_and_only_receives_relevant_evidence():
    text = "# Optics\nPRISM_EVIDENCE.\n## Lenses\nCURVED_SURFACE.\n# Sound\nOSCILLATION_EVIDENCE."
    doc = NormalizedDocument.from_source_text(text)
    plan = plan_from_document(doc)
    seen, finished = [], []
    count = peak = 0

    async def provider(_session, _settings, *, system, user, schema, **_kwargs):
        nonlocal count, peak
        contract = _topic(user)
        local = contract["plan"]["position"] - 1
        assert schema == notes_response_schema()
        assert len(SECTION_EXPANSION_MATRIX) == 17
        assert all(dimension in system for dimension in SECTION_EXPANSION_MATRIX)
        assert "توضیح تکمیلی (نه متن منبع)" in system
        assert contract["topic"]["id"] == plan.topics[local].id
        assert contract["topic"]["parent_id"] == plan.topics[local].parent_id
        for i, signal in enumerate(("PRISM_EVIDENCE", "CURVED_SURFACE", "OSCILLATION_EVIDENCE")):
            assert (signal in user) == (i == local)
        seen.append(local)
        count += 1
        peak = max(peak, count)
        await asyncio.sleep(0.015 if local == 0 else 0)
        count -= 1
        finished.append(local)
        return _payload(plan.topics[local].title, ["Prism detail.", "Lens detail.", "Wave detail."][local])

    settings = make_settings(note_concurrency=2, note_structured_output=True, note_compile=False)
    with patch("gamas_bot.notes.complete_json", side_effect=provider):
        notes = asyncio.run(structure_document(doc, settings, object()))
    assert len(seen) == 3 and peak == 2 and finished[0] != 0
    assert [section.heading for section in notes.sections] == ["Optics", "Lenses", "Sound"]
    assert [section.topic_id for section in notes.sections] == [topic.id for topic in plan.topics]
    assert notes.sections[1].parent_topic_id == plan.topics[0].id
    assert [section.source_block_ids for section in notes.sections] == [topic.source_block_ids for topic in plan.topics]
    assert "source_block_ids" not in notes.to_json()


def test_public_writer_returns_note_section_with_verified_locators_and_fallback_routing(tmp_path):
    source = NormalizedDocument.from_source_text("# Optics\n[صفحهٔ ۲]\nLight crosses the lens.")
    plan = plan_from_document(source)
    settings = make_settings(avalai_api_key="avalai-synthetic", openrouter_api_key="router-synthetic",
                             note_compile=False, db_path=tmp_path / "quota.db")
    response = _payload("Optics", "Light crosses the lens.")
    response["sections"][0]["source_locators"] = ["[صفحهٔ ۹۹]"]
    unauthorized = _FakeResponse(401, {"error": {"message": "synthetic auth failure"}})
    session = FakeSession([unauthorized, json.dumps(response)])
    section = asyncio.run(generate_section(plan.topics[0], source, plan, SectionWriter(settings, session)))
    assert "avalai" in session.calls[0]["url"] and "openrouter" in session.calls[1]["url"]
    assert section.heading == "Optics"
    assert section.source_locators == ("صفحهٔ ۲",)
    assert section.source_block_ids == plan.topics[0].source_block_ids


@pytest.mark.parametrize("split_failure", [False, True])
def test_serializable_checkpoint_resumes_only_failed_section_or_leaf(split_failure):
    document = NormalizedDocument.from_source_text(
        "# First\n FIRST_EVIDENCE.\n# Failing\n" + "FIRST_HALF. " * 45 + "\n\n" + "SECOND_HALF. " * 48
        + "\n# Last\n LAST_EVIDENCE."
    )
    settings = make_settings(note_compile=False, note_enable_deterministic_fallback=False, note_concurrency=3)
    called, saved = [], []

    async def provider(_session, _settings, *, user, **_kwargs):
        title = _topic(user)["topic"]["title"]
        has_first, has_second = "FIRST_HALF" in user, "SECOND_HALF" in user
        called.append((title, has_first, has_second))
        if title == "Failing":
            if split_failure and has_first and has_second:
                raise LLMContextOverflowError("synthetic overflow")
            if not split_failure or has_second:
                raise LLMResponseError("synthetic writer failure")
        return _payload(title, "Preserved complete result " + title + ".")

    async def persist(checkpoint):
        saved.append(json.loads(json.dumps(checkpoint.to_payload())))

    async def run():
        with patch("gamas_bot.notes.complete_json", side_effect=provider):
            with pytest.raises(SectionGenerationError) as caught:
                await structure_document(document, settings, object(), on_section_checkpoint=persist)
        assert len(caught.value.failures) == 1
        assert caught.value.failures[0].topic_id == "t002"
        assert caught.value.failures[0].recovered is False
        assert len(caught.value.checkpoint.completed) == 2 + int(split_failure)
        assert saved and saved[-1] == caught.value.checkpoint.to_payload()
        checkpoint = SectionCheckpoint.from_payload(saved[-1])
        resumed = []

        async def repaired(_session, _settings, *, user, **_kwargs):
            resumed.append(user)
            return _payload("Failing", "Recovered section on retry.")

        with patch("gamas_bot.notes.complete_json", side_effect=repaired):
            notes = await structure_document(document, settings, object(), section_checkpoint=checkpoint)
        assert len(resumed) == 1 and _topic(resumed[0])["topic"]["title"] == "Failing"
        assert ("FIRST_HALF" not in resumed[0]) == split_failure
        assert not notes.failed_sections
        assert notes.sections[0].paragraphs[0] == "Preserved complete result First."
        assert notes.sections[-1].paragraphs[0] == "Preserved complete result Last."
        assert "".join(document.text[span.start_offset:span.end_offset] for span in notes.input_ranges) == document.text
        with pytest.raises(ValueError, match="different|document"):
            await structure_document(document.text + " different", settings, object(), section_checkpoint=checkpoint)

    asyncio.run(run())


def test_live_verifier_recovers_unbacked_measurement_only_in_the_affected_topic():
    document = "# Optics\nSynthetic lens evidence.\n# Quantity\nدوز آزمایشی گزارش‌شده ۵ میلی‌گرم است."
    settings = make_settings(note_compile=True, note_completeness_fill=True)
    calls = []

    async def provider(_session, _settings, *, user, task, **_kwargs):
        calls.append(task)
        title = _topic(user)["topic"]["title"]
        if title == "Quantity":
            return _payload(title, "دوز آزمایشی گزارش‌شده ۹۹۹ میلی‌گرم است.")
        return _payload(title, "Synthetic lens evidence.")

    with patch("gamas_bot.notes.complete_json", side_effect=provider):
        notes = asyncio.run(structure_document(document, settings, object()))
    assert calls == ["BOOKLET_GENERATION"] * 2
    assert notes.quality_state == "RECOVERED"
    assert notes.sections[0].heading == "Optics"
    assert "۹۹۹" not in notes.to_json() and "۵ میلی‌گرم" in notes.to_json()
    assert not notes.failed_sections  # mapping succeeded; verification recovered the node
    assert any(issue.code == "source_node_fallback" and issue.topic_id == "t002"
               for issue in notes.verification_report.issues)


def test_standalone_strict_section_checks_claims_against_its_own_source():
    source = NormalizedDocument.from_source_text("# Lab\nThe only measured mass was 5 mg.")
    plan = plan_from_document(source)
    settings = make_settings(note_strict_grounding=True, note_enable_deterministic_fallback=False)

    async def provider(*args, **kwargs):
        return _payload("Lab", "The only measured mass was 999 mg.")

    with patch("gamas_bot.notes.complete_json", side_effect=provider), pytest.raises(SectionGenerationError):
        asyncio.run(generate_section(plan.topics[0], source, plan, SectionWriter(settings, object())))
