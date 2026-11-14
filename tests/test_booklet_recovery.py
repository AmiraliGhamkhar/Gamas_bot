"""Focused regressions for source-faithful booklet recovery.

These fixtures are synthetic and non-sensitive.  They exercise the recovery
policy without contacting an LLM provider.
"""

from __future__ import annotations

import unittest
from unittest import mock

from gamas_bot import notes as notes_mod
from gamas_bot.llm import LLMError, LLMOutputBudgetError, LLMResponseError
from gamas_bot.notes import (
    QUALITY_DEGRADED,
    QUALITY_RECOVERED,
    REPAIR_MISSING_NUMBER,
    REPAIR_MISSING_TERM,
    REPAIR_UNSUPPORTED_CLAIM,
    REPAIR_WRONG_NUMBER,
    NoteSection,
    NoteTable,
    StructuredNotes,
    build_repair_issues,
    build_repair_document,
    deterministic_fallback_notes,
    gate_notes,
    repair_improves,
    structure_document,
)
from tests.support import make_settings


SOURCE = (
    "دوز آغازین 500 mg است. "
    "Metformin همراه غذا مصرف می‌شود. "
    "نسبت HbA1c برابر 8.5 درصد ثبت شد."
)


def notes_with(*paragraphs: str) -> StructuredNotes:
    return StructuredNotes(
        title="جزوهٔ آزمایشی",
        sections=(NoteSection(heading="درمان", paragraphs=paragraphs),),
    )


class RepairIssueTests(unittest.TestCase):
    def test_perfect_booklet_has_no_repair_issue(self):
        _, report = gate_notes(notes_with(SOURCE), SOURCE)
        self.assertEqual(build_repair_issues(notes_with(SOURCE), report, SOURCE), ())

    def test_fabricated_number_has_explicit_wrong_number_command(self):
        notes, report = gate_notes(notes_with("دوز آغازین 750 mg است."), SOURCE)
        issues = build_repair_issues(notes, report, SOURCE)
        wrong = next(issue for issue in issues if issue.code == REPAIR_WRONG_NUMBER)
        self.assertEqual(wrong.severity, "critical")
        self.assertEqual(wrong.section_id, "s1")
        self.assertIn("750", wrong.current_text)
        self.assertEqual(wrong.action, "REMOVE_OR_REWRITE_FROM_SOURCE")

    def test_unsupported_claim_has_sentence_level_command(self):
        claim = "این دارو با تغییر ناگهانی همهٔ بیماری‌ها را بدون عارضه درمان می‌کند."
        notes, report = gate_notes(notes_with(SOURCE, claim), SOURCE)
        issues = build_repair_issues(notes, report, SOURCE)
        unsupported = next(issue for issue in issues if issue.code == REPAIR_UNSUPPORTED_CLAIM)
        self.assertEqual(unsupported.field, "sections[0].paragraphs[1]")
        self.assertEqual(unsupported.current_text, claim)
        self.assertEqual(unsupported.action, "REMOVE_OR_REWRITE_FROM_SOURCE")

    def test_unsupported_global_summary_has_a_targeted_command(self):
        claim = "پژوهش تازهٔ ساختگی عوارض ناشناخته و نتایج قطعی شگفت‌انگیز را تأیید می‌کند."
        notes = StructuredNotes(
            title="جزوهٔ آزمایشی",
            summary=claim,
            sections=(NoteSection(heading="درمان", paragraphs=(SOURCE,)),),
        )
        _, report = gate_notes(notes, SOURCE)
        issue = next(
            item for item in build_repair_issues(notes, report, SOURCE)
            if item.code == REPAIR_UNSUPPORTED_CLAIM
        )
        self.assertEqual(issue.field, "summary")
        self.assertEqual(issue.current_text, claim)

    def test_missing_number_and_term_have_targeted_commands(self):
        notes, report = gate_notes(notes_with("دوز آغازین دارو بررسی شد."), SOURCE)
        codes = {issue.code for issue in build_repair_issues(notes, report, SOURCE)}
        self.assertIn(REPAIR_MISSING_NUMBER, codes)
        self.assertIn(REPAIR_MISSING_TERM, codes)

    def test_repair_document_is_bounded_and_command_driven(self):
        notes, report = gate_notes(notes_with("دوز آغازین 750 mg است."), SOURCE)
        document = build_repair_document(notes, report, SOURCE, pass_name="claim_repair")
        self.assertIn("فرمان تعمیر", document)
        self.assertIn(REPAIR_WRONG_NUMBER, document)
        self.assertIn("کمترین بازهٔ لازم", document)
        self.assertIn("750 mg", document)
        self.assertLess(len(document), 12_000)

    def test_removing_hallucination_at_same_coverage_is_accepted(self):
        current, current_report = gate_notes(
            notes_with("دوز آغازین 500 mg است.", "دوز نگه‌دارنده 750 mg است."), SOURCE
        )
        candidate, candidate_report = gate_notes(notes_with("دوز آغازین 500 mg است."), SOURCE)
        self.assertGreater(len(current_report.audit.fabricated), 0)
        self.assertEqual(current_report.audit.coverage, candidate_report.audit.coverage)
        self.assertTrue(repair_improves(candidate_report, current_report))
        self.assertTrue(current.has_content and candidate.has_content)

    def test_repair_that_introduces_hallucination_is_rejected(self):
        _, current_report = gate_notes(notes_with("دوز آغازین 500 mg است."), SOURCE)
        _, candidate_report = gate_notes(notes_with("دوز آغازین 999 mg است."), SOURCE)
        self.assertFalse(repair_improves(candidate_report, current_report))


class FallbackAndCompileTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_chunk_failure_preserves_successful_chunks(self):
        chunks = ("بخش موفق اول.", "بخش ناموفق میانی.", "بخش موفق آخر.")

        async def fake_piece(_session, _settings, text, **_kwargs):
            if "ناموفق" in text:
                raise LLMError("synthetic provider failure")
            return StructuredNotes(
                title="مدل",
                sections=(NoteSection(heading=text[:10], paragraphs=(text,)),),
            )

        settings = make_settings(note_strict_grounding=False, note_compile=False)
        with (
            mock.patch.object(notes_mod, "split_transcript", return_value=list(chunks)),
            mock.patch.object(notes_mod, "_structure_piece", new=fake_piece),
        ):
            notes = await structure_document("منبع غیرخالی", settings, object())
        text = notes.to_json()
        self.assertEqual(notes.quality_state, QUALITY_RECOVERED)
        self.assertIn("موفق اول", text)
        self.assertIn("ناموفق میانی", text)
        self.assertIn("موفق آخر", text)

    async def test_chunk_gate_cleanup_is_recorded_as_repaired(self):
        chunks = (
            "محتوای معتبر نخست فقط از منبع آمده است.",
            "محتوای معتبر دوم فقط از منبع آمده است.",
        )

        async def fake_piece(_session, _settings, text, **_kwargs):
            table = None
            if text == chunks[0]:
                table = NoteTable(
                    headers=["عنوان", "مقدار"],
                    rows=[["محتوای معتبر", "999"], ["منبع", "999"]],
                )
            return StructuredNotes(
                title="مدل",
                sections=(NoteSection(heading="بخش", paragraphs=(text,), table=table),),
            )

        settings = make_settings(note_strict_grounding=True, note_compile=False)
        with (
            mock.patch.object(notes_mod, "split_transcript", return_value=list(chunks)),
            mock.patch.object(notes_mod, "_structure_piece", new=fake_piece),
        ):
            notes = await structure_document(" ".join(chunks), settings, object())
        self.assertEqual(notes.quality_state, notes_mod.QUALITY_REPAIRED)
        self.assertFalse(any(section.all_tables for section in notes.sections))

    async def test_provider_failure_uses_deterministic_chunk_fallback(self):
        async def failed_piece(*_args, **_kwargs):
            raise LLMError("synthetic provider failure")

        settings = make_settings(note_strict_grounding=False, note_compile=False)
        with mock.patch.object(notes_mod, "_structure_piece", new=failed_piece):
            notes = await structure_document("فقط متن منبع 500 mg.", settings, object())
        self.assertEqual(notes.quality_state, QUALITY_RECOVERED)
        self.assertIn("500 mg", notes.to_json())

    async def test_malformed_model_json_recovers_to_source_booklet(self):
        async def malformed(*_args, **_kwargs):
            raise LLMResponseError("malformed JSON")

        settings = make_settings(note_strict_grounding=False, note_compile=False)
        with mock.patch.object(notes_mod, "complete_json", new=malformed):
            notes = await structure_document("منبع سالم با 500 mg.", settings, object())
        self.assertEqual(notes.quality_state, QUALITY_RECOVERED)
        self.assertIn("500 mg", notes.to_json())

    async def test_truncated_model_response_recovers_to_source_booklet(self):
        async def truncated(*_args, **_kwargs):
            raise LLMOutputBudgetError("truncated")

        settings = make_settings(note_strict_grounding=False, note_compile=False)
        with mock.patch.object(notes_mod, "complete_json", new=truncated):
            notes = await structure_document("منبع سالم با HbA1c.", settings, object())
        self.assertEqual(notes.quality_state, QUALITY_RECOVERED)
        self.assertIn("HbA1c", notes.to_json())

    async def test_compile_loss_of_coverage_is_rejected(self):
        baseline = notes_with(SOURCE)
        compiled = notes_with("دوز آغازین بررسی شد.")
        _, report = gate_notes(baseline, SOURCE)
        settings = make_settings(note_strict_grounding=True)
        accepted = notes_mod._gate_compiled(
            compiled, baseline, report, SOURCE, settings, mode="full"
        )
        self.assertIsNone(accepted)

    async def test_compile_fabrication_is_rejected(self):
        baseline = notes_with("دوز آغازین 500 mg است.")
        compiled = notes_with("دوز آغازین 999 mg است.")
        _, report = gate_notes(baseline, SOURCE)
        settings = make_settings(note_strict_grounding=True)
        accepted = notes_mod._gate_compiled(
            compiled, baseline, report, SOURCE, settings, mode="full"
        )
        self.assertIsNone(accepted)


class StrictFillAndFinalSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_grounding_still_executes_completeness_fill(self):
        draft = {
            "title": "درس",
            "summary": "خلاصه",
            "key_points": ["نکته"],
            "glossary": [{"term": "HbA1c", "definition": "اصطلاح منبع"}],
            "sections": [{"heading": "دوز", "paragraphs": ["دوز آغازین 500 mg است."]}],
        }
        fill = {
            "sections": [{"heading": "مصرف", "paragraphs": ["Metformin همراه غذا مصرف می‌شود."]}],
        }
        calls: list[dict] = []

        async def fake_complete_json(_session, _settings, **kwargs):
            calls.append(kwargs)
            return fill if kwargs["system"] == notes_mod.COVERAGE_FILL_SYSTEM_PROMPT else draft

        settings = make_settings(
            note_strict_grounding=True,
            note_quality_gate="repair",
            note_max_repair_attempts=0,
            note_completeness_fill=True,
            note_compile=False,
        )
        with mock.patch.object(notes_mod, "complete_json", new=fake_complete_json):
            notes = await structure_document(SOURCE, settings, object())
        self.assertTrue(any(call["system"] == notes_mod.COVERAGE_FILL_SYSTEM_PROMPT for call in calls))
        self.assertIn("Metformin", notes.to_json())

    async def test_unrepairable_claim_is_removed_and_final_artifact_is_degraded(self):
        source = "گزارهٔ معتبر اول در منبع وجود دارد. گزارهٔ معتبر دوم نیز در منبع وجود دارد."
        generated = {
            "title": "درس",
            "sections": [{"heading": "بخش", "paragraphs": [
                "گزارهٔ معتبر اول در منبع وجود دارد.",
                "این جملهٔ طولانی و ساختگی هیچ پشتوانه‌ای در منبع آموزشی ندارد.",
            ]}],
        }

        async def fake_complete_json(*_args, **_kwargs):
            return generated

        settings = make_settings(note_strict_grounding=True, note_compile=False)
        with mock.patch.object(notes_mod, "complete_json", new=fake_complete_json):
            notes = await structure_document(source, settings, object())
        self.assertEqual(notes.quality_state, QUALITY_DEGRADED)
        payload = notes.to_json()
        self.assertIn("گزارهٔ معتبر", payload)
        self.assertNotIn("ساختگی", payload)

    def test_empty_source_is_a_real_failure(self):
        with self.assertRaises(notes_mod.StructuringError):
            deterministic_fallback_notes("  \n")

    def test_deterministic_fallback_preserves_order_numbers_terms_and_locators(self):
        source = "[صفحهٔ ۱]\n\nMetformin با 500 mg.\n\n[صفحهٔ ۲]\n\nHbA1c برابر 8.5 درصد است."
        notes = deterministic_fallback_notes(source)
        body = "\n".join(paragraph for section in notes.sections for paragraph in section.paragraphs)
        self.assertLess(body.index("Metformin"), body.index("HbA1c"))
        self.assertIn("500 mg", body)
        self.assertIn("8.5", body)
        self.assertIn("صفحهٔ ۱", " ".join(notes.sections[0].source_locators))
        self.assertEqual(notes.quality_state, QUALITY_RECOVERED)


if __name__ == "__main__":
    unittest.main()
