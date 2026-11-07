"""Append-only second-granularity ledger, FIFO credit lots, and hold lifecycle (SRP / B1–B9)."""

from __future__ import annotations

from datetime import timedelta
import logging
import sqlite3
from typing import Any

import aiosqlite

from .metrics import METRICS
from .db_core import (
    DatabaseCore,
    InsufficientBalanceError,
    UserBannedError,
    _rows,
    iso,
)

logger = logging.getLogger("gamas_bot.db.billing")


class BillingRepositoryMixin(DatabaseCore):
    """Single-responsibility persistence slice for billing ledger, lots, and holds."""

    async def _credit_lots(
        self,
        user_id: int,
        at: str,
        before_id: int | None = None,
        *,
        conn: aiosqlite.Connection | None = None,
    ) -> list[sqlite3.Row]:
        handle = conn if conn is not None else self.db
        return await _rows(
            handle,
            "SELECT l.id, l.seconds "
            "- COALESCE((SELECT SUM(a.seconds) FROM debit_allocations a "
            "WHERE a.credit_id = l.id), 0) "
            "- COALESCE((SELECT SUM(a.seconds) FROM hold_allocations a "
            "JOIN holds h ON h.submission_id = a.submission_id "
            "WHERE a.credit_id = l.id AND h.released_at IS NULL), 0) AS remaining "
            "FROM ledger l WHERE l.user_id = ? AND l.kind = 'credit' "
            "AND julianday(l.created_at) <= julianday(?) "
            "AND (l.expires_at IS NULL OR julianday(l.expires_at) > julianday(?)) "
            "AND (? IS NULL OR l.id < ?) "
            "ORDER BY l.expires_at IS NULL, julianday(l.expires_at), l.id",
            (user_id, at, at, before_id, before_id),
        )

    async def _backfill_allocations(self) -> None:
        """Replay legacy usage once, without ever charging future purchases."""
        async with self._transaction():
            if await _rows(
                self.db,
                "SELECT 1 FROM billing_migrations WHERE name = 'credit_lots_v1'",
            ):
                return
            events = await _rows(
                self.db,
                "SELECT l.id, l.user_id, l.seconds, COALESCE("
                "(SELECT h.created_at FROM holds h WHERE l.note = "
                "'submission:' || h.submission_id AND h.settled = 1), l.created_at) "
                "AS created_at, 'debit' AS kind FROM ledger l WHERE l.kind = 'debit' "
                "AND NOT EXISTS (SELECT 1 FROM debit_allocations a WHERE a.debit_id = l.id) "
                "UNION ALL "
                "SELECT submission_id, user_id, seconds, created_at, 'hold' "
                "FROM holds WHERE released_at IS NULL ORDER BY created_at, id",
            )
            for event in events:
                remaining = int(event["seconds"])
                for credit_id, available in await self._credit_lots(
                    event["user_id"],
                    event["created_at"],
                    event["id"] if event["kind"] == "debit" else None,
                ):
                    used = min(remaining, max(0, available))
                    if used:
                        table, key = (
                            ("debit_allocations", "debit_id")
                            if event["kind"] == "debit"
                            else ("hold_allocations", "submission_id")
                        )
                        await self.db.execute(
                            f"INSERT INTO {table}({key}, credit_id, seconds) VALUES (?, ?, ?)",
                            (event["id"], credit_id, used),
                        )
                        remaining -= used
                    if not remaining:
                        break
                if remaining:
                    logger.warning(
                        "legacy billing event has %s unallocated seconds: %s %s",
                        remaining,
                        event["kind"],
                        event["id"],
                    )
            await self.db.execute(
                "UPDATE submissions SET delivered_at = COALESCE("
                "(SELECT released_at FROM holds WHERE holds.submission_id = "
                "submissions.submission_id AND settled = 1), finished_at) "
                "WHERE status = 'done' OR submission_id IN "
                "(SELECT submission_id FROM holds WHERE settled = 1)"
            )
            await self.db.execute(
                "UPDATE submissions SET status = 'partial' "
                "WHERE status = 'failed' AND delivered_at IS NOT NULL"
            )
            await self.db.execute(
                "INSERT INTO billing_migrations(name) VALUES ('credit_lots_v1')"
            )

    async def _refresh_balance_locked(self, user_id: int) -> int:
        """Recompute the cache from authoritative allocations inside the current write."""
        now = iso(self._now())
        exists = await _rows(self.db, "SELECT 1 FROM users WHERE telegram_id=?", (int(user_id),))
        if not exists:
            await self.db.execute("DELETE FROM balance_dirty WHERE user_id=?", (int(user_id),))
            return 0
        balance = await self._available_seconds_locked(int(user_id))
        # Credits can begin in the future or expire without a database write.
        transitions = await _rows(
            self.db,
            "SELECT moment FROM (SELECT created_at AS moment FROM ledger WHERE user_id=? AND kind='credit' "
            "UNION ALL SELECT expires_at AS moment FROM ledger WHERE user_id=? AND kind='credit' AND expires_at IS NOT NULL) "
            "WHERE julianday(moment) > julianday(?) ORDER BY julianday(moment) LIMIT 1",
            (int(user_id), int(user_id), now),
        )
        next_transition = transitions[0][0] if transitions else None
        await self.db.execute(
            "INSERT INTO user_balances(user_id,available_seconds,updated_at,next_transition) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET available_seconds=excluded.available_seconds, "
            "updated_at=excluded.updated_at,next_transition=excluded.next_transition",
            (int(user_id), balance, now, next_transition),
        )
        await self.db.execute("DELETE FROM balance_dirty WHERE user_id=?", (int(user_id),))
        return balance

    async def _flush_balance_dirty_locked(self, limit: int | None = None) -> int:
        sql = "SELECT user_id FROM balance_dirty ORDER BY user_id"
        params = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        rows = await _rows(self.db, sql, params)
        for row in rows:
            await self._refresh_balance_locked(int(row[0]))
        return len(rows)

    async def _backfill_user_balances(self) -> None:
        """Resume a chunked, idempotent migration before serving balance reads."""
        while True:
            async with self._transaction():
                state = (await _rows(self.db, "SELECT last_user_id,done FROM balance_backfill_state WHERE id=1"))[0]
                if state[1]:
                    break
                if state[0] is None:
                    rows = await _rows(self.db, "SELECT telegram_id FROM users ORDER BY telegram_id LIMIT 500")
                else:
                    rows = await _rows(self.db, "SELECT telegram_id FROM users WHERE telegram_id > ? ORDER BY telegram_id LIMIT 500", (state[0],))
                for row in rows:
                    await self._refresh_balance_locked(int(row[0]))
                last_id = int(rows[-1][0]) if rows else state[0]
                await self.db.execute("UPDATE balance_backfill_state SET last_user_id=?, done=? WHERE id=1", (last_id, int(len(rows) < 500)))
        # An offline importer or interrupted migration can leave dirty markers.
        while True:
            async with self._transaction():
                count = await self._flush_balance_dirty_locked(500)
            if count < 500:
                break

    async def _check_user_balances(self) -> None:
        async with self._transaction():
            rows = await _rows(self.db, "SELECT telegram_id FROM users ORDER BY telegram_id LIMIT 20")
            for row in rows:
                user_id = int(row[0])
                cached = await _rows(self.db, "SELECT available_seconds FROM user_balances WHERE user_id=?", (user_id,))
                actual = await self._available_seconds_locked(user_id)
                if not cached or int(cached[0][0]) != actual:
                    logger.warning("balance cache mismatch user=%s", user_id)
                    METRICS.record_balance_repair()
                    await self._refresh_balance_locked(user_id)

    async def available_seconds(self, user_id: int) -> int:
        if not self._balances_ready:
            if self._read_db is not None:
                return sum(max(0, int(row[1])) for row in await self._credit_lots(user_id, iso(self._now()), conn=self._read_db))
            async with self._write_lock:
                return await self._available_seconds_locked(user_id)
        rows = await _rows(
            self.read_db,
            "SELECT available_seconds FROM user_balances b WHERE b.user_id=? "
            "AND julianday(b.updated_at) <= julianday(?) "
            "AND (b.next_transition IS NULL OR julianday(b.next_transition) > julianday(?)) "
            "AND NOT EXISTS (SELECT 1 FROM balance_dirty d WHERE d.user_id=b.user_id)",
            (int(user_id), iso(self._now()), iso(self._now())),
        )
        if rows:
            return int(rows[0][0])
        async with self._transaction():
            return await self._refresh_balance_locked(int(user_id))

    async def grant_free_plan_once(self, user_id: int, seconds: int) -> bool:
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, created_at) "
                "SELECT ?, ?, 'credit', 'free_plan', ? "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM ledger WHERE user_id = ? AND note = 'free_plan'"
                ")",
                (user_id, seconds, iso(self._now()), user_id),
            )
            return cursor.rowcount > 0

    async def add_credit(
        self,
        user_id: int,
        seconds: int,
        *,
        note: str,
        validity_days: int | None = None,
        admin_id: int | None = None,
    ) -> None:
        now_dt = self._now()
        expires = (
            iso(now_dt + timedelta(days=validity_days)) if validity_days else None
        )
        now = iso(now_dt)
        async with self._transaction():
            await self.db.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
                "VALUES (?, ?, 'credit', ?, ?, ?)",
                (user_id, int(seconds), note, expires, now),
            )
            if admin_id is not None:
                await self.db.execute(
                    "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
                    "VALUES (?, 'grant_credit', ?, ?, ?)",
                    (admin_id, note, user_id, now),
                )

    async def reserve(self, user_id: int, submission_id: int, seconds: int) -> None:
        if seconds <= 0:
            raise ValueError("reservation must be positive")
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT is_unlimited, banned FROM users WHERE telegram_id = ?",
                (user_id,),
            )
            if rows and rows[0][1]:
                raise UserBannedError("⛔ دسترسی شما به ربات مسدود شده است.")
            owner = await _rows(
                self.db,
                "SELECT 1 FROM submissions WHERE submission_id = ? AND user_id = ?",
                (submission_id, user_id),
            )
            if not owner:
                raise ValueError("submission does not belong to user")
            if rows and rows[0][0]:
                return
            available = await self._available_seconds_locked(user_id)
            if available < seconds:
                raise InsufficientBalanceError(available, seconds)
            now = iso(self._now())
            await self.db.execute(
                "INSERT INTO holds(submission_id, user_id, seconds, created_at) "
                "VALUES (?, ?, ?, ?)",
                (submission_id, user_id, seconds, now),
            )
            remaining = seconds
            for credit_id, lot_available in await self._credit_lots(user_id, now):
                used = min(remaining, max(0, lot_available))
                if used:
                    await self.db.execute(
                        "INSERT INTO hold_allocations(submission_id, credit_id, seconds) "
                        "VALUES (?, ?, ?)",
                        (submission_id, credit_id, used),
                    )
                    remaining -= used
                if not remaining:
                    break
            if remaining:
                raise InsufficientBalanceError(seconds - remaining, seconds)

    async def _available_seconds_locked(self, user_id: int) -> int:
        return sum(
            max(0, int(row[1]))
            for row in await self._credit_lots(user_id, iso(self._now()))
        )

    async def settle_hold(self, submission_id: int, measured_seconds: int) -> None:
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT h.user_id, h.seconds, u.is_unlimited FROM holds h "
                "JOIN users u ON u.telegram_id = h.user_id "
                "WHERE h.submission_id = ? AND h.released_at IS NULL",
                (submission_id,),
            )
            now = iso(self._now())
            if not rows:
                await self.db.execute(
                    "UPDATE submissions SET delivered_at = COALESCE(delivered_at, ?) "
                    "WHERE submission_id = ? AND NOT EXISTS "
                    "(SELECT 1 FROM holds WHERE submission_id = ? "
                    "AND COALESCE(release_reason, '') != 'unlimited user')",
                    (now, submission_id, submission_id),
                )
                return
            user_id, reserved, unlimited = rows[0]
            if not unlimited:
                allocations = await _rows(
                    self.db,
                    "SELECT a.credit_id, a.seconds FROM hold_allocations a "
                    "JOIN ledger l ON l.id = a.credit_id WHERE a.submission_id = ? "
                    "ORDER BY l.expires_at IS NULL, l.expires_at, l.id",
                    (submission_id,),
                )
                charge = max(0, min(int(measured_seconds), reserved))
                charge = min(charge, sum(row[1] for row in allocations))
                cursor = await self.db.execute(
                    "INSERT INTO ledger(user_id, seconds, kind, note, created_at, submission_id) "
                    "VALUES (?, ?, 'debit', ?, ?, ?)",
                    (user_id, charge, f"submission:{submission_id}", now, submission_id),
                )
                remaining = charge
                for credit_id, seconds in allocations:
                    used = min(remaining, seconds)
                    if used:
                        await self.db.execute(
                            "INSERT INTO debit_allocations(debit_id, credit_id, seconds) "
                            "VALUES (?, ?, ?)",
                            (cursor.lastrowid, credit_id, used),
                        )
                        remaining -= used
            await self.db.execute(
                "UPDATE holds SET released_at = ?, settled = ?, release_reason = ? "
                "WHERE submission_id = ? AND released_at IS NULL",
                (
                    now,
                    int(not unlimited),
                    "unlimited user" if unlimited else None,
                    submission_id,
                ),
            )
            await self.db.execute(
                "UPDATE submissions SET delivered_at = COALESCE(delivered_at, ?) "
                "WHERE submission_id = ?",
                (now, submission_id),
            )

    async def release_hold(self, submission_id: int, reason: str) -> bool:
        async with self._transaction():
            cursor = await self.db.execute(
                "UPDATE holds SET released_at = ?, settled = 0, release_reason = ? "
                "WHERE submission_id = ? AND released_at IS NULL",
                (iso(self._now()), reason[:200], submission_id),
            )
            return bool(cursor.rowcount)

    async def spend_seconds(
        self,
        user_id: int,
        seconds: int,
        note: str,
        *,
        operation_id: str | None = None,
    ) -> bool:
        if seconds <= 0:
            raise ValueError("spend must be positive")
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT is_unlimited, banned FROM users WHERE telegram_id = ?",
                (user_id,),
            )
            if not rows:
                raise ValueError("unknown user")
            if rows[0][1]:
                raise UserBannedError("⛔ دسترسی شما به ربات مسدود شده است.")
            if rows[0][0]:
                return False
            available = await self._available_seconds_locked(user_id)
            if available < seconds:
                raise InsufficientBalanceError(available, seconds)
            now = iso(self._now())
            cursor = await self.db.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, created_at) "
                "VALUES (?, ?, 'debit', ?, ?)",
                (user_id, int(seconds), note[:200], now),
            )
            remaining = seconds
            for credit_id, lot_seconds in await self._credit_lots(user_id, now):
                used = min(remaining, max(0, lot_seconds))
                if used:
                    await self.db.execute(
                        "INSERT INTO debit_allocations(debit_id, credit_id, seconds) "
                        "VALUES (?, ?, ?)",
                        (cursor.lastrowid, credit_id, used),
                    )
                    remaining -= used
                if not remaining:
                    break
            if remaining:
                raise InsufficientBalanceError(seconds - remaining, seconds)
            if operation_id is not None:
                if not operation_id:
                    raise ValueError("learning charge operation_id must not be empty")
                await self.db.execute(
                    "INSERT INTO learning_charges(operation_id, user_id, debit_id, seconds, "
                    "note, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        operation_id,
                        int(user_id),
                        cursor.lastrowid,
                        int(seconds),
                        note[:200],
                        now,
                        now,
                    ),
                )
            return True

    async def _finalize_learning_charge_locked(
        self, user_id: int, operation_id: str | None, refund_seconds_value: int = 0
    ) -> None:
        if operation_id is None:
            if refund_seconds_value:
                raise ValueError("refund requires a paid learning charge")
            return
        if refund_seconds_value:
            rows = await _rows(
                self.db,
                "SELECT debit_id, seconds FROM learning_charges "
                "WHERE operation_id = ? AND user_id = ? AND status = 'pending'",
                (operation_id, int(user_id)),
            )
            if not rows or not 0 < refund_seconds_value <= int(rows[0][1]):
                METRICS.record_db_error()
                raise ValueError("invalid pro-rata learning refund")
            await self._refund_debit_locked(
                user_id, int(rows[0][0]), refund_seconds_value,
                "pro-rata learning refund", iso(self._now()),
            )
        cursor = await self.db.execute(
            "UPDATE learning_charges SET status = 'finalized', updated_at = ? "
            "WHERE operation_id = ? AND user_id = ? AND status = 'pending'",
            (iso(self._now()), operation_id, int(user_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError("learning charge is not pending for this user")

    async def _refund_debit_locked(
        self, user_id: int, debit_id: int, seconds: int, note: str, now: str
    ) -> None:
        """Mint a refund using the original debit's allocations, inside a transaction."""
        rows = await _rows(
            self.db,
            "SELECT l.id, l.expires_at, a.seconds, julianday(l.expires_at) FROM debit_allocations a "
            "JOIN ledger l ON l.id = a.credit_id "
            "JOIN ledger d ON d.id = a.debit_id "
            "WHERE d.id = ? AND d.user_id = ? AND d.kind = 'debit' "
            "AND l.user_id = d.user_id ORDER BY l.id",
            (int(debit_id), int(user_id)),
        )
        if not rows or sum(int(row[2]) for row in rows) < seconds:
            METRICS.record_db_error()
            raise ValueError("refund requires sufficient original debit allocations")
        expiring = [row for row in rows if row[1] is not None]
        if any(row[3] is None for row in expiring):
            METRICS.record_db_error()
            logger.warning("refund rejected for invalid credit expiry debit_id=%s", debit_id)
            raise ValueError("invalid source credit expiry")
        # Use the same SQLite timestamp semantics as balance calculation, not
        # lexical order: imported values may have offsets or mixed formats.
        earliest = min(expiring, key=lambda row: row[3])[1] if expiring else None
        lot_ids = ",".join(str(row[0]) for row in rows)
        await self.db.execute(
            "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
            "VALUES (?, ?, 'credit', ?, ?, ?)",
            (int(user_id), int(seconds), "refund: " + note[:180] + " lots:" + lot_ids,
             earliest, now),
        )

    async def refund_learning_charge(self, user_id: int, operation_id: str) -> bool:
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT seconds, note, debit_id FROM learning_charges "
                "WHERE operation_id = ? AND user_id = ? AND status = 'pending'",
                (operation_id, int(user_id)),
            )
            if not rows:
                return False
            now = iso(self._now())
            await self._refund_debit_locked(user_id, int(rows[0][2]), int(rows[0][0]), str(rows[0][1]), now)
            await self.db.execute(
                "UPDATE learning_charges SET status = 'refunded', updated_at = ? "
                "WHERE operation_id = ? AND user_id = ? AND status = 'pending'",
                (now, operation_id, int(user_id)),
            )
            return True

    async def recover_pending_learning_charges(self) -> int:
        async with self._transaction():
            now = iso(self._now())
            recovered = 0
            while True:
                rows = await _rows(
                    self.db,
                    "SELECT operation_id, user_id, debit_id, seconds, note "
                    "FROM learning_charges WHERE status = 'pending' LIMIT 500",
                )
                if not rows:
                    return recovered
                for row in rows:
                    await self._refund_debit_locked(int(row[1]), int(row[2]), int(row[3]), str(row[4]), now)
                    await self.db.execute(
                        "UPDATE learning_charges SET status = 'refunded', updated_at = ? "
                        "WHERE operation_id = ? AND status = 'pending'",
                        (now, row[0]),
                    )
                    recovered += 1

    async def refund_seconds(
        self, user_id: int, seconds: int, note: str, *, debit_id: int | None = None
    ) -> Any:
        if seconds <= 0:
            logger.warning("refund ignored user=%s seconds=%s", user_id, seconds)
            return
        if debit_id is None:
            METRICS.record_db_error()
            raise ValueError("refund requires the original debit_id")
        async with self._transaction():
            await self._refund_debit_locked(user_id, debit_id, int(seconds), note, iso(self._now()))
