"""Tests for the 2026-10 note-quality upgrade.

Covers: chunking invariants (completeness, order, paragraph/sentence
boundaries, decimal safety), the deterministic QA layer, the extended note
schema (definitions/examples/steps/formulas), merge dedup semantics, prompt
mode construction, BiDi run segmentation, and the DOCX rendering of
direction runs, fonts, fallbacks, header, glossary table and new blocks.
"""

from __future__ import annotations

import io
import json
import unittest
import zipfile
from datetime import datetime
from unittest.mock import patch

from gamas_bot.bidi import is_rtl_dominant, split_direction_runs
from gamas_bot.config import NOTE_MODES, resolve_note_mode
from gamas_bot.docx_export import (
    DocumentMeta,
    build_notes_docx,
    build_plain_docx,
    resolve_fonts,
)
from gamas_bot.qa import run_note_qa
from gamas_bot.structuring import (
    NoteSection,
    StructuredNotes,
    build_presentation_system_prompt,
    build_system_prompt,
    merge_structured_notes,
    parse_structured_notes,
    split_transcript,
)

from support import docx_text, make_settings, sample_notes_json

META = DocumentMeta(reference="GMS-000777", created_at=datetime(2026, 10, 1, 9, 0))


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


class ChunkingTests(unittest.TestCase):
    def test_paragraphs_are_never_split_when_they_fit(self):
        text = "\n\n".join(
            f"پاراگراف {index} با محتوای کامل تعریف و مثال. " * 3 for index in range(6)
        )
        chunks = split_transcript(text, max_chars=400)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            # No chunk ends mid-paragraph: every paragraph boundary survives.
            self.assertTrue(
                chunk.count("پاراگراف") >= 1,
                f"chunk lost its paragraph: {chunk[:60]}",
            )

    def test_concatenation_is_lossless_and_ordered(self):
        sentence = "بیمار با Metformin 500 mg شروع شد و HbA1c برابر 7.2٪ بود. "
        text = (sentence * 400).strip()
        chunks = split_transcript(text)
        joined = " ".join(chunks)
        self.assertEqual(joined.split(), text.split())
        self.assertEqual(joined.count("Metformin"), 400)
        self.assertEqual(joined.count("500 mg"), 400)
        self.assertEqual(joined.count("7.2٪"), 400)

    def test_decimal_numbers_are_never_sentence_boundaries(self):
        text = "قند خون 7.2 گزارش شد. دوز 1.5 میلی‌گرم تزریق شد. مقدار 120/80 ثبت شد."
        chunks = split_transcript(text, max_chars=30)
        joined = " ".join(chunks)
        self.assertEqual(joined.split(), text.split())
        self.assertIn("7.2", joined)
        self.assertIn("120/80", joined)

    def test_oversized_single_sentence_is_hard_cut_without_loss(self):
        text = "کلمه " * 900 + "پایان."
        chunks = split_transcript(text, max_chars=500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_sentence_packing_keeps_neighbours_together(self):
        text = "جملهٔ اول کامل است. جملهٔ دوم هم کامل است. " * 20
        chunks = split_transcript(text, max_chars=200)
        # Greedy packing: chunks are near the budget, not single sentences.
        self.assertTrue(all(len(chunk) > 100 for chunk in chunks[:-1]))

    def test_a_topic_cue_ends_a_mostly_full_chunk(self):
        """A slide heading or a lecture transition is a better cut than a budget."""
        paragraph = "توضیح کامل مفهوم با مثال عددی 500 mg و توضیح بیشتر. " * 6
        cue = "## اسلاید ۹ — ایندکس\n- ساختار کمکی برای جست‌وجوی سریع"
        text = "\n\n".join([paragraph, paragraph, cue, paragraph])
        chunks = split_transcript(text, max_chars=1000)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[1].lstrip().startswith("## اسلاید"))
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_a_topic_cue_is_ignored_while_the_chunk_is_still_short(self):
        """The fill gate keeps chunks balanced instead of cue-chasing."""
        paragraph = "توضیح کامل مفهوم با مثال عددی 500 mg. " * 3
        cue = "## اسلاید ۹ — ایندکس"
        text = "\n\n".join([paragraph, cue, paragraph])
        chunks = split_transcript(text, max_chars=1000)
        self.assertEqual(len(chunks), 1, "a 25%-full chunk must not break early")

    def test_cue_free_material_still_packs_to_the_budget(self):
        """Without a semantic cue nothing changes: greedy packing to the budget."""
        paragraph = "توضیح کامل مفهوم با مثال عددی 500 mg و توضیح بیشتر. " * 6
        text = "\n\n".join([paragraph, paragraph, paragraph])
        chunks = split_transcript(text, max_chars=1000)
        self.assertGreater(
            len(chunks[0]), 900, "an ordinary transcript must still fill its chunk"
        )
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_topic_cue_detection_covers_slides_and_transitions_only(self):
        from gamas_bot.structuring import _starts_a_topic

        for cue in (
            "### اسلاید ۳ — مدل دادهٔ رابطه‌ای",
            "## متن اسلایدها",
            "اسلاید ۱۲",
            "بخش بعدی دربارهٔ ایندکس است.",
            "حالا می‌رسیم به نرمال‌سازی.",
            "خب برویم سراغ ایندکس.",
        ):
            self.assertTrue(_starts_a_topic(cue), cue)
        for ordinary in (
            "این پاراگراف موضوع تازه‌ای را شروع نمی‌کند و ادامهٔ بحث قبلی است.",
            "۱۲۰/۸۰ میلی‌متر جیوه و نبض ۸۰ ثبت شد.",
            "در نتیجه سرعت جست‌وجو بهبود می‌یابد.",
        ):
            self.assertFalse(_starts_a_topic(ordinary), ordinary)

    def test_empty_and_tiny_inputs(self):
        self.assertEqual(split_transcript(""), [])
        self.assertEqual(split_transcript("   \n  "), [])
        self.assertEqual(split_transcript("سلام.", max_chars=100), ["سلام."])
        with self.assertRaises(ValueError):
            split_transcript("متن", 0)


# ---------------------------------------------------------------------------
# QA layer
# ---------------------------------------------------------------------------


class QATests(unittest.TestCase):
    def _notes(self, payload: dict):
        return parse_structured_notes(json.dumps(payload, ensure_ascii=False))

    def test_numbers_units_and_terms_are_recognised_as_preserved(self):
        source = (
            "دوز دارو 500 mg و حجم 0.5 mL بود. فشار خون 120/80 و SpO2 95% ثبت شد. "
            "درمان با Metformin انجام شد و HbA1c پایش گردید."
        )
        notes = self._notes(
            {
                "title": "جزوه",
                "sections": [
                    {
                        "heading": "داروها",
                        "paragraphs": [
                            "دوز دارو 500 mg و حجم 0.5 mL بود. فشار خون 120/80 و "
                            "SpO2 95% ثبت شد. درمان با Metformin انجام شد و "
                            "HbA1c پایش گردید."
                        ],
                    }
                ],
            }
        )
        report = run_note_qa(notes, [source])
        self.assertFalse(report.has_findings)
        self.assertEqual(report.source_numbers, report.preserved_numbers)
        self.assertEqual(report.source_terms, report.preserved_terms)

    def test_missing_dosage_and_term_are_reported(self):
        source = "دوز 5 mg تجویز شد و بیمار با MRI بررسی شد."
        notes = self._notes(
            {
                "title": "جزوه",
                "sections": [{"heading": "بخش", "paragraphs": ["دارو تجویز شد."]}],
            }
        )
        report = run_note_qa(notes, [source])
        self.assertTrue(report.has_findings)
        self.assertIn("5 mg", report.missing_numbers)
        self.assertIn("MRI", report.missing_terms)

    def test_persian_digits_match_latin_digits(self):
        source = "دوز ۵۰۰ میلی‌گرم و SpO2 ٪۹۵ بود."
        notes = self._notes(
            {
                "title": "جزوه",
                "sections": [
                    {"heading": "بخش", "paragraphs": ["دوز 500 mg و SpO2 95% بود."]}
                ],
            }
        )
        report = run_note_qa(notes, [source])
        self.assertEqual(report.source_numbers, report.preserved_numbers)

    def test_notes_are_never_modified(self):
        source = "مقدار 5 mg مهم است."
        notes = self._notes(
            {
                "title": "عنوان",
                "sections": [{"heading": "بخش", "bullets": ["مورد"]}],
            }
        )
        snapshot = notes.to_json()
        run_note_qa(notes, [source])
        self.assertEqual(notes.to_json(), snapshot)

    def test_common_english_words_are_not_treated_as_terms(self):
        report_source = "the patient was not well and this is fine"
        notes = self._notes(
            {
                "title": "جزوه",
                "sections": [{"heading": "بخش", "paragraphs": ["متن فارسی"]}],
            }
        )
        report = run_note_qa(notes, [report_source])
        self.assertEqual(report.source_terms, 0)


# ---------------------------------------------------------------------------
# Schema extensions
# ---------------------------------------------------------------------------


class ExtendedSchemaTests(unittest.TestCase):
    def test_definitions_examples_steps_formulas_round_trip(self):
        payload = {
            "title": "جزوه",
            "sections": [
                {
                    "heading": "بخش",
                    "definitions": [
                        {"term": "متفرمین", "term_en": "Metformin", "definition": "خط اول درمان"}
                    ],
                    "examples": ["بیمار ۴۵ ساله"],
                    "steps": ["شروع دوز", "پایش"],
                    "formulas": ["BMI = W/H"],
                }
            ],
        }
        notes = parse_structured_notes(json.dumps(payload, ensure_ascii=False))
        section = notes.sections[0]
        self.assertEqual(section.definitions[0].term_en, "Metformin")
        self.assertEqual(section.steps, ("شروع دوز", "پایش"))
        # Round-trips through to_payload without data loss.
        reloaded = parse_structured_notes(json.dumps(notes.to_payload(), ensure_ascii=False))
        self.assertEqual(reloaded.to_json(), notes.to_json())
        # Visible in the Telegram markdown renderer too.
        markdown = notes.to_markdown()
        self.assertIn("تعریف‌ها", markdown)
        self.assertIn("Metformin", markdown)
        self.assertIn("مراحل انجام", markdown)
        self.assertIn("فرمول‌ها", markdown)
        self.assertIn("مثال‌ها", markdown)

    def test_definitions_without_definition_text_are_dropped(self):
        notes = parse_structured_notes(
            '{"title": "ت", "sections": [{"heading": "ب", "definitions": [{"term": "x"}, {"term": "y", "definition": "d"}]}]}'
        )
        self.assertEqual(len(notes.sections[0].definitions), 1)
        self.assertEqual(notes.sections[0].definitions[0].term, "y")

    def test_merge_deduplicates_only_exact_duplicates(self):
        first = parse_structured_notes(
            '{"title": "جزوه", "key_points": ["نکتهٔ مشترک", "نکتهٔ یک"], '
            '"glossary": [{"term": "MRI", "definition": "تصویربرداری"}], '
            '"sections": [{"heading": "الف", "bullets": ["مورد مشترک", "مورد یک"]}]}'
        )
        second = parse_structured_notes(
            '{"title": "جزوه", "summary": "خلاصه", "key_points": ["نکتهٔ مشترک", "نکتهٔ دو"], '
            '"glossary": [{"term": "MRI", "definition": "تصویربرداری تشدید مغناطیسی وسیع‌تر"}], '
            '"sections": [{"heading": "ب", "bullets": ["مورد مشترک", "مورد دو"]}]}'
        )
        merged = merge_structured_notes([first, second])
        # Paraphrases survive; verbatim repeats collapse to one.
        self.assertEqual(merged.key_points.count("نکتهٔ مشترک"), 1)
        self.assertEqual(merged.key_points, ("نکتهٔ مشترک", "نکتهٔ یک", "نکتهٔ دو"))
        bullets = [bullet for section in merged.sections for bullet in section.bullets]
        self.assertEqual(bullets.count("مورد مشترک"), 1)
        self.assertIn("مورد یک", bullets)
        self.assertIn("مورد دو", bullets)
        # Glossary keeps the first term order and the richer definition.
        self.assertEqual(len(merged.glossary), 1)
        self.assertEqual(merged.glossary[0].definition, "تصویربرداری تشدید مغناطیسی وسیع‌تر")
        self.assertEqual(merged.summary, "خلاصه")

    def test_merge_keeps_note_mode(self):
        notes = parse_structured_notes(
            '{"title": "جزوه", "sections": [{"heading": "الف", "bullets": ["۱"]}]}'
        )
        merged = merge_structured_notes([notes])
        self.assertEqual(merged.note_mode, "full")
        summary_notes = parse_structured_notes(sample_notes_json())
        merged = merge_structured_notes([notes, summary_notes])
        self.assertEqual(merged.note_mode, "full")


# ---------------------------------------------------------------------------
# Prompts and modes
# ---------------------------------------------------------------------------


class PromptModeTests(unittest.TestCase):
    def test_mode_resolution(self):
        self.assertEqual(resolve_note_mode("full"), "full")
        self.assertEqual(resolve_note_mode("SUMMARY"), "summary")
        self.assertEqual(resolve_note_mode("standard"), "standard")
        self.assertEqual(resolve_note_mode(None), "full")
        self.assertEqual(resolve_note_mode("garbage"), "full")
        self.assertEqual(NOTE_MODES, ("full", "standard", "summary"))

    def test_full_mode_prompt_is_a_preservation_contract(self):
        prompt = build_system_prompt("full")
        # The lecture-to-notes compiler stance and the preservation rules.
        self.assertIn("FULL", prompt)
        self.assertIn("خلاصه‌ساز", prompt)
        self.assertIn("هیچ عدد، واحد", prompt)
        self.assertIn("انگلیسی", prompt)
        self.assertIn("definitions", prompt)
        self.assertIn("examples", prompt)
        self.assertIn("steps", prompt)
        self.assertIn("formulas", prompt)
        # The strict JSON contract survives.
        self.assertIn('"sections"', prompt)
        self.assertIn("JSON", prompt)

    def test_summary_mode_prompt_allows_condensing(self):
        prompt = build_system_prompt("summary")
        self.assertIn("SUMMARY", prompt)
        self.assertIn("کوتاه و فشرده", prompt)

    def test_mode_prompts_differ(self):
        self.assertNotEqual(build_system_prompt("full"), build_system_prompt("summary"))
        self.assertNotEqual(
            build_system_prompt("standard"), build_system_prompt("full")
        )

    def test_presentation_prompt_keeps_slide_rules_and_schema(self):
        prompt = build_presentation_system_prompt("full")
        self.assertIn("اسلاید", prompt)
        self.assertIn("یادداشت گوینده", prompt)
        self.assertIn("definitions", prompt)

    def test_structuring_uses_the_configured_mode(self):
        from gamas_bot.structuring import structure_transcript

        seen_prompts: list[str] = []

        async def fake_chunk(chunk, settings, session, prompt=None, *, system_prompt="", reminder=""):
            seen_prompts.append(system_prompt)
            return sample_notes_json()

        class _FakeSession:
            """Minimal async context manager standing in for aiohttp."""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            def close(self) -> None:
                pass

        with patch(
            "gamas_bot.structuring._structure_chunk", side_effect=fake_chunk
        ), patch(
            "gamas_bot.structuring.aiohttp.ClientSession",
            lambda *args, **kwargs: _FakeSession(),
        ):
            import asyncio

            asyncio.run(structure_transcript("متن درس.", make_settings(), mode="summary"))
        self.assertEqual(len(seen_prompts), 1)
        self.assertIn("SUMMARY", seen_prompts[0])


# ---------------------------------------------------------------------------
# BiDi segmentation
# ---------------------------------------------------------------------------


class BidiTests(unittest.TestCase):
    def test_mixed_sentence_splits_into_direction_runs(self):
        runs = split_direction_runs(
            "شبکه عصبی Artificial Neural Network یکی از روش‌های یادگیری ماشین است."
        )
        directions = [run.rtl for run in runs]
        self.assertEqual(directions, [True, False, True])
        # The logical text is preserved exactly (no reordering).
        self.assertEqual("".join(run.text for run in runs).split()[:2], ["شبکه", "عصبی"])

    def test_dosage_stays_in_one_ltr_run(self):
        runs = split_direction_runs("دوز 500 mg روزانه")
        ltr = [run.text for run in runs if not run.rtl]
        self.assertTrue(any("500 mg" in text for text in ltr))

    def test_bp_pair_and_terms_keep_ltr_order(self):
        runs = split_direction_runs("ثبت شد BP 120/80 و HR 80 bpm")
        joined_ltr = "".join(run.text for run in runs if not run.rtl)
        self.assertIn("BP 120/80", joined_ltr)
        self.assertIn("HR 80 bpm", joined_ltr)

    def test_pure_persian_and_pure_latin(self):
        self.assertEqual(
            [(run.text, run.rtl) for run in split_direction_runs("خط اول درمان")],
            [("خط اول درمان", True)],
        )
        self.assertEqual(
            [(run.text, run.rtl) for run in split_direction_runs("Metformin 500 mg")],
            [("Metformin 500 mg", False)],
        )

    def test_url_stays_together(self):
        runs = split_direction_runs("طبق https://example.com/doc?page=2 مراجعه کنید")
        ltr = "".join(run.text for run in runs if not run.rtl)
        self.assertIn("https://example.com/doc?page=2", ltr)

    def test_rtl_dominance(self):
        self.assertTrue(is_rtl_dominant("سلام دنیا"))
        self.assertTrue(is_rtl_dominant("سلام Metformin"))
        self.assertFalse(is_rtl_dominant("BMI = W/H"))
        self.assertFalse(is_rtl_dominant(""))

    def test_empty_input(self):
        self.assertEqual(split_direction_runs(""), [])


# ---------------------------------------------------------------------------
# DOCX rendering
# ---------------------------------------------------------------------------


RICH_NOTES_JSON = (
    '{"title": "فارماکولوژی دیابت", "summary": "مرور داروها.", '
    '"sections": [{'
    '"heading": "Metformin", '
    '"paragraphs": ["شبکه عصبی Artificial Neural Network یکی از روش‌هاست."], '
    '"definitions": [{"term": "متفرمین", "term_en": "Metformin", "definition": "خط اول درمان دیابت نوع ۲ است."}], '
    '"examples": ["بیمار با HbA1c برابر 8.5"], '
    '"steps": ["شروع با 500 mg", "افزایش تدریجی دوز"], '
    '"formulas": ["eGFR = 140 - age x W / 72 x SCr"], '
    '"key_points": ["در نارسایی کلیوی منع مصرف دارد"], '
    '"callouts": [{"kind": "هشدار", "text": "در AKI قطع شود"}], '
    '"table": {"headers": ["دارو", "دوز روزانه"], "rows": [["Metformin", "500-2000 mg"]]}'
    "}], "
    '"key_points": ["HbA1c هدف زیر ۷ درصد"], '
    '"glossary": [{"term": "HbA1c", "definition": "هموگلوبین گلیکوزیله"}]}'
)


class DocxRenderingTests(unittest.TestCase):
    def test_new_content_blocks_are_rendered(self):
        notes = parse_structured_notes(RICH_NOTES_JSON)
        text = docx_text(build_notes_docx(notes, meta=META))
        self.assertIn("متفرمین (Metformin)", text)
        self.assertIn("خط اول درمان دیابت نوع ۲ است.", text)
        self.assertIn("مثال‌ها", text)
        self.assertIn("بیمار با HbA1c برابر 8.5", text)
        self.assertIn("مراحل انجام", text)
        self.assertIn("۱. شروع با 500 mg", text)
        self.assertIn("eGFR = 140 - age x W / 72 x SCr", text)

    def test_glossary_renders_as_rtl_table(self):
        notes = parse_structured_notes(RICH_NOTES_JSON)
        text = docx_text(build_notes_docx(notes, meta=META))
        self.assertIn("اصطلاح | توضیح", text)
        self.assertIn("HbA1c | هموگلوبین گلیکوزیله", text)

    def test_mixed_text_becomes_direction_runs(self):
        notes = parse_structured_notes(RICH_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
        # The English term keeps an explicit LTR run; Persian keeps RTL runs.
        self.assertIn('<w:rtl w:val="0"/>', body)
        self.assertIn("<w:rtl/>", body)
        # The full English phrase survives as logical text inside one run.
        self.assertIn("Artificial Neural Network", body)

    def test_font_roles_and_fallback_are_applied(self):
        notes = parse_structured_notes(sample_notes_json())
        fonts = resolve_fonts(
            {"body": "Vazirmatn", "heading": "B Nazanin", "latin": "Calibri", "fallback": "Tahoma"}
        )
        data = build_notes_docx(notes, fonts=fonts, meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
            font_table = archive.read("word/fontTable.xml").decode("utf-8")
        self.assertIn('w:cs="Vazirmatn"', body)
        self.assertIn("B Nazanin", body)  # the document title uses the heading face
        self.assertIn('w:ascii="Calibri"', body)
        # Vazirmatn advertises Tahoma as the substitution font.
        self.assertIn('w:altName w:val="Tahoma"', font_table)

    def test_single_font_config_has_no_bogus_fallback_entry(self):
        notes = parse_structured_notes(sample_notes_json())
        fonts = resolve_fonts(font="Tahoma")
        data = build_notes_docx(notes, fonts=fonts, meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            font_table = archive.read("word/fontTable.xml").decode("utf-8")
        self.assertNotIn("altName", font_table)

    def test_document_has_running_header_and_mode_label(self):
        notes = parse_structured_notes(RICH_NOTES_JSON)
        data = build_notes_docx(notes, meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            headers = [
                archive.read(name).decode("utf-8")
                for name in archive.namelist()
                if name.startswith("word/header")
            ]
            body = archive.read("word/document.xml").decode("utf-8")
        self.assertTrue(headers)
        self.assertIn("Gamas Bot", headers[0])
        self.assertIn("حالت تولید: کامل", body)

    def test_summary_and_plain_documents_skip_mode_label(self):
        data = build_plain_docx("عنوان", "# متن\n\nسطر.", meta=META)
        text = docx_text(data)
        self.assertIn("متن", text)
        self.assertNotIn("حالت تولید", text)

    def test_resolve_fonts_backfills_missing_roles(self):
        fonts = resolve_fonts({"body": "Vazirmatn"})
        self.assertEqual(fonts.body, "Vazirmatn")
        self.assertEqual(fonts.heading, "Tahoma")
        self.assertEqual(fonts.latin, "Tahoma")
        self.assertEqual(fonts.fallback, "Tahoma")
        fonts = resolve_fonts(font="B Nazanin")
        self.assertEqual(
            (fonts.body, fonts.heading, fonts.latin, fonts.fallback),
            ("B Nazanin", "B Nazanin", "B Nazanin", "B Nazanin"),
        )

    def test_font_table_injection_failure_never_loses_the_document(self):
        notes = parse_structured_notes(sample_notes_json())
        fonts = resolve_fonts({"body": "Vazirmatn", "fallback": "Tahoma"})
        data = build_notes_docx(notes, fonts=fonts, meta=META)
        # The document still opens and contains all its content.
        self.assertIn("جزوهٔ آزمایشی", docx_text(data))


# ---------------------------------------------------------------------------
# Global coherence: one lecture, not a stack of independent summaries
# ---------------------------------------------------------------------------


def _note_doc(heading: str, *paragraphs: str):
    """A one-section draft, the shape one chunk's answer arrives in."""
    return parse_structured_notes(
        json.dumps(
            {
                "title": "درس",
                "sections": [
                    {"heading": heading, "paragraphs": list(paragraphs) or ["توضیح."]}
                ],
            },
            ensure_ascii=False,
        )
    )


class MergeCoherenceTests(unittest.TestCase):
    """A topic split by a chunk boundary must become one section again."""

    def test_leading_continuation_marker_is_the_same_topic(self):
        merged = merge_structured_notes(
            [
                _note_doc("مقدمه و طرح مسئله", "پاراگراف نخست."),
                _note_doc("ادامهٔ مقدمه و طرح مسئله", "پاراگراف دوم."),
            ]
        )
        self.assertEqual([section.heading for section in merged.sections], ["مقدمه و طرح مسئله"])
        self.assertEqual(merged.sections[0].paragraphs, ("پاراگراف نخست.", "پاراگراف دوم."))

    def test_trailing_and_parenthesised_markers_are_the_same_topic(self):
        for first, second in (
            ("نرمال‌سازی", "نرمال‌سازی (ادامه)"),
            ("Introduction", "Introduction — continued"),
            ("کلید و رابطه", "کلید، رابطه و صورت‌بندی جدول"),
        ):
            merged = merge_structured_notes([_note_doc(first, "الف"), _note_doc(second, "ب")])
            self.assertEqual(len(merged.sections), 1, (first, second))

    def test_unrelated_neighbours_are_never_fused(self):
        merged = merge_structured_notes(
            [_note_doc("ایندکس و کارایی", "الف"), _note_doc("نرمال‌سازی", "ب")]
        )
        self.assertEqual(
            [section.heading for section in merged.sections],
            ["ایندکس و کارایی", "نرمال‌سازی"],
        )

    def test_sibling_sections_that_differ_by_a_number_stay_separate(self):
        merged = merge_structured_notes(
            [_note_doc("صورت اول", "الف"), _note_doc("صورت دوم", "ب")]
        )
        self.assertEqual(len(merged.sections), 2)
        merged = merge_structured_notes([_note_doc("مرحله ۱", "الف"), _note_doc("مرحله ۲", "ب")])
        self.assertEqual(len(merged.sections), 2)

    def test_joining_never_drops_prose(self):
        merged = merge_structured_notes(
            [
                _note_doc("مقدمه", "یک."),
                _note_doc("ادامهٔ مقدمه", "دو.", "سه."),
                _note_doc("نتیجه", "چهار."),
            ]
        )
        paragraphs = [text for section in merged.sections for text in section.paragraphs]
        self.assertEqual(paragraphs, ["یک.", "دو.", "سه.", "چهار."])

    def test_looser_topic_test_applies_only_at_a_chunk_boundary(self):
        # Inside one chunk the stricter test decides: overlapping (but not
        # identical) headings are two sections, not one.
        from gamas_bot.structuring import _merge_sections

        sections = [
            NoteSection(heading="مقدمه و طرح مسئله", paragraphs=("الف",)),
            NoteSection(heading="طرح مسئله و مثال‌ها", paragraphs=("ب",)),
        ]
        inside = _merge_sections(list(sections))
        self.assertEqual(len(inside), 2)
        # Across a boundary the same pair is one topic a model split.
        across = _merge_sections(list(sections), chunk_starts=frozenset({1}))
        self.assertEqual(len(across), 1)
        self.assertEqual(across[0].paragraphs, ("الف", "ب"))


class CoherenceDiagnosticsTests(unittest.TestCase):
    """The QA layer must make the remaining coherence defects measurable."""

    def test_split_topic_is_reported(self):
        from gamas_bot.qa import analyze_structure

        report = analyze_structure(
            StructuredNotes(
                title="درس",
                sections=(
                    NoteSection(heading="نرمال‌سازی", paragraphs=("توضیح الف",)),
                    NoteSection(heading="ادامهٔ نرمال‌سازی", paragraphs=("توضیح ب",)),
                ),
            )
        )
        self.assertEqual(report.split_topics, 1)
        self.assertIn("adjacent sections about one split topic: 1", report.findings)

    def test_sentence_headings_and_bullet_only_sections_are_reported(self):
        from gamas_bot.qa import analyze_structure

        report = analyze_structure(
            StructuredNotes(
                title="درس",
                sections=(
                    NoteSection(
                        heading="این عنوان در واقع یک جملهٔ کامل است که نباید عنوان باشد و خیلی هم طولانی است.",
                        paragraphs=("توضیح",),
                    ),
                    NoteSection(heading="فهرست", bullets=("یک", "دو", "سه")),
                ),
            )
        )
        self.assertEqual(report.sentence_headings, 1)
        self.assertEqual(report.sections_without_paragraphs, 1)
        self.assertTrue(any("headings written as sentences" in item for item in report.findings))

    def test_a_clean_document_reports_nothing(self):
        from gamas_bot.qa import analyze_structure

        report = analyze_structure(
            StructuredNotes(
                title="درس",
                sections=(
                    NoteSection(
                        heading="مقدمه",
                        paragraphs=("توضیح کامل و کافی دربارهٔ مقدمه و هدف درس در این بخش آمده است. " * 3,),
                    ),
                    NoteSection(
                        heading="نتیجه",
                        paragraphs=("جمع‌بندی این بخش با اشاره به نتیجهٔ اصلی درس نوشته شده است. " * 3,),
                    ),
                ),
            )
        )
        self.assertTrue(report.is_clean, report.findings)


class SemanticContractPromptTests(unittest.TestCase):
    """The FULL prompt must state the deletion policy that FULL means."""

    def test_educational_units_are_enumerated(self):
        prompt = build_system_prompt("full")
        self.assertIn("واحد آموزشی یعنی", prompt)
        for unit in ("تعریف", "مثال نقض", "هشدار", "استثنا", "فرمول", "اصطلاح انگلیسی"):
            self.assertIn(unit, prompt)

    def test_deletion_policy_is_explicit(self):
        prompt = build_system_prompt("full")
        self.assertIn("فقط این چهار چیز را می‌توانید حذف کنید", prompt)
        self.assertIn("جایگزین نکنید", prompt)  # no one-line replacement
        self.assertIn("قرارداد معنایی", prompt)

    def test_professor_style_and_transition_rules_are_present(self):
        prompt = build_system_prompt("full")
        self.assertIn("پیوند آن را با مفهوم پیشین", prompt)
        self.assertIn("در نتیجه", prompt)
        self.assertIn("برای نمونه", prompt)

    def test_presentation_prompt_inherits_the_semantic_contract(self):
        prompt = build_presentation_system_prompt("full")
        self.assertIn("واحد آموزشی یعنی", prompt)
        self.assertIn("قرارداد معنایی", prompt)


if __name__ == "__main__":
    unittest.main()
