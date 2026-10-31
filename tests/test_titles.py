"""The 2–4-word naming contract behind library rows and booklet covers."""

from __future__ import annotations

import unittest
import unicodedata

from gamas_bot.titles import (
    FALLBACK_BOOKLET,
    FALLBACK_OFFICE,
    MAX_TITLE_CHARS,
    MAX_TITLE_WORDS,
    MIN_TITLE_WORDS,
    short_title,
    word_count,
)


class ShortTitleTests(unittest.TestCase):
    def test_shortens_a_long_lesson_title_to_four_words(self):
        title = short_title(
            "جزوهٔ جامع فیزیولوژی قلب و عروق برای آزمون پایان ترم"
        )
        self.assertLessEqual(word_count(title), MAX_TITLE_WORDS)
        self.assertGreaterEqual(word_count(title), MIN_TITLE_WORDS)
        # The user's own words are kept, in order — nothing is rewritten.
        self.assertEqual(title, "جامع فیزیولوژی قلب")

    def test_keeps_a_two_to_four_word_title_untouched(self):
        for title in ("فیزیولوژی قلب", "گزارش فروش ماهانه فروردین"):
            self.assertEqual(short_title(title), title)

    def test_drops_a_title_that_was_cut_mid_phrase(self):
        # «و» at the edge means the sentence continued; keeping it reads broken.
        self.assertNotEqual(short_title("جامعی از قلب و").split()[-1], "و")

    def test_caption_decoration_and_reference_codes_are_not_a_name(self):
        self.assertEqual(short_title("📖 جزوهٔ Word — GMS-12345"), "جزوهٔ Word")
        self.assertEqual(
            short_title("", "🎧", "OFF-1A2B", fallback=FALLBACK_BOOKLET),
            FALLBACK_BOOKLET,
        )

    def test_a_single_word_is_widened_with_the_kind_word(self):
        self.assertEqual(short_title("قلب"), "جزوهٔ قلب")

    def test_word_joiners_inside_a_word_survive(self):
        # ZWNJ carries Persian morphology (P1): it is never replaced by a space.
        title = "می‌شود و می‌رود"
        self.assertIn("\u200c", short_title(title))

    def test_windows_never_exceed_the_button_budget(self):
        title = short_title("کلمهٔ بسیار بسیار طولانی دیگری برای آزمون نهایی امروز")
        self.assertLessEqual(len(title), MAX_TITLE_CHARS)
        self.assertGreaterEqual(word_count(title), MIN_TITLE_WORDS)

    def test_first_usable_candidate_wins(self):
        # The model title wins; the upload name is only the fallback.
        self.assertEqual(
            short_title("دینامیک قلب", "lecture-3-cardio.m4a"), "دینامیک قلب"
        )
        self.assertEqual(short_title(None, "lecture-3-cardio.m4a"), "lecture 3 cardio")
        # A timestamp fragment is not a name, but «فصل ۳» is.
        self.assertEqual(short_title("voice_2026_11_01.ogg", fallback="متن گفتار"), "متن voice")
        self.assertEqual(short_title("فصل ۳ قلب"), "فصل ۳ قلب")

    def test_office_titles_keep_a_single_format_token(self):
        base = short_title(
            "گزارش فروش ماهانه فروردین", fallback=FALLBACK_OFFICE, max_words=3
        )
        self.assertEqual(base, "گزارش فروش ماهانه")
        self.assertLessEqual(word_count(f"{base} — Excel"), MAX_TITLE_WORDS)

    def test_is_idempotent(self):
        # R14: the same input always yields the same name, so a booklet's cover,
        # filename, caption and library row cannot disagree.
        for source in ("جزوهٔ جامع فیزیولوژی قلب و عروق", "قلب", "📖 جزوهٔ Word — GMS-1"):
            once = short_title(source)
            self.assertEqual(short_title(once), once)

    def test_normalizes_arabic_letter_variants(self):
        folded = short_title("جزوهٔ فيزيولوژي قلب")
        self.assertTrue(unicodedata.is_normalized("NFC", folded))
        self.assertNotIn("ي", folded)


if __name__ == "__main__":
    unittest.main()
