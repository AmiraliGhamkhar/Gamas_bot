"""SQLite storage for Gamas Bot v3.

Eight tables, one connection, WAL mode, ``BEGIN IMMEDIATE`` writes. The v2
database is not migrated in place: v3 opens a fresh file (``DB_PATH``, default
``data/gamas_v3.db``) and :mod:`scripts.import_v2` can copy users, credit and
the ledger across from a v2 database.

Billing model — three append-only facts and one derived balance:

* ``ledger`` rows of kind ``credit`` (seconds granted, with optional expiry);
* ``ledger`` rows of kind ``debit`` (seconds consumed by a finished job);
* ``holds`` rows (seconds reserved by a running job, released on settle).

``available = sum(unexpired credit lot remainders minus active allocations)``. A job
reserves before it runs and settles at raw-transcript delivery. Failures before
that milestone release the hold. Later booklet failures are recorded as partial
success and retain the transcription charge.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "v3"

TERMINAL_SUBMISSION_STATUSES = ("done", "partial", "failed", "rejected")

ADD_COLUMN_RE = re.compile(
    r"(?i)^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+"
    r"[\"'`]?([A-Za-z_][A-Za-z0-9_]*)[\"'`]?"
)


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


class UserBannedError(ValueError):
    """A blocked user cannot start billable work."""


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


class TooManyActiveJobsError(RuntimeError):
    """Admission control: this user already has the maximum active work."""

    def __init__(self, limit: int):
        self.limit = int(limit)
        super().__init__(f"user already has {self.limit} active submission(s)")


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
        self._read_db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.connect() must be awaited first")
        return self._db

    @property
    def read_db(self) -> aiosqlite.Connection:
        """Read-only WAL connection so concurrent reads never stall behind writes."""
        if self._read_db is not None:
            return self._read_db
        return self.db

    async def connect(self) -> None:
        await asyncio.to_thread(
            self.path.parent.mkdir, parents=True, exist_ok=True
        )
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA temp_store=MEMORY")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._apply_migrations()
        await self._db.commit()
        await self._backfill_allocations()
        if str(self.path) != ":memory:":
            self._read_db = await aiosqlite.connect(self.path)
            self._read_db.row_factory = aiosqlite.Row
            await self._read_db.execute("PRAGMA query_only=ON")
            await self._read_db.execute("PRAGMA temp_store=MEMORY")
            await self._read_db.execute("PRAGMA busy_timeout=5000")
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
        if self._read_db is not None:
            await self._read_db.close()
            self._read_db = None
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
        known: dict[str, set[str]] = {}
        kept: list[str] = []
        for line in raw_sql.splitlines():
            match = ADD_COLUMN_RE.match(line)
            if match:
                table, column = match.group(1), match.group(2)
                if table not in known:
                    rows = await self.db.execute_fetchall(f"PRAGMA table_info({table})")
                    known[table] = {row[1] for row in rows}
                if column in known[table]:
                    continue
            kept.append(line)
        return "\n".join(kept)

    @asynccontextmanager
    async def _transaction(self):
        """Serialize writers, including across connections; rollback partial work.

        Cancellation also rolls back before another coroutine can use the shared
        connection. Helpers called inside this context must not open another one.
        """
        async with self._write_lock:
            if self.db.in_transaction:
                await self.db.rollback()
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                yield
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def _credit_lots(
        self,
        user_id: int,
        at: str,
        before_id: int | None = None,
        *,
        conn: aiosqlite.Connection | None = None,
    ):
        handle = conn if conn is not None else self.db
        return await handle.execute_fetchall(
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

    async def _backfill_allocations(self):
        """Replay legacy usage once, without ever charging future purchases.

        Old records do not identify the consumed lot. Use earliest-expiry-first
        at the event time. Unfunded historical debits remain in the ledger for
        audit but cannot become debt against future grants.
        """
        async with self._transaction():
            if await self.db.execute_fetchall(
                "SELECT 1 FROM billing_migrations WHERE name = 'credit_lots_v1'"
            ):
                return
            events = await self.db.execute_fetchall(
                "SELECT l.id, l.user_id, l.seconds, COALESCE("
                "(SELECT h.created_at FROM holds h WHERE l.note = "
                "'submission:' || h.submission_id AND h.settled = 1), l.created_at) "
                "AS created_at, 'debit' AS kind FROM ledger l WHERE l.kind = 'debit' "
                "AND NOT EXISTS (SELECT 1 FROM debit_allocations a WHERE a.debit_id = l.id) "
                "UNION ALL "
                "SELECT submission_id, user_id, seconds, created_at, 'hold' "
                "FROM holds WHERE released_at IS NULL ORDER BY created_at, id"
            )
            for event in events:
                remaining = int(event['seconds'])
                for credit_id, available in await self._credit_lots(
                    event['user_id'], event['created_at'],
                    event['id'] if event['kind'] == 'debit' else None
                ):
                    used = min(remaining, max(0, available))
                    if used:
                        table, key = (('debit_allocations', 'debit_id')
                                      if event['kind'] == 'debit'
                                      else ('hold_allocations', 'submission_id'))
                        await self.db.execute(
                            f"INSERT INTO {table}({key}, credit_id, seconds) VALUES (?, ?, ?)",
                            (event['id'], credit_id, used),
                        )
                        remaining -= used
                    if not remaining:
                        break
                if remaining:
                    logger.warning("legacy billing event has %s unallocated seconds: %s %s",
                                   remaining, event['kind'], event['id'])
            # Historical settled jobs reached the old raw-delivery milestone.
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

    # ------------------------------------------------------------------ write
    async def _write(self, sql: str, params: tuple = ()) -> None:
        async with self._transaction():
            await self.db.execute(sql, params)

    async def _write_many(self, statements: list[tuple[str, tuple]]) -> None:
        async with self._transaction():
            for sql, params in statements:
                await self.db.execute(sql, params)

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
        now = iso(utc_now())
        async with self._transaction():
            await self.db.execute(
                "UPDATE users SET banned = ? WHERE telegram_id = ?",
                (int(banned), telegram_id),
            )
            if admin_id is not None:
                await self.db.execute(
                    "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (admin_id, "user_banned" if banned else "user_unbanned",
                     "ban flag changed", telegram_id, now),
                )

    async def is_banned(self, telegram_id: int) -> bool:
        rows = await self.read_db.execute_fetchall(
            "SELECT banned FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        return bool(rows and rows[0][0])

    async def user_ids(self) -> list[int]:
        """Every user id, without the per-row balance work of ``list_users``."""
        rows = await self.read_db.execute_fetchall(
            "SELECT telegram_id FROM users ORDER BY telegram_id"
        )
        return [int(row[0]) for row in rows]

    async def list_users(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        now = iso(utc_now())
        rows = await self.read_db.execute_fetchall(
            "SELECT u.telegram_id, u.joined_at, u.banned, u.is_unlimited, "
            "COALESCE(("
            "  SELECT SUM(MAX(0, l.seconds "
            "    - COALESCE((SELECT SUM(a.seconds) FROM debit_allocations a "
            "                WHERE a.credit_id = l.id), 0) "
            "    - COALESCE((SELECT SUM(a.seconds) FROM hold_allocations a "
            "                JOIN holds h ON h.submission_id = a.submission_id "
            "                WHERE a.credit_id = l.id AND h.released_at IS NULL), 0)"
            "  )) "
            "  FROM ledger l "
            "  WHERE l.user_id = u.telegram_id AND l.kind = 'credit' "
            "    AND julianday(l.created_at) <= julianday(?) "
            "    AND (l.expires_at IS NULL OR julianday(l.expires_at) > julianday(?))"
            "), 0) AS available_seconds "
            "FROM users u "
            "ORDER BY u.joined_at DESC LIMIT ? OFFSET ?",
            (now, now, limit, offset),
        )
        return [
            {
                "telegram_id": row[0],
                "joined_at": row[1],
                "banned": bool(row[2]),
                "is_unlimited": bool(row[3]),
                "available_seconds": int(row[4]),
            }
            for row in rows
        ]

    async def is_unlimited(self, user_id: int) -> bool:
        rows = await self.read_db.execute_fetchall(
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
        async with self._transaction():
            if enabled:
                await self.db.execute(
                    "UPDATE users SET is_unlimited = 1, unlimited_reason = ?, "
                    "unlimited_granted_by = ?, unlimited_granted_at = ? "
                    "WHERE telegram_id = ?",
                    (reason[:200], admin_id, now, user_id),
                )
                await self.db.execute(
                    "UPDATE holds SET released_at = ?, settled = 0, "
                    "release_reason = 'unlimited user' WHERE user_id = ? AND released_at IS NULL",
                    (now, user_id),
                )
            else:
                await self.db.execute(
                    "UPDATE users SET is_unlimited = 0, unlimited_reason = NULL, "
                    "unlimited_granted_by = NULL, unlimited_granted_at = NULL "
                    "WHERE telegram_id = ?",
                    (user_id,),
                )
            await self.db.execute(
                "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (admin_id, "unlimited_on" if enabled else "unlimited_off",
                 reason or "-", user_id, now),
            )

    async def unlimited_users(self) -> list[dict[str, Any]]:
        rows = await self.read_db.execute_fetchall(
            "SELECT telegram_id, unlimited_reason, unlimited_granted_by, unlimited_granted_at "
            "FROM users WHERE is_unlimited = 1 ORDER BY unlimited_granted_at DESC, telegram_id"
        )
        return [dict(row) for row in rows]

    async def count_users(self) -> int:
        row = await self.read_db.execute_fetchall("SELECT COUNT(*) FROM users")
        return int(row[0][0]) if row else 0

    # ----------------------------------------------------------- submissions
    async def create_submission(
        self,
        user_id: int,
        kind: str,
        filename: str | None,
        *,
        active_limit: int | None = None,
    ) -> int:
        """Insert a pending submission, optionally enforcing a per-user cap.

        The count and the insert share one write transaction: two uploads that
        arrive back-to-back cannot both observe the same free slot.
        """
        async with self._transaction():
            if active_limit is not None:
                rows = await self.db.execute_fetchall(
                    "SELECT COUNT(*) FROM submissions WHERE user_id = ? "
                    "AND status IN ('pending', 'processing')",
                    (user_id,),
                )
                if rows and int(rows[0][0]) >= int(active_limit):
                    raise TooManyActiveJobsError(int(active_limit))
            cursor = await self.db.execute(
                "INSERT INTO submissions(user_id, kind, filename, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (user_id, kind, filename, iso(utc_now())),
            )
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
            "UPDATE submissions SET status = CASE WHEN ? = 'failed' AND delivered_at IS NOT NULL "
            "THEN 'partial' ELSE ? END, error = ?, "
            "duration_seconds = COALESCE(?, duration_seconds), "
            "finished_at = COALESCE(?, finished_at) "
            "WHERE submission_id = ?",
            (status, status, error, duration_seconds, finished, submission_id),
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

    async def expire_transcriptions(
        self, *, older_than_days: int, batch: int = 200
    ) -> int:
        """Delete at most ``batch`` finished transcripts older than the window.

        The ``transcriptions`` table grows forever (one row per finished job,
        both text and the notes JSON), so a long-lived deployment pays for every
        lecture it has ever transcribed. Only rows of *finished* submissions are
        touched -- including ``partial`` ones, whose booklet failed after the raw
        transcript was delivered -- because a pending or processing job's row is
        what a retry or a support ticket reads, and deleting it mid-flight would
        lose the only copy of the work while it is still being delivered.

        The caller loops over batches, so a large backlog is deleted in bounded
        transactions instead of one long write lock.
        """
        if older_than_days <= 0:
            return 0
        cutoff = iso(utc_now() - timedelta(days=older_than_days))
        rows = await self.db.execute_fetchall(
            "SELECT t.submission_id FROM transcriptions t "
            "JOIN submissions s ON s.submission_id = t.submission_id "
            "WHERE t.created_at < ? AND s.status IN "
            "('done', 'partial', 'failed', 'rejected') "
            "ORDER BY t.created_at LIMIT ?",
            (cutoff, max(1, batch)),
        )
        ids = [row[0] for row in rows]
        if not ids:
            return 0
        await self._write_many(
            [
                ("DELETE FROM transcriptions WHERE submission_id = ?", (submission_id,))
                for submission_id in ids
            ]
        )
        return len(ids)

    async def transcription(self, submission_id: int) -> dict | None:
        """The persisted transcript (engine, text, notes JSON) for auditing."""
        rows = await self.read_db.execute_fetchall(
            "SELECT engine, text, notes_json FROM transcriptions "
            "WHERE submission_id = ?",
            (submission_id,),
        )
        if not rows:
            return None
        engine, text, notes_json = rows[0]
        return {"engine": engine, "text": text, "notes_json": notes_json}

    async def submission(self, submission_id: int) -> SubmissionRow | None:
        rows = await self.read_db.execute_fetchall(
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
        rows = await self.read_db.execute_fetchall(
            "SELECT submission_id, kind, filename, status, duration_seconds, created_at, "
            "finished_at FROM submissions WHERE user_id = ? "
            "ORDER BY submission_id DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(row) for row in rows]

    async def stats(self) -> dict[str, Any]:
        totals = await self.read_db.execute_fetchall(
            "SELECT status, COUNT(*) FROM submissions GROUP BY status"
        )
        by_status = {row[0]: int(row[1]) for row in totals}
        users = await self.count_users()
        seconds = await self.read_db.execute_fetchall(
            "SELECT COALESCE(SUM(duration_seconds), 0) FROM submissions WHERE delivered_at IS NOT NULL OR status = 'done'"
        )
        # Revenue is the sum of what each user was actually quoted at purchase,
        # never the plan's current price (audit F-14).
        revenue = await self.read_db.execute_fetchall(
            "SELECT COALESCE(SUM(COALESCE(p.purchased_price, "
            "  (SELECT price FROM plans pl WHERE pl.code = p.plan_code), 0)), 0) "
            "FROM payments p WHERE p.status = 'approved'"
        )
        return {
            "users": users,
            "submissions_done": by_status.get("done", 0),
            "submissions_failed": by_status.get("failed", 0) + by_status.get("partial", 0),
            "submissions_partial": by_status.get("partial", 0),
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
        async with self._transaction():
            recovered = await self.db.execute_fetchall(
                "SELECT submission_id, user_id FROM submissions "
                "WHERE status IN ('pending', 'processing')"
            )
            for row in recovered:
                await self.db.execute(
                    "UPDATE submissions SET status = CASE WHEN delivered_at IS NOT NULL "
                    "THEN 'partial' ELSE 'failed' END, error = 'interrupted by restart', "
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
            logger.warning("Recovered %d interrupted submissions at startup", len(recovered))
        return len(recovered)

    # ---------------------------------------------------------------- billing
    async def available_seconds(self, user_id: int) -> int:
        """Unexpired lot remainders, excluding their active reservations."""
        if self._read_db is not None:
            return sum(
                max(0, int(row[1]))
                for row in await self._credit_lots(
                    user_id, iso(utc_now()), conn=self._read_db
                )
            )
        async with self._write_lock:
            return await self._available_seconds_locked(user_id)

    async def grant_free_plan_once(self, user_id: int, seconds: int) -> bool:
        """Grant the free plan the first time a user is seen.

        A single statement under the write lock: two concurrent ``/start``
        handlers can no longer both observe "no grant yet" and insert
        (audit F-04).
        """
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, created_at) "
                "SELECT ?, ?, 'credit', 'free_plan', ? "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM ledger WHERE user_id = ? AND note = 'free_plan'"
                ")",
                (user_id, seconds, iso(utc_now()), user_id),
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
        expires = (
            iso(utc_now() + timedelta(days=validity_days)) if validity_days else None
        )
        async with self._transaction():
            await self.db.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
                "VALUES (?, ?, 'credit', ?, ?, ?)",
                (user_id, int(seconds), note, expires, iso(utc_now())),
            )
            if admin_id is not None:
                await self.db.execute(
                    "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
                    "VALUES (?, 'grant_credit', ?, ?, ?)",
                    (admin_id, note, user_id, iso(utc_now())),
                )

    async def reserve(self, user_id: int, submission_id: int, seconds: int) -> None:
        """Reserve credit for a job, or raise :class:`InsufficientBalanceError`.

        Special (unlimited) users get no reservation and are never denied; the
        job's submission row still records its duration for the audit trail.
        """
        if seconds <= 0:
            raise ValueError("reservation must be positive")
        async with self._transaction():
            # Re-check inside the lock to close TOCTOU with set_unlimited
            # happening concurrently (audit F-M01).
            rows = await self.db.execute_fetchall(
                "SELECT is_unlimited, banned FROM users WHERE telegram_id = ?", (user_id,)
            )
            if rows and rows[0][1]:
                raise UserBannedError("⛔ دسترسی شما به ربات مسدود شده است.")
            owner = await self.db.execute_fetchall(
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
            await self.db.execute(
                "INSERT INTO holds(submission_id, user_id, seconds, created_at) "
                "VALUES (?, ?, ?, ?)",
                (submission_id, user_id, seconds, iso(utc_now())),
            )
            remaining = seconds
            for credit_id, available in await self._credit_lots(user_id, iso(utc_now())):
                used = min(remaining, max(0, available))
                if used:
                    await self.db.execute(
                        "INSERT INTO hold_allocations(submission_id, credit_id, seconds) "
                        "VALUES (?, ?, ?)", (submission_id, credit_id, used),
                    )
                    remaining -= used
                if not remaining:
                    break
            if remaining:
                raise InsufficientBalanceError(seconds - remaining, seconds)


    async def _available_seconds_locked(self, user_id: int) -> int:
        return sum(max(0, int(row[1])) for row in
                   await self._credit_lots(user_id, iso(utc_now())))

    async def settle_hold(self, submission_id: int, measured_seconds: int) -> None:
        """Exactly-once billing and raw-delivery milestone in one transaction."""
        async with self._transaction():
            rows = await self.db.execute_fetchall(
                "SELECT h.user_id, h.seconds, u.is_unlimited FROM holds h "
                "JOIN users u ON u.telegram_id = h.user_id "
                "WHERE h.submission_id = ? AND h.released_at IS NULL",
                (submission_id,),
            )
            now = iso(utc_now())
            if not rows:
                # Unlimited jobs intentionally have no hold. A released or
                # already-settled paid hold must never be charged again.
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
                allocations = await self.db.execute_fetchall(
                    "SELECT a.credit_id, a.seconds FROM hold_allocations a "
                    "JOIN ledger l ON l.id = a.credit_id WHERE a.submission_id = ? "
                    "ORDER BY l.expires_at IS NULL, l.expires_at, l.id", (submission_id,),
                )
                charge = max(0, min(int(measured_seconds), reserved))
                # A legacy hold may be underfunded; never charge an unrelated lot.
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
                            "VALUES (?, ?, ?)", (cursor.lastrowid, credit_id, used),
                        )
                        remaining -= used
            await self.db.execute(
                "UPDATE holds SET released_at = ?, settled = ?, release_reason = ? "
                "WHERE submission_id = ? AND released_at IS NULL",
                (now, int(not unlimited), "unlimited user" if unlimited else None, submission_id),
            )
            await self.db.execute(
                "UPDATE submissions SET delivered_at = COALESCE(delivered_at, ?) "
                "WHERE submission_id = ?", (now, submission_id),
            )

    async def release_hold(self, submission_id: int, reason: str) -> bool:
        """Release a hold untouched (failure/cancellation path). Exactly once.

        The update takes the write lock like every other write: committing
        outside it would also commit whatever another coroutine had pending on
        this shared connection, which can make a debit durable while its hold
        is still open (audit F-03).
        """
        async with self._transaction():
            cursor = await self.db.execute(
                "UPDATE holds SET released_at = ?, settled = 0, release_reason = ? "
                "WHERE submission_id = ? AND released_at IS NULL",
                (iso(utc_now()), reason[:200], submission_id),
            )
            return bool(cursor.rowcount)

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
    # Payment states: awaiting_payment -> pending_review -> approved/rejected,
    # with cancelled as user-initiated terminal. Legacy 'pending' is treated as
    # awaiting_payment (no receipt) or pending_review (with receipt) for compat.
    PAYMENT_OPEN_STATUSES = ("awaiting_payment", "pending_review", "pending")
    PAYMENT_PENDING_REVIEW_STATUSES = ("pending_review", "pending")

    async def create_payment(self, user_id: int, plan_code: str) -> int:
        """Create a payment with the purchase terms frozen onto the row.

        The approval credits this snapshot rather than the live plan row, so a
        plan deactivated or re-priced between purchase and review cannot change
        what the user paid for (audit F-02/F-14). A second tap on the same plan
        returns the existing open request; a different plan is refused because
        only one request per user may be open (audit F-16).
        """
        async with self._transaction():
            open_rows = await self.db.execute_fetchall(
                "SELECT payment_id, plan_code FROM payments "
                "WHERE user_id = ? AND status IN ('pending','awaiting_payment','pending_review') "
                "ORDER BY payment_id LIMIT 1",
                (user_id,),
            )
            if open_rows:
                if str(open_rows[0][1]) == plan_code:
                    return int(open_rows[0][0])
                raise OpenPaymentError(str(open_rows[0][1]))
            plan = await self.db.execute_fetchall(
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
                (user_id, plan_code, iso(utc_now()), seconds, int(price), validity_days),
            )
            payment_id = int(cursor.lastrowid or 0)
        logger.info(
            "payment state transition payment_id=%s plan_code=%s status=awaiting_payment",
            payment_id,
            plan_code,
        )
        return payment_id

    async def cancel_payment(self, payment_id: int, user_id: int) -> bool:
        """Only the owner may cancel an unsubmitted purchase."""
        async with self._transaction():
            cursor = await self.db.execute(
                "UPDATE payments SET status = 'cancelled', reviewed_at = ?, "
                "review_reason = 'cancelled by owner before receipt' "
                "WHERE payment_id = ? AND user_id = ? AND status IN ('pending','awaiting_payment') "
                "AND receipt_path IS NULL", (iso(utc_now()), payment_id, user_id),
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
        """Store one receipt and move an owned order into admin review.

        ``user_id`` is optional only for compatibility with older internal
        callers and migrations. Telegram handlers always supply it, so a
        forged or stale order id cannot attach a receipt to another user's
        order even if the surrounding handler changes later.
        """
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
            logger.info("payment receipt stored payment_id=%s status=pending_review", payment_id)
        return changed

    async def forget_receipts(self, paths: tuple[str, ...] | list[str]) -> int:
        """Clear stored receipt references whose files were deleted.

        The retention job removes old image files; leaving ``receipt_path``
        behind made the admin's «مشاهدهٔ رسید» button point at nothing and left
        the deletion unauditable. Both are fixed here, in one transaction.
        """
        cleaned = tuple(dict.fromkeys(path for path in paths if path))
        if not cleaned:
            return 0
        now = iso(utc_now())
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

    _PAYMENT_COLUMNS = (
        "payment_id, user_id, plan_code, status, receipt_path, created_at, "
        "reviewed_at, reviewed_by, purchased_seconds, purchased_price, "
        "purchased_validity_days, review_reason, "
        "(SELECT l.id FROM ledger l WHERE l.payment_id = payments.payment_id "
        "AND l.kind = 'credit') AS credit_ledger_id"
    )

    async def payment(self, payment_id: int) -> dict[str, Any] | None:
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments WHERE payment_id = ?",
                (payment_id,),
            )
            return dict(rows[0]) if rows else None

    async def open_awaiting_payment_for_user(self, user_id: int) -> dict[str, Any] | None:
        """Return the user's open unsubmitted order awaiting a receipt, if any."""
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments "
                "WHERE user_id = ? AND status IN ('awaiting_payment', 'pending') "
                "AND receipt_path IS NULL ORDER BY payment_id DESC LIMIT 1",
                (user_id,),
            )
            return dict(rows[0]) if rows else None

    async def pending_payments(self) -> list[dict[str, Any]]:
        # For backward compat, includes legacy 'pending' and new explicit states.
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments WHERE status IN ('pending','awaiting_payment','pending_review') "
                "ORDER BY payment_id"
            )
            return [dict(row) for row in rows]

    async def awaiting_payments(self) -> list[dict[str, Any]]:
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments WHERE status = 'awaiting_payment' "
                "ORDER BY payment_id"
            )
            return [dict(row) for row in rows]

    async def pending_review_payments(
        self, *, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Receipt-backed orders ready for review, oldest first.

        ``limit`` is optional for compatibility with maintenance callers that
        need the complete set. Telegram screens always pass a small bounded
        limit so a backlog cannot produce an oversized message or keyboard.
        """
        query = (
            f"SELECT {self._PAYMENT_COLUMNS} FROM payments "
            "WHERE status IN ('pending_review','pending') AND receipt_path IS NOT NULL "
            "ORDER BY payment_id"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params = (max(0, int(limit)), max(0, int(offset)))
        async with self._write_lock:
            rows = await self.db.execute_fetchall(query, params)
            return [dict(row) for row in rows]

    async def count_pending_review_payments(self) -> int:
        """Count reviewable orders without loading an unbounded backlog."""
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                "SELECT COUNT(*) FROM payments "
                "WHERE status IN ('pending_review','pending') AND receipt_path IS NOT NULL"
            )
            return int(rows[0][0]) if rows else 0

    async def all_payments(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments ORDER BY payment_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return [dict(row) for row in rows]

    async def payment_history_paginated(self, user_id: int, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments WHERE user_id = ? ORDER BY payment_id DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            )
            return [dict(row) for row in rows]

    async def count_payments_by_status(self) -> dict[str, int]:
        async with self._write_lock:
            rows = await self.db.execute_fetchall("SELECT status, COUNT(*) FROM payments GROUP BY status")
            return {row[0]: int(row[1]) for row in rows}

    async def review_payment(
        self,
        payment_id: int,
        approved: bool,
        admin_id: int,
        *,
        reason: str = "",
    ) -> bool:
        """Review a receipt-backed order and grant its entitlement at most once.

        Only ``pending_review`` (or a legacy ``pending`` row with a receipt)
        can be decided. Approval, the credit insert and the audit record are one
        transaction. The unique ``ledger.payment_id`` index added by migration
        006 is a second line of defence against duplicate grants across worker
        retries or multiple database connections.

        If any activation write fails, the whole transaction rolls back and the
        order stays reviewable; an administrator can safely retry after fixing
        the database problem. No partial ``approved`` state is left behind.
        """
        if not approved and not reason.strip():
            raise ValueError("rejecting a payment requires a reason")
        async with self._transaction():
            rows = await self.db.execute_fetchall(
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
                # Never report success while crediting nothing or accepting an
                # incomplete amount snapshot. The stored plan code and snapshot
                # are authoritative; a live, possibly re-priced plan is not.
                raise PaymentSnapshotError(payment_id)
            now = iso(utc_now())
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
                    iso(utc_now() + timedelta(days=int(validity_days)))
                    if validity_days
                    else None
                )
                await self.db.execute(
                    "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at, "
                    "payment_id) VALUES (?, ?, 'credit', ?, ?, ?, ?)",
                    (user_id, seconds, f"plan:{plan_code}", expires, now, payment_id),
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
        logger.info(
            "payment reviewed payment_id=%s decision=%s",
            payment_id,
            "approved" if approved else "rejected",
        )
        return True

    async def payment_history(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                "SELECT payment_id, plan_code, status, receipt_path, created_at, reviewed_at FROM payments "
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
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                "SELECT admin_id, action, detail, subject_user, created_at FROM audit "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in rows]

    # ----------------------------------------------------------------- support tickets
    #: Fixed categories shown to the user (callback key -> stored value).
    SUPPORT_CATEGORIES = ("payment", "technical", "subscription", "other")
    SUPPORT_STATUSES = ("open", "awaiting_admin", "awaiting_user", "closed")
    SUPPORT_SUBJECT_MAX = 400
    SUPPORT_MESSAGE_MAX = 4000

    #: The single place where ticket status changes are defined. Every handler
    #: goes through :meth:`_transition`; no caller writes a status directly.
    #: ``event`` -> status it leads to, for the status the ticket is in now.
    SUPPORT_TRANSITIONS = {
        "create": {None: "open"},
        "user_message": {"open": "awaiting_admin", "awaiting_admin": "awaiting_admin",
                         "awaiting_user": "awaiting_admin"},
        "admin_message": {"open": "awaiting_user", "awaiting_admin": "awaiting_user",
                          "awaiting_user": "awaiting_user"},
        "close": {"open": "closed", "awaiting_admin": "closed", "awaiting_user": "closed"},
        "reopen": {"closed": "open"},
    }

    @classmethod
    def _transition(cls, event: str, current: str | None) -> str:
        """The status an event leads to, or :class:`TicketStateError`."""
        target = cls.SUPPORT_TRANSITIONS.get(event, {}).get(current)
        if target is None:
            raise TicketStateError(event, current)
        return target

    @staticmethod
    def _ticket_public_id(seq_id: int) -> str:
        return f"T-{seq_id:06d}"

    async def create_ticket(
        self,
        user_id: int,
        category: str,
        subject: str,
        *,
        initial_message: str | None = None,
        telegram_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a ticket, optionally with its first message, atomically.

        The Telegram flow supplies ``initial_message``. A failure writing that
        message therefore rolls back the ticket and public-id sequence insert
        too, instead of leaving an empty conversation with no admin alert.
        Direct service callers may still create an empty ``open`` ticket and
        add messages later.
        """
        if category not in self.SUPPORT_CATEGORIES:
            raise ValueError(f"invalid category {category}")
        subject = " ".join(subject.split())
        if not 2 <= len(subject) <= self.SUPPORT_SUBJECT_MAX:
            raise ValueError("subject length out of range")
        if initial_message is not None:
            initial_message = initial_message.strip()
            if not 1 <= len(initial_message) <= self.SUPPORT_MESSAGE_MAX:
                raise ValueError("message length out of range")
        now = iso(utc_now())
        status = self._transition("create", None)
        async with self._transaction():
            cursor = await self.db.execute("INSERT INTO support_ticket_seq DEFAULT VALUES")
            public_id = self._ticket_public_id(int(cursor.lastrowid or 0))
            cursor = await self.db.execute(
                "INSERT INTO support_tickets(public_ticket_id, user_id, category, subject, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (public_id, user_id, category, subject, status, now, now),
            )
            ticket_id = int(cursor.lastrowid or 0)
            if initial_message is not None:
                target = self._transition("user_message", status)
                await self.db.execute(
                    "INSERT INTO support_messages(ticket_id, sender_id, sender_role, "
                    "message_text, telegram_message_id, created_at) "
                    "VALUES (?, ?, 'user', ?, ?, ?)",
                    (ticket_id, user_id, initial_message, telegram_message_id, now),
                )
                await self.db.execute(
                    "UPDATE support_tickets SET status = ?, updated_at = ? WHERE id = ?",
                    (target, now, ticket_id),
                )
                status = target
        logger.info(
            "support ticket state transition ticket_id=%s public_id=%s category=%s status=%s",
            ticket_id,
            public_id,
            category,
            status,
        )
        return {
            "id": ticket_id, "public_ticket_id": public_id, "user_id": user_id,
            "category": category, "subject": subject, "status": status,
            "created_at": now, "updated_at": now, "closed_at": None,
        }

    async def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                "SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)
            )
            return dict(rows[0]) if rows else None

    async def get_ticket_by_public_id(self, public_id: str) -> dict[str, Any] | None:
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                "SELECT * FROM support_tickets WHERE public_ticket_id = ?", (public_id,)
            )
            return dict(rows[0]) if rows else None

    async def list_user_tickets(self, user_id: int, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                "SELECT * FROM support_tickets WHERE user_id = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            )
            return [dict(row) for row in rows]

    #: Admin list filters: ``active`` is every ticket not closed. ``open`` is
    #: the new/open work queue: freshly created tickets already carry their
    #: first user message and are therefore ``awaiting_admin``; reopened ones
    #: remain ``open`` until the next message.
    SUPPORT_FILTERS = {
        "active": "status != 'closed'",
        "open": "status IN ('open', 'awaiting_admin')",
        "wait": "status = 'awaiting_admin'",
        "user": "status = 'awaiting_user'",
        "closed": "status = 'closed'",
        "all": "1 = 1",
    }

    async def list_tickets(
        self, *, view: str = "active", category: str | None = None,
        limit: int = 10, offset: int = 0,
    ) -> list[dict[str, Any]]:
        clause = self.SUPPORT_FILTERS.get(view)
        if clause is None:
            raise ValueError(f"unknown view {view}")
        params: list[Any] = []
        if category is not None:
            if category not in self.SUPPORT_CATEGORIES:
                raise ValueError(f"invalid category {category}")
            clause += " AND category = ?"
            params.append(category)
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                f"SELECT * FROM support_tickets WHERE {clause} "
                "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            return [dict(row) for row in rows]

    async def count_tickets_by_status(self) -> dict[str, int]:
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                "SELECT status, COUNT(*) FROM support_tickets GROUP BY status"
            )
            return {row[0]: int(row[1]) for row in rows}

    async def add_ticket_message(
        self,
        ticket_id: int,
        sender_id: int,
        sender_role: str,
        message_text: str,
        telegram_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Persist one message and advance the ticket. Ownership is checked here.

        A user may only write to a ticket they own; a closed ticket must be
        reopened first, so no message can land in a conversation nobody watches.
        """
        if sender_role not in ("user", "admin"):
            raise ValueError("invalid role")
        message_text = message_text.strip()
        if not 1 <= len(message_text) <= self.SUPPORT_MESSAGE_MAX:
            raise ValueError("message length out of range")
        now = iso(utc_now())
        async with self._transaction():
            rows = await self.db.execute_fetchall(
                "SELECT user_id, status FROM support_tickets WHERE id = ?", (ticket_id,)
            )
            if not rows:
                raise ValueError("ticket not found")
            owner_id, current = int(rows[0][0]), str(rows[0][1])
            if sender_role == "user" and owner_id != int(sender_id):
                raise PermissionError("user cannot write to another user's ticket")
            event = "user_message" if sender_role == "user" else "admin_message"
            target = self._transition(event, current)
            cursor = await self.db.execute(
                "INSERT INTO support_messages(ticket_id, sender_id, sender_role, message_text, "
                "telegram_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ticket_id, sender_id, sender_role, message_text, telegram_message_id, now),
            )
            message_id = int(cursor.lastrowid or 0)
            await self.db.execute(
                "UPDATE support_tickets SET status = ?, updated_at = ? WHERE id = ?",
                (target, now, ticket_id),
            )
        logger.info(
            "support ticket state transition ticket_id=%s role=%s from_status=%s status=%s",
            ticket_id,
            sender_role,
            current,
            target,
        )
        return {
            "id": message_id, "ticket_id": ticket_id, "sender_id": sender_id,
            "sender_role": sender_role, "message_text": message_text,
            "created_at": now, "status": target, "owner_id": owner_id,
        }

    async def ticket_messages(self, ticket_id: int, *, limit: int = 6, offset: int = 0) -> list[dict[str, Any]]:
        """One page of a history, newest first offset, returned oldest-first."""
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                "SELECT * FROM (SELECT * FROM support_messages WHERE ticket_id = ? "
                "ORDER BY id DESC LIMIT ? OFFSET ?) ORDER BY id ASC",
                (ticket_id, limit, offset),
            )
            return [dict(row) for row in rows]

    async def count_ticket_messages(self, ticket_id: int) -> int:
        async with self._write_lock:
            rows = await self.db.execute_fetchall(
                "SELECT COUNT(*) FROM support_messages WHERE ticket_id = ?", (ticket_id,)
            )
            return int(rows[0][0]) if rows else 0

    async def close_ticket(self, ticket_id: int, actor_id: int, *, admin: bool) -> bool:
        """Close a ticket. Returns False when it is already closed."""
        now = iso(utc_now())
        async with self._transaction():
            rows = await self.db.execute_fetchall(
                "SELECT user_id, status FROM support_tickets WHERE id = ?", (ticket_id,)
            )
            if not rows:
                raise ValueError("ticket not found")
            owner_id, current = int(rows[0][0]), str(rows[0][1])
            if not admin and owner_id != int(actor_id):
                raise PermissionError("user cannot close another user's ticket")
            if current == "closed":
                return False
            target = self._transition("close", current)
            await self.db.execute(
                "UPDATE support_tickets SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                (target, now, now, ticket_id),
            )
            if admin:
                await self.db.execute(
                    "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
                    "VALUES (?, 'ticket_closed', ?, ?, ?)",
                    (actor_id, f"ticket:{ticket_id}", owner_id, now),
                )
        logger.info(
            "support ticket state transition ticket_id=%s role=%s from_status=%s status=%s",
            ticket_id,
            "admin" if admin else "user",
            current,
            target,
        )
        return True

    async def reopen_ticket(self, ticket_id: int, actor_id: int, *, admin: bool) -> bool:
        """Reopen a closed ticket; its earlier messages are kept untouched."""
        now = iso(utc_now())
        async with self._transaction():
            rows = await self.db.execute_fetchall(
                "SELECT user_id, status FROM support_tickets WHERE id = ?", (ticket_id,)
            )
            if not rows:
                raise ValueError("ticket not found")
            owner_id, current = int(rows[0][0]), str(rows[0][1])
            if not admin and owner_id != int(actor_id):
                raise PermissionError("user cannot reopen another user's ticket")
            if current != "closed":
                return False
            target = self._transition("reopen", current)
            await self.db.execute(
                "UPDATE support_tickets SET status = ?, closed_at = NULL, updated_at = ? WHERE id = ?",
                (target, now, ticket_id),
            )
            if admin:
                await self.db.execute(
                    "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
                    "VALUES (?, 'ticket_reopened', ?, ?, ?)",
                    (actor_id, f"ticket:{ticket_id}", owner_id, now),
                )
        logger.info(
            "support ticket state transition ticket_id=%s role=%s from_status=%s status=%s",
            ticket_id,
            "admin" if admin else "user",
            current,
            target,
        )
        return True


class TicketStateError(RuntimeError):
    """An event that the ticket's current status does not allow."""

    def __init__(self, event: str, current: str | None):
        self.event = event
        self.current = current
        super().__init__(f"ticket event {event!r} not allowed in status {current!r}")
