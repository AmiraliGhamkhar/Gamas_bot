"""The offline HTML study booklet: content, offline safety and delivery wiring."""

from __future__ import annotations

import json
import re
import unittest
from dataclasses import replace

from gamas_bot.artifacts import (
    KIND_BOOKLET,
    KIND_FLASHCARDS,
    KIND_STUDY_HTML,
    KIND_TRANSCRIPT,
    delivery_markup,
    parse_callback,
)
from gamas_bot.notes import StructuredNotes
from gamas_bot.study_notes_html import (
    StudyNotesRenderError,
    render_study_notes_html,
    study_notes_filename,
)
from tests.support import sample_notes_json
from tests.test_bot import _button_data


def rich_notes() -> StructuredNotes:
    """A booklet that uses every component the renderer supports."""
    payload = {
        "title": "فیزیولوژی غدد درون‌ریز",
        "summary": "این جزوه سازوکار محور هیپوفیز-تیروئید و پیامدهای بالینی آن را مرور می‌کند.",
        "learning_objectives": [
            "توضیح محور هیپوتالاموس-هیپوفیز-تیروئید",
            "تشخیص تفاوت کم‌کاری و پرکاری تیروئید",
        ],
        "sections": [
            {
                "heading": "محور هیپوتالاموس-هیپوفیز-تیروئید",
                "paragraphs": [
                    "هیپوتالاموس با TRH و هیپوفیز با TSH غدهٔ تیروئید را تنظیم می‌کند."
                ],
                "bullets": ["TRH از هیپوتالاموس ترشح می‌شود", "TSH از هیپوفیز قدامی"],
                "definitions": [
                    {
                        "term": "هورمون محرک تیروئید",
                        "term_en": "TSH",
                        "definition": "گلیکوپروتئینی که ترشح T4 و T3 را تحریک می‌کند.",
                    }
                ],
                "steps": ["ترشح TRH", "ترشح TSH", "سنتز T4 و T3"],
                "formulas": ["T4 → T3 (deiodinase)"],
                "examples": ["کم‌کاری تیروئید با TSH بالا"],
                "key_points": ["TSH شاخص اصلی غربالگری است"],
                "tables": [
                    {
                        "caption": "مقایسهٔ کم‌کاری و پرکاری",
                        "headers": ["ویژگی", "کم‌کاری", "پرکاری"],
                        "rows": [["TSH", "بالا", "پایین"], ["متابولیسم", "کاهش", "افزایش"]],
                    }
                ],
                "callouts": [
                    {"kind": "هشدار", "text": "تفسیر TSH بدون T4 کامل نیست."},
                    {"kind": "نکته", "text": "«نیازمند بررسی» برای داده‌های ناقص."},
                ],
            },
            {
                "heading": "پیامدهای بالینی",
                "paragraphs": ["خستگی و افزایش وزن در کم‌کاری شایع است."],
                "key_points": ["علائم کم‌کاری اغلب تدریجی است"],
            },
        ],
        "key_points": ["TSH شاخص اصلی غربالگری است", "تفسیر TSH بدون T4 کامل نیست"],
        "glossary": [
            {"term": "T4", "term_en": "Thyroxine", "definition": "هورمون اصلی تیروئید"},
        ],
    }
    return StructuredNotes.from_payload(payload)


class StudyNotesHtmlTests(unittest.TestCase):
    def test_generation_succeeds_and_the_document_is_complete(self):
        html_text = render_study_notes_html(rich_notes())
        self.assertTrue(html_text.strip())
        self.assertTrue(html_text.lstrip().startswith("<!DOCTYPE html>"))
        self.assertIn('<html lang="fa" dir="rtl">', html_text)
        self.assertIn("<title>", html_text)
        self.assertIn("</html>", html_text)
        # The booklet content itself, not a placeholder.
        self.assertIn("فیزیولوژی غدد درون‌ریز", html_text)
        self.assertIn("محور هیپوتالاموس-هیپوفیز-تیروئید", html_text)
        self.assertIn("TSH", html_text)

    def test_the_document_is_valid_utf8(self):
        html_text = render_study_notes_html(rich_notes())
        self.assertEqual(html_text.encode("utf-8").decode("utf-8"), html_text)
        self.assertIn('charset="utf-8"', html_text)

    def test_the_required_study_blocks_are_present(self):
        html_text = render_study_notes_html(rich_notes())
        self.assertIn('id="objectives"', html_text)
        self.assertIn("اهداف یادگیری", html_text)
        self.assertIn('class="toc"', html_text)
        self.assertIn("فهرست مطالب", html_text)
        self.assertIn('href="#s1"', html_text)
        self.assertIn('id="key-points"', html_text)
        self.assertIn("نکات کلیدی", html_text)
        self.assertIn('id="summary"', html_text)
        self.assertIn('id="quick-review"', html_text)
        self.assertIn('id="terms"', html_text)
        self.assertIn('id="final-review"', html_text)
        # Components: definitions, steps, tables, callouts, formulas.
        self.assertIn('class="def"', html_text)
        self.assertIn('class="steps"', html_text)
        self.assertIn("<table>", html_text)
        self.assertIn('class="callout warn"', html_text)
        self.assertIn('class="formula"', html_text)
        # Summary text reaches the reader.
        self.assertIn("سازوکار محور هیپوفیز-تیروئید", html_text)

    def test_the_table_of_contents_matches_the_rendered_sections(self):
        html_text = render_study_notes_html(rich_notes())
        sections = re.findall(r'<article class="card" id="s(\d+)"', html_text)
        self.assertEqual(sections, ["1", "2"])
        self.assertIn('href="#s2"', html_text)
        self.assertNotIn('href="#s3"', html_text)

    def test_no_transcript_metadata_reaches_the_booklet(self):
        notes = rich_notes()
        # A stored booklet can carry speaker names and timestamps for the PDF;
        # the HTML study copy must never render them.
        notes = replace(
            notes,
            speakers=("گوینده ۱ — ۵۰ واژه",),
            timeline=("[۰۱:۳۰] مقدمه",),
        )
        html_text = render_study_notes_html(notes)
        for forbidden in ("گوینده ۱", "Speaker 1", "۰۱:۳۰", "01:30", "00:01:32"):
            self.assertNotIn(forbidden, html_text)
        self.assertNotIn("استاد گفت", html_text)

    def test_no_quiz_or_leaderboard_ui(self):
        html_text = render_study_notes_html(rich_notes())
        for forbidden in ("لیدربورد", "رتبه", "امتیاز", "آزمون", "خودآزمایی", "quiz", "leaderboard"):
            self.assertNotIn(forbidden, html_text)

    def test_the_document_needs_no_network_or_javascript(self):
        html_text = render_study_notes_html(rich_notes())
        self.assertNotIn("<script", html_text)
        self.assertNotIn("http://", html_text)
        self.assertNotIn("https://", html_text)
        self.assertNotIn("fonts.googleapis", html_text)
        self.assertNotIn("cdn.", html_text)
        self.assertNotIn('rel="stylesheet"', html_text)
        self.assertNotIn("<img", html_text)
        self.assertIn("default-src 'none'", html_text)
        # Fonts are embedded, never fetched.
        self.assertIn("@font-face", html_text)
        self.assertIn("data:font/ttf;base64,", html_text)

    def test_the_layout_is_mobile_first_and_printable(self):
        html_text = render_study_notes_html(rich_notes())
        self.assertIn('name="viewport"', html_text)
        self.assertIn("@media (max-width:560px)", html_text)
        self.assertIn("@media print", html_text)
        self.assertIn("overflow-x:auto", html_text)

    def test_empty_notes_are_refused(self):
        with self.assertRaises(StudyNotesRenderError):
            render_study_notes_html(StructuredNotes())

    def test_the_sample_transcript_booklet_renders_too(self):
        notes = StructuredNotes.from_payload(json.loads(sample_notes_json("جزوهٔ کلاس")))
        html_text = render_study_notes_html(notes)
        self.assertIn("جزوهٔ کلاس", html_text)
        self.assertIn('class="toc"', html_text)


class StudyNotesFilenameTests(unittest.TestCase):
    def test_filename_is_clean_and_has_no_internal_ids(self):
        name = study_notes_filename("فیزیولوژی / غدد: *ویرایش* ۲")
        self.assertTrue(name.startswith("Gamas_"), name)
        self.assertTrue(name.endswith("_notes.html"), name)
        self.assertNotIn("/", name)
        self.assertNotIn(":", name)
        self.assertNotIn("*", name)
        self.assertNotIn("GMS-", name)

    def test_filename_always_has_a_topic(self):
        self.assertEqual(study_notes_filename(""), "Gamas_study_notes.html")


class StudyNotesDeliveryTests(unittest.TestCase):
    def test_the_html_button_is_offered_next_to_the_other_artifacts(self):
        buttons = delivery_markup(
            reference="GMS-5",
            speakers=0,
            has_booklet=True,
            kinds={KIND_BOOKLET, KIND_STUDY_HTML, KIND_TRANSCRIPT},
            item_id=9,
        )
        payloads = {_button_data(button) for row in buttons for button in row}
        self.assertIn(b"art:GMS-5:review_html", payloads)
        self.assertEqual(parse_callback("art:GMS-5:review_html"), ("GMS-5", "review_html"))

    def test_no_quiz_or_leaderboard_button_is_ever_offered(self):
        buttons = delivery_markup(
            reference="GMS-5",
            speakers=2,
            has_booklet=True,
            kinds={KIND_BOOKLET, KIND_STUDY_HTML, KIND_FLASHCARDS},
            item_id=9,
        )
        data = b" ".join(_button_data(button) for row in buttons for button in row)
        self.assertNotIn(b"quiz:", data)
        self.assertNotIn(b"board", data)
        self.assertIn(b"fc:ai:9", data)


if __name__ == "__main__":
    unittest.main()
