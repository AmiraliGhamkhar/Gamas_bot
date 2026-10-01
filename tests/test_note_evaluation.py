"""Deterministic note-quality benchmark over the lecture corpus.

``tests/fixtures/notes`` holds realistic source transcripts (medical, HCI,
computer science, fully Persian, heavy code-switching and a PowerPoint slide
outline) paired with hand-written *reference documents* — the shape a good
model answer should take.  This module measures, without any network, provider
or model call, whether the deterministic pipeline keeps that corpus intact:

* ``split_transcript`` must be lossless and ordered for every fixture;
* ``run_note_qa`` must observe at least the coverage floor declared in
  ``corpus.json`` between the reference notes and the source transcript;
* ``merge_structured_notes`` must not lose blocks when chunks are joined;
* the DOCX renderer must emit a readable document for every reference.

A real model can only ever be *scored* against these fixtures; the checks here
are the reproducible baseline that a prompt change must not regress.
"""

from __future__ import annotations

import io
import json
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from gamas_bot.docx_export import DocumentMeta, build_notes_docx
from gamas_bot.qa import run_note_qa
from gamas_bot.structuring import merge_structured_notes, parse_structured_notes, split_transcript

from support import docx_text

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "notes"
CORPUS = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
ENTRIES = CORPUS["fixtures"]
ENTRY_IDS = [entry["id"] for entry in ENTRIES]

META = DocumentMeta(reference="BENCH-001", created_at=None)


def load_reference(entry: dict):
    payload = (FIXTURES / entry["reference"]).read_text(encoding="utf-8")
    return parse_structured_notes(payload)


def load_transcript(entry: dict) -> str:
    return (FIXTURES / entry["transcript"]).read_text(encoding="utf-8")


def coverage(report, source: int, preserved: int) -> float:
    return 1.0 if not source else preserved / source


class CorpusTests(unittest.TestCase):
    def test_corpus_covers_every_required_domain(self):
        self.assertEqual(
            {entry["kind"] for entry in ENTRIES},
            {"medical", "hit", "cs", "persian_only", "persian_english", "pptx"},
        )
        self.assertEqual(len(ENTRY_IDS), len(set(ENTRY_IDS)))

    def test_every_fixture_file_exists(self):
        for entry in ENTRIES:
            with self.subTest(fixture=entry["id"]):
                self.assertTrue((FIXTURES / entry["transcript"]).is_file())
                self.assertTrue((FIXTURES / entry["reference"]).is_file())

    def test_references_declare_only_known_blocks(self):
        for entry in ENTRIES:
            with self.subTest(fixture=entry["id"]):
                notes = load_reference(entry)
                self.assertTrue(notes.has_content)
                self.assertTrue(notes.sections)
                self.assertGreaterEqual(entry["min_number_coverage"], 1.0)
                self.assertGreaterEqual(entry["min_term_coverage"], 0.8)


class ChunkingBenchmarkTests(unittest.TestCase):
    def test_chunking_is_lossless_for_every_fixture(self):
        for entry in ENTRIES:
            with self.subTest(fixture=entry["id"]):
                text = load_transcript(entry)
                chunks = split_transcript(text)
                self.assertTrue(chunks)
                self.assertEqual(" ".join(chunks).split(), text.split())

    def test_chunking_stays_lossless_under_tight_budgets(self):
        for entry in ENTRIES:
            text = load_transcript(entry)
            for max_chars in (400, 900, 22000):
                with self.subTest(fixture=entry["id"], max_chars=max_chars):
                    chunks = split_transcript(text, max_chars=max_chars)
                    self.assertEqual(" ".join(chunks).split(), text.split())
                    self.assertTrue(all(len(chunk) <= max_chars for chunk in chunks))

    def test_repeated_chunking_is_idempotent(self):
        # Chunking a transcript that already carries chunk prefixes must not
        # amplify or drop content beyond the prefix itself.
        for entry in ENTRIES:
            with self.subTest(fixture=entry["id"]):
                text = load_transcript(entry)
                once = split_transcript(text, max_chars=900)
                twice = split_transcript(" ".join(once), max_chars=900)
                self.assertEqual(
                    " ".join(twice).split(),
                    " ".join(once).split(),
                )


class CoverageBenchmarkTests(unittest.TestCase):
    def test_reference_notes_meet_the_declared_coverage(self):
        for entry in ENTRIES:
            with self.subTest(fixture=entry["id"]):
                report = run_note_qa(load_reference(entry), [load_transcript(entry)])
                numbers = coverage(
                    report, report.source_numbers, report.preserved_numbers
                )
                terms = coverage(report, report.source_terms, report.preserved_terms)
                self.assertGreaterEqual(
                    numbers, entry["min_number_coverage"], report.missing_numbers
                )
                self.assertGreaterEqual(
                    terms, entry["min_term_coverage"], report.missing_terms
                )
                self.assertEqual(report.uncovered_chunks, ())

    def test_persian_only_fixture_reports_no_signals(self):
        entry = next(e for e in ENTRIES if e["kind"] == "persian_only")
        report = run_note_qa(load_reference(entry), [load_transcript(entry)])
        self.assertEqual(report.source_numbers, 0)
        self.assertEqual(report.source_terms, 0)
        self.assertFalse(report.has_findings)

    def test_a_truncated_document_is_caught(self):
        # The benchmark must be able to fail: drop a dosage-bearing section and
        # the same corpus entry reports a gap.
        entry = next(e for e in ENTRIES if e["kind"] == "medical")
        notes = load_reference(entry)
        stripped = replace(
            notes,
            sections=notes.sections[:1],
            key_points=(),
            glossary=(),
        )
        report = run_note_qa(stripped, [load_transcript(entry)])
        self.assertTrue(report.missing_numbers)
        self.assertLess(report.preserved_numbers, report.source_numbers)


class PipelineBenchmarkTests(unittest.TestCase):
    def test_merge_of_chunked_notes_is_additive_but_deduplicated(self):
        for entry in ENTRIES:
            with self.subTest(fixture=entry["id"]):
                notes = load_reference(entry)
                merged = merge_structured_notes([notes, notes])
                # Merging is additive: both chunk results keep their sections.
                self.assertEqual(len(merged.sections), 2 * len(notes.sections))
                # ...while verbatim repeats collapse instead of stacking.
                self.assertEqual(merged.key_points, notes.key_points)
                self.assertEqual(merged.glossary, notes.glossary)
                for index, section in enumerate(notes.sections):
                    self.assertEqual(
                        merged.sections[index].bullets, section.bullets
                    )

    def test_every_reference_renders_to_a_readable_document(self):
        for entry in ENTRIES:
            with self.subTest(fixture=entry["id"]):
                notes = load_reference(entry)
                text = docx_text(build_notes_docx(notes, meta=META))
                self.assertIn(notes.title, text)
                self.assertIn(notes.summary, text)
                self.assertGreater(len(text), 400)

    def test_mixed_script_documents_keep_both_directions(self):
        entry = next(e for e in ENTRIES if e["kind"] == "medical")
        notes = load_reference(entry)
        data = build_notes_docx(notes, meta=META)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("<w:rtl/>", body)
        self.assertIn('<w:rtl w:val="0"/>', body)


if __name__ == "__main__":
    unittest.main()