"""Phase 3: reward redemption must move real credits, atomically (audit D-05).

The behaviour under test is narrow and literal: a redemption either writes *both*
a reward debit and a credit lot in ``ledger`` in one committed transaction, or it
writes nothing and says so. A success response is never allowed to be the only
evidence that credits were granted, a reward balance can never go negative, and
retrying a request can never spend twice.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gamas_bot.db import Database
from gamas_bot.rewards import REWARD_CATALOG, reward_cost_points
from gamas_bot.webapp_api import make_wsgi_app
from tests.support import make_settings
from tests.test_webapp_api import BOT_TOKEN, _call_wsgi, _sign_init_data

USER = 900
OTHER = 901


async def _award(db: Database, user_id: int, points: int, *, start: int = 0) -> None:
    """Award points the way Explore does: +10 rows with a unique ``ref_id``."""
    entries = points // 10
    for index in range(start, start + entries):
        await db.db.execute(
            "INSERT INTO reward_points_ledger(user_id, delta, reason, ref_id, created_at) "
            "VALUES(?, 10, 'explore_correct_answer', ?, '2026-01-01T00:00:00+00:00')",
            (user_id, f"explore_q:{user_id}:{index}"),
        )
    await db.db.commit()


async def _count(db: Database, sql: str, params: tuple = ()) -> int:
    rows = await db.db.execute_fetchall(sql, params)
    return int(rows[0][0])


class RewardRedemptionRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN,
            db_path=root / "rewards.db",
            temp_dir=root / "tmp",
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(USER, "Redeemer")
        await self.db.ensure_user(OTHER, "Other")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------- success path
    async def test_success_writes_reward_debit_and_real_credit_lot(self) -> None:
        await _award(self.db, USER, 60)
        result = await self.db.rewards.redeem(USER, "credit_5m")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["credit_seconds_granted"], 300)
        self.assertTrue(result["credited"])
        self.assertEqual(result["reward_points"], 10)
        redemption_id = result["redemption_id"]
        ledger_id = result["ledger_id"]
        self.assertIsInstance(ledger_id, int)
        self.assertGreater(ledger_id or 0, 0)

        # The credit is a normal ledger row, which is what the pipelines consume.
        lots = await self.db.db.execute_fetchall(
            "SELECT id, seconds, kind, note, expires_at FROM ledger WHERE user_id = ?",
            (USER,),
        )
        self.assertEqual(len(lots), 1)
        self.assertEqual(int(lots[0]["id"]), ledger_id)
        self.assertEqual(int(lots[0]["seconds"]), 300)
        self.assertEqual(str(lots[0]["kind"]), "credit")
        self.assertEqual(str(lots[0]["note"]), "redeem:credit_5m")
        self.assertIsNone(lots[0]["expires_at"])

        # ...and the debit points at the redemption that produced it.
        debits = await self.db.db.execute_fetchall(
            "SELECT delta, reason, ref_id FROM reward_points_ledger "
            "WHERE user_id = ? AND delta < 0",
            (USER,),
        )
        self.assertEqual(len(debits), 1)
        self.assertEqual(int(debits[0]["delta"]), -50)
        self.assertEqual(str(debits[0]["reason"]), "redeem:credit_5m")
        self.assertEqual(str(debits[0]["ref_id"]), f"redemption:{redemption_id}")

        # One row ties the two halves together.
        rows = await self.db.db.execute_fetchall(
            "SELECT reward_code, cost_points, credit_seconds, ledger_id "
            "FROM reward_redemptions WHERE redemption_id = ?",
            (redemption_id,),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["reward_code"]), "credit_5m")
        self.assertEqual(int(rows[0]["cost_points"]), 50)
        self.assertEqual(int(rows[0]["credit_seconds"]), 300)
        self.assertEqual(int(rows[0]["ledger_id"]), ledger_id)

        self.assertEqual(int(await self.db.available_seconds(USER)), 300)

    async def test_granted_credit_is_consumable_by_the_existing_billing_path(self) -> None:
        """The grant is real seconds, not a parallel wallet (no second system)."""
        await _award(self.db, USER, 60)
        granted = await self.db.rewards.redeem(USER, "credit_5m")

        self.assertTrue(await self.db.spend_seconds(USER, 120, "quiz"))
        self.assertEqual(int(await self.db.available_seconds(USER)), 180)
        # FIFO allocation consumed the redeemed lot, exactly like a paid one.
        allocated = await _count(
            self.db,
            "SELECT COALESCE(SUM(seconds), 0) FROM debit_allocations WHERE credit_id = ?",
            (int(granted["ledger_id"]),),
        )
        self.assertEqual(allocated, 120)

    async def test_cosmetic_reward_never_claims_a_credit_grant(self) -> None:
        await _award(self.db, USER, 100)
        result = await self.db.rewards.redeem(USER, "theme_ocean")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["credit_seconds_granted"], 0)
        self.assertFalse(result["credited"])
        self.assertIsNone(result["ledger_id"])
        self.assertEqual(result["reward_points"], 70)
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 0)

    # ------------------------------------------------------------ refusal paths
    async def test_insufficient_points_changes_nothing(self) -> None:
        await _award(self.db, USER, 40)
        result = await self.db.rewards.redeem(USER, "credit_5m")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "insufficient_points")
        self.assertEqual(result["reward_points"], 40)
        self.assertEqual(
            await _count(self.db, "SELECT COUNT(*) FROM reward_redemptions"), 0
        )
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 0)
        self.assertEqual(
            await self.db.study_packs.reward_points_balance(USER), 40
        )

    async def test_unknown_or_empty_code_is_refused(self) -> None:
        await _award(self.db, USER, 100)
        for code in ("no_such_reward", "", "   "):
            result = await self.db.rewards.redeem(USER, code)
            self.assertFalse(result["ok"], code)
            self.assertEqual(result["error"], "unknown_reward")
        self.assertEqual(
            await _count(self.db, "SELECT COUNT(*) FROM reward_redemptions"), 0
        )
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 0)

    async def test_cost_rules_reject_non_positive_amounts(self) -> None:
        with self.assertRaises(ValueError):
            reward_cost_points({"code": "x", "cost_points": 0})
        with self.assertRaises(ValueError):
            reward_cost_points({"code": "x", "cost_points": -10})
        # Every shipped entry has an integer cost and a non-negative integer grant.
        for item in REWARD_CATALOG:
            self.assertIsInstance(item["cost_points"], int)
            self.assertGreater(item["cost_points"], 0)
            self.assertIsInstance(item["credit_seconds"], int)
            self.assertGreaterEqual(item["credit_seconds"], 0)

    async def test_usage_limit_uses_existing_max_per_user_rule(self) -> None:
        await _award(self.db, USER, 500)
        for _ in range(5):
            self.assertTrue((await self.db.rewards.redeem(USER, "credit_5m"))["ok"])
        refused = await self.db.rewards.redeem(USER, "credit_5m")

        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], "usage_limit")
        self.assertEqual(refused["reward_points"], 250)
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 5)
        self.assertEqual(
            await _count(self.db, "SELECT COUNT(*) FROM reward_redemptions"), 5
        )

    async def test_legacy_redemptions_still_count_toward_the_limit(self) -> None:
        """Debits written before 041 (``ref_id IS NULL``) keep their uses."""
        await _award(self.db, USER, 500)
        for _ in range(5):
            await self.db.db.execute(
                "INSERT INTO reward_points_ledger(user_id, delta, reason, ref_id, created_at) "
                "VALUES(?, -50, 'redeem:credit_5m', NULL, '2025-12-01T00:00:00+00:00')",
                (USER,),
            )
        await self.db.db.commit()

        result = await self.db.rewards.redeem(USER, "credit_5m")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "usage_limit")
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 0)

    # ------------------------------------------------------ duplicates & retries
    async def test_repeat_request_with_same_key_is_idempotent(self) -> None:
        await _award(self.db, USER, 120)
        first = await self.db.rewards.redeem(USER, "credit_5m", idempotency_key="req-1")
        second = await self.db.rewards.redeem(USER, "credit_5m", idempotency_key="req-1")

        self.assertTrue(first["ok"] and second["ok"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["redemption_id"], second["redemption_id"])
        self.assertEqual(first["ledger_id"], second["ledger_id"])
        self.assertEqual(second["credit_seconds_granted"], 300)
        # Spent once: 120 - 50, and exactly one credit lot exists.
        self.assertEqual(second["reward_points"], 70)
        self.assertEqual(
            await self.db.study_packs.reward_points_balance(USER), 70
        )
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 1)
        self.assertEqual(
            await _count(self.db, "SELECT COUNT(*) FROM reward_redemptions"), 1
        )
        self.assertEqual(int(await self.db.available_seconds(USER)), 300)

    async def test_idempotency_key_is_scoped_to_the_user(self) -> None:
        await _award(self.db, USER, 60)
        await _award(self.db, OTHER, 60)
        mine = await self.db.rewards.redeem(USER, "credit_5m", idempotency_key="shared")
        theirs = await self.db.rewards.redeem(OTHER, "credit_5m", idempotency_key="shared")

        self.assertTrue(mine["ok"] and theirs["ok"])
        self.assertNotEqual(mine["redemption_id"], theirs["redemption_id"])
        self.assertFalse(theirs["idempotent"])
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 2)

    async def test_duplicate_without_key_spends_each_time_but_never_negative(self) -> None:
        await _award(self.db, USER, 60)
        first = await self.db.rewards.redeem(USER, "credit_5m")
        second = await self.db.rewards.redeem(USER, "credit_5m")

        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "insufficient_points")
        self.assertEqual(await self.db.study_packs.reward_points_balance(USER), 10)
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 1)

    async def test_concurrent_attempts_cannot_overspend(self) -> None:
        await _award(self.db, USER, 60)
        results = await asyncio.gather(
            *(
                self.db.rewards.redeem(USER, "credit_5m", idempotency_key=f"k{i}")
                for i in range(4)
            )
        )
        succeeded = [r for r in results if r["ok"]]
        self.assertEqual(len(succeeded), 1, results)
        self.assertEqual(
            {r["error"] for r in results if not r["ok"]}, {"insufficient_points"}
        )
        self.assertEqual(await self.db.study_packs.reward_points_balance(USER), 10)
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 1)
        self.assertEqual(
            await _count(self.db, "SELECT COUNT(*) FROM reward_redemptions"), 1
        )
        self.assertEqual(int(await self.db.available_seconds(USER)), 300)

    # ------------------------------------------------------------- rollback path
    async def test_failed_credit_grant_rolls_back_the_whole_redemption(self) -> None:
        await _award(self.db, USER, 60)

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("ledger unavailable")

        with mock.patch.object(type(self.db), "_add_credit_locked", _boom):
            with self.assertRaises(RuntimeError):
                await self.db.rewards.redeem(USER, "credit_5m")

        self.assertEqual(await self.db.study_packs.reward_points_balance(USER), 60)
        self.assertEqual(
            await _count(self.db, "SELECT COUNT(*) FROM reward_redemptions"), 0
        )
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 0)
        self.assertFalse(
            await _count(
                self.db, "SELECT COUNT(*) FROM reward_points_ledger WHERE delta < 0"
            )
        )
        self.assertEqual(int(await self.db.available_seconds(USER)), 0)

        # The same request succeeds once the grant works again: nothing is stuck.
        self.assertTrue((await self.db.rewards.redeem(USER, "credit_5m"))["ok"])
        self.assertEqual(int(await self.db.available_seconds(USER)), 300)

    async def test_grant_that_writes_no_ledger_row_is_not_reported_as_success(self) -> None:
        await _award(self.db, USER, 60)

        async def _no_row(*_args, **_kwargs):
            return 0

        with mock.patch.object(type(self.db), "_add_credit_locked", _no_row):
            with self.assertRaises(RuntimeError):
                await self.db.rewards.redeem(USER, "credit_5m")

        self.assertEqual(await self.db.study_packs.reward_points_balance(USER), 60)
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 0)
        self.assertEqual(
            await _count(self.db, "SELECT COUNT(*) FROM reward_redemptions"), 0
        )

    async def test_balance_can_never_be_committed_negative(self) -> None:
        """Even if the pre-check were bypassed, the post-check aborts the commit."""
        await _award(self.db, USER, 60)
        repo = self.db.rewards
        real = type(repo).reward_points_balance
        calls = {"n": 0}

        async def _dropping_balance(self, user_id):  # noqa: ANN001 - test double
            calls["n"] += 1
            # First call (the pre-check) sees the real balance; the second one
            # (the confirmation) pretends the debit over-drew the account.
            return 60 if calls["n"] == 1 else -5

        with mock.patch.object(type(repo), "reward_points_balance", _dropping_balance):
            with self.assertRaises(RuntimeError):
                await repo.redeem(USER, "credit_5m")

        self.assertEqual(await real(repo, USER), 60)
        self.assertEqual(await _count(self.db, "SELECT COUNT(*) FROM ledger"), 0)
        self.assertEqual(
            await _count(self.db, "SELECT COUNT(*) FROM reward_redemptions"), 0
        )


class RewardRedemptionApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN,
            db_path=root / "rewards_api.db",
            temp_dir=root / "tmp",
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(101, "Alice")
        await self.db.close()
        self.app = make_wsgi_app(self.settings)
        self.alice = {"X-Telegram-Init-Data": _sign_init_data(101)}

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _award(self, points: int) -> None:
        db = Database(self.settings.db_path)
        await db.connect()
        try:
            await _award(db, 101, points)
        finally:
            await db.close()

    async def _db_counts(self) -> tuple[int, int, int]:
        db = Database(self.settings.db_path)
        await db.connect()
        try:
            return (
                await _count(db, "SELECT COUNT(*) FROM ledger"),
                await _count(db, "SELECT COUNT(*) FROM reward_points_ledger WHERE delta < 0"),
                await _count(db, "SELECT COUNT(*) FROM reward_redemptions"),
            )
        finally:
            await db.close()

    async def _redeem(self, payload: dict, *, headers: dict | None = None):
        return await asyncio.to_thread(
            _call_wsgi,
            self.app,
            "POST",
            "/api/v1/rewards/redeem",
            headers=self.alice if headers is None else headers,
            body=json.dumps(payload).encode(),
        )

    async def test_success_response_reports_only_what_was_committed(self) -> None:
        await self._award(60)
        status, body = await self._redeem({"reward_code": "credit_5m"})

        self.assertTrue(status.startswith("200"), body)
        payload = json.loads(body)
        self.assertEqual(payload["balance"], 10)
        self.assertEqual(payload["credit_seconds_granted"], 300)
        self.assertTrue(payload["credited"])
        self.assertEqual(payload["available_seconds"], 300)
        self.assertEqual(payload["reward_code"], "credit_5m")
        self.assertFalse(payload["idempotent"])
        self.assertIsInstance(payload["redemption_id"], int)
        self.assertIsInstance(payload["ledger_id"], int)
        self.assertEqual(await self._db_counts(), (1, 1, 1))

        # The Mini App can refresh from the authoritative account endpoint.
        status, body = await asyncio.to_thread(
            _call_wsgi, self.app, "GET", "/api/v1/me", headers=self.alice
        )
        self.assertTrue(status.startswith("200"))
        me = json.loads(body)
        self.assertEqual(me["credit_seconds"], 300)
        self.assertEqual(me["reward_points"], 10)

        status, body = await asyncio.to_thread(
            _call_wsgi, self.app, "GET", "/api/v1/me/credits", headers=self.alice
        )
        self.assertEqual(json.loads(body)["credit_seconds"], 300)

    async def test_insufficient_points_returns_400_and_writes_nothing(self) -> None:
        status, body = await self._redeem({"reward_code": "credit_5m"})

        self.assertTrue(status.startswith("400"), body)
        self.assertEqual(json.loads(body)["error"], "insufficient points")
        self.assertEqual(await self._db_counts(), (0, 0, 0))

    async def test_invalid_payload_and_unknown_code_are_refused(self) -> None:
        await self._award(100)
        for payload in ({}, {"reward_code": ""}, {"reward_code": "nope"}):
            status, _ = await self._redeem(payload)
            self.assertTrue(status.startswith("400"), payload)
        status, _ = await self._redeem({"reward_code": "credit_5m", "idempotency_key": 5})
        self.assertTrue(status.startswith("400"))
        self.assertEqual(await self._db_counts(), (0, 0, 0))

    async def test_client_supplied_balances_are_ignored(self) -> None:
        status, body = await self._redeem(
            {"reward_code": "credit_5m", "balance": 10_000, "reward_points": 10_000}
        )
        self.assertTrue(status.startswith("400"), body)
        self.assertEqual(await self._db_counts(), (0, 0, 0))

    async def test_retry_with_same_key_returns_the_original_redemption(self) -> None:
        await self._award(60)
        first_status, first_body = await self._redeem(
            {"reward_code": "credit_5m", "idempotency_key": "client-uuid-1"}
        )
        second_status, second_body = await self._redeem(
            {"reward_code": "credit_5m", "idempotency_key": "client-uuid-1"}
        )

        self.assertTrue(first_status.startswith("200"), first_body)
        self.assertTrue(second_status.startswith("200"), second_body)
        first, second = json.loads(first_body), json.loads(second_body)
        self.assertEqual(first["redemption_id"], second["redemption_id"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["credit_seconds_granted"], 300)
        self.assertEqual(second["balance"], 10)
        self.assertEqual(await self._db_counts(), (1, 1, 1))

    async def test_idempotency_key_header_is_accepted(self) -> None:
        await self._award(60)
        headers = dict(self.alice, **{"Idempotency-Key": "header-key"})
        status, body = await self._redeem({"reward_code": "credit_5m"}, headers=headers)
        self.assertTrue(status.startswith("200"), body)
        status, body = await self._redeem({"reward_code": "credit_5m"}, headers=headers)
        self.assertTrue(status.startswith("200"), body)
        self.assertTrue(json.loads(body)["idempotent"])
        self.assertEqual(await self._db_counts(), (1, 1, 1))

    async def test_redeem_requires_authentication(self) -> None:
        status, _ = await self._redeem({"reward_code": "credit_5m"}, headers={})
        self.assertTrue(status.startswith("401"), status)
        self.assertEqual(await self._db_counts(), (0, 0, 0))

    async def test_catalog_advertises_the_server_owned_prices(self) -> None:
        status, body = await asyncio.to_thread(
            _call_wsgi, self.app, "GET", "/api/v1/rewards/catalog", headers=self.alice
        )
        self.assertTrue(status.startswith("200"))
        catalog = {item["code"]: item for item in json.loads(body)["catalog"]}
        self.assertEqual(catalog["credit_5m"]["credit_seconds"], 300)
        self.assertEqual(catalog["credit_5m"]["cost_points"], 50)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
