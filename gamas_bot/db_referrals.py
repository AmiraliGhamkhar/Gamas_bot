"""Invite attribution and append-only referral rewards (SRP).

Attribution happens once, on the referred user's first ``/start``. Rewards are
ordinary ledger credits with their own idempotency table, so a replayed approval
or a duplicate start can never pay twice. All writes run inside the caller's
``BEGIN IMMEDIATE`` transaction; this module never opens a nested one.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from typing import Any

from .db_core import DatabaseCore, _rows, iso

logger = logging.getLogger("gamas_bot.db.referrals")

#: Reward for a successful invite, in integer seconds (20 minutes).
REFERRAL_JOIN_SECONDS = 1_200
#: Reward for the invited user's first approved payment, in seconds (1 hour).
REFERRAL_PAYMENT_SECONDS = 3_600
REFERRAL_CODE_LENGTH = 8
_REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class ReferralsRepositoryMixin(DatabaseCore):
    """Attribution, idempotent rewards, and read-only referral reporting."""

    async def ensure_referral_code(self, user_id: int) -> str:
        """Return the user's stable invite code, creating it once."""
        rows = await _rows(
            self.read_db,
            "SELECT referral_code FROM users WHERE telegram_id = ?",
            (user_id,),
        )
        if rows and rows[0][0]:
            return str(rows[0][0])
        for _attempt in range(5):
            code = "".join(
                secrets.choice(_REFERRAL_ALPHABET)
                for _ in range(REFERRAL_CODE_LENGTH)
            )
            try:
                await self._write(
                    "UPDATE users SET referral_code = ? "
                    "WHERE telegram_id = ? AND referral_code IS NULL",
                    (code, user_id),
                )
            except sqlite3.IntegrityError:
                continue
            rows = await _rows(
                self.read_db,
                "SELECT referral_code FROM users WHERE telegram_id = ?",
                (user_id,),
            )
            if rows and rows[0][0]:
                return str(rows[0][0])
        raise RuntimeError("could not allocate a unique referral code")

    async def attribute_referral(
        self, referred_user_id: int, code: str
    ) -> tuple[str, int]:
        """Attribute a first start to *code*; return ``(status, seconds)``.

        Status is ``invalid``, ``self``, ``duplicate``, ``attributed`` (referrer
        is banned, so nothing is paid now) or ``rewarded``. The referral row and
        its credit commit together.
        """
        normalized = (code or "").strip().upper()
        if not normalized:
            return "invalid", 0
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT telegram_id, banned FROM users WHERE referral_code = ?",
                (normalized,),
            )
            if not rows:
                return "invalid", 0
            referrer_id = int(rows[0][0])
            referrer_banned = bool(rows[0][1])
            if referrer_id == int(referred_user_id):
                return "self", 0
            cursor = await self.db.execute(
                "INSERT OR IGNORE INTO referrals"
                "(referred_user_id, referrer_id, code, created_at) "
                "VALUES (?, ?, ?, ?)",
                (referred_user_id, referrer_id, normalized, iso(self._now())),
            )
            if cursor.rowcount != 1:
                return "duplicate", 0
            if referrer_banned:
                return "attributed", 0
            paid = await self._grant_referral(
                referrer_id,
                referred_user_id,
                kind="join",
                payment_id=0,
                seconds=REFERRAL_JOIN_SECONDS,
            )
        return ("rewarded", REFERRAL_JOIN_SECONDS) if paid else ("duplicate", 0)

    async def _grant_referral(
        self,
        referrer_id: int,
        referred_user_id: int,
        *,
        kind: str,
        payment_id: int,
        seconds: int,
    ) -> bool:
        """Insert one reward and its ledger credit; caller owns the transaction."""
        cursor = await self.db.execute(
            "INSERT OR IGNORE INTO referral_rewards"
            "(referrer_id, referred_user_id, kind, payment_id, seconds, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                referrer_id,
                referred_user_id,
                kind,
                int(payment_id),
                int(seconds),
                iso(self._now()),
            ),
        )
        if cursor.rowcount != 1:
            return False
        note = (
            f"referral:join:{referred_user_id}"
            if kind == "join"
            else f"referral:payment:{payment_id}"
        )
        await self.db.execute(
            "INSERT INTO ledger(user_id, seconds, kind, note, created_at) "
            "VALUES (?, ?, 'credit', ?, ?)",
            (referrer_id, int(seconds), note, iso(self._now())),
        )
        logger.info("referral reward granted kind=%s seconds=%s", kind, seconds)
        return True

    async def _credit_referral_for_payment(
        self, referred_user_id: int, payment_id: int
    ) -> bool:
        """Apply the one first-approved-payment decision in the caller's txn.

        ``review_payment`` has already changed this payment to approved. The
        surrounding BEGIN IMMEDIATE serializes approvals, so the first approval
        consumes qualification exactly once. If the referrer is banned then,
        the reward is forfeited, not deferred to a later purchase. Existing
        reward rows (including any historical duplicates) suppress new credits.
        """
        rows = await _rows(
            self.db,
            "SELECT r.referrer_id, u.banned, r.first_approved_payment_id, "
            "r.payment_reward_status FROM referrals r "
            "JOIN users u ON u.telegram_id = r.referrer_id "
            "WHERE r.referred_user_id = ?",
            (referred_user_id,),
        )
        if not rows:
            return False
        referrer_id = int(rows[0][0])
        if rows[0][2] is not None or rows[0][3] is not None:
            return False

        # No retroactive reward: if any legacy payment reward exists, it is the
        # authoritative record that this invite has already been paid.
        existing = await _rows(
            self.db,
            "SELECT payment_id FROM referral_rewards "
            "WHERE referred_user_id = ? AND kind = 'payment' "
            "ORDER BY created_at, id LIMIT 1",
            (referred_user_id,),
        )
        if existing:
            await self.db.execute(
                "UPDATE referrals SET first_approved_payment_id = ?, "
                "payment_reward_status = 'rewarded' WHERE referred_user_id = ? "
                "AND first_approved_payment_id IS NULL AND payment_reward_status IS NULL",
                (int(existing[0][0]), referred_user_id),
            )
            return False

        prior_approval = await _rows(
            self.db,
            "SELECT payment_id FROM payments "
            "WHERE user_id = ? AND status = 'approved' AND payment_id != ? "
            "ORDER BY COALESCE(reviewed_at, created_at), payment_id LIMIT 1",
            (referred_user_id, int(payment_id)),
        )
        # The current approval is already visible in this transaction. The
        # immediate writer lock means any other approved row is older in the
        # decision lifecycle, even if second-resolution timestamps tie.
        first_payment_id = (
            int(prior_approval[0][0]) if prior_approval else int(payment_id)
        )
        if prior_approval:
            status = "forfeited_prior_approval"
        elif bool(rows[0][1]):
            status = "forfeited_banned"
        else:
            paid = await self._grant_referral(
                referrer_id,
                referred_user_id,
                kind="payment",
                payment_id=payment_id,
                seconds=REFERRAL_PAYMENT_SECONDS,
            )
            status = "rewarded" if paid else None
            if not paid:
                return False

        await self.db.execute(
            "UPDATE referrals SET first_approved_payment_id = ?, "
            "payment_reward_status = ? WHERE referred_user_id = ? "
            "AND first_approved_payment_id IS NULL AND payment_reward_status IS NULL",
            (first_payment_id, status, referred_user_id),
        )
        return status == "rewarded"

    async def referral_summary(self, user_id: int) -> dict[str, int]:
        """Invited users and reward seconds for one referrer."""
        invited = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?",
            (user_id,),
        )
        rewarded = await _rows(
            self.read_db,
            "SELECT COALESCE(SUM(seconds), 0) FROM referral_rewards "
            "WHERE referrer_id = ?",
            (user_id,),
        )
        return {
            "invited": int(invited[0][0]) if invited else 0,
            "seconds": int(rewarded[0][0]) if rewarded else 0,
        }

    async def referral_totals(self) -> dict[str, int]:
        """Total rewarded invites and seconds across the bot."""
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*), COALESCE(SUM(seconds), 0) FROM referral_rewards",
        )
        return {
            "rewards": int(rows[0][0]) if rows else 0,
            "seconds": int(rows[0][1]) if rows else 0,
        }

    async def top_referrers(self, limit: int = 10) -> list[dict[str, Any]]:
        """Read-only leaderboard for the administrator referral report."""
        rows = await _rows(
            self.read_db,
            "WITH invite_counts AS ("
            "  SELECT referrer_id, COUNT(*) AS invited FROM referrals GROUP BY referrer_id"
            "), reward_totals AS ("
            "  SELECT referrer_id, SUM(seconds) AS seconds FROM referral_rewards "
            "  GROUP BY referrer_id"
            ") "
            "SELECT i.referrer_id, i.invited, COALESCE(w.seconds, 0) AS seconds "
            "FROM invite_counts i LEFT JOIN reward_totals w "
            "ON w.referrer_id = i.referrer_id "
            "ORDER BY i.invited DESC, seconds DESC, i.referrer_id ASC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [
            {
                "referrer_id": int(row[0]),
                "invited": int(row[1]),
                "seconds": int(row[2]),
            }
            for row in rows
        ]
