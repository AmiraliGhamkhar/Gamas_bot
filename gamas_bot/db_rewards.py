"""Reward persistence slice: transactional point→credit redemption.

One redemption is one database transaction, because the two halves are only
meaningful together:

* a **reward debit** (``reward_points_ledger``) that spends the user's points,
* a **credit grant** (``ledger``, ``kind='credit'``) that the processing
  pipelines actually consume — the same rows that payments and admin grants
  write, allocated FIFO by the existing billing code, never a parallel wallet.

The redemption row is created first so the debit and the grant both reference a
single identity, which is also what makes a retry idempotent: the same
``idempotency_key`` returns the original result instead of spending twice.

Nothing here reports success on its own. The caller receives either an ``ok``
result that was confirmed *inside* the committed transaction, or an error, and a
failure anywhere rolls the whole thing back (``DatabaseCore._transaction``).
"""

from __future__ import annotations

from typing import Any

from .db_core import _rows, iso
from .rewards import (
    reward_cost_points,
    reward_credit_seconds,
    reward_is_expired,
    reward_item,
)

#: Reasons a redemption can be refused. The API maps them to status codes; the
#: domain never invents a success.
ERROR_UNKNOWN_REWARD = "unknown_reward"
ERROR_REWARD_EXPIRED = "reward_expired"
ERROR_USAGE_LIMIT = "usage_limit"
ERROR_INSUFFICIENT_POINTS = "insufficient_points"

_MAX_IDEMPOTENCY_KEY = 120


class RewardRepository:
    """Single-responsibility persistence slice for reward redemption."""

    def __init__(self, core: Any) -> None:
        self._core = core

    async def reward_points_balance(self, user_id: int) -> int:
        """Authoritative point balance (the sum of the append-only ledger)."""
        return await self._core.study_packs.reward_points_balance(int(user_id))

    async def redeem(
        self,
        user_id: int,
        code: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Spend points for credits in one atomic, retry-safe transaction.

        The returned mapping always carries ``ok``; when it is ``True`` the
        transaction that produced it has already written both the reward debit
        and (for a credit-bearing entry) the credit ledger row, and the amounts
        reported are the ones that were actually written.
        """
        uid = int(user_id)
        item = reward_item(code)
        if item is None or not item.get("enabled", True):
            return {"ok": False, "error": ERROR_UNKNOWN_REWARD}
        now_iso = iso(self._core._now())
        if reward_is_expired(item, now_iso=now_iso):
            return {"ok": False, "error": ERROR_REWARD_EXPIRED}

        reward_code = str(item["code"])
        cost = reward_cost_points(item)
        grant = reward_credit_seconds(item)
        key = self._normalize_key(idempotency_key)

        async with self._core._transaction():
            if key is not None:
                # A retry of the same request must not spend a second time.
                existing = await _rows(
                    self._core.db,
                    "SELECT redemption_id, reward_code, cost_points, credit_seconds, "
                    "ledger_id, created_at FROM reward_redemptions "
                    "WHERE user_id = ? AND idempotency_key = ?",
                    (uid, key),
                )
                if existing:
                    return await self._replay(uid, existing[0], key=key)

            used = await self._redemption_count_locked(uid, reward_code)
            max_per_user = item.get("max_per_user")
            if max_per_user is not None and used >= int(max_per_user):
                return {
                    "ok": False,
                    "error": ERROR_USAGE_LIMIT,
                    "reward_points": await self.reward_points_balance(uid),
                }

            balance = await self.reward_points_balance(uid)
            if balance < cost:
                return {
                    "ok": False,
                    "error": ERROR_INSUFFICIENT_POINTS,
                    "reward_points": balance,
                }

            cursor = await self._core.db.execute(
                "INSERT INTO reward_redemptions(user_id, reward_code, cost_points, "
                "credit_seconds, idempotency_key, ledger_id, created_at) "
                "VALUES(?, ?, ?, ?, ?, NULL, ?)",
                (uid, reward_code, cost, grant, key, now_iso),
            )
            redemption_id = int(cursor.lastrowid or 0)
            await self._core.db.execute(
                "INSERT INTO reward_points_ledger(user_id, delta, reason, ref_id, "
                "created_at) VALUES(?, ?, ?, ?, ?)",
                (
                    uid,
                    -cost,
                    f"redeem:{reward_code}",
                    f"redemption:{redemption_id}",
                    now_iso,
                ),
            )
            ledger_id: int | None = None
            if grant > 0:
                ledger_id = await self._core._add_credit_locked(
                    uid,
                    grant,
                    note=f"redeem:{reward_code}",
                )
                if ledger_id <= 0:
                    # Never hand back a success that no ledger row backs.
                    raise RuntimeError("credit grant produced no ledger row")
                await self._core.db.execute(
                    "UPDATE reward_redemptions SET ledger_id = ? WHERE redemption_id = ?",
                    (ledger_id, redemption_id),
                )

            balance_after = await self.reward_points_balance(uid)
            if balance_after < 0:
                # Belt and braces: the guard above runs under the write lock, and
                # a negative balance must never be committed.
                raise RuntimeError("reward balance would become negative")
            return {
                "ok": True,
                "reward_code": reward_code,
                "cost_points": cost,
                "credit_seconds_granted": grant,
                "credited": grant > 0,
                "ledger_id": ledger_id,
                "redemption_id": redemption_id,
                "reward_points": balance_after,
                "idempotency_key": key,
                "idempotent": False,
            }

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _normalize_key(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text[:_MAX_IDEMPOTENCY_KEY]

    async def _redemption_count_locked(self, user_id: int, reward_code: str) -> int:
        """How many times this user already took this reward.

        Redemptions recorded by the current schema count once each; point debits
        written by the earlier implementation (``ref_id IS NULL``) still count, so
        an upgrade cannot hand a user extra uses of a capped reward.
        """
        rows = await _rows(
            self._core.db,
            "SELECT (SELECT COUNT(*) FROM reward_redemptions "
            "        WHERE user_id = ? AND reward_code = ?) "
            "     + (SELECT COUNT(*) FROM reward_points_ledger "
            "        WHERE user_id = ? AND reason = ? AND ref_id IS NULL) AS used",
            (int(user_id), str(reward_code), int(user_id), f"redeem:{reward_code}"),
        )
        return int(rows[0]["used"]) if rows else 0

    async def _replay(
        self, user_id: int, row: Any, *, key: str | None
    ) -> dict[str, Any]:
        """Return the result of an already-committed redemption, unchanged."""
        credit_seconds = int(row["credit_seconds"] or 0)
        ledger_id = row["ledger_id"]
        return {
            "ok": True,
            "reward_code": str(row["reward_code"]),
            "cost_points": int(row["cost_points"]),
            "credit_seconds_granted": credit_seconds,
            "credited": credit_seconds > 0 and ledger_id is not None,
            "ledger_id": int(ledger_id) if ledger_id is not None else None,
            "redemption_id": int(row["redemption_id"]),
            "reward_points": await self.reward_points_balance(int(user_id)),
            "idempotency_key": key,
            "idempotent": True,
        }
