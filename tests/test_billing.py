from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from gamas_bot.billing import (
    FREE_PLAN_SECONDS,
    PLAN_25_SECONDS,
    PLAN_50_SECONDS,
    plan_catalog,
)
from gamas_bot.database import Database, split_sql_statements


def canonical_plan_records():
    return [plan.as_record() for plan in plan_catalog()]


class BillingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "billing.sqlite3")
        await self.db.open()
        await self.db.sync_plan_catalog(canonical_plan_records())

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _new_user(self, telegram_id: int = 101) -> dict:
        return await self.db.upsert_user(telegram_id, "student")

    async def _request_with_receipt(self, user_id: int, plan_code: str) -> dict:
        request = await self.db.create_payment_request(user_id, plan_code)
        accepted = await self.db.submit_payment_receipt(
            int(request["id"]), user_id, "/private/receipt.jpg", 12345
        )
        self.assertTrue(accepted)
        return request

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits are not available")
    async def test_sqlite_database_and_sidecars_are_owner_only(self):
        for path in (
            self.db.path,
            Path(f"{self.db.path}-wal"),
            Path(f"{self.db.path}-shm"),
        ):
            if path.exists():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)

    async def test_catalog_uses_the_canonical_integer_seconds_and_prices(self):
        records = {row.code: row for row in plan_catalog()}
        self.assertEqual(records["free_lifetime_1h"].included_seconds, 3_600)
        self.assertEqual(records["paid_25h_30d"].included_seconds, 90_000)
        self.assertEqual(records["paid_25h_30d"].price_toman, 150_000)
        self.assertEqual(records["paid_25h_30d"].validity_days, 30)
        self.assertEqual(records["paid_50h_30d"].included_seconds, 180_000)
        self.assertEqual(records["paid_50h_30d"].price_toman, 250_000)
        self.assertEqual(records["paid_50h_30d"].validity_days, 30)

    async def test_zero_second_reservations_are_rejected(self):
        user = await self._new_user()
        submission = await self.db.create_submission(
            int(user["id"]), "zero", 0, "empty.wav", "audio/wav"
        )
        with self.assertRaisesRegex(ValueError, "positive"):
            await self.db.reserve_usage(int(user["id"]), submission, 0)
        self.assertEqual(
            (await self.db.user_balance(int(user["id"])))["available_seconds"],
            FREE_PLAN_SECONDS,
        )
        self.assertIsNone(await self.db.usage_reservation(submission))

    async def test_lifetime_free_grant_is_awarded_once_and_start_does_not_renew_it(self):
        user = await self._new_user()
        initial = await self.db.user_balance(int(user["id"]))
        self.assertEqual(initial["available_seconds"], FREE_PLAN_SECONDS)
        self.assertEqual(len(initial["entitlements"]), 1)
        self.assertIsNone(initial["entitlements"][0]["expires_at"])

        submission = await self.db.create_submission(
            int(user["id"]), "file-free", 3_600, "lecture.wav", "audio/wav"
        )
        reserved = await self.db.reserve_usage(int(user["id"]), submission, 3_600)
        self.assertTrue(reserved["ok"])
        self.assertTrue(await self.db.finalize_usage(submission, 3_600))
        await self.db.upsert_user(int(user["telegram_id"]), "student")
        after_start = await self.db.user_balance(int(user["id"]))
        self.assertEqual(after_start["available_seconds"], 0)
        self.assertEqual(len(after_start["entitlements"]), 1)
        grants = [row for row in await self.db.usage_ledger(int(user["id"])) if row["event_type"] == "grant"]
        self.assertEqual(len(grants), 1)

    async def test_existing_users_receive_free_entitlement_when_migration_003_runs(self):
        await self.db.close()
        legacy_path = self.root / "legacy.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        try:
            for migration_name in ("001_initial.sql", "002_presentations.sql"):
                migration_path = Path(__file__).resolve().parents[1] / "migrations" / migration_name
                for statement in split_sql_statements(migration_path.read_text(encoding="utf-8")):
                    legacy.execute(statement)
            legacy.execute(
                "INSERT INTO users(telegram_id, username, first_seen) VALUES (?, ?, ?)",
                (909, "existing", "2026-01-01T00:00:00+00:00"),
            )
            legacy.commit()
        finally:
            legacy.close()

        upgraded = Database(legacy_path)
        await upgraded.open()
        try:
            await upgraded.sync_plan_catalog(canonical_plan_records())
            user = await upgraded.get_user(909)
            self.assertIsNotNone(user)
            balance = await upgraded.user_balance(int(user["id"]))
            self.assertEqual(balance["available_seconds"], FREE_PLAN_SECONDS)
            self.assertEqual(len(balance["entitlements"]), 1)
            await upgraded.upsert_user(909, "existing")
            self.assertEqual(
                (await upgraded.user_balance(int(user["id"])))["available_seconds"],
                FREE_PLAN_SECONDS,
            )
        finally:
            await upgraded.close()
            # Restore the fixture database for asyncTearDown.
            self.db = Database(self.root / "billing.sqlite3")
            await self.db.open()
            await self.db.sync_plan_catalog(canonical_plan_records())

    async def test_receipt_submission_grants_nothing_and_approval_is_idempotent(self):
        user = await self._new_user()
        request = await self._request_with_receipt(int(user["id"]), "paid_25h_30d")
        self.assertEqual((await self.db.user_balance(int(user["id"]))) ["available_seconds"], 3_600)

        approval = await self.db.approve_payment(int(request["id"]), admin_id=700)
        self.assertIsNotNone(approval)
        self.assertEqual(approval["granted_seconds"], PLAN_25_SECONDS)
        self.assertIsNone(await self.db.approve_payment(int(request["id"]), admin_id=701))
        balance = await self.db.user_balance(int(user["id"]))
        self.assertEqual(balance["available_seconds"], 3_600 + PLAN_25_SECONDS)
        self.assertEqual(len(balance["entitlements"]), 2)
        paid = next(row for row in balance["entitlements"] if row["source"] == "payment")
        self.assertEqual(paid["remaining_seconds"], PLAN_25_SECONDS)
        self.assertEqual(paid["plan_code"], "paid_25h_30d")

    async def test_receipt_retention_starts_after_manual_review_not_submission(self):
        user = await self._new_user()
        request = await self._request_with_receipt(int(user["id"]), "paid_25h_30d")
        await self.db._db().execute(
            "UPDATE payment_requests SET receipt_submitted_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", request["id"]),
        )
        await self.db._db().commit()

        # Pending receipts stay available for human review even if an operator
        # has not examined them for longer than the configured retention window.
        self.assertEqual(
            await self.db.receipt_cleanup_candidates("2020-01-01T00:00:00+00:00"),
            [],
        )
        await self.db.approve_payment(int(request["id"]), admin_id=700)
        self.assertEqual(
            await self.db.receipt_cleanup_candidates("2020-01-01T00:00:00+00:00"),
            [],
        )
        candidates = await self.db.receipt_cleanup_candidates("2100-01-01T00:00:00+00:00")
        self.assertEqual([row["id"] for row in candidates], [request["id"]])

    async def test_paid_entitlements_accumulate_and_earliest_expiry_is_consumed_first(self):
        user = await self._new_user()
        first = await self._request_with_receipt(int(user["id"]), "paid_25h_30d")
        await self.db.approve_payment(int(first["id"]), admin_id=700)
        second = await self._request_with_receipt(int(user["id"]), "paid_50h_30d")
        await self.db.approve_payment(int(second["id"]), admin_id=700)

        balance = await self.db.user_balance(int(user["id"]))
        self.assertEqual(
            balance["available_seconds"],
            FREE_PLAN_SECONDS + PLAN_25_SECONDS + PLAN_50_SECONDS,
        )
        self.assertEqual(
            [row["source"] for row in balance["entitlements"]],
            ["payment", "payment", "free_lifetime"],
        )
        submission = await self.db.create_submission(
            int(user["id"]), "file-paid", 90_001, "paid.wav", "audio/wav"
        )
        reservation = await self.db.reserve_usage(int(user["id"]), submission, 90_001)
        self.assertTrue(reservation["ok"])
        ledger = await self.db.usage_ledger(int(user["id"]))
        allocations = [row for row in ledger if row["event_type"] == "reserve"]
        self.assertEqual(
            [row["entitlement_source"] for row in allocations[:2]],
            ["payment", "payment"],
        )
        self.assertEqual(
            sum(int(row["reserved_seconds"]) for row in allocations[:2]),
            PLAN_25_SECONDS + 1,
        )

    async def test_rejection_and_cancellation_never_grant_credit(self):
        user = await self._new_user()
        request = await self._request_with_receipt(int(user["id"]), "paid_50h_30d")
        self.assertTrue(await self.db.reject_payment(int(request["id"]), 700, "رسید نامعتبر"))
        self.assertFalse(await self.db.reject_payment(int(request["id"]), 701, "تکرار"))
        self.assertEqual((await self.db.user_balance(int(user["id"]))) ["available_seconds"], FREE_PLAN_SECONDS)

        next_request = await self.db.create_payment_request(int(user["id"]), "paid_25h_30d")
        self.assertTrue(await self.db.cancel_payment_intent(int(next_request["id"]), int(user["id"])))
        self.assertEqual((await self.db.user_balance(int(user["id"]))) ["available_seconds"], FREE_PLAN_SECONDS)

    async def test_finalize_refunds_unused_seconds_and_failure_release_is_idempotent(self):
        user = await self._new_user()
        partial = await self.db.create_submission(int(user["id"]), "p1", 100, "a.wav", "audio/wav")
        self.assertTrue((await self.db.reserve_usage(int(user["id"]), partial, 100))["ok"])
        self.assertTrue(await self.db.finalize_usage(partial, 60))
        reservation = await self.db.usage_reservation(partial)
        self.assertEqual(reservation["status"], "partially_consumed")
        self.assertEqual(reservation["consumed_seconds"], 60)
        self.assertEqual(reservation["released_seconds"], 40)
        self.assertEqual((await self.db.user_balance(int(user["id"]))) ["available_seconds"], 3_540)

        failed = await self.db.create_submission(int(user["id"]), "p2", 500, "b.wav", "audio/wav")
        self.assertTrue((await self.db.reserve_usage(int(user["id"]), failed, 500))["ok"])
        self.assertEqual(await self.db.release_usage(failed, "STT failed"), 500)
        self.assertEqual(await self.db.release_usage(failed, "duplicate cleanup"), 0)
        self.assertEqual((await self.db.usage_reservation(failed))["status"], "released")
        self.assertEqual((await self.db.user_balance(int(user["id"]))) ["available_seconds"], 3_540)

    async def test_insufficient_balance_is_audited_without_overspending(self):
        user = await self._new_user()
        submission = await self.db.create_submission(int(user["id"]), "large", 3_601, "large.wav", "audio/wav")
        result = await self.db.reserve_usage(int(user["id"]), submission, 3_601)
        self.assertFalse(result["ok"])
        self.assertEqual(result["available_seconds"], FREE_PLAN_SECONDS)
        self.assertEqual((await self.db.usage_reservation(submission))["status"], "insufficient")
        ledger = await self.db.usage_ledger(int(user["id"]))
        self.assertTrue(any(row["event_type"] == "denied" for row in ledger))
        self.assertEqual((await self.db.user_balance(int(user["id"]))) ["available_seconds"], FREE_PLAN_SECONDS)

    async def test_two_database_connections_cannot_reserve_the_same_credit_twice(self):
        user = await self._new_user()
        first_submission = await self.db.create_submission(int(user["id"]), "one", 3_600, "one.wav", "audio/wav")
        second_submission = await self.db.create_submission(int(user["id"]), "two", 3_600, "two.wav", "audio/wav")
        second_db = Database(self.root / "billing.sqlite3")
        await second_db.open()
        try:
            first, second = await asyncio.gather(
                self.db.reserve_usage(int(user["id"]), first_submission, 3_600),
                second_db.reserve_usage(int(user["id"]), second_submission, 3_600),
            )
        finally:
            await second_db.close()
        self.assertEqual(sum(bool(result["ok"]) for result in (first, second)), 1)
        self.assertEqual((await self.db.user_balance(int(user["id"]))) ["available_seconds"], 0)
        reservations = [
            await self.db.usage_reservation(first_submission),
            await self.db.usage_reservation(second_submission),
        ]
        self.assertEqual(sum(row["status"] == "reserved" for row in reservations), 1)
        self.assertEqual(sum(row["status"] == "insufficient" for row in reservations), 1)


if __name__ == "__main__":
    unittest.main()
