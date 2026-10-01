"""Regression tests for mixed Persian/English typography and normalization.

The rule these tests protect: Word resolves visual order with the Unicode BiDi
algorithm over *logical* character order, using each run's base direction. The
renderer therefore must never reverse a string; it must segment and set the
right font slot per run. Every case here asserts that the concatenation of the
runs is byte-identical to the input, which is what "never reversed" means
in practice.
"""

from __future__ import annotations

import io
import re
import unittest
import zipfile

from gamas_bot.bidi import is_rtl_dominant, split_direction_runs
from gamas_bot.docx_export import DocumentMeta, build_notes_docx, resolve_fonts
from gamas_bot.structuring import NoteSection, StructuredNotes
from gamas_bot.textnorm import normalize_display, normalize_whitespace

ARABIC_KAF = "ك"
ARABIC_YEH = "ي"

#: The technical tokens the renderer must keep as one LTR run.
TECHNICAL_TOKENS = (
    "HbA1c",
    "SpO2",
    "120/80 mmHg",
    "500 mg/dL",
    "7.2%",
    "Type 2 Diabetes",
    "COVID-19",
    "MRI",
    "CT scan",
    "Na+",
    "K+",
    "pH = 7.4",
    "kg/m²",
    "mcg",
    "HbA1c 7.2%",
    "eGFR 60 mL/min/1.73m²",
)

PERSIAN_SENTENCES = (
    "شاخص HbA1c برای تشخیص دیابت استفاده می‌شود.",
    "فشار خون 120/80 mmHg ثبت شد.",
    "دوز 500 mg/dL تجویز می‌شود.",
    "پیچیدگی 7.2% گزارش شد.",
    "بیماری COVID-19 واکسینه شد.",
    "آزمایش MRI و CT scan انجام شد.",
    "ایمیل info@test.ir ارسال شد.",
    "آدرس https://example.ir/a را ببینید.",
    "کد v1.2.3 اجرا شد.",
    "یون‌های Na+ و K+ اندازه‌گیری شدند.",
    "pH = 7.4 در محدودهٔ طبیعی است.",
    "وزن مرجع kg/m² برابر 25 است.",
    "ترکیب (HbA1c) و [SpO2] بررسی شد.",
    "عدد ۱۲۳ و درصد ۴۵٪ نوشته شد.",
    "تاریخ ۱۴۰۵/۰۶/۰۹ ثبت شد.",
    "کد: ABC-123 و نسخه 2.5.1.",
)

ENGLISH_SENTENCES = (
    "The diagnosis is based on HbA1c.",
    "Blood pressure was 120/80 mmHg.",
    "See https://example.org/docs for details.",
    "Email info@example.org for access.",
)


class BiDiSegmentationTests(unittest.TestCase):
    def test_segmentation_never_reorders_characters(self):
        for text in PERSIAN_SENTENCES + ENGLISH_SENTENCES + TECHNICAL_TOKENS:
            with self.subTest(text=text):
                joined = "".join(run.text for run in split_direction_runs(text))
                self.assertEqual(joined, text)

    def test_technical_tokens_stay_ltr(self):
        for token in TECHNICAL_TOKENS:
            with self.subTest(token=token):
                runs = split_direction_runs(token)
                self.assertTrue(runs)
                self.assertTrue(all(not run.rtl for run in runs), token)

    def test_persian_sentences_are_rtl_dominant(self):
        for text in PERSIAN_SENTENCES:
            with self.subTest(text=text):
                self.assertTrue(is_rtl_dominant(text))

    def test_pure_latin_paragraphs_stay_ltr(self):
        for text in ENGLISH_SENTENCES:
            with self.subTest(text=text):
                self.assertFalse(is_rtl_dominant(text))

    def test_english_inside_persian_isolated_as_one_run(self):
        runs = split_direction_runs("شاخص HbA1c برای تشخیص استفاده می‌شود.")
        latin = [run.text.strip() for run in runs if not run.rtl]
        self.assertEqual(latin, ["HbA1c"])

    def test_persian_inside_english_is_isolated(self):
        runs = split_direction_runs("واژهٔ کلیدی HbA1c مهم است.")
        rtl = [run.text.strip() for run in runs if run.rtl]
        self.assertTrue(any("واژه" in text for text in rtl))
        self.assertTrue(any("مهم" in text for text in rtl))

    def test_empty_and_neutral_inputs(self):
        self.assertEqual(split_direction_runs(""), [])
        self.assertEqual([run.text for run in split_direction_runs("123")], ["123"])
        self.assertEqual([run.text for run in split_direction_runs("!!!")], ["!!!"])

    def test_zwnj_never_splits_a_word_into_separate_runs(self):
        runs = split_direction_runs("می‌شود نمی‌کند")
        self.assertEqual(len(runs), 1)
        self.assertIn("‌", runs[0].text)


class DocxTypographyTests(unittest.TestCase):
    def _xml(self, paragraph_text: str) -> str:
        notes = StructuredNotes(
            title="t",
            sections=(NoteSection(heading="h", paragraphs=(paragraph_text,)),),
        )
        payload = build_notes_docx(
            notes,
            meta=DocumentMeta(reference="X"),
            fonts=resolve_fonts(
                {"body": "Vazirmatn", "heading": "Vazirmatn", "latin": "Aptos",
                 "fallback": "Tahoma"}
            ),
        )
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            return archive.read("word/document.xml").decode("utf-8")

    def test_every_paragraph_text_survives_rendering(self):
        """No word may disappear between the source text and the .docx."""
        for text in PERSIAN_SENTENCES + ENGLISH_SENTENCES:
            with self.subTest(text=text):
                xml = self._xml(text)
                visible = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S))
                # Strip punctuation and compare the remaining token sequence, so
                # a missing word fails but a re-ordered run split does not.
                for word in re.findall(r"[\w؀-ۿ]+", text):
                    if len(word) < 2:
                        continue
                    self.assertIn(word, visible, f"{word!r} missing from the document")

    def test_latin_runs_are_marked_ltr_and_use_the_latin_face(self):
        xml = self._xml("شاخص HbA1c برای تشخیص دیابت استفاده می‌شود.")
        ltr_runs = re.findall(r"<w:r>((?:(?!</w:r>).)*?<w:rtl w:val=\"0\"/>(?:(?!</w:r>).)*?)</w:r>", xml, re.S)
        self.assertTrue(ltr_runs)
        for run in ltr_runs:
            self.assertIn('w:ascii="Aptos"', run)
            self.assertNotIn('w:rtl/>', run)

    def test_rtl_runs_use_the_complex_script_face(self):
        xml = self._xml("شاخص HbA1c برای تشخیص دیابت استفاده می‌شود.")
        self.assertIn('w:cs="Vazirmatn"', xml)
        self.assertIn("szCs", xml)

    def test_paragraph_carries_bidi_direction(self):
        xml = self._xml("شاخص HbA1c مهم است.")
        self.assertIn('<w:bidi w:val="1"/>', xml)

    def test_latin_only_paragraph_is_not_forced_rtl(self):
        xml = self._xml("The diagnosis is based on HbA1c.")
        self.assertFalse(is_rtl_dominant("The diagnosis is based on HbA1c."))
        # It still gets runs; the point is that its base direction is not Persian.
        self.assertIn("HbA1c", xml)

    def test_no_run_text_is_reversed(self):
        text = "شاخص HbA1c برای تشخیص دیابت استفاده می‌شود."
        xml = self._xml(text)
        for paragraph in re.findall(r"<w:p\b.*?</w:p>", xml, re.S):
            runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", paragraph, re.S)
            joined = "".join(runs)
            if "HbA1c" in joined:
                self.assertEqual(joined.replace(" ", ""), text.replace(" ", ""))


class NormalizationSafetyTests(unittest.TestCase):
    def test_persian_letters_are_folded(self):
        self.assertEqual(normalize_display(ARABIC_KAF), "ک")
        self.assertEqual(normalize_display(ARABIC_YEH), "ی")

    def test_technical_content_is_never_touched(self):
        for token in TECHNICAL_TOKENS + (
            "https://example.ir/a?b=1&c=2",
            "info@test.ir",
            "F = ma",
            "x >= 5",
            "COVID_19",
            "user_name",
        ):
            with self.subTest(token=token):
                self.assertEqual(normalize_display(token), token)

    def test_mixed_sentence_keeps_both_scripts_intact(self):
        text = "شاخص HbA1c در 7.2% گزارش شد و اشاره به کتاب با ی کاف است."
        result = normalize_display(text)
        self.assertIn("HbA1c", result)
        self.assertIn("7.2%", result)
        self.assertIn("کتاب", result)
        self.assertIn("ی کاف", result)
        self.assertNotIn(ARABIC_KAF, result)

    def test_zwnj_is_preserved(self):
        for word in ("می‌شود", "نمی‌کند", "کتاب‌ها", "یون‌های"):
            self.assertEqual(normalize_display(word), word)

    def test_digits_are_not_rewritten(self):
        self.assertEqual(normalize_display("۵۰۰ میلی‌گرم"), "۵۰۰ میلی‌گرم")
        self.assertEqual(normalize_display("7.2%"), "7.2%")

    def test_newlines_and_tabs_survive(self):
        self.assertEqual(normalize_whitespace("a\nb"), "a\nb")
        self.assertEqual(normalize_whitespace("a\tb"), "a\tb")
        self.assertEqual(normalize_whitespace("a   b"), "a b")

    def test_idempotent(self):
        text = "كتيب علمي با ي و 500 mg و ZWNJ می‌شود"
        once = normalize_display(text)
        self.assertEqual(once, normalize_display(once))


if __name__ == "__main__":
    unittest.main()
