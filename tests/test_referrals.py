"""Referral attribution, idempotent rewards, and reporting tests."""

from __future__ import annotations

import asyncio

from gamas_bot.db import Database
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
