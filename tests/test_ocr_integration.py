"""OCR wiring: the extraction hook, the intake flow and the stored document.

The service itself is replaced by a fake client here (its protocol is covered
by ``tests/test_ocr.py``); these tests are about what the bot does with the
recognised text — merge it page by page, store it, and explain the failures.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gamas_bot.bot import DOC_OCR_FAILED, DOC_PHOTO_HINT, GamasBot
from gamas_bot.db import Database
from gamas_bot.doc_extract import (
    IMAGE_EMPTY_MESSAGE,
    SCANNED_PDF_MESSAGE,
    SCANNED_PDF_OCR_EMPTY_MESSAGE,
    DocExtractError,
    ScannedPdfError,
    classify_image,
    extract_document,
)
from gamas_bot.ocr import OcrPage, OcrUnavailableError
from tests.support import FakeJobEvent, FakeTelegramClient, make_settings
from tests.test_doc_extract import make_blank_pdf_bytes, make_pdf_bytes

OWNER = 909


def make_mixed_pdf_bytes() -> bytes:
    """Page 1 carries a short text layer; pages 2 and 3 are image-only.

    The whole document stays below the "looks scanned" threshold, so OCR runs —
    but page 1 has enough of its own text to be kept as-is.
    """
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    chart = canvas.Canvas(buffer)
    chart.drawString(72, 760, "ATP synthase")
    chart.showPage()
    chart.showPage()
    chart.showPage()
    chart.save()
    return buffer.getvalue()


def make_png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (240, 120), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class FakeOcrClient:
    """Stands in for :class:`gamas_bot.ocr.I2OCRClient`."""

    def __init__(self, pages: dict[int, str] | None = None, error: BaseException | None = None):
        self.pages = pages or {}
        self.error = error
        self.calls: list[tuple[str, list[int], str]] = []

    def extract_pages(self, path, pages, *, language, budget_seconds=600.0):
        self.calls.append((Path(path).name, list(pages), language))
        if self.error is not None:
            raise self.error
        # Mirrors the real client: a page it did not read is simply absent.
        return tuple(
            OcrPage(number, self.pages[number]) for number in pages if number in self.pages
        )

    def close(self) -> None:
        return


class ExtractDocumentOcrTests(unittest.TestCase):
    """The hook contract inside :func:`gamas_bot.doc_extract.extract_document`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_scanned_pdf_is_ocrd_when_the_hook_is_present(self):
        path = self.root / "scan.pdf"
        path.write_bytes(make_blank_pdf_bytes())
        seen: list[list[int]] = []

        def ocr(file: Path, pages):
            seen.append(list(pages))
            return [f"متن صفحهٔ {page}" .replace(str(page), "۱۲۳"[page - 1]) for page in pages]

        document = extract_document(path, "pdf", ocr=ocr)
        self.assertEqual(seen, [[1]])
        self.assertEqual(document.page_count, 1)
        self.assertIn("متن صفحهٔ ۱", document.text)

    def test_only_text_poor_pages_are_sent_to_ocr(self):
        path = self.root / "mixed.pdf"
        path.write_bytes(make_mixed_pdf_bytes())
        client = FakeOcrClient({2: "صفحهٔ دوم اسکن‌شده", 3: "صفحهٔ سوم اسکن‌شده"})
        document = extract_document(
            path, "pdf", ocr=lambda file, pages: [
                page.text for page in client.extract_pages(file, pages, language="persian")
            ]
        )
        self.assertEqual(client.calls[0][1], [2, 3])
        self.assertIn("ATP synthase", document.text)            # text layer kept
        self.assertIn("صفحهٔ دوم اسکن‌شده", document.text)      # OCR filled in
        self.assertEqual(document.page_count, 3)

    def test_a_text_rich_document_is_never_sent_to_the_service(self):
        path = self.root / "notes.pdf"
        path.write_bytes(make_pdf_bytes())
        client = FakeOcrClient({1: "نباید فراخوانی شود"})
        extract_document(
            path, "pdf", ocr=lambda file, pages: [
                page.text for page in client.extract_pages(file, pages, language="persian")
            ]
        )
        self.assertEqual(client.calls, [])

    def test_page_ceiling_limits_the_service_calls(self):
        path = self.root / "long.pdf"
        path.write_bytes(make_mixed_pdf_bytes())
        client = FakeOcrClient({2: "دو", 3: "سه"})
        extract_document(
            path, "pdf", ocr=lambda file, pages: [
                page.text for page in client.extract_pages(file, pages, language="persian")
            ], ocr_max_pages=1,
        )
        self.assertEqual(client.calls[0][1], [2])

    def test_disabled_ocr_keeps_the_original_message(self):
        path = self.root / "scan.pdf"
        path.write_bytes(make_blank_pdf_bytes())
        with self.assertRaises(ScannedPdfError) as caught:
            extract_document(path, "pdf")
        self.assertEqual(str(caught.exception), SCANNED_PDF_MESSAGE)
        self.assertFalse(caught.exception.ocr_attempted)

    def test_ocr_that_reads_nothing_reports_a_different_message(self):
        path = self.root / "scan.pdf"
        path.write_bytes(make_blank_pdf_bytes())
        with self.assertRaises(ScannedPdfError) as caught:
            extract_document(path, "pdf", ocr=lambda file, pages: [""])
        self.assertEqual(str(caught.exception), SCANNED_PDF_OCR_EMPTY_MESSAGE)
        self.assertTrue(caught.exception.ocr_attempted)

    def test_an_image_becomes_a_one_page_document(self):
        path = self.root / "photo.png"
        path.write_bytes(make_png_bytes())
        document = extract_document(path, "image", ocr=lambda file, pages: ["متن عکس"])
        self.assertEqual(document.page_count, 1)
        self.assertEqual(document.text, "متن عکس")
        self.assertEqual(document.sections[0].locator, "صفحهٔ ۱")

    def test_an_unreadable_image_is_refused(self):
        path = self.root / "not-an-image.png"
        path.write_bytes(b"this is not a PNG")
        with self.assertRaises(DocExtractError):
            extract_document(path, "image", ocr=lambda file, pages: ["x"])

    def test_an_oversized_image_is_refused_before_ocr(self):
        path = self.root / "huge.png"
        path.write_bytes(make_png_bytes())
        called: list[int] = []
        with mock.patch("gamas_bot.doc_extract.MAX_IMAGE_PIXELS", 10):
            with self.assertRaises(DocExtractError):
                extract_document(
                    path, "image", ocr=lambda file, pages: called.append(1) or ["x"]
                )
        self.assertEqual(called, [])

    def test_an_image_without_ocr_is_refused(self):
        path = self.root / "photo.png"
        path.write_bytes(make_png_bytes())
        with self.assertRaises(DocExtractError):
            extract_document(path, "image")

    def test_empty_recognition_of_an_image_asks_for_a_better_photo(self):
        path = self.root / "photo.png"
        path.write_bytes(make_png_bytes())
        with self.assertRaises(DocExtractError) as caught:
            extract_document(path, "image", ocr=lambda file, pages: [""])
        self.assertEqual(str(caught.exception), IMAGE_EMPTY_MESSAGE)

    def test_classify_image_accepts_photos_and_rejects_other_documents(self):
        self.assertTrue(classify_image("page.jpg", "image/jpeg"))
        self.assertTrue(classify_image("scan.TIFF", None))
        self.assertFalse(classify_image("notes.pdf", "application/pdf"))
        self.assertFalse(classify_image("archive.zip", "application/zip"))
        self.assertFalse(classify_image(None, None))


class BotOcrFlowTests(unittest.IsolatedAsyncioTestCase):
    """The bot flow with OCR on: scanned PDF, photo, and both failure modes."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(db_path=root / "ocr.db", temp_dir=root / "tmp")
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(OWNER)
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        self.bot.client = self.client
        self.bot.settings = self.settings
        self._uploads = 0

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def upload(self, name: str, payload: bytes, *, mime: str):
        path = Path(self._tmp.name) / name
        path.write_bytes(payload)
        self._uploads += 1
        event = FakeJobEvent(source=path, chat_id=OWNER, message_id=700 + self._uploads)
        event.client = self.client
        event.message.client = self.client
        event.message.attach_document(name, mime_type=mime, size=len(payload))
        return event

    def sent_text(self) -> str:
        return "\n".join(item["text"] for item in self.client.texts)

    async def test_scanned_pdf_is_recognised_and_stored_when_ocr_is_on(self):
        self.bot.settings = make_settings(
            db_path=self.settings.db_path,
            temp_dir=self.settings.temp_dir,
            ocr_enabled=True,
            ocr_language="persian",
        )
        self.bot.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.bot._ocr_client = FakeOcrClient({1: "متن شناسایی‌شدهٔ اسکن"})
        event = self.upload("scan.pdf", make_blank_pdf_bytes(), mime="application/pdf")
        await self.bot._handle_message(event)
        documents = await self.db.documents_list(OWNER)
        self.assertEqual(len(documents), 1)
        stored = await self.db.document_get(OWNER, documents[0]["id"])
        self.assertEqual(stored["source_type"], "pdf")
        self.assertIn("متن شناسایی‌شدهٔ اسکن", stored["text"])
        self.assertIn("متن شناسایی‌شدهٔ اسکن", self.sent_text())

    async def test_ocr_stays_off_until_the_operator_enables_it(self):
        self.assertFalse(self.settings.ocr_enabled)
        event = self.upload("scan.pdf", make_blank_pdf_bytes(), mime="application/pdf")
        await self.bot._handle_message(event)
        self.assertEqual(await self.db.document_count(OWNER), 0)
        self.assertIn("OCR پشتیبانی نمی‌شود", self.sent_text())

    async def test_photo_becomes_an_image_document(self):
        self.bot.settings = make_settings(
            db_path=self.settings.db_path,
            temp_dir=self.settings.temp_dir,
            ocr_enabled=True,
        )
        self.bot.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.bot._ocr_client = FakeOcrClient({1: "صورت‌مسئلهٔ عکس‌گرفته‌شده"})
        event = self.upload("photo.png", make_png_bytes(), mime="image/jpeg")
        # A Telegram photo carries no file name; the intake must still work.
        event.message.photo = SimpleNamespace(id=1234)
        await self.bot._handle_message(event)
        documents = await self.db.documents_list(OWNER)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["source_type"], "image")
        stored = await self.db.document_get(OWNER, documents[0]["id"])
        self.assertIn("صورت‌مسئلهٔ عکس‌گرفته‌شده", stored["text"])
        self.assertIn("نوع: تصویر", self.sent_text())

    async def test_photo_is_still_refused_when_ocr_is_off(self):
        event = self.upload("photo.png", make_png_bytes(), mime="image/jpeg")
        event.message.photo = SimpleNamespace(id=1234)
        await self.bot._handle_message(event)
        self.assertEqual(await self.db.document_count(OWNER), 0)
        self.assertIn(DOC_PHOTO_HINT.splitlines()[0], self.sent_text())

    async def test_service_failure_gets_its_own_message_and_stores_nothing(self):
        self.bot.settings = make_settings(
            db_path=self.settings.db_path,
            temp_dir=self.settings.temp_dir,
            ocr_enabled=True,
        )
        self.bot.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.bot._ocr_client = FakeOcrClient(error=OcrUnavailableError("service down"))
        event = self.upload("scan.pdf", make_blank_pdf_bytes(), mime="application/pdf")
        await self.bot._handle_message(event)
        self.assertEqual(await self.db.document_count(OWNER), 0)
        self.assertIn(DOC_OCR_FAILED.splitlines()[0], self.sent_text())

    async def test_extractor_is_only_built_when_ocr_is_enabled(self):
        self.assertIsNone(self.bot._ocr_extractor())
        enabled = make_settings(
            db_path=self.settings.db_path,
            temp_dir=self.settings.temp_dir,
            ocr_enabled=True,
        )
        self.bot.settings = enabled
        self.bot._ocr_client = FakeOcrClient({1: "متن"})
        hook = self.bot._ocr_extractor()
        self.assertIsNotNone(hook)
        path = Path(self._tmp.name) / "page.png"
        path.write_bytes(make_png_bytes())
        self.assertEqual(hook(path, [1]), ["متن"])
        self.assertEqual(hook(path, [2]), [None])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
