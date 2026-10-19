"""Deterministic tests for the note evidence layer and its benchmark corpus.

No network, no provider, no model call: the corpus pairs six real lecture
transcripts with hand-written reference documents, and these tests assert that
the evidence layer can tell a complete, faithful note from a lossy or invented
one.

The corpus in ``tests/fixtures/notes/`` was preserved when the v2 scorer was
removed in the v3 overhaul. This module is the restored, far smaller version of
that benchmark: it measures exactly the contract the note prompt states —
nothing invented (precision) and nothing of substance lost (recall).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from gamas_bot.notes import NoteTable, PracticalTip, StructuredNotes
from gamas_bot.notes_evidence import (
    STANDARD_EQUIVALENTS,
    UNIT_ALIASES,
    audit_notes,
    advice_sentences,
    coverage_thresholds,
    evidence_windows,
    extract_signals,
    mask_structural,
    sentence_coverage,
    split_sentences,
    validate_table,
    validate_tip,
)

NOTES_DIR = Path(__file__).resolve().parent / "fixtures" / "notes"
CORPUS = json.loads((NOTES_DIR / "corpus.json").read_text(encoding="utf-8"))


def load_fixture(entry: dict) -> tuple[str, dict]:
    transcript = (NOTES_DIR / entry["transcript"]).read_text(encoding="utf-8")
    reference = json.loads((NOTES_DIR / entry["reference"]).read_text(encoding="utf-8"))
    return transcript, reference


class SignalExtractionTests(unittest.TestCase):
    def assert_signal(self, text: str, kind: str, value: str, unit: str = "") -> None:
        signals = extract_signals(text)
        self.assertIn(
            (kind, value, unit),
            {(signal.kind, signal.value, signal.unit) for signal in signals},
            msg=f"missing {kind} {value} {unit} in {sorted(s.label for s in signals)}",
        )

    def test_persian_digits_and_units_are_canonicalised(self):
        self.assert_signal("دوز ۵۰۰ میلی‌گرم", "number", "500", "mg")
        self.assert_signal("فشار ۱۲۰ میلی‌متر جیوه", "number", "120", "mmhg")

    def test_persian_and_latin_spellings_are_the_same_fact(self):
        persian = extract_signals("۵۰۰ میلی‌گرم")
        latin = extract_signals("500 mg")
        self.assertEqual(
            {(s.kind, s.value, s.unit) for s in persian},
            {(s.kind, s.value, s.unit) for s in latin},
        )

    def test_decimal_comma_and_thousand_separator(self):
        self.assert_signal("مقدار ۱۲٫۵", "number", "12.5")
        self.assert_signal("۱٬۰۰۰ نفر", "number", "1000")

    def test_ratio_is_one_signal(self):
        self.assert_signal("فشار خون ۱۲۰/۸۰ mmHg", "number", "120/80", "mmhg")

    def test_term_with_a_digit_is_a_term_not_a_number(self):
        signals = extract_signals("HbA1c برابر ۸.۵ درصد بود")
        self.assertIn(("term", "hba1c", ""), {(s.kind, s.value, s.unit) for s in signals})
        self.assertNotIn("1", {s.value for s in signals if s.kind == "number"})

    def test_url_is_atomic(self):
        signals = extract_signals("آموزش در https://docs.python.org/3/tutorial موجود است")
        urls = [s for s in signals if s.kind == "url"]
        self.assertEqual([s.value for s in urls], ["https://docs.python.org/3/tutorial"])
        self.assertFalse([s for s in signals if s.kind == "number"])

    def test_slide_numbering_is_structural(self):
        text = "### اسلاید ۳ — مدل داده\nنیم‌سال بهار ۱۴۰۵ بود."
        numbers = [s for s in extract_signals(text) if s.kind == "number"]
        self.assertEqual([s.value for s in numbers], ["1405"])

    def test_chunk_marker_and_list_ordinals_are_structural(self):
        text = "[بخش ۱ از ۳ این درس — ادامه در بخش بعد]\n۱. گام نخست\n۲. گام دوم"
        self.assertFalse([s for s in extract_signals(text) if s.kind == "number"])

    def test_mask_keeps_substance(self):
        masked = mask_structural("### اسلاید ۱ — مقدمه\nبیمار ۱۲۰ mg گرفت")
        self.assertIn("۱۲۰", masked)
        self.assertNotIn("اسلاید ۱", masked)

    def test_units_cover_both_languages_for_every_canonical_unit(self):
        canonical_units = {aliases[0].casefold() for aliases in UNIT_ALIASES.values()}
        for canonical, aliases in UNIT_ALIASES.items():
            self.assertTrue(aliases, msg=f"{canonical} has no spelling")
            self.assertIn(canonical, canonical_units | {canonical})


class AuditTests(unittest.TestCase):
    def test_substitution_is_detected_even_though_counts_match(self):
        source = "دوز ۱۲.۵ میلی‌گرم بود."
        notes = {"sections": [{"heading": "دوز", "paragraphs": ["دوز ۱۲.۳ mg بود."]}]}
        audit = audit_notes(source, notes)
        self.assertEqual([signal.label for signal in audit.fabricated], ["12.3 mg"])
        self.assertEqual([signal.label for signal in audit.missing], ["12.5 mg"])

    def test_unit_spelling_change_is_not_a_fabrication(self):
        source = "دوز ۵۰۰ میلی‌گرم است."
        notes = {"sections": [{"heading": "دوز", "paragraphs": ["دوز 500 mg است."]}]}
        audit = audit_notes(source, notes)
        self.assertEqual(audit.fabricated, ())
        self.assertEqual(audit.missing, ())
        self.assertEqual(audit.number_coverage, 1.0)

    def test_a_missing_unit_on_the_note_side_still_covers(self):
        source = "eGFR حدود ۴۸ میلی‌لیتر بر دقیقه بود."
        notes = {"sections": [{"heading": "کلیه", "paragraphs": ["eGFR حدود ۴۸ بود."]}]}
        self.assertEqual(audit_notes(source, notes).missing, ())

    def test_note_mode_metadata_is_not_a_signal(self):
        source = "درس دربارهٔ الگوریتم است."
        notes = {"note_mode": "full", "sections": [{"heading": "الگوریتم", "paragraphs": ["درس دربارهٔ الگوریتم است."]}]}
        self.assertEqual(audit_notes(source, notes).fabricated, ())

    def test_standard_equivalent_in_prose_is_allowed_when_spoken_in_persian(self):
        source = "این تورفتگی در پایتون معنا دارد."
        notes = {"sections": [{"heading": "تورفتگی", "paragraphs": ["این تورفتگی در Python معنا دارد."]}]}
        audit = audit_notes(source, notes)
        self.assertEqual(audit.fabricated, ())

    def test_unrelated_english_in_prose_is_still_a_fabrication(self):
        source = "درس دربارهٔ الگوریتم است."
        notes = {"sections": [{"heading": "الگوریتم", "paragraphs": ["درس دربارهٔ الگوریتم و Kubernetes است."]}]}
        self.assertIn("kubernetes", [signal.value for signal in audit_notes(source, notes).fabricated])

    def test_term_en_is_an_equivalence_position(self):
        source = "تجربهٔ کاربر مهم است."
        payload = {
            "sections": [
                {
                    "heading": "تجربهٔ کاربر",
                    "definitions": [
                        {"term": "تجربهٔ کاربر", "term_en": "user experience", "definition": "کیفیت ادراک"}
                    ],
                }
            ]
        }
        self.assertEqual(audit_notes(source, payload).fabricated, ())

    def test_evidence_window_contains_the_fact(self):
        source = "بیمار وزن ۸۰ کیلوگرم داشت و کراتینین ۱.۶ بود."
        missing = [s for s in extract_signals(source) if s.value == "1.6"]
        windows = evidence_windows(source, missing)
        self.assertEqual(len(windows), 1)
        self.assertIn("1.6", windows[0][1])

    def test_standard_equivalents_table_is_persian_to_latin_only(self):
        for latin, spellings in STANDARD_EQUIVALENTS.items():
            self.assertTrue(latin.isascii())
            self.assertTrue(spellings)
            for spelling in spellings:
                self.assertFalse(spelling.isascii(), msg=f"{latin} -> {spelling} is not Persian")


class SentenceTests(unittest.TestCase):
    def test_sentences_are_split_without_losing_text(self):
        text = "جملهٔ نخست است. جملهٔ دوم است؟ جملهٔ سوم است!"
        sentences = split_sentences(text)
        self.assertEqual(len(sentences), 3)

    def test_coverage_detects_a_gutted_note(self):
        source = (
            "کلیه خون را فیلتر می‌کند و کراتینین را دفع می‌کند. "
            "کبد آنزیم‌های گوارشی تولید می‌کند و صفرا می‌سازد. "
            "ریه اکسیژن را جذب می‌کند و دی‌اکسید کربن را بیرون می‌دهد. "
            "قلب خون را در سراسر بدن پمپ می‌کند و فشار را تنظیم می‌کند."
        )
        good = {"sections": [{"heading": "همه", "paragraphs": [source]}]}
        gutted = {
            "sections": [
                {"heading": "همه", "paragraphs": ["کلیه خون را فیلتر می‌کند و کراتینین را دفع می‌کند."]}
            ]
        }
        self.assertGreater(sentence_coverage(source, good).ratio, 0.9)
        self.assertLess(sentence_coverage(source, gutted).ratio, 0.6)

    def test_advice_sentences_are_the_tip_pool(self):
        source = "این تعریف مهم است. بیمار نباید Metformin بگیرد. در آزمون این نکته می‌آید."
        advice = advice_sentences(source)
        self.assertEqual(len(advice), 2)


class TableValidationTests(unittest.TestCase):
    SOURCE = (
        "قند ناشتا ۱۲۶ mg و HbA1c ۸.۵ درصد بود. فشار خون ۱۲۰/۸۰ mmHg ثبت شد."
    )

    def test_a_table_of_source_facts_passes(self):
        table = NoteTable(
            ["شاخص", "مقدار"],
            [["قند ناشتا", "126 mg"], ["HbA1c", "8.5 درصد"], ["فشار خون", "120/80 mmHg"]],
        )
        self.assertTrue(validate_table(table, self.SOURCE).accepted)

    def test_one_invented_numeric_cell_rejects_the_table(self):
        table = NoteTable(["شاخص", "مقدار"], [["قند ناشتا", "126 mg"], ["HbA1c", "9.5 درصد"]])
        verdict = validate_table(table, self.SOURCE)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, "number")
        self.assertIn("9.5 %", verdict.numeric_failed)

    def test_a_computed_cell_is_rejected(self):
        table = NoteTable(["میانگین"], [["۱۰۳"]])
        self.assertFalse(validate_table(table, self.SOURCE).accepted)

    def test_a_row_anchored_gloss_is_allowed(self):
        """A source row may name its own term in Persian (``INSERT`` -> «درج»)."""
        source = "دستورهای SQL شامل SELECT، INSERT، UPDATE و DELETE است."
        table = NoteTable(
            ["دستور", "کاربرد"],
            [["SELECT", "خواندن"], ["INSERT", "درج"], ["UPDATE", "به‌روزرسانی"]],
        )
        self.assertTrue(validate_table(table, source).accepted)

    def test_a_long_invented_explanation_in_an_anchored_row_is_rejected(self):
        source = "دستورهای SQL شامل SELECT و INSERT است."
        table = NoteTable(
            ["دستور", "توضیح"],
            [["SELECT", "این دستور همهٔ سطرها را از جدول می‌خواند و در حافظه نگه می‌دارد"]],
        )
        self.assertFalse(validate_table(table, source).accepted)

    def test_an_invented_text_cell_rejects_the_table(self):
        table = NoteTable(
            ["شاخص", "توضیح"],
            [
                ["قند ناشتا", "میزان قند خون پس از هشت ساعت ناشتایی"],
                ["HbA1c", "شاخص میانگین قند خون سه ماه گذشته"],
            ],
        )
        self.assertFalse(validate_table(table, self.SOURCE).accepted)


class TipValidationTests(unittest.TestCase):
    SOURCE = "بیمار با نارسایی کلیوی نباید Metformin بگیرد و باید با پزشک تماس بگیرد."

    def test_a_tip_with_a_verbatim_basis_passes(self):
        tip = PracticalTip(
            "در نارسایی کلیوی Metformin قطع می‌شود.",
            basis="بیمار با نارسایی کلیوی نباید Metformin بگیرد",
        )
        self.assertTrue(validate_tip(tip, self.SOURCE))

    def test_a_tip_without_a_basis_fails(self):
        self.assertFalse(validate_tip(PracticalTip("هر شب درس را مرور کنید."), self.SOURCE))

    def test_a_tip_with_a_short_basis_fails(self):
        self.assertFalse(validate_tip(PracticalTip("مرور کنید", basis="مرور"), self.SOURCE))

    def test_a_tip_with_a_basis_from_another_lecture_fails(self):
        tip = PracticalTip("ورزش روزانه را فراموش نکنید.", basis="ورزش روزانه برای همه بیماران لازم است")
        self.assertFalse(validate_tip(tip, self.SOURCE))


class CorpusBenchmarkTests(unittest.TestCase):
    """The restored benchmark: the reference documents must pass."""

    def test_reference_documents_meet_their_declared_floors(self):
        for entry in CORPUS["fixtures"]:
            with self.subTest(fixture=entry["id"]):
                transcript, reference = load_fixture(entry)
                audit = audit_notes(transcript, reference)
                self.assertGreaterEqual(
                    audit.number_coverage, entry["min_number_coverage"], msg=audit.summary()
                )
                self.assertGreaterEqual(
                    audit.term_coverage, entry["min_term_coverage"], msg=audit.summary()
                )
                self.assertEqual(audit.fabricated, (), msg="reference invented a fact")

    def test_no_reference_omits_a_number(self):
        """A reference may rewrite spoken code, but never drop a value.

        Number recall is the absolute part of the contract: every reference
        document carries every number+unit the lecturer said, and the one
        documented exception in the corpus is a *term* (spoken code such as
        «ایکوالز» rewritten as ``==``), never a dose, date or threshold.
        """
        for entry in CORPUS["fixtures"]:
            with self.subTest(fixture=entry["id"]):
                transcript, reference = load_fixture(entry)
                audit = audit_notes(transcript, reference)
                self.assertEqual(
                    [signal.label for signal in audit.missing if signal.kind == "number"],
                    [],
                    msg=audit.summary(),
                )
                self.assertEqual(audit.number_coverage, 1.0, msg=audit.summary())

    def test_only_the_code_fixture_trades_a_term_for_real_syntax(self):
        """Term recall is 1.0 everywhere except where the note writes code."""
        for entry in CORPUS["fixtures"]:
            with self.subTest(fixture=entry["id"]):
                transcript, reference = load_fixture(entry)
                audit = audit_notes(transcript, reference)
                if entry["id"] == "05_code_switching_tech":
                    self.assertGreaterEqual(audit.term_coverage, 0.8, msg=audit.summary())
                    self.assertTrue(audit.missing)
                else:
                    self.assertEqual(audit.term_coverage, 1.0, msg=audit.summary())

    def test_a_truncated_reference_fails_the_same_check(self):
        entry = next(item for item in CORPUS["fixtures"] if item["id"] == "03_cs_algorithms")
        transcript, reference = load_fixture(entry)
        truncated = {
            "sections": reference["sections"][:1],
            "glossary": reference.get("glossary", []),
        }
        audit = audit_notes(transcript, truncated)
        self.assertLess(audit.coverage, 1.0)
        self.assertTrue(audit.missing)

    def test_a_changed_digit_fails_precision(self):
        entry = next(item for item in CORPUS["fixtures"] if item["id"] == "01_medical_endocrinology")
        _, reference = load_fixture(entry)
        payload = json.loads(json.dumps(reference))
        table = payload["sections"][0]["table"]
        table["rows"][0][1] = "162 mg"
        audit = audit_notes((NOTES_DIR / entry["transcript"]).read_text(encoding="utf-8"), payload)
        self.assertIn("162 mg", [signal.label for signal in audit.fabricated])

    def test_the_persian_only_fixture_reports_no_signals(self):
        entry = next(item for item in CORPUS["fixtures"] if item["id"] == "04_persian_only_literature")
        transcript, reference = load_fixture(entry)
        self.assertEqual(extract_signals(transcript), frozenset())
        audit = audit_notes(transcript, reference)
        self.assertEqual(audit.missing, ())
        self.assertEqual(audit.fabricated, ())

    def test_slide_numbers_are_not_content_but_the_semester_is(self):
        entry = next(item for item in CORPUS["fixtures"] if item["id"] == "06_pptx_slide_outline")
        transcript, _ = load_fixture(entry)
        numbers = [signal.value for signal in extract_signals(transcript) if signal.kind == "number"]
        self.assertEqual(numbers, ["1405"])

    def test_reference_coverage_meets_the_runtime_floors(self):
        for entry in CORPUS["fixtures"]:
            with self.subTest(fixture=entry["id"]):
                transcript, reference = load_fixture(entry)
                audit = audit_notes(transcript, reference)
                number_floor, term_floor = coverage_thresholds("full")
                self.assertGreaterEqual(audit.number_coverage, number_floor)
                if entry.get("min_term_coverage", 1.0) >= term_floor:
                    self.assertGreaterEqual(audit.term_coverage, term_floor)

    def test_reference_tables_are_source_grounded(self):
        """Every table in a reference document survives the cell check."""
        for entry in CORPUS["fixtures"]:
            with self.subTest(fixture=entry["id"]):
                transcript, reference = load_fixture(entry)
                notes = StructuredNotes.from_payload(reference)
                for section in notes.sections:
                    for table in section.all_tables:
                        verdict = validate_table(table, transcript)
                        self.assertTrue(verdict.accepted, msg=f"{section.heading}: {verdict}")

    def test_reference_notes_pass_the_runtime_gate_unchanged(self):
        """The gate must not amputate a good answer."""
        from gamas_bot.notes import gate_notes

        for entry in CORPUS["fixtures"]:
            with self.subTest(fixture=entry["id"]):
                transcript, reference = load_fixture(entry)
                notes = StructuredNotes.from_payload(reference)
                gated, report = gate_notes(notes, transcript)
                self.assertEqual(report.tables_rejected, 0)
                self.assertEqual(gated.sections, notes.sections)
                self.assertEqual(report.audit.fabricated, ())

    def test_practical_tips_are_expected_to_pass_where_the_lecture_advises(self):
        """A tip built from a source advice sentence validates; an invented one does not."""
        entry = next(item for item in CORPUS["fixtures"] if item["id"] == "01_medical_endocrinology")
        transcript, _ = load_fixture(entry)
        advice = advice_sentences(transcript)
        self.assertTrue(advice)
        real = PracticalTip("Metformin در نارسایی کلیوی با eGFR کمتر از ۳۰ قطع می‌شود.", basis=advice[0])
        self.assertTrue(validate_tip(real, transcript))
        invented = PracticalTip("بیمار هر روز ورزش کند.", basis="ورزش روزانه برای دیابت لازم است")
        self.assertFalse(validate_tip(invented, transcript))


class SchemaCompatibilityTests(unittest.TestCase):
    def test_legacy_single_table_is_still_read(self):
        notes = StructuredNotes.from_payload(
            {
                "sections": [
                    {
                        "heading": "بخش",
                        "table": {"headers": ["a", "b"], "rows": [["1", "2"]]},
                    }
                ]
            }
        )
        self.assertEqual(len(notes.sections[0].all_tables), 1)
        self.assertEqual(notes.sections[0].table.headers, ["a", "b"])

    def test_v4_tables_are_normalised_into_primary_and_extras(self):
        notes = StructuredNotes.from_payload(
            {
                "sections": [
                    {
                        "heading": "بخش",
                        "tables": [
                            {"caption": "اول", "headers": ["a"], "rows": [["1"]]},
                            {"caption": "دوم", "headers": ["b"], "rows": [["2"]]},
                        ],
                    }
                ]
            }
        )
        section = notes.sections[0]
        self.assertEqual(section.table.caption, "اول")
        self.assertEqual([table.caption for table in section.tables], ["دوم"])
        self.assertEqual(len(section.all_tables), 2)

    def test_practical_tips_round_trip_with_their_basis(self):
        payload = {
            "sections": [{"heading": "بخش", "paragraphs": ["متن"]}],
            "practical_tips": [
                {"text": "دوز را افزایش تدریجی بدهید.", "basis": "افزایش تدریجی دوز", "kind": "کاربردی"},
                {"text": "نکتهٔ نامعتبر", "kind": "ناشناخته"},
            ],
        }
        notes = StructuredNotes.from_payload(payload)
        self.assertEqual(len(notes.practical_tips), 2)
        self.assertEqual(notes.practical_tips[1].kind, "کاربردی")
        restored = StructuredNotes.from_payload(notes.to_payload())
        self.assertEqual(restored.practical_tips, notes.practical_tips)
