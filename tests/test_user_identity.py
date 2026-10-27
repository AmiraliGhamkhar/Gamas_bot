"""User identity (R2): int64 ids stored exactly, @username captured, no duplicates.

Telegram ids are int64 and routinely exceed 2**53, the range where a float
round-trip starts losing precision. The upsert therefore binds an ``int`` to an
``INTEGER`` primary key and these tests prove end-to-end that nothing in the
read paths turns it into a float, a string or a truncated value.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from tests.support import FakeCallbackEvent, FakeTelegramClient, make_settings
from tests.test_bot import FakeMessageEvent

HUGE_ID = 770000000123456789  # > 2**53: a float round-trip would corrupt it
#: ``make_settings`` configures exactly one admin id.
ADMIN_ID = 1


class UserIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(db_path=root / "identity.db", temp_dir=root / "tmp")
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        self.bot.client = self.client

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    async def test_int64_id_round_trips_through_every_read_path(self):
        await self.db.ensure_user(HUGE_ID, "علی", username="ali_test", last_name="رضایی")
        self.assertEqual((await self.db.ensure_user(HUGE_ID)).telegram_id, HUGE_ID)
        identity = await self.db.user_identity(HUGE_ID)
        assert identity is not None
        self.assertEqual(identity["telegram_id"], HUGE_ID)
        self.assertIsInstance(identity["telegram_id"], int)
        self.assertEqual(identity["username"], "ali_test")
        self.assertEqual(identity["first_name"], "علی")
        self.assertEqual(identity["last_name"], "رضایی")
        listed = await self.db.list_users()
        self.assertEqual(listed[0]["telegram_id"], HUGE_ID)
        self.assertEqual((await self.db.search_users(str(HUGE_ID)))[0]["telegram_id"], HUGE_ID)
        self.assertEqual(len(await self.db.user_ids()), 1)
        await self.db.add_credit(HUGE_ID, 120, note="test", validity_days=30)
        self.assertEqual(await self.db.available_seconds(HUGE_ID), 120)
        # The stored type is a real SQLite INTEGER, not TEXT or REAL.
        rows = await self.db.db.execute_fetchall(
            "SELECT typeof(telegram_id) FROM users WHERE telegram_id = ?", (HUGE_ID,)
        )
        self.assertEqual(rows[0][0], "integer")

    async def test_double_start_does_not_duplicate_the_row(self):
        event = FakeMessageEvent(HUGE_ID, "/start")
        await self.bot._handle_message(event)
        await self.bot._handle_message(FakeMessageEvent(HUGE_ID, "/start"))
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM users WHERE telegram_id = ?", (HUGE_ID,)
        )
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(await self.db.count_users(), 1)

    async def test_username_change_propagates_and_latest_wins(self):
        await self.db.ensure_user(HUGE_ID, "علی", username="old_name")
        await self.db.ensure_user(HUGE_ID, "علی", username="new_name")
        identity = await self.db.user_identity(HUGE_ID)
        assert identity is not None
        self.assertEqual(identity["username"], "new_name")

    async def test_identity_is_captured_from_the_telegram_sender(self):
        from types import SimpleNamespace

        sender = SimpleNamespace(first_name="سارا", last_name="محمدی", username="sara_m")
        event = FakeMessageEvent(HUGE_ID, "/start")
        event.sender = sender
        await self.bot._handle_message(event)
        identity = await self.db.user_identity(HUGE_ID)
        assert identity is not None
        self.assertEqual(identity["username"], "sara_m")
        self.assertEqual(identity["first_name"], "سارا")
        self.assertEqual(identity["last_name"], "محمدی")
        self.assertTrue(identity["last_seen_at"])

    async def test_a_missing_username_is_stored_as_null_and_rendered_with_a_dash(self):
        await self.db.ensure_user(HUGE_ID)
        identity = await self.db.user_identity(HUGE_ID)
        assert identity is not None
        self.assertEqual(identity["username"], "")
        line = self.bot._identity_line(
            username=identity["username"],
            telegram_id=identity["telegram_id"],
            display_name="",
        )
        self.assertIn("— · شناسه:", line)
        self.assertIn(str(HUGE_ID), line)

    async def test_search_finds_by_id_and_by_username(self):
        await self.db.ensure_user(HUGE_ID, "علی", username="Search_Me")
        by_id = await self.db.search_users(str(HUGE_ID))
        by_name = await self.db.search_users("@search_me")
        self.assertEqual(by_id[0]["telegram_id"], HUGE_ID)
        self.assertEqual(by_name[0]["telegram_id"], HUGE_ID)
        self.assertEqual(await self.db.search_users("nobody_here"), [])
        self.assertEqual(await self.db.search_users(""), [])

    async def test_reference_integrity_is_clean_after_real_activity(self):
        await self.db.ensure_user(HUGE_ID, "علی", username="ali")
        item = await self.db.library_add(HUGE_ID, "booklet", 1, 2, None, title="جزوه")
        await self.db.library_set_content(item, "متن")
        await self.db.add_credit(HUGE_ID, 600, note="t", validity_days=30)
        deck = await self.db.deck_create_with_cards(
            HUGE_ID, item, "دسته", [{"front": "پ", "back": "پاسخ"}]
        )
        self.assertGreater(deck, 0)
        await self.db.quiz_create_session(
            HUGE_ID,
            item,
            [
                {
                    "question": "سؤال؟",
                    "options": ["a", "b", "c", "d"],
                    "correct_index": 0,
                    "explanation": "توضیح",
                }
            ],
        )
        await self.db.consume_free_use(HUGE_ID, "deck")
        await self.db.document_add(
            HUGE_ID,
            file_msg_ref="1:2",
            title="سند",
            source_type="pdf",
            page_count=1,
            sha256="abc",
            text="متن",
            chunks_json="[]",
            library_item_id=item,
        )
        self.assertEqual(await self.db.user_reference_integrity(), [])
        await self.db.assert_user_reference_integrity()

    async def test_integrity_check_detects_an_orphan(self):
        # Foreign keys are ON, so create the orphan with them off to prove the
        # check itself is not merely echoing SQLite's enforcement.
        await self.db.db.execute("PRAGMA foreign_keys=OFF")
        await self.db.db.execute(
            "INSERT INTO library_items(user_id, kind, chat_id, message_id, title, created_at) "
            "VALUES (?, 'booklet', 1, 2, 'orphan', '2026-01-01')",
            (HUGE_ID,),
        )
        await self.db.db.commit()
        await self.db.db.execute("PRAGMA foreign_keys=ON")
        problems = await self.db.user_reference_integrity()
        self.assertTrue(any("library_items.user_id" in problem for problem in problems))
        with self.assertRaisesRegex(RuntimeError, "reference integrity"):
            await self.db.assert_user_reference_integrity()

    async def test_admin_user_card_shows_username_id_and_name(self):
        await self.db.ensure_user(ADMIN_ID, "مدیر", username="boss")
        await self.db.ensure_user(HUGE_ID, "علی", username="ali_user", last_name="رضایی")
        event = FakeCallbackEvent(
            self.client, data=f"adm:hub:usr:card:{HUGE_ID}".encode(), chat_id=ADMIN_ID
        )
        event.client = self.client
        await self.bot._handle_callback(event)
        text = event.edits[-1]["text"]
        self.assertIn("@ali_user", text)
        self.assertIn(f"شناسه: {HUGE_ID}", text)
        self.assertIn("علی رضایی", text)
        self.assertIn("هدیهٔ اولین بار", text)

    async def test_admin_search_by_username_opens_the_card(self):
        await self.db.ensure_user(ADMIN_ID, "مدیر", username="boss")
        await self.db.ensure_user(HUGE_ID, "علی", username="ali_user")
        hub = FakeCallbackEvent(self.client, data=b"adm:hub:usr", chat_id=ADMIN_ID)
        hub.client = self.client
        await self.bot._handle_callback(hub)
        self.assertIn(ADMIN_ID, self.bot._pending_user_search)
        search = FakeMessageEvent(ADMIN_ID, "@ali_user")
        await self.bot._handle_message(search)
        self.assertNotIn(ADMIN_ID, self.bot._pending_user_search)
        sent = "\n".join(search.replies) + "\n".join(
            item["text"] for item in search.client.texts
        )
        self.assertIn("@ali_user", sent)
        self.assertIn(f"شناسه: {HUGE_ID}", sent)


    async def test_signed_int64_boundaries_and_lossy_inputs(self):
        for value in (-(1 << 63), (1 << 63) - 1):
            await self.db.ensure_user(value, "نام", username="boundary")
            self.assertEqual((await self.db.search_users(str(value)))[0]["telegram_id"], value)
        for invalid in (float(HUGE_ID), str(HUGE_ID), True):
            with self.assertRaises(TypeError):
                await self.db.ensure_user(invalid)
        for invalid in (-(1 << 63) - 1, 1 << 63):
            with self.assertRaises(ValueError):
                await self.db.ensure_user(invalid)
            self.assertEqual(await self.db.search_users(str(invalid)), [])

    async def test_repeated_start_refreshes_identity_without_duplicate(self):
        from types import SimpleNamespace

        first = FakeMessageEvent(HUGE_ID, "/start")
        first.sender = SimpleNamespace(first_name="نام قدیمی", last_name="پیشین", username="old")
        await self.bot._handle_message(first)
        await self.db._write("UPDATE users SET last_seen_at='2020-01-01' WHERE telegram_id=?", (HUGE_ID,))
        second = FakeMessageEvent(HUGE_ID, "/start")
        second.sender = SimpleNamespace(first_name="نام تازه", last_name="جدید", username="new")
        await self.bot._handle_message(second)
        identity = await self.db.user_identity(HUGE_ID)
        self.assertEqual((identity["username"], identity["first_name"], identity["last_name"]),
                         ("new", "نام تازه", "جدید"))
        self.assertGreater(identity["last_seen_at"], "2020-01-01")
        self.assertEqual(await self.db.count_users(), 1)

    async def test_callback_refreshes_identity_and_clears_removed_handle(self):
        from types import SimpleNamespace

        await self.db.ensure_user(HUGE_ID, "نام", username="old", last_name="قدیمی")
        await self.db._write("UPDATE users SET last_seen_at='2020-01-01' WHERE telegram_id=?", (HUGE_ID,))
        event = FakeCallbackEvent(self.client, data=b"menu:main", chat_id=HUGE_ID)
        event.sender = SimpleNamespace(first_name="تازه", last_name=None, username=None)
        await self.bot._handle_callback(event)
        identity = await self.db.user_identity(HUGE_ID)
        self.assertEqual((identity["username"], identity["first_name"], identity["last_name"]), ("", "تازه", ""))
        self.assertGreater(identity["last_seen_at"], "2020-01-01")


if __name__ == "__main__":
    unittest.main()
