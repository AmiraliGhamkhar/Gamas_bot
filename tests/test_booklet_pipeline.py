"""Offline integration at the source, provider, and artifact boundaries."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from docx import Document
from pptx import Presentation
from reportlab.pdfgen import canvas

from gamas_bot import llm, notes as n
from gamas_bot.artifact_validation import validate_docx_artifact, validate_pdf_artifact
from gamas_bot.doc_extract import chunk_sections, extract_document
from gamas_bot.document_model import NormalizedDocument
from gamas_bot.docx_export import DocumentMeta, build_notes_docx
from gamas_bot.notes_pdf import build_notes_pdf
from gamas_bot.office import read_text_file
from gamas_bot.office_notes import generate_full_booklet, office_source_chunks
from gamas_bot.review_sheet import render_booklet_review
from tests.support import docx_text, make_settings
from tests.test_notes import FakeSession, _FakeResponse

CONTENT = (
    "Plants create sugars from water and sunlight.",
    "Roots absorb water and dissolved minerals.",
    "Leaves exchange gases through small openings.",
    "Stems transport water to the leaves.",
)


def _wire_reply(text, *, exact=False):
    payload = {"choices": [{"message": {"content": text}}]}
    if exact:
        payload["usage"] = {"prompt_tokens": 123, "completion_tokens": 45}
    return _FakeResponse(200, payload)


def _make_source(path: Path, kind: str) -> None:
    if kind == "txt":
        path.write_text("\n".join(CONTENT), encoding="utf-8")
    elif kind == "pdf":
        pdf = canvas.Canvas(str(path))
        for index, line in enumerate(CONTENT):
            pdf.drawString(60, 750 - index * 18, line)
        pdf.save()
    elif kind == "docx":
        document = Document()
        document.add_heading("Plants", 1)
        for line in CONTENT:
            document.add_paragraph(line)
        document.save(path)
    else:
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "Plants"
        slide.placeholders[1].text = "\n".join(CONTENT)
        presentation.save(str(path))


@pytest.mark.parametrize("kind", ["txt", "pdf", "pptx", "docx"])
@pytest.mark.parametrize("strict", [False, True])
def test_extracted_file_reaches_verified_office_booklet_and_real_renderers(kind, strict, tmp_path):
    path = tmp_path / f"plants.{kind}"
    _make_source(path, kind)
    if kind == "txt":
        chunks = office_source_chunks(read_text_file(path, max_bytes=2048), [])
    else:
        extracted = extract_document(path, kind)
        flat = "\n\n".join(section.render() for section in extracted.sections)
        chunks = office_source_chunks(flat, chunk_sections(extracted.sections))
    payload = {"title": "Plants", "sections": [{"heading": "Plants", "paragraphs": CONTENT}]}
    session = FakeSession([json.dumps(payload)])
    settings = make_settings(db_path=tmp_path / "usage.sqlite", avalai_api_key="fake-key", note_compile=False,
                             note_strict_grounding=strict)
    booklet, fallback = asyncio.run(generate_full_booklet(chunks, title="Plants", settings=settings,
                                                        session=session, source_type=kind))
    assert not fallback
    assert booklet.verification_report is not None and booklet.verification_report.passed
    assert len(session.calls) == 1 and CONTENT[0] in str(session.calls[0]["json"])
    if kind == "pptx":
        assert "اسلاید ۱" in booklet.sections[0].source_locators
    before = len(session.calls)
    pdf, docx = build_notes_pdf(booklet), build_notes_docx(booklet, meta=DocumentMeta(reference="TEST"))
    validate_pdf_artifact(pdf)
    validate_docx_artifact(docx)
    assert CONTENT[-1] in docx_text(docx)
    with patch("gamas_bot.review_sheet.complete_json", side_effect=AssertionError("render must be offline")):
        review = asyncio.run(render_booklet_review(booklet, reference="TEST"))
    assert review and b'<html lang="fa" dir="rtl">' in review.content
    assert len(session.calls) == before


def test_editor_runs_before_bounded_repair_and_incomplete_artifact_is_retained(tmp_path):
    measured = "طول نمونه در آزمایش برابر ۷۵ میلی‌متر است."
    incomplete = "طول نمونه در آزمایش ثبت شد."
    other = "وجود تغییر در رنگ نمونه به کمک نور مشخص شد."
    source = NormalizedDocument.from_source_text(f"# اندازه\n\n{measured}\n\n# رنگ\n\n{other}")
    activity = []

    async def provider(_session, _settings, *, task, user, **_kwargs):
        activity.append(task)
        if task == "BOOKLET_GENERATION":
            text = incomplete if activity.count(task) == 1 else other
            return {"title": "نمونه", "sections": [{"heading": "نتیجه", "paragraphs": [text]}]}
        if task == "BOOKLET_COMPILE":
            return {"order": ["s0001", "s0002"]}
        if task == "BOOKLET_REPAIR":
            assert measured in user and other not in user
            return {"sections": [{"heading": "نتیجه", "paragraphs": [incomplete]}]}
        pytest.fail(f"unexpected task {task}")

    settings = make_settings(db_path=tmp_path / "usage.sqlite", avalai_api_key="fake-key", note_compile=True,
                             note_strict_grounding=True)
    with patch.object(n, "complete_json", provider), patch.object(n, "COMPILE_MIN_NOTES_CHARS", 0):
        result = asyncio.run(n.structure_document(source, settings, object()))
    assert activity == ["BOOKLET_GENERATION", "BOOKLET_GENERATION", "BOOKLET_COMPILE", "BOOKLET_REPAIR"]
    assert result.verification_report is not None and not result.verification_report.passed
    assert "missing_number" in {issue.code for issue in result.verification_report.issues}
    assert other in result.to_markdown()
    assert asyncio.run(render_booklet_review(result, reference="TEST")) is None


def test_attempt_telemetry_has_stage_retry_exact_counts_estimates_and_no_content(tmp_path, caplog):
    settings = make_settings(db_path=tmp_path / "usage.sqlite", note_providers=("avalai", "openrouter"),
                             avalai_api_key="very-private-key", openrouter_api_key="second-private-key")
    session = FakeSession([_FakeResponse(503, {"error": {"message": "unavailable"}}), _wire_reply("{}", exact=True)])
    with caplog.at_level(logging.INFO, logger="gamas_bot.llm"), patch.object(llm, "_retry_delay_seconds", return_value=0):
        assert asyncio.run(llm.complete_json(session, settings, task="BOOKLET_VERIFY",
                                             system="PRIVATE INSTRUCTIONS", user="PRIVATE CONTENT")) == {}
        session = FakeSession([_wire_reply("not-json-but-has-text"), "{}"])
        assert asyncio.run(llm.complete_json(session, settings, task="BOOKLET_REPAIR", strict_json=True,
                                             system="PRIVATE INSTRUCTIONS", user="PRIVATE CONTENT")) == {}
    records = [record.getMessage() for record in caplog.records if record.getMessage().startswith("llm request stage=")]
    assert len(records) == 4
    assert "stage=BOOKLET_VERIFY" in records[0] and "provider=avalai" in records[0] and "retries=0" in records[0]
    assert "error=LLMTransientError" in records[0] and "estimated_input_tokens=None" not in records[0]
    assert "retries=1" in records[1] and "error=none input_tokens=123 output_tokens=45" in records[1]
    assert "stage=BOOKLET_REPAIR" in records[2] and "error=LLMResponseError" in records[2]
    assert "estimated_output_tokens=None" not in records[2]
    assert "provider=openrouter" in records[3] and "retries=0" in records[3]
    assert all("model=" in line and "duration_ms=" in line for line in records)
    assert all(private not in "\n".join(records) for private in ("PRIVATE ", "private-key", "not-json-but-has-text"))
