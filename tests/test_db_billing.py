"""Tests for storage and the billing ledger invariants."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from gamas_bot.billing import plan_catalog
from gamas_bot.db import Database, InsufficientBalanceError


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        self.db = Database(self.path)
        await self.db.connect()
        await self.db.ensure_user(100)
        await self.db.sync_plans([
            {"code": "paid_5h_30d", "hours": 5, "price": 50000, "validity_days": 30},
        ])

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def grant(self, user_id: int, seconds: int, days=None):
        await self.db.add_credit(user_id, seconds, note="test", validity_days=days)


class LedgerTests(DatabaseTestCase):
    async def test_free_plan_granted_once(self):
        self.assertTrue(await self.db.grant_free_plan_once(100, 3600))
        self.assertFalse(await self.db.grant_free_plan_once(100, 3600))
        self.assertEqual(await self.db.available_seconds(100), 3600)

    async def test_reserve_then_settle_charges_measured(self):
        await self.grant(100, 3600)
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        await self.db.reserve(100, submission, 3600)
        # Held credit is not available.
        self.assertEqual(await self.db.available_seconds(100), 0)
        await self.db.settle_hold(submission, 1800)
        self.assertEqual(await self.db.available_seconds(100), 1800)

    async def test_reserve_then_release_returns_everything(self):
        await self.grant(100, 3600)
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        await self.db.reserve(100, submission, 3600)
        self.assertTrue(await self.db.release_hold(submission, "failed"))
        self.assertEqual(await self.db.available_seconds(100), 3600)
        # A second release is a no-op (exactly-once).
        self.assertFalse(await self.db.release_hold(submission, "failed again"))

    async def test_settle_never_exceeds_reservation(self):
        await self.grant(100, 100)
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        await self.db.reserve(100, submission, 100)
        await self.db.settle_hold(submission, 999)
        self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_insufficient_balance_rejected(self):
        await self.grant(100, 60)
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        with self.assertRaises(InsufficientBalanceError) as ctx:
            await self.db.reserve(100, submission, 120)
        self.assertEqual(ctx.exception.available_seconds, 60)
        self.assertEqual(ctx.exception.required_seconds, 120)

    async def test_expired_credit_not_available(self):
        await self.grant(100, 3600, days=None)   # no expiry
        await self.grant(100, 7200, days=-1)     # already expired
        self.assertEqual(await self.db.available_seconds(100), 3600)

    async def test_concurrent_reservations_are_serialized(self):
        """Two jobs competing for the same credit: one wins, one is denied."""
        await self.grant(100, 100)
        results = []

        async def try_reserve(submission_index: int) -> bool:
            submission = await self.db.create_submission(100, "audio", f"a{submission_index}")
            try:
                await self.db.reserve(100, submission, 60)
                return True
            except InsufficientBalanceError:
                return False

        results = await asyncio.gather(try_reserve(1), try_reserve(2), try_reserve(3))
        self.assertEqual(sorted(results), [False, False, True])
        self.assertEqual(await self.db.available_seconds(100), 40)


class RecoveryTests(DatabaseTestCase):
    async def test_interrupted_submissions_failed_and_released(self):
        await self.grant(100, 3600)
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        await self.db.reserve(100, submission, 3600)
        await self.db.set_submission_status(submission, "processing")

        recovered = await self.db.recover_interrupted()
        self.assertEqual(recovered, 1)
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "failed")
        self.assertEqual(await self.db.available_seconds(100), 3600)
        # Idempotent: nothing left to recover.
        self.assertEqual(await self.db.recover_interrupted(), 0)

    async def test_done_submissions_untouched(self):
        await self.grant(100, 3600)
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        await self.db.reserve(100, submission, 600)
        await self.db.settle_hold(submission, 600)
        await self.db.set_submission_status(submission, "done")
        self.assertEqual(await self.db.recover_interrupted(), 0)
        self.assertEqual(await self.db.available_seconds(100), 3000)


class UnlimitedUserTests(DatabaseTestCase):
    """Special (unlimited) users: never billed, usage still recorded."""

    async def debit_rows(self, user_id: int) -> int:
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE user_id = ? AND kind = 'debit'", (user_id,)
        )
        return int(rows[0][0])

    async def hold_rows(self, user_id: int) -> int:
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM holds WHERE user_id = ?", (user_id,)
        )
        return int(rows[0][0])

    async def test_new_users_are_not_special(self):
        self.assertFalse(await self.db.is_unlimited(100))
        self.assertFalse(await self.db.is_unlimited(999_999))  # unknown user

    async def test_special_user_reserve_is_never_denied(self):
        await self.db.set_unlimited(100, True, admin_id=1, reason="استاد")
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        # No credit at all, and a reservation far beyond any balance.
        await self.db.reserve(100, submission, 10 * 3600)
        self.assertEqual(await self.hold_rows(100), 0)
        self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_special_user_settle_writes_no_debit(self):
        await self.db.set_unlimited(100, True, admin_id=1, reason="استاد")
        await self.grant(100, 3600)
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        await self.db.reserve(100, submission, 3600)
        await self.db.settle_hold(submission, 1800)
        self.assertEqual(await self.debit_rows(100), 0)
        self.assertEqual(await self.db.available_seconds(100), 3600)

    async def test_job_started_before_grant_is_released_not_charged(self):
        await self.grant(100, 3600)
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        await self.db.reserve(100, submission, 3600)
        await self.db.set_unlimited(100, True, admin_id=1, reason="استاد")
        await self.db.settle_hold(submission, 1800)
        self.assertEqual(await self.debit_rows(100), 0)
        # The held credit came back in full.
        self.assertEqual(await self.db.available_seconds(100), 3600)

    async def test_settle_without_hold_is_quiet_for_special_user(self):
        await self.db.set_unlimited(100, True, admin_id=1, reason="استاد")
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        with self.assertNoLogs("gamas_bot.db", level="WARNING"):
            await self.db.settle_hold(submission, 600)
        self.assertEqual(await self.debit_rows(100), 0)

    async def test_turning_off_restores_billing(self):
        await self.db.set_unlimited(100, True, admin_id=1, reason="استاد")
        await self.db.set_unlimited(100, False, admin_id=1)
        self.assertFalse(await self.db.is_unlimited(100))
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        with self.assertRaises(InsufficientBalanceError):
            await self.db.reserve(100, submission, 60)

    async def test_grant_is_audited_with_reason_and_provenance(self):
        await self.db.set_unlimited(100, True, admin_id=7, reason="  استاد درس  ")
        rows = await self.db.db.execute_fetchall(
            "SELECT unlimited_reason, unlimited_granted_by, unlimited_granted_at "
            "FROM users WHERE telegram_id = 100"
        )
        self.assertEqual(rows[0][0], "  استاد درس  ")
        self.assertEqual(rows[0][1], 7)
        self.assertTrue(rows[0][2])
        audit = await self.db.recent_audit(limit=1)
        self.assertEqual(audit[0]["action"], "unlimited_on")
        self.assertEqual(audit[0]["admin_id"], 7)
        self.assertEqual(audit[0]["subject_user"], 100)

    async def test_revoke_is_audited_and_clears_provenance(self):
        await self.db.set_unlimited(100, True, admin_id=7, reason="استاد")
        await self.db.set_unlimited(100, False, admin_id=8)
        rows = await self.db.db.execute_fetchall(
            "SELECT unlimited_reason, unlimited_granted_by FROM users WHERE telegram_id = 100"
        )
        self.assertIsNone(rows[0][0])
        self.assertIsNone(rows[0][1])
        audit = await self.db.recent_audit(limit=1)
        self.assertEqual(audit[0]["action"], "unlimited_off")
        self.assertEqual(audit[0]["admin_id"], 8)

    async def test_special_users_listing_and_user_flag(self):
        await self.db.ensure_user(101)
        await self.db.set_unlimited(100, True, admin_id=1, reason="استاد")
        listed = await self.db.unlimited_users()
        self.assertEqual([row["telegram_id"] for row in listed], [100])
        flags = {u["telegram_id"]: u["is_unlimited"] for u in await self.db.list_users()}
        self.assertEqual(flags, {100: True, 101: False})


class PaymentTests(DatabaseTestCase):
    async def test_approve_payment_credits_once(self):
        await self.db.sync_plans([
            {"code": "paid_5h_30d", "hours": 5, "price": 50000, "validity_days": 30},
        ])
        payment = await self.db.create_payment(100, "paid_5h_30d")
        self.assertTrue(await self.db.review_payment(
            payment, True, 1, plan_hours=5, plan_validity_days=30
        ))
        self.assertEqual(await self.db.available_seconds(100), 5 * 3600)
        # A second review is rejected (exactly-once).
        self.assertFalse(await self.db.review_payment(
            payment, True, 1, plan_hours=5, plan_validity_days=30
        ))
        self.assertEqual(await self.db.available_seconds(100), 5 * 3600)

    async def test_reject_payment_credits_nothing(self):
        payment = await self.db.create_payment(100, "paid_5h_30d")
        self.assertTrue(await self.db.review_payment(
            payment, False, 1, plan_hours=5, plan_validity_days=30
        ))
        self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_pending_payments_listing(self):
        await self.db.create_payment(100, "paid_5h_30d")
        pending = await self.db.pending_payments()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["user_id"], 100)


class PlanTests(DatabaseTestCase):
    async def test_catalog_sync_and_custom_protection(self):
        catalog = [
            {"code": plan.code, "hours": plan.included_seconds / 3600,
             "price": plan.price_toman, "validity_days": plan.validity_days}
            for plan in plan_catalog() if not plan.is_free
        ]
        await self.db.sync_plans(catalog)
        plans = await self.db.plans()
        codes = {plan["code"] for plan in plans}
        self.assertIn("paid_5h_30d", codes)
        self.assertIn("paid_50h_30d", codes)
        self.assertNotIn("free_1h", codes)  # the free plan is granted, not sold

        # Re-sync updates the price.
        await self.db.sync_plans([
            {"code": "paid_5h_30d", "hours": 5, "price": 60000, "validity_days": 30},
        ])
        plan = await self.db.plan("paid_5h_30d")
        assert plan is not None
        self.assertEqual(plan["price"], 60000)


class SubmissionTests(DatabaseTestCase):
    async def test_submission_lifecycle(self):
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        await self.db.set_submission_status(submission, "processing")
        await self.db.save_transcription(submission, "deepgram", "متن", "{}")
        await self.db.set_submission_status(
            submission, "done", duration_seconds=120.5
        )
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")
        self.assertEqual(row.duration_seconds, 120.5)
        self.assertIsNotNone(row.finished_at)

    async def test_user_submissions_ordered_newest_first(self):
        for index in range(3):
            await self.db.create_submission(100, "audio", f"a{index}.mp3")
        rows = await self.db.user_submissions(100)
        self.assertEqual(len(rows), 3)

    async def test_stats(self):
        await self.grant(100, 3600)
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        await self.db.reserve(100, submission, 60)
        await self.db.settle_hold(submission, 60)
        await self.db.set_submission_status(submission, "done", duration_seconds=60)
        stats = await self.db.stats()
        self.assertEqual(stats["users"], 1)
        self.assertEqual(stats["submissions_done"], 1)
        self.assertEqual(stats["seconds_transcribed"], 60.0)


if __name__ == "__main__":
    unittest.main()
