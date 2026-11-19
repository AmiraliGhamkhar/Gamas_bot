"""Offline contract tests for the verifier and its single bounded repair pass."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from gamas_bot import notes as n
from gamas_bot.booklet_verification import verify_and_repair, verify_booklet
from gamas_bot.document_model import NormalizedDocument, plan_from_document
from gamas_bot.llm import LLMOutputBudgetError
from tests.support import make_settings


FIRST = "طول نمونه در آزمایش برابر 75 mm است."
SECOND = "جهت تابش از سطح بیرونی نمونه تغییر می‌کند."
DATE = "تاریخ ثبت نتیجه 2024-05-17 است."


def artifact(*facts: str):
    document = NormalizedDocument.from_source_text("\n\n".join(f"# عنوان درس {chr(97 + i)}\n{fact}"
                                                                 for i, fact in enumerate(facts)))
    plan = plan_from_document(document)
    assert len(plan.topics) == len(facts)
    sections = tuple(n.NoteSection(topic.title, paragraphs=(fact,), topic_id=topic.id,
                                   parent_topic_id=topic.parent_id, section_id=f"node-{i + 1}",
                                   source_block_ids=topic.unit_ids)
                     for i, (fact, topic) in enumerate(zip(facts, plan.topics, strict=True)))
    notes = n.StructuredNotes(title="آزمایش ویژگی نمونه", sections=sections, final_review=(facts[-1],))
    return document, plan, notes


def test_fully_valid_has_all_scores_and_does_not_require_a_provider():
    document, plan, notes = artifact(FIRST, SECOND)
    with patch.object(n, "complete_json", side_effect=AssertionError("no extra call for deterministic checks")):
        report = verify_booklet(notes, document, plan)
        delivered = asyncio.run(verify_and_repair(notes, document, plan, n.SectionWriter(
            make_settings(note_compile=False, note_strict_grounding=True), object(),
        )))
    assert report.passed
    assert report.issues == []
    assert (report.coverage_score, report.structural_score, report.evidence_score) == (1, 1, 1)
    assert delivered.verification_report == report
    assert "verification_report" not in delivered.to_payload()


def test_missing_topic_blank_content_duplicate_and_broken_ref_report_exact_node():
    document, plan, notes = artifact(FIRST, SECOND, DATE)
    first, _, third = notes.sections
    duplicate = replace(first, section_id="duplicate-node")
    draft = replace(notes, sections=(first, duplicate, replace(third, heading="", paragraphs=("",),
                                                               source_block_ids=("missing-block",))))
    report = verify_booklet(draft, document, plan)
    assert not report.passed
    by_code = {(item.code, item.section_id): item for item in report.issues}
    assert ("duplicate_section", "duplicate-node") in by_code
    assert ("empty_heading", "node-3") in by_code
    assert ("empty_section", "node-3") in by_code
    assert ("empty_field", "node-3") in by_code
    assert ("broken_reference", "node-3") in by_code
    missing = {item.topic_id for item in report.issues if item.code == "missing_topic"}
    assert missing == {plan.topics[1].id, plan.topics[2].id}
    assert by_code[("broken_reference", "node-3")].source_references == list(plan.topics[2].unit_ids)
    assert report.coverage_score < 1 and report.structural_score < 1


@pytest.mark.parametrize(("statement", "issue_code"), [
    ("طول نمونه در آزمایش برابر 99 mm است.", "number_mismatch"),
    ("طول نمونه در آزمایش برابر 75 cm است.", "unit_mismatch"),
    ("طول نمونه در آزمایش برابر 75 است.", "unit_mismatch"),
    ("تاریخ ثبت نتیجه 2024-07-17 است.", "date_mismatch"),
    ("پرندگان مهاجر مسیر طولانی زمستان را بر فراز اقیانوس طی کردند.", "unsupported_claim"),
])
def test_source_evidence_mismatches_are_local(statement, issue_code):
    document, plan, notes = artifact(FIRST if issue_code != "date_mismatch" else DATE, SECOND)
    first, second = notes.sections
    draft = replace(notes, sections=(replace(first, paragraphs=(statement,)), second))
    report = verify_booklet(draft, document, plan)
    assert not report.passed and report.evidence_score < 1
    issue = next(item for item in report.issues if item.code == issue_code)
    assert issue.section_id == first.section_id
    assert issue.topic_id == first.topic_id
    assert issue.severity == "critical"
    assert issue.source_references == list(first.source_block_ids)


def test_numbers_present_elsewhere_do_not_back_a_swapped_value():
    source = "ارتفاع نمونهٔ اول 35 mm است. ارتفاع نمونهٔ دوم 55 mm است."
    document, plan, notes = artifact(source)
    draft = replace(notes, sections=(replace(notes.sections[0], paragraphs=("ارتفاع نمونهٔ اول 55 mm است.",)),))
    report = verify_booklet(draft, document, plan)
    assert any(issue.code == "number_mismatch" for issue in report.issues)


def test_missing_dimensions_and_inconsistent_terminology_are_separate():
    definition = "پراش عبارت است از تغییر مسیر موج در لبهٔ مانع."
    document, plan, notes = artifact(definition)
    first = replace(notes.sections[0], paragraphs=("مسیر موج در لبهٔ مانع بررسی شد.",))
    report = verify_booklet(replace(notes, sections=(first,)), document, plan)
    assert any(i.code == "missing_dimension" and "definitions" in i.description for i in report.issues)
    first = replace(first, definitions=(n.NoteDefinition("پراش", definition, "diffraction"),
                                        n.NoteDefinition("پراش", definition, "dispersion")))
    report = verify_booklet(replace(notes, sections=(first,)), document, plan)
    assert any(i.code == "terminology_inconsistency" for i in report.issues)


@pytest.mark.parametrize("success", [True, False])
def test_single_section_repair_success_or_lossless_local_failure(success):
    preserve = "خط‌کش آزمایش روی میز قرار دارد."
    document, plan, notes = artifact(FIRST + " " + preserve, SECOND)
    first, second = notes.sections
    wrong = "طول نمونه در آزمایش برابر 99 mm است."
    draft = replace(notes, sections=(replace(first, paragraphs=(wrong, preserve)), second))
    calls = []

    async def complete(_session, _settings, *, task, user, **kwargs):
        calls.append(task)
        assert task == "BOOKLET_REPAIR"  # mismatch is deterministic
        assert kwargs["strict_json"] and kwargs["adaptive_capacity"]
        request = json.loads(user)
        assert request["target"]["section_id"] == "node-1"
        assert request["section"]["paragraphs"] == [wrong, preserve]
        assert FIRST in request["source_evidence"]
        assert SECOND not in user and "node-2" not in user
        assert request["source_references"] == list(first.source_block_ids)
        assert any(item["code"] == "number_mismatch" and item["expected_correction"] for item in request["issues"])
        assert request["preserve"]
        if not success:
            raise LLMOutputBudgetError("synthetic truncated result")
        return {"sections": [{"heading": first.heading, "paragraphs": [FIRST, preserve]}]}

    with patch.object(n, "complete_json", complete):
        result = asyncio.run(verify_and_repair(draft, document, plan, n.SectionWriter(
            make_settings(note_compile=False, note_strict_grounding=True), object(),
        )))
    assert calls == ["BOOKLET_REPAIR"]
    assert result.sections[1] == second
    assert result.sections[1].source_block_ids == second.source_block_ids
    assert result.sections[0].section_id == first.section_id
    assert result.verification_report is not None and result.verification_report.passed is success
    if success:
        assert result.sections[0].paragraphs == (FIRST, preserve)
        assert result.quality_state == n.QUALITY_REPAIRED
    else:
        assert result.sections[0].paragraphs == (preserve,)
        assert any(issue.code == "removed_unverified_content" for issue in result.verification_report.issues)
        assert result.quality_state == n.QUALITY_DEGRADED


def test_cap_is_total_not_per_issue_or_per_pass_and_retains_other_nodes():
    facts = tuple(f"ارتفاع نمونهٔ آزمایشی در این گزارش برابر {50 + i} mm است." for i in range(6))
    document, plan, notes = artifact(*facts)
    draft = replace(notes, sections=tuple(replace(item, paragraphs=("ارتفاع نمونه در گزارش بیان شد.",))
                                          for item in notes.sections))
    calls = []

    async def complete(_session, _settings, *, task, user, **kwargs):
        assert task == "BOOKLET_REPAIR"
        data = json.loads(user)
        calls.append(data["target"]["section_id"])
        return {"sections": [data["section"], data["section"]]}  # malformed two-node edit

    settings = make_settings(note_compile=False, note_strict_grounding=True, note_max_repair_attempts=99)
    with patch.object(n, "complete_json", complete):
        retained = asyncio.run(verify_and_repair(draft, document, plan, n.SectionWriter(settings, object())))
    assert calls == ["node-1", "node-2", "node-3"]
    assert len(calls) == n.MAX_NOTE_REPAIR_ATTEMPTS
    assert not retained.verification_report.passed
    assert retained.sections == draft.sections


def test_semantic_verification_checks_only_ambiguous_assertion_and_reuses_verdict():
    source = "دارو فقط همراه غذا مصرف شود تا از عارضه گوارشی جلوگیری شود."
    document, plan, notes = artifact(source, SECOND)
    assertion = "برای جلوگیری از عارضه گوارشی دارو باید فقط با غذا مصرف شود."
    draft = replace(notes, sections=(replace(notes.sections[0], paragraphs=(assertion,)), notes.sections[1]))
    calls = []

    async def complete(_session, _settings, *, task, user, **kwargs):
        calls.append(task)
        assert task == "BOOKLET_VERIFY"
        claims = json.loads(user)["claims"]
        assert len(claims) == 1 and claims[0]["assertion"] == assertion and claims[0]["evidence"] == source
        return {"verdicts": [{"id": claims[0]["id"], "verdict": "supported"}]}

    settings = make_settings(note_compile=False, note_strict_grounding=True)
    with patch.object(n, "complete_json", complete):
        result = asyncio.run(verify_and_repair(draft, document, plan, n.SectionWriter(settings, object())))
    assert calls == ["BOOKLET_VERIFY"]
    assert result.verification_report.passed


def test_missing_topic_can_be_restored_without_sending_unrelated_sections():
    document, plan, notes = artifact(FIRST, SECOND, DATE)
    first, _, third = notes.sections
    draft = replace(notes, sections=(first, third))
    calls = []

    async def complete(_session, _settings, *, task, user, **kwargs):
        assert task == "BOOKLET_REPAIR"
        data = json.loads(user)
        assert data["target"]["topic_id"] == plan.topics[1].id and data["section"] is None
        assert FIRST not in user and DATE not in user and SECOND in data["source_evidence"]
        calls.append(data["target"]["topic_id"])
        return {"sections": [{"heading": plan.topics[1].title, "paragraphs": [SECOND]}]}

    with patch.object(n, "complete_json", complete):
        result = asyncio.run(verify_and_repair(draft, document, plan, n.SectionWriter(
            make_settings(note_compile=False, note_strict_grounding=True), object(),
        )))
    assert calls == [plan.topics[1].id]
    assert result.verification_report.passed
    assert result.sections[0] == first and result.sections[2] == third
    assert result.sections[1].source_block_ids == plan.topics[1].unit_ids


def test_over_budget_node_is_never_truncated_or_resent():
    document, plan, notes = artifact(FIRST, SECOND)
    draft = replace(notes, sections=(replace(notes.sections[0], paragraphs=("طول نمونه در گزارش مشخص شد.",)),
                                     notes.sections[1]))
    with patch.object(n, "complete_json", side_effect=AssertionError("complete node request cannot fit")):
        retained = asyncio.run(verify_and_repair(draft, document, plan, n.SectionWriter(
            make_settings(note_compile=False, note_strict_grounding=True, note_compile_max_chars=30), object(),
        )))
    assert retained.sections == draft.sections
    assert any(issue.code == "missing_number" for issue in retained.verification_report.issues)


def test_signed_measurement_error_is_deterministic():
    source = "اختلاف ثبت‌شده در این آزمایش −7 mm است."
    document, plan, notes = artifact(source)
    draft = replace(notes, sections=(replace(notes.sections[0], paragraphs=(source.replace("−7", "+7"),)),))
    report = verify_booklet(draft, document, plan)
    assert any(issue.code == "number_mismatch" for issue in report.issues)
    assert not any(issue.code == "semantic_unverified" for issue in report.issues)


def test_same_numeric_context_with_an_equivalent_unit_spelling_needs_no_llm():
    document, plan, notes = artifact(FIRST)
    draft = replace(notes, sections=(replace(notes.sections[0], paragraphs=(FIRST.replace("mm", "میلی‌متر"),)),))
    report = verify_booklet(draft, document, plan)
    assert report.passed


def test_direct_mode_withholds_unsupported_measurement_in_summary_only():
    document, plan, notes = artifact(FIRST, SECOND)
    draft = replace(notes, summary="طول نمونه در آزمایش برابر 999 mm است.")
    settings = make_settings(note_compile=False, note_strict_grounding=False)
    with patch.object(n, "complete_json", side_effect=AssertionError("deterministic local cleanup")):
        result = asyncio.run(verify_and_repair(draft, document, plan, n.SectionWriter(settings, object())))
    assert result.sections == notes.sections
    assert "999" not in result.to_json()
    assert not result.verification_report.passed
