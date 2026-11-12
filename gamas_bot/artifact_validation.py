"""Lightweight integrity checks for final student-facing PDF/DOCX artifacts."""

from __future__ import annotations

from io import BytesIO
import zipfile


def validate_pdf_artifact(content: bytes) -> None:
    """Require a complete, parseable PDF with at least one page."""
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise ValueError("renderer returned an invalid PDF envelope")
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if not reader.pages:
            raise ValueError("renderer returned a PDF with no pages")
    except Exception as exc:
        raise ValueError("renderer returned an invalid PDF document") from exc


def validate_docx_artifact(content: bytes) -> None:
    """Require an intact OOXML ZIP with the main Word document parts."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as package:
            required_parts = {"[Content_Types].xml", "word/document.xml"}
            if not required_parts.issubset(package.namelist()) or package.testzip():
                raise ValueError("renderer returned an invalid DOCX package")
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ValueError("renderer returned an invalid DOCX package") from exc
