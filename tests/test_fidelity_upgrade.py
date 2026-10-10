"""Regression tests for the high-fidelity upgrade: Persian normalization,
extended QA metrics, the optional repair pass, and the font profiles.

Every test here corresponds to behaviour the audit found *missing*, so a
regression is a real content-fidelity bug rather than a style preference.
"""

from __future__ import annotations

import json
import re
import unittest
from unittest.mock import AsyncMock, patch

from support import make_settings, sample_notes_json

from gamas_bot import structuring as S
from gamas_bot.config import FONT_PROFILES, resolve_font_profile
from gamas_bot.docx_export import (
    DocumentMeta,
    build_notes_docx,
    xml_safe,
)
from gamas_bot.qa import run_note_qa
from gamas_bot.structuring import (
    NoteDefinition,
    NoteSection,
    NoteTable,
    StructuredNotes,
)
from gamas_bot.textnorm import (
    contains_arabic_script,
    normalize_display,
    normalize_digits,
    normalize_for_compare,
    normalize_persian,
    normalize_whitespace,
)

ARABIC_YEH = "ي"      # ARABIC YEH
ARABIC_KAF = "ك"      # ARABIC KAF
ARABIC_ALEF_MAKSURA = "ى"  # ALEF MAKSURA
FA_YEH = "ی"          # FARSI YEH
FA_KEHEH = "ک"        # KEHEH


class PersianNormalizationTests(unittest.TestCase):
    """The IANA/IRNIC fa-IR table folds Arabic keyboard letters to Persian."""

    def test_arabic_kaf_and_yeh_become_persian(self):
        self.assertEqual(normalize_display(f"م{ARABIC_KAF}تاب"), f"م{FA_KEHEH}تاب")
        self.assertEqual(normalize_display(f"ع{ARABIC_YEH}لم"), f"ع{FA_YEH}لم")
        self.assertEqual(normalize_display(ARABIC_KAF), FA_KEHEH)
        self.assertEqual(normalize_display(ARABIC_YEH), FA_YEH)
        self.assertEqual(normalize_display(ARABIC_ALEF_MAKSURA), FA_YEH)

    def test_normalization_is_idempotent(self):
        once = normalize_display(f"كتاب علمي{ARABIC_YEH}ن")
        self.assertEqual(once, normalize_display(once))

    def test_zwnj_is_preserved_exactly(self):
        # ZWNJ carries Persian morphology; removing it corrupts compounds.
        for word in ("می‌شود", "نمی‌کند", "کتاب‌ها"):
            self.assertIn("‌", normalize_display(word))
            self.assertEqual(normalize_display(word), word)

    def test_latin_technical_tokens_are_untouched(self):
        for token in (
            "HbA1c", "COVID-19", "mg/dL", "120/80", "MRI", "Type 2 Diabetes",
            "https://t.ir/a?b=1", "info@test.ir", "7.2%", "kg/m²", "v1.2.3",
        ):
            self.assertEqual(normalize_display(token), token, token)

    def test_digits_are_not_rewritten_by_the_renderer_normalizer(self):
        self.assertEqual(normalize_display("۵۰۰ میلی‌گرم"), "۵۰۰ میلی‌گرم")
        self.assertEqual(normalize_digits("۵۰۰"), "500")
        self.assertEqual(normalize_digits("٥٠٠"), "500")

    def test_whitespace_collapses_without_deleting_zwnj(self):
        # Horizontal runs collapse; newlines/tabs are the caller's structure
        # and must survive so a transcript is never reflowed into one run.
        self.assertEqual(normalize_whitespace("a   b"), "a b")
        self.assertEqual(normalize_whitespace("می‌شود  x"), "می‌شود x")
        self.assertEqual(normalize_whitespace("a\nb"), "a\nb")
        self.assertEqual(normalize_whitespace("a\tb"), "a\tb")

    def test_pure_latin_text_is_returned_unchanged_and_fast(self):
        self.assertFalse(contains_arabic_script("MRI 120/80"))
        self.assertEqual(normalize_persian("MRI 120/80"), "MRI 120/80")

    def test_normalize_compare_is_lossy_only_for_comparison(self):
        # It may drop ZWNJ/case for equality checks, but the display form
        # must keep them.
        self.assertEqual(
            normalize_for_compare(f"كتابعلمي{ARABIC_KAF}"),
            normalize_for_compare(f"کتابعلمي{FA_KEHEH}"),
        )
        self.assertIn("‌", normalize_display("می‌شود"))


class DocxNormalizationTests(unittest.TestCase):
    """xml_safe is the single funnel for text stored in a .docx part."""

    def test_xml_safe_folds_persian_letters(self):
        self.assertEqual(xml_safe(ARABIC_KAF + ARABIC_YEH), FA_KEHEH + FA_YEH)

    def test_xml_safe_still_removes_xml_illegal_characters(self):
        self.assertEqual(xml_safe("a\x00b\x1fc"), "abc")

    def test_xml_safe_turns_soft_breaks_into_spaces(self):
        self.assertEqual(xml_safe("a\x0bb"), "a b")

    def test_rendered_document_uses_persian_letters(self):
        notes = StructuredNotes(
            title="عنوان",
            sections=(NoteSection(heading="بخش", paragraphs=(f"متن {ARABIC_KAF}تاب",)),),
        )
        payload = build_notes_docx(notes, meta=DocumentMeta(reference="X"))
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn(FA_KEHEH + "تاب", xml)
        self.assertNotIn(ARABIC_KAF, xml)

    def test_every_rendered_block_is_normalized(self):
        """Definitions/bullets/tables must not bypass the normalization funnel.

        These blocks call ``_add_directional_text`` directly rather than going
        through ``_add_rtl_paragraph``, so they are a real regression risk.
        """
        arabic = f"{ARABIC_KAF}تاب {ARABIC_YEH}ک"
        notes = StructuredNotes(
            title="t",
            summary="s",
            sections=(
                NoteSection(
                    heading=arabic,
                    paragraphs=(f"پاراگراف {arabic}",),
                    bullets=(f"بولت {arabic}",),
                    definitions=(NoteDefinition(arabic, "تعریف", "English"),),
                    examples=(f"مثال {arabic}",),
                    steps=(f"گام {arabic}",),
                    formulas=(f"f = {ARABIC_KAF}",),
                    table=NoteTable([arabic], [[arabic]]),
                ),
            ),
        )
        payload = build_notes_docx(notes, meta=DocumentMeta(reference="X"))
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S))
        self.assertNotIn(ARABIC_KAF, text, "Arabic KAF survived rendering")
        self.assertNotIn(ARABIC_YEH, text, "Arabic YEH survived rendering")
        self.assertIn(FA_KEHEH, text)


class QAMetricTests(unittest.TestCase):
    """Deterministic coverage/compression facts used by logs and the repair gate."""

    def _report(self, notes, source):
        return run_note_qa(notes, [source])

    def test_healthy_notes_need_no_repair(self):
        # A genuinely faithful booklet: it keeps the numbers *and* the
        # explanation that gives them meaning. Keeping only "500 mg / HbA1c"
        # while deleting the sentence around them is precisely the loss the
        # semantic-coverage metric exists to catch, so it must not pass here.
        source = ("جلسه درباره دیابت بود. دوز 500 mg است. HbA1c زیر 7 درصد. " * 40)
        notes = StructuredNotes(
            title="د",
            sections=(
                NoteSection(
                    heading="ب",
                    paragraphs=(
                        "جلسه دربارهٔ دیابت بود. دوز متفورمین 500 mg است و "
                        "شاخص HbA1c زیر 7 درصد قرار می‌گیرد.",
                    ),
                ),
            ),
        )
        report = self._report(notes, source)
        self.assertEqual(report.coverage, 1.0)
        self.assertEqual(report.semantic_coverage, 1.0)
        self.assertFalse(report.needs_repair)

    def test_missing_numbers_lower_coverage_and_trigger_repair(self):
        source = ("جلسه درباره دیابت بود. دوز 500 mg است. HbA1c زیر 7 درصد. " * 40)
        notes = StructuredNotes(
            title="د", sections=(NoteSection(heading="ب", paragraphs=("یک خلاصهٔ کوتاه.",)),)
        )
        report = self._report(notes, source)
        self.assertLess(report.coverage, 0.75)
        self.assertTrue(report.needs_repair)
        self.assertIn("500 mg", report.missing_numbers)

    def test_compression_ratio_and_chunk_coverage_are_reported(self):
        source = ("متن آزمایشی با HbA1c و 500 mg. " * 40)
        notes = StructuredNotes(
            title="د",
            sections=(NoteSection(heading="ب", paragraphs=("HbA1c و 500 mg",)),),
        )
        report = self._report(notes, source)
        self.assertGreater(report.source_chars, 0)
        self.assertGreater(report.compression_ratio, 0.0)
        self.assertEqual(report.chunk_coverage, 1.0)

    def test_a_chunk_of_only_terms_is_judged_on_its_terms(self):
        """A chunk carrying no numbers must not be "covered" by its numbers.

        Regression: the check required every number *and* every term to be
        missing, so a term-only chunk whose terms were all dropped reported
        ``chunk_coverage == 1.0`` while ``missing_terms`` listed the loss.
        """
        source = "در این جلسه درباره Metformin و Glibenclamide و Insulin صحبت می‌کنیم و نحوهٔ تجویز را توضیح می‌دهیم."
        notes = StructuredNotes(
            title="د",
            sections=(NoteSection(heading="ب", paragraphs=("موضوع دیگری مطرح شد.",)),),
        )
        report = self._report(notes, source)
        self.assertIn("Metformin", report.missing_terms)
        self.assertEqual(report.uncovered_chunks, (1,))
        self.assertEqual(report.chunk_coverage, 0.0)

    def test_a_chunk_keeping_one_signal_is_still_covered(self):
        source = "درس درباره Metformin و Glibenclamide و دوز 500 mg است."
        notes = StructuredNotes(
            title="د",
            sections=(NoteSection(heading="ب", paragraphs=("دوز 500 mg",)),),
        )
        report = self._report(notes, source)
        self.assertEqual(report.uncovered_chunks, ())
        self.assertEqual(report.chunk_coverage, 1.0)

    def test_empty_source_defines_coverage_as_complete(self):
        report = run_note_qa(StructuredNotes(title="t"), [""])
        self.assertEqual(report.coverage, 1.0)
        self.assertFalse(report.needs_repair)

    def test_heavily_compressed_notes_are_flagged(self):
        """A very small ratio is the worst case and must still be reported.

        Regression: the repair gate once required ``ratio >= MIN_RATIO``, so the
        *most* over-compressed notes (ratio ~0.02) were the ones that never
        triggered it.
        """
        source = ("متن درس با جزئیات فراوان دربارهٔ موضوع. " * 200)
        notes = StructuredNotes(
            title="د", sections=(NoteSection(heading="ب", paragraphs=("کوتاه.",)),)
        )
        report = self._report(notes, source)
        self.assertLess(report.compression_ratio, 0.10)
        self.assertTrue(
            any("aggressive compression" in finding for finding in report.findings),
            report.findings,
        )

    def test_short_source_never_trips_the_compression_finding(self):
        # Ratios are noise on a small job; a 2-line "lecture" must stay quiet.
        report = self._report(
            StructuredNotes(title="د", sections=(NoteSection(heading="ب", paragraphs=("کوتاه.",)),)),
            "متن کوتاهی با HbA1c و 500 mg است.",
        )
        self.assertFalse(any("aggressive compression" in f for f in report.findings))
        self.assertFalse(report.needs_repair)

    def test_notes_text_includes_every_schema_block(self):
        notes = StructuredNotes(
            title="t",
            summary="s",
            sections=(
                NoteSection(
                    heading="h",
                    paragraphs=("p",),
                    bullets=("b",),
                    examples=("e",),
                    steps=("s1",),
                    formulas=("f",),
                ),
            ),
        )
        text = run_note_qa(notes, [""]).notes_text_chars
        self.assertGreater(text, 0)


class FontProfileTests(unittest.TestCase):
    def test_named_profiles_resolve(self):
        modern = resolve_font_profile("persian_modern")
        self.assertEqual(modern["body"], "Vazirmatn")
        traditional = resolve_font_profile("traditional")
        self.assertEqual(traditional["body"], "B Nazanin")
        self.assertEqual(traditional["latin"], "Times New Roman")

    def test_unknown_profile_falls_back_to_modern(self):
        self.assertEqual(resolve_font_profile("nope"), resolve_font_profile("persian_modern"))

    def test_settings_docx_fonts_use_the_profile(self):
        settings = make_settings(docx_font_profile="traditional")
        fonts = settings.docx_fonts
        self.assertEqual(fonts["body"], "B Nazanin")
        self.assertEqual(fonts["fallback"], "Tahoma")

    def test_explicit_font_overrides_profile(self):
        settings = make_settings(docx_font_profile="traditional", docx_font_body="Custom")
        self.assertEqual(settings.docx_fonts["body"], "Custom")

    def test_documented_docx_switches_all_reach_the_design(self):
        """Every documented DOCX_* switch reaches the renderer's design object."""
        from unittest.mock import patch

        from gamas_bot.config import Settings

        base = {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_API_ID": "1",
            "TELEGRAM_API_HASH": "h",
            "DOCX_COVER_ENABLED": "false",
            "DOCX_TOC_ENABLED": "true",
            "DOCX_TOC_LEVELS": "1-2",
            "DOCX_TOC_MIN_SECTIONS": "7",
            "DOCX_PAGE_BORDER_ENABLED": "true",
            "DOCX_PAGE_BORDER_STYLE": "double",
            "DOCX_PAGE_BORDER_COLOR": "112233",
            "DOCX_PAGE_BORDER_WIDTH": "12",
            "DOCX_PAGE_BORDER_SPACE": "18",
            "DOCX_SHOW_FOOTER_BRAND": "false",
            "DOCX_BODY_FONT": "BodyFont",
            "DOCX_HEADING_FONT": "HeadFont",
            "DOCX_LATIN_FONT": "LatinFont",
            "DOCX_FALLBACK_FONT": "FallbackFont",
        }
        with patch.dict("os.environ", base, clear=False):
            settings = Settings.from_env()
        design = settings.docx_design
        self.assertFalse(design["cover_enabled"])
        self.assertEqual(design["toc_levels"], "1-2")
        self.assertEqual(design["toc_min_sections"], 7)
        self.assertEqual(design["border_style"], "double")
        self.assertEqual(design["border_color"], "112233")
        self.assertEqual(design["border_size"], 12)
        self.assertEqual(design["border_space"], 18)
        self.assertFalse(design["footer_brand"])
        fonts = settings.docx_fonts
        self.assertEqual(fonts["body"], "BodyFont")
        self.assertEqual(fonts["heading"], "HeadFont")
        self.assertEqual(fonts["latin"], "LatinFont")
        self.assertEqual(fonts["fallback"], "FallbackFont")

    def test_every_profile_declares_a_fallback(self):
        for name, profile in FONT_PROFILES.items():
            self.assertTrue(profile["fallback"], name)

    def test_both_font_role_spellings_are_accepted(self):
        """``DOCX_FONT_BODY`` and ``DOCX_BODY_FONT`` name the same setting."""
        from unittest.mock import patch

        from gamas_bot.config import Settings

        base = {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_API_ID": "1",
            "TELEGRAM_API_HASH": "h",
            # Clear any ambient value so the test is deterministic.
            "DOCX_FONT_BODY": "",
            "DOCX_FONT_HEADING": "",
            "DOCX_FONT_LATIN": "",
            "DOCX_FONT_FALLBACK": "",
        }
        with patch.dict(
            "os.environ",
            {**base, "DOCX_BODY_FONT": "AliasBody", "DOCX_HEADING_FONT": "AliasHead"},
            clear=False,
        ):
            fonts = Settings.from_env().docx_fonts
        self.assertEqual(fonts["body"], "AliasBody")
        self.assertEqual(fonts["heading"], "AliasHead")
        self.assertEqual(fonts["latin"], "Aptos")  # profile default, untouched

        with patch.dict(
            "os.environ",
            {**base, "DOCX_BODY_FONT": "AliasBody", "DOCX_FONT_BODY": "Canonical"},
            clear=False,
        ):
            fonts = Settings.from_env().docx_fonts
        self.assertEqual(fonts["body"], "Canonical")


class RepairPassTests(unittest.IsolatedAsyncioTestCase):
    """The repair is a targeted restoration, never a second summarisation."""

    SOURCE = (
        "جلسه درباره دیابت بود. دوز متفورمین 500 mg است. HbA1c باید زیر 7% باشد. "
        "فشار خون 120/80 و نبض 80 bpm است."
    ) * 12

    POOR = {
        "title": "د",
        "summary": "s",
        "sections": [{"heading": "م", "paragraphs": ["یک خلاصهٔ کوتاه."]}],
    }
    # A faithful booklet: it restates every content unit of the source, keeping
    # the numbers *and* the explanation they belong to. A payload that kept
    # only the numbers — or that reworded a claim into a vaguer one — would be
    # (correctly) treated as degraded and would trigger the repair pass, so the
    # "healthy" fixtures here must be genuinely faithful.
    RICH = {
        "title": "د",
        "summary": "s",
        "sections": [
            {
                "heading": "د",
                "paragraphs": [
                    "در این جلسه دربارهٔ دیابت بود. دوز متفورمین 500 mg است. "
                    "HbA1c باید زیر 7% باشد. فشار خون 120/80 و نبض 80 bpm است."
                ],
            }
        ],
    }

    def _chunks(self):
        return S.split_transcript(
            self.SOURCE, max_chars=S.TRANSCRIPT_CHUNK_CHARS - S._CHUNK_PREFIX_RESERVE
        )

    async def _run(self, payloads, settings=None):
        calls = []

        class Response:
            def __init__(self, payload):
                self._payload = payload

            async def json(self, **kwargs):
                return self._payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            @property
            def status(self):
                return 200

        def envelope(payload):
            return {
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}
                ]
            }

        class Session:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def post(self, url, **kwargs):
                calls.append(1)
                payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
                return Response(envelope(payload))

        with patch.object(S.aiohttp, "ClientSession", Session):
            notes = await S.structure_transcript(
                self.SOURCE, settings or make_settings(), mode="full"
            )
        return notes, len(calls)

    async def test_degraded_notes_trigger_exactly_one_repair(self):
        notes, calls = await self._run([self.POOR, self.RICH])
        self.assertEqual(calls, 2)
        report = run_note_qa(notes, self._chunks())
        self.assertEqual(report.coverage, 1.0)

    async def test_healthy_notes_never_trigger_a_second_call(self):
        notes, calls = await self._run([self.RICH])
        self.assertEqual(calls, 1)
        self.assertEqual(run_note_qa(notes, self._chunks()).coverage, 1.0)

    async def test_repair_is_skipped_when_disabled(self):
        notes, calls = await self._run(
            [self.POOR, self.RICH], make_settings(note_repair_enabled=False)
        )
        self.assertEqual(calls, 1)

    async def test_repair_that_does_not_improve_is_rejected(self):
        notes, calls = await self._run([self.POOR, self.POOR])
        self.assertEqual(calls, 2)
        # The original is kept: the notes must not have become empty/broken.
        self.assertTrue(notes.has_content)

    async def test_repair_obeys_the_character_budget(self):
        # A long source must not be re-sent as one oversized request.
        long_source = "first " * 4000 + "LAST_SLIDE_MARKER"
        with patch(
            "gamas_bot.structuring._structure_chunk",
            new=AsyncMock(return_value=sample_notes_json()),
        ) as chunker:
            await S.structure_presentation(long_source, "", make_settings(), max_chars=4000)
        for call in chunker.await_args_list:
            self.assertLessEqual(len(call.args[0]), 4000)


    #: A booklet that restates every source unit *and* adds a dosage the
    #: lecture never stated. Recall is perfect; the invented 2500 mg is the
    #: only defect, and a recall-only QA could not see it.
    INVENTED = {
        "title": "د",
        "summary": "s",
        "sections": [
            {
                "heading": "د",
                "paragraphs": [
                    "در این جلسه دربارهٔ دیابت بود. دوز متفورمین 500 mg است. "
                    "HbA1c باید زیر 7% باشد. فشار خون 120/80 و نبض 80 bpm است. "
                    "همچنین دوز 2500 mg توصیه شد."
                ],
            }
        ],
    }

    async def test_invented_value_triggers_the_corrective_pass(self):
        """A fabricated fact is corrected even when nothing needs restoring."""
        notes, calls = await self._run([self.INVENTED, self.RICH])
        self.assertEqual(calls, 2)
        report = run_note_qa(notes, self._chunks())
        self.assertFalse(report.has_unsupported_facts, report.unsupported_numbers)
        self.assertEqual(report.coverage, 1.0)

    async def test_faithful_notes_never_trigger_a_corrective_call(self):
        # Regression guard: the new precision trigger must not fire on a
        # booklet that adds nothing.
        notes, calls = await self._run([self.RICH])
        self.assertEqual(calls, 1)
        self.assertFalse(run_note_qa(notes, self._chunks()).has_unsupported_facts)

    async def test_a_provider_that_keeps_the_invented_value_is_not_accepted(self):
        """The pass cannot lower quality by swapping in an equally wrong answer."""
        notes, calls = await self._run([self.INVENTED, self.INVENTED])
        self.assertEqual(calls, 2)
        # The original is kept unchanged rather than "repaired" into another
        # invented booklet; the QA finding is what surfaces the problem.
        report = run_note_qa(notes, self._chunks())
        self.assertTrue(report.has_unsupported_facts)

    async def test_corrective_pass_respects_disabled_repair(self):
        notes, calls = await self._run(
            [self.INVENTED, self.RICH], make_settings(note_repair_enabled=False)
        )
        self.assertEqual(calls, 1)


class RepairPromptTests(unittest.TestCase):
    def test_repair_prompt_names_missing_signals_and_forbids_invention(self):
        prompt = S.build_repair_prompt("متن", ("500 mg", "HbA1c"), ("gap",))
        self.assertIn("500 mg", prompt)
        self.assertIn("HbA1c", prompt)
        # The reminder must forbid invention and keep the JSON contract.
        self.assertIn("حدس نزنید", S.REPAIR_REMINDER)
        self.assertIn("اضافه نکنید", S.REPAIR_REMINDER)
        self.assertIn("JSON", S.REPAIR_REMINDER)


if __name__ == "__main__":
    unittest.main()
