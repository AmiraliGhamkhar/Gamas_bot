"""Referral attribution, idempotent rewards, and reporting tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

from gamas_bot import texts
from gamas_bot.billing import format_seconds_as_credits
from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from gamas_bot.db_referrals import REFERRAL_JOIN_SECONDS, REFERRAL_PAYMENT_SECONDS
from tests.support import FakeCallbackEvent, FakeTelegramClient, make_settings
from tests.test_db_billing import DatabaseTestCase


class ReferralCodeTests(DatabaseTestCase):
    async def test_code_is_stable_and_unique(self):
        first = await self.db.ensure_referral_code(100)
        self.assertEqual(await self.db.ensure_referral_code(100), first)
        await self.db.ensure_user(200)
        second = await self.db.ensure_referral_code(200)
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 8)
        self.assertTrue(first.isalnum())

    async def test_unknown_code_is_refused(self):
        await self.db.ensure_user(200)
        self.assertEqual(
            await self.db.attribute_referral(200, "NOPE1234"), ("invalid", 0)
        )


class JoinRewardTests(DatabaseTestCase):
    async def test_first_start_credits_the_referrer_once(self):
        code = await self.db.ensure_referral_code(100)
        await self.db.ensure_user(200)
        self.assertEqual(
            await self.db.attribute_referral(200, code),
            ("rewarded", REFERRAL_JOIN_SECONDS),
        )
        self.assertEqual(await self.db.available_seconds(100), REFERRAL_JOIN_SECONDS)
        summary = await self.db.referral_summary(100)
        self.assertEqual(summary["invited"], 1)
        self.assertEqual(summary["seconds"], REFERRAL_JOIN_SECONDS)
        # A repeated start can never pay twice.
        self.assertEqual(
            await self.db.attribute_referral(200, code), ("duplicate", 0)
        )
        self.assertEqual(await self.db.available_seconds(100), REFERRAL_JOIN_SECONDS)

    async def test_self_referral_is_refused(self):
        code = await self.db.ensure_referral_code(100)
        self.assertEqual(await self.db.attribute_referral(100, code), ("self", 0))
        self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_banned_referrer_records_attribution_without_paying(self):
        code = await self.db.ensure_referral_code(100)
        await self.db.set_banned(100, True, admin_id=1)
        await self.db.ensure_user(200)
        self.assertEqual(
            await self.db.attribute_referral(200, code), ("attributed", 0)
        )
        self.assertEqual(await self.db.available_seconds(100), 0)
        # Unbanned later, the first approved payment still honours the invite.
        await self.db.set_banned(100, False, admin_id=1)
        payment_id = await self.db.create_payment(200, "paid_5h_30d")
        await self.db.set_payment_receipt(payment_id, "receipt.jpg")
        self.assertTrue(await self.db.review_payment(payment_id, True, 1))
        self.assertEqual(await self.db.available_seconds(100), REFERRAL_PAYMENT_SECONDS)


class PaymentRewardTests(DatabaseTestCase):
    async def _referred_user(self) -> int:
        code = await self.db.ensure_referral_code(100)
        await self.db.ensure_user(200)
        await self.db.attribute_referral(200, code)
        return code

    async def test_receipt_upload_grants_no_referral_reward(self):
        await self._referred_user()
        payment_id = await self.db.create_payment(200, "paid_5h_30d")
        await self.db.set_payment_receipt(payment_id, "receipt.jpg")
        self.assertEqual(await self.db.available_seconds(100), REFERRAL_JOIN_SECONDS)
        totals = await self.db.referral_totals()
        self.assertEqual(totals["rewards"], 1)

    async def test_approval_credits_the_referrer_once_and_replays_cleanly(self):
        await self._referred_user()
        payment_id = await self.db.create_payment(200, "paid_5h_30d")
        await self.db.set_payment_receipt(payment_id, "receipt.jpg")
        self.assertTrue(await self.db.review_payment(payment_id, True, 1))
        self.assertEqual(
            await self.db.available_seconds(100),
            REFERRAL_JOIN_SECONDS + REFERRAL_PAYMENT_SECONDS,
        )
        self.assertEqual(
            await self.db.available_seconds(200), 5 * 3600
        )
        # A replayed approval is a no-op for both buyer and referrer.
        self.assertFalse(await self.db.review_payment(payment_id, True, 1))
        self.assertEqual(
            await self.db.available_seconds(100),
            REFERRAL_JOIN_SECONDS + REFERRAL_PAYMENT_SECONDS,
        )
        totals = await self.db.referral_totals()
        self.assertEqual(totals["rewards"], 2)

    async def test_rejection_pays_nothing(self):
        await self._referred_user()
        payment_id = await self.db.create_payment(200, "paid_5h_30d")
        await self.db.set_payment_receipt(payment_id, "receipt.jpg")
        self.assertTrue(
            await self.db.review_payment(payment_id, False, 1, reason="نامعتبر")
        )
        self.assertEqual(await self.db.available_seconds(100), REFERRAL_JOIN_SECONDS)

    async def test_only_first_approved_payment_can_reward(self):
        await self._referred_user()
        first = await self.db.create_payment(200, "paid_5h_30d")
        await self.db.set_payment_receipt(first, "receipt-1.jpg")
        self.assertTrue(await self.db.review_payment(first, True, 1))
        second = await self.db.create_payment(200, "paid_5h_30d")
        await self.db.set_payment_receipt(second, "receipt-2.jpg")
        self.assertTrue(await self.db.review_payment(second, True, 1))
        self.assertEqual(
            await self.db.available_seconds(100),
            REFERRAL_JOIN_SECONDS + REFERRAL_PAYMENT_SECONDS,
        )
        row = await self.db.read_db.execute_fetchall(
            "SELECT first_approved_payment_id, payment_reward_status "
            "FROM referrals WHERE referred_user_id=200"
        )
        self.assertEqual(tuple(row[0]), (first, "rewarded"))
        self.assertEqual((await self.db.referral_totals())["rewards"], 2)

    async def test_concurrent_duplicate_approval_pays_once(self):
        await self._referred_user()
        payment_id = await self.db.create_payment(200, "paid_5h_30d")
        await self.db.set_payment_receipt(payment_id, "receipt-1.jpg")
        other = Database(self.path)
        await other.connect()
        try:
            outcomes = await asyncio.gather(
                self.db.review_payment(payment_id, True, 1),
                other.review_payment(payment_id, True, 2),
            )
        finally:
            await other.close()
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(
            await self.db.available_seconds(100),
            REFERRAL_JOIN_SECONDS + REFERRAL_PAYMENT_SECONDS,
        )
        rows = await self.db.read_db.execute_fetchall(
            "SELECT COUNT(*) FROM referral_rewards "
            "WHERE referred_user_id=200 AND kind='payment'"
        )
        self.assertEqual(rows[0][0], 1)

    async def test_first_approval_during_referrer_ban_permanently_forfeits_reward(self):
        await self._referred_user()
        await self.db.set_banned(100, True, admin_id=1)
        first = await self.db.create_payment(200, "paid_5h_30d")
        await self.db.set_payment_receipt(first, "receipt-1.jpg")
        self.assertTrue(await self.db.review_payment(first, True, 1))
        await self.db.set_banned(100, False, admin_id=1)
        second = await self.db.create_payment(200, "paid_5h_30d")
        await self.db.set_payment_receipt(second, "receipt-2.jpg")
        self.assertTrue(await self.db.review_payment(second, True, 1))
        self.assertEqual(await self.db.available_seconds(100), REFERRAL_JOIN_SECONDS)
        row = await self.db.read_db.execute_fetchall(
            "SELECT first_approved_payment_id, payment_reward_status "
            "FROM referrals WHERE referred_user_id=200"
        )
        self.assertEqual(tuple(row[0]), (first, "forfeited_banned"))

    async def test_top_referrers_orders_by_invites(self):
        first = await self.db.ensure_referral_code(100)
        await self.db.ensure_user(200)
        await self.db.attribute_referral(200, first)
        await self.db.ensure_user(300)
        code_300 = await self.db.ensure_referral_code(300)
        await self.db.ensure_user(400)
        await self.db.attribute_referral(400, code_300)
        await self.db.ensure_user(500)
        await self.db.attribute_referral(500, code_300)
        for referred_user in (400, 500):
            payment_id = await self.db.create_payment(referred_user, "paid_5h_30d")
            await self.db.set_payment_receipt(payment_id, f"receipt-{referred_user}.jpg")
            self.assertTrue(await self.db.review_payment(payment_id, True, 1))
        payment_id = await self.db.create_payment(200, "paid_5h_30d")
        await self.db.set_payment_receipt(payment_id, "receipt-200.jpg")
        self.assertTrue(await self.db.review_payment(payment_id, True, 1))

        rows = await self.db.top_referrers(5)
        self.assertEqual(rows[0]["referrer_id"], 300)
        self.assertEqual(rows[0]["invited"], 2)
        self.assertEqual(rows[0]["seconds"], 2 * (REFERRAL_JOIN_SECONDS + REFERRAL_PAYMENT_SECONDS))
        self.assertEqual(rows[1]["referrer_id"], 100)
        self.assertEqual(rows[1]["invited"], 1)
        self.assertEqual(rows[1]["seconds"], REFERRAL_JOIN_SECONDS + REFERRAL_PAYMENT_SECONDS)


class ReferralScreenTests(unittest.IsolatedAsyncioTestCase):
    """The referral screen must render with a real bot username attached.

    Regression: the share button's URL carried the Persian share text raw
    (spaces and all). That is not a valid URL, Telegram rejected the whole
    inline keyboard with BUTTON_URL_INVALID, and — because the respond
    fallback used the same buttons — pressing «دعوت دوستان» did nothing at
    all. No test saw it because the fake client has no username, so the
    share button was never built.
    """

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "bot.sqlite3",
            temp_dir=root / "tmp",
            receipt_dir=root / "receipts",
            session_path=root / "session",
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.client = FakeTelegramClient()
        self.client.get_me = AsyncMock(
            return_value=SimpleNamespace(username="GamasTestBot")
        )
        self.bot = GamasBot(self.settings, db=self.db, client=self.client)

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def test_referral_screen_renders_with_a_share_button(self):
        event = FakeCallbackEvent(self.client, data=b"ref:home")
        await self.bot._handle_callback(event)

        self.assertTrue(event.edits, "the referral screen never rendered")
        screen = event.edits[-1]
        self.assertIn("دعوت دوستان", screen["text"])
        url_buttons = [
            button
            for row in screen["buttons"]
            for button in row
            if getattr(getattr(button, "type", None), "url", None)
            or getattr(button, "url", None)
        ]
        self.assertEqual(len(url_buttons), 1)
        share_url = getattr(url_buttons[0], "url", None) or url_buttons[0].type.url
        # A URL button must be plain ASCII: every non-ASCII byte and every
        # space must be percent-encoded or Telegram rejects the keyboard.
        self.assertTrue(share_url.isascii(), share_url)
        self.assertNotIn(" ", share_url)
        parsed = urlparse(share_url)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/share/url")
        code = await self.db.ensure_referral_code(event.sender_id)
        self.assertEqual(
            params["url"], [f"https://t.me/GamasTestBot?start=ref_{code}"]
        )
        self.assertEqual(params["text"], [texts.REFERRAL_SHARE_TEXT])
        self.assertIn(f"start=ref_{code}", screen["text"])

    async def test_rules_and_reward_templates_do_not_double_the_unit(self):
        rules = texts.REFERRALS_RULES.format(
            signup_credits=format_seconds_as_credits(REFERRAL_JOIN_SECONDS),
            purchase_credits=format_seconds_as_credits(REFERRAL_PAYMENT_SECONDS),
        )
        self.assertNotIn("اعتبار اعتبار", rules)
        reward = texts.REFERRAL_JOIN_REWARD_TEMPLATE.format(
            seconds=format_seconds_as_credits(REFERRAL_JOIN_SECONDS)
        )
        self.assertNotIn("اعتبار اعتبار", reward)

    async def test_invalid_buttons_never_blank_the_screen(self):
        class RejectingEvent(FakeCallbackEvent):
            async def edit(self, text: str = "", buttons=None):
                raise RuntimeError("BUTTON_URL_INVALID")

            async def respond(self, *args, **kwargs):
                if kwargs.get("buttons") is not None:
                    raise RuntimeError("BUTTON_URL_INVALID")
                return await super().respond(*args, **kwargs)

        event = RejectingEvent(self.client, data=b"ref:home")
        await self.bot._handle_callback(event)
        delivered = [m for m in self.client.texts if "دعوت دوستان" in m.get("text", "")]
        self.assertTrue(delivered, "no fallback delivery without buttons")


if __name__ == "__main__":
    unittest.main()
