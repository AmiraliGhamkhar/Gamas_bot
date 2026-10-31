"""Mission acceptance suite: one executable check per Definition-of-Done item.

The broader suite covers every path in isolation; this file drives the real
Telegram handlers end to end and asserts, in one place, the database state and
the exact Persian copy the user and the administrator see for the manual
payment workflow, the support tickets, and the main menu.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from gamas_bot.billing import PLAN_25_CODE, PLAN_50_CODE, plan_catalog
from gamas_bot.bot import GamasBot, SUPPORT_CATEGORY_BUTTONS
from gamas_bot.db import Database
from gamas_bot.media import MediaInfo
from gamas_bot.notes import StructuredNotes
from gamas_bot.progress import to_persian_digits
from gamas_bot.transcript import RawTranscript
from tests.support import FakeJobEvent, FakeTelegramClient, make_settings, wav_bytes
from tests.test_bot import FakeCallbackEvent, FakeMessageEvent

ADMIN = 1
USER = 555
OTHER = 556


class MissionAcceptanceCase(unittest.IsolatedAsyncioTestCase):
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
        for user_id in (ADMIN, USER, OTHER):
            await self.db.ensure_user(user_id)
        # Seed exactly what GamasBot.run() seeds in production.
        await self.db.sync_plans([
            {
                "code": plan.code,
                "hours": plan.included_seconds / 3600,
                "price": plan.price_toman,
                "validity_days": plan.validity_days,
            }
            for plan in plan_catalog(self.settings.tariff_values())
            if not plan.is_free
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

    async def new_order(self) -> int:
        return await self.db.create_payment(USER, PLAN_25_CODE)

    async def upload_receipt_photo(self, payment_id: int):
        source = self.root / f"receipt-{payment_id}.png"
        Image.new("RGB", (24, 24), "white").save(source)
        event = FakeJobEvent(source=source, chat_id=USER)
        event.message.photo = object()
        event.message.file.mime_type = "image/png"
        event.message.file.size = source.stat().st_size
        await self.bot._handle_message(event)
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

    def texts_to(self, entity: int) -> list[str]:
        return [item["text"] for item in self.bot.client.texts if item["entity"] == entity]

    # ------------------------------------------------------------- payments
    async def test_approval_flow_activates_once_with_the_expected_copy(self):
        buy = await self.press(USER, "wallet")
        screen = buy.edits[-1][0]
        self.assertIn("۲۵ ساعت", screen)
        self.assertIn("۱۵۰٬۰۰۰ تومان", screen)
        self.assertIn("۵۰ ساعت", screen)
        self.assertIn("۲۵۰٬۰۰۰ تومان", screen)

        plan_tap = await self.press(USER, f"plan:{PLAN_25_CODE}")
        order_id = (await self.db.pending_payments())[0]["payment_id"]
        self.assertEqual((await self.db.payment(order_id))["status"], "awaiting_payment")
        order_screen = plan_tap.responses[-1][0]
        self.assertIn("شماره کارت", order_screen)
        self.assertIn("بانک تست", order_screen)
        self.assertIn("پس از واریز، تصویر رسید را بفرستید", order_screen)

        # Nothing is listed for review and no credit exists before the receipt.
        empty_queue = await self.press(ADMIN, "adm:pay")
        self.assertIn("پرداختی برای بررسی وجود ندارد", empty_queue.edits[-1][0])
        await self.press(ADMIN, f"payv:{order_id}:a:yes")
        self.assertEqual((await self.db.payment(order_id))["status"], "awaiting_payment")
        self.assertEqual(await self.db.available_seconds(USER), 0)

        await self.press(USER, f"payr:{order_id}")
        receipt_event = await self.upload_receipt_photo(order_id)
        self.assertEqual((await self.db.payment(order_id))["status"], "pending_review")
        self.assertTrue(any(
            "🕒 رسید پرداخت شما ثبت شد و در انتظار بررسی است" in text
            for text in receipt_event.replies
        ))
        self.assertEqual(await self.db.available_seconds(USER), 0)

        admin_deliveries = [
            item for item in self.bot.client.deliveries if item["entity"] == ADMIN
        ]
        self.assertEqual(len(admin_deliveries), 1)
        caption = admin_deliveries[0]["caption"]
        for expected in (to_persian_digits(order_id), str(USER), "۲۵ ساعت", "۱۵۰٬۰۰۰ تومان"):
            self.assertIn(expected, caption)

        detail = await self.press(ADMIN, f"adm:pd:{order_id}")
        detail_payloads = self.payloads(detail.edits[-1][1])
        self.assertIn(f"payv:{order_id}:a".encode(), detail_payloads)
        self.assertIn(f"payv:{order_id}:r".encode(), detail_payloads)

        # The first approval tap only renders a confirmation; nothing changes.
        confirmation = await self.press(ADMIN, f"payv:{order_id}:a")
        self.assertEqual((await self.db.payment(order_id))["status"], "pending_review")
        self.assertIn(
            f"payv:{order_id}:a:yes".encode(),
            self.payloads(confirmation.edits[-1][1]),
        )

        await self.press(ADMIN, f"payv:{order_id}:a:yes")
        approved = await self.db.payment(order_id)
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["reviewed_by"], ADMIN)
        self.assertIsNotNone(approved["reviewed_at"])
        self.assertEqual(await self.db.available_seconds(USER), 25 * 3600)
        self.assertTrue(any(
            text.startswith("✅ پرداخت شما تأیید شد. اشتراک شما فعال است.")
            for text in self.texts_to(USER)
        ))

        # A repeated approval (stale keyboard / retry) never credits twice.
        await self.press(ADMIN, f"payv:{order_id}:a:yes")
        self.assertEqual(await self.db.available_seconds(USER), 25 * 3600)
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE payment_id = ? AND kind = 'credit'",
            (order_id,),
        )
        self.assertEqual(rows[0][0], 1)

        # Both histories report the final state.
        user_history = await self.press(USER, "wallet")
        self.assertIn("تأیید شده", user_history.edits[-1][0])
        admin_history = await self.press(ADMIN, "adm:ph:0")
        self.assertIn("تأیید شده", admin_history.edits[-1][0])

    async def test_rejection_tells_the_user_the_reason_and_grants_nothing(self):
        order_id = await self.new_order()
        await self.press(USER, f"payr:{order_id}")
        await self.upload_receipt_photo(order_id)

        await self.press(ADMIN, f"payv:{order_id}:r")
        await self.say(ADMIN, "مبلغ واریزی کمتر از مبلغ بسته است")
        order = await self.db.payment(order_id)
        self.assertEqual(order["status"], "rejected")
        self.assertEqual(order["reviewed_by"], ADMIN)
        self.assertEqual(await self.db.available_seconds(USER), 0)
        self.assertTrue(any(
            text.startswith("❌ پرداخت تأیید نشد.")
            and "دلیل: مبلغ واریزی کمتر از مبلغ بسته است" in text
            for text in self.texts_to(USER)
        ))

        # A later approval attempt on a decided order is refused.
        await self.press(ADMIN, f"payv:{order_id}:a:yes")
        self.assertEqual(await self.db.available_seconds(USER), 0)

        # The user-visible history shows the rejection and its reason.
        history = await self.press(USER, "wallet")
        self.assertIn("رد شده", history.edits[-1][0])
        self.assertIn("دلیل: مبلغ واریزی کمتر از مبلغ بسته است", history.edits[-1][0])

    async def test_cancellation_visible_grants_nothing_and_rejection_presets_apply(self):
        order_id = await self.new_order()
        await self.press(USER, f"payc:{order_id}")
        self.assertEqual((await self.db.payment(order_id))["status"], "cancelled")
        self.assertEqual(await self.db.available_seconds(USER), 0)
        history = await self.press(USER, "wallet")
        self.assertIn("لغو شده", history.edits[-1][0])

        second_id = await self.new_order()
        await self.press(USER, f"payr:{second_id}")
        await self.upload_receipt_photo(second_id)
        await self.press(ADMIN, f"payv:{second_id}:rp:blur")
        rejected = await self.db.payment(second_id)
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("ناخوانا", rejected["review_reason"])
        self.assertEqual(await self.db.available_seconds(USER), 0)

    async def test_catalogue_units_and_free_plan_invariants(self):
        catalogue = {plan.code: plan for plan in plan_catalog(self.settings.tariff_values())}
        standard = catalogue[PLAN_25_CODE]
        plus = catalogue[PLAN_50_CODE]
        free = catalogue["free_1h"]
        self.assertEqual((standard.included_seconds, standard.price_toman, standard.validity_days),
                         (25 * 3600, 150_000, 30))
        self.assertEqual((plus.included_seconds, plus.price_toman, plus.validity_days),
                         (50 * 3600, 250_000, 30))
        self.assertEqual((free.included_seconds, free.price_toman), (3600, 0))

        self.assertTrue(await self.db.grant_free_plan_once(USER, 3600))
        self.assertFalse(await self.db.grant_free_plan_once(USER, 3600))
        self.assertEqual(await self.db.available_seconds(USER), 3600)

    # -------------------------------------------------------------- support
    async def test_two_way_ticket_conversation_with_the_expected_copy(self):
        public_ids: list[str] = []
        for category in ("payment", "technical", "subscription", "other"):
            await self.press(USER, "sup:new")
            await self.press(USER, f"sup:cat:{category}")
            creation = await self.say(USER, f"شرح درخواست در دستهٔ {category}")
            reply = creation.replies[-1]
            match = re.fullmatch(
                r"✅ درخواست شما ثبت شد\.\n"
                r"شماره پیگیری: (T-\d{6})\n"
                r"از همین بخش می‌توانید پاسخ‌ها را پیگیری کنید\.",
                reply,
            )
            self.assertIsNotNone(match, reply)
            public_ids.append(match.group(1))
            ticket = (await self.db.list_user_tickets(USER, limit=1))[0]
            self.assertEqual(ticket["public_ticket_id"], match.group(1))
            self.assertEqual(ticket["category"], category)
            self.assertEqual(ticket["status"], "awaiting_admin")
        self.assertEqual(len(set(public_ids)), 4)
        admin_alerts = self.texts_to(ADMIN)
        self.assertTrue(all(
            any(public_id in text and "درخواست پشتیبانی جدید" in text for text in admin_alerts)
            for public_id in public_ids
        ))

        # The administrator answers the selected ticket; the user is told.
        ticket = (await self.db.list_user_tickets(USER, limit=1))[0]
        await self.press(ADMIN, "adm:sup")
        await self.press(ADMIN, f"adm:sv:{ticket['id']}")
        await self.press(ADMIN, f"adm:sr:{ticket['id']}")
        await self.say(ADMIN, "پاسخ پشتیبانی ثبت شد؛ لطفاً دوباره امتحان کنید.")
        self.assertEqual(
            (await self.db.get_ticket(ticket["id"]))["status"], "awaiting_user"
        )
        self.assertTrue(any(
            text == f"📩 پاسخ جدیدی برای درخواست {ticket['public_ticket_id']} ثبت شده است."
            for text in self.texts_to(USER)
        ))

        # The user answers back; every administrator is told.
        await self.press(USER, f"sup:r:{ticket['id']}")
        await self.say(USER, "ممنون؛ بررسی و نتیجه را اعلام می‌کنم.")
        self.assertEqual(
            (await self.db.get_ticket(ticket["id"]))["status"], "awaiting_admin"
        )
        self.assertTrue(any("پیام جدیدی" in text for text in self.texts_to(ADMIN)))

    async def test_close_reopen_keeps_history_and_new_tickets_stay_separate(self):
        await self.press(USER, "sup:new")
        await self.press(USER, "sup:cat:technical")
        await self.say(USER, "مشکل در مشاهدهٔ جزوه")
        ticket = (await self.db.list_user_tickets(USER, limit=1))[0]
        await self.press(USER, f"sup:c:{ticket['id']}")
        self.assertEqual((await self.db.get_ticket(ticket["id"]))["status"], "closed")
        await self.press(USER, f"sup:o:{ticket['id']}")
        self.assertEqual((await self.db.get_ticket(ticket["id"]))["status"], "open")
        messages = await self.db.ticket_messages(ticket["id"], limit=10)
        self.assertEqual([m["message_text"] for m in messages], ["مشکل در مشاهدهٔ جزوه"])

        await self.press(USER, "sup:new")
        await self.press(USER, "sup:cat:other")
        await self.say(USER, "مشکل دوم، درخواست مستقل")
        tickets = await self.db.list_user_tickets(USER, limit=10)
        self.assertEqual(len(tickets), 2)
        counts = [await self.db.count_ticket_messages(t["id"]) for t in tickets]
        self.assertEqual(sorted(counts), [1, 1])

    async def test_forbidden_and_forged_actions_are_refused(self):
        order_id = await self.new_order()
        await self.db.set_payment_receipt(order_id, "receipt.jpg", user_id=USER)
        for data in (f"payv:{order_id}:a:yes", "adm:sup", "adm:pay", f"payv:{order_id}:r"):
            event = await self.press(OTHER, data)
            self.assertTrue(event.answers and event.answers[-1][1], data)
        self.assertEqual((await self.db.payment(order_id))["status"], "pending_review")
        self.assertEqual(await self.db.available_seconds(USER), 0)

        await self.press(USER, "sup:new")
        await self.press(USER, "sup:cat:payment")
        await self.say(USER, "رسیدم بررسی نشده است")
        ticket = (await self.db.list_user_tickets(USER, limit=1))[0]
        view = await self.press(OTHER, f"sup:v:{ticket['id']}")
        self.assertTrue(view.answers and view.answers[-1][1])
        rendered = " ".join(text for text, _ in view.edits)
        self.assertNotIn("رسیدم بررسی نشده است", rendered)
        reply_prompt = await self.press(OTHER, f"sup:r:{ticket['id']}")
        self.assertTrue(reply_prompt.answers[-1][1])
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 1)

    async def test_notification_outage_keeps_everything_stored(self):
        async def outage(*_args, **_kwargs):
            raise RuntimeError("telegram unavailable (simulated)")

        self.bot.client.send_message = outage
        await self.press(USER, "sup:new")
        await self.press(USER, "sup:cat:other")
        await self.say(USER, "درخواست در زمان قطعی ثبت شد")
        ticket = (await self.db.list_user_tickets(USER, limit=1))[0]
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 1)
        self.assertEqual(ticket["status"], "awaiting_admin")

        await self.press(ADMIN, f"adm:sr:{ticket['id']}")
        await self.say(ADMIN, "پاسخ ثبت‌شده در زمان قطعی")
        self.assertEqual(await self.db.count_ticket_messages(ticket["id"]), 2)
        self.assertEqual((await self.db.get_ticket(ticket["id"]))["status"], "awaiting_user")

        await self.press(USER, f"plan:{PLAN_50_CODE}")
        payment_id = (await self.db.pending_payments())[0]["payment_id"]
        await self.db.set_payment_receipt(payment_id, "receipt.jpg", user_id=USER)
        await self.press(ADMIN, f"payv:{payment_id}:a:yes")
        self.assertEqual((await self.db.payment(payment_id))["status"], "approved")
        self.assertEqual(await self.db.available_seconds(USER), 50 * 3600)
        await self.press(ADMIN, f"payv:{payment_id}:a:yes")
        self.assertEqual(await self.db.available_seconds(USER), 50 * 3600)

    # ------------------------------------------------------------ main menu
    async def test_upload_without_mode_button_runs_full_pipeline_and_saves_both_artifacts(self):
        await self.say(USER, "/start")
        source = self.root / "lecture.ogg"
        source.write_bytes(wav_bytes(0.2))
        event = FakeJobEvent(source=source, chat_id=USER)
        event.message.attach_voice(duration=12)
        self.bot.client = event.client
        raw = RawTranscript.from_text("متن کامل جلسه", engine="deepgram")
        notes = StructuredNotes(title="جزوهٔ خودکار", summary="خلاصهٔ جلسه")

        with (
            mock.patch(
                "gamas_bot.bot.probe_media",
                new=mock.AsyncMock(return_value=MediaInfo(
                    has_audio=True, duration=12.0, audio_codec="opus"
                )),
            ),
            mock.patch("gamas_bot.bot.needs_transcode", return_value=False),
            mock.patch("gamas_bot.bot.transcribe", new=mock.AsyncMock(return_value=raw)),
            mock.patch(
                "gamas_bot.bot.structure_transcript",
                new=mock.AsyncMock(return_value=notes),
            ) as generate_notes,
            mock.patch.object(self.bot, "_http_session", return_value=None),
        ):
            await self.bot._handle_message(event)
            job = self.bot.queue.pop_pending()
            await self.bot._run_job(job)

        generate_notes.assert_awaited_once()
        self.assertTrue(event.file_bytes(".txt"))
        self.assertTrue(event.file_bytes(".docx").startswith(b"PK"))
        self.assertIn("متن خام", event.files[0][0])
        self.assertIn("جزوهٔ Word", event.files[1][0])
        self.assertIn("📚 در کتابخانهٔ شما ذخیره شد", event.files[1][0])
        items = await self.db.library_list(USER, limit=6)
        self.assertEqual([item["kind"] for item in items], ["booklet", "transcript"])
        self.assertEqual((await self.db.submission(job.submission_id)).status, "done")

    async def test_main_menu_matches_the_mission_buttons(self):
        from tests.test_bot import menu_payloads

        menu = self.bot._user_menu()
        payloads = menu_payloads(menu)
        flat_labels = [button.text for row in menu for button in row]
        self.assertEqual(
            flat_labels,
            [
                "📚 کتابخانه من",
                "🛒 خرید اشتراک",
                "🗂 آفیس رایگان",
                "🏆 لیدربورد",
                "🎧 پشتیبانی",
                "ℹ️ راهنما",
            ],
        )
        self.assertEqual(
            payloads,
            {b"library", b"wallet", b"support", b"help", b"board", b"office:start"},
        )

        admin_labels = [button.text for row in self.bot._admin_menu() for button in row]
        # R1: the nine payment/user/health buttons are three hubs now.
        self.assertIn("💳 پرداخت‌ها", admin_labels)
        self.assertIn("👥 کاربران", admin_labels)
        self.assertIn("🩺 سامانه", admin_labels)
        self.assertNotIn("💳 پرداخت‌های در انتظار", admin_labels)
        self.assertNotIn("📋 تاریخچه پرداخت‌ها", admin_labels)
        self.assertIn("🎧 مدیریت پشتیبانی", admin_labels)
        self.assertIn("🏆 برترین کاربران", admin_labels)
        self.assertIn("↩️ پنل کاربری", admin_labels)

        self.assertEqual(
            tuple(SUPPORT_CATEGORY_BUTTONS),
            ("payment", "technical", "subscription", "other"),
        )


if __name__ == "__main__":
    unittest.main()
