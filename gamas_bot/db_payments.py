"""Tariff catalog and manual card-to-card payment repository (SRP / B7)."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from .db_core import (
    DatabaseCore,
    OpenPaymentError,
    PaymentSnapshotError,
    _rows,
    iso,
)

logger = logging.getLogger("gamas_bot.db.payments")


class PaymentsRepositoryMixin(DatabaseCore):
    async def _credit_referral_for_payment(
        self, referred_user_id: int, payment_id: int
    ) -> bool:
        """Provided by the referrals slice; overridden by the composed database."""
        raise NotImplementedError

    """Single-responsibility persistence slice for plans and manual payments."""

    PAYMENT_OPEN_STATUSES = ("awaiting_payment", "pending_review", "pending")
    PAYMENT_PENDING_REVIEW_STATUSES = ("pending_review", "pending")

    _PAYMENT_COLUMNS = (
        "payment_id, user_id, plan_code, status, receipt_path, created_at, "
        "reviewed_at, reviewed_by, purchased_seconds, purchased_price, "
        "purchased_validity_days, review_reason, "
        "(SELECT l.id FROM ledger l WHERE l.payment_id = payments.payment_id "
        "AND l.kind = 'credit') AS credit_ledger_id"
    )

    async def sync_plans(self, catalog: list[dict[str, Any]]) -> None:
        statements: list[tuple[str, tuple[Any, ...]]] = []
        for plan in catalog:
            statements.append(
                (
                    "INSERT INTO plans(code, hours, price, validity_days, is_custom, active) "
                    "VALUES (?, ?, ?, ?, 0, 1) "
                    "ON CONFLICT(code) DO UPDATE SET hours = excluded.hours, "
                    "price = excluded.price, validity_days = excluded.validity_days "
                    "WHERE is_custom = 0",
                    (plan["code"], plan["hours"], plan["price"], plan["validity_days"]),
                )
            )
        await self._write_many(statements)

    async def plans(self) -> list[dict[str, Any]]:
        rows = await _rows(
            self.db,
            "SELECT code, hours, price, validity_days, is_custom, active FROM plans "
            "WHERE active = 1 ORDER BY price",
        )
        return [dict(row) for row in rows]

    async def plan(self, code: str) -> dict[str, Any] | None:
        rows = await _rows(
            self.db,
            "SELECT code, hours, price, validity_days, is_custom, active FROM plans "
            "WHERE code = ? AND active = 1",
            (code,),
        )
        return dict(rows[0]) if rows else None

    async def create_payment(self, user_id: int, plan_code: str) -> int:
        async with self._transaction():
            open_rows = await _rows(
                self.db,
                "SELECT payment_id, plan_code FROM payments "
                "WHERE user_id = ? AND status IN ('pending','awaiting_payment','pending_review') "
                "ORDER BY payment_id LIMIT 1",
                (user_id,),
            )
            if open_rows:
                if str(open_rows[0][1]) == plan_code:
                    return int(open_rows[0][0])
                raise OpenPaymentError(str(open_rows[0][1]))
            plan = await _rows(
                self.db,
                "SELECT hours, price, validity_days FROM plans "
                "WHERE code = ? AND active = 1 AND price > 0",
                (plan_code,),
            )
            if not plan:
                raise ValueError(f"plan {plan_code} is not on sale")
            hours, price, validity_days = plan[0]
            seconds = int(round(float(hours) * 3600))
            cursor = await self.db.execute(
                "INSERT INTO payments(user_id, plan_code, status, created_at, "
                "purchased_seconds, purchased_price, purchased_validity_days) "
                "VALUES (?, ?, 'awaiting_payment', ?, ?, ?, ?)",
                (
                    user_id,
                    plan_code,
                    iso(self._now()),
                    seconds,
                    int(price),
                    validity_days,
                ),
            )
            payment_id = int(cursor.lastrowid or 0)
        logger.info(
            "payment state transition payment_id=%s plan_code=%s status=awaiting_payment",
            payment_id,
            plan_code,
        )
        return payment_id

    async def cancel_payment(self, payment_id: int, user_id: int) -> bool:
        async with self._transaction():
            cursor = await self.db.execute(
                "UPDATE payments SET status = 'cancelled', reviewed_at = ?, "
                "review_reason = 'cancelled by owner before receipt' "
                "WHERE payment_id = ? AND user_id = ? AND status IN ('pending','awaiting_payment') "
                "AND receipt_path IS NULL",
                (iso(self._now()), payment_id, user_id),
            )
            changed = bool(cursor.rowcount)
        if changed:
            logger.info(
                "payment state transition payment_id=%s status=cancelled",
                payment_id,
            )
        return changed

    async def set_payment_receipt(
        self, payment_id: int, receipt_path: str, user_id: int | None = None
    ) -> bool:
        async with self._transaction():
            ownership = "AND user_id = ? " if user_id is not None else ""
            params: tuple[Any, ...] = (receipt_path, payment_id)
            if user_id is not None:
                params += (int(user_id),)
            cursor = await self.db.execute(
                "UPDATE payments SET receipt_path = ?, status = 'pending_review' "
                f"WHERE payment_id = ? {ownership}"
                "AND status IN ('pending','awaiting_payment')",
                params,
            )
            changed = bool(cursor.rowcount)
        if changed:
            logger.info(
                "payment receipt stored payment_id=%s status=pending_review",
                payment_id,
            )
        return changed

    async def forget_receipts(self, paths: tuple[str, ...] | list[str]) -> int:
        cleaned = tuple(dict.fromkeys(path for path in paths if path))
        if not cleaned:
            return 0
        now = iso(self._now())
        async with self._transaction():
            cleared = 0
            for path in cleaned:
                cursor = await self.db.execute(
                    "UPDATE payments SET receipt_path = NULL WHERE receipt_path = ?",
                    (path,),
                )
                cleared += max(0, int(cursor.rowcount or 0))
                await self.db.execute(
                    "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
                    "VALUES (0, 'receipt_expired', ?, NULL, ?)",
                    (path, now),
                )
            return cleared

    async def payment(self, payment_id: int) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            f"SELECT {self._PAYMENT_COLUMNS} FROM payments WHERE payment_id = ?",
            (payment_id,),
        )
        return dict(rows[0]) if rows else None

    async def open_awaiting_payment_for_user(
        self, user_id: int
    ) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            f"SELECT {self._PAYMENT_COLUMNS} FROM payments "
            "WHERE user_id = ? AND status IN ('awaiting_payment', 'pending') "
            "AND receipt_path IS NULL ORDER BY payment_id DESC LIMIT 1",
            (user_id,),
        )
        return dict(rows[0]) if rows else None

    async def pending_payments(self) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            f"SELECT {self._PAYMENT_COLUMNS} FROM payments "
            "WHERE status IN ('pending','awaiting_payment','pending_review') "
            "ORDER BY payment_id",
        )
        return [dict(row) for row in rows]

    async def awaiting_payments(self) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            f"SELECT {self._PAYMENT_COLUMNS} FROM payments "
            "WHERE status = 'awaiting_payment' ORDER BY payment_id",
        )
        return [dict(row) for row in rows]

    async def pending_review_payments(
        self, *, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        query = (
            f"SELECT {self._PAYMENT_COLUMNS} FROM payments "
            "WHERE status IN ('pending_review','pending') AND receipt_path IS NOT NULL "
            "ORDER BY payment_id"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params = (max(0, int(limit)), max(0, int(offset)))
        rows = await _rows(self.read_db, query, params)
        return [dict(row) for row in rows]

    async def count_pending_review_payments(self) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM payments "
            "WHERE status IN ('pending_review','pending') AND receipt_path IS NOT NULL",
        )
        return int(rows[0][0]) if rows else 0

    async def all_payments(
        self, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...] = (int(limit), int(offset))
        if status:
            where = "WHERE status = ? "
            params = (str(status), int(limit), int(offset))
        rows = await _rows(
            self.read_db,
            f"SELECT {self._PAYMENT_COLUMNS} FROM payments {where}"
            "ORDER BY payment_id DESC LIMIT ? OFFSET ?",
            params,
        )
        return [dict(row) for row in rows]

    async def payment_history_paginated(
        self, user_id: int, limit: int = 10, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            f"SELECT {self._PAYMENT_COLUMNS} FROM payments "
            "WHERE user_id = ? ORDER BY payment_id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
        return [dict(row) for row in rows]

    async def count_payments_by_status(self) -> dict[str, int]:
        rows = await _rows(
            self.read_db, "SELECT status, COUNT(*) FROM payments GROUP BY status"
        )
        return {row[0]: int(row[1]) for row in rows}

    async def review_payment(
        self,
        payment_id: int,
        approved: bool,
        admin_id: int,
        *,
        reason: str = "",
    ) -> bool:
        if not approved and not reason.strip():
            raise ValueError("rejecting a payment requires a reason")
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT status, user_id, plan_code, purchased_seconds, "
                "purchased_validity_days, receipt_path, purchased_price "
                "FROM payments WHERE payment_id = ?",
                (payment_id,),
            )
            if not rows:
                return False
            status = str(rows[0][0])
            receipt_path = rows[0][5]
            if status not in ("pending_review", "pending") or not receipt_path:
                return False
            user_id, plan_code = int(rows[0][1]), str(rows[0][2])
            seconds = int(rows[0][3] or 0)
            price = int(rows[0][6] or 0)
            validity_days = rows[0][4]
            if approved and (seconds <= 0 or price <= 0):
                raise PaymentSnapshotError(payment_id)
            now_dt = self._now()
            now = iso(now_dt)
            cursor = await self.db.execute(
                "UPDATE payments SET status = ?, reviewed_at = ?, reviewed_by = ?, "
                "review_reason = ? WHERE payment_id = ? "
                "AND status IN ('pending_review','pending') AND receipt_path IS NOT NULL",
                (
                    "approved" if approved else "rejected",
                    now,
                    admin_id,
                    reason.strip()[:400] or None,
                    payment_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if approved:
                expires = (
                    iso(now_dt + timedelta(days=int(validity_days)))
                    if validity_days
                    else None
                )
                await self.db.execute(
                    "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at, "
                    "payment_id) VALUES (?, ?, 'credit', ?, ?, ?, ?)",
                    (user_id, seconds, f"plan:{plan_code}", expires, now, payment_id),
                )
                # The referrer's reward commits with the approval itself, so a
                # rollback can never pay it without the buyer's credit.
                await self._credit_referral_for_payment(user_id, payment_id)
            await self.db.execute(
                "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    admin_id,
                    "payment_approved" if approved else "payment_rejected",
                    f"payment:{payment_id} plan:{plan_code}"
                    + (f" reason:{reason.strip()[:200]}" if reason.strip() else ""),
                    user_id,
                    now,
                ),
            )
        logger.info(
            "payment reviewed payment_id=%s decision=%s",
            payment_id,
            "approved" if approved else "rejected",
        )
        return True

    async def payment_history(
        self, user_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT payment_id, plan_code, status, receipt_path, created_at, "
            "reviewed_at FROM payments "
            "WHERE user_id = ? ORDER BY payment_id DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(row) for row in rows]
