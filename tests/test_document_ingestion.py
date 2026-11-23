"""Document ingestion end-to-end (R4): upload → extract → library → generation.

The LLM is stubbed at the ``gamas_bot.bot.complete_json`` seam, exactly like the
learning tests, so the whole flow runs offline while the files themselves are
real PDF/DOCX/PPTX payloads.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from gamas_bot import texts
from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from tests.support import (
    FakeCallbackEvent,
    FakeJobEvent,
    FakeTelegramClient,
    make_settings,
)
from tests.test_doc_extract import make_docx_bytes, make_pdf_bytes, make_pptx_bytes
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
            db_path=root / "docs.db", temp_dir=root / "tmp",
            # Document generation flow tests build a fake avalai route behind
            # the mocked complete_json (tests must never perform live I/O).
            avalai_api_key="avalai-test-key",
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

    def upload(self, name: str, payload: bytes, *, mime: str, size: int | None = None, user_id: int = OWNER):
        path = Path(self._tmp.name) / name
        path.write_bytes(payload)
        self._uploads = getattr(self, "_uploads", 0) + 1
        event = FakeJobEvent(source=path, chat_id=user_id, message_id=500 + self._uploads)
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

    async def test_scanned_pdf_gets_the_explicit_message_and_stores_nothing(self):
        from tests.test_doc_extract import make_blank_pdf_bytes

        event = self.upload("scan.pdf", make_blank_pdf_bytes(), mime="application/pdf")
        await self.bot._handle_message(event)
        self.assertEqual(await self.db.document_count(OWNER), 0)
        sent = "\n".join(item["text"] for item in self.client.texts)
        self.assertIn("قابل‌خواندن نیست", sent)

    async def test_document_card_hides_unavailable_learning(self):
        _, documents = await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        self.bot.settings = replace(
            self.bot.settings,
            note_api_key="",
            note_api_base_url="",
            note_providers=(),
        )
        card = self.press(f"doc:show:{documents[0]['id']}".encode())

        await self.bot._handle_callback(card)

        text, buttons = card.edits[-1]["text"], card.edits[-1]["buttons"]
        self.assertIn(texts.LEARNING_GENERATION_UNAVAILABLE, text)
        payloads = self.button_payloads([{"buttons": buttons}])
        self.assertFalse(any(data.startswith((b"doc:quiz:", b"doc:fc:")) for data in payloads))

    async def test_empty_document_state_does_not_promise_learning_when_unavailable(self):
        self.bot.settings = replace(
            self.bot.settings,
            note_api_key="",
            note_api_base_url="",
            note_providers=(),
        )
        screen = self.press(b"lib:docs:0")

        await self.bot._handle_callback(screen)

        text = screen.edits[-1]["text"]
        self.assertIn("هنوز فایلی از ربات دریافت نکرده‌اید", text)
        self.assertNotIn("کوییز تعاملی HTML", text)

    async def test_document_screen_metadata_excludes_full_text_and_chunks(self):
        _, documents = await self.ingest("first.pdf", make_pdf_bytes(), mime="application/pdf")
        document = documents[0]
        metadata = await self.db.document_meta(OWNER, document["id"])
        self.assertNotIn("text", metadata)
        self.assertNotIn("chunks_json", metadata)
        self.assertGreater(metadata["text_chars"], 0)
        with mock.patch.object(self.db, "document_get", side_effect=AssertionError("large read")):
            await self.bot._show_document_generate_screen(self.press(b"menu:main"), OWNER, document["id"], "deck")
            await self.bot._show_document_card(self.press(b"menu:main"), OWNER, document["id"])

    async def test_oversized_document_is_rejected_before_download(self):
        self.assertEqual(self.settings.doc_max_bytes, 1024**3)
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
        self.assertIn(texts.DOCUMENT_PHOTO_UNSUPPORTED, sent)

    # --------------------------------------------------------------- library
    async def test_uploaded_document_stays_out_of_delivered_library_but_card_works(self):
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        # User-uploaded files are NOT shown in the delivered-only Library.
        self.assertEqual(await self.db.library_list(OWNER), [])
        section = self.press(b"library")
        await self.bot._handle_callback(section)
        self.assertIn("هنوز فایلی از ربات دریافت نکرده‌اید", section.edits[-1]["text"])

        document = (await self.db.documents_list(OWNER))[0]
        card = self.press(f"doc:show:{document['id']}".encode())
        await self.bot._handle_callback(card)
        text = card.edits[-1]["text"]
        self.assertIn("پیش‌نمایش متن", text)
        self.assertIn("📥 دریافت فایل اصلی", [b.text for row in card.edits[-1]["buttons"] for b in row])

    # ------------------------------------------------------------ generation
    async def test_questions_from_a_document_are_created_directly_as_html(self):
        await self.ingest("lecture.docx", make_docx_bytes(), mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        document = (await self.db.documents_list(OWNER))[0]
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(5, source="بخش «تنفس سلولی»")),
        ):
            await self.bot._handle_callback(
                self.press(f"doc:gen:quiz:{document['id']}:5".encode())
            )
        rows = await self.db.db.execute_fetchall(
            "SELECT question, source FROM quiz_questions"
        )
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row[1] for row in rows))
        lib_items = await self.db.library_list_kind(OWNER, "quiz")
        self.assertEqual(len(lib_items), 1)
        blob = await self.db.library_file_blob(OWNER, lib_items[0]["item_id"])
        self.assertIsNotNone(blob)
        self.assertIn(b"gamas-learning-data", bytes(blob["content"]))

    async def test_overflow_split_persists_real_parent_locator(self):
        from gamas_bot.doc_extract import DocChunk
        from gamas_bot.llm import LLMContextOverflowError

        item = await self.db.library_add(OWNER, "booklet", 1, 2, None, title="سند")
        source = {"item_id": item, "doc_id": None, "title": "سند", "chunks": [
            DocChunk(0, "صفحه ۳", ("صفحه ۳",), "متن درس " * 500)
        ]}
        calls = 0
        async def provider(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise LLMContextOverflowError("overflow")
            return {"questions": [
                {"question": f"پرسش شماره {calls * 100 + i}", "options": ["الف", "ب", "ج", "د"], "correct": 0, "explain": "چرا", "difficulty": "medium", "source": "fabricated"}
                for i in range(5)
            ]}
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            await self.bot._generate_learning(self.press(b"menu:main"), user_id=OWNER,
                                               source=source, wanted=5, cost=0, note="test", title="سند")
        rows = await self.db.db.execute_fetchall("SELECT source FROM quiz_questions")
        self.assertGreaterEqual(len(rows), 5)
        self.assertTrue(all(row[0].startswith("صفحه ۳") for row in rows))
        self.assertTrue(all(row[0] != "بخش ۱" for row in rows))

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
            payload = quiz_payload(5, source="صفحهٔ ۲")
            return payload

        with mock.patch(
            "gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=flaky)
        ):
            await self.bot._generate_learning(
                self.press(b"menu:main"),
                user_id=OWNER,
                source=source,
                wanted=10,
                cost=0,
                note="quiz-pending:doc:1",
                title="جزوهٔ دو بخشی — آزمون",
            )
        sent = "\n".join(item["text"] for item in self.client.texts)
        self.assertIn("بخش به‌طور کامل پردازش نشد", sent)
        # The cards that did arrive are delivered directly as HTML and saved.
        qs = list(await self.db.db.execute_fetchall("SELECT question_id FROM quiz_questions"))
        self.assertGreaterEqual(len(qs), 3)
        # Coverage is exactly what the user is told: one of two parts.
        self.assertIn("۱ بخش", sent)


    async def test_legacy_ppt_best_effort_uses_existing_converter(self):
        source = Path("tests/fixtures/visual_minimal.ppt").read_bytes()
        upload, _ = await self.ingest("legacy.ppt", source, mime="application/vnd.ms-powerpoint")
        stored = await self.db.documents_list(OWNER)
        # The historical converter is exercised; text-only decks are cached,
        # while image-only decks are refused with the explicit no-text message.
        if stored:
            self.assertEqual(stored[0]["source_type"], "pptx")
            document = await self.db.document_get(OWNER, stored[0]["id"])
            self.assertTrue(document["text"])
            # The subsequent presentation job hashes the original upload, not
            # the converted PPTX, and therefore cannot create a duplicate.
            from types import SimpleNamespace

            job = SimpleNamespace(user_id=OWNER, filename="legacy.ppt", message=upload.message, submission_id=1)
            slide = SimpleNamespace(number=1, title="عنوان", body=("متن",), notes="")
            await self.bot._store_presentation_document(job, upload.source, SimpleNamespace(slides=[slide]))
            self.assertEqual(await self.db.document_count(OWNER), 1)
        else:
            self.assertTrue(
                any("متن قابل‌استخراجی" in entry["text"] for entry in self.client.texts)
            )

    async def test_identical_uploads_are_deduplicated_per_user_and_reference_is_refreshed(self):
        payload = make_pdf_bytes()
        first, documents = await self.ingest("first.pdf", payload, mime="application/pdf")
        document_id = documents[0]["id"]
        second = self.upload("second.pdf", payload, mime="application/pdf")
        with mock.patch("gamas_bot.bot.extract_document", side_effect=AssertionError("duplicate was parsed")):
            await self.bot._handle_message(second)
        stored = await self.db.document_get(OWNER, document_id)
        self.assertEqual(stored["file_msg_ref"], f"{OWNER}:{second.message.id}")
        self.assertNotEqual(first.message.id, second.message.id)
        self.assertEqual((await self.db.db.execute_fetchall("SELECT COUNT(*) FROM library_items"))[0][0], 1)
        other = self.upload("other.pdf", payload, mime="application/pdf", user_id=OWNER + 1)
        await self.bot._handle_message(other)
        self.assertEqual(await self.db.document_count(OWNER), 1)
        self.assertEqual(await self.db.document_count(OWNER + 1), 1)
        self.assertNotEqual((await self.db.documents_list(OWNER + 1))[0]["id"], document_id)

    async def test_concurrent_identical_uploads_produce_one_document_and_one_mirror(self):
        payload = make_pdf_bytes()
        first = self.upload("race-a.pdf", payload, mime="application/pdf")
        second = self.upload("race-b.pdf", payload, mime="application/pdf")
        await asyncio.gather(self.bot._handle_message(first), self.bot._handle_message(second))
        self.assertEqual(await self.db.document_count(OWNER), 1)
        self.assertEqual((await self.db.db.execute_fetchall("SELECT COUNT(*) FROM library_items"))[0][0], 1)
        await self.db.assert_user_reference_integrity()

    async def test_exact_limit_is_accepted_and_underreported_oversize_is_rejected(self):
        from dataclasses import replace

        payload = make_pdf_bytes()
        self.bot.settings = replace(self.settings, doc_max_bytes=len(payload))
        exact = self.upload("exact.pdf", payload, mime="application/pdf", size=len(payload))
        await self.bot._handle_message(exact)
        self.assertEqual(await self.db.document_count(OWNER), 1)
        # Size is absent in Telegram metadata; the streaming byte gate still applies.
        huge = self.upload("unreported.pdf", payload + b"x", mime="application/pdf", size=0)
        await self.bot._handle_message(huge)
        self.assertEqual(await self.db.document_count(OWNER), 1)
        self.assertTrue(any("سقف مجاز اسناد" in entry["text"] for entry in self.client.texts))

    async def test_mirrored_library_entry_uses_the_full_document_and_real_locators(self):
        await self.ingest("lecture.docx", make_docx_bytes(),
                          mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        document = (await self.db.documents_list(OWNER))[0]
        item_id = document["library_item_id"]
        # The mirrored row carries the same 2–4-word name the document card shows
        # (never the upload file name and never an icon/reference caption).
        from gamas_bot.titles import word_count

        mirrored = await self.db.library_item(OWNER, item_id)
        self.assertEqual(mirrored["delivered"], 0)
        self.assertGreaterEqual(word_count(mirrored["title"]), 2, mirrored["title"])
        self.assertLessEqual(word_count(mirrored["title"]), 4, mirrored["title"])
        self.assertNotIn("📄", mirrored["title"])
        select = self.press(f"quiz:new:{item_id}".encode())
        await self.bot._handle_callback(select)
        self.assertIn(f"doc:gen:quiz:{document['id']}:5".encode(), self.button_payloads(select.edits))
        # A stale, pre-upgrade quiz:go callback is redirected to the same cached document.
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(
                return_value=quiz_payload(5, source="بخش «تنفس سلولی»"))) as generator:
            await self.bot._handle_callback(self.press(f"quiz:go:{item_id}:5".encode()))
        user_prompt = generator.await_args.kwargs["user"]
        self.assertIn("تنفس سلولی", user_prompt)
        self.assertIn("چرخهٔ کربس", user_prompt)
        self.assertIn("بخش «تنفس سلولی»", generator.await_args.kwargs["user"])

    async def test_forwarding_the_original_document_shows_a_bar(self):
        """Re-sending an original file is work; the panel must show it."""
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        doc_id = (await self.db.documents_list(OWNER))[0]["id"]
        self.client.forward_messages = mock.AsyncMock(return_value=[object()])
        event = self.press(f"doc:f:{doc_id}".encode())
        await self.bot._handle_callback(event)
        frames = [edit["text"] for edit in event.edits]
        self.assertTrue(frames)
        self.assertIn("٪", frames[0])
        self.assertIn("۱۰۰٪", frames[-1])
        self.assertIn("دوباره در همین گفتگو ارسال شد", frames[-1])

    async def test_foreign_user_cannot_read_or_generate_another_users_document(self):
        await self.ingest("private.pdf", make_pdf_bytes(), mime="application/pdf")
        doc_id = (await self.db.documents_list(OWNER))[0]["id"]
        for data in (f"doc:show:{doc_id}", f"doc:f:{doc_id}", f"doc:gen:fc:{doc_id}:quick"):
            event = self.press(data.encode(), user_id=OWNER + 1)
            with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock()) as generator:
                await self.bot._handle_callback(event)
            generator.assert_not_awaited()
            self.assertTrue(any(alert for _, alert in event.answers))

    async def test_a_fabricated_source_is_anchored_and_never_stored(self):
        """The chunk a card came from is its source; an invented page is not.

        Refusing every card over an invented locator lost a whole paid
        generation. The card keeps the page it was actually built from, and the
        fabricated string is never written.
        """
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        doc_id = (await self.db.documents_list(OWNER))[0]["id"]
        bad = quiz_payload(3, source="صفحهٔ ساختگی")
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=bad)) as generator:
            await self.bot._handle_callback(self.press(f"doc:gen:quiz:{doc_id}:5".encode()))
        # A top-up was requested for the four cards still missing after the
        # first six arrived; the answer is not thrown away.
        self.assertEqual(generator.await_count, 2)
        cards = list(await self.db.db.execute_fetchall("SELECT source FROM quiz_questions"))
        self.assertTrue(cards)
        self.assertTrue(all(card["source"].startswith("صفحهٔ") for card in cards))
        self.assertTrue(all("ساختگی" not in card["source"] for card in cards))

    async def test_persian_difficulty_labels_are_mapped_and_stored(self):
        """A Persian «سخت»/«متوسط» answer is mapped to the required tag.

        The quiz schema requires a ``medium``/``hard`` difficulty on every
        generated question; a route that answers in Persian keeps its questions
        because the label maps onto a valid tag, while a genuinely missing or
        unrecognized tag is inadmissible (the question is dropped and the
        shortfall retried once rather than silently misfilled).
        """
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        doc_id = (await self.db.documents_list(OWNER))[0]["id"]
        payload = quiz_payload(5, source="صفحهٔ ۱")
        for index, question in enumerate(payload["questions"]):
            question["difficulty"] = "سخت" if index % 2 else "متوسط"
        expected = ["hard" if index % 2 else "medium" for index in range(5)]
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=payload)) as generator:
            await self.bot._handle_callback(self.press(f"doc:gen:quiz:{doc_id}:5".encode()))
        self.assertEqual(generator.await_count, 1)
        rows = await self.db.db.execute_fetchall("SELECT difficulty,source FROM quiz_questions")
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row[1] == "صفحهٔ ۱" for row in rows))
        self.assertEqual(sorted(row[0] for row in rows), sorted(expected))

    async def test_persian_difficulty_and_retyped_locator_still_produce_html(self):
        """A re-typed locator («صفحه 1» vs «صفحهٔ ۱») cites the same page."""
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        doc_id = (await self.db.documents_list(OWNER))[0]["id"]
        payload = quiz_payload(5, source="صفحه 1")
        for index, question in enumerate(payload["questions"]):
            question["difficulty"] = "سخت" if index % 2 else "متوسط"
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=payload)) as generator:
            await self.bot._handle_callback(self.press(f"doc:gen:quiz:{doc_id}:5".encode()))
        self.assertEqual(generator.await_count, 1)
        rows = await self.db.db.execute_fetchall("SELECT difficulty,source FROM quiz_questions")
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row[0] in {"medium", "hard"} for row in rows))
        # The stored locator is the document's own spelling, so two cards from
        # one page cannot disagree about its name.
        self.assertTrue(all(row[1] == "صفحهٔ ۱" for row in rows))

    async def test_parallel_chunk_calls_are_bounded_and_report_progress(self):
        """Concurrent chunk calls are bounded by the quiz scheduling window.

        Decks deliberately serialize chunk calls (one provider request at a
        time); quizzes use ``learning_chunk_concurrency``. The barrier matches
        the boundary's dedupe-feedback preamble on `user`, not a raw endswith,
        so a prompt that already carries prior stems still matches its chunk.
        """
        from gamas_bot.doc_extract import DocChunk

        chunks = [DocChunk(i, f"صفحهٔ {i}", (f"صفحهٔ {i}",), f"متن یکتا {i}") for i in range(8)]
        active = peak = 0
        barrier = asyncio.Event()

        def worker_source(user: str) -> str:
            # The boundary may append prior stems after the chunk text, so a
            # raw endswith would miss: match the chunk that appears at all.
            return next(chunk.sources[0] for chunk in chunks if chunk.text in user)

        async def provider(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active >= 3:
                barrier.set()
            try:
                await asyncio.wait_for(barrier.wait(), 2)
                await asyncio.sleep(0.01)
                source = worker_source(kwargs["user"])
                return {"questions": [
                    {"question": f"پرسش {source} {n}", "options": ["الف", "ب", "ج", "د"],
                     "correct": 0, "explain": "چرا", "difficulty": "medium", "source": source}
                    # At least the quiz batch minimum (3) survives validation.
                    for n in range(3)
                ]}
            finally:
                active -= 1

        # Explicitly model a five-slot quiz window; the production default of
        # one would never reach this fixture's three-request barrier.
        self.bot.settings = replace(self.bot.settings, learning_chunk_concurrency=5)
        placeholder = await self.press(b"menu:main").respond("در حال ساخت")
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            items, failures = await self.bot._generate_chunks(
                chunks=chunks, counts=[3] * len(chunks), placeholder=placeholder
            )
        self.assertGreaterEqual(peak, 3)
        self.assertLessEqual(peak, 5)
        self.assertEqual(len(items), 24)
        self.assertEqual(failures, 0)
        self.assertIn("۸", placeholder.record["text"])



    async def test_partial_output_is_retried_and_shortfall_is_visible(self):
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        doc_id = (await self.db.documents_list(OWNER))[0]["id"]
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(
                side_effect=[quiz_payload(3), quiz_payload(2)])) as generator:
            await self.bot._handle_callback(self.press(f"doc:gen:quiz:{doc_id}:5".encode()))
        self.assertEqual(generator.await_count, 2)
        cards = list(await self.db.db.execute_fetchall("SELECT source FROM quiz_questions"))
        self.assertEqual(len(cards), 3)  # retain the best valid partial
        summary = self.client.texts[-1]["text"]
        self.assertIn("۵ مورد درخواست‌شده", summary)
        self.assertIn("۳ مورد معتبر", summary)

    async def test_unallocated_chunks_are_reported_to_the_user(self):
        from gamas_bot.doc_extract import DocChunk

        item = await self.db.library_add(OWNER, "booklet", 1, 2, None, title="سند")
        chunks = [DocChunk(i, f"صفحهٔ {i}", (f"صفحهٔ {i}",), f"متن یکتا {i}") for i in range(12)]
        source = {"item_id": item, "doc_id": None, "title": "سند", "chunks": chunks}

        async def provider(*args, **kwargs):
            # Read the source line, not a substring of a prior stem or page 10.
            source_line = kwargs["user"].split("متن منبع:\n", 1)[1].split("\n", 1)[0]
            chunk = next(c for c in chunks if c.text == source_line)
            return {"questions": [{"question": f"پرسش {chunk.index} مورد {i}", "options": ["الف", "ب", "ج", "د"], "correct": 0, "explain": "چرا", "difficulty": "medium", "source": chunk.locator} for i in range(5)]}
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            await self.bot._generate_learning(self.press(b"menu:main"), user_id=OWNER,
                                               source=source, wanted=5, cost=0, note="test", title="سند")
        cards = list(await self.db.db.execute_fetchall("SELECT source FROM quiz_questions"))
        self.assertEqual(len(cards), 5)
        summary = self.client.texts[-1]["text"]
        self.assertIn("۱۰ بخش", summary)
        self.assertIn("بررسی نشد", summary)


if __name__ == "__main__":
    unittest.main()
