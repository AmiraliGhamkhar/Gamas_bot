"""Read-only admin reporting, schema reference integrity, audit trail, and crash recovery (SRP / R4)."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from pathlib import Path
import sqlite3
from typing import Any

from .db_core import _rows, iso
from .db_users import UsersRepositoryMixin
from .metrics import METRICS

logger = logging.getLogger("gamas_bot.db.reporting")


class ReportingRepositoryMixin(UsersRepositoryMixin):
    """Single-responsibility persistence slice for reporting, integrity, audit, and recovery."""

    USER_REFERENCE_COLUMNS: tuple[tuple[str, str], ...] = (
        ("submissions", "user_id"),
        ("library_items", "user_id"),
        ("ledger", "user_id"),
        ("holds", "user_id"),
        ("payments", "user_id"),
        ("quiz_sessions", "user_id"),
        ("flashcard_decks", "user_id"),
        ("free_uses", "user_id"),
        ("documents", "user_id"),
        ("learning_charges", "user_id"),
        ("support_tickets", "user_id"),
    )
    REFERENCE_LINKS: tuple[tuple[str, str, str, str], ...] = (
        ("quiz_sessions", "item_id", "library_items", "item_id"),
        ("quiz_questions", "session_id", "quiz_sessions", "session_id"),
        ("quiz_answers", "session_id", "quiz_sessions", "session_id"),
        ("quiz_answers", "question_id", "quiz_questions", "question_id"),
        ("flashcard_decks", "item_id", "library_items", "item_id"),
        ("flashcards", "deck_id", "flashcard_decks", "deck_id"),
        ("debit_allocations", "debit_id", "ledger", "id"),
        ("debit_allocations", "credit_id", "ledger", "id"),
        ("hold_allocations", "submission_id", "holds", "submission_id"),
        ("hold_allocations", "credit_id", "ledger", "id"),
        ("holds", "submission_id", "submissions", "submission_id"),
        ("retired_holds", "submission_id", "submissions", "submission_id"),
        ("library_items", "submission_id", "submissions", "submission_id"),
        ("ledger", "submission_id", "submissions", "submission_id"),
        ("ledger", "payment_id", "payments", "payment_id"),
        ("documents", "library_item_id", "library_items", "item_id"),
        ("learning_charges", "debit_id", "ledger", "id"),
    )

    async def expire_retained_data(self, kind: str, days: int, *, batch: int = 500) -> int:
        """Delete one bounded batch; active artifacts and live document mirrors survive."""
        if days <= 0:
            return 0
        predicates = {
            "documents": ("documents", "id", "created_at < ? AND library_item_id IS NULL"),
            "learning": ("quiz_sessions", "session_id", "updated_at < ? AND status IN ('done','quit','expired')"),
            "audit": ("audit", "id", "created_at < ?"),
            "holds": ("holds", "submission_id", "released_at IS NOT NULL AND released_at < ?"),
        }
        if kind not in predicates:
            raise ValueError("unknown retention kind")
        table, key, predicate = predicates[kind]
        cutoff = iso(self._now() - timedelta(days=int(days)))
        limit = min(500, max(1, int(batch)))
        async with self._transaction():
            rows = await _rows(self.db, f"SELECT {key} FROM {table} WHERE {predicate} ORDER BY {key} LIMIT ?", (cutoff, limit))
            ids = [row[0] for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            if kind == "holds":
                # Preserve the idempotency marker and delivery semantics after details expire.
                await self.db.execute(
                    f"INSERT OR IGNORE INTO retired_holds(submission_id, release_reason, settled) "
                    f"SELECT submission_id, release_reason, settled FROM holds WHERE submission_id IN ({placeholders})",
                    ids,
                )
                await self.db.execute(f"DELETE FROM hold_allocations WHERE submission_id IN ({placeholders})", ids)
            cursor = await self.db.execute(f"DELETE FROM {table} WHERE {key} IN ({placeholders})", ids)
            count = max(0, cursor.rowcount)
        METRICS.record_retention_deleted(kind, count)
        return count

    async def maintenance(self, *, optimize: bool = False) -> None:
        """Checkpoint/optimize under the writer lock (SQLite forbids checkpoint in a transaction)."""
        if str(self.path) == ":memory:":
            return
        async with self._write_lock:
            try:
                await self.db.execute_fetchall("PRAGMA wal_checkpoint(PASSIVE)")
                if optimize:
                    await self.db.execute("PRAGMA analysis_limit=400")
                    await self.db.execute("PRAGMA optimize")
            except sqlite3.Error:
                METRICS.record_retention_error()
                raise

    DELIVERED_STATUSES = ("done", "partial")
    UNDELIVERED_STATUSES = ("failed", "rejected")
    REPORT_SERIES_MAX_DAYS = 93

    async def stats(self) -> dict[str, Any]:
        totals = await _rows(
            self.read_db,
            "SELECT status, COUNT(*) FROM submissions GROUP BY status",
        )
        by_status = {row[0]: int(row[1]) for row in totals}
        users = await self.count_users()
        seconds = await _rows(
            self.read_db,
            "SELECT COALESCE(SUM(duration_seconds), 0) FROM submissions "
            "WHERE delivered_at IS NOT NULL OR status = 'done'",
        )
        revenue = await _rows(
            self.read_db,
            "SELECT COALESCE(SUM(COALESCE(p.purchased_price, "
            "  (SELECT price FROM plans pl WHERE pl.code = p.plan_code), 0)), 0) "
            "FROM payments p WHERE p.status = 'approved'",
        )
        return {
            "users": users,
            "submissions_done": by_status.get("done", 0),
            "submissions_failed": by_status.get("failed", 0)
            + by_status.get("partial", 0),
            "submissions_partial": by_status.get("partial", 0),
            "submissions_active": by_status.get("processing", 0)
            + by_status.get("pending", 0),
            "seconds_transcribed": float(seconds[0][0]) if seconds else 0.0,
            "toman_approved": int(revenue[0][0]) if revenue else 0,
        }

    async def user_reference_integrity(self) -> list[str]:
        problems: list[str] = []
        try:
            fk_rows = await _rows(self.read_db, "PRAGMA foreign_key_check")
            for row in fk_rows:
                problems.append(
                    f"foreign_key_check: table={row[0]} rowid={row[1]} parent={row[2]}"
                )
        except sqlite3.Error as exc:
            problems.append(f"foreign_key_check failed: {exc}")
        links = [
            (table, column, "users", "telegram_id")
            for table, column in self.USER_REFERENCE_COLUMNS
        ] + list(self.REFERENCE_LINKS)
        for table, column, parent, parent_column in links:
            info = await _rows(self.read_db, f"PRAGMA table_info({table})")
            declared = {str(row[1]): str(row[2]).upper() for row in info}
            if declared.get(column) != "INTEGER":
                problems.append(f"missing INTEGER column: {table}.{column}")
                continue
            fks = await _rows(self.read_db, f"PRAGMA foreign_key_list({table})")
            # Legacy library items have a logical submission link but no
            # declared FK; inspect orphans without rebuilding applied schema.
            logical_only = (table, column) == ("library_items", "submission_id")
            if not logical_only and not any(
                row[2] == parent and row[3] == column and row[4] == parent_column
                for row in fks
            ):
                problems.append(
                    f"missing foreign key: {table}.{column} -> {parent}.{parent_column}"
                )
            orphans = await _rows(
                self.read_db,
                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL "
                f"AND {column} NOT IN (SELECT {parent_column} FROM {parent})",
            )
            count = int(orphans[0][0]) if orphans else 0
            if count:
                problems.append(f"orphan rows: {table}.{column} = {count}")
        info = await _rows(self.read_db, "PRAGMA table_info(users)")
        if not any(
            row[1] == "telegram_id"
            and str(row[2]).upper() == "INTEGER"
            and row[5]
            for row in info
        ):
            problems.append("users.telegram_id is not an INTEGER primary key")
        affinity = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM users WHERE typeof(telegram_id) != 'integer'",
        )
        if affinity and int(affinity[0][0]):
            problems.append(
                f"users.telegram_id not stored as INTEGER: {affinity[0][0]}"
            )
        return problems

    async def assert_user_reference_integrity(self) -> None:
        problems = await self.user_reference_integrity()
        if problems:
            raise RuntimeError(
                "SQLite reference integrity failed: " + "; ".join(problems)
            )

    @staticmethod
    def _period_clause(
        period_start: str | None, column: str = "created_at"
    ) -> tuple[str, tuple[Any, ...]]:
        if not period_start:
            return "", ()
        return f"{column} >= ?", (period_start,)

    async def _period_duration(
        self, expression: str, where: str, params: tuple[Any, ...]
    ) -> dict[str, float | int | None]:
        query = (
            f"WITH s AS (SELECT {expression} AS v FROM submissions WHERE {where}), "
            "st AS (SELECT COUNT(*) AS n, AVG(v) AS avg_v, MAX(v) AS max_v FROM s), "
            "r AS (SELECT v, ROW_NUMBER() OVER (ORDER BY v) AS rn FROM s) "
            "SELECT st.n, st.avg_v, st.max_v, "
            "(SELECT v FROM r WHERE rn = (st.n + 1) / 2), "
            "(SELECT v FROM r WHERE rn = (st.n * 95 + 99) / 100) FROM st"
        )
        rows = await _rows(self.read_db, query, params)
        count = int(rows[0][0] or 0) if rows else 0
        if not count:
            return {
                "count": 0,
                "avg_seconds": None,
                "median_seconds": None,
                "p95_seconds": None,
                "max_seconds": None,
            }

        def _round(value: Any) -> float | None:
            return None if value is None else round(float(value), 3)

        return {
            "count": count,
            "avg_seconds": _round(rows[0][1]),
            "median_seconds": _round(rows[0][3]),
            "p95_seconds": _round(rows[0][4]),
            "max_seconds": _round(rows[0][2]),
        }

    async def admin_health(self) -> dict[str, Any]:
        last_rows = await _rows(
            self.read_db,
            "SELECT MAX(finished_at) FROM submissions WHERE status IN ('done','partial')",
        )
        db_bytes = 0
        wal_bytes = 0
        if str(self.path) != ":memory:":
            try:
                db_bytes = self.path.stat().st_size
                wal_path = Path(f"{self.path}-wal")
                if wal_path.exists():
                    wal_bytes = wal_path.stat().st_size
            except OSError:
                db_bytes = 0
        return {
            "last_success": (
                str(last_rows[0][0]) if last_rows and last_rows[0][0] else None
            ),
            "db_bytes": int(db_bytes),
            "wal_bytes": int(wal_bytes),
        }

    async def admin_report(
        self,
        *,
        period_start: str | None,
        now: datetime | None = None,
        expiring_within_days: int = 7,
        with_timings: bool = True,
    ) -> dict[str, Any]:
        now = now or self._now()
        now_iso = iso(now)
        series_floor = iso(now - timedelta(days=self.REPORT_SERIES_MAX_DAYS))
        series_since = (
            max(period_start, series_floor) if period_start else series_floor
        )
        period_where, period_params = self._period_clause(period_start)
        where = period_where or "1 = 1"

        new_users_where, new_users_params = self._period_clause(
            period_start, "joined_at"
        )
        user_rows = await _rows(
            self.read_db,
            "SELECT "
            "(SELECT COUNT(*) FROM users), "
            f"(SELECT COUNT(*) FROM users WHERE {new_users_where or '1 = 1'}), "
            f"(SELECT COUNT(DISTINCT user_id) FROM submissions WHERE {where}), "
            "(SELECT COUNT(DISTINCT user_id) FROM submissions WHERE "
            f"  status IN ('done','partial') AND {where}), "
            "(SELECT COUNT(*) FROM users WHERE banned = 1), "
            "(SELECT COUNT(*) FROM users WHERE is_unlimited = 1)",
            (*new_users_params, *period_params, *period_params),
        )
        users_row = user_rows[0] if user_rows else (0, 0, 0, 0, 0, 0)

        status_rows = await _rows(
            self.read_db,
            f"SELECT status, COUNT(*) FROM submissions WHERE {where} GROUP BY status",
            period_params,
        )
        by_status = {str(row[0]): int(row[1]) for row in status_rows}

        kind_rows = await _rows(
            self.read_db,
            f"SELECT kind, COUNT(*) FROM submissions WHERE {where} GROUP BY kind",
            period_params,
        )
        by_kind = {str(row[0]): int(row[1]) for row in kind_rows}

        delivered = sum(
            by_status.get(status, 0) for status in self.DELIVERED_STATUSES
        )
        undelivered = sum(
            by_status.get(status, 0) for status in self.UNDELIVERED_STATUSES
        )
        terminal = delivered + undelivered

        error_rows = await _rows(
            self.read_db,
            "SELECT COALESCE(error_kind, 'unclassified') AS error_kind, COUNT(*) "
            f"FROM submissions WHERE status IN ('failed','rejected') AND {where} "
            "GROUP BY COALESCE(error_kind, 'unclassified') "
            "ORDER BY COUNT(*) DESC LIMIT 12",
            period_params,
        )

        day_rows = await _rows(
            self.read_db,
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*), "
            "SUM(CASE WHEN status IN ('done','partial') THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status IN ('failed','rejected') THEN 1 ELSE 0 END) "
            f"FROM submissions WHERE created_at >= ? AND {where} "
            "GROUP BY day ORDER BY day DESC LIMIT ?",
            (series_since, *period_params, self.REPORT_SERIES_MAX_DAYS),
        )
        new_user_rows = await _rows(
            self.read_db,
            "SELECT substr(joined_at, 1, 10) AS day, COUNT(*) FROM users "
            f"WHERE joined_at >= ? AND {new_users_where or '1 = 1'} "
            "GROUP BY day ORDER BY day DESC LIMIT ?",
            (series_since, *new_users_params, self.REPORT_SERIES_MAX_DAYS),
        )
        new_by_day = {str(row[0]): int(row[1]) for row in new_user_rows}
        job_by_day = {
            str(row[0]): (int(row[1]), int(row[2] or 0), int(row[3] or 0))
            for row in day_rows
        }
        series = [
            {
                "day": day,
                "new_users": new_by_day.get(day, 0),
                "jobs": job_by_day.get(day, (0, 0, 0))[0],
                "delivered": job_by_day.get(day, (0, 0, 0))[1],
                "undelivered": job_by_day.get(day, (0, 0, 0))[2],
            }
            for day in sorted(set(new_by_day) | set(job_by_day))
        ]

        queue_wait = processing_time = end_to_end = None
        if with_timings:
            queue_wait = await self._period_duration(
                "(julianday(started_at) - julianday(created_at)) * 86400.0",
                f"started_at IS NOT NULL AND {where}",
                period_params,
            )
            processing_time = await self._period_duration(
                "(julianday(finished_at) - julianday(started_at)) * 86400.0",
                f"started_at IS NOT NULL AND finished_at IS NOT NULL AND {where}",
                period_params,
            )
            end_to_end = await self._period_duration(
                "(julianday(finished_at) - julianday(created_at)) * 86400.0",
                f"finished_at IS NOT NULL AND {where}",
                period_params,
            )

        payment_rows = await _rows(
            self.read_db,
            "SELECT status, COUNT(*), COALESCE(SUM(COALESCE(purchased_price, 0)), 0) "
            f"FROM payments WHERE {where} GROUP BY status",
            period_params,
        )
        payments_by_status = {
            str(row[0]): {"count": int(row[1]), "toman": int(row[2] or 0)}
            for row in payment_rows
        }
        pending_rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM payments "
            "WHERE status IN ('pending_review','pending') AND receipt_path IS NOT NULL",
        )
        future = iso(now + timedelta(days=max(1, expiring_within_days)))
        expiring_rows = await _rows(
            self.read_db,
            "SELECT COUNT(*), COALESCE(SUM(seconds), 0) FROM ledger "
            "WHERE kind = 'credit' AND expires_at IS NOT NULL "
            "AND julianday(expires_at) > julianday(?) AND julianday(expires_at) <= julianday(?)",
            (now_iso, future),
        )

        health = await self.admin_health()

        return {
            "period": {
                "start": period_start,
                "end": now_iso,
                "series_since": series_since,
            },
            "users": {
                "total": int(users_row[0] or 0),
                "new": int(users_row[1] or 0),
                "active": int(users_row[2] or 0),
                "delivered": int(users_row[3] or 0),
                "banned": int(users_row[4] or 0),
                "unlimited": int(users_row[5] or 0),
            },
            "processing": {
                "by_status": by_status,
                "by_kind": by_kind,
                "delivered": delivered,
                "undelivered": undelivered,
                "terminal": terminal,
                "success_rate": (delivered / terminal) if terminal else None,
                "errors": [
                    {"kind": str(row[0]), "count": int(row[1])}
                    for row in error_rows
                ],
                "queue_wait": queue_wait,
                "processing_time": processing_time,
                "end_to_end": end_to_end,
                "series": series,
            },
            "payments": {
                "by_status": payments_by_status,
                "pending_review": int(pending_rows[0][0]) if pending_rows else 0,
                "expiring": {
                    "within_days": max(1, expiring_within_days),
                    "count": int(expiring_rows[0][0]) if expiring_rows else 0,
                    "seconds": (
                        int(expiring_rows[0][1] or 0) if expiring_rows else 0
                    ),
                },
            },
            "health": health,
        }

    async def admin_stt_engines(
        self, *, period_start: str | None
    ) -> list[dict[str, Any]]:
        period_where, params = self._period_clause(period_start)
        rows = await _rows(
            self.read_db,
            "SELECT COALESCE(stt_engine, 'unrecorded') AS engine, COUNT(*) "
            f"FROM submissions WHERE {period_where or '1 = 1'} "
            "GROUP BY COALESCE(stt_engine, 'unrecorded') "
            "ORDER BY COUNT(*) DESC",
            params,
        )
        return [{"engine": str(row[0]), "count": int(row[1])} for row in rows]

    async def recover_interrupted(self) -> int:
        now = iso(self._now())
        async with self._transaction():
            recovered = await _rows(
                self.db,
                "SELECT submission_id, user_id FROM submissions "
                "WHERE status IN ('pending', 'processing')",
            )
            for row in recovered:
                await self.db.execute(
                    "UPDATE submissions SET status = CASE WHEN delivered_at IS NOT NULL "
                    "THEN 'partial' ELSE 'failed' END, error = 'interrupted by restart', "
                    "error_kind = COALESCE(error_kind, 'cancelled'), "
                    "finished_at = ? WHERE submission_id = ?",
                    (now, row[0]),
                )
                await self.db.execute(
                    "UPDATE holds SET released_at = ?, "
                    "release_reason = COALESCE(release_reason, 'interrupted by restart') "
                    "WHERE submission_id = ? AND released_at IS NULL",
                    (now, row[0]),
                )
        if recovered:
            logger.warning(
                "Recovered %d interrupted submissions at startup", len(recovered)
            )
        return len(recovered)

    async def audit(
        self,
        admin_id: int,
        action: str,
        detail: str,
        subject_user: int | None = None,
    ) -> None:
        await self._write(
            "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (admin_id, action, detail[:400], subject_user, iso(self._now())),
        )

    async def recent_audit(
        self, limit: int = 30, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT admin_id, action, detail, subject_user, created_at FROM audit "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(row) for row in rows]
