"""Document ingestion end-to-end (R4): upload → extract → library → generation.

The LLM is stubbed at the ``gamas_bot.bot.complete_json`` seam, exactly like the
learning tests, so the whole flow runs offline while the files themselves are
real PDF/DOCX/PPTX payloads.
"""

from __future__ import annotations

import asyncio
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
        self.assertIn("📥 دریافت فایل اصلی", [b.text for row in card.edits[-1]["buttons"] for b in row])

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
            new=mock.AsyncMock(return_value=cards_payload(10)),
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
            new=mock.AsyncMock(return_value=quiz_payload(5, source="بخش «تنفس سلولی»")),
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
            payload = cards_payload(10)
            for card in payload["cards"]:
                card["source"] = "صفحهٔ ۲"
            return payload

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
        self.assertIn("بخش به‌طور کامل پردازش نشد", sent)
        # The cards that did arrive are still offered for curation.
        self.assertGreaterEqual(len(self.bot._curation[OWNER]["items"]), 5)
        # Coverage is exactly what the user is told: one of two parts.
        self.assertIn("۱ بخش", sent)


    async def test_pptx_upload_to_anchored_flashcards_and_presentation_queue(self):
        upload, documents = await self.ingest(
            "lesson.pptx", make_pptx_bytes(),
            mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document["source_type"], "pptx")
        self.assertEqual(self.bot.queue.pending, 0)  # extraction precedes the optional audio job
        self.assertIn(f"doc:lecture:{document['id']}".encode(), self.button_payloads(self.client.texts))
        payload = cards_payload(10)
        for card in payload["cards"]:
            card["source"] = "اسلاید ۱"
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=payload)), \
             mock.patch("gamas_bot.bot.extract_document", side_effect=AssertionError("cached extraction expected")):
            await self.bot._handle_callback(self.press(f"doc:gen:fc:{document['id']}:quick".encode()))
        await self.bot._handle_callback(self.press(b"fc:cur:save"))
        cards = await self.db.deck_cards((await self.db.decks_list(OWNER))[0]["deck_id"])
        self.assertEqual(len(cards), 10)
        self.assertTrue(all(card["source"] == "اسلاید ۱" for card in cards))

        # Exercise the real presentation admission and Job shape, not just the adapter call.
        await self.db.add_credit(OWNER, 20_000, note="presentation", validity_days=30)
        self.client.get_messages = mock.AsyncMock(return_value=upload.message)
        await self.bot._handle_callback(self.press(f"doc:lecture:{document['id']}".encode()))
        self.client.get_messages.assert_awaited_once_with(OWNER, ids=upload.message.id)
        self.assertEqual(self.bot.queue.pending, 1)
        job = self.bot.queue.pop_pending()
        self.assertEqual(job.kind, "presentation")
        self.assertEqual(job.meta["presentation_kind"], "native")
        self.assertIs(job.message, upload.message)
        await job.meta["progress"].complete()

    async def test_legacy_ppt_best_effort_uses_existing_converter(self):
        source = Path("tests/fixtures/visual_minimal.ppt").read_bytes()
        upload, _ = await self.ingest("legacy.ppt", source, mime="application/vnd.ms-powerpoint")
        stored = await self.db.documents_list(OWNER)
        # The historical converter is exercised; text-only decks are cached,
        # while image-only decks receive the explicit OCR limitation.
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
            self.assertTrue(any("OCR" in entry["text"] for entry in self.client.texts))

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
        self.assertGreaterEqual(word_count(mirrored["title"]), 2, mirrored["title"])
        self.assertLessEqual(word_count(mirrored["title"]), 4, mirrored["title"])
        self.assertNotIn("📄", mirrored["title"])
        library = self.press(f"lib:it:{item_id}".encode())
        await self.bot._handle_callback(library)
        self.assertIn(f"doc:quiz:{document['id']}".encode(), self.button_payloads(library.edits))
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
        self.assertIn("بخش «تنفس سلولی»", generator.await_args.kwargs["system"])

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
        bad = cards_payload(6)
        for card in bad["cards"]:
            card["source"] = "صفحهٔ ساختگی"
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=bad)) as generator:
            await self.bot._handle_callback(self.press(f"doc:gen:fc:{doc_id}:quick".encode()))
        # A top-up was requested for the four cards still missing after the
        # first six arrived; the answer is not thrown away.
        self.assertEqual(generator.await_count, 2)
        state = self.bot._curation[OWNER]
        self.assertTrue(state["items"])
        self.assertTrue(
            all(item["source"].startswith("صفحهٔ") for item in state["items"])
        )
        self.assertNotIn(
            "ساختگی", " ".join(item["source"] for item in state["items"])
        )
        await self.bot._handle_callback(self.press(b"fc:cur:save"))
        cards = await self.db.deck_cards((await self.db.decks_list(OWNER))[0]["deck_id"])
        self.assertTrue(all(card["source"] and "ساختگی" not in card["source"] for card in cards))

    async def test_missing_quiz_difficulty_never_rejects_a_valid_question(self):
        """S5: the difficulty tag is metadata, not an admission requirement.

        It used to be enforced at the generation boundary, so a route that
        answers «سخت»/«متوسط» in Persian (or omits the tag entirely) produced
        zero valid questions and the user paid for a failed generation.
        """
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        doc_id = (await self.db.documents_list(OWNER))[0]["id"]
        payload = quiz_payload(5, source="صفحهٔ ۱")
        for question in payload["questions"]:
            question.pop("difficulty")
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=payload)) as generator:
            await self.bot._handle_callback(self.press(f"doc:gen:quiz:{doc_id}:5".encode()))
        self.assertEqual(generator.await_count, 1)
        self.assertIn(OWNER, self.bot._curation)
        await self.bot._handle_callback(self.press(b"quiz:cur:save"))
        rows = await self.db.db.execute_fetchall("SELECT difficulty,source FROM quiz_questions")
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row[0] == "" and row[1] == "صفحهٔ ۱" for row in rows))

    async def test_persian_difficulty_and_retyped_locator_still_produce_a_preview(self):
        """A re-typed locator («صفحه 1» vs «صفحهٔ ۱») cites the same page.

        Byte-identical matching used to discard every item the model returned,
        which ended as «ساخت محتوا ناموفق بود» for a perfectly grounded answer.
        """
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        doc_id = (await self.db.documents_list(OWNER))[0]["id"]
        payload = quiz_payload(5, source="صفحه 1")
        for index, question in enumerate(payload["questions"]):
            question["difficulty"] = "سخت" if index % 2 else "متوسط"
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=payload)) as generator:
            await self.bot._handle_callback(self.press(f"doc:gen:quiz:{doc_id}:5".encode()))
        self.assertEqual(generator.await_count, 1)
        state = self.bot._curation[OWNER]
        self.assertEqual(len(state["items"]), 5)
        self.assertEqual(state["failures"], 0)
        self.assertTrue(all(item["difficulty"] in {"medium", "hard"} for item in state["items"]))
        # The stored locator is the document's own spelling, so two cards from
        # one page cannot disagree about its name.
        self.assertTrue(all(item["source"] == "صفحهٔ ۱" for item in state["items"]))

    async def test_parallel_chunk_calls_are_bounded_and_report_progress(self):
        from gamas_bot.doc_extract import DocChunk

        chunks = [DocChunk(i, f"صفحهٔ {i}", (f"صفحهٔ {i}",), f"متن یکتا {i}") for i in range(8)]
        active = peak = 0
        barrier = asyncio.Event()
        async def provider(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active >= 3:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 2)
            await asyncio.sleep(0.01)
            source = next(chunk.sources[0] for chunk in chunks if chunk.text in kwargs["user"])
            active -= 1
            return {"cards": [{"front": f"پرسش {source} {n}", "back": f"پاسخ {n}", "source": source}
                              for n in range(2)]}
        placeholder = await self.press(b"menu:main").respond("در حال ساخت")
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            items, failures = await self.bot._generate_chunks(
                kind="deck", chunks=chunks, counts=[2] * len(chunks), placeholder=placeholder
            )
        self.assertGreaterEqual(peak, 3)
        self.assertLessEqual(peak, 5)
        self.assertEqual(len(items), 16)
        self.assertEqual(failures, 0)
        self.assertIn("۸", placeholder.record["text"])


    async def test_partial_output_is_retried_and_shortfall_is_visible(self):
        await self.ingest("notes.pdf", make_pdf_bytes(), mime="application/pdf")
        doc_id = (await self.db.documents_list(OWNER))[0]["id"]
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(
                side_effect=[cards_payload(6), cards_payload(4)])) as generator:
            await self.bot._handle_callback(self.press(f"doc:gen:fc:{doc_id}:quick".encode()))
        self.assertEqual(generator.await_count, 2)
        state = self.bot._curation[OWNER]
        self.assertEqual(len(state["items"]), 6)  # retain the best valid partial
        self.assertEqual(state["failures"], 1)
        preview = self.client.texts[-1]["text"]
        self.assertIn("۱۰ مورد درخواست‌شده", preview)
        self.assertIn("۶ مورد معتبر", preview)
        await self.bot._handle_callback(self.press(b"fc:cur:cancel"))
        self.assertTrue((await self.db.free_use_state(OWNER))["deck"])

    async def test_unallocated_chunks_are_reported_to_the_user(self):
        from gamas_bot.doc_extract import DocChunk

        item = await self.db.library_add(OWNER, "booklet", 1, 2, None, title="سند")
        chunks = [DocChunk(i, f"صفحهٔ {i}", (f"صفحهٔ {i}",), f"متن یکتا {i}") for i in range(12)]
        source = {"item_id": item, "doc_id": None, "title": "سند", "chunks": chunks}
        async def provider(*args, **kwargs):
            chunk = next(chunk for chunk in chunks if chunk.text == kwargs["user"])
            return {"cards": [{"front": f"پرسش {chunk.index}", "back": "پاسخ", "source": chunk.locator}]}
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            await self.bot._generate_learning(self.press(b"menu:main"), user_id=OWNER, kind="deck",
                                               source=source, wanted=5, cost=0, note="test", title="سند")
        self.assertEqual(self.bot._curation[OWNER]["skipped"], 7)
        self.assertIn("۷ بخش", self.client.texts[-1]["text"])
        self.assertIn("بررسی نشد", self.client.texts[-1]["text"])


if __name__ == "__main__":
    unittest.main()
