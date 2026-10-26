"""The standalone PDF renderer produces a real right-to-left PDF from text.

The tests cover the pure export contract shared with the other document helpers:

* the file is a real PDF package a reader can open;
* the user's words end up in the document **verbatim** — nothing is rewritten
  and the model is never involved in this path;
* the document carries the brand (cover, header, footer page number) and long
  documents carry a clickable topic list (no page numbers — never guessed);
* the build is **deterministic**: the same text always yields the same bytes.

Persian text in a PDF is stored in *visual* order (shaped presentation forms,
reversed), and ``pypdf`` extracts it back in *logical* order — with its own
noise: whitespace varies, ZWNJ can drop, and number runs may be reordered.
The helpers below normalise both sides the same way before comparing.
"""

from __future__ import annotations

import io
import re
import unittest
from datetime import datetime
from unittest import mock

import arabic_reshaper
from pypdf import PdfReader

import gamas_bot.pdf_export as pdf_export
from gamas_bot.docx_export import DocumentMeta
from gamas_bot.office import OfficeError, build_office_document
from gamas_bot.pdf_export import build_office_pdf

SAMPLE = (
    "# گزارش عملکرد\n"
    "## بخش اول\n"
    "این متن عیناً در سند می‌آید.\n"
    "- مورد اول\n"
    "- مورد دوم\n"
    "## بخش دوم\n"
    "نام\tسن\tشهر\n"
    "علی\t۲۰\tتهران\n"
    "سارا\t25\tشیراز\n"
)

MODE_LABEL = "سند آزاد — PDF"
META = DocumentMeta(reference="GMS-1", created_at=datetime(2026, 10, 17, 10))


def _build(document=None, meta=META) -> bytes:
    return build_office_pdf(
        document or build_office_document(SAMPLE), meta=meta, mode_label=MODE_LABEL
    )


def _extract(payload: bytes) -> str:
    reader = PdfReader(io.BytesIO(payload))
    return "\n".join(page.extract_text() for page in reader.pages)


def _norm(text: str) -> str:
    """Fold the extraction noise: whitespace, ZWNJ and (reordered) digits."""
    text = "".join(text.split()).replace("\u200c", "")
    return re.sub(r"[0-9۰-۹]+", "", text)


def _probe(text: str) -> str:
    """The reader's form of a logical line: shaped, in logical order."""
    return _norm(arabic_reshaper.reshape(text))


class PdfPackageTests(unittest.TestCase):
    def test_a_valid_pdf_package_with_a_cover_and_body(self):
        payload = _build()
        self.assertTrue(payload.startswith(b"%PDF"))
        reader = PdfReader(io.BytesIO(payload))
        # cover page + at least the first body page
        self.assertGreaterEqual(len(reader.pages), 2)

    def test_rebuilding_is_byte_identical(self):
        document = build_office_document(SAMPLE)
        self.assertEqual(_build(document), _build(document))


class VerbatimTests(unittest.TestCase):
    """The reader's text is the user's text, word for word."""

    def text(self) -> str:
        return _norm(_extract(_build()))

    def test_title_and_lines_appear_verbatim(self):
        for line in (
            "گزارش عملکرد",
            "بخش اول",
            "این متن عیناً در سند می‌آید.",
            "مورد اول",
            "مورد دوم",
            "بخش دوم",
        ):
            self.assertIn(_probe(line), self.text(), line)

    def test_table_cells_appear_verbatim(self):
        for cell in ("نام", "سن", "شهر", "علی", "سارا", "تهران", "شیراز"):
            self.assertIn(_probe(cell), self.text(), cell)

    def test_persian_and_ascii_digits_survive(self):
        """«۲۰» stays Persian text, «25» stays ASCII — as in the user's table."""
        digits = set(re.findall(r"[0-9۰-۹]", _extract(_build())))
        self.assertIn("۲", digits)
        self.assertIn("۰", digits)
        self.assertIn("2", digits)
        self.assertIn("5", digits)


class BrandTests(unittest.TestCase):
    def test_cover_header_footer_and_mode_label(self):
        text = _extract(_build())
        self.assertIn("GAMAS", text)
        self.assertIn("Gamas Bot", text)
        self.assertIn("PDF", text)  # the mode label on the cover
        self.assertIn(_probe("تاریخ"), _norm(text))
        self.assertIn(_probe("به نام خدا"), _norm(text))

    def test_cover_quotation_is_quoted_verbatim(self):
        from gamas_bot.docx_export import COVER_QUOTE

        self.assertIn(_probe(COVER_QUOTE), _norm(_extract(_build())))


class TopicListTests(unittest.TestCase):
    #: Six level-1 headings: long enough and structured enough for a topic list.
    LONG = "\n".join(
        f"# فصل {index}\n" + "متن آزمایشی این بخش که کمی طولانی‌تر است.\n" * 10
        for index in range(1, 7)
    )

    def test_long_document_gets_a_topic_list_with_clickable_links(self):
        payload = build_office_pdf(build_office_document(self.LONG), meta=META, mode_label=MODE_LABEL)
        reader = PdfReader(io.BytesIO(payload))
        # cover + topic list + body
        self.assertGreaterEqual(len(reader.pages), 3)
        text = _norm(_extract(payload))
        self.assertIn(_probe("فهرست مطالب"), text)
        for index in range(1, 7):
            self.assertIn(_probe(f"فصل {index}"), text)
        # One clickable link per level-1 heading.
        links = len(re.findall(rb"/Subtype\s*/Link", payload))
        self.assertEqual(links, 6)

    def test_short_document_has_no_topic_list(self):
        self.assertNotIn(_probe("فهرست مطالب"), _norm(_extract(_build())))


class FontErrorTests(unittest.TestCase):
    def test_a_missing_font_is_an_office_error_not_a_crash(self):
        with mock.patch.object(pdf_export, "_find_font", return_value=None), mock.patch.object(
            pdf_export, "_font_names", None
        ):
            with self.assertRaises(OfficeError):
                _build()


if __name__ == "__main__":
    unittest.main()
