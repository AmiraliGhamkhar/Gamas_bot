"""Tests for the semantic-completeness layer and the font-role correction.

The content-unit layer exists because signal-level QA (numbers, units, terms)
cannot see the worst kind of loss: a whole explanation deleted while every
number survives. These tests pin that behaviour, plus the two bugs found while
building it (a cross-namespace number comparison and a substring false
positive on Persian compounds).
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from support import make_settings, sample_notes_json

from gamas_bot import structuring as S
from gamas_bot.config import FONT_PROFILES, resolve_font_profile
from gamas_bot.docx_export import DocumentMeta, build_notes_docx, resolve_fonts
from gamas_bot.qa import run_note_qa
from gamas_bot.structuring import NoteSection, StructuredNotes
from gamas_bot.units import (
    _classify,
    _contains,
    extract_all_units,
    extract_units,
    semantic_coverage,
    unit_is_covered,
    notes_word_set,
)

RICH_SOURCE = (
    "تعریف دیابت بر پایهٔ HbA1c برابر یا بیشتر از ۶.۵ درصد انجام می‌شود. "
    "مثال: بیماری با قند ناشتای ۱۲۶ داشت و HbA1c برابر ۸.۵ درصد بود. "
    "گام اول اندازه‌گیری فشار خون ۱۲۰/۸۰ میلی‌متر جیوه است. "
    "هشدار: بدون تجویز پزشک دارو را قطع نکنید. "
    "در مقایسه با روش قدیمی، این روش سریع‌تر است. "
    "در نتیجه تشخیص زودهنگام انجام می‌شود."
)


def _notes(text: str) -> StructuredNotes:
    return StructuredNotes(
        title="d", sections=(NoteSection(heading="b", paragraphs=(text,)),)
    )


class ClassificationTests(unittest.TestCase):
    def test_cue_phrases_type_the_unit(self):
        self.assertEqual(_classify("دیابت یعنی بیماری مزمن قند خون."), "definition")
        self.assertEqual(_classify("مثال: بیماری با فشار ۱۲۰/۸۰."), "example")
        self.assertEqual(_classify("هشدار: دارو را قطع نکنید."), "warning")
        self.assertEqual(_classify("در مقایسه با روش قبلی، این روش بهتر است."), "comparison")
        self.assertEqual(_classify("گام اول اندازه‌گیری فشار خون است."), "procedure")

    def test_conclusion_beats_the_procedure_cue(self):
        # Regression: "در نتیجه" contains the substring "گام" inside
        # "زودهنگام", which retyped a conclusion as a procedure step.
        self.assertEqual(
            _classify("در نتیجه تشخیص زودهنگام انجام می‌شود."), "conclusion"
        )

    def test_weak_cues_are_reachable(self):
        # Regression: the weak comparison cue was written as "اما " with a
        # trailing space, but ``_contains`` enforces a word boundary after the
        # cue, so the space made it unmatchable and the branch was dead.
        for text in ("اما این روش همیشه جواب نمی‌دهد.", "ولی این روش هزینه دارد."):
            with self.subTest(text=text):
                self.assertEqual(_classify(text), "comparison")

    def test_contains_respects_word_boundaries(self):
        # "گام" is a real step cue, but it occurs only *inside* the compound
        # "زودهنگام", which must not match.
        self.assertFalse(_contains("تشخیص زودهنگام", "گام"))
        self.assertTrue(_contains("گام اول اندازه بگیرید", "گام"))

    def test_punctuation_does_not_split_words(self):
        words = notes_word_set("روش قدیمی، سریع‌تر است.")
        self.assertIn("قدیمی", words)
        self.assertNotIn("قدیمی،", words)

    def test_filler_is_not_an_expected_unit(self):
        units = extract_units("بله خوب. ادامه می‌دهیم.", 1)
        for unit in units:
            self.assertFalse(unit.is_expectation, unit.text)

    def test_decimal_values_are_not_split_into_two_units(self):
        units = extract_units("قیمت ۷.۲ میلی‌مول و نسبت 1.000 واحد است.", 1)
        text = " ".join(unit.text for unit in units)
        self.assertIn("۷.۲", text)
        self.assertIn("1.000", text)


class SemanticCoverageTests(unittest.TestCase):
    def test_faithful_notes_score_full_coverage(self):
        units = extract_units(RICH_SOURCE, 1)
        coverage, missing = semantic_coverage(units, RICH_SOURCE)
        self.assertEqual(coverage, 1.0, [unit.text for unit in missing])

    def test_numbers_without_explanations_score_low(self):
        """The failure mode signal-level QA cannot see."""
        units = extract_units(RICH_SOURCE, 1)
        numbers_only = "HbA1c ۶.۵ درصد. ۱۲۶. ۸.۵ درصد. ۱۲۰/۸۰."
        coverage, missing = semantic_coverage(units, numbers_only)
        self.assertLess(coverage, 0.5)
        self.assertGreater(len(missing), 2)

    def test_regression_numbers_live_in_a_separate_namespace(self):
        """Regression: numbers were compared against the *word* set, so every
        numeric unit looked missing and faithful notes scored ~0.33."""
        unit = extract_units("HbA1c زیر ۷ درصد است.", 1)[0]
        notes = "شاخص HbA1c زیر ۷ درصد قرار می‌گیرد."
        self.assertTrue(
            unit_is_covered(unit, notes_word_set(notes), frozenset({"7 %"}))
        )

    def test_compound_bp_pair_does_not_invent_a_second_number(self):
        """120/80 mmHg must not also require a phantom "80 mmHg"."""
        unit = extract_units("فشار خون ۱۲۰/۸۰ میلی‌متر جیوه ثبت شد.", 1)[0]
        self.assertIn("120/80", unit.numbers)
        self.assertNotIn("80 mmhg", unit.numbers)

    def test_source_with_no_expected_units_is_complete_by_definition(self):
        coverage, missing = semantic_coverage(extract_units("بله خوب.", 1), "بله خوب.")
        self.assertEqual(coverage, 1.0)
        self.assertEqual(missing, [])

    def test_units_carry_provenance(self):
        units = extract_all_units(["تعریف الف این است.", "مثال: بیماری با ۱۲۶."])
        self.assertTrue(all(unit.id.startswith("chunk") for unit in units))
        # Each chunk is numbered, so a unit can be traced to its source part.
        self.assertEqual([unit.source_chunk for unit in units], [1, 2])
        self.assertEqual([unit.id for unit in units][:2], ["chunk1-unit1", "chunk2-unit1"])


class QASemanticIntegrationTests(unittest.TestCase):
    def test_report_exposes_semantic_coverage(self):
        report = run_note_qa(_notes(RICH_SOURCE), [RICH_SOURCE])
        self.assertEqual(report.semantic_coverage, 1.0)
        self.assertEqual(report.total_units, report.covered_units)
        self.assertEqual(report.missing_units_count, 0)

    def test_deleted_explanation_is_reported_and_triggers_repair(self):
        source = RICH_SOURCE * 12
        report = run_note_qa(_notes("HbA1c ۶.۵ درصد. ۱۲۶."), [source])
        self.assertLess(report.semantic_coverage, 1.0)
        self.assertGreater(report.missing_units_count, 0)
        self.assertTrue(report.needs_repair)

    def test_compression_alone_is_never_a_failure(self):
        """A tightened-but-complete booklet must not be flagged."""
        source = RICH_SOURCE * 12
        condensed = (
            "تعریف دیابت بر پایهٔ HbA1c برابر ۶.۵ درصد انجام می‌شود. "
            "مثال: بیماری با قند ناشتای ۱۲۶ داشت و HbA1c برابر ۸.۵ درصد بود. "
            "گام اول اندازه‌گیری فشار خون ۱۲۰/۸۰ میلی‌متر جیوه است. "
            "هشدار: بدون تجویز پزشک دارو را قطع نکنید. "
            "در مقایسه با روش قدیمی، این روش سریع‌تر است. "
            "در نتیجه تشخیص زودهنگام انجام می‌شود."
        )
        report = run_note_qa(_notes(condensed), [source])
        self.assertLess(report.compression_ratio, 1.0)
        # Length alone is not a defect: all the content units survived.
        self.assertEqual(report.semantic_coverage, 1.0)
        self.assertFalse(report.compression_is_concerning)
        self.assertFalse(report.needs_repair)

    def test_severe_compression_with_lost_units_is_flagged(self):
        source = RICH_SOURCE * 12
        report = run_note_qa(_notes("HbA1c ۶.۵ درصد."), [source])
        self.assertLess(report.compression_ratio, 0.10)
        self.assertTrue(report.compression_is_concerning)

    def test_heavy_compression_without_loss_is_not_flagged(self):
        """The distinction this metric exists for.

        A transcript legitimately shrinks a long way when speech artifacts and
        repetition are removed. Compression alone is not a defect, so a
        tightened-but-complete booklet must pass even at a ratio far below the
        threshold — and must not cost an extra provider call.
        """
        source = RICH_SOURCE * 12
        tightened = (
            "تشخیص دیابت بر پایهٔ HbA1c برابر ۶.۵ درصد انجام می‌شود. "
            "مثال: بیماری با قند ناشتای ۱۲۶ داشت و HbA1c برابر ۸.۵ درصد بود. "
            "گام اول اندازه‌گیری فشار خون ۱۲۰/۸۰ میلی‌متر جیوه است. "
            "هشدار: بدون تجویز پزشک دارو را قطع نکنید. "
            "در مقایسه با روش قدیمی، این روش سریع‌تر است. "
            "در نتیجه تشخیص زودهنگام انجام می‌شود."
        )
        report = run_note_qa(_notes(tightened), [source])
        self.assertLess(report.compression_ratio, 0.10)
        self.assertEqual(report.semantic_coverage, 1.0)
        self.assertEqual(report.coverage, 1.0)
        self.assertFalse(report.compression_is_concerning)
        self.assertFalse(report.needs_repair)


class RepairAcceptanceTests(unittest.TestCase):
    """A repair is accepted only when it measurably restores information."""

    def _report(self, text, source):
        return run_note_qa(_notes(text), [source])

    def test_restored_content_is_accepted(self):
        source = RICH_SOURCE * 12
        before = self._report("HbA1c ۶.۵ درصد. ۱۲۶.", source)
        after = self._report(RICH_SOURCE, source)
        accepted, reason = S._repair_is_better(before, after)
        self.assertTrue(accepted, reason)
        self.assertIn("semantic", reason)

    def test_repair_that_adds_nothing_is_rejected(self):
        source = RICH_SOURCE * 12
        report = self._report("HbA1c ۶.۵ درصد.", source)
        accepted, reason = S._repair_is_better(report, report)
        self.assertFalse(accepted)
        self.assertIn("no measurable improvement", reason)

    def test_semantic_gain_is_rejected_when_numbers_regress(self):
        source = RICH_SOURCE * 12
        before = self._report("HbA1c ۶.۵ درصد. ۱۲۶. ۸.۵ درصد. ۱۲۰/۸۰.", source)
        # A repair that restores the prose but drops a signal must be rejected
        # even though its semantic coverage improved. ``semantic_coverage`` is
        # derived from the unit counts, so those are what must be set.
        regressed = replace(
            before,
            total_units=before.total_units,
            covered_units=before.total_units,  # semantic_coverage -> 1.0
            source_numbers=1,
            preserved_numbers=0,  # signal coverage -> 0.0
            missing_numbers=("500 mg",),
        )
        accepted, reason = S._repair_is_better(before, regressed)
        self.assertFalse(accepted)
        self.assertIn("regressed", reason)

    def test_repair_prompt_carries_context_and_forbids_lengthening(self):
        prompt = S.build_repair_prompt(
            "متن درس",
            ("500 mg",),
            ("gap",),
            existing="جزوهٔ قبلی",
            missing_units=("تعریف دیابت ...",),
        )
        self.assertIn("متن درس", prompt)
        self.assertIn("500 mg", prompt)
        self.assertIn("جزوهٔ قبلی", prompt)
        self.assertIn("مطالب آموزشی غایب", prompt)
        self.assertIn("طولانی‌تر کردن", S.REPAIR_REMINDER)
        self.assertIn("حدس نزنید", S.REPAIR_REMINDER)

    def test_repair_reuses_original_documents(self):
        """Regression: re-joining and re-splitting dropped the slide outline."""
        outline = "### اسلاید 1 — مقدمه"
        transcript = "این یک جملهٔ فارسی برای آزمون است. " * 200
        with patch(
            "gamas_bot.structuring._structure_chunk",
            new=AsyncMock(return_value=sample_notes_json()),
        ) as chunker:
            import asyncio

            asyncio.run(
                S.structure_presentation(
                    outline, transcript, make_settings(), max_chars=4000
                )
            )
        # Every repair request must still carry the slide outline.
        for call in chunker.await_args_list:
            self.assertIn(outline, call.args[0])

    def test_repair_prompt_bounds_the_number_of_units(self):
        units = tuple(f"unit-{index}" for index in range(50))
        prompt = S.build_repair_prompt("d", (), (), existing="", missing_units=units)
        self.assertLess(len(prompt), 4000)


class FontRoleTests(unittest.TestCase):
    def test_modern_profile_separates_latin_from_persian(self):
        profile = FONT_PROFILES["persian_modern"]
        self.assertEqual(profile["body"], "Vazirmatn")
        self.assertEqual(profile["heading"], "Vazirmatn")
        self.assertNotEqual(
            profile["latin"],
            profile["body"],
            "Vazirmatn's Latin is merged from Roboto; a dedicated face is required",
        )
        self.assertEqual(profile["fallback"], "Tahoma")

    def test_traditional_profile_pairs_b_nazanin_with_times(self):
        profile = FONT_PROFILES["traditional"]
        self.assertEqual(profile["body"], "B Nazanin")
        self.assertEqual(profile["latin"], "Times New Roman")
        self.assertEqual(profile["fallback"], "Tahoma")

    def test_every_profile_separates_or_declares_its_fallback(self):
        for name, profile in FONT_PROFILES.items():
            self.assertTrue(profile["fallback"], name)
            self.assertTrue(profile["latin"], name)

    def test_unknown_profile_falls_back_to_modern(self):
        self.assertEqual(
            resolve_font_profile("nope"), resolve_font_profile("persian_modern")
        )

    def test_rendered_runs_use_distinct_faces(self):
        import io
        import re
        import zipfile

        notes = StructuredNotes(
            title="t",
            sections=(
                NoteSection(heading="h", paragraphs=("HbA1c و Type 2 Diabetes در فارسی.",)),
            ),
        )
        payload = build_notes_docx(
            notes,
            meta=DocumentMeta(reference="X"),
            fonts=resolve_fonts(resolve_font_profile("persian_modern")),
        )
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        latin_faces = {
            re.search(r'w:ascii="([^"]+)"', run).group(1)
            for run in re.findall(r"<w:r>(.*?)</w:r>", xml, re.S)
            if 'w:ascii="Aptos"' in run
        }
        self.assertEqual(latin_faces, {"Aptos"})
        self.assertIn('w:cs="Vazirmatn"', xml)

    def test_environment_overrides_still_win(self):
        settings = make_settings(
            docx_font_profile="persian_modern", docx_font_latin="Helvetica"
        )
        self.assertEqual(settings.docx_fonts["latin"], "Helvetica")
        self.assertEqual(settings.docx_fonts["body"], "Vazirmatn")


if __name__ == "__main__":
    unittest.main()
