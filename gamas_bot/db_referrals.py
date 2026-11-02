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
        """Pay the referrer once, inside the caller's approval transaction."""
        rows = await _rows(
            self.db,
            "SELECT r.referrer_id FROM referrals r "
            "JOIN users u ON u.telegram_id = r.referrer_id "
            "WHERE r.referred_user_id = ? AND u.banned = 0",
            (referred_user_id,),
        )
        if not rows:
            return False
        return await self._grant_referral(
            int(rows[0][0]),
            referred_user_id,
            kind="payment",
            payment_id=payment_id,
            seconds=REFERRAL_PAYMENT_SECONDS,
        )

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
            "SELECT r.referrer_id, COUNT(DISTINCT r.referred_user_id) AS invited, "
            "COALESCE(SUM(w.seconds), 0) AS seconds "
            "FROM referrals r "
            "LEFT JOIN referral_rewards w ON w.referrer_id = r.referrer_id "
            "GROUP BY r.referrer_id "
            "ORDER BY invited DESC, seconds DESC, r.referrer_id ASC LIMIT ?",
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
