"""Final booklet bytes are validated before they can be advertised or stored."""

from __future__ import annotations

import unittest

from gamas_bot.artifact_validation import (
    validate_docx_artifact,
    validate_pdf_artifact,
)


class FinalArtifactValidationTests(unittest.TestCase):
    def test_pdf_with_an_envelope_but_no_parseable_document_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_pdf_artifact(b"%PDF-1.7\nnot a PDF document\n%%EOF")

    def test_non_zip_docx_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_docx_artifact(b"PK\x03\x04truncated")
