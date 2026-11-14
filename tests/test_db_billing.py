"""Tests for storage and the billing ledger invariants."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path

from gamas_bot.billing import plan_catalog
from gamas_bot.db import (
    Database,
    InsufficientBalanceError,
    OpenPaymentError,
    PaymentSnapshotError,
)


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
    async def test_learning_refund_and_recovery_inherit_contributing_expiry(self):
        await self.grant(100, 7200, days=30)
        original = (await self.db.db.execute_fetchall(
            "SELECT id, expires_at FROM ledger WHERE user_id=100 AND kind='credit'"
        ))[0]
        await self.db.spend_seconds(100, 3600, "learning", operation_id="first")
        self.assertTrue(await self.db.refund_learning_charge(100, "first"))
        refund = (await self.db.db.execute_fetchall(
            "SELECT seconds, expires_at, note FROM ledger WHERE note LIKE 'refund:%'"
        ))[0]
        self.assertEqual((refund[0], refund[1]), (3600, original[1]))
        self.assertIn("lots:" + str(original[0]), refund[2])
        self.assertEqual(await self.db.available_seconds(100), 7200)
        await self.db.spend_seconds(100, 3600, "learning", operation_id="second")
        self.assertEqual(await self.db.recover_pending_learning_charges(), 1)
        recovered = (await self.db.db.execute_fetchall(
            "SELECT expires_at FROM ledger WHERE note LIKE 'refund:%' ORDER BY id DESC LIMIT 1"
        ))[0]
        self.assertEqual(recovered[0], original[1])
        self.assertEqual(await self.db.available_seconds(100), 7200)

    async def test_refund_compares_imported_expiries_as_instants(self):
        await self.grant(100, 100, days=30)
        await self.grant(100, 100, days=30)
        credits = await self.db.db.execute_fetchall(
            "SELECT id FROM ledger WHERE user_id=100 AND kind='credit' ORDER BY id"
        )
        # The lexically later timestamp is earlier in UTC.
        await self.db._write("UPDATE ledger SET expires_at=? WHERE id=?",
                             ("2099-01-01T00:30:00+02:00", credits[0][0]))
        await self.db._write("UPDATE ledger SET expires_at=? WHERE id=?",
                             ("2098-12-31T23:00:00+00:00", credits[1][0]))
        await self.db.spend_seconds(100, 200, "learning", operation_id="mixed-offset")
        await self.db.refund_learning_charge(100, "mixed-offset")
        refund = await self.db.db.execute_fetchall(
            "SELECT expires_at FROM ledger WHERE note LIKE 'refund:%'"
        )
        self.assertEqual(refund[0][0], "2099-01-01T00:30:00+02:00")

    async def test_hold_retention_preserves_idempotency_and_delivery_state(self):
        await self.grant(100, 100)
        submission = await self.db.create_submission(100, "audio", "sample")
        await self.db.reserve(100, submission, 50)
        await self.db.release_hold(submission, "failed")
        await self.db._write("UPDATE holds SET released_at='2000-01-01' WHERE submission_id=?", (submission,))
        self.assertEqual(await self.db.expire_retained_data("holds", 30), 1)
        await self.db.settle_hold(submission, 50)
        row = (await self.db.db.execute_fetchall("SELECT delivered_at FROM submissions WHERE submission_id=?", (submission,)))[0]
        self.assertIsNone(row[0])
        with self.assertRaises(ValueError):
            await self.db.reserve(100, submission, 50)
        self.assertEqual(await self.db.available_seconds(100), 100)
        self.assertEqual(await self.db.db.execute_fetchall("PRAGMA foreign_key_check"), [])

    async def test_settlement_allocates_earliest_expiry_with_imported_offsets(self):
        await self.grant(100, 100, days=30)
        await self.grant(100, 100, days=30)
        credits = await self.db.db.execute_fetchall("SELECT id FROM ledger WHERE kind='credit' ORDER BY id")
        await self.db._write("UPDATE ledger SET expires_at=? WHERE id=?", ("2099-01-01T00:30:00+02:00", credits[0][0]))
        await self.db._write("UPDATE ledger SET expires_at=? WHERE id=?", ("2098-12-31T23:00:00+00:00", credits[1][0]))
        submission = await self.db.create_submission(100, "audio", "sample")
        await self.db.reserve(100, submission, 200)
        await self.db.settle_hold(submission, 100)
        allocations = await self.db.db.execute_fetchall("SELECT credit_id,seconds FROM debit_allocations")
        self.assertEqual([tuple(row) for row in allocations], [(credits[0][0], 100)])

    async def test_invalid_source_expiry_aborts_refund(self):
        await self.grant(100, 100, days=30)
        await self.db.spend_seconds(100, 100, "learning", operation_id="bad-expiry")
        await self.db._write("UPDATE ledger SET expires_at='invalid' WHERE kind='credit'")
        with self.assertRaisesRegex(ValueError, "invalid source credit expiry"):
            await self.db.refund_learning_charge(100, "bad-expiry")
        rows = await self.db.db.execute_fetchall(
            "SELECT status FROM learning_charges WHERE operation_id='bad-expiry'"
        )
        self.assertEqual(rows[0][0], "pending")
        self.assertEqual((await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE note LIKE 'refund:%'"
        ))[0][0], 0)

    async def test_generic_refund_requires_original_debit_and_inherits_expiry(self):
        await self.grant(100, 3600, days=30)
        await self.db.spend_seconds(100, 3600, "test")
        debit = (await self.db.db.execute_fetchall("SELECT id FROM ledger WHERE kind='debit'"))[0][0]
        with self.assertRaises(ValueError):
            await self.db.refund_seconds(100, 3600, "test")
        await self.db.refund_seconds(100, 3600, "test", debit_id=debit)
        rows = await self.db.db.execute_fetchall("SELECT expires_at FROM ledger WHERE kind='credit' ORDER BY id")
        self.assertEqual(rows[0][0], rows[1][0])
        self.assertEqual(await self.db.available_seconds(100), 3600)

    async def test_nonpositive_refund_is_logged(self):
        with self.assertLogs("gamas_bot.db.billing", level="WARNING") as logs:
            await self.db.refund_seconds(100, 0, "test")
        self.assertIn("user=100 seconds=0", logs.output[0])

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
        payment = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(payment, "receipt.jpg", user_id=100)
        self.assertTrue(await self.db.review_payment(payment, True, 1))
        self.assertEqual(await self.db.available_seconds(100), 5 * 3600)
        # A second review is rejected (exactly-once).
        self.assertFalse(await self.db.review_payment(payment, True, 1))
        self.assertEqual(await self.db.available_seconds(100), 5 * 3600)

    async def test_reject_payment_credits_nothing(self):
        payment = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(payment, "receipt.jpg", user_id=100)
        self.assertTrue(await self.db.review_payment(
            payment, False, 1, reason="رسید ناخوانا"
        ))
        self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_reject_requires_a_reason(self):
        payment = await self.db.create_payment(100, "paid_5h_30d")
        with self.assertRaises(ValueError):
            await self.db.review_payment(payment, False, 1)
        # Nothing was written.
        row = await self.db.payment(payment)
        assert row is not None
        self.assertIn(row["status"], ("pending", "awaiting_payment"))

    async def test_approval_uses_the_purchase_snapshot(self):
        """Deactivating or re-pricing the plan cannot change the credit (F-02/F-14)."""
        payment = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(payment, "receipt.jpg", user_id=100)
        await self.db.db.execute("UPDATE plans SET active = 0, hours = 1 WHERE code = ?",
                                 ("paid_5h_30d",))
        await self.db.db.commit()
        self.assertTrue(await self.db.review_payment(payment, True, 1))
        self.assertEqual(await self.db.available_seconds(100), 5 * 3600)
        row = await self.db.payment(payment)
        assert row is not None
        self.assertEqual(row["purchased_seconds"], 5 * 3600)

    async def test_approval_without_snapshot_is_refused(self):
        payment = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(payment, "receipt.jpg", user_id=100)
        await self.db.db.execute(
            "UPDATE payments SET purchased_seconds = NULL WHERE payment_id = ?",
            (payment,),
        )
        await self.db.db.commit()
        with self.assertRaises(PaymentSnapshotError):
            await self.db.review_payment(payment, True, 1)
        row = await self.db.payment(payment)
        assert row is not None
        self.assertEqual(row["status"], "pending_review")
        self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_second_tap_reuses_the_open_request(self):
        first = await self.db.create_payment(100, "paid_5h_30d")
        second = await self.db.create_payment(100, "paid_5h_30d")
        self.assertEqual(first, second)
        self.assertEqual(len(await self.db.pending_payments()), 1)

    async def test_a_second_plan_is_refused_while_one_is_open(self):
        await self.db.sync_plans([
            {"code": "paid_10h_30d", "hours": 10, "price": 75000, "validity_days": 30},
        ])
        await self.db.create_payment(100, "paid_5h_30d")
        with self.assertRaises(OpenPaymentError):
            await self.db.create_payment(100, "paid_10h_30d")

    async def test_review_and_audit_commit_together(self):
        payment = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(payment, "receipt.jpg", user_id=100)
        await self.db.review_payment(payment, True, 7)
        audit = await self.db.recent_audit(limit=1)
        self.assertEqual(audit[0]["action"], "payment_approved")
        self.assertEqual(audit[0]["admin_id"], 7)
        self.assertEqual(audit[0]["subject_user"], 100)

    async def test_revenue_uses_the_purchase_price(self):
        payment = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(payment, "receipt.jpg", user_id=100)
        await self.db.review_payment(payment, True, 1)
        await self.db.sync_plans([
            {"code": "paid_5h_30d", "hours": 5, "price": 90000, "validity_days": 30},
        ])
        stats = await self.db.stats()
        self.assertEqual(stats["toman_approved"], 50000)

    async def test_pending_payments_listing(self):
        await self.db.create_payment(100, "paid_5h_30d")
        pending = await self.db.pending_payments()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["user_id"], 100)
        self.assertEqual(pending[0]["purchased_seconds"], 5 * 3600)


class AtomicityTests(DatabaseTestCase):
    async def test_concurrent_free_grants_grant_once(self):
        results = await asyncio.gather(
            *(self.db.grant_free_plan_once(100, 3600) for _ in range(10))
        )
        self.assertEqual(results.count(True), 1)
        self.assertEqual(await self.db.available_seconds(100), 3600)

    async def test_release_hold_takes_the_write_lock(self):
        await self.grant(100, 3600)
        submission = await self.db.create_submission(100, "audio", "a.mp3")
        await self.db.reserve(100, submission, 3600)
        async with self.db._write_lock:
            task = asyncio.create_task(self.db.release_hold(submission, "test"))
            await asyncio.sleep(0.05)
            self.assertFalse(task.done(), "release_hold must wait for the lock")
        self.assertTrue(await task)
        self.assertEqual(await self.db.available_seconds(100), 3600)

    async def test_database_file_is_owner_only(self):
        await self.grant(100, 3600)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                self.assertEqual(stat.S_IMODE(os.stat(sidecar).st_mode), 0o600)


class PlanTests(DatabaseTestCase):
    @staticmethod
    def catalog(*, only: tuple[str, ...] | None = None) -> list[dict[str, object]]:
        return [
            {
                "code": plan.code,
                "hours": plan.included_seconds / 3600,
                "original_price": plan.original_price_toman,
                "price": plan.price_toman,
                "validity_days": plan.validity_days,
            }
            for plan in plan_catalog()
            if not plan.is_free and (only is None or plan.code in only)
        ]

    async def test_catalog_sync_uses_explicit_original_and_sale_prices(self):
        catalog = self.catalog()
        await self.db.sync_plans(catalog)
        plans = await self.db.plans()
        codes = {plan["code"] for plan in plans}
        self.assertEqual(len([plan for plan in plans if not plan["is_custom"]]), 7)
        self.assertIn("paid_30h_30d", codes)
        self.assertNotIn("paid_60h_30d", codes)
        self.assertNotIn("paid_50h_30d", codes)
        self.assertNotIn("free_1h", codes)  # the free plan is granted, not sold

        plan = await self.db.plan("paid_3h_30d")
        assert plan is not None
        self.assertEqual((plan["original_price"], plan["price"]), (160_000, 79_000))

    async def test_payment_snapshot_and_charge_keep_the_final_sale_price(self):
        await self.db.sync_plans(self.catalog(only=("paid_3h_30d",)))
        payment_id = await self.db.create_payment(100, "paid_3h_30d")
        payment = await self.db.payment(payment_id)
        assert payment is not None
        self.assertEqual(payment["purchased_price"], 79_000)
        self.assertEqual(payment["purchased_seconds"], 3 * 3600)

        # A later price change affects only future orders, not this snapshot.
        await self.db.sync_plans([
            {"code": "paid_3h_30d", "hours": 3, "original_price": 170_000,
             "price": 89_000, "validity_days": 30},
        ])
        unchanged = await self.db.payment(payment_id)
        assert unchanged is not None
        self.assertEqual(unchanged["purchased_price"], 79_000)
        self.assertEqual(unchanged["purchased_seconds"], 3 * 3600)

        await self.db.set_payment_receipt(payment_id, "receipt.jpg", user_id=100)
        self.assertTrue(await self.db.review_payment(payment_id, True, 1))
        self.assertEqual(await self.db.available_seconds(100), 3 * 3600)
        stats = await self.db.stats()
        self.assertEqual(stats["toman_approved"], 79_000)

    async def test_partial_sync_is_non_destructive_and_full_sync_retires_only_standard_rows(self):
        payment_id = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.db.execute(
            "INSERT INTO plans(code, hours, price, original_price, validity_days, is_custom, active) "
            "VALUES ('custom_7h', 7, 12000, 15000, 30, 1, 1)"
        )
        await self.db.db.commit()
        catalog = self.catalog()

        # The default partial sync never hides omitted standard or custom rows.
        await self.db.sync_plans(catalog)
        old = await self.db.plan("paid_5h_30d")
        self.assertIsNotNone(old)

        # Full startup sync deactivates omitted standard plans, but never deletes
        # or deactivates custom rows and never rewrites a historical payment.
        await self.db.sync_plans(catalog, full_sync=True)
        self.assertIsNone(await self.db.plan("paid_5h_30d"))
        custom = await self.db.plan("custom_7h")
        self.assertIsNotNone(custom)
        assert custom is not None
        self.assertEqual(custom["is_custom"], 1)
        row = await self.db.db.execute_fetchall(
            "SELECT active, is_custom FROM plans WHERE code = 'paid_5h_30d'"
        )
        self.assertEqual(tuple(row[0]), (0, 0))
        history = await self.db.payment(payment_id)
        assert history is not None
        self.assertEqual(history["status"], "awaiting_payment")
        self.assertEqual(history["purchased_price"], 50_000)
        self.assertEqual(history["purchased_seconds"], 5 * 3600)


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
