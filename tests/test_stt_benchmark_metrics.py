"""Offline metric tests for the STT benchmark system (spec §55-§58)."""

from __future__ import annotations

import unittest
from pathlib import Path

from gamas_bot.stt_platform.benchmark_metrics import (
    BENCHMARK_PROFILES,
    character_error_rate,
    composite_score,
    extract_numbers,
    numeric_preservation,
    profile_from_path,
    punctuation_density,
    quality_signals,
    repetition_ratio,
    result_row,
    terminology_preservation,
    word_error_rate,
)


class EditDistanceTests(unittest.TestCase):
    def test_wer_zero_for_identical_text(self):
        self.assertEqual(word_error_rate("متن آزمایشی اینجاست", "متن آزمایشی اینجاست"), 0.0)

    def test_wer_counts_substitutions_and_deletions(self):
        self.assertAlmostEqual(
            word_error_rate("یک دو سه", "یک دو چهار"), 1 / 3
        )
        self.assertAlmostEqual(word_error_rate("یک دو سه", "یک دو"), 1 / 3)

    def test_wer_normalizes_persian_orthography(self):
        self.assertEqual(word_error_rate("كتاب درسي", "کتاب درسی"), 0.0)

    def test_empty_reference_rules(self):
        self.assertEqual(word_error_rate("", ""), 0.0)
        self.assertEqual(word_error_rate("", "متن"), float("inf"))

    def test_cer_measures_orthography(self):
        self.assertGreater(character_error_rate("کتاب", "کتالف"), 0.0)
        self.assertEqual(character_error_rate("کتاب", "کتاب"), 0.0)


class TerminologyTests(unittest.TestCase):
    def test_term_preservation_scores_presence(self):
        hypothesis = "بیمار متفورم و HbA1c و لوتیروکسین مصرف می‌کند"
        score = terminology_preservation(hypothesis, ["Metformin", "HbA1c", "Levothyroxine"])
        # Metformin is written in Persian script here, so only HbA1c matches literally.
        self.assertAlmostEqual(score, 1 / 3)

    def test_term_matching_is_case_insensitive_and_normalized(self):
        score = terminology_preservation("نمونه hba1c و METFORMIN", ["HbA1c", "metformin"])
        self.assertEqual(score, 1.0)

    def test_no_terms_is_unknown_not_perfect(self):
        self.assertIsNone(terminology_preservation("متن", []))


class NumericTests(unittest.TestCase):
    def test_extracts_latin_and_persian_digits(self):
        numbers = extract_numbers("دوز 500 mg و ۱۲۰ واحد")
        self.assertTrue(any(n.startswith("500") for n in numbers))
        self.assertIn("120", numbers)

    def test_numeric_preservation(self):
        score = numeric_preservation("دوز 500 mg هر ۱۲ ساعت", "دوز 500 mg هر 12 ساعت")
        self.assertEqual(score, 1.0)

    def test_missing_numbers_is_unknown(self):
        self.assertIsNone(numeric_preservation("بدون عدد", "بدون عدد"))


class SignalTests(unittest.TestCase):
    def test_punctuation_density(self):
        self.assertGreater(punctuation_density("سلام. خوبی؟ بله!"), 10.0)

    def test_repetition_flag(self):
        self.assertGreater(repetition_ratio("تکرار تکرار تکرار تکرار"), 0.5)

    def test_quality_signals_flag_high_rate(self):
        signals = quality_signals("کلمه " * 300, 5.0)
        self.assertIn("high_words_per_second", signals.flags)

    def test_quality_signals_never_reject_on_one_metric(self):
        signals = quality_signals("متن کوتاه.", 3600.0)
        self.assertIsInstance(signals.flags, tuple)


class ScoreTests(unittest.TestCase):
    def test_accuracy_dominates_the_composite(self):
        accurate = composite_score(
            reference="متن آزمایشی اینجاست",
            hypothesis="متن آزمایشی اینجاست",
            terms=["HbA1c"],
            reliability=0.5,
            latency_score=0.5,
            quota_efficiency=0.5,
        )
        sloppy = composite_score(
            reference="متن آزمایشی اینجاست",
            hypothesis="کاملا متفاوت است این",
            terms=["HbA1c"],
            reliability=1.0,
            latency_score=1.0,
            quota_efficiency=1.0,
        )
        self.assertGreater(accurate.total, sloppy.total)

    def test_unmeasured_components_are_not_zero_or_perfect(self):
        score = composite_score(reference=None, hypothesis="متن", terms=())
        self.assertIsNone(score.persian_accuracy)
        self.assertIsNone(score.total)

    def test_profile_from_fixture_path(self):
        path = Path("tests/fixtures/stt/benchmarks/medical_lecture/a.wav")
        self.assertEqual(profile_from_path(path), "medical_lecture")
        self.assertEqual(profile_from_path(Path("elsewhere/a.wav")), "unclassified")

    def test_all_ten_profiles_are_declared(self):
        self.assertEqual(len(BENCHMARK_PROFILES), 10)
        self.assertIn("medical_lecture", BENCHMARK_PROFILES)

    def test_result_row_has_reviewed_columns_and_no_text(self):
        row = result_row(
            provider="groq",
            model="whisper-large-v3",
            profile="numbers_and_units",
            language="fa",
            audio_duration=12.0,
            reference="دوز 500 mg",
            hypothesis="دوز 500 mg",
            terms=["mg"],
            latency_seconds=3.0,
            reliability=1.0,
        )
        for key in (
            "provider", "model", "profile", "language", "audio_duration_seconds",
            "wer", "cer", "terminology_preservation", "numeric_preservation",
            "punctuation_per_100_words", "quality_flags", "latency_seconds", "score",
        ):
            self.assertIn(key, row)
        self.assertNotIn("hypothesis", row)
        self.assertNotIn("reference", row)
        self.assertEqual(row["wer"], 0.0)
        self.assertIsNotNone(row["score"])


if __name__ == "__main__":
    unittest.main()
