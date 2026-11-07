"""Randomized ledger/cache invariants over real SQLite transactions."""
from __future__ import annotations

import random
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gamas_bot.db import Database
from gamas_bot.db_core import iso


class MoneyInvariantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.db = Database(Path(self.tmp.name) / "money.sqlite", clock=lambda: self.now)
        await self.db.connect()
        for user in range(100, 150):
            await self.db.ensure_user(user)
            await self.db.add_credit(user, 5000, note="seed", validity_days=30)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def assert_balance(self, user):
        rows = await self.db.db.execute_fetchall(
            "SELECT COALESCE(SUM(MAX(0,l.seconds "
            "- COALESCE((SELECT SUM(seconds) FROM debit_allocations d WHERE d.credit_id=l.id),0) "
            "- COALESCE((SELECT SUM(a.seconds) FROM hold_allocations a JOIN holds h "
            "ON h.submission_id=a.submission_id WHERE a.credit_id=l.id AND h.released_at IS NULL),0))),0) "
            "FROM ledger l WHERE l.user_id=? AND l.kind='credit' "
            "AND julianday(l.created_at)<=julianday(?) "
            "AND (l.expires_at IS NULL OR julianday(l.expires_at)>julianday(?))",
            (user, iso(self.now), iso(self.now)),
        )
        actual = await self.db.available_seconds(user)
        self.assertEqual(actual, rows[0][0])
        cached = await self.db.db.execute_fetchall(
            "SELECT available_seconds FROM user_balances WHERE user_id=?", (user,)
        )
        self.assertEqual(cached[0][0], actual)

    async def test_randomized_ledger_and_materialized_balances(self):
        rng = random.Random(91643)
        holds = []
        pending = []
        for step in range(200):
            user = rng.randrange(100, 150)
            operation = rng.randrange(6)
            if operation == 0:
                await self.db.add_credit(user, rng.randrange(1, 1000), note="grant", validity_days=rng.choice((1, 3, None)))
            elif operation == 1:
                available = await self.db.available_seconds(user)
                if available:
                    claim = f"operation-{step}"
                    amount = min(available, rng.randrange(1, 500))
                    await self.db.spend_seconds(user, amount, "learning", operation_id=claim)
                    pending.append((user, claim))
            elif operation == 2 and pending:
                index = rng.randrange(len(pending))
                owner, claim = pending.pop(index)
                await self.db.refund_learning_charge(owner, claim)
                await self.assert_balance(owner)
            elif operation == 3:
                available = await self.db.available_seconds(user)
                if available:
                    sid = await self.db.create_submission(user, "audio", "sample")
                    amount = min(available, rng.randrange(1, 500))
                    await self.db.reserve(user, sid, amount)
                    holds.append((user, sid, amount))
            elif operation in (4, 5) and holds:
                owner, sid, amount = holds.pop(rng.randrange(len(holds)))
                if operation == 4:
                    await self.db.release_hold(sid, "test")
                else:
                    await self.db.settle_hold(sid, rng.randrange(amount + 1))
                await self.assert_balance(owner)
            if step in (70, 140):
                self.now += timedelta(days=2)
            await self.assert_balance(user)
        for user in range(100, 150):
            await self.assert_balance(user)
        listed = {row["telegram_id"]: row["available_seconds"] for row in await self.db.list_users(limit=100)}
        for user in range(100, 150):
            self.assertEqual(listed[user], await self.db.available_seconds(user))

    async def test_offline_import_dirty_rows_are_repaired_at_startup(self):
        path = self.db.path
        await self.db.close()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO ledger(user_id,seconds,kind,note,created_at) VALUES (100,700,'credit','import',?)", (iso(self.now),))
        conn.commit()
        conn.close()
        await self.db.connect()
        self.assertEqual(await self.db.available_seconds(100), 5700)
        self.assertEqual((await self.db.db.execute_fetchall("SELECT COUNT(*) FROM balance_dirty"))[0][0], 0)

    async def test_startup_detects_and_repairs_cache_corruption(self):
        from gamas_bot.metrics import METRICS
        before = METRICS.snapshot()["balance_repairs"]
        await self.db._write("UPDATE user_balances SET available_seconds=1 WHERE user_id=100")
        await self.db.close()
        await self.db.connect()
        self.assertEqual(await self.db.available_seconds(100), 5000)
        self.assertGreater(METRICS.snapshot()["balance_repairs"], before)

    async def test_clock_moves_forward_and_back_across_expiry(self):
        user = 100
        await self.db.add_credit(user, 100, note="short", validity_days=1)
        await self.assert_balance(user)
        self.now += timedelta(days=2)
        await self.assert_balance(user)
        self.assertEqual(await self.db.available_seconds(user), 5000)
        self.now -= timedelta(days=2)
        await self.assert_balance(user)
        self.assertEqual(await self.db.available_seconds(user), 5100)
