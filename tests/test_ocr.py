"""Tests for the local, API-free OCR path."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gamas_bot.bot import _ocr_metadata
from gamas_bot.ocr import OcrError, extract_text


class OcrMetadataTests(unittest.TestCase):
    def test_accepts_pdf_by_extension(self):
        message = SimpleNamespace(
            file=SimpleNamespace(name="scan.pdf", mime_type="application/octet-stream"),
            photo=None,
        )
        self.assertEqual(_ocr_metadata(message), ("scan.pdf", ".pdf"))

    def test_accepts_image_by_mime_when_extension_is_missing(self):
        message = SimpleNamespace(
            file=SimpleNamespace(name=None, mime_type="image/png"),
            photo=None,
        )
        self.assertEqual(_ocr_metadata(message), ("document.png", ".png"))

    def test_ignores_audio_and_unknown_attachments(self):
        message = SimpleNamespace(
            file=SimpleNamespace(name="lecture.mp3", mime_type="audio/mpeg"),
            photo=None,
        )
        self.assertIsNone(_ocr_metadata(message))

    def test_telegram_photo_has_safe_extension(self):
        message = SimpleNamespace(
            file=SimpleNamespace(name=None, mime_type="image/jpeg"),
            photo=object(),
        )
        self.assertEqual(_ocr_metadata(message), ("telegram-photo.jpg", ".jpg"))


class LocalOcrDependencyTests(unittest.TestCase):
    def test_missing_tesseract_has_actionable_error_and_no_api_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "page.png"
            source.write_bytes(b"fake image bytes")
            with mock.patch.dict(os.environ, {"TESSERACT_CMD": "", "PATH": ""}):
                with self.assertRaisesRegex(OcrError, "Tesseract"):
                    extract_text(source)

    def test_rejects_unsupported_extension_before_running_tesseract(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "notes.docx"
            source.write_bytes(b"not an image")
            with mock.patch("gamas_bot.ocr._tesseract_command") as command:
                with self.assertRaisesRegex(OcrError, "فرمت"):
                    extract_text(source)
                command.assert_not_called()

    def test_rejects_empty_or_oversized_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.png"
            empty.write_bytes(b"")
            with self.assertRaises(OcrError):
                extract_text(empty)
            large = Path(tmp) / "large.png"
            large.write_bytes(b"x" * 33)
            with self.assertRaises(OcrError):
                extract_text(large, max_bytes=32)


if __name__ == "__main__":
    unittest.main()
