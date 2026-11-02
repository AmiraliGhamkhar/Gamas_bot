"""Referral attribution, idempotent rewards, and reporting tests."""

from __future__ import annotations

from gamas_bot.db_referrals import REFERRAL_JOIN_SECONDS, REFERRAL_PAYMENT_SECONDS
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
        rows = await self.db.top_referrers(5)
        self.assertEqual(rows[0]["referrer_id"], 300)
        self.assertEqual(rows[0]["invited"], 2)
        self.assertEqual(rows[1]["referrer_id"], 100)
        self.assertEqual(rows[1]["invited"], 1)
