"""Speaker diarization, button-driven delivery and the bottom keyboard.

These three features share one acceptance rule: the chat must only ever contain
what the user asked for. Diarization decides *who* a paragraph belongs to, the
delivery layer decides *when* an artifact appears, and the reply keyboard decides
*where* the navigation lives. Each is tested against the promise it makes rather
than against its implementation.
"""

from __future__ import annotations

import json
import unittest

from gamas_bot import texts
from gamas_bot.artifacts import (
    KIND_BOOKLET,
    KIND_KEYPOINTS,
    KIND_TRANSCRIPT,
    callback_for,
    key_points_of,
    parse_callback,
    reference_for,
    submission_from_callback,
)
from gamas_bot.transcript import (
    MAX_SPEAKERS,
    SPEAKER_UNKNOWN,
    Paragraph,
    RawTranscript,
    TranscriptDocument,
    Word,
    build_document,
    paragraphs_from_words_by_speaker,
    resolve_speakers,
    speaker_name,
    speaker_name_from_label,
)


def _turn(label: str, count: int, start: float = 0.0, step: float = 0.4) -> list[Word]:
    """``count`` words of one speaker, timed one after another."""
    words: list[Word] = []
    for index in range(count):
        begin = start + index * step
        words.append(Word(f"واژه{index}", begin, begin + step * 0.8, label))
    return words


def _conversation(*turns: tuple[str, int]) -> list[Word]:
    words: list[Word] = []
    clock = 0.0
    for label, count in turns:
        words.extend(_turn(label, count, start=clock))
        clock += count * 0.4 + 1.0
    return words


class SpeakerResolutionTests(unittest.TestCase):
    def test_two_balanced_speakers_are_both_kept(self):
        words, count = resolve_speakers(_conversation(("S1", 40), ("S2", 30)))
        self.assertEqual(count, 2)
        self.assertEqual({word.speaker for word in words}, {"0", "1"})

    def test_labels_are_ordered_by_how_much_each_voice_said(self):
        # S2 speaks four times as much, so S2 becomes the first voice the reader
        # meets and S1 the second — the lecturer leads, not whoever coughed first.
        words, count = resolve_speakers(_conversation(("S1", 20), ("S2", 80)))
        self.assertEqual(count, 2)
        by_label: dict[str, int] = {}
        for word in words:
            by_label[word.speaker] = by_label.get(word.speaker, 0) + 1
        self.assertEqual(by_label["0"], 80)
        self.assertEqual(by_label["1"], 20)

    def test_a_speck_of_noise_is_not_a_speaker(self):
        """One cough must not become «گویندهٔ ۲» in a solo lecture."""
        words = _conversation(("S1", 400)) + _turn("S2", 3, start=400.0)
        resolved, count = resolve_speakers(words)
        self.assertEqual(count, 1)
        # The three orphan words are marked unplaceable rather than promoted to
        # a second speaker; the turn builder later folds them into the turn.
        self.assertEqual(
            {word.speaker for word in resolved}, {"0", SPEAKER_UNKNOWN}
        )

    def test_unknown_labels_do_not_create_a_speaker(self):
        words = _conversation(("S1", 40), ("UU", 5), ("S2", 40))
        _resolved, count = resolve_speakers(words)
        self.assertEqual(count, 2)

    def test_a_run_without_any_label_reports_no_speakers(self):
        words = [Word("سلام", 0.0, 1.0), Word("دنیا", 1.0, 2.0)]
        resolved, count = resolve_speakers(words)
        self.assertEqual(count, 0)
        self.assertEqual(resolved, words)

    def test_the_reported_count_is_capped(self):
        turns = [(f"S{index}", 30) for index in range(1, MAX_SPEAKERS + 6)]
        _resolved, count = resolve_speakers(_conversation(*turns))
        self.assertEqual(count, MAX_SPEAKERS)

    def test_provider_label_spellings_agree(self):
        words = _conversation(("S1", 40)) + _conversation(("s1", 40))
        _resolved, count = resolve_speakers(words)
        self.assertEqual(count, 1)


class SpeakerTurnTests(unittest.TestCase):
    def test_each_turn_becomes_one_paragraph(self):
        words, _count = resolve_speakers(
            _conversation(("S1", 20), ("S2", 20), ("S1", 20))
        )
        paragraphs = paragraphs_from_words_by_speaker(words)
        self.assertEqual(len(paragraphs), 3)
        self.assertEqual([p.speaker for p in paragraphs], ["0", "1", "0"])

    def test_a_long_pause_inside_a_turn_starts_a_new_paragraph(self):
        words = _turn("S1", 10)
        words += _turn("S1", 10, start=words[-1].end + 30)  # 30 s of silence
        _resolved, _count = resolve_speakers(words)
        paragraphs = paragraphs_from_words_by_speaker(_resolved)
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual({p.speaker for p in paragraphs}, {"0"})

    def test_timings_survive_the_turn_builder(self):
        words, _count = resolve_speakers(_conversation(("S1", 5), ("S2", 5)))
        paragraphs = paragraphs_from_words_by_speaker(words)
        self.assertEqual(paragraphs[0].start, 0.0)
        self.assertIsNotNone(paragraphs[-1].end)

    def test_an_unplaceable_word_inherits_its_turn(self):
        words = _turn("S1", 10) + [Word("هوم", 4.1, 4.3, SPEAKER_UNKNOWN)] + _turn(
            "S1", 10, start=4.4
        )
        resolved, _count = resolve_speakers(words)
        paragraphs = paragraphs_from_words_by_speaker(resolved)
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0].speaker, "0")

    def test_speaker_names_are_persian_ordinals(self):
        self.assertEqual(speaker_name(0), "گویندهٔ ۱")
        self.assertEqual(speaker_name(2), "گویندهٔ ۳")
        self.assertIsNone(speaker_name_from_label(None))
        self.assertIsNone(speaker_name_from_label(SPEAKER_UNKNOWN))
        self.assertEqual(speaker_name_from_label("1"), "گویندهٔ ۲")


class DiarizedDocumentTests(unittest.TestCase):
    def test_build_document_labels_turns_and_keeps_the_count(self):
        words = _conversation(("S1", 40), ("S2", 40))
        raw = RawTranscript(
            words=words,
            text=" ".join(word.text for word in words),
            engine="speechmatics",
            speakers=2,
        )
        document = build_document(raw)
        self.assertEqual(document.speakers, 2)
        self.assertEqual(len(document.paragraphs), 2)
        plain = document.plain()
        self.assertTrue(plain.startswith("گویندهٔ ۱: "))
        self.assertIn("\n\nگویندهٔ ۲: ", plain)
        # The timestamped profile keeps both the stamp and the speaker.
        stamped = document.timestamped()
        self.assertIn("[۰۰:۰۰] گویندهٔ ۱: ", stamped)
        self.assertIn("گویندهٔ ۲: ", stamped)

    def test_a_single_voice_is_not_labelled(self):
        """A solo lecture keeps plain paragraphs; only the count is reported."""
        words = _conversation(("S1", 80))
        document = build_document(
            RawTranscript(words=words, text="متن", engine="deepgram", speakers=1)
        )
        self.assertEqual(document.speakers, 1)
        self.assertNotIn("گویندهٔ", document.plain())

    def test_a_speaker_change_inside_one_paragraph_is_never_lost(self):
        words = _conversation(("S1", 30), ("S2", 30), ("S3", 30))
        document = build_document(
            RawTranscript(words=words, text="متن", engine="deepgram", speakers=3)
        )
        self.assertEqual(document.speakers, 3)
        self.assertEqual(
            [p.speaker for p in document.paragraphs], ["0", "1", "2"]
        )

    def test_a_run_without_labels_is_the_ordinary_transcript(self):
        words = _conversation(("", 40)) + _conversation(("", 40))
        document = build_document(
            RawTranscript(words=words, text="متن", engine="openai_compatible")
        )
        self.assertEqual(document.speakers, 0)
        self.assertFalse(document.diarization_attempted)
        self.assertNotIn("گویندهٔ", document.plain())


class DeliveryCallbackTests(unittest.TestCase):
    def test_reference_round_trips(self):
        reference = reference_for(12)
        self.assertEqual(reference, "GMS-12")
        for kind in (KIND_BOOKLET, KIND_TRANSCRIPT, KIND_KEYPOINTS):
            payload = callback_for(reference, kind)
            self.assertEqual(payload, f"art:GMS-12:{kind}".encode("ascii"))
            self.assertEqual(
                parse_callback(payload.decode("ascii")), (reference, kind)
            )
            self.assertEqual(submission_from_callback(payload.decode("ascii")), 12)

    def test_malformed_payloads_are_refused(self):
        for payload in (
            "",
            "art:",
            "art:GMS-12",
            "art:GMS-12:transcript:extra",
            "art:GMS-x:transcript",
            "art:GMS-12:everything",
            "lib:f:3",
        ):
            self.assertIsNone(parse_callback(payload), payload)
            self.assertIsNone(submission_from_callback(payload), payload)

    def test_an_unknown_kind_is_never_a_button(self):
        with self.assertRaises(ValueError):
            callback_for("GMS-1", "secret")

    def test_key_points_are_read_from_the_stored_notes(self):
        notes = json.dumps({"key_points": ["الف", "ب", " "], "summary": "x"})
        self.assertEqual(key_points_of(notes), ["الف", "ب"])

    def test_unreadable_notes_report_no_key_points(self):
        self.assertEqual(key_points_of(None), [])
        self.assertEqual(key_points_of(""), [])
        self.assertEqual(key_points_of("{not json"), [])
        self.assertEqual(key_points_of("[1, 2, 3]"), [])
        self.assertEqual(key_points_of(json.dumps({"key_points": "abc"})), [])
        self.assertEqual(key_points_of(json.dumps({"key_points": []})), [])


class ResultsScreenTests(unittest.TestCase):
    def test_the_screen_lists_only_what_exists(self):
        text = texts.delivery_screen(
            reference="GMS-4", speakers=2, has_booklet=True, has_keypoints=False
        )
        self.assertIn("GMS-4", text)
        self.assertIn("گویندگان شناسایی‌شده: ۲ نفر", text)
        self.assertIn(texts.DELIVERY_ROW_BOOKLET, text)
        self.assertIn(texts.DELIVERY_ROW_TRANSCRIPT, text)
        self.assertNotIn(texts.DELIVERY_ROW_KEYPOINTS, text)

    def test_one_speaker_and_no_speaker_read_differently(self):
        one = texts.delivery_screen(
            reference="GMS-1", speakers=1, has_booklet=True, has_keypoints=True
        )
        self.assertIn(texts.DELIVERY_SPEAKERS_ONE, one)
        unknown = texts.delivery_screen(
            reference="GMS-1", speakers=0, has_booklet=True, has_keypoints=True
        )
        self.assertIn(texts.DELIVERY_SPEAKERS_NONE, unknown)

    def test_key_points_text_never_sends_an_empty_message(self):
        body = texts.keypoints_text(reference="GMS-1", points=[])
        self.assertIn(texts.DELIVERY_KEYPOINTS_EMPTY, body)

    def test_key_points_text_lists_the_points(self):
        body = texts.keypoints_text(reference="GMS-1", points=["الف", "ب"], limit=1)
        self.assertIn("• الف", body)
        self.assertNotIn("• ب", body)


class ReplyKeyboardTests(unittest.TestCase):
    def test_the_keyboard_always_offers_start_and_results(self):
        for is_admin in (False, True):
            labels = [
                label
                for row in texts.reply_menu_rows(is_admin=is_admin)
                for label in row
            ]
            self.assertIn(texts.BTN_START_HERE, labels)
            self.assertIn(texts.BTN_MY_RESULTS, labels)
            self.assertIn(texts.BTN_LIBRARY, labels)
            self.assertIn(texts.BTN_UPGRADE, labels)

    def test_the_admin_button_is_only_for_admins(self):
        user = [label for row in texts.reply_menu_rows(is_admin=False) for label in row]
        admin = [label for row in texts.reply_menu_rows(is_admin=True) for label in row]
        self.assertNotIn(texts.BTN_ADMIN, user)
        self.assertIn(texts.BTN_ADMIN, admin)

    def test_the_office_button_follows_the_setting(self):
        on = [
            label
            for row in texts.reply_menu_rows(is_admin=False, office_enabled=True)
            for label in row
        ]
        off = [
            label
            for row in texts.reply_menu_rows(is_admin=False, office_enabled=False)
            for label in row
        ]
        self.assertIn(texts.BTN_OFFICE, on)
        self.assertNotIn(texts.BTN_OFFICE, off)

    def test_every_label_fits_a_telegram_button(self):
        """Telegram rejects a keyboard button longer than a short label."""
        for row in texts.reply_menu_rows(is_admin=True):
            for label in row:
                self.assertLessEqual(len(label), 24, label)
        self.assertNotIn("\n", "".join(
            label for row in texts.reply_menu_rows(is_admin=True) for label in row
        ))


class TranscriptDocumentSpeakerRenderingTests(unittest.TestCase):
    def test_a_repeated_speaker_is_not_repeated_in_the_body(self):
        document = TranscriptDocument(
            (
                Paragraph("نخست", 0.0, 1.0, "0"),
                Paragraph("دوم", 30.0, 31.0, "0"),
                Paragraph("سوم", 60.0, 61.0, "1"),
            ),
            speakers=2,
        )
        plain = document.plain()
        self.assertEqual(plain.count("گویندهٔ ۱"), 1)
        self.assertEqual(plain.count("گویندهٔ ۲"), 1)
        self.assertIn("گویندهٔ ۲: سوم", plain)


if __name__ == "__main__":
    unittest.main()
