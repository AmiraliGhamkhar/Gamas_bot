"""Admin hubs (R1): three entry points, deep views intact, old callbacks alive.

The merge is a *navigation* change only. These tests assert both halves of that
claim: the top level is three hubs, and the payment/users/system screens behind
them render exactly what they rendered before — including the aliases an admin
with a stale keyboard may still press after deploy.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from tests.support import FakeCallbackEvent, FakeTelegramClient, make_settings

ADMIN_ID = 1
USER_ID = 505


class AdminHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(db_path=root / "hubs.db", temp_dir=root / "tmp")
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(ADMIN_ID, "مدیر", username="boss")
        await self.db.ensure_user(USER_ID, "دانشجو", username="student", last_name="رضایی")
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        self.bot.client = self.client

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def press(self, data: bytes, user_id: int = ADMIN_ID) -> FakeCallbackEvent:
        event = FakeCallbackEvent(self.client, data=data, chat_id=user_id)
        event.client = self.client
        return event

    @staticmethod
    def payloads(event: FakeCallbackEvent) -> set[bytes]:
        from tests.test_bot import menu_payloads

        return menu_payloads(event.edits[-1]["buttons"])

    async def test_top_level_menu_is_three_hubs_plus_unrelated_sections(self):
        from tests.test_bot import menu_payloads

        payloads = menu_payloads(self.bot._admin_menu())
        self.assertLessEqual(
            {b"adm:hub:pay", b"adm:hub:usr", b"adm:hub:sys"}, payloads
        )
        # The nine merged buttons are gone from the top level.
        for retired in (
            b"adm:pay",
            b"adm:ph:0",
            b"adm:users",
            b"adm:ovu:7d",
            b"adm:ovp:7d",
            b"adm:ovpay:7d",
            b"adm:ovs:7d",
        ):
            self.assertNotIn(retired, payloads)
        # Sections belonging to no merge group stay.
        for kept in (b"adm:sup", b"adm:bcast", b"adm:audit", b"adm:board"):
            self.assertIn(kept, payloads)

    async def test_payments_hub_opens_the_pending_queue_with_history_and_stats(self):
        event = self.press(b"adm:hub:pay")
        await self.bot._handle_callback(event)
        self.assertIn("پرداخت‌های در انتظار", event.edits[-1]["text"])
        payloads = self.payloads(event)
        self.assertIn(b"adm:hub:pay:hist", payloads)
        self.assertIn(b"adm:hub:pay:stats", payloads)

    async def test_payments_hub_stats_and_history_render(self):
        stats = self.press(b"adm:hub:pay:stats")
        await self.bot._handle_callback(stats)
        self.assertIn("نرخ تأیید", stats.edits[-1]["text"])
        self.assertIn(b"adm:ovpay:7d", self.payloads(stats))  # window selector

        history = self.press(b"adm:hub:pay:hist")
        await self.bot._handle_callback(history)
        self.assertIn("تاریخچه پرداخت‌ها", history.edits[-1]["text"])

    async def test_history_filters_by_status(self):
        await self.db.sync_plans(
            [{"code": "paid_5h_30d", "hours": 5, "price": 50_000, "validity_days": 30}]
        )
        approved = await self.db.create_payment(USER_ID, "paid_5h_30d")
        await self.db.set_payment_receipt(approved, "a.jpg", USER_ID)
        await self.db.review_payment(approved, True, ADMIN_ID)
        # One open order per user, so the second one is created after the first
        # has been decided.
        rejected = await self.db.create_payment(USER_ID, "paid_5h_30d")
        await self.db.set_payment_receipt(rejected, "b.jpg", USER_ID)
        await self.db.review_payment(rejected, False, ADMIN_ID, reason="نامعتبر")
        self.assertNotEqual(approved, rejected)

        filtered = self.press(b"adm:hub:pay:hist:approved")
        await self.bot._handle_callback(filtered)
        text = filtered.edits[-1]["text"]
        self.assertIn("تأییدشده", text)
        from gamas_bot.progress import to_persian_digits

        self.assertIn(f"#{to_persian_digits(approved)}", text)
        self.assertNotIn(f"#{to_persian_digits(rejected)}", text)

        rejected_view = self.press(b"adm:hub:pay:hist:rejected")
        await self.bot._handle_callback(rejected_view)
        self.assertIn(
            f"#{to_persian_digits(rejected)}", rejected_view.edits[-1]["text"]
        )

        forged = self.press(b"adm:hub:pay:hist:whatever")
        await self.bot._handle_callback(forged)
        self.assertEqual(forged.edits, [])
        self.assertIn("نامعتبر", forged.answers[-1][0])

    async def test_old_payment_callbacks_still_land_on_working_screens(self):
        # A stale keyboard from before the deploy must not dead-end.
        for data in (b"adm:pay", b"adm:ph:0", b"adm:ovpay:7d"):
            event = self.press(data)
            await self.bot._handle_callback(event)
            self.assertTrue(event.edits, data)
            self.assertIn(b"adm:hub:pay", self.payloads(event) | {b"adm:hub:pay"}, data)

    async def test_forged_hub_page_is_refused(self):
        event = self.press(b"adm:hub:pay:hist:not-a-page")
        await self.bot._handle_callback(event)
        self.assertEqual(event.edits, [])
        self.assertIn("نامعتبر", event.answers[-1][0])

        period = self.press(b"adm:hub:pay:stats:90d")
        await self.bot._handle_callback(period)
        self.assertEqual(period.edits, [])
        self.assertIn("نامعتبر", period.answers[-1][0])

    async def test_unknown_hub_is_refused(self):
        event = self.press(b"adm:hub:nope")
        await self.bot._handle_callback(event)
        self.assertEqual(event.edits, [])
        self.assertTrue(event.answers[-1][1])

    async def test_users_hub_defaults_to_search_and_keeps_the_guides(self):
        event = self.press(b"adm:hub:usr")
        await self.bot._handle_callback(event)
        text = event.edits[-1]["text"]
        self.assertIn("جست‌وجوی کاربر", text)
        self.assertIn("دستورهای مدیریت", text)
        self.assertIn("وضعیت حساب", text)
        self.assertIn("@student", text)
        payloads = self.payloads(event)
        for expected in (
            b"adm:hub:usr:stats",
            b"adm:hub:usr:pending",
            b"adm:hub:usr:banned",
        ):
            self.assertIn(expected, payloads)

    async def test_users_hub_stats_is_the_old_users_report(self):
        event = self.press(b"adm:hub:usr:stats")
        await self.bot._handle_callback(event)
        self.assertIn("👥 کاربران", event.edits[-1]["text"])
        self.assertIn(b"adm:ovu:7d", self.payloads(event))

    async def test_blocked_users_list_and_toggle(self):
        await self.db.set_banned(USER_ID, True, admin_id=ADMIN_ID)
        banned = self.press(b"adm:hub:usr:banned")
        await self.bot._handle_callback(banned)
        self.assertIn("@student", banned.edits[-1]["text"])

        card = self.press(f"adm:hub:usr:card:{USER_ID}".encode())
        await self.bot._handle_callback(card)
        self.assertIn("مسدود", card.edits[-1]["text"])

        toggle = self.press(f"adm:hub:usr:tg:{USER_ID}".encode())
        await self.bot._handle_callback(toggle)
        self.assertFalse(await self.db.is_banned(USER_ID))

    async def test_pending_receipt_users_jump_into_the_payment_detail(self):
        await self.db.sync_plans(
            [{"code": "paid_5h_30d", "hours": 5, "price": 50_000, "validity_days": 30}]
        )
        await self.db._write(
            "INSERT INTO payments(user_id, plan_code, status, receipt_path, created_at, "
            "purchased_seconds, purchased_price, purchased_validity_days) "
            "VALUES (?, 'paid_5h_30d', 'pending_review', 'r.jpg', '2026-01-01', 18000, 50000, 30)",
            (USER_ID,),
        )
        event = self.press(b"adm:hub:usr:pending")
        await self.bot._handle_callback(event)
        text = event.edits[-1]["text"]
        self.assertIn("رسیدهای در انتظار", text)
        self.assertIn("@student", text)
        self.assertTrue(
            any(data.startswith(b"adm:pd:") for data in self.payloads(event))
        )

    async def test_system_hub_shows_live_status_and_links_processing_details(self):
        event = self.press(b"adm:hub:sys")
        await self.bot._handle_callback(event)
        text = event.edits[-1]["text"]
        self.assertIn("سلامت سیستم", text)
        self.assertIn("آخرین پردازش موفق", text)
        self.assertIn("حافظهٔ اوج", text)
        self.assertIn("WAL", text)
        payloads = self.payloads(event)
        self.assertTrue(any(data.startswith(b"adm:hub:sys:proc") for data in payloads))

        proc = self.press(b"adm:hub:sys:proc")
        await self.bot._handle_callback(proc)
        self.assertIn("پردازش", proc.edits[-1]["text"])

    async def test_old_system_callbacks_still_render(self):
        for data in (b"adm:ovs:7d", b"adm:ovp:7d", b"adm:ovu:7d", b"adm:ovpay:7d"):
            event = self.press(data)
            await self.bot._handle_callback(event)
            self.assertTrue(event.edits, data)

    async def test_approve_from_the_hub_credits_the_user(self):
        await self.db.sync_plans(
            [{"code": "paid_5h_30d", "hours": 5, "price": 50_000, "validity_days": 30}]
        )
        payment_id = await self.db.create_payment(USER_ID, "paid_5h_30d")
        await self.db.set_payment_receipt(payment_id, "receipt.jpg", USER_ID)
        detail = self.press(f"adm:pd:{payment_id}".encode())
        await self.bot._handle_callback(detail)
        self.assertIn(b"adm:hub:pay", self.payloads(detail) | {b"adm:hub:pay"})

        # The confirmation step is unchanged by the hub merge.
        confirm = self.press(f"payv:{payment_id}:a".encode())
        await self.bot._handle_callback(confirm)
        self.assertEqual(
            (await self.db.payment(payment_id))["status"], "pending_review"
        )
        approve = self.press(f"payv:{payment_id}:a:yes".encode())
        await self.bot._handle_callback(approve)
        payment = await self.db.payment(payment_id)
        assert payment is not None
        self.assertEqual(payment["status"], "approved")
        self.assertEqual(await self.db.available_seconds(USER_ID), 5 * 3600)

    async def test_non_admin_cannot_open_a_hub(self):
        event = self.press(b"adm:hub:pay", user_id=USER_ID)
        await self.bot._handle_callback(event)
        self.assertEqual(event.edits, [])
        self.assertIn("مدیران", event.answers[-1][0])


if __name__ == "__main__":
    unittest.main()
