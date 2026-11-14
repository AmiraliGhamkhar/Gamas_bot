"""Lightweight integrity checks for final student-facing artifacts.

These checks answer one question only: *can this file be delivered as what its
extension claims?* They are not content validators. The PDF/DOCX checks confirm
the container envelope; the HTML check confirms the offline study booklet is a
complete, self-contained UTF-8 document without external references. Content
quality is the note pipeline's job (grounding + repair), not a validator's.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO


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


#: Upper bound for one study booklet. Two embedded faces are roughly 0.35 MB in
#: base64; the rest is booklet text, so this is generous but finite.
MAX_STUDY_HTML_BYTES = 8 * 1024 * 1024
#: A reference to anything the document would have to fetch to render itself.
_EXTERNAL_REFERENCE = re.compile(
    r"""(?:
        (?:src|href)\s*=\s*["']?(?:https?:)?//
        | url\(\s*["']?(?!data:)
        | @import
        | fonts\.googleapis
        | fonts\.gstatic
        | cdn\.
        | <script\b
        | <iframe\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def validate_study_html_artifact(content: bytes) -> None:
    """The smallest check the delivery path needs for the offline booklet.

    Rejected: empty or oversized output, non-UTF-8 bytes, a document that is not
    a complete HTML envelope in Persian RTL, and any reference that would need a
    network request or a script. Nothing here judges the educational content.
    """
    if not content:
        raise ValueError("study HTML is empty")
    if len(content) > MAX_STUDY_HTML_BYTES:
        raise ValueError("study HTML exceeds the size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("study HTML is not valid UTF-8") from exc
    if not text.lstrip().lower().startswith("<!doctype html>"):
        raise ValueError("study HTML is missing <!DOCTYPE html>")
    if not re.search(r'<html[^>]*\blang="fa"', text, re.IGNORECASE) or not re.search(
        r"<html[^>]*\bdir=\"rtl\"", text, re.IGNORECASE
    ):
        raise ValueError('study HTML root must be <html lang="fa" dir="rtl">')
    if "<title>" not in text or "</html>" not in text:
        raise ValueError("study HTML document is incomplete")
    if _EXTERNAL_REFERENCE.search(text):
        raise ValueError("study HTML references an external resource or script")
