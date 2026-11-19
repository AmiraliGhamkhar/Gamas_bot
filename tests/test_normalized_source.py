"""Synthetic sources only; no live Telegram, files from users, or provider calls."""
from __future__ import annotations

import io
import json
import zipfile
import unittest
from pathlib import Path
from unittest import mock

from gamas_bot.doc_extract import (
    DocExtractError,
    DocSection,
    ExtractedDoc,
    chunk_sections,
    chunks_from_json,
    chunks_to_json,
    extract_document,
)
from gamas_bot.llm import LLMContextOverflowError, LLMOutputBudgetError
from gamas_bot.document_model import NormalizedDocument
from gamas_bot.notes import build_presentation_document, structure_document
from gamas_bot.office_notes import generate_full_booklet, office_source_chunks, source_text
from gamas_bot.presentations import CHART_NS, DIAGRAM_NS, DRAWING_NS, _chart_lines, _diagram_lines
from gamas_bot.transcript import Paragraph, TranscriptDocument, split_transcript
from tests.support import make_settings


def _payload(text: str) -> dict:
    return {"title": "درس آزمایشی", "sections": [{"heading": "نمونه", "paragraphs": [text]}]}


class NormalizedSourceTests(unittest.TestCase):
    def test_contract_round_trip_exact_offsets_and_content_addressed_ids(self):
        raw = "\ufeff  مقدمه\r\n\r\nاتصال‌واژه  و  3.50 mg\r پایان  "
        doc = NormalizedDocument.from_source_text(raw, source_type="txt", title="تمرین", metadata=(("unit", "1"),))
        self.assertEqual(doc.text, "  مقدمه\n\nاتصال‌واژه  و  3.50 mg\n پایان  ")
        self.assertEqual("".join(block.text for block in doc.source_blocks), doc.text)
        self.assertEqual(doc, NormalizedDocument.from_json(doc.to_json()))
        self.assertEqual(doc.source_blocks, NormalizedDocument.from_source_text(doc.text, source_type="txt").source_blocks)
        self.assertNotEqual(doc.source_blocks[0].id, NormalizedDocument.from_source_text(raw + "!", source_type="txt").source_blocks[0].id)
        payload = json.loads(doc.to_json())
        payload["units"][0]["end_offset"] -= 1
        with self.assertRaisesRegex(ValueError, "partition"):
            NormalizedDocument.from_json(json.dumps(payload))

    def test_adapters_keep_physical_locators_outside_transcript_text(self):
        doc = NormalizedDocument.from_extracted(ExtractedDoc("pdf", 3, (
            DocSection("صفحهٔ ۱", "", "اول"), DocSection("صفحهٔ ۳", "", "آخر"),
        ), (2,)))
        self.assertEqual([block.page_number for block in doc.source_blocks], [1, 3])
        self.assertEqual(doc.metadata_map["pages_without_text"], "2")
        timed = TranscriptDocument((Paragraph("نمونهٔ علمی", 1.25, 2.5, "نام آزمایشی"),))
        speech = NormalizedDocument.from_transcript(timed, source_type="video")
        self.assertEqual(speech.text, timed.llm_document())
        self.assertEqual((speech.source_blocks[0].start_ms, speech.source_blocks[0].end_ms), (1250, 2500))
        self.assertNotIn("نام آزمایشی", speech.text)

    def test_hard_cut_preserves_source_spans_and_rejects_dropped_or_reordered_text(self):
        doc = NormalizedDocument.from_source_text("  ابتدا\n\nabcδε‌123\n  پایان  ", source_type="txt")
        for budget in (1, 2, 7, 30):
            with self.subTest(budget=budget):
                pieces = split_transcript(doc.text, budget)
                self.assertTrue(all(0 < len(piece) <= budget for piece in pieces))
                ranges = doc.chunk_ranges(pieces)
                self.assertEqual("".join(doc.text[r.start_offset:r.end_offset] for r in ranges), doc.text)
                self.assertEqual({i for r in ranges for i in r.block_ids}, {b.id for b in doc.source_blocks})
        with self.assertRaisesRegex(ValueError, "unassigned"):
            doc.chunk_ranges(["ابتدا"])
        with self.assertRaisesRegex(ValueError, "changed or reordered"):
            doc.chunk_ranges(["پایان", "ابتدا"])

    def test_corrupt_partial_and_stale_caches_cannot_replace_complete_stored_text(self):
        sections = [DocSection("صفحهٔ ۱", "", "ابتدا\n[صفحهٔ ۱]\nمتن اصلی"), DocSection("صفحهٔ ۲", "", "انتهای متمایز متن آزمایشی")]
        chunks = chunk_sections(sections, max_chars=64)
        stored = "\n\n".join(section.render() for section in sections)
        self.assertEqual(chunks_from_json(chunks_to_json(chunks), source_text=stored), chunks)
        self.assertEqual(chunks_from_json(chunks_to_json(chunks[:1]), source_text=stored), [])
        broken = json.loads(chunks_to_json(chunks))
        broken[1] = None
        self.assertEqual(chunks_from_json(json.dumps(broken), source_text=stored), [])
        rebuilt = office_source_chunks(stored, [])
        self.assertEqual("\n\n".join(chunk.text for chunk in rebuilt), stored)
        located = office_source_chunks(stored, chunks)
        self.assertIn("[صفحهٔ ۱]", source_text(located, include_locators=False))

    def test_supported_chart_and_smartart_parts_keep_the_tail(self):
        diagram_nodes = "".join(
            f"<d:pt><d:t><a:p><a:r><a:t>Node-{i:03d}</a:t></a:r></a:p></d:t></d:pt>"
            for i in range(210)
        )
        chart_points = "".join(f'<c:pt idx="{i}"><c:v>{1000+i}</c:v></c:pt>' for i in range(75))
        category_points = "".join(f'<c:pt idx="{i}"><c:v>Category-{i}</c:v></c:pt>' for i in range(75))
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as output:
            output.writestr("diagram.xml", f'<d:dataModel xmlns:d="{DIAGRAM_NS}" xmlns:a="{DRAWING_NS}">{diagram_nodes}</d:dataModel>')
            output.writestr("chart.xml", f'<c:chart xmlns:c="{CHART_NS}"><c:ser><c:cat>{category_points}</c:cat><c:val>{chart_points}</c:val></c:ser></c:chart>')
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(_diagram_lines(archive, "diagram.xml"), [f"Node-{i:03d}" for i in range(210)])
            chart = "\n".join(_chart_lines(archive, "chart.xml"))
        self.assertIn("Category-74=1074", chart)
        self.assertNotIn("…", chart)

    def test_pdf_page_failure_does_not_return_partial_extraction(self):
        pages = [mock.Mock(), mock.Mock(), mock.Mock()]
        pages[0].extract_text.return_value = "An extractable synthetic page."
        pages[1].extract_text.side_effect = ValueError("synthetic parse failure")
        pages[2].extract_text.return_value = "An extractable synthetic tail."
        reader = mock.Mock(pages=pages, is_encrypted=False)
        with mock.patch("pypdf.PdfReader", return_value=reader):
            with self.assertRaisesRegex(DocExtractError, "صفحهٔ ۲"):
                extract_document(Path("fixture-only.pdf"), "pdf")


class NormalizedBookletTests(unittest.IsolatedAsyncioTestCase):
    async def test_aligned_presentation_preserves_both_preambles_and_duplicate_title(self):
        outline = (
            "مقدمهٔ مستقل اسلایدها.\n\n### اسلاید 1 — عنوان یک\n\nبدنهٔ اول.\n\n"
            "### اسلاید 1 — عنوان جایگزین\n\nبدنهٔ دیگر."
        )
        narration = "مقدمهٔ مستقل گفتار.\n\n### گفتار مرتبط با اسلاید 1\n\nگفتار اصلی."
        text = build_presentation_document(outline, narration)
        for fragment in ("مقدمهٔ مستقل اسلایدها.", "مقدمهٔ مستقل گفتار.", "عنوان جایگزین", "بدنهٔ اول.", "بدنهٔ دیگر.", "گفتار اصلی."):
            self.assertEqual(text.count(fragment), 1)
        with mock.patch("gamas_bot.notes.complete_json", new=mock.AsyncMock(return_value=_payload("نمونهٔ آموزشی."))):
            notes = await structure_document(text, make_settings(note_chunk_chars=90), object(), presentation=True)
        self.assertGreater(len(notes.input_ranges), 1)
        self.assertEqual(notes.input_ranges[-1].end_offset, len(text))
        self.assertNotIn("input_ranges", notes.to_payload())

    async def test_pathological_chunk_count_recovers_all_ranges_without_provider_calls(self):
        source = "\n\n".join(f"UNIT-{index:04d}: " + "x" * 70 for index in range(215))
        doc = NormalizedDocument.from_source_text(source, source_type="txt")
        settings = make_settings(note_chunk_chars=96, note_compile=True, note_completeness_fill=True)
        provider = mock.AsyncMock()
        with mock.patch("gamas_bot.notes.complete_json", new=provider):
            notes = await structure_document(doc, settings, object())
        provider.assert_not_awaited()
        self.assertEqual(notes.quality_state, "RECOVERED")
        self.assertEqual(len(notes.input_ranges), 215)
        self.assertEqual("".join(doc.text[span.start_offset:span.end_offset] for span in notes.input_ranges), doc.text)
        for index in range(215):
            self.assertIn(f"UNIT-{index:04d}", notes.to_json())

    async def test_context_and_output_overflow_retries_include_all_source_characters(self):
        text = "HEAD " + "الف 123.45 mg. " * 20 + "TAIL"
        for error in (LLMContextOverflowError, LLMOutputBudgetError):
            with self.subTest(error=error):
                sent: list[str] = []

                async def provider(_session, _settings, *, user, sent=sent, error=error, **_kwargs):
                    body = user.split("\n\n", 1)[1]
                    sent.append(body)
                    if len(sent) == 1:
                        raise error("synthetic budget exhaustion")
                    return _payload(body)

                with mock.patch("gamas_bot.notes.complete_json", new=provider):
                    await structure_document(text, make_settings(note_compile=False), object())
                self.assertGreater(len(sent), 2)
                NormalizedDocument.from_source_text(text, source_type="txt").chunk_ranges(sent[1:])

    async def test_office_full_booklet_over_previous_cap_retains_the_source_tail(self):
        text = "آغاز منحصربه‌فرد.\n\n" + "توضیح آموزشی آزمایشی. " * 12500 + "\n\nپایان منحصربه‌فرد."
        self.assertGreater(len(text), 250_000)
        notes, recovered = await generate_full_booklet(
            office_source_chunks(text), title="تمرین", settings=make_settings(), session=None,
        )
        self.assertTrue(recovered)
        self.assertIn("آغاز منحصربه‌فرد", notes.to_json())
        self.assertIn("پایان منحصربه‌فرد", notes.to_json())
