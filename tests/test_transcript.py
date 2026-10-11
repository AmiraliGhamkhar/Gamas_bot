"""Tests for the raw text engine — the text that is passed to the LLM."""

from __future__ import annotations

import unittest

from gamas_bot.transcript import (
    PARAGRAPH_GAP_SECONDS,
    RawTranscript,
    Word,
    build_document,
    clean_typography,
    fingerprint,
    guarded,
    paragraphs_from_text,
    paragraphs_from_words,
    render,
    sentence_spans,
    split_transcript,
    starts_a_topic,
)


def words_from_spec(spec: list[tuple[str, float, float]]) -> list[Word]:
    return [Word(text, start, end) for text, start, end in spec]


class FingerprintTests(unittest.TestCase):
    def test_identical_text_matches(self):
        text = "فشار خون 120/80 و HbA1c برابر ۷٫۵ بود."
        self.assertEqual(fingerprint(text), fingerprint(text))

    def test_punctuation_and_space_changes_do_not_change_fingerprint(self):
        base = "سلام ، دنیا .  چطوری؟"
        changed = "سلام، دنیا. چطوری؟"
        self.assertEqual(fingerprint(base), fingerprint(changed))

    def test_word_loss_changes_fingerprint(self):
        base = "یک دو سه چهار پنج"
        self.assertNotEqual(fingerprint(base), fingerprint(base + " شش"))

    def test_digit_loss_changes_fingerprint(self):
        self.assertNotEqual(fingerprint("دوز 500 میلی‌گرم"), fingerprint("دوز 50 میلی‌گرم"))

    def test_latin_token_loss_changes_fingerprint(self):
        self.assertNotEqual(fingerprint("HbA1c مهم است"), fingerprint("HbA1 مهم است"))

    def test_zwnj_loss_changes_fingerprint(self):
        self.assertNotEqual(fingerprint("می‌شود"), fingerprint("میشود"))

    def test_persian_digits_folded_like_ascii(self):
        self.assertEqual(fingerprint("۱۲۰ میلی‌گرم"), fingerprint("120 میلی‌گرم"))


class GuardedTests(unittest.TestCase):
    def test_clean_typography_passes_the_gate(self):
        text = "سلام ، دنیا؛ چطوری ؟"
        result = guarded(text, clean_typography, name="typography")
        self.assertEqual(result, "سلام، دنیا؛ چطوری؟")

    def test_dangerous_transform_is_rejected(self):
        text = "عدد 500 و 120/80 را حفظ کن"
        evil = lambda value: value.replace("500", "5")  # noqa: E731
        self.assertEqual(guarded(text, evil, name="evil"), text)

    def test_emptying_transform_is_rejected(self):
        self.assertEqual(guarded("متن", lambda _v: "", name="empty"), "متن")

    def test_raising_transform_is_rejected(self):
        def boom(_value):
            raise RuntimeError("boom")

        self.assertEqual(guarded("متن", boom, name="boom"), "متن")


class CleanTypographyTests(unittest.TestCase):
    def test_persian_punctuation_hugs_previous_word(self):
        self.assertEqual(clean_typography("سلام ، دنیا"), "سلام، دنیا")

    def test_space_after_persian_punctuation(self):
        self.assertEqual(clean_typography("سلام،دنیا"), "سلام، دنیا")

    def test_space_before_sentence_dot_removed(self):
        self.assertEqual(clean_typography("سلام . دنیا"), "سلام. دنیا")

    def test_dot_glued_to_next_persian_word_gets_space(self):
        self.assertEqual(clean_typography("سلام.دنیا"), "سلام. دنیا")

    def test_decimal_numbers_untouched(self):
        self.assertEqual(clean_typography("مقدار 7.2 واحد است"), "مقدار 7.2 واحد است")

    def test_latin_text_untouched(self):
        latin = "The value of X is 3.5 mg/dL, correct?"
        self.assertEqual(clean_typography(latin), latin)

    def test_terminator_runs_collapse(self):
        self.assertEqual(clean_typography("واقعا؟؟؟"), "واقعا؟")

    def test_arabic_letters_fold_to_persian(self):
        self.assertEqual(clean_typography("كتاب يوجد"), "کتاب یوجد")

    def test_zwnj_preserved(self):
        self.assertEqual(clean_typography("این می‌شود است"), "این می‌شود است")

    def test_content_survives_idempotently(self):
        text = "دوز 500mg هر 8 ساعت؛ فشار 120/80 و HbA1c=7.5 می‌باشد."
        once = clean_typography(text)
        twice = clean_typography(once)
        self.assertEqual(once, twice)
        self.assertIn("500mg", once)
        self.assertIn("120/80", once)
        self.assertIn("HbA1c=7.5", once)


class SentenceSpanTests(unittest.TestCase):
    def test_decimal_not_a_boundary(self):
        spans = sentence_spans("مقدار 7.2 و 1.000 واحد است.")
        self.assertEqual(len(spans), 1)

    def test_persian_terminator_is_boundary(self):
        spans = sentence_spans("اول این است. دوم آن است!")
        self.assertEqual(len(spans), 2)

    def test_ellipsis_and_question_marks(self):
        spans = sentence_spans("چرا؟ چون… همین.")
        self.assertEqual(len(spans), 3)


class ParagraphFromWordsTests(unittest.TestCase):
    def test_gap_creates_paragraph_break(self):
        words = words_from_spec([
            ("سلام", 0.0, 0.5), ("دوستان", 0.6, 1.1), (".", 1.1, 1.1),
            ("حالا", 1.1 + PARAGRAPH_GAP_SECONDS + 1, 3.5), ("موضوع", 3.6, 4.0),
        ])
        paragraphs = paragraphs_from_words(words)
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual(paragraphs[0].text, "سلام دوستان .")
        self.assertEqual(paragraphs[1].text, "حالا موضوع")

    def test_topic_marker_breaks_paragraph(self):
        words = words_from_spec([
            ("بحث", 0.0, 0.4), ("اول", 0.5, 0.9), ("بود", 1.0, 1.3), (".", 1.3, 1.3),
            ("حالا", 1.4, 1.8), ("می‌رسیم", 1.9, 2.4), ("سراغ", 2.5, 2.9),
            ("موضوع", 3.0, 3.4), ("دوم", 3.5, 3.8), (".", 3.8, 3.8),
        ])
        paragraphs = paragraphs_from_words(words)
        self.assertEqual(len(paragraphs), 2)

    def test_soft_cap_breaks_at_sentence_boundary_only(self):
        # A single long sentence over the cap stays whole; the break happens
        # at the next sentence boundary.
        filler = "کلمه"
        words: list[Word] = []
        time_cursor = 0.0
        for index in range(200):
            words.append(Word(filler, time_cursor, time_cursor + 0.05))
            time_cursor += 0.1
            if index == 199:
                words.append(Word(".", time_cursor, time_cursor))
        words.append(Word("پایان", time_cursor + 0.1, time_cursor + 0.2))
        paragraphs = paragraphs_from_words(words, soft_cap=100)
        self.assertGreaterEqual(len(paragraphs), 2)
        # Nothing is lost: every token appears exactly once across paragraphs.
        joined = " ".join(p.text for p in paragraphs)
        self.assertEqual(joined.count(filler), 200)
        self.assertIn("پایان", joined)

    def test_timing_metadata_preserved(self):
        words = words_from_spec([
            ("اول", 0.0, 0.4), (".", 0.4, 0.4),
            ("دوم", 5.0, 5.4), (".", 5.4, 5.4),
        ])
        paragraphs = paragraphs_from_words(words)
        self.assertEqual(paragraphs[0].start, 0.0)
        self.assertEqual(paragraphs[1].start, 5.0)


class ParagraphFromTextTests(unittest.TestCase):
    def test_grouping_by_punctuation(self):
        text = "یک است. دو است. سه است. چهار است. پنج است."
        paragraphs = paragraphs_from_text(text, soft_cap=10)
        self.assertGreater(len(paragraphs), 1)
        joined = " ".join(p.text for p in paragraphs)
        self.assertEqual(joined, text)

    def test_no_text_no_paragraphs(self):
        self.assertEqual(paragraphs_from_text("   "), [])


class BuildDocumentTests(unittest.TestCase):
    def test_from_timed_words(self):
        raw = RawTranscript(
            words=words_from_spec([
                ("سلام", 0.0, 0.4), ("،", 0.4, 0.4), ("دوستان", 0.5, 0.9),
                (".", 0.9, 0.9),
                ("موضوع", 10.0, 10.4), ("جديد", 10.5, 10.8),
            ]),
            text="سلام ، دوستان . موضوع جديد",
        )
        document = build_document(raw)
        self.assertEqual(len(document.paragraphs), 2)
        # cleanup applied: no space before ،
        self.assertIn("سلام، دوستان.", document.plain())
        # Arabic YEH folded to Persian
        self.assertIn("جدید", document.plain())

    def test_from_plain_text_fallback(self):
        document = build_document(RawTranscript.from_text("اول است. دوم است. سوم است."))
        self.assertTrue(document.paragraphs)
        self.assertIn("اول است.", document.plain())

    def test_empty_words_fall_back_to_text(self):
        document = build_document(RawTranscript(words=[], text="فقط متنplain است."))
        self.assertIn("فقط متنplain است.", document.plain())

    def test_llm_document_equals_plain(self):
        document = build_document(RawTranscript.from_text("یکی. دو. سه."))
        self.assertEqual(document.llm_document(), document.plain())

    def test_timestamped_rendering(self):
        raw = RawTranscript(
            words=words_from_spec([
                ("اول", 0.0, 0.4), (".", 0.4, 0.4),
                ("دوم", 65.0, 65.4), (".", 65.4, 65.4),
            ]),
            text="اول. دوم.",
        )
        document = build_document(raw)
        rendered = render(document, "timestamped")
        self.assertIn("[۰۰:۰۰]", rendered)
        self.assertIn("[۰۱:۰۵]", rendered)

    def test_plain_rendering_has_no_stamps(self):
        document = build_document(RawTranscript.from_text("یک. دو."))
        self.assertNotIn("[", render(document, "plain"))

    def test_fidelity_end_to_end(self):
        """Cleanup can never lose a number, unit or Latin token."""
        text = (
            "دوز دارو 500 mg هر 8 ساعت با فشار 120/80 و HbA1c 7.5 درصد ، "
            "همچنین COVID-19 و ۱۲۰ میلی‌گرم آزمایش شد."
        )
        document = build_document(RawTranscript.from_text(text))
        rendered = document.plain()
        self.assertIn("500 mg", rendered)
        self.assertIn("120/80", rendered)
        self.assertIn("HbA1c", rendered)
        self.assertIn("COVID-19", rendered)
        self.assertIn("میلی‌گرم", rendered)
        self.assertEqual(fingerprint(rendered), fingerprint(text))


class SplitTranscriptTests(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(split_transcript("سلام. این یک متن کوتاه است."), ["سلام. این یک متن کوتاه است."])

    def test_no_text_no_chunks(self):
        self.assertEqual(split_transcript("   "), [])

    def test_split_is_lossless(self):
        paragraphs = "\n\n".join(
            f"پاراگراف شمارهٔ {index} با چند جمله. جملهٔ دوم. جملهٔ سوم."
            for index in range(80)
        )
        chunks = split_transcript(paragraphs, max_chars=500)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks).replace("\n\n", ""), paragraphs.replace("\n\n", ""))

    def test_never_mid_sentence_when_avoidable(self):
        text = "\n\n".join(
            "جملهٔ یکم این بخش. جملهٔ دوم این بخش. جملهٔ سوم این بخش."
            for _ in range(40)
        )
        chunks = split_transcript(text, max_chars=300)
        for chunk in chunks:
            # every chunk boundary sits at a paragraph/sentence edge: no chunk
            # starts mid-sentence (lowercase continuation) in this fixture
            self.assertTrue(chunk.startswith("جملهٔ"))

    def test_topic_boundary_preferred(self):
        paragraphs = ["مقدمهٔ درس " + "متن " * 100]
        paragraphs.append("حالا می‌رسیم سراغ موضوع دوم " + "متن " * 100)
        text = "\n\n".join(paragraphs)
        chunks = split_transcript(text, max_chars=800)
        self.assertGreater(len(chunks), 1)
        # The second paragraph opens a topic: it must start its own chunk.
        for chunk in chunks[1:]:
            self.assertTrue(chunk.startswith("حالا می‌رسیم"))

    def test_oversized_paragraph_sentence_split(self):
        paragraph = "یک جملهٔ بلند است اینجا. " * 400  # one giant paragraph
        chunks = split_transcript(paragraph, max_chars=2000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 2000 + 60)  # sentence slack only

    def test_numbers_not_split(self):
        paragraph = "مقدار 1234567.891011 واحد بود. " * 60
        chunks = split_transcript(paragraph, max_chars=300)
        joined = " ".join(chunks)
        self.assertIn("1234567.891011", joined)


class TopicMarkerTests(unittest.TestCase):
    def test_common_lecture_transitions(self):
        for opener in (
            "حالا می‌رسیم سراغ شبکه‌های عصبی",
            "اسلاید ۱۲ عنوان بعدی است",
            "بخش بعد دربارهٔ داروهاست",
            "فصل دوم را شروع می‌کنیم",
            "### اسلاید ۳ — نتایج",
        ):
            self.assertTrue(starts_a_topic(opener), opener)

    def test_ordinary_sentences_are_not_topics(self):
        for sentence in (
            "این یک جملهٔ عادی است",
            "و بعد ادامه می‌دهیم",
            "عدد 500 را نگه دار",
        ):
            self.assertFalse(starts_a_topic(sentence), sentence)


if __name__ == "__main__":
    unittest.main()
