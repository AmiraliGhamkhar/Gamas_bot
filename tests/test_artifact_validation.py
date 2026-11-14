"""Final artifact bytes are validated before they can be advertised or stored."""

from __future__ import annotations

import unittest

from gamas_bot.artifact_validation import (
    MAX_STUDY_HTML_BYTES,
    validate_docx_artifact,
    validate_pdf_artifact,
    validate_study_html_artifact,
)


def _booklet(body: str = "<main><p>متن درس</p></main>") -> bytes:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="fa" dir="rtl">\n<head>\n'
        '<meta charset="utf-8">\n'
        "<title>جزوهٔ مطالعه</title>\n"
        "<style>body{font-family:sans-serif}</style>\n"
        f"</head>\n<body>\n{body}\n</body>\n</html>\n"
    ).encode("utf-8")


class FinalArtifactValidationTests(unittest.TestCase):
    def test_pdf_with_an_envelope_but_no_parseable_document_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_pdf_artifact(b"%PDF-1.7\nnot a PDF document\n%%EOF")

    def test_non_zip_docx_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_docx_artifact(b"PK\x03\x04truncated")


class StudyHtmlValidationTests(unittest.TestCase):
    """The minimal technical gate: envelope, encoding, size, offline safety."""

    def test_a_well_formed_offline_booklet_passes(self):
        validate_study_html_artifact(_booklet())

    def test_empty_and_oversized_files_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_study_html_artifact(b"")
        with self.assertRaises(ValueError):
            validate_study_html_artifact(b"x" * (MAX_STUDY_HTML_BYTES + 1))

    def test_non_utf8_bytes_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_study_html_artifact(b"\xff\xfe\x00bad")

    def test_a_missing_envelope_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_study_html_artifact(b"<html><body>no doctype</body></html>")
        with self.assertRaises(ValueError):
            validate_study_html_artifact(
                b"<!DOCTYPE html>\n<html lang=\"en\"><head><title>x</title></head></html>"
            )
        with self.assertRaises(ValueError):
            validate_study_html_artifact(
                b"<!DOCTYPE html>\n<html lang=\"fa\" dir=\"rtl\"><head></head></html>"
            )

    def test_network_references_are_rejected(self):
        for extra in (
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Vazirmatn">',
            '<script src="https://cdn.example.com/app.js"></script>',
            "<style>body{background:url(https://example.com/a.png)}</style>",
            "<style>@import url('theme.css');</style>",
        ):
            with self.assertRaises(ValueError, msg=extra):
                validate_study_html_artifact(_booklet(extra))

    def test_embedded_data_uris_are_allowed(self):
        validate_study_html_artifact(
            _booklet(
                '<img src="data:image/png;base64,iVBORw0KGgo=" alt="">'
                "<style>@font-face{src:url(data:font/ttf;base64,AAAA)}</style>"
            )
        )


if __name__ == "__main__":
    unittest.main()
