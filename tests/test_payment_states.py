"""Explicit payment states: awaiting_payment -> pending_review -> approved/rejected/cancelled."""

from __future__ import annotations

import asyncio

from tests.test_db_billing import DatabaseTestCase


class PaymentStateTests(DatabaseTestCase):
    async def status(self, payment_id: int) -> str:
        return (await self.db.payment(payment_id))["status"]

    async def test_receipt_moves_awaiting_payment_to_pending_review(self):
        pid = await self.db.create_payment(100, "paid_5h_30d")
        self.assertEqual(await self.status(pid), "awaiting_payment")
        self.assertTrue(await self.db.set_payment_receipt(pid, "r.jpg"))
        self.assertEqual(await self.status(pid), "pending_review")
        # Receipt submission alone grants nothing.
        self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_pending_review_is_listed_for_the_admin(self):
        pid = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(pid, "r.jpg")
        self.assertEqual([p["payment_id"] for p in await self.db.pending_payments()], [pid])
        self.assertEqual([p["payment_id"] for p in await self.db.pending_review_payments()], [pid])
        self.assertEqual(await self.db.awaiting_payments(), [])

    async def test_approval_from_pending_review_credits_once(self):
        pid = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(pid, "r.jpg")
        results = await asyncio.gather(
            self.db.review_payment(pid, True, 1), self.db.review_payment(pid, True, 1)
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(await self.status(pid), "approved")
        self.assertEqual(await self.db.available_seconds(100), 5 * 3600)

    async def test_rejection_from_pending_review_grants_nothing(self):
        pid = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(pid, "r.jpg")
        self.assertTrue(await self.db.review_payment(pid, False, 1, reason="مبلغ نامطابق"))
        self.assertEqual(await self.status(pid), "rejected")
        self.assertEqual(await self.db.available_seconds(100), 0)
        # A rejected request cannot later be approved.
        self.assertFalse(await self.db.review_payment(pid, True, 1))

    async def test_cancel_only_before_receipt(self):
        pid = await self.db.create_payment(100, "paid_5h_30d")
        self.assertTrue(await self.db.cancel_payment(pid, 100))
        self.assertEqual(await self.status(pid), "cancelled")
        self.assertEqual(await self.db.available_seconds(100), 0)

        pid2 = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(pid2, "r.jpg")
        self.assertFalse(await self.db.cancel_payment(pid2, 100))
        self.assertEqual(await self.status(pid2), "pending_review")
