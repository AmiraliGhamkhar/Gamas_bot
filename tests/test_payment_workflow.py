"""Telegram-level manual card-to-card workflow regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from gamas_bot.progress import to_persian_digits
from tests.support import FakeJobEvent, FakeTelegramClient, make_settings
from tests.test_bot import FakeCallbackEvent, FakeMessageEvent

ADMIN = 1
USER = 555
OTHER_USER = 556
PLAN = "paid_25h_30d"


class PaymentWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.settings = make_settings(
            db_path=root / "bot.db",
            temp_dir=root / "tmp",
            receipt_dir=root / "receipts",
            note_api_key="",
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.bot.client = FakeTelegramClient()
        for user_id in (ADMIN, USER, OTHER_USER):
            await self.db.ensure_user(user_id)
        await self.db.sync_plans([
            {"code": PLAN, "hours": 25, "price": 150_000, "validity_days": 30},
            {"code": "paid_50h_30d", "hours": 50, "price": 250_000, "validity_days": 30},
        ])

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    async def press(self, sender: int, data: str):
        event = FakeCallbackEvent(sender, data.encode())
        await self.bot._handle_callback(event)
        return event

    async def say(self, sender: int, text: str):
        event = FakeMessageEvent(sender, text)
        await self.bot._handle_message(event)
        return event

    @staticmethod
    def payloads(buttons) -> set[bytes]:
        result = set()
        for row in buttons or []:
            for button in row:
                data = getattr(getattr(button, "type", None), "data", None) or getattr(button, "data", None)
                if data:
                    result.add(data)
        return result

    async def new_order(self, user_id: int = USER) -> int:
        return await self.db.create_payment(user_id, PLAN)

    async def submit_receipt(self, payment_id: int, *, document: bool = False):
        source = self.root / f"source-{payment_id}.png"
        Image.new("RGB", (24, 24), "white").save(source)
        event = FakeJobEvent(source=source, chat_id=USER)
        if document:
            event.message.attach_document(
                "receipt.png", mime_type="image/png", size=source.stat().st_size
            )
        else:
            event.message.photo = object()
            event.message.file.mime_type = "image/png"
            event.message.file.size = source.stat().st_size
        await self.bot._handle_message(event)
        return event

    async def test_complete_photo_receipt_confirmation_and_duplicate_protection(self):
        plans = await self.press(USER, "buy")
        self.assertIn("۲۵ ساعت", plans.edits[-1][0])
        self.assertIn("۱۵۰٬۰۰۰ تومان", plans.edits[-1][0])
        await self.press(USER, f"plan:{PLAN}")
        payment_id = (await self.db.pending_payments())[0]["payment_id"]
        self.assertEqual((await self.db.payment(payment_id))["status"], "awaiting_payment")

        # An administrator cannot approve an order before its receipt exists.
        await self.press(ADMIN, f"payv:{payment_id}:a")
        self.assertEqual((await self.db.payment(payment_id))["status"], "awaiting_payment")
        self.assertEqual(await self.db.available_seconds(USER), 0)
        empty_admin_list = await self.press(ADMIN, "adm:pay")
        self.assertIn("پرداختی برای بررسی وجود ندارد", empty_admin_list.edits[-1][0])

        await self.press(USER, f"payr:{payment_id}")
        receipt_event = await self.submit_receipt(payment_id)
        payment = await self.db.payment(payment_id)
        self.assertEqual(payment["status"], "pending_review")
        self.assertEqual(await self.db.available_seconds(USER), 0)
        self.assertTrue(any("در انتظار بررسی" in text for text in receipt_event.replies))
        admin_receipts = [
            item for item in self.bot.client.deliveries if item["entity"] == ADMIN
        ]
        self.assertEqual(len(admin_receipts), 1)
        caption = admin_receipts[0]["caption"]
        self.assertIn(str(USER), caption)
        self.assertIn("۲۵ ساعت", caption)
        self.assertIn("۱۵۰٬۰۰۰ تومان", caption)
        self.assertIn(to_persian_digits(payment_id), caption)

        # A forged user callback and an invalid verdict never change the order.
        await self.press(OTHER_USER, f"payv:{payment_id}:a:yes")
        await self.press(OTHER_USER, f"payv:{payment_id}:r")
        self.assertNotIn(OTHER_USER, self.bot._pending_reject_reason)
        await self.press(ADMIN, f"payv:{payment_id}:forged")
        self.assertEqual((await self.db.payment(payment_id))["status"], "pending_review")

        confirmation = await self.press(ADMIN, f"payv:{payment_id}:a")
        self.assertEqual((await self.db.payment(payment_id))["status"], "pending_review")
        self.assertIn(
            f"payv:{payment_id}:a:yes".encode(),
            self.payloads(confirmation.edits[-1][1]),
        )
        self.assertEqual(await self.db.available_seconds(USER), 0)

        await self.press(ADMIN, f"payv:{payment_id}:a:yes")
        approved = await self.db.payment(payment_id)
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["reviewed_by"], ADMIN)
        self.assertIsNotNone(approved["credit_ledger_id"])
        self.assertEqual(await self.db.available_seconds(USER), 25 * 3600)
        self.assertTrue(any(
            item["entity"] == USER and "پرداخت شما تأیید شد" in item["text"]
            for item in self.bot.client.texts
        ))

        # Stale keyboards and retries cannot add a second ledger lot.
        await self.press(ADMIN, f"payv:{payment_id}:a:yes")
        self.assertEqual(await self.db.available_seconds(USER), 25 * 3600)
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE payment_id = ? AND kind = 'credit'",
            (payment_id,),
        )
        self.assertEqual(rows[0][0], 1)

        history = await self.press(USER, "payments")
        self.assertIn("تأیید شده", history.edits[-1][0])
        admin_history = await self.press(ADMIN, "adm:ph:0")
        self.assertIn("تأیید شده", admin_history.edits[-1][0])

    async def test_image_document_receipt_is_supported(self):
        payment_id = await self.new_order()
        await self.press(USER, f"payr:{payment_id}")
        await self.submit_receipt(payment_id, document=True)
        self.assertEqual((await self.db.payment(payment_id))["status"], "pending_review")

    async def test_receipt_prompt_rejects_other_input_and_start_disarms_it(self):
        payment_id = await self.new_order()
        await self.press(USER, f"payr:{payment_id}")
        invalid = FakeJobEvent(chat_id=USER)
        invalid.message.message = "این پیام رسید نیست"
        await self.bot._handle_message(invalid)
        self.assertTrue(any("رسید باید عکس" in text for text in invalid.replies))
        self.assertEqual(self.bot._pending_payments.get(USER), payment_id)
        self.assertEqual((await self.db.payment(payment_id))["status"], "awaiting_payment")

        start = FakeJobEvent(chat_id=USER)
        start.message.message = "/start"
        await self.bot._handle_message(start)
        self.assertNotIn(USER, self.bot._pending_payments)
        await self.press(USER, f"payr:{payment_id}")
        await self.submit_receipt(payment_id)
        self.assertEqual((await self.db.payment(payment_id))["status"], "pending_review")

    async def test_rejection_sends_reason_and_never_grants_credit(self):
        payment_id = await self.new_order()
        await self.db.set_payment_receipt(payment_id, "receipt.jpg", user_id=USER)
        await self.press(ADMIN, f"payv:{payment_id}:r")
        await self.say(ADMIN, "رسید ناخوانا است")
        payment = await self.db.payment(payment_id)
        self.assertEqual(payment["status"], "rejected")
        self.assertEqual(payment["reviewed_by"], ADMIN)
        self.assertEqual(payment["review_reason"], "رسید ناخوانا است")
        self.assertEqual(await self.db.available_seconds(USER), 0)
        self.assertTrue(any(
            item["entity"] == USER and "دلیل: رسید ناخوانا است" in item["text"]
            for item in self.bot.client.texts
        ))
        await self.press(ADMIN, f"payv:{payment_id}:a:yes")
        self.assertEqual(await self.db.available_seconds(USER), 0)

    async def test_failed_user_notification_does_not_repeat_activation(self):
        payment_id = await self.new_order()
        await self.db.set_payment_receipt(payment_id, "receipt.jpg", user_id=USER)

        async def blocked(*_args, **_kwargs):
            raise RuntimeError("bot blocked (simulated)")

        self.bot.client.send_message = blocked
        await self.press(ADMIN, f"payv:{payment_id}:a:yes")
        self.assertEqual((await self.db.payment(payment_id))["status"], "approved")
        self.assertEqual(await self.db.available_seconds(USER), 25 * 3600)
        await self.press(ADMIN, f"payv:{payment_id}:a:yes")
        self.assertEqual(await self.db.available_seconds(USER), 25 * 3600)

    async def test_receipt_upload_enforces_ownership_in_database(self):
        payment_id = await self.new_order()
        changed = await self.db.set_payment_receipt(
            payment_id, "forged.jpg", user_id=OTHER_USER
        )
        self.assertFalse(changed)
        self.assertEqual((await self.db.payment(payment_id))["status"], "awaiting_payment")

    async def test_receipt_delivery_failure_falls_back_to_admin_text_alert(self):
        payment_id = await self.new_order()
        path = self.settings.receipt_dir / "receipt-test.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 24), "white").save(path)
        await self.db.set_payment_receipt(payment_id, str(path), user_id=USER)

        async def failed_upload(*_args, **_kwargs):
            raise RuntimeError("temporary upload failure (simulated)")

        self.bot.client.send_file = failed_upload
        await self.bot._notify_admins_of_payment(payment_id)
        self.assertTrue(any(
            item["entity"] == ADMIN and "جزئیات سفارش" in item["text"]
            for item in self.bot.client.texts
        ))
        self.assertEqual((await self.db.payment(payment_id))["status"], "pending_review")
        self.assertEqual(await self.db.available_seconds(USER), 0)


if __name__ == "__main__":
    unittest.main()
