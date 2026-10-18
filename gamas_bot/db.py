"""SQLite storage for Gamas Bot v3.

Eight tables, one connection, WAL mode, ``BEGIN IMMEDIATE`` writes. The v2
database is not migrated in place: v3 opens a fresh file (``DB_PATH``, default
``data/gamas_v3.db``) and :mod:`scripts.import_v2` can copy users, credit and
the ledger across from a v2 database.

Billing model — three append-only facts and one derived balance:

* ``ledger`` rows of kind ``credit`` (seconds granted, with optional expiry);
* ``ledger`` rows of kind ``debit`` (seconds consumed by a finished job);
* ``holds`` rows (seconds reserved by a running job, released on settle).

``available = credits(not expired) − debits − holds(not released)``. A job
reserves before it runs, settles for the measured duration on success and
releases everything on any failure or cancellation — so a failed job can never
silently consume credit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "v3"

TERMINAL_SUBMISSION_STATUSES = ("done", "failed", "rejected")
LEDGER_KINDS = ("credit", "debit")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class InsufficientBalanceError(RuntimeError):
    """A billing denial carrying the auditable available/required seconds."""

    def __init__(self, available_seconds: int, required_seconds: int):
        self.available_seconds = int(available_seconds)
        self.required_seconds = int(required_seconds)
        super().__init__(
            f"required={self.required_seconds}s available={self.available_seconds}s"
        )


class PaymentSnapshotError(RuntimeError):
    """An approval that would credit nothing because the snapshot is missing."""

    def __init__(self, payment_id: int):
        self.payment_id = int(payment_id)
        super().__init__(f"payment {payment_id} has no purchase snapshot")


class OpenPaymentError(RuntimeError):
    """The user already has an open request for a different plan."""

    def __init__(self, plan_code: str):
        self.plan_code = plan_code
        super().__init__(f"an open payment already exists for plan {plan_code}")


@dataclass(frozen=True, slots=True)
class UserRow:
    telegram_id: int
    joined_at: str
    banned: bool


@dataclass(frozen=True, slots=True)
class SubmissionRow:
    submission_id: int
    user_id: int
    kind: str
    filename: str | None
    status: str
    error: str | None
    duration_seconds: float | None
    created_at: str
    finished_at: str | None


class Database:
    """All persistence. One connection guarded by a write lock."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.connect() must be awaited first")
        return self._db

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._apply_migrations()
        await self._db.commit()
        self._secure_file_permissions()

    def _secure_file_permissions(self) -> None:
        """Best-effort ``0600`` on the database file and its WAL sidecars.

        ``__main__`` sets ``umask(077)``, but the importer, maintenance scripts
        and tests do not go through it; the file mode is the guarantee
        ``docs/SECURITY.md`` makes, so it is enforced here (audit F-12).
        """
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            try:
                if candidate.exists():
                    os.chmod(candidate, 0o600)
            except OSError:
                logger.warning("could not restrict permissions on %s", candidate)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _apply_migrations(self) -> None:
        if not MIGRATIONS_DIR.is_dir():
            raise RuntimeError(f"Missing migrations directory: {MIGRATIONS_DIR}")
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            row[0]
            for row in await self.db.execute_fetchall("SELECT name FROM schema_migrations")
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            logger.info("Applying migration %s", path.name)
            # SQLite lacks ADD COLUMN IF NOT EXISTS and executescript is not
            # transactional, so a crash midway through a migration must be safe
            # to retry (audit verification gap 7).
            raw_sql = path.read_text(encoding="utf-8")
            raw_sql = await self._without_applied_columns(raw_sql)
            await self.db.executescript(raw_sql)
            await self.db.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (path.name, iso(utc_now())),
            )
        await self.db.commit()

    async def _without_applied_columns(self, raw_sql: str) -> str:
        """Drop ``ADD COLUMN`` statements whose column already exists."""
        pattern = re.compile(
            r"(?i)^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+"
            r"[\"'`]?([A-Za-z_][A-Za-z0-9_]*)[\"'`]?"
        )
        known: dict[str, set[str]] = {}
        kept: list[str] = []
        for line in raw_sql.splitlines():
            match = pattern.match(line)
            if match:
                table, column = match.group(1), match.group(2)
                if table not in known:
                    rows = await self.db.execute_fetchall(f"PRAGMA table_info({table})")
                    known[table] = {row[1] for row in rows}
                if column in known[table]:
                    continue
            kept.append(line)
        return "\n".join(kept)

    # ------------------------------------------------------------------ write
    async def _write(self, sql: str, params: tuple = ()) -> None:
        async with self._write_lock:
            await self.db.execute(sql, params)
            await self.db.commit()

    async def _write_many(self, statements: list[tuple[str, tuple]]) -> None:
        async with self._write_lock:
            for sql, params in statements:
                await self.db.execute(sql, params)
            await self.db.commit()

    # ------------------------------------------------------------------ users
    async def ensure_user(self, telegram_id: int) -> UserRow:
        await self._write(
            "INSERT OR IGNORE INTO users(telegram_id, joined_at) VALUES (?, ?)",
            (telegram_id, iso(utc_now())),
        )
        row = await self.db.execute_fetchall(
            "SELECT telegram_id, joined_at, banned FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        if not row:
            raise RuntimeError(f"user {telegram_id} vanished after insert")
        return UserRow(row[0][0], row[0][1], bool(row[0][2]))

    async def set_banned(
        self, telegram_id: int, banned: bool, *, admin_id: int | None = None
    ) -> None:
        await self._write(
            "UPDATE users SET banned = ? WHERE telegram_id = ?", (1 if banned else 0, telegram_id)
        )
        if admin_id is not None:
            await self.audit(
                admin_id,
                "user_banned" if banned else "user_unbanned",
                "ban flag changed",
                telegram_id,
            )

    async def is_banned(self, telegram_id: int) -> bool:
        rows = await self.db.execute_fetchall(
            "SELECT banned FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        return bool(rows and rows[0][0])

    async def user_ids(self) -> list[int]:
        """Every user id, without the per-row balance work of ``list_users``."""
        rows = await self.db.execute_fetchall(
            "SELECT telegram_id FROM users ORDER BY telegram_id"
        )
        return [int(row[0]) for row in rows]

    async def list_users(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        rows = await self.db.execute_fetchall(
            "SELECT telegram_id, joined_at, banned, is_unlimited FROM users "
            "ORDER BY joined_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        result = []
        for row in rows:
            available = await self.available_seconds(row[0])
            result.append(
                {
                    "telegram_id": row[0],
                    "joined_at": row[1],
                    "banned": bool(row[2]),
                    "is_unlimited": bool(row[3]),
                    "available_seconds": available,
                }
            )
        return result

    async def is_unlimited(self, user_id: int) -> bool:
        rows = await self.db.execute_fetchall(
            "SELECT is_unlimited FROM users WHERE telegram_id = ?", (user_id,)
        )
        return bool(rows and rows[0][0])

    async def set_unlimited(
        self,
        user_id: int,
        enabled: bool,
        *,
        admin_id: int,
        reason: str = "",
    ) -> None:
        """Mark or unmark a special (unlimited) user, and audit the change."""
        now = iso(utc_now())
        async with self._write_lock:
            if enabled:
                await self.db.execute(
                    "UPDATE users SET is_unlimited = 1, unlimited_reason = ?, "
                    "unlimited_granted_by = ?, unlimited_granted_at = ? "
                    "WHERE telegram_id = ?",
                    (reason[:200], admin_id, now, user_id),
                )
            else:
                await self.db.execute(
                    "UPDATE users SET is_unlimited = 0, unlimited_reason = NULL, "
                    "unlimited_granted_by = NULL, unlimited_granted_at = NULL "
                    "WHERE telegram_id = ?",
                    (user_id,),
                )
            await self.db.commit()
        await self.audit(
            admin_id,
            "unlimited_on" if enabled else "unlimited_off",
            reason or "-",
            user_id,
        )

    async def unlimited_users(self) -> list[dict[str, Any]]:
        rows = await self.db.execute_fetchall(
            "SELECT telegram_id, unlimited_reason, unlimited_granted_by, unlimited_granted_at "
            "FROM users WHERE is_unlimited = 1 ORDER BY unlimited_granted_at DESC, telegram_id"
        )
        return [dict(row) for row in rows]

    async def count_users(self) -> int:
        row = await self.db.execute_fetchall("SELECT COUNT(*) FROM users")
        return int(row[0][0]) if row else 0

    # ----------------------------------------------------------- submissions
    async def create_submission(
        self, user_id: int, kind: str, filename: str | None
    ) -> int:
        cursor = await self._write_returning(
            "INSERT INTO submissions(user_id, kind, filename, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (user_id, kind, filename, iso(utc_now())),
        )
        return cursor

    async def _write_returning(self, sql: str, params: tuple = ()) -> int:
        async with self._write_lock:
            cursor = await self.db.execute(sql, params)
            await self.db.commit()
            return int(cursor.lastrowid or 0)

    async def set_submission_status(
        self,
        submission_id: int,
        status: str,
        *,
        error: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        finished = iso(utc_now()) if status in TERMINAL_SUBMISSION_STATUSES else None
        await self._write(
            "UPDATE submissions SET status = ?, error = ?, "
            "duration_seconds = COALESCE(?, duration_seconds), "
            "finished_at = COALESCE(?, finished_at) "
            "WHERE submission_id = ?",
            (status, error, duration_seconds, finished, submission_id),
        )

    async def save_transcription(
        self, submission_id: int, engine: str, text: str, notes_json: str
    ) -> None:
        await self._write(
            "INSERT INTO transcriptions(submission_id, engine, text, notes_json, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(submission_id) DO UPDATE SET engine = excluded.engine, "
            "text = excluded.text, notes_json = excluded.notes_json",
            (submission_id, engine, text, notes_json, iso(utc_now())),
        )

    async def transcription(self, submission_id: int) -> dict | None:
        """The persisted transcript (engine, text, notes JSON) for auditing."""
        rows = await self.db.execute_fetchall(
            "SELECT engine, text, notes_json FROM transcriptions "
            "WHERE submission_id = ?",
            (submission_id,),
        )
        if not rows:
            return None
        engine, text, notes_json = rows[0]
        return {"engine": engine, "text": text, "notes_json": notes_json}

    async def submission(self, submission_id: int) -> SubmissionRow | None:
        rows = await self.db.execute_fetchall(
            "SELECT submission_id, user_id, kind, filename, status, error, "
            "duration_seconds, created_at, finished_at FROM submissions "
            "WHERE submission_id = ?",
            (submission_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return SubmissionRow(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8])

    async def user_submissions(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.db.execute_fetchall(
            "SELECT submission_id, kind, filename, status, duration_seconds, created_at, "
            "finished_at FROM submissions WHERE user_id = ? "
            "ORDER BY submission_id DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(row) for row in rows]

    async def stats(self) -> dict[str, Any]:
        totals = await self.db.execute_fetchall(
            "SELECT status, COUNT(*) FROM submissions GROUP BY status"
        )
        by_status = {row[0]: int(row[1]) for row in totals}
        users = await self.count_users()
        seconds = await self.db.execute_fetchall(
            "SELECT COALESCE(SUM(duration_seconds), 0) FROM submissions WHERE status = 'done'"
        )
        # Revenue is the sum of what each user was actually quoted at purchase,
        # never the plan's current price (audit F-14).
        revenue = await self.db.execute_fetchall(
            "SELECT COALESCE(SUM(COALESCE(p.purchased_price, "
            "  (SELECT price FROM plans pl WHERE pl.code = p.plan_code), 0)), 0) "
            "FROM payments p WHERE p.status = 'approved'"
        )
        return {
            "users": users,
            "submissions_done": by_status.get("done", 0),
            "submissions_failed": by_status.get("failed", 0),
            "submissions_active": by_status.get("processing", 0) + by_status.get("pending", 0),
            "seconds_transcribed": float(seconds[0][0]) if seconds else 0.0,
            "toman_approved": int(revenue[0][0]) if revenue else 0,
        }

    async def recover_interrupted(self) -> int:
        """Startup recovery: fail interrupted work and release its credit holds.

        Returns the number of submissions recovered. A job is either fully
        recorded or fully failed — never silently dropped with a ``processing``
        row and a live hold left behind.
        """
        now = iso(utc_now())
        recovered = await self.db.execute_fetchall(
            "SELECT submission_id, user_id FROM submissions "
            "WHERE status IN ('pending', 'processing')"
        )
        async with self._write_lock:
            for row in recovered:
                await self.db.execute(
                    "UPDATE submissions SET status = 'failed', error = 'interrupted by restart', "
                    "finished_at = ? WHERE submission_id = ?",
                    (now, row[0]),
                )
                await self.db.execute(
                    "UPDATE holds SET released_at = ? "
                    "WHERE submission_id = ? AND released_at IS NULL",
                    (now, row[0]),
                )
            await self.db.commit()
        if recovered:
            logger.warning("Recovered %d interrupted submissions at startup", len(recovered))
        return len(recovered)

    # ---------------------------------------------------------------- billing
    async def available_seconds(self, user_id: int) -> int:
        """Credits(not expired) − debits − active holds, never below zero."""
        now = iso(utc_now())
        rows = await self.db.execute_fetchall(
            "SELECT "
            "COALESCE((SELECT SUM(seconds) FROM ledger "
            "  WHERE user_id = ? AND kind = 'credit' "
            "  AND (expires_at IS NULL OR expires_at > ?)), 0) "
            "- COALESCE((SELECT SUM(seconds) FROM ledger "
            "  WHERE user_id = ? AND kind = 'debit'), 0) "
            "- COALESCE((SELECT SUM(seconds) FROM holds "
            "  WHERE user_id = ? AND released_at IS NULL), 0)",
            (user_id, now, user_id, user_id),
        )
        return max(0, int(rows[0][0])) if rows else 0

    async def grant_free_plan_once(self, user_id: int, seconds: int) -> bool:
        """Grant the free plan the first time a user is seen.

        A single statement under the write lock: two concurrent ``/start``
        handlers can no longer both observe "no grant yet" and insert
        (audit F-04).
        """
        async with self._write_lock:
            cursor = await self.db.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, created_at) "
                "SELECT ?, ?, 'credit', 'free_plan', ? "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM ledger WHERE user_id = ? AND note = 'free_plan'"
                ")",
                (user_id, seconds, iso(utc_now()), user_id),
            )
            await self.db.commit()
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
        expires = (
            iso(utc_now() + timedelta(days=validity_days)) if validity_days else None
        )
        await self._write(
            "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
            "VALUES (?, ?, 'credit', ?, ?, ?)",
            (user_id, int(seconds), note, expires, iso(utc_now())),
        )
        if admin_id is not None:
            await self.audit(admin_id, "grant_credit", note, user_id)

    async def reserve(self, user_id: int, submission_id: int, seconds: int) -> None:
        """Reserve credit for a job, or raise :class:`InsufficientBalanceError`.

        Special (unlimited) users get no reservation and are never denied; the
        job's submission row still records its duration for the audit trail.
        """
        if seconds <= 0:
            raise ValueError("reservation must be positive")
        async with self._write_lock:
            # Re-check inside the lock to close TOCTOU with set_unlimited
            # happening concurrently (audit F-M01).
            rows = await self.db.execute_fetchall(
                "SELECT is_unlimited FROM users WHERE telegram_id = ?", (user_id,)
            )
            if rows and rows[0][0]:
                return
            available = await self._available_seconds_locked(user_id)
            if available < seconds:
                raise InsufficientBalanceError(available, seconds)
            await self.db.execute(
                "INSERT INTO holds(submission_id, user_id, seconds, created_at) "
                "VALUES (?, ?, ?, ?)",
                (submission_id, user_id, seconds, iso(utc_now())),
            )
            await self.db.commit()

    async def _available_seconds_locked(self, user_id: int) -> int:
        now = iso(utc_now())
        rows = await self.db.execute_fetchall(
            "SELECT "
            "COALESCE((SELECT SUM(seconds) FROM ledger "
            "  WHERE user_id = ? AND kind = 'credit' "
            "  AND (expires_at IS NULL OR expires_at > ?)), 0) "
            "- COALESCE((SELECT SUM(seconds) FROM ledger "
            "  WHERE user_id = ? AND kind = 'debit'), 0) "
            "- COALESCE((SELECT SUM(seconds) FROM holds "
            "  WHERE user_id = ? AND released_at IS NULL), 0)",
            (user_id, now, user_id, user_id),
        )
        return max(0, int(rows[0][0])) if rows else 0

    async def settle_hold(self, submission_id: int, measured_seconds: int) -> None:
        """Charge the measured duration and close the hold (success path)."""
        rows = await self.db.execute_fetchall(
            "SELECT user_id, seconds FROM holds "
            "WHERE submission_id = ? AND released_at IS NULL",
            (submission_id,),
        )
        if not rows:
            owner = await self.db.execute_fetchall(
                "SELECT u.is_unlimited FROM submissions s "
                "JOIN users u ON u.telegram_id = s.user_id "
                "WHERE s.submission_id = ?",
                (submission_id,),
            )
            if owner and owner[0][0]:
                logger.debug("special user job not billed submission=%s", submission_id)
                return
            logger.warning("settle_hold found no active hold submission=%s", submission_id)
            return
        user_id, reserved = int(rows[0][0]), int(rows[0][1])
        if await self.is_unlimited(user_id):
            # Made special while the job was running: release, never charge.
            await self.release_hold(submission_id, "unlimited user")
            return
        charge = max(0, min(int(measured_seconds), reserved))
        now = iso(utc_now())
        await self._write_many(
            [
                (
                    "INSERT INTO ledger(user_id, seconds, kind, note, created_at) "
                    "VALUES (?, ?, 'debit', ?, ?)",
                    (user_id, charge, f"submission:{submission_id}", now),
                ),
                (
                    "UPDATE holds SET released_at = ?, settled = 1 WHERE submission_id = ?",
                    (now, submission_id),
                ),
            ]
        )

    async def release_hold(self, submission_id: int, reason: str) -> bool:
        """Release a hold untouched (failure/cancellation path). Exactly once.

        The update takes the write lock like every other write: committing
        outside it would also commit whatever another coroutine had pending on
        this shared connection, which can make a debit durable while its hold
        is still open (audit F-03).
        """
        async with self._write_lock:
            cursor = await self.db.execute(
                "UPDATE holds SET released_at = ?, settled = 0, release_reason = ? "
                "WHERE submission_id = ? AND released_at IS NULL",
                (iso(utc_now()), reason[:200], submission_id),
            )
            await self.db.commit()
            return bool(cursor.rowcount)

    async def credit_history(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.db.execute_fetchall(
            "SELECT seconds, kind, note, expires_at, created_at FROM ledger "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ plans
    async def sync_plans(self, catalog: list[dict[str, Any]]) -> None:
        """Upsert the tariff catalog; admin-created custom plans are untouched."""
        statements: list[tuple[str, tuple]] = []
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
        rows = await self.db.execute_fetchall(
            "SELECT code, hours, price, validity_days, is_custom, active FROM plans "
            "WHERE active = 1 ORDER BY price"
        )
        return [dict(row) for row in rows]

    async def plan(self, code: str) -> dict[str, Any] | None:
        rows = await self.db.execute_fetchall(
            "SELECT code, hours, price, validity_days, is_custom, active FROM plans "
            "WHERE code = ? AND active = 1",
            (code,),
        )
        return dict(rows[0]) if rows else None

    # --------------------------------------------------------------- payments
    async def create_payment(self, user_id: int, plan_code: str) -> int:
        """Create a payment with the purchase terms frozen onto the row.

        The approval credits this snapshot rather than the live plan row, so a
        plan deactivated or re-priced between purchase and review cannot change
        what the user paid for (audit F-02/F-14). A second tap on the same plan
        returns the existing open request; a different plan is refused because
        only one request per user may be open (audit F-16).
        """
        async with self._write_lock:
            plan = await self.db.execute_fetchall(
                "SELECT hours, price, validity_days FROM plans "
                "WHERE code = ? AND active = 1",
                (plan_code,),
            )
            if not plan:
                raise ValueError(f"plan {plan_code} is not on sale")
            hours, price, validity_days = plan[0]
            seconds = int(round(float(hours) * 3600))
            open_rows = await self.db.execute_fetchall(
                "SELECT payment_id, plan_code FROM payments "
                "WHERE user_id = ? AND status = 'pending' "
                "ORDER BY payment_id LIMIT 1",
                (user_id,),
            )
            if open_rows:
                if str(open_rows[0][1]) == plan_code:
                    return int(open_rows[0][0])
                raise OpenPaymentError(str(open_rows[0][1]))
            cursor = await self.db.execute(
                "INSERT INTO payments(user_id, plan_code, status, created_at, "
                "purchased_seconds, purchased_price, purchased_validity_days) "
                "VALUES (?, ?, 'pending', ?, ?, ?, ?)",
                (user_id, plan_code, iso(utc_now()), seconds, int(price), validity_days),
            )
            await self.db.commit()
            return int(cursor.lastrowid or 0)

    async def set_payment_receipt(self, payment_id: int, receipt_path: str) -> None:
        await self._write(
            "UPDATE payments SET receipt_path = ? WHERE payment_id = ?",
            (receipt_path, payment_id),
        )

    _PAYMENT_COLUMNS = (
        "payment_id, user_id, plan_code, status, receipt_path, created_at, "
        "reviewed_at, reviewed_by, purchased_seconds, purchased_price, "
        "purchased_validity_days, review_reason"
    )

    async def payment(self, payment_id: int) -> dict[str, Any] | None:
        rows = await self.db.execute_fetchall(
            f"SELECT {self._PAYMENT_COLUMNS} FROM payments WHERE payment_id = ?",
            (payment_id,),
        )
        return dict(rows[0]) if rows else None

    async def pending_payments(self) -> list[dict[str, Any]]:
        rows = await self.db.execute_fetchall(
            f"SELECT {self._PAYMENT_COLUMNS} FROM payments WHERE status = 'pending' "
            "ORDER BY payment_id"
        )
        return [dict(row) for row in rows]

    async def review_payment(
        self,
        payment_id: int,
        approved: bool,
        admin_id: int,
        *,
        reason: str = "",
    ) -> bool:
        """Approve (crediting the purchase snapshot) or reject. Exactly once.

        The audit row is written in the same transaction as the status change:
        an approved payment can never exist without its audit entry, and a
        rejection always carries the admin's reason (audit F-15).
        """
        if not approved and not reason.strip():
            raise ValueError("rejecting a payment requires a reason")
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                "SELECT status, user_id, plan_code, purchased_seconds, "
                "purchased_validity_days FROM payments WHERE payment_id = ?",
                (payment_id,),
            )
            if not rows or rows[0][0] != "pending":
                return False
            user_id, plan_code = int(rows[0][1]), str(rows[0][2])
            seconds = int(rows[0][3] or 0)
            validity_days = rows[0][4]
            if approved and seconds <= 0:
                # Never report success while crediting nothing.
                raise PaymentSnapshotError(payment_id)
            now = iso(utc_now())
            await self.db.execute(
                "UPDATE payments SET status = ?, reviewed_at = ?, reviewed_by = ?, "
                "review_reason = ? WHERE payment_id = ?",
                (
                    "approved" if approved else "rejected",
                    now,
                    admin_id,
                    reason.strip()[:400] or None,
                    payment_id,
                ),
            )
            if approved:
                expires = (
                    iso(utc_now() + timedelta(days=int(validity_days)))
                    if validity_days
                    else None
                )
                await self.db.execute(
                    "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
                    "VALUES (?, ?, 'credit', ?, ?, ?)",
                    (user_id, seconds, f"plan:{plan_code}", expires, now),
                )
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
            await self.db.commit()
        return True

    async def payment_history(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.db.execute_fetchall(
            "SELECT payment_id, plan_code, status, created_at, reviewed_at FROM payments "
            "WHERE user_id = ? ORDER BY payment_id DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ audit
    async def audit(
        self, admin_id: int, action: str, detail: str, subject_user: int | None = None
    ) -> None:
        await self._write(
            "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (admin_id, action, detail[:400], subject_user, iso(utc_now())),
        )

    async def recent_audit(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = await self.db.execute_fetchall(
            "SELECT admin_id, action, detail, subject_user, created_at FROM audit "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]
