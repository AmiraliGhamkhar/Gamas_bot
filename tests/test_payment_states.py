"""Explicit payment states: awaiting_payment -> pending_review -> approved/rejected/cancelled."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from gamas_bot.db import Database, MIGRATIONS_DIR
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

    async def test_review_requires_a_receipt(self):
        pid = await self.db.create_payment(100, "paid_5h_30d")
        self.assertFalse(await self.db.review_payment(pid, True, 1))
        self.assertFalse(await self.db.review_payment(pid, False, 1, reason="بدون رسید"))
        self.assertEqual(await self.status(pid), "awaiting_payment")
        self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_plan_units_expiry_and_existing_balance_are_preserved(self):
        await self.db.sync_plans([
            {"code": "paid_25h_30d", "hours": 25, "price": 150_000, "validity_days": 30},
            {"code": "paid_50h_30d", "hours": 50, "price": 250_000, "validity_days": 30},
        ])
        await self.db.grant_free_plan_once(100, 3600)
        pid = await self.db.create_payment(100, "paid_25h_30d")
        await self.db.set_payment_receipt(pid, "r.jpg", user_id=100)
        self.assertTrue(await self.db.review_payment(pid, True, 1))
        self.assertEqual(await self.db.available_seconds(100), 3600 + 25 * 3600)
        rows = await self.db.db.execute_fetchall(
            "SELECT seconds, created_at, expires_at, payment_id FROM ledger "
            "WHERE payment_id = ?",
            (pid,),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 90_000)
        self.assertEqual(rows[0][3], pid)
        lifetime = datetime.fromisoformat(rows[0][2]) - datetime.fromisoformat(rows[0][1])
        self.assertAlmostEqual(lifetime.total_seconds(), 30 * 86400, delta=2)

    async def test_activation_failure_rolls_back_and_can_be_retried(self):
        pid = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(pid, "r.jpg", user_id=100)
        await self.db._write(
            "CREATE TEMP TRIGGER fail_paid_credit BEFORE INSERT ON ledger "
            "WHEN NEW.payment_id IS NOT NULL "
            "BEGIN SELECT RAISE(ABORT, 'injected activation failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.review_payment(pid, True, 1)
        self.assertEqual(await self.status(pid), "pending_review")
        self.assertEqual(await self.db.available_seconds(100), 0)
        await self.db._write("DROP TRIGGER fail_paid_credit")
        self.assertTrue(await self.db.review_payment(pid, True, 1))
        self.assertFalse(await self.db.review_payment(pid, True, 1))
        self.assertEqual(await self.db.available_seconds(100), 18_000)

    async def test_database_constraint_rejects_a_second_credit_for_one_payment(self):
        pid = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(pid, "r.jpg", user_id=100)
        await self.db.review_payment(pid, True, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db._write(
                "INSERT INTO ledger(user_id, seconds, kind, note, created_at, payment_id) "
                "VALUES (100, 18000, 'credit', 'duplicate', '2026-01-01', ?)",
                (pid,),
            )
        self.assertEqual(await self.db.available_seconds(100), 18_000)

    async def test_history_and_credit_survive_restart(self):
        pid = await self.db.create_payment(100, "paid_5h_30d")
        await self.db.set_payment_receipt(pid, "r.jpg", user_id=100)
        await self.db.close()
        self.db = Database(self.path)
        await self.db.connect()
        self.assertEqual(await self.status(pid), "pending_review")
        self.assertEqual((await self.db.payment_history(100))[0]["payment_id"], pid)
        await self.db.review_payment(pid, True, 1)
        await self.db.close()
        self.db = Database(self.path)
        await self.db.connect()
        self.assertEqual(await self.status(pid), "approved")
        self.assertEqual(await self.db.available_seconds(100), 18_000)


class PaymentMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_entitlement_link_migration_preserves_existing_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration.name >= "006":
                    continue
                conn.executescript(migration.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations VALUES (?, '2026-01-01')",
                    (migration.name,),
                )
            conn.execute("INSERT INTO users(telegram_id, joined_at) VALUES (100, '2026-01-01')")
            conn.execute(
                "INSERT INTO plans(code, hours, price, validity_days) "
                "VALUES ('paid_5h_30d', 5, 50000, 30)"
            )
            conn.execute(
                "INSERT INTO payments(user_id, plan_code, status, receipt_path, created_at, "
                "purchased_seconds, purchased_price, purchased_validity_days) "
                "VALUES (100, 'paid_5h_30d', 'approved', 'old.jpg', '2026-01-01', "
                "18000, 50000, 30)"
            )
            conn.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, created_at) "
                "VALUES (100, 5000, 'credit', 'legacy balance', '2026-01-01')"
            )
            conn.commit()
            conn.close()

            db = Database(path)
            await db.connect()
            try:
                self.assertEqual(await db.available_seconds(100), 5000)
                self.assertEqual((await db.payment(1))["status"], "approved")
                rows = await db.db.execute_fetchall(
                    "SELECT seconds, payment_id FROM ledger WHERE note = 'legacy balance'"
                )
                self.assertEqual((rows[0][0], rows[0][1]), (5000, None))
                self.assertEqual(
                    (await db.db.execute_fetchall("SELECT COUNT(*) FROM payments"))[0][0],
                    1,
                )
            finally:
                await db.close()
