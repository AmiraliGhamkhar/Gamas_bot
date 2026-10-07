"""End-to-end (offline) payment, credit-management and receipt-isolation tests.

Every test here runs against a real SQLite database and the real bot handlers
with Telethon replaced by a recording double: no network, no Telegram login.
The contracts pinned down are the ones a paying user and an administrator
depend on:

* a receipt image never becomes a transcription job, and a lecture file never
  becomes a receipt;
* payment state is database state, so it survives a restart;
* only an administrator sees a receipt, and only once;
* approval is atomic and idempotent; rejection grants nothing;
* the admin credit screen reads the same entitlement rows billing consumes and
  every manual adjustment carries a reason and an audit entry.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gamas_bot.billing import FREE_PLAN_SECONDS, PLAN_25_SECONDS
from gamas_bot.bot import StudyBot, _is_receipt_image
from gamas_bot.database import Database
from support import make_settings


class _Event:
    """Minimal incoming-message double."""

    def __init__(self, *, photo=False, document=False, mime="image/jpeg", name="receipt.jpg"):
        file_obj = SimpleNamespace(
            mime_type=mime,
            name=name,
            size=1024,
            duration=None,
            id="file-id-123",
        )
        message = SimpleNamespace(
            id=555,
            photo=SimpleNamespace(file_id="photo") if photo else None,
            document=SimpleNamespace(file_id="doc") if document else None,
            voice=None,
            audio=None,
            video=None,
            video_note=None,
            gif=None,
            media=None,
            file=file_obj,
            download_media=AsyncMock(),
        )
        self.message = message
        self.is_private = True
        self.raw_text = ""
        self.replies: list[str] = []
        self.responses: list[str] = []

    async def get_sender(self):
        return SimpleNamespace(id=101, username="student", bot=False)

    async def reply(self, text="", **_kwargs):
        self.replies.append(text)

    async def respond(self, text="", **_kwargs):
        self.responses.append(text)


class PaymentIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "payments.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({7}),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()
        await self.bot.db.sync_plan_catalog(
            [plan.as_record() for plan in __import__("gamas_bot.billing", fromlist=["plan_catalog"]).plan_catalog()]
        )
        self.bot._pending_admin_actions = {}
        self.bot._pending_credential_setup = {}
        self.bot.provider_health = SimpleNamespace(invalidate=lambda: None)
        self.user = await self.bot.db.upsert_user(101, "student")

    async def asyncTearDown(self):
        await self.bot.db.close()
        self.temp.cleanup()

    def _stub_media(self) -> list[str]:
        """Record media acceptance; the receipt handler answers like production."""
        accepted: list[str] = []

        async def accept_media(*_args, **_kwargs):
            accepted.append("media")

        async def handle_receipt(event, _user, _payment):
            return _is_receipt_image(event.message)

        self.bot._accept_media = accept_media
        self.bot._accept_presentation = AsyncMock()
        self.bot._handle_receipt_upload = AsyncMock(side_effect=handle_receipt)
        return accepted

    async def test_receipt_image_with_an_open_payment_is_never_a_media_job(self):
        await self.bot.db.create_payment_request(int(self.user["id"]), "paid_25h_30d")
        accepted = self._stub_media()
        event = _Event(photo=True)
        await self.bot._handle_message(event)
        self.bot._handle_receipt_upload.assert_awaited_once()
        self.assertEqual(accepted, [])

    async def test_a_lecture_file_with_an_open_payment_still_goes_to_the_pipeline(self):
        await self.bot.db.create_payment_request(int(self.user["id"]), "paid_25h_30d")
        accepted = self._stub_media()
        audio = _Event(document=True, mime="audio/mpeg", name="lecture.mp3")
        await self.bot._handle_message(audio)
        # The receipt handler is consulted (payment state is open) but declines a
        # non-image, so the lecture still reaches the media pipeline.
        self.bot._handle_receipt_upload.assert_awaited_once()
        self.assertEqual(accepted, ["media"])

    async def test_a_second_image_for_a_pending_payment_is_refused_with_the_request_id(self):
        request = await self.bot.db.create_payment_request(int(self.user["id"]), "paid_25h_30d")
        await self.bot.db.submit_payment_receipt(
            int(request["id"]), int(self.user["id"]), str(self.root / "r.jpg"), 99
        )
        accepted = self._stub_media()
        event = _Event(photo=True)
        await self.bot._handle_message(event)
        self.assertEqual(accepted, [])
        self.bot._handle_receipt_upload.assert_not_awaited()
        self.assertTrue(any(str(request["id"]) in reply for reply in event.replies), event.replies)

    def test_receipt_detection_is_image_only(self):
        photo = _Event(photo=True)
        self.assertTrue(_is_receipt_image(photo.message, "image/jpeg"))
        png = _Event(document=True, mime="image/png", name="r.png")
        self.assertTrue(_is_receipt_image(png.message, "image/png"))
        audio = _Event(document=True, mime="audio/mpeg", name="lecture.mp3")
        self.assertFalse(_is_receipt_image(audio.message, "audio/mpeg"))
        video = _Event(document=True, mime="video/mp4", name="lecture.mp4")
        self.assertFalse(_is_receipt_image(video.message, "video/mp4"))
        deck = _Event(document=True, mime="application/vnd.ms-powerpoint", name="slides.ppt")
        self.assertFalse(_is_receipt_image(deck.message, "application/vnd.ms-powerpoint"))

    async def test_payment_and_entitlement_state_survive_a_restart(self):
        request = await self.bot.db.create_payment_request(int(self.user["id"]), "paid_25h_30d")
        await self.bot.db.submit_payment_receipt(
            int(request["id"]), int(self.user["id"]), str(self.root / "r.jpg"), 99
        )
        await self.bot.db.close()
        reopened = Database(self.settings.database_path)
        await reopened.open()
        try:
            detail = await reopened.payment_detail(int(request["id"]))
            self.assertEqual(detail["status"], "pending")
            self.assertEqual(detail["receipt_path"], str(self.root / "r.jpg"))
            self.assertEqual(detail["receipt_message_id"], 99)
            approved = await reopened.approve_payment(int(request["id"]), 7)
            self.assertIsNotNone(approved)
        finally:
            await reopened.close()
        again = Database(self.settings.database_path)
        await again.open()
        try:
            detail = await again.payment_detail(int(request["id"]))
            self.assertEqual(detail["status"], "approved")
            balance = await again.user_balance(int(self.user["id"]))
            self.assertEqual(balance["available_seconds"], FREE_PLAN_SECONDS + PLAN_25_SECONDS)
            # A second approval after the restart cannot grant again.
            self.assertIsNone(await again.approve_payment(int(request["id"]), 8))
        finally:
            await again.close()
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()


class PaymentReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "review.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({7}),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()
        self.bot._pending_admin_actions = {}
        self.bot.provider_health = SimpleNamespace(invalidate=lambda: None)
        self.bot.client = SimpleNamespace(
            send_message=AsyncMock(), send_file=AsyncMock()
        )
        self.bot._cleanup_expired_receipts = AsyncMock()
        self.bot._receipt_root = self.root
        self.user = await self.bot.db.upsert_user(101, "student")

    async def asyncTearDown(self):
        await self.bot.db.close()
        self.temp.cleanup()

    async def _pending_payment(self) -> dict:
        request = await self.bot.db.create_payment_request(int(self.user["id"]), "paid_50h_30d")
        receipt = self.root / "private-receipt.jpg"
        receipt.write_bytes(b"\xff\xd8\xff\xe0private")
        await self.bot.db.submit_payment_receipt(
            int(request["id"]), int(self.user["id"]), str(receipt), 77
        )
        return request

    async def test_admin_lists_a_pending_payment_including_its_private_receipt(self):
        request = await self._pending_payment()
        event = SimpleNamespace(
            respond=AsyncMock(), answer=AsyncMock(), edit=AsyncMock()
        )
        await self.bot._show_pending_payments(event)
        captions = [call.args[0] if call.args else call.kwargs.get("caption", "")
                    for call in event.respond.await_args_list]
        files = [call.kwargs.get("file") for call in event.respond.await_args_list]
        self.assertTrue(any(str(request["id"]) in caption for caption in captions), captions)
        self.assertEqual(files[0], str(self.root / "private-receipt.jpg"))

    async def test_approval_notifies_the_user_and_is_idempotent(self):
        request = await self._pending_payment()
        event = SimpleNamespace(edit=AsyncMock(), answer=AsyncMock(), respond=AsyncMock())
        await self.bot._payment_callback(event, 7, "approve", int(request["id"]))
        self.bot.client.send_message.assert_awaited_once()
        target, message = self.bot.client.send_message.await_args.args[:2]
        self.assertEqual(int(target), 101)
        self.assertIn("تأیید", message)
        # A stale second click changes nothing and does not notify twice.
        await self.bot._payment_callback(event, 8, "approve", int(request["id"]))
        self.bot.client.send_message.assert_awaited_once()
        self.assertEqual(
            (await self.bot.db.payment_detail(int(request["id"])))["status"], "approved"
        )

    async def test_rejection_stores_the_reason_and_never_grants_credit(self):
        request = await self._pending_payment()
        before = await self.bot.db.user_balance(int(self.user["id"]))
        reason = "مبلغ واریزشده با طرح هم‌خوانی ندارد"
        event = SimpleNamespace(reply=AsyncMock(), respond=AsyncMock(), answer=AsyncMock())
        self.bot._pending_admin_actions[7] = f"payment_reject:{request['id']}"
        await self.bot._handle_pending_admin_input(event, 7, f"payment_reject:{request['id']}", reason)
        detail = await self.bot.db.payment_detail(int(request["id"]))
        self.assertEqual(detail["status"], "rejected")
        self.assertEqual(detail["rejection_reason"], reason)
        self.assertEqual(detail["admin_note"], reason)
        self.assertEqual(detail["reviewer_telegram_id"], 7)
        self.assertIsNotNone(detail["reviewed_at"])
        after = await self.bot.db.user_balance(int(self.user["id"]))
        self.assertEqual(after["available_seconds"], before["available_seconds"])
        # The user is told, and can start a new request afterwards.
        self.bot.client.send_message.assert_awaited_once()
        self.assertTrue(await self.bot.db.create_payment_request(int(self.user["id"]), "paid_25h_30d"))

    async def test_rejection_reason_is_sanitized_before_storage(self):
        request = await self._pending_payment()
        event = SimpleNamespace(reply=AsyncMock(), respond=AsyncMock(), answer=AsyncMock())
        dirty = "خطا\x00\x07 در متن | " + "ط" * 900
        await self.bot._handle_pending_admin_input(
            event, 7, f"payment_reject:{request['id']}", dirty
        )
        detail = await self.bot.db.payment_detail(int(request["id"]))
        self.assertEqual(detail["status"], "rejected")
        self.assertNotIn("\x00", detail["rejection_reason"])
        self.assertNotIn("\x07", detail["rejection_reason"])
        self.assertLessEqual(len(detail["rejection_reason"]), 500)

    async def test_receipt_file_id_is_recorded_for_later_recovery(self):
        request = await self.bot.db.create_payment_request(int(self.user["id"]), "paid_25h_30d")
        accepted = await self.bot.db.submit_payment_receipt(
            int(request["id"]), int(self.user["id"]), str(self.root / "r.jpg"), 42,
            receipt_file_id="telegram-file-id-42",
        )
        self.assertTrue(accepted)
        detail = await self.bot.db.payment_detail(int(request["id"]))
        self.assertEqual(detail["receipt_file_id"], "telegram-file-id-42")


class AdminCreditScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "credits.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({7}),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()
        self.bot._pending_admin_actions = {}
        self.bot.provider_health = SimpleNamespace(invalidate=lambda: None)
        self.bot.client = SimpleNamespace(send_message=AsyncMock(), send_file=AsyncMock())
        self.user = await self.bot.db.upsert_user(101, "student")

    async def asyncTearDown(self):
        await self.bot.db.close()
        self.temp.cleanup()

    async def test_overview_splits_free_and_paid_credit_and_lists_expiry(self):
        request = await self.bot.db.create_payment_request(int(self.user["id"]), "paid_25h_30d")
        await self.bot.db.submit_payment_receipt(
            int(request["id"]), int(self.user["id"]), str(self.root / "r.jpg"), 5
        )
        await self.bot.db.approve_payment(int(request["id"]), 7)
        overview = await self.bot.db.admin_user_credit_overview(101)
        self.assertEqual(overview["free_seconds"], FREE_PLAN_SECONDS)
        self.assertEqual(overview["paid_seconds"], PLAN_25_SECONDS)
        self.assertEqual(
            overview["available_seconds"], FREE_PLAN_SECONDS + PLAN_25_SECONDS
        )
        self.assertEqual(len(overview["entitlements"]), 2)
        paid = next(row for row in overview["entitlements"] if row["source"] == "payment")
        self.assertIsNotNone(paid["expires_at"])
        text = self.bot._admin_credit_text(overview)
        self.assertIn("رایگان", text)
        self.assertIn("خریداری‌شده", text)
        self.assertIn("۲۵ ساعت / ۳۰ روز", text)
        self.assertIn(str(paid["expires_at"])[:10], text)
        # The screen never leaks receipt storage details.
        self.assertNotIn(str(self.root), text)

    async def test_manual_adjustment_requires_seconds_and_a_reason(self):
        event = SimpleNamespace(reply=AsyncMock(), respond=AsyncMock(), answer=AsyncMock())
        self.bot._pending_admin_actions[7] = "credit_add:101"
        await self.bot._handle_pending_admin_input(event, 7, "credit_add:101", "120")
        self.assertIn("3600", " ".join(call.args[0] for call in event.reply.await_args_list))
        self.bot._pending_admin_actions[7] = "credit_add:101"
        await self.bot._handle_pending_admin_input(event, 7, "credit_add:101", "120 | ")
        self.assertEqual(
            (await self.bot.db.user_balance(int(self.user["id"])))["available_seconds"],
            FREE_PLAN_SECONDS,
        )
        self.bot._pending_admin_actions[7] = "credit_add:101"
        await self.bot._handle_pending_admin_input(event, 7, "credit_add:101", "120 | بابت قطعی")
        balance = await self.bot.db.user_balance(int(self.user["id"]))
        self.assertEqual(balance["available_seconds"], FREE_PLAN_SECONDS + 120)
        entries = await self.bot.db.audit_entries(limit=10)
        credit = next(row for row in entries if row["action"] == "credit_granted")
        self.assertEqual(credit["admin_telegram_id"], 7)
        self.assertEqual(credit["details"]["seconds"], 120)
        self.assertEqual(credit["details"]["reason"], "بابت قطعی")

    async def test_admin_credit_is_never_granted_to_another_admin(self):
        event = SimpleNamespace(reply=AsyncMock(), respond=AsyncMock(), answer=AsyncMock())
        admin_user = await self.bot.db.upsert_user(7, "admin")
        before = await self.bot.db.user_balance(int(admin_user["id"]))
        self.bot._pending_admin_actions[7] = "credit_add:7"
        await self.bot._handle_pending_admin_input(event, 7, "credit_add:7", "600 | مساعدت")
        after = await self.bot.db.user_balance(int(admin_user["id"]))
        self.assertEqual(after["available_seconds"], before["available_seconds"])
        self.assertTrue(
            any("مدیر دیگری" in call.args[0] for call in event.reply.await_args_list),
            event.reply.await_args_list,
        )

    async def test_unknown_user_id_is_reported_without_crashing(self):
        event = SimpleNamespace(reply=AsyncMock())
        self.bot._pending_admin_actions[7] = "credits_lookup"
        await self.bot._handle_pending_admin_input(event, 7, "credits_lookup", "999999")
        self.assertTrue(any("پیدا نشد" in call.args[0] for call in event.reply.await_args_list))


class ReceiptIsolationTests(unittest.IsolatedAsyncioTestCase):
    """A receipt is administrator-only, and its path can never escape storage."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "isolation.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({7}),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()
        self.bot._pending_admin_actions = {}
        self.bot.provider_health = SimpleNamespace(invalidate=lambda: None)
        self.bot._receipt_root = self.root / "receipts"
        self.bot._receipt_root.mkdir()
        self.bot.client = SimpleNamespace(send_message=AsyncMock(), send_file=AsyncMock())
        self.user = await self.bot.db.upsert_user(101, "student")

    async def asyncTearDown(self):
        await self.bot.db.close()
        self.temp.cleanup()

    async def _pending_payment(self) -> dict:
        request = await self.bot.db.create_payment_request(int(self.user["id"]), "paid_25h_30d")
        receipt = self.bot._receipt_root / "receipt-secret.jpg"
        receipt.write_bytes(b"\xff\xd8\xff\xe0private")
        await self.bot.db.submit_payment_receipt(
            int(request["id"]), int(self.user["id"]), str(receipt), 12
        )
        return request

    def _callback_event(self, data: bytes, sender_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            data=data,
            is_private=True,
            get_sender=AsyncMock(
                return_value=SimpleNamespace(id=sender_id, username="user", bot=False)
            ),
            answer=AsyncMock(),
            edit=AsyncMock(),
            respond=AsyncMock(),
        )

    async def test_a_non_admin_cannot_open_a_receipt_or_a_payment(self):
        request = await self._pending_payment()
        for data in (
            f"admin:payment:receipt:{request['id']}",
            f"admin:payment:approve:{request['id']}",
            f"admin:payment:reject:{request['id']}",
            b"admin:payments".decode(),
            b"admin:credits".decode(),
            b"admin:credentials".decode(),
            b"admin:health".decode(),
        ):
            with self.subTest(data=data):
                event = self._callback_event(data.encode(), sender_id=101)
                await self.bot._handle_callback(event)
                event.edit.assert_not_awaited()
                event.respond.assert_not_awaited()
                event.answer.assert_awaited_once()
        # The payment is still pending after every refused attempt.
        detail = await self.bot.db.payment_detail(int(request["id"]))
        self.assertEqual(detail["status"], "pending")
        # And the user (id 101) never received a file.
        self.bot.client.send_file.assert_not_awaited()

    async def test_receipt_paths_outside_private_storage_are_refused(self):
        outside = self.root / "public" / "receipt.jpg"
        outside.parent.mkdir()
        outside.write_bytes(b"\xff\xd8\xff")
        for candidate in (
            "/etc/passwd",
            str(outside),
            str(self.bot._receipt_root / ".." / "public" / "receipt.jpg"),
            "../../public_html/receipt.jpg",
        ):
            with self.subTest(candidate=candidate):
                self.assertIsNone(self.bot._safe_receipt_path(candidate))
        self.assertIsNone(self.bot._safe_receipt_path(None))
        inside = self.bot._receipt_root / "receipt-ok.jpg"
        inside.write_bytes(b"\xff\xd8\xff")
        self.assertEqual(self.bot._safe_receipt_path(str(inside)), inside)

    async def test_group_chat_cannot_reach_the_admin_payment_panel(self):
        request = await self._pending_payment()
        event = self._callback_event(f"admin:payment:receipt:{request['id']}".encode(), 7)
        event.is_private = False
        await self.bot._handle_callback(event)
        event.edit.assert_not_awaited()
        event.respond.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
