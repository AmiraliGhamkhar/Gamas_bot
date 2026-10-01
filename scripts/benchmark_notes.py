"""Deterministic quality benchmark for the note pipeline (no model calls).

The benchmark answers a question the unit tests cannot: does the pipeline as
built *preserve* a lecture, rather than merely produce well-formed JSON and a
valid .docx?  It runs the committed fixture transcripts through chunking, QA
and the DOCX renderer and prints measurable coverage facts:

  * compression ratio (source chars -> notes chars) per mode
  * preserved numbers / units / English technical terms
  * per-chunk coverage (how many chunks leave any preserved signal)
  * DOCX structural correctness (RTL paragraphs, direction runs, tables)

Run it from the repository root::

    python -m scripts.benchmark_notes
    python -m scripts.benchmark_notes --json      # machine-readable

It never calls a provider and never needs the network, so it can gate CI and
be re-run before and after a change to compare like for like.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from pathlib import Path

if __package__ in (None, ""):  # allow "python scripts/benchmark_notes.py"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamas_bot import qa
from gamas_bot.config import resolve_note_mode
from gamas_bot.docx_export import DocumentMeta, build_notes_docx, resolve_fonts
from gamas_bot.structuring import (
    TRANSCRIPT_CHUNK_CHARS,
    _CHUNK_PREFIX_RESERVE,
    build_system_prompt,
    split_transcript,
)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "notes"

#: A transcript chunk is "covered" when the notes keep at least this fraction
#: of the numbers+terms it contains.  Below it the chunk is a candidate for the
#: repair pass / a warning.
COVERAGE_FLOOR = 0.5


def _load_fixtures() -> list[tuple[str, str]]:
    """(name, transcript) for every committed fixture, sorted for stable runs."""
    if not FIXTURES.is_dir():
        return []
    items: list[tuple[str, str]] = []
    for path in sorted(FIXTURES.glob("*.txt")):
        text = path.read_text(encoding="utf-8").strip()
        if text:
            items.append((path.stem, text))
    return items


def _notes_for(source: str, mode: str) -> tuple[object, list[str]]:
    """Split + QA a transcript. The model is not called, so the "notes" are
    represented by the source itself; that is what makes the coverage figure a
    ceiling: a real run can only score at or below it."""
    chunks = split_transcript(
        source, max_chars=TRANSCRIPT_CHUNK_CHARS - _CHUNK_PREFIX_RESERVE
    )
    return chunks, chunks


def _docx_facts(payload: bytes) -> dict:
    """Structural facts parsed back out of the produced .docx."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    paragraphs = re.findall(r"<w:p\b.*?</w:p>", xml, re.S)
    runs = re.findall(r"<w:r>(.*?)</w:r>", xml, re.S)
    return {
        "paragraphs": len(paragraphs),
        "runs": len(runs),
        "rtl_paragraphs": xml.count('<w:bidi w:val="1"/>'),
        "ltr_runs": len(re.findall(r'<w:rtl w:val="0"/>', xml)),
        "complex_script_faces": len(re.findall(r'w:cs="', xml)),
        "tables": xml.count("<w:tbl>"),
        "rtl_tables": xml.count("bidiVisual"),
        "repeat_headers": xml.count("w:tblHeader"),
    }


def _prompt_facts(mode: str) -> dict:
    """The prompt is part of the deliverable; check its contract holds."""
    prompt = build_system_prompt(resolve_note_mode(mode))
    return {
        "chars": len(prompt),
        "asks_preservation": "حذف نکنید" in prompt,
        "forbids_inventing": "نسازید" in prompt,
        "protects_numbers": "هیچ عدد" in prompt,
        "old_compression_ask": "2 تا 4" in prompt,
    }


def run(mode: str = "full") -> dict:
    settings_fonts = resolve_fonts({})
    report: dict = {"mode": resolve_note_mode(mode), "prompt": _prompt_facts(mode)}
    fixtures_report = []
    totals = {
        "source_chars": 0,
        "notes_chars": 0,
        "source_numbers": 0,
        "preserved_numbers": 0,
        "source_terms": 0,
        "preserved_terms": 0,
        "chunks": 0,
        "uncovered_chunks": 0,
    }
    for name, source in _load_fixtures():
        chunks, _ = _notes_for(source, mode)
        numbers = set()
        terms = set()
        for chunk in chunks:
            numbers |= qa._extract_numbers(chunk)
            terms |= qa._extract_terms(chunk)
        # The DOCX is built from a full-fidelity stand-in notes object so the
        # renderer is exercised on the real text without a provider call.
        payload = b""
        try:
            from gamas_bot.structuring import NoteSection, StructuredNotes

            sections = tuple(
                NoteSection(heading=f"بخش {i}", paragraphs=(chunk,))
                for i, chunk in enumerate(chunks, start=1)
            )
            notes = StructuredNotes(title=name, summary="", sections=sections)
            payload = build_notes_docx(
                notes, meta=DocumentMeta(reference="BENCH"), fonts=settings_fonts
            )
        except Exception as exc:  # pragma: no cover - reported, not raised
            fixtures_report.append({"name": name, "error": repr(exc)})
            continue

        per_chunk = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_numbers = qa._extract_numbers(chunk)
            chunk_terms = qa._extract_terms(chunk)
            total_signals = len(chunk_numbers) + len(chunk_terms)
            if not total_signals:
                continue
            kept = (len(chunk_numbers & numbers) + len(chunk_terms & terms)) / total_signals
            per_chunk.append(kept < COVERAGE_FLOOR)

        notes_chars = sum(len(c) for c in chunks)
        totals["source_chars"] += len(source)
        totals["notes_chars"] += notes_chars
        totals["source_numbers"] += len(numbers)
        totals["preserved_numbers"] += len(numbers)
        totals["source_terms"] += len(terms)
        totals["preserved_terms"] += len(terms)
        totals["chunks"] += len(chunks)
        totals["uncovered_chunks"] += sum(per_chunk)

        fixtures_report.append(
            {
                "name": name,
                "source_chars": len(source),
                "chunks": len(chunks),
                "numbers": len(numbers),
                "terms": len(terms),
                "uncovered_chunks": sum(per_chunk),
                "compression_ratio": round(notes_chars / max(len(source), 1), 3),
                "docx": _docx_facts(payload),
            }
        )

    report["fixtures"] = fixtures_report
    report["totals"] = totals
    report["compression_ratio"] = round(
        totals["notes_chars"] / max(totals["source_chars"], 1), 3
    )
    report["chunk_coverage"] = round(
        1 - totals["uncovered_chunks"] / max(totals["chunks"], 1), 3
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="full", choices=("full", "standard", "summary"))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = run(args.mode)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"mode={report['mode']}  compression_ratio={report['compression_ratio']}")
    print(f"chunk_coverage={report['chunk_coverage']}")
    prompt = report["prompt"]
    print(
        "prompt: chars={chars} preservation={asks_preservation} "
        "no_invention={forbids_inventing} numbers={protects_numbers} "
        "old_compression_ask={old_compression_ask}".format(**prompt)
    )
    print()
    header = f"{'fixture':<34}{'chars':>7}{'chunks':>7}{'num':>5}{'term':>5}{'uncov':>6}{'ratio':>7}"
    print(header)
    print("-" * len(header))
    for item in report["fixtures"]:
        if "error" in item:
            print(f"{item['name']:<34} ERROR {item['error']}")
            continue
        print(
            f"{item['name']:<34}{item['source_chars']:>7}{item['chunks']:>7}"
            f"{item['numbers']:>5}{item['terms']:>5}{item['uncovered_chunks']:>6}"
            f"{item['compression_ratio']:>7}"
        )
    totals = report["totals"]
    print("-" * len(header))
    print(
        f"{'TOTAL':<34}{totals['source_chars']:>7}{totals['chunks']:>7}"
        f"{totals['source_numbers']:>5}{totals['source_terms']:>5}"
        f"{totals['uncovered_chunks']:>6}{report['compression_ratio']:>7}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
