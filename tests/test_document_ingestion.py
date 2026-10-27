"""Document ingestion end-to-end (R4): upload → extract → library → generation.

The LLM is stubbed at the ``gamas_bot.bot.complete_json`` seam, exactly like the
learning tests, so the whole flow runs offline while the files themselves are
real PDF/DOCX/PPTX payloads.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from tests.support import (
    FakeCallbackEvent,
    FakeJobEvent,
    FakeTelegramClient,
    make_settings,
)
from tests.test_doc_extract import make_docx_bytes, make_pdf_bytes
from tests.test_learning import quiz_payload

OWNER = 707


def cards_payload(count: int) -> dict:
    return {
        "cards": [
            {"front": f"پرسش {index}", "back": f"پاسخ {index}", "source": "صفحهٔ ۱"}
            for index in range(count)
        ]
    }


class DocumentIngestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "docs.db", temp_dir=root / "tmp", flashcard_ai_deck_size=6
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(OWNER)
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        self.bot.client = self.client

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def press(self, data: bytes, user_id: int = OWNER) -> FakeCallbackEvent:
        event = FakeCallbackEvent(self.client, data=data, chat_id=user_id)
        event.client = self.client
        return event

    def upload(self, name: str, payload: bytes, *, mime: str, size: int | None = None):
        path = Path(self._tmp.name) / name
        path.write_bytes(payload)
        self._uploads = getattr(self, "_uploads", 0) + 1
        event = FakeJobEvent(source=path, chat_id=OWNER, message_id=500 + self._uploads)
        event.client = self.client
        # Replies must land on the same recorder the assertions read.
        event.message.client = self.client
        event.message.attach_document(
            name, mime_type=mime, size=len(payload) if size is None else size
        )
        return event

    @staticmethod
    def button_payloads(records) -> set[bytes]:
        """Every inline-button payload of the recorded messages."""
        payloads: set[bytes] = set()
        for record in records:
            for row in record.get("buttons") or ():
                for button in row:
                    data = getattr(getattr(button, "type", None), "data", None)
                    if data is not None:
                        payloads.add(data)
        return payloads

    async def ingest(self, name: str, payload: bytes, *, mime: str):
        event = self.upload(name, payload, mime=mime)
        await self.bot._handle_message(event)
        documents = await self.db.documents_list(OWNER)
        return event, documents

    # ------------------------------------------------------------------ intake
    async def test_pdf_is_extracted_previewed_and_offered_for_generation(self):
        event, documents = await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document["source_type"], "pdf")
        self.assertIn("Cellular respiration", (await self.db.document_get(OWNER, document["id"]))["text"])
        sent = "\n".join(item["text"] for item in self.client.texts)
        self.assertIn("پیش‌نمایش متن", sent)
        self.assertIn("ATP", sent)  # the extracted text really is in the preview
        payloads = self.button_payloads(self.client.texts)
        self.assertIn(f"doc:fc:{document['id']}".encode(), payloads)
        self.assertIn(f"doc:quiz:{document['id']}".encode(), payloads)
        # The document is mirrored as a library item so decks/quizzes can cite it.
        self.assertTrue(document["library_item_id"])

    async def test_docx_is_extracted(self):
        _, documents = await self.ingest(
            "lecture.docx",
            make_docx_bytes(),
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(len(documents), 1)
        stored = await self.db.document_get(OWNER, documents[0]["id"])
        self.assertIn("گلیکولیز", stored["text"])
        self.assertEqual(stored["source_type"], "docx")

    async def test_scanned_pdf_gets_the_ocr_message_and_stores_nothing(self):
        from tests.test_doc_extract import make_blank_pdf_bytes

        event = self.upload("scan.pdf", make_blank_pdf_bytes(), mime="application/pdf")
        await self.bot._handle_message(event)
        self.assertEqual(await self.db.document_count(OWNER), 0)
        sent = "\n".join(item["text"] for item in self.client.texts)
        self.assertIn("OCR پشتیبانی نمی‌شود", sent)

    async def test_oversized_document_is_rejected_before_download(self):
        event = self.upload(
            "huge.pdf",
            make_pdf_bytes(),
            mime="application/pdf",
            size=self.settings.doc_max_bytes + 1,
        )
        await self.bot._handle_message(event)
        self.assertEqual(await self.db.document_count(OWNER), 0)
        sent = "\n".join(item["text"] for item in self.client.texts)
        self.assertIn("سقف مجاز اسناد", sent)

    async def test_identical_reupload_reuses_the_stored_extraction(self):
        payload = make_pdf_bytes()
        await self.ingest("notes.pdf", payload, mime="application/pdf")
        event = self.upload("notes-again.pdf", payload, mime="application/pdf")
        await self.bot._handle_message(event)
        self.assertEqual(await self.db.document_count(OWNER), 1)
        sent = "\n".join(item["text"] for item in self.client.texts)
        self.assertIn("قبلاً استخراج شده بود", sent)

    async def test_a_photo_instead_of_a_document_gets_a_hint(self):
        event = FakeJobEvent(chat_id=OWNER, message_id=991)
        event.client = self.client
        event.message.client = self.client
        event.message.photo = object()
        event.message.media = event.message.photo
        event.message.file.mime_type = "image/jpeg"
        await self.bot._handle_message(event)
        sent = "\n".join(item["text"] for item in self.client.texts)
        self.assertIn("ارسال به‌صورت فایل", sent)

    # --------------------------------------------------------------- library
    async def test_library_documents_section_lists_and_opens_the_document(self):
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        section = self.press(b"lib:docs:0")
        await self.bot._handle_callback(section)
        self.assertIn("📄 اسناد", section.edits[-1]["text"])

        document = (await self.db.documents_list(OWNER))[0]
        card = self.press(f"doc:show:{document['id']}".encode())
        await self.bot._handle_callback(card)
        text = card.edits[-1]["text"]
        self.assertIn("پیش‌نمایش متن", text)
        self.assertIn("📥 دریافت فایل", [b.text for row in card.edits[-1]["buttons"] for b in row])

    # ------------------------------------------------------------ generation
    async def test_flashcards_from_a_document_carry_source_locators(self):
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        document = (await self.db.documents_list(OWNER))[0]
        screen = self.press(f"doc:fc:{document['id']}".encode())
        await self.bot._handle_callback(screen)
        payloads = {
            button.type.data
            for row in screen.edits[-1]["buttons"]
            for button in row
        }
        self.assertIn(
            f"doc:gen:fc:{document['id']}:standard".encode(), payloads
        )

        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=cards_payload(6)),
        ) as generator:
            event = self.press(f"doc:gen:fc:{document['id']}:quick".encode())
            await self.bot._handle_callback(event)
            generator.assert_awaited_once()
        # The prompt names the chunk locator, so the model can anchor items.
        system = generator.await_args.kwargs["system"]
        self.assertIn("صفحهٔ", system)
        await self.bot._handle_callback(self.press(b"fc:cur:save"))
        decks = await self.db.decks_list(OWNER)
        self.assertEqual(len(decks), 1)
        cards = await self.db.deck_cards(decks[0]["deck_id"])
        self.assertTrue(all(card["source"] for card in cards))

    async def test_questions_from_a_document_are_created_after_curation(self):
        await self.ingest("lecture.docx", make_docx_bytes(), mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        document = (await self.db.documents_list(OWNER))[0]
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(5)),
        ):
            await self.bot._handle_callback(
                self.press(f"doc:gen:quiz:{document['id']}:5".encode())
            )
        self.assertEqual(
            len(await self.db.db.execute_fetchall("SELECT * FROM quiz_sessions")), 0
        )
        await self.bot._handle_callback(self.press(b"quiz:cur:save"))
        rows = await self.db.db.execute_fetchall(
            "SELECT question, source FROM quiz_questions"
        )
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row[1] for row in rows))

    async def test_generation_reports_partial_chunk_failures(self):
        """One uncovered chunk must not fail the whole generation."""
        from gamas_bot.doc_extract import DocChunk

        item_id = await self.db.library_add(OWNER, "booklet", 1, 2, None, title="جزوه")
        await self.db.library_set_content(item_id, "متن")
        source = {
            "item_id": item_id,
            "doc_id": None,
            "title": "جزوهٔ دو بخشی",
            "chunks": [
                DocChunk(0, "صفحهٔ ۱", ("صفحهٔ ۱",), "متن بخش نخست " * 20),
                DocChunk(1, "صفحهٔ ۲", ("صفحهٔ ۲",), "متن بخش دوم " * 20),
            ],
        }
        async def flaky(*_args, **kwargs):
            # The first chunk's provider is down; the second one is healthy.
            if "بخش نخست" in str(kwargs.get("user", "")):
                raise RuntimeError("provider down")
            return cards_payload(10)

        with mock.patch(
            "gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=flaky)
        ):
            await self.bot._generate_learning(
                self.press(b"menu:main"),
                user_id=OWNER,
                kind="deck",
                source=source,
                wanted=10,
                cost=0,
                note="fc-pending:doc:1",
                title="جزوهٔ دو بخشی — فلش‌کارت",
            )
        self.assertIn(OWNER, self.bot._curation)
        self.assertEqual(self.bot._curation[OWNER]["failures"], 1)
        sent = "\n".join(item["text"] for item in self.client.texts)
        self.assertIn("بخش پردازش نشد", sent)
        # The cards that did arrive are still offered for curation.
        self.assertGreaterEqual(len(self.bot._curation[OWNER]["items"]), 5)
        # Coverage is exactly what the user is told: one of two parts.
        self.assertIn("۱ بخش", sent)


if __name__ == "__main__":
    unittest.main()
