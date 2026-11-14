"""Telegram-backed personal library: capture, pagination, isolation and pruning."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime
from unittest import mock
from pathlib import Path

from gamas_bot.bot import GamasBot
from gamas_bot.db import MIGRATIONS_DIR, Database
from gamas_bot.docx_export import DocumentMeta
from gamas_bot.jobs import Job
from gamas_bot.notes import NoteSection, StructuredNotes
from tests.support import FakeJobEvent, FakeTelegramClient, make_settings
from tests.test_bot import FakeCallbackEvent, menu_payloads

OWNER = 555
OTHER = 556


class _Progress:
    async def update(self, *_args, **_kwargs) -> None:
        pass

    async def complete(self, *_args, **_kwargs) -> None:
        pass


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=self.root / "library.db",
            temp_dir=self.root / "tmp",
            library_max_items=3,
            avalai_api_key="avalai-test-key",  # configured learning-action UI; no model I/O
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(OWNER)
        await self.db.ensure_user(OTHER)
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        self.bot.client = self.client

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    async def _add_item(
        self, user_id: int, title: str, *, message_id: int, chat_id: int | None = None
    ) -> int:
        return await self.db.library_add(
            user_id,
            "transcript",
            chat_id or user_id,
            message_id,
            title=title,
        )

    async def test_persisted_booklet_and_results_are_available_for_the_owner(self):
        """Booklet bytes are durable DB artifacts; no completion file is pushed."""
        event = FakeJobEvent(chat_id=OWNER)
        # Record the delivered bytes so the booklet's own cover can be read.
        self.client.recorder = event
        submission_id = await self.db.create_submission(OWNER, "audio", "lecture.ogg")
        job = Job(
            submission_id=submission_id,
            user_id=OWNER,
            kind="audio",
            filename="lecture.ogg",
            message=event.message,
            is_admin=False,
        )
        # A model-written lesson title is a full sentence; the artifact is named
        # with the 2–4-word contract instead, and that one name is the cover.
        lesson_title = "جزوهٔ جامع فیزیولوژی قلب و عروق برای آزمون پایان ترم"
        notes = StructuredNotes(
            title=lesson_title,
            summary="خلاصهٔ درس",
            sections=(NoteSection(heading="فیزیولوژی", paragraphs=("محتوای آموزشی",)),),
        )
        meta = DocumentMeta(
            reference="GMS-1",
            source_name="lecture.ogg",
            created_at=datetime.now(),
        )
        await self.bot._deliver_booklet(
            job,
            self.root,
            notes,
            document=None,
            reference="GMS-1",
            meta=meta,
            progress=_Progress(),
        )

        items = await self.db.library_list(OWNER, limit=10)
        self.assertEqual([item["kind"] for item in items], ["booklet"])
        # The virtual booklet row intentionally has no Telegram message: its
        # primary PDF and editable DOCX are persisted artifacts instead.
        self.assertEqual(items[0]["chat_id"], 0)
        self.assertEqual(items[0]["message_id"], 0)
        self.assertEqual(items[0]["title"], "جامع فیزیولوژی قلب")
        self.assertIn(len(items[0]["title"].split()), (2, 3, 4))
        self.assertFalse(
            any(str(delivery["path"]).endswith((".pdf", ".docx")) for delivery in self.client.deliveries)
        )
        with mock.patch.object(self.bot, "_reply", new_callable=mock.AsyncMock) as reply:
            await self.bot._deliver_results(
                job, None, "GMS-1", has_booklet=True, has_keypoints=False, has_docx=True
            )
        buttons = reply.await_args.kwargs["buttons"]
        payloads = menu_payloads(buttons)
        self.assertTrue(any(b"booklet" in payload for payload in payloads))
        self.assertTrue(any(b"transcript" in payload for payload in payloads))
        pdf = await self.db.binary_artifact(submission_id, OWNER, "booklet")
        docx = await self.db.binary_artifact(submission_id, OWNER, "docx")
        assert pdf is not None and docx is not None
        self.assertTrue(bytes(pdf["content"]).startswith(b"%PDF-"))
        with zipfile.ZipFile(io.BytesIO(bytes(docx["content"]))) as archive:
            cover = archive.read("word/document.xml").decode("utf-8")
        # The renderer preserves the validated lecture title; only the library
        # label is shortened for a readable button/list row.
        self.assertIn(lesson_title, cover)

    async def test_empty_library_uses_the_persian_empty_state(self):
        event = FakeCallbackEvent(OWNER, b"library")
        await self.bot._handle_callback(event)
        text, buttons = event.edits[-1]
        self.assertEqual(
            text,
            "📚 کتابخانه من\n\n"
            "کتابخانهٔ شما خالی است.\n"
            "اولین فایل درس را بفرستید؛ متن و جزوه به‌صورت خودکار این‌جا ذخیره می‌شوند.",
        )
        self.assertIn(b"lib:docs:0", menu_payloads(buttons))

    async def test_library_screen_paginates_six_newest_items_and_counts_in_persian(self):
        for index in range(8):
            await self._add_item(OWNER, f"درس {index}", message_id=100 + index)
        await self._add_item(OTHER, "خصوصی", message_id=999)

        first = FakeCallbackEvent(OWNER, b"library")
        first.client = self.client
        await self.bot._handle_callback(first)
        first_text, first_buttons = first.edits[-1]
        first_payloads = menu_payloads(first_buttons)
        self.assertIn("📚 کتابخانه من", first_text)
        self.assertIn("تعداد موارد: ۸", first_text)
        self.assertIn(b"lib:1", first_payloads)
        self.assertEqual(sum(payload.startswith(b"lib:it:") for payload in first_payloads), 6)
        # The flashcard tab toggle is part of every populated library screen.
        self.assertIn(b"library:fc", first_payloads)
        first_labels = " ".join(button.text for row in first_buttons for button in row)
        self.assertIn("درس 7", first_text + " " + first_labels)
        self.assertNotIn("خصوصی", first_text + " " + first_labels)

        second = FakeCallbackEvent(OWNER, b"lib:1")
        second.client = self.client
        await self.bot._handle_callback(second)
        _second_text, second_buttons = second.edits[-1]
        second_payloads = menu_payloads(second_buttons)
        self.assertIn(b"lib:0", second_payloads)
        self.assertEqual(sum(payload.startswith(b"lib:it:") for payload in second_payloads), 2)
        labels = [button.text for row in second_buttons for button in row]
        # Each row is labelled by what it is, not by one icon for everything.
        self.assertEqual(
            sum(label.startswith("📝 ") for label in labels), 2, labels
        )

    async def test_forward_success_reuses_the_saved_source_message(self):
        item_id = await self._add_item(OWNER, "متن کلاس", message_id=4321, chat_id=777)
        event = FakeCallbackEvent(OWNER, f"lib:f:{item_id}".encode())
        event.client = self.client

        await self.bot._handle_callback(event)

        self.assertEqual(
            self.client.forwarded,
            [{"entity": OWNER, "messages": [4321], "from_peer": 777}],
        )
        self.assertFalse((await self.db.library_item(OWNER, item_id))["unavailable"])
        # The re-send is a Telegram round trip: a bar exists before it starts and
        # finishes on the completed frame with a way back.
        frames = [edit[0] for edit in event.edits]
        self.assertTrue(frames)
        self.assertIn("٪", frames[0])
        self.assertIn("۱۰۰٪", frames[-1])
        self.assertIn("دوباره در همین گفتگو ارسال شد", frames[-1])

    async def test_deleted_source_marks_item_unavailable_and_alerts(self):
        from telethon.errors import MessageIdInvalidError

        item_id = await self._add_item(OWNER, "فایل حذف‌شده", message_id=4322)
        event = FakeCallbackEvent(OWNER, f"lib:f:{item_id}".encode())
        event.client = self.client

        with mock.patch.object(
            self.client, "forward_messages", new=mock.AsyncMock(side_effect=MessageIdInvalidError(request=None))
        ):
            await self.bot._handle_callback(event)

        self.assertTrue((await self.db.library_item(OWNER, item_id))["unavailable"])
        self.assertEqual(event.answers[-1], ("این فایل دیگر در دسترس نیست.", True))

    async def test_temporary_forward_failure_is_retryable_even_with_an_old_flag(self):
        item_id = await self._add_item(OWNER, "فایل", message_id=4322)
        self.client.fail_forward = True
        event = FakeCallbackEvent(OWNER, f"lib:f:{item_id}".encode())
        await self.bot._handle_callback(event)
        self.assertFalse((await self.db.library_item(OWNER, item_id))["unavailable"])
        self.assertIn("موقتاً", event.answers[-1][0])

        # Old releases marked every exception permanently. Those records can
        # now be retried, and a successful forward repairs the stale flag.
        await self.db.library_mark_unavailable(item_id)
        self.client.fail_forward = False
        await self.bot._handle_callback(FakeCallbackEvent(OWNER, f"lib:f:{item_id}".encode()))
        self.assertEqual(len(self.client.forwarded), 1)
        self.assertFalse((await self.db.library_item(OWNER, item_id))["unavailable"])

    async def test_library_item_isolated_from_other_users(self):
        item_id = await self._add_item(OWNER, "یادداشت خصوصی", message_id=5001)
        event = FakeCallbackEvent(OTHER, f"lib:f:{item_id}".encode())
        event.client = self.client

        await self.bot._handle_callback(event)

        self.assertEqual(self.client.forwarded, [])
        self.assertEqual(event.answers[-1], ("این فایل دیگر در دسترس نیست.", True))
        self.assertFalse((await self.db.library_item(OWNER, item_id))["unavailable"])
        self.assertIsNone(await self.db.library_item(OTHER, item_id))

    async def test_migration_010_applies_to_a_populated_pre_library_database(self):
        path = self.root / "pre-library.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration.name >= "010_library.sql":
                    break
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    (migration.name, "2026-10-26T00:00:00+00:00"),
                )
            connection.execute(
                "INSERT INTO users(telegram_id, joined_at) VALUES (?, ?)",
                (9090, "2026-10-01T00:00:00+00:00"),
            )
            connection.commit()
        finally:
            connection.close()

        upgraded = Database(path)
        await upgraded.connect()
        try:
            self.assertEqual(await upgraded.count_users(), 1)
            rows = await upgraded.db.execute_fetchall(
                "SELECT name FROM schema_migrations WHERE name = '010_library.sql'"
            )
            self.assertEqual(len(rows), 1)
            table = await upgraded.db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'library_items'"
            )
            self.assertEqual(len(table), 1)
        finally:
            await upgraded.close()

    async def test_prune_and_unavailable_accessors_are_user_scoped(self):
        first = await self._add_item(OWNER, "قدیمی", message_id=10)
        await self._add_item(OTHER, "دیگری", message_id=11)
        await self._add_item(OWNER, "جدید", message_id=12)

        removed = await self.db.library_prune(OWNER, keep=1)

        self.assertEqual(removed, 1)
        self.assertIsNone(await self.db.library_item(OWNER, first))
        self.assertIsNotNone(await self.db.library_item(OTHER, first + 1))
        newest = (await self.db.library_list(OWNER, limit=1))[0]
        self.assertTrue(await self.db.library_mark_unavailable(newest["item_id"]))
        self.assertTrue((await self.db.library_item(OWNER, newest["item_id"]))["unavailable"])

    async def test_prune_keeps_newest_items_and_a_paid_quiz_source_until_it_ends(self):
        source = await self._add_item(OWNER, "منبع آزمون", message_id=50)
        questions = [{
            "question": "سؤال", "options": ["الف", "ب", "ج", "د"],
            "correct_index": 0, "explanation": "توضیح",
        }]
        session = await self.db.quiz_create_session(OWNER, source, questions)
        for index in range(4):
            await self._add_item(OWNER, f"جدید {index}", message_id=100 + index)
        await self.db.library_prune(OWNER, keep=3)
        self.assertEqual(await self.db.library_count(OWNER), 4)  # newest three plus active source
        self.assertIsNotNone(await self.db.quiz_get_session(session, OWNER))
        self.assertIsNotNone(await self.db.quiz_current_question(session))
        await self.db.quiz_finish(session, "quit")
        await self.db.library_prune(OWNER, keep=3)
        self.assertEqual(await self.db.library_count(OWNER), 3)
        self.assertIsNone(await self.db.quiz_get_session(session, OWNER))

    async def _booklet(self, *, with_content: bool) -> int:
        item_id = await self.db.library_add(
            OWNER, "booklet", OWNER, 4321, None, title="📖 جزوهٔ Word — GMS-77"
        )
        if with_content:
            await self.db.library_set_content(item_id, "متن کامل جزوه برای آزمون")
        return item_id

    async def test_detail_screen_offers_learning_actions_for_archived_booklet(self):
        item_id = await self._booklet(with_content=True)
        event = FakeCallbackEvent(OWNER, f"lib:it:{item_id}".encode())
        await self.bot._handle_callback(event)
        text, buttons = event.edits[-1]
        # A legacy row («📖 جزوهٔ Word — GMS-77») is shown by its name: the icon
        # is painted by the screen and the reference code is not part of a name.
        self.assertIn("جزوهٔ Word", text)
        self.assertNotIn("GMS-77", text)
        payloads = menu_payloads(buttons)
        self.assertIn(f"lib:f:{item_id}".encode(), payloads)
        self.assertIn(f"quiz:new:{item_id}".encode(), payloads)
        self.assertIn(f"fc:ai:{item_id}".encode(), payloads)
        self.assertIn(b"lib:0", payloads)

    async def test_detail_screen_legacy_booklet_shows_notice_without_learning_buttons(self):
        item_id = await self._booklet(with_content=False)
        event = FakeCallbackEvent(OWNER, f"lib:it:{item_id}".encode())
        await self.bot._handle_callback(event)
        text, buttons = event.edits[-1]
        self.assertIn("قدیمی ذخیره نشده است", text)
        payloads = menu_payloads(buttons)
        self.assertIn(f"lib:f:{item_id}".encode(), payloads)
        self.assertFalse(any(p.startswith(b"quiz:") or p.startswith(b"fc:") for p in payloads))

    async def test_detail_screen_for_transcript_only_offers_redownload(self):
        item_id = await self._add_item(OWNER, "متن خام", message_id=8800)
        event = FakeCallbackEvent(OWNER, f"lib:it:{item_id}".encode())
        await self.bot._handle_callback(event)
        payloads = menu_payloads(event.edits[-1][1])
        self.assertIn(f"lib:f:{item_id}".encode(), payloads)
        self.assertFalse(any(p.startswith(b"quiz:") for p in payloads))

    async def test_detail_screen_is_owner_scoped(self):
        item_id = await self._booklet(with_content=True)
        event = FakeCallbackEvent(OTHER, f"lib:it:{item_id}".encode())
        await self.bot._handle_callback(event)
        self.assertEqual(event.answers[-1][1], True)
        self.assertFalse(event.edits)


if __name__ == "__main__":
    unittest.main()
