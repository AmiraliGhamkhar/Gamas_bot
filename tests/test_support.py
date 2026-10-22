"""Persistent support tickets: DB transitions, ownership, bot flows, notifications."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from gamas_bot.bot import GamasBot
from gamas_bot.db import Database, MIGRATIONS_DIR
from gamas_bot.db import TicketStateError
from tests.support import FakeTelegramClient, make_settings
from tests.test_bot import FakeCallbackEvent, FakeMessageEvent
from tests.test_db_billing import DatabaseTestCase

ADMIN = 1
USER_A = 555
USER_B = 556


class SupportDatabaseTests(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.db.ensure_user(USER_A)
        await self.db.ensure_user(USER_B)

    async def ticket(self, user=USER_A, category="technical", subject="مشکل ورود"):
        return await self.db.create_ticket(user, category, subject)

    async def status(self, ticket_id):
        return (await self.db.get_ticket(ticket_id))["status"]

    async def test_ids_are_unique_and_human_readable(self):
        first = await self.ticket()
        second = await self.ticket()
        self.assertEqual(first["public_ticket_id"], "T-000001")
        self.assertEqual(second["public_ticket_id"], "T-000002")
        self.assertEqual(first["status"], "open")

    async def test_all_four_categories_are_accepted_and_others_refused(self):
        for category in ("payment", "technical", "subscription", "other"):
            self.assertEqual((await self.ticket(category=category))["category"], category)
        with self.assertRaises(ValueError):
            await self.db.create_ticket(USER_A, "bogus", "سلام")
        with self.assertRaises(ValueError):
            await self.db.create_ticket(USER_A, "other", "x")

    async def test_status_transitions_follow_the_central_table(self):
        t = await self.ticket()
        tid = t["id"]
        await self.db.add_ticket_message(tid, USER_A, "user", "سلام")
        self.assertEqual(await self.status(tid), "awaiting_admin")
        await self.db.add_ticket_message(tid, ADMIN, "admin", "پاسخ")
        self.assertEqual(await self.status(tid), "awaiting_user")
        await self.db.add_ticket_message(tid, USER_A, "user", "ممنون")
        self.assertEqual(await self.status(tid), "awaiting_admin")
        self.assertTrue(await self.db.close_ticket(tid, ADMIN, admin=True))
        self.assertEqual(await self.status(tid), "closed")
        self.assertFalse(await self.db.close_ticket(tid, ADMIN, admin=True))

    async def test_closed_ticket_refuses_messages_until_reopened(self):
        tid = (await self.ticket())["id"]
        await self.db.close_ticket(tid, USER_A, admin=False)
        with self.assertRaises(TicketStateError):
            await self.db.add_ticket_message(tid, USER_A, "user", "هنوز")
        with self.assertRaises(TicketStateError):
            await self.db.add_ticket_message(tid, ADMIN, "admin", "هنوز")
        self.assertTrue(await self.db.reopen_ticket(tid, ADMIN, admin=True))
        self.assertEqual(await self.status(tid), "open")
        await self.db.add_ticket_message(tid, USER_A, "user", "دوباره")

    async def test_reopen_preserves_previous_history(self):
        tid = (await self.ticket())["id"]
        await self.db.add_ticket_message(tid, USER_A, "user", "اول")
        await self.db.add_ticket_message(tid, ADMIN, "admin", "دوم")
        await self.db.close_ticket(tid, ADMIN, admin=True)
        await self.db.reopen_ticket(tid, ADMIN, admin=True)
        messages = await self.db.ticket_messages(tid, limit=10)
        self.assertEqual([m["message_text"] for m in messages], ["اول", "دوم"])

    async def test_other_users_cannot_read_write_close_or_reopen(self):
        tid = (await self.ticket(user=USER_A))["id"]
        with self.assertRaises(PermissionError):
            await self.db.add_ticket_message(tid, USER_B, "user", "نفوذ")
        with self.assertRaises(PermissionError):
            await self.db.close_ticket(tid, USER_B, admin=False)
        await self.db.close_ticket(tid, ADMIN, admin=True)
        with self.assertRaises(PermissionError):
            await self.db.reopen_ticket(tid, USER_B, admin=False)
        self.assertEqual(await self.db.list_user_tickets(USER_B), [])
        self.assertEqual(len(await self.db.list_user_tickets(USER_A)), 1)
        self.assertEqual(await self.db.count_ticket_messages(tid), 0)

    async def test_history_is_ordered_and_paged_newest_first(self):
        tid = (await self.ticket())["id"]
        for index in range(8):
            await self.db.add_ticket_message(tid, USER_A, "user", f"پیام {index}")
        newest = await self.db.ticket_messages(tid, limit=3, offset=0)
        self.assertEqual([m["message_text"] for m in newest], ["پیام 5", "پیام 6", "پیام 7"])
        older = await self.db.ticket_messages(tid, limit=3, offset=3)
        self.assertEqual([m["message_text"] for m in older], ["پیام 2", "پیام 3", "پیام 4"])
        self.assertEqual(await self.db.count_ticket_messages(tid), 8)

    async def test_new_ticket_is_an_independent_conversation(self):
        first = (await self.ticket(subject="اول"))["id"]
        await self.db.add_ticket_message(first, USER_A, "user", "متن اول")
        second = (await self.ticket(subject="دوم"))["id"]
        self.assertEqual(await self.db.count_ticket_messages(second), 0)
        self.assertEqual(await self.db.count_ticket_messages(first), 1)

    async def test_filters_and_pagination_views(self):
        a = (await self.ticket(category="payment"))["id"]
        b = (await self.ticket(category="technical"))["id"]
        c = (await self.ticket(category="payment"))["id"]
        await self.db.add_ticket_message(a, USER_A, "user", "x")   # awaiting_admin
        await self.db.add_ticket_message(b, ADMIN, "admin", "y")   # awaiting_user (admin only)
        await self.db.close_ticket(c, ADMIN, admin=True)
        ids = lambda rows: sorted(r["id"] for r in rows)  # noqa: E731
        self.assertEqual(ids(await self.db.list_tickets(view="active")), sorted([a, b]))
        self.assertEqual(ids(await self.db.list_tickets(view="wait")), [a])
        self.assertEqual(ids(await self.db.list_tickets(view="user")), [b])
        self.assertEqual(ids(await self.db.list_tickets(view="closed")), [c])
        self.assertEqual(ids(await self.db.list_tickets(view="all", category="payment")), sorted([a, c]))
        self.assertEqual(len(await self.db.list_tickets(view="all", limit=1, offset=1)), 1)
        with self.assertRaises(ValueError):
            await self.db.list_tickets(view="drop table")

    async def test_tickets_and_messages_survive_restart(self):
        tid = (await self.ticket())["id"]
        await self.db.add_ticket_message(tid, USER_A, "user", "باقی می‌ماند")
        await self.db.close()
        self.db = Database(self.path)
        await self.db.connect()
        ticket = await self.db.get_ticket(tid)
        self.assertEqual(ticket["status"], "awaiting_admin")
        self.assertEqual((await self.db.ticket_messages(tid))[0]["message_text"], "باقی می‌ماند")

    async def test_subject_is_normalised_and_bounded(self):
        t = await self.db.create_ticket(USER_A, "technical", "  خیلی\n  خوب  ")
        self.assertEqual(t["subject"], "خیلی خوب")
        with self.assertRaises(ValueError):
            await self.db.create_ticket(USER_A, "technical", "x" * 401)
        with self.assertRaises(ValueError):
            await self.db.add_ticket_message((await self.ticket())["id"], USER_A, "user", "   ")

    async def test_ticket_and_initial_message_are_atomic(self):
        await self.db._write(
            "CREATE TEMP TRIGGER fail_initial_message BEFORE INSERT ON support_messages "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.create_ticket(
                USER_A, "technical", "مشکل تازه", initial_message="شرح مشکل"
            )
        self.assertEqual(await self.db.list_user_tickets(USER_A), [])
        self.assertEqual(
            (await self.db.db.execute_fetchall("SELECT COUNT(*) FROM support_ticket_seq"))[0][0],
            0,
        )
        await self.db._write("DROP TRIGGER fail_initial_message")
        ticket = await self.db.create_ticket(
            USER_A,
            "technical",
            "مشکل تازه",
            initial_message="شرح مشکل",
            telegram_message_id=77,
        )
        self.assertEqual(ticket["status"], "awaiting_admin")
        messages = await self.db.ticket_messages(ticket["id"])
        self.assertEqual(messages[0]["message_text"], "شرح مشکل")
        self.assertEqual(messages[0]["telegram_message_id"], 77)


class SupportMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_pending_payments_migrate_and_tickets_table_is_created(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            files = sorted(MIGRATIONS_DIR.glob("*.sql"))
            before_005 = [f for f in files if f.name < "005"]
            for path_sql in before_005:
                conn.executescript(path_sql.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations VALUES (?, '2026-01-01')", (path_sql.name,))
            conn.execute("INSERT INTO users(telegram_id, joined_at) VALUES (100, '2026-01-01')")
            conn.execute(
                "INSERT INTO payments(user_id, plan_code, status, created_at, purchased_seconds, "
                "purchased_price, purchased_validity_days) VALUES (100, 'paid_5h_30d', 'pending', "
                "'2026-01-01', 18000, 50000, 30)"
            )
            conn.execute("INSERT INTO users(telegram_id, joined_at) VALUES (101, '2026-01-01')")
            conn.execute(
                "INSERT INTO payments(user_id, plan_code, status, receipt_path, created_at, "
                "purchased_seconds, purchased_price, purchased_validity_days) VALUES (101, "
                "'paid_5h_30d', 'pending', 'r.jpg', '2026-01-01', 18000, 50000, 30)"
            )
            conn.commit()
            conn.close()

            db = Database(path)
            await db.connect()
            try:
                rows = await db.db.execute_fetchall(
                    "SELECT payment_id, status FROM payments ORDER BY payment_id"
                )
                self.assertEqual([tuple(r) for r in rows],
                                 [(1, "awaiting_payment"), (2, "pending_review")])
                tables = await db.db.execute_fetchall(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='support_tickets'"
                )
                self.assertEqual(len(tables), 1)
                # A migrated database keeps working for the new ticket flow.
                ticket = await db.create_ticket(100, "payment", "رسید نامشخص")
                self.assertEqual(ticket["public_ticket_id"], "T-000001")
            finally:
                await db.close()


class SupportBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "bot.db", temp_dir=root / "tmp", note_api_key="",
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.bot.client = FakeTelegramClient()
        await self.db.ensure_user(USER_A)
        await self.db.ensure_user(USER_B)

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    async def say(self, sender, text):
        event = FakeMessageEvent(sender, text)
        await self.bot._handle_message(event)
        return event

    async def press(self, sender, data: str):
        event = FakeCallbackEvent(sender, data.encode())
        await self.bot._handle_callback(event)
        return event

    @staticmethod
    def payloads(buttons) -> set[bytes]:
        out = set()
        for row in buttons or []:
            for button in row:
                data = getattr(getattr(button, "type", None), "data", None) or getattr(button, "data", None)
                if data:
                    out.add(data)
        return out

    async def open_ticket(self, sender=USER_A, category="payment", subject="رسید پرداخت"):
        await self.press(sender, "sup:new")
        await self.press(sender, f"sup:cat:{category}")
        event = await self.say(sender, subject)
        return event

    async def latest_ticket(self, user=USER_A):
        return (await self.db.list_user_tickets(user, limit=1))[0]

    async def test_main_menu_offers_support(self):
        from tests.test_bot import menu_payloads
        self.assertIn(b"support", menu_payloads(self.bot._user_menu()))

    async def test_create_ticket_end_to_end_with_admin_notification(self):
        event = await self.open_ticket()
        ticket = await self.latest_ticket()
        self.assertEqual(ticket["category"], "payment")
        self.assertEqual(ticket["status"], "awaiting_admin")
        self.assertTrue(any(ticket["public_ticket_id"] in r for r in event.replies))
        notified = [item["text"] for item in self.bot.client.texts if item["entity"] == ADMIN]
        self.assertTrue(any("درخواست پشتیبانی جدید" in t and ticket["public_ticket_id"] in t for t in notified))

    async def test_subject_prompt_requires_text_and_cancel_clears_it(self):
        await self.press(USER_A, "sup:cat:technical")
        event = await self.say(USER_A, "")  # photo without text
        self.assertTrue(any("متن" in r for r in event.replies))
        await self.say(USER_A, "/cancel")
        await self.say(USER_A, "این متن نباید درخواست شود")
        self.assertEqual(await self.db.list_user_tickets(USER_A), [])

    async def test_user_lists_and_views_only_own_tickets(self):
        await self.open_ticket(USER_A)
        ticket = await self.latest_ticket(USER_A)
        listing = await self.press(USER_A, "sup:list")
        self.assertIn(f"sup:v:{ticket['id']}".encode(), self.payloads(listing.edits[-1][1]))
        view = await self.press(USER_B, f"sup:v:{ticket['id']}")
        self.assertTrue(view.answers and view.answers[-1][1])
        # Another user's ticket is never rendered; only their own (empty) list may appear.
        rendered = " ".join(text for text, _ in view.edits) + str(view.responses)
        self.assertNotIn(ticket["public_ticket_id"], rendered)
        self.assertNotIn("رسید پرداخت", rendered)

    async def test_user_reply_is_stored_and_admin_is_notified(self):
        await self.open_ticket()
        ticket = await self.latest_ticket()
        await self.press(USER_A, f"sup:r:{ticket['id']}")
        await self.say(USER_A, "توضیح بیشتر")
        messages = await self.db.ticket_messages(ticket["id"], limit=10)
        self.assertEqual([m["message_text"] for m in messages], ["رسید پرداخت", "توضیح بیشتر"])
        self.assertEqual((await self.db.get_ticket(ticket["id"]))["status"], "awaiting_admin")
        self.assertTrue(any("پیام جدیدی" in item["text"] for item in self.bot.client.texts
                            if item["entity"] == ADMIN))

    async def test_reply_prompt_for_someone_elses_ticket_is_refused(self):
        await self.open_ticket(USER_A)
        ticket = await self.latest_ticket(USER_A)
        event = await self.press(USER_B, f"sup:r:{ticket['id']}")
        self.assertTrue(event.answers[-1][1])
        self.assertNotIn(USER_B, self.bot._support_states)
        await self.say(USER_B, "نفوذ")
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 1)

    async def test_admin_reply_routes_to_selected_ticket_and_notifies_user(self):
        await self.open_ticket()
        ticket = await self.latest_ticket()
        await self.press(ADMIN, "adm:sup")
        await self.press(ADMIN, f"adm:sv:{ticket['id']}")
        await self.press(ADMIN, f"adm:sr:{ticket['id']}")
        await self.say(ADMIN, "پرداخت شما بررسی شد.")
        messages = await self.db.ticket_messages(ticket["id"], limit=10)
        self.assertEqual(messages[-1]["sender_role"], "admin")
        self.assertEqual((await self.db.get_ticket(ticket["id"]))["status"], "awaiting_user")
        user_texts = [item["text"] for item in self.bot.client.texts if item["entity"] == USER_A]
        self.assertTrue(any(f"پاسخ جدیدی برای درخواست {ticket['public_ticket_id']} ثبت شده است." in t
                            for t in user_texts))

    async def test_non_admin_cannot_open_admin_support_or_reply(self):
        await self.open_ticket()
        ticket = await self.latest_ticket()
        for data in ("adm:sup", f"adm:sv:{ticket['id']}", f"adm:sr:{ticket['id']}", f"adm:sc:{ticket['id']}"):
            event = await self.press(USER_A, data)
            self.assertTrue(event.answers and event.answers[-1][1], data)
            self.assertFalse(event.edits)
        self.assertEqual((await self.db.get_ticket(ticket["id"]))["status"], "awaiting_admin")

    async def test_stale_admin_reply_state_is_disarmed_by_navigation(self):
        await self.open_ticket()
        ticket = await self.latest_ticket()
        await self.press(ADMIN, f"adm:sr:{ticket['id']}")
        await self.press(ADMIN, "adm:home")
        await self.say(ADMIN, "این نباید ثبت شود")
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 1)

    async def test_admin_close_notifies_user_and_user_must_reopen_to_reply(self):
        await self.open_ticket()
        ticket = await self.latest_ticket()
        await self.press(ADMIN, f"adm:sc:{ticket['id']}")
        self.assertEqual((await self.db.get_ticket(ticket["id"]))["status"], "closed")
        self.assertTrue(any("بسته شد" in item["text"] for item in self.bot.client.texts
                            if item["entity"] == USER_A))
        await self.press(USER_A, f"sup:r:{ticket['id']}")
        self.assertNotIn(USER_A, self.bot._support_states)
        await self.press(USER_A, f"sup:o:{ticket['id']}")
        self.assertEqual((await self.db.get_ticket(ticket["id"]))["status"], "open")
        await self.press(USER_A, f"sup:r:{ticket['id']}")
        await self.say(USER_A, "بعد از بازگشایی")
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 2)

    async def test_user_close_and_reopen_keep_history(self):
        await self.open_ticket()
        ticket = await self.latest_ticket()
        await self.press(USER_A, f"sup:c:{ticket['id']}")
        self.assertEqual((await self.db.get_ticket(ticket["id"]))["status"], "closed")
        await self.press(USER_A, f"sup:o:{ticket['id']}")
        self.assertEqual((await self.db.get_ticket(ticket["id"]))["status"], "open")
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 1)

    async def test_failed_admin_notification_keeps_the_ticket(self):
        async def refuse(*_args, **_kwargs):
            raise RuntimeError("telegram unavailable (simulated)")

        self.bot.client.send_message = refuse
        event = await self.open_ticket()
        ticket = await self.latest_ticket()
        self.assertEqual(ticket["status"], "awaiting_admin")
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 1)
        self.assertTrue(any(ticket["public_ticket_id"] in r for r in event.replies))

    async def test_blocked_user_does_not_lose_admin_reply(self):
        await self.open_ticket()
        ticket = await self.latest_ticket()

        async def blocked(*_args, **_kwargs):
            raise RuntimeError("bot was blocked by the user (simulated)")

        self.bot.client.send_message = blocked
        await self.press(ADMIN, f"adm:sr:{ticket['id']}")
        await self.say(ADMIN, "پاسخ ثبت شود")
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 2)

    async def test_admin_list_filters_and_pagination_render(self):
        for index in range(9):
            await self.open_ticket(USER_A, subject=f"موضوع {index}")
        first = await self.press(ADMIN, "adm:sl:active:all:0")
        text, buttons = first.edits[-1]
        self.assertIn(b"adm:sl:active:all:1", self.payloads(buttons))
        second = await self.press(ADMIN, "adm:sl:active:all:1")
        self.assertIn(b"adm:sl:active:all:0", self.payloads(second.edits[-1][1]))
        category = await self.press(ADMIN, "adm:sl:active:payment:0")
        self.assertIn("پرداخت", category.edits[-1][0])
        bad = await self.press(ADMIN, "adm:sl:active:bogus:0")
        self.assertTrue(bad.answers[-1][1])

    async def test_unknown_or_forged_ticket_ids_are_refused(self):
        for data in ("sup:v:abc", "sup:v:999", "adm:sv:999", "adm:sc:999", "sup:x:1"):
            event = await self.press(ADMIN if data.startswith("adm") else USER_A, data)
            self.assertTrue(event.answers, data)
            self.assertTrue(event.answers[-1][1], data)

    async def test_invalid_or_empty_input_keeps_the_support_prompt_active(self):
        await self.press(USER_A, "sup:cat:technical")
        await self.say(USER_A, "")
        self.assertIn(USER_A, self.bot._support_states)
        await self.say(USER_A, "x")
        self.assertIn(USER_A, self.bot._support_states)
        await self.say(USER_A, "شرح کامل مشکل")
        self.assertNotIn(USER_A, self.bot._support_states)
        self.assertEqual(len(await self.db.list_user_tickets(USER_A)), 1)

    async def test_start_disarms_user_and_admin_reply_prompts(self):
        await self.open_ticket()
        ticket = await self.latest_ticket()
        await self.press(USER_A, f"sup:r:{ticket['id']}")
        await self.say(USER_A, "/start")
        self.assertNotIn(USER_A, self.bot._support_states)
        await self.say(USER_A, "متن unrelated")
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 1)

        await self.press(ADMIN, f"adm:sr:{ticket['id']}")
        await self.say(ADMIN, "/start")
        self.assertNotIn(ADMIN, self.bot._support_admin_states)
        await self.say(ADMIN, "پاسخ unrelated")
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 1)

    async def test_long_history_is_complete_and_split_for_telegram(self):
        await self.open_ticket()
        ticket = await self.latest_ticket()
        body = "ا" * self.db.SUPPORT_MESSAGE_MAX
        await self.press(USER_A, f"sup:r:{ticket['id']}")
        await self.say(USER_A, body)
        view = await self.press(USER_A, f"sup:v:{ticket['id']}")
        rendered_parts = [text for text, _buttons in view.edits]
        rendered_parts += [text for text, _buttons in view.responses]
        self.assertGreater(len(rendered_parts), 1)
        self.assertIn(body, "".join(rendered_parts))
        self.assertTrue(all(
            len(part.encode("utf-16-le")) // 2 <= 4096 for part in rendered_parts
        ))
        self.assertTrue(view.responses, "navigation belongs on the final chunk")
        self.assertIsNotNone(view.responses[-1][1])


if __name__ == "__main__":
    unittest.main()
