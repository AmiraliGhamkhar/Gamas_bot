from __future__ import annotations

from gamas_bot import notes as n
from gamas_bot.booklet_synthesis import _finalize_global_learning_materials
from gamas_bot.booklet_verification import _present_dimensions, verify_booklet
from gamas_bot.document_model import NormalizedDocument
from gamas_bot.note_planning import build_deterministic_plan


def test_pedagogical_dimensions_only_required_when_supported_by_source() -> None:
    doc = NormalizedDocument.from_source_text(
        "# Metformin\n"
        "Metformin reduces hepatic gluconeogenesis; this mechanism improves insulin sensitivity.\n"
        "Common mistake: do not administer if eGFR is below 30 mL/min."
    )
    plan = build_deterministic_plan(doc)
    assert len(plan.topics) == 1
    dims = set(plan.topics[0].required_dimensions)
    assert "mechanism" in dims
    assert "exam_pitfalls" in dims
    # Unsupported dimensions must not be marked required.
    assert "classification" not in dims
    assert "signs_symptoms" not in dims


def test_verify_booklet_flags_missing_pedagogical_dimension_and_ignores_unsupported() -> None:
    doc = NormalizedDocument.from_source_text(
        "# Metformin\n"
        "Metformin reduces hepatic gluconeogenesis; this mechanism improves insulin sensitivity.\n"
        "Common mistake: do not administer if eGFR is below 30 mL/min."
    )
    plan = build_deterministic_plan(doc)
    topic = plan.topics[0]
    section = n.NoteSection(
        heading="Metformin",
        topic_id=topic.id,
        section_id="s1",
        source_block_ids=topic.unit_ids,
        paragraphs=("Metformin is a first-line agent.",),
    )
    notes = n.StructuredNotes(title="Pharmacology", sections=(section,), final_review=("Review Metformin.",))
    report = verify_booklet(notes, doc, plan, strict=False)
    missing_descs = [issue.description for issue in report.issues if issue.code == "missing_dimension"]
    assert any("mechanism" in d for d in missing_descs)
    assert any("exam_pitfalls" in d for d in missing_descs)
    assert not any("classification" in d for d in missing_descs)


def test_present_dimensions_recognizes_structured_fields() -> None:
    sec = n.NoteSection(
        heading="Topic",
        examples=("For instance, 500 mg daily.",),
        callouts=(n.NoteCallout("هشدار", "نکته مهم"),),
    )
    dims, _ = _present_dimensions((sec,))
    assert "examples_comparisons" in dims
    assert "exam_pitfalls" in dims


def test_finalize_global_learning_materials_deduplicates_without_loss() -> None:
    notes = n.StructuredNotes(
        title="Test",
        sections=(n.NoteSection(heading="A", paragraphs=("Body",)),),
        learning_objectives=("Obj 1", "Obj 1", "Obj 2"),
        final_review=("Rev 1", "Rev 1"),
        review_questions=("Q1?", "Q1?", "Q2?"),
    )
    finalized = _finalize_global_learning_materials(notes)
    assert finalized.learning_objectives == ("Obj 1", "Obj 2")
    assert finalized.final_review == ("Rev 1",)
    assert finalized.review_questions == ("Q1?", "Q2?")
