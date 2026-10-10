"""The admin credit screen has to show the balance billing actually spends.

Two defects met on this one screen. The free/paid split was a Python sum over
the *newest 50* entitlement rows, so a user whose history outgrew that window
read as owning less credit than a job could spend — and the row that expired
tomorrow counted even though it had not started. The two lists were then sliced
again by the panel (``[:10]`` / ``[:8]``), so a user's entitlement history and
spend ledger looked complete while rows were left out with no word about it.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gamas_bot.bot import StudyBot
from gamas_bot.database import Database
from gamas_bot.progress import to_persian_digits


def _stamp(day_offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=day_offset)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class CreditOverviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "credit.sqlite3")
        await self.db.open()
        self.user = await self.db.upsert_user(101, "buyer")
        self.user_id = int(self.user["id"])
        plan = await self.db.create_plan(
            code="deep12h",
            name="طرح آزمون",
            hours=12,
            price_toman=1000,
            validity_days=30,
            admin_id=7,
        )
        self.plan_id = int(plan["id"])

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp.cleanup()

    async def _insert(
        self,
        *,
        remaining: int,
        count: int = 1,
        status: str = "active",
        starts_day: int = 0,
        expires_day: int | None = 30,
        source: str = "admin_credit",  # 'payment' would need a payment_id (CHECK)
    ) -> None:
        async with self.db._transaction(immediate=True) as db:
            for _ in range(count):
                await db.execute(
                    "INSERT INTO entitlements (user_id, plan_id, granted_seconds, "
                    "remaining_seconds, starts_at, expires_at, status, source, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, datetime(?), ?, ?, ?, datetime(?), datetime(?))",
                    (
                        self.user_id,
                        self.plan_id,
                        remaining + 1_000,
                        remaining,
                        _stamp(starts_day),
                        _stamp(expires_day) if expires_day is not None else None,
                        status,
                        source,
                        _stamp(starts_day),
                        _stamp(starts_day),
                    ),
                )

    async def test_old_spendable_credit_survives_deep_history(self) -> None:
        base = await self.db.admin_user_credit_overview(101)
        await self._insert(remaining=900)
        # Fifty-five exhausted, newer rows: the grant above is now outside any
        # "newest 50" window, which is exactly where the old code lost it.
        await self._insert(remaining=0, count=55, status="expired")
        overview = await self.db.admin_user_credit_overview(101)
        self.assertEqual(overview["paid_seconds"], 900)
        self.assertEqual(overview["free_seconds"], base["free_seconds"])
        self.assertEqual(
            overview["available_seconds"], base["available_seconds"] + 900
        )
        balance = await self.db.user_balance(self.user_id)
        self.assertEqual(overview["available_seconds"], balance["available_seconds"])

    async def test_not_yet_started_credit_is_not_reported_as_usable(self) -> None:
        base = await self.db.admin_user_credit_overview(101)
        await self._insert(remaining=600, starts_day=3)
        overview = await self.db.admin_user_credit_overview(101)
        self.assertEqual(overview["available_seconds"], base["available_seconds"])
        balance = await self.db.user_balance(self.user_id)
        self.assertEqual(overview["available_seconds"], balance["available_seconds"])

    async def test_expired_rows_are_excluded_from_the_split(self) -> None:
        # The row is listed by the screen (history is history) but must not be
        # counted as spendable: the aggregate uses the same expiry predicate the
        # reservation engine uses, not just ``status == 'active'``.
        base = await self.db.admin_user_credit_overview(101)
        await self._insert(remaining=700, status="active", expires_day=-1)
        overview = await self.db.admin_user_credit_overview(101)
        self.assertEqual(overview["available_seconds"], base["available_seconds"])

    async def test_history_lists_are_windows_and_say_so(self) -> None:
        await self._insert(remaining=60, count=25)
        overview = await self.db.admin_user_credit_overview(101)
        self.assertEqual(len(overview["entitlements"]), 10)
        self.assertGreater(overview["entitlement_total"], len(overview["entitlements"]))
        text = StudyBot._admin_credit_text(overview)
        self.assertIn(
            "اعتبارها (جدیدترین) — ۱۰ مورد از "
            f"{to_persian_digits(overview['entitlement_total'])} سابقه",
            text,
        )

    async def test_short_history_is_not_labelled_as_a_window(self) -> None:
        overview = await self.db.admin_user_credit_overview(101)
        self.assertLessEqual(overview["entitlement_total"], len(overview["entitlements"]))
        self.assertNotIn("مورد از", StudyBot._admin_credit_text(overview))

    async def test_spend_ledger_window_is_counted(self) -> None:
        async with self.db._transaction(immediate=True) as db:
            for _ in range(15):
                await db.execute(
                    "INSERT INTO usage_ledger (user_id, event_type, requested_seconds, "
                    "consumed_seconds, created_at) VALUES (?, 'consume', 60, 60, datetime(?))",
                    (self.user_id, _stamp(0)),
                )
        overview = await self.db.admin_user_credit_overview(101)
        self.assertEqual(len(overview["usage"]), 8)
        self.assertGreater(overview["usage_total"], len(overview["usage"]))
        text = StudyBot._admin_credit_text(overview)
        self.assertIn(
            f"آخرین رویدادهای مصرف (ثانیه) — ۸ مورد از "
            f"{to_persian_digits(overview['usage_total'])} رویداد",
            text,
        )

    async def test_display_window_size_is_the_callers_choice(self) -> None:
        await self._insert(remaining=60, count=25)
        overview = await self.db.admin_user_credit_overview(101, recent_rows=20, usage_rows=2)
        self.assertEqual(len(overview["entitlements"]), 20)
        self.assertLessEqual(len(overview["usage"]), 2)
        # The counts describe the table, not the window that was asked for.
        self.assertGreater(overview["entitlement_total"], 20)


if __name__ == "__main__":
    unittest.main()
