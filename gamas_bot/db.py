"""SQLite storage for Gamas Bot v3.

One WAL database, serialized ``BEGIN IMMEDIATE`` writes, and numbered additive
migrations. The library stores Telegram message references only; it never
copies generated artifacts to local storage. Free Office exports use the same
library index as booklets and transcripts. The v2 database is not migrated in
place: v3 opens a fresh file (``DB_PATH``, default
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
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .metrics import METRICS

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "v3"

#: Every application table a fully migrated database must contain (the
#: ``schema_migrations`` bookkeeping table excluded). CI's migration gate and
#: ``tests/test_migration_schema.py`` both compare against this one set, so a
#: new migration that adds a table cannot silently drift from the gate.
APPLICATION_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "submissions",
        "transcriptions",
        "plans",
        "payments",
        "ledger",
        "holds",
        "audit",
        "debit_allocations",
        "hold_allocations",
        "billing_migrations",
        "support_tickets",
        "support_messages",
        "support_ticket_seq",
        # Migration 010 (library), 012 (learning loop) and 013 (admin hubs,
        # free first try, documents).
        "library_items",
        "quiz_sessions",
        "quiz_questions",
        "quiz_answers",
        "flashcard_decks",
        "flashcards",
        "free_uses",
        "documents",
        "learning_charges",
    }
)


async def _rows(
    connection: aiosqlite.Connection,
    sql: str,
    parameters: Iterable[Any] | None = None,
) -> list[sqlite3.Row]:
    """Fetch every row of ``sql`` as a list.

    ``aiosqlite`` 0.22 types ``execute_fetchall`` as returning a bare
    ``Iterable`` although it always returns the fully materialised list from
    ``Cursor.fetchall()``. Callers index into and measure the result, so the
    widening happens once here instead of a cast at every call site.
    """
    return list(await connection.execute_fetchall(sql, parameters))

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
    first_name: str = ""
    username: str = ""


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
        await self._db.execute("PRAGMA foreign_keys=ON")
        problems = await self.user_reference_integrity()
        if problems:
            # Some old databases predate a plan catalog and sync_plans runs
            # immediately after connect. Report legacy inconsistencies without
            # blocking that repair; deployment/CI can run the strict assertion.
            logger.warning("SQLite reference integrity: %s", "; ".join(problems[:20]))
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
            for row in await _rows(self.db, "SELECT name FROM schema_migrations")
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
                    rows = await _rows(self.db, f"PRAGMA table_info({table})")
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
            except BaseException as exc:
                await self.db.rollback()
                if isinstance(exc, sqlite3.Error):
                    # Counted for the admin health panel («خطاهای پایگاه‌داده») so a
                    # failing disk/lock does not stay invisible outside the log.
                    METRICS.record_db_error()
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
        return await _rows(handle,
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
            if await _rows(self.db,
                "SELECT 1 FROM billing_migrations WHERE name = 'credit_lots_v1'"
            ):
                return
            events = await _rows(self.db,
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
    async def ensure_user(
        self,
        telegram_id: int,
        first_name: str | None = None,
        *,
        username: str | None = None,
        last_name: str | None = None,
    ) -> UserRow:
        """Upsert the user and refresh the identity fields we were given.

        ``telegram_id`` is bound as a Python ``int`` and the column is SQLite
        ``INTEGER``, so a 64-bit Telegram id (well above 2**53) round-trips
        exactly: nothing here converts it to float or formats it as a string.
        A double ``/start`` therefore hits the primary key instead of creating a
        second row, and a username change overwrites the previous value.
        """
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int):
            raise TypeError("Telegram user IDs must be exact integers")
        if not -(1 << 63) <= telegram_id < (1 << 63):
            raise ValueError("Telegram user ID is outside SQLite's int64 range")
        now = iso(utc_now())
        await self._write(
            "INSERT OR IGNORE INTO users(telegram_id, joined_at, last_seen_at) "
            "VALUES (?, ?, ?)",
            (telegram_id, now, now),
        )
        await self.update_identity(
            telegram_id,
            first_name=first_name,
            username=username,
            last_name=last_name,
        )
        row = await _rows(self.db,
            "SELECT telegram_id, joined_at, banned, first_name, username "
            "FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        if not row:
            raise RuntimeError(f"user {telegram_id} vanished after insert")
        return UserRow(
            int(row[0][0]),
            row[0][1],
            bool(row[0][2]),
            str(row[0][3] or ""),
            str(row[0][4] or ""),
        )

    async def update_identity(
        self,
        user_id: int,
        *,
        first_name: str | None = None,
        username: str | None = None,
        last_name: str | None = None,
    ) -> None:
        """Refresh the latest-known identity and stamp ``last_seen_at``.

        ``username=None`` means "Telegram did not tell us this time", so the
        stored value is kept; an empty string means "the user has no username
        now" and clears it. Names are bounded because they are rendered in the
        admin panel and on the public leaderboard.
        """
        sets = ["last_seen_at = ?"]
        params: list[Any] = [iso(utc_now())]
        if first_name is not None:
            sets.append("first_name = ?")
            params.append(" ".join(str(first_name).split())[:64])
        if last_name is not None:
            sets.append("last_name = ?")
            params.append(" ".join(str(last_name).split())[:64])
        if username is not None:
            cleaned = " ".join(str(username).split()).lstrip("@")[:64]
            sets.append("username = ?")
            params.append(cleaned or None)
        params.append(int(user_id))
        await self._write(
            f"UPDATE users SET {', '.join(sets)} WHERE telegram_id = ?",
            tuple(params),
        )

    async def set_first_name(self, user_id: int, first_name: str) -> None:
        """Store the bounded display name used by the public leaderboard."""
        bounded = " ".join(str(first_name).split())[:64]
        if not bounded:
            return
        await self._write(
            "UPDATE users SET first_name = ? WHERE telegram_id = ?",
            (bounded, int(user_id)),
        )

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
        rows = await _rows(self.read_db,
            "SELECT banned FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        return bool(rows and rows[0][0])

    async def set_subscription_state(
        self,
        telegram_id: int,
        status: str,
        *,
        verified_at: str | None = None,
    ) -> None:
        """Record the last observed membership status (observability only).

        The gate never trusts this value: :meth:`SubscriptionService` re-checks
        Telegram on every protected interaction. Writing it on every check keeps
        the admin panel honest without deleting or restricting any user.
        """
        await self._write(
            "UPDATE users SET subscription_status = ?, subscription_verified_at = ? "
            "WHERE telegram_id = ?",
            (str(status), verified_at, telegram_id),
        )

    async def user_ids(self) -> list[int]:
        """Every user id, without the per-row balance work of ``list_users``."""
        rows = await _rows(self.read_db,
            "SELECT telegram_id FROM users ORDER BY telegram_id"
        )
        return [int(row[0]) for row in rows]

    async def list_users(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        now = iso(utc_now())
        rows = await _rows(self.read_db,
            "SELECT u.telegram_id, u.joined_at, u.banned, u.is_unlimited, "
            "COALESCE(u.username, '') AS username, "
            "COALESCE(u.first_name, '') AS first_name, "
            "COALESCE(u.last_name, '') AS last_name, "
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
                "telegram_id": int(row[0]),
                "joined_at": row[1],
                "banned": bool(row[2]),
                "is_unlimited": bool(row[3]),
                "username": str(row["username"] or ""),
                "first_name": str(row["first_name"] or ""),
                "last_name": str(row["last_name"] or ""),
                "available_seconds": int(row["available_seconds"] or 0),
            }
            for row in rows
        ]

    async def search_users(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Find users by exact numeric id or by ``@username`` (case-insensitive).

        Both lookups are parameterised and return the same row shape as
        ``list_users``, so every admin screen can render one identity line
        without branching on how the user was found.
        """
        token = " ".join(str(query or "").split()).lstrip("@")
        if not token:
            return []
        columns = (
            "u.telegram_id, u.joined_at, u.banned, u.is_unlimited, "
            "COALESCE(u.username, '') AS username, "
            "COALESCE(u.first_name, '') AS first_name, "
            "COALESCE(u.last_name, '') AS last_name, "
            "u.last_seen_at"
        )
        if token.lstrip("+-").isdigit():
            try:
                numeric_id = int(token)
            except ValueError:
                return []
            if not -(1 << 63) <= numeric_id < (1 << 63):
                return []
            rows = await _rows(
                self.read_db,
                f"SELECT {columns} FROM users u WHERE u.telegram_id = ?",
                (numeric_id,),
            )
        else:
            rows = await _rows(
                self.read_db,
                f"SELECT {columns} FROM users u "
                "WHERE LOWER(COALESCE(u.username, '')) = LOWER(?) "
                "ORDER BY u.last_seen_at DESC, u.telegram_id LIMIT ?",
                (token, int(limit)),
            )
        return [
            {
                "telegram_id": int(row[0]),
                "joined_at": row[1],
                "banned": bool(row[2]),
                "is_unlimited": bool(row[3]),
                "username": str(row[4] or ""),
                "first_name": str(row[5] or ""),
                "last_name": str(row[6] or ""),
                "last_seen_at": row[7],
            }
            for row in rows
        ]

    async def blocked_users(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Users currently blocked from the bot, newest identity first."""
        rows = await _rows(
            self.read_db,
            "SELECT telegram_id, COALESCE(username, ''), COALESCE(first_name, ''), "
            "COALESCE(last_name, ''), banned, last_seen_at FROM users "
            "WHERE banned = 1 ORDER BY last_seen_at DESC, telegram_id DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        )
        return [
            {
                "telegram_id": int(row[0]),
                "username": str(row[1] or ""),
                "first_name": str(row[2] or ""),
                "last_name": str(row[3] or ""),
                "banned": bool(row[4]),
                "last_seen_at": row[5],
            }
            for row in rows
        ]

    # -------------------------------------------------------------- free uses
    FREE_USE_COLUMNS: dict[str, str] = {
        "deck": "free_deck_used",
        "quiz": "free_quiz_used",
    }
    FREE_CLAIM_COLUMNS: dict[str, str] = {"deck": "deck_claim", "quiz": "quiz_claim"}

    def _free_use_column(self, kind: str) -> str:
        column = self.FREE_USE_COLUMNS.get(str(kind))
        if column is None:
            raise ValueError(f"unknown free-use kind {kind!r}")
        return column

    async def free_use_state(self, user_id: int) -> dict[str, bool]:
        """Which free first tries this user still has (missing row = both free)."""
        rows = await _rows(
            self.read_db,
            "SELECT free_deck_used, free_quiz_used FROM free_uses WHERE user_id = ?",
            (int(user_id),),
        )
        if not rows:
            return {"deck": True, "quiz": True}
        return {"deck": not int(rows[0][0]), "quiz": not int(rows[0][1])}

    async def consume_free_use(
        self, user_id: int, kind: str, *, claim_id: str | None = None,
        require_limited: bool = False,
    ) -> bool:
        """Claim with one conditional UPDATE, optionally marking it in-flight.

        A unique claim token lets a failure refund only *its own* attempt.  This
        matters if an admin resets the gift while an older generation runs.
        Calls without a token represent an already-finalized use, preserving the
        original API for administrative callers and existing databases.
        ``require_limited`` rechecks account status in the same transaction as
        a generation's claim, closing the race with an administrator's update.
        """
        column = self._free_use_column(kind)
        claim_column = self.FREE_CLAIM_COLUMNS[kind]
        now = iso(utc_now())
        async with self._transaction():
            await self.db.execute(
                "INSERT OR IGNORE INTO free_uses(user_id, free_deck_used, "
                "free_quiz_used, updated_at) VALUES (?, 0, 0, ?)",
                (int(user_id), now),
            )
            guard = (
                " AND EXISTS (SELECT 1 FROM users WHERE telegram_id = ? "
                "AND is_unlimited = 0 AND banned = 0)"
                if require_limited else ""
            )
            params: tuple[Any, ...] = (claim_id, now, int(user_id))
            if require_limited:
                params += (int(user_id),)
            cursor = await self.db.execute(
                f"UPDATE free_uses SET {column} = 1, {claim_column} = ?, updated_at = ? "
                f"WHERE user_id = ? AND {column} = 0{guard}",
                params,
            )
            return cursor.rowcount == 1

    async def refund_free_use(
        self, user_id: int, kind: str, *, claim_id: str | None = None
    ) -> bool:
        """Restore only the matching claim, never a newer post-reset attempt."""
        column = self._free_use_column(kind)
        claim_column = self.FREE_CLAIM_COLUMNS[kind]
        async with self._transaction():
            cursor = await self.db.execute(
                f"UPDATE free_uses SET {column} = 0, {claim_column} = NULL, updated_at = ? "
                f"WHERE user_id = ? AND {column} = 1 AND {claim_column} IS ?",
                (iso(utc_now()), int(user_id), claim_id),
            )
            return cursor.rowcount == 1

    async def _finalize_free_use_locked(
        self, user_id: int, kind: str, claim_id: str | None
    ) -> None:
        """Finalize a matching claim inside the artifact's write transaction.

        A reset may have invalidated this token; in that case the newly granted
        free use remains available instead of being consumed by an older job.
        """
        if claim_id is None:
            return
        column = self._free_use_column(kind)
        claim_column = self.FREE_CLAIM_COLUMNS[kind]
        await self.db.execute(
            f"UPDATE free_uses SET {claim_column} = NULL, updated_at = ? "
            f"WHERE user_id = ? AND {column} = 1 AND {claim_column} = ?",
            (iso(utc_now()), int(user_id), claim_id),
        )

    async def recover_pending_free_uses(self) -> int:
        """Restore unfinished claims at bot startup, after its instance lock.

        Saved artifacts and claim finalization share a transaction, so a claim
        still carrying a token after a crash cannot be a completed generation.
        """
        recovered = 0
        async with self._transaction():
            for kind, column in self.FREE_USE_COLUMNS.items():
                claim_column = self.FREE_CLAIM_COLUMNS[kind]
                cursor = await self.db.execute(
                    f"UPDATE free_uses SET {column} = 0, {claim_column} = NULL, "
                    f"updated_at = ? WHERE {claim_column} IS NOT NULL",
                    (iso(utc_now()),),
                )
                recovered += max(0, cursor.rowcount)
        return recovered

    async def reset_free_uses(
        self, user_id: int, *, admin_id: int, detail: str = "free gift reset"
    ) -> None:
        """Admin action: restore both free first tries and audit the change."""
        now = iso(utc_now())
        async with self._transaction():
            await self.db.execute(
                "INSERT OR IGNORE INTO free_uses(user_id, free_deck_used, "
                "free_quiz_used, updated_at) VALUES (?, 0, 0, ?)",
                (int(user_id), now),
            )
            await self.db.execute(
                "UPDATE free_uses SET free_deck_used = 0, free_quiz_used = 0, "
                "deck_claim = NULL, quiz_claim = NULL, updated_at = ? WHERE user_id = ?",
                (now, int(user_id)),
            )
            await self.db.execute(
                "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (admin_id, "free_use_reset", str(detail)[:200], int(user_id), now),
            )

    async def is_unlimited(self, user_id: int) -> bool:
        rows = await _rows(self.read_db,
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
        rows = await _rows(self.read_db,
            "SELECT telegram_id, unlimited_reason, unlimited_granted_by, unlimited_granted_at "
            "FROM users WHERE is_unlimited = 1 ORDER BY unlimited_granted_at DESC, telegram_id"
        )
        return [dict(row) for row in rows]

    async def count_users(self) -> int:
        row = await _rows(self.read_db, "SELECT COUNT(*) FROM users")
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
                rows = await _rows(self.db,
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
        error_kind: str | None = None,
    ) -> None:
        """Move a submission through its lifecycle, recording reporting facts.

        Two facts are stamped here rather than at every call site:

        * ``started_at`` on the first transition to ``processing`` — it is the
          only record of when the worker actually picked the job up, and it is
          what makes queue wait and processing time measurable per job;
        * ``error_kind``, the bounded failure category the admin report groups
          by (``COALESCE`` keeps the first value, so a terminal category is
          never overwritten by a later, vaguer one).
        """
        now = iso(utc_now())
        finished = now if status in TERMINAL_SUBMISSION_STATUSES else None
        started = now if status == "processing" else None
        await self._write(
            "UPDATE submissions SET status = CASE WHEN ? = 'failed' AND delivered_at IS NOT NULL "
            "THEN 'partial' ELSE ? END, error = ?, "
            "error_kind = COALESCE(error_kind, ?), "
            "duration_seconds = COALESCE(?, duration_seconds), "
            "started_at = CASE WHEN ? = 'processing' THEN COALESCE(started_at, ?) ELSE started_at END, "
            "finished_at = COALESCE(?, finished_at) "
            "WHERE submission_id = ?",
            (
                status, status, error, error_kind, duration_seconds,
                status, started, finished, submission_id,
            ),
        )

    async def save_transcription(
        self, submission_id: int, engine: str, text: str, notes_json: str
    ) -> None:
        """Store the transcript and remember which engine produced it.

        The engine is duplicated onto ``submissions`` in the same transaction:
        the retention sweep deletes transcript rows, and provider-usage
        reporting must outlive that (see migration 009).
        """
        await self._write_many(
            [
                (
                    "INSERT INTO transcriptions(submission_id, engine, text, notes_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(submission_id) DO UPDATE SET engine = excluded.engine, "
                    "text = excluded.text, notes_json = excluded.notes_json",
                    (submission_id, engine, text, notes_json, iso(utc_now())),
                ),
                (
                    "UPDATE submissions SET stt_engine = COALESCE(?, stt_engine) "
                    "WHERE submission_id = ?",
                    (engine or None, submission_id),
                ),
            ]
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
        rows = await _rows(self.db,
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
        rows = await _rows(self.read_db,
            "SELECT engine, text, notes_json FROM transcriptions "
            "WHERE submission_id = ?",
            (submission_id,),
        )
        if not rows:
            return None
        engine, text, notes_json = rows[0]
        return {"engine": engine, "text": text, "notes_json": notes_json}

    async def submission(self, submission_id: int) -> SubmissionRow | None:
        rows = await _rows(self.read_db,
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
        rows = await _rows(self.read_db,
            "SELECT submission_id, kind, filename, status, duration_seconds, created_at, "
            "finished_at FROM submissions WHERE user_id = ? "
            "ORDER BY submission_id DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- library
    async def library_add(
        self,
        user_id: int,
        kind: str,
        chat_id: int,
        message_id: int,
        submission_id: int | None = None,
        *,
        title: str = "",
    ) -> int:
        """Save a Telegram message reference, not a local copy of the artifact.

        The message is written only after Telegram accepted its delivery. Its
        source chat and message id are enough for a later ``forward_messages``;
        the library therefore adds no persistent file storage or retention risk.
        """
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO library_items(user_id, submission_id, kind, chat_id, "
                "message_id, title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    int(user_id),
                    submission_id,
                    kind,
                    int(chat_id),
                    int(message_id),
                    str(title),
                    iso(utc_now()),
                ),
            )
            return int(cursor.lastrowid or 0)

    async def library_list(
        self, user_id: int, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return one newest-first page, always scoped to the requesting user."""
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable FROM library_items WHERE user_id = ? "
            "ORDER BY item_id DESC LIMIT ? OFFSET ?",
            (int(user_id), max(0, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]

    async def library_item(self, user_id: int, item_id: int) -> dict[str, Any] | None:
        """Fetch one artifact only when it belongs to ``user_id``."""
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable FROM library_items "
            "WHERE user_id = ? AND item_id = ?",
            (int(user_id), int(item_id)),
        )
        return dict(rows[0]) if rows else None

    async def library_item_detail(
        self, user_id: int, item_id: int
    ) -> dict[str, Any] | None:
        """Fetch one artifact *with* its archived learning text.

        The list/forward queries deliberately omit ``content_text`` (up to
        60 KB per row) so paging never hauls whole booklets; the learning
        screens are the only readers that need it.
        """
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable, content_text FROM library_items "
            "WHERE user_id = ? AND item_id = ?",
            (int(user_id), int(item_id)),
        )
        return dict(rows[0]) if rows else None

    async def library_set_content(self, item_id: int, content_text: str) -> None:
        """Persist the bounded plain text a quiz/flashcard run is grounded in.

        Best-effort by contract: callers wrap this in the same delivery
        try/except boundary as the library insert so it can never fail a send.
        """
        await self._write(
            "UPDATE library_items SET content_text = ? WHERE item_id = ?",
            (str(content_text), int(item_id)),
        )

    async def library_count(self, user_id: int) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM library_items WHERE user_id = ?",
            (int(user_id),),
        )
        return int(rows[0][0]) if rows else 0

    async def library_mark_unavailable(self, item_id: int) -> bool:
        """Remember that Telegram no longer retains a referenced source message."""
        async with self._transaction():
            cursor = await self.db.execute(
                "UPDATE library_items SET unavailable = 1 WHERE item_id = ?",
                (int(item_id),),
            )
            return cursor.rowcount == 1

    async def library_mark_available(self, item_id: int) -> None:
        """Clear an old unavailable flag after Telegram successfully forwards it."""
        await self._write(
            "UPDATE library_items SET unavailable = 0 WHERE item_id = ?", (int(item_id),)
        )

    async def library_prune(self, user_id: int, keep: int) -> int:
        """Keep newest references plus any source needed by an active quiz.

        Quiz rows cascade when their source disappears. Retain active sources so
        a new delivery cannot erase a paid, unfinished quiz; the next prune can
        remove them after the session ends or expires.
        """
        keep = max(0, int(keep))
        async with self._transaction():
            cursor = await self.db.execute(
                "DELETE FROM library_items WHERE user_id = ? AND item_id NOT IN ("
                "SELECT item_id FROM library_items WHERE user_id = ? "
                "ORDER BY item_id DESC LIMIT ?) "
                "AND NOT EXISTS (SELECT 1 FROM quiz_sessions q "
                "WHERE q.user_id = library_items.user_id AND q.item_id = library_items.item_id "
                "AND q.status = 'active')",
                (int(user_id), int(user_id), keep),
            )
            return max(0, int(cursor.rowcount))

    async def stats(self) -> dict[str, Any]:
        totals = await _rows(self.read_db,
            "SELECT status, COUNT(*) FROM submissions GROUP BY status"
        )
        by_status = {row[0]: int(row[1]) for row in totals}
        users = await self.count_users()
        seconds = await _rows(self.read_db,
            "SELECT COALESCE(SUM(duration_seconds), 0) FROM submissions WHERE delivered_at IS NOT NULL OR status = 'done'"
        )
        # Revenue is the sum of what each user was actually quoted at purchase,
        # never the plan's current price (audit F-14).
        revenue = await _rows(self.read_db,
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

    # ---------------------------------------------------------- learning: quiz
    async def quiz_admission(
        self,
        user_id: int,
        *,
        max_active_hours: int,
        max_sessions_per_hour: int,
    ) -> str | None:
        """Serialise the start-quiz guards in one write transaction.

        Returns ``None`` when the user may start, or a machine reason:
        ``"active"`` (an unfinished quiz exists) or ``"rate"`` (the hourly
        session cap is reached). Stale active sessions are expired here first,
        so a quiz abandoned mid-flow never blocks the user forever.
        """
        now = utc_now()
        cutoff = iso(now - timedelta(hours=max_active_hours))
        hour_ago = iso(now - timedelta(hours=1))
        async with self._transaction():
            await self.db.execute(
                "UPDATE quiz_sessions SET status = 'expired', updated_at = ? "
                "WHERE user_id = ? AND status = 'active' AND updated_at < ?",
                (iso(now), int(user_id), cutoff),
            )
            active = await _rows(self.db,
                "SELECT 1 FROM quiz_sessions WHERE user_id = ? AND status = 'active' "
                "LIMIT 1",
                (int(user_id),),
            )
            if active:
                return "active"
            recent = await _rows(self.db,
                "SELECT COUNT(*) FROM quiz_sessions WHERE user_id = ? "
                "AND julianday(created_at) >= julianday(?)",
                (int(user_id), hour_ago),
            )
            if recent and int(recent[0][0]) >= int(max_sessions_per_hour):
                return "rate"
            return None

    async def quiz_create_session(
        self,
        user_id: int,
        item_id: int,
        questions: list[dict[str, Any]],
        *,
        free_claim_id: str | None = None,
        paid_charge_id: str | None = None,
    ) -> int:
        """Insert the questions and finalize their learning budget atomically."""
        if not questions:
            raise ValueError("quiz session needs at least one question")
        now = iso(utc_now())
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO quiz_sessions(user_id, item_id, question_count, "
                "current_index, score, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, 0, 'active', ?, ?)",
                (int(user_id), int(item_id), len(questions), now, now),
            )
            session_id = int(cursor.lastrowid or 0)
            for idx, question in enumerate(questions):
                await self.db.execute(
                    "INSERT INTO quiz_questions(session_id, idx, question, "
                    "options_json, correct_index, explanation, source, difficulty) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        idx,
                        str(question["question"]),
                        json.dumps(question["options"], ensure_ascii=False),
                        int(question["correct_index"]),
                        str(question.get("explanation", "")),
                        str(question.get("source", ""))[:120],
                        str(question.get("difficulty", ""))[:20],
                    ),
                )
            await self._finalize_free_use_locked(user_id, "quiz", free_claim_id)
            await self._finalize_learning_charge_locked(user_id, paid_charge_id)
            return session_id

    async def quiz_get_session(
        self, session_id: int, user_id: int
    ) -> dict[str, Any] | None:
        """Fetch a session only when it belongs to ``user_id``."""
        rows = await _rows(
            self.read_db,
            "SELECT session_id, user_id, item_id, question_count, current_index, "
            "score, status, created_at, updated_at FROM quiz_sessions "
            "WHERE session_id = ? AND user_id = ?",
            (int(session_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

    async def quiz_current_question(self, session_id: int) -> dict[str, Any] | None:
        """The next unanswered question of an active session, or ``None``."""
        rows = await _rows(
            self.read_db,
            "SELECT q.question_id, q.idx, q.question, q.options_json, "
            "q.correct_index, q.explanation, q.source, q.difficulty, s.current_index, s.question_count "
            "FROM quiz_questions q JOIN quiz_sessions s ON s.session_id = q.session_id "
            "WHERE q.session_id = ? AND s.status = 'active' "
            "AND q.idx = s.current_index",
            (int(session_id),),
        )
        if not rows:
            return None
        row = dict(rows[0])
        row["options"] = json.loads(row.pop("options_json"))
        return row

    async def quiz_record_answer(
        self, session_id: int, question_id: int, chosen_index: int
    ) -> dict[str, Any]:
        """Record exactly one answer and advance the session.

        Return shapes:
        * ``{"status": "already"}`` — a second tap on an answered question;
        * ``{"status": "stale"}`` — wrong session/question/order or inactive;
        * ``{"status": "ok", ...}`` — the reveal payload and finished flag.

        The unique ``quiz_answers(question_id)`` index is the race-proof
        exactly-once guard; its IntegrityError is caught *inside* the
        transaction so the caller can show a friendly re-ack instead of the
        generic crash alert (sqlite keeps the transaction usable after a
        statement-level constraint failure).
        """
        now = iso(utc_now())
        async with self._transaction():
            session_rows = await _rows(self.db,
                "SELECT question_count, current_index, status FROM quiz_sessions "
                "WHERE session_id = ?",
                (int(session_id),),
            )
            if not session_rows or session_rows[0]["status"] != "active":
                return {"status": "stale"}
            question_count = int(session_rows[0]["question_count"])
            current_index = int(session_rows[0]["current_index"])
            question_rows = await _rows(self.db,
                "SELECT question_id, idx, options_json, correct_index, explanation "
                "FROM quiz_questions WHERE question_id = ? AND session_id = ?",
                (int(question_id), int(session_id)),
            )
            if not question_rows or not 0 <= int(chosen_index) <= 3:
                return {"status": "stale"}
            # Check the exactly-once guard before the index check: a rapid
            # double tap on the still-on-screen question arrives after the
            # first answer already advanced current_index, and must report
            # "already answered" rather than the generic stale-callback alert.
            existing = await _rows(self.db,
                "SELECT 1 FROM quiz_answers WHERE question_id = ?",
                (int(question_id),),
            )
            if existing:
                return {"status": "already"}
            if int(question_rows[0]["idx"]) != current_index:
                return {"status": "stale"}
            question = question_rows[0]
            correct_index = int(question["correct_index"])
            is_correct = int(chosen_index) == correct_index
            try:
                await self.db.execute(
                    "INSERT INTO quiz_answers(session_id, question_id, "
                    "chosen_index, is_correct, answered_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        int(session_id),
                        int(question_id),
                        int(chosen_index),
                        int(is_correct),
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # Concurrent inserts racing on the unique index: same friendly
                # outcome as the pre-check above.
                return {"status": "already"}
            if is_correct:
                await self.db.execute(
                    "UPDATE quiz_sessions SET score = score + 10, "
                    "current_index = current_index + 1, updated_at = ? "
                    "WHERE session_id = ?",
                    (now, int(session_id)),
                )
                # The all-time leaderboard total lives on the user row; quiz
                # answers remain the per-question audit trail.
                await self.db.execute(
                    "UPDATE users SET quiz_points = quiz_points + 10 "
                    "WHERE telegram_id = (SELECT user_id FROM quiz_sessions "
                    "WHERE session_id = ?)",
                    (int(session_id),),
                )
            else:
                await self.db.execute(
                    "UPDATE quiz_sessions SET current_index = current_index + 1, "
                    "updated_at = ? WHERE session_id = ?",
                    (now, int(session_id)),
                )
            finished = current_index + 1 >= question_count
            if finished:
                await self.db.execute(
                    "UPDATE quiz_sessions SET status = 'done', updated_at = ? "
                    "WHERE session_id = ?",
                    (now, int(session_id)),
                )
            score_rows = await _rows(self.db,
                "SELECT score FROM quiz_sessions WHERE session_id = ?",
                (int(session_id),),
            )
            return {
                "status": "ok",
                "correct": is_correct,
                "chosen_index": int(chosen_index),
                "correct_index": correct_index,
                "options": json.loads(question["options_json"]),
                "explanation": str(question["explanation"] or ""),
                "finished": finished,
                "score": int(score_rows[0]["score"]) if score_rows else 0,
                "total": question_count,
            }

    async def quiz_finish(self, session_id: int, status: str) -> None:
        """Move an active session to a terminal status (quit path)."""
        if status not in ("quit", "expired", "done"):
            raise ValueError(f"invalid terminal quiz status {status!r}")
        await self._write(
            "UPDATE quiz_sessions SET status = ?, updated_at = ? "
            "WHERE session_id = ? AND status = 'active'",
            (status, iso(utc_now()), int(session_id)),
        )

    # ----------------------------------------------------- learning: leaderboard
    async def leaderboard_top(self, limit: int = 20) -> list[dict[str, Any]]:
        """Top point earners among active users; first names only, never ids."""
        rows = await _rows(
            self.read_db,
            "SELECT first_name, quiz_points FROM users "
            "WHERE banned = 0 AND quiz_points > 0 "
            "ORDER BY quiz_points DESC, telegram_id ASC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [
            {"first_name": str(row[0] or ""), "quiz_points": int(row[1])}
            for row in rows
        ]

    async def leaderboard_rank(self, user_id: int) -> dict[str, int]:
        """All-time rank (1-based, ties broken by points only) and own points."""
        rows = await _rows(
            self.read_db,
            "SELECT quiz_points FROM users WHERE telegram_id = ?",
            (int(user_id),),
        )
        points = int(rows[0][0]) if rows else 0
        higher = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM users WHERE banned = 0 AND quiz_points > ?",
            (points,),
        )
        return {"rank": (int(higher[0][0]) if higher else 0) + 1, "points": points}

    async def leaderboard_admin_top(self, limit: int = 5) -> list[dict[str, Any]]:
        """Top earners *with* ids — admin gift screens only, never the user board."""
        rows = await _rows(
            self.read_db,
            "SELECT telegram_id, first_name, quiz_points FROM users "
            "WHERE banned = 0 AND quiz_points > 0 "
            "ORDER BY quiz_points DESC, telegram_id ASC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [
            {
                "telegram_id": int(row[0]),
                "first_name": str(row[1] or ""),
                "quiz_points": int(row[2]),
            }
            for row in rows
        ]

    async def user_identity(self, user_id: int) -> dict[str, Any] | None:
        """Display identity + flags for admin screens and gift validation.

        ``username`` is the latest known one (empty string when Telegram never
        reported one) and is always shown next to the numeric id, so a renamed
        account can never be mistaken for a different user.
        """
        rows = await _rows(
            self.read_db,
            "SELECT telegram_id, first_name, banned, is_unlimited, "
            "COALESCE(username, ''), COALESCE(last_name, ''), last_seen_at, joined_at, "
            "subscription_status, subscription_verified_at "
            "FROM users WHERE telegram_id = ?",
            (int(user_id),),
        )
        if not rows:
            return None
        identity = {
            "telegram_id": int(rows[0][0]),
            "first_name": str(rows[0][1] or ""),
            "banned": bool(rows[0][2]),
            "is_unlimited": bool(rows[0][3]),
            "username": str(rows[0][4] or ""),
            "last_name": str(rows[0][5] or ""),
            "last_seen_at": rows[0][6],
            "joined_at": rows[0][7],
            "subscription_status": rows[0][8],
            "subscription_verified_at": rows[0][9],
        }
        identity["free_uses"] = await self.free_use_state(int(user_id))
        return identity

    async def identities_for(self, user_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """Identities for a batch of ids, for admin lists that name many owners.

        One query per screen instead of one per row; ids that were never seen
        are simply absent and the caller falls back to the bare number.
        """
        wanted = sorted({int(value) for value in user_ids})
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        rows = await _rows(
            self.read_db,
            "SELECT telegram_id, COALESCE(username, ''), COALESCE(first_name, ''), "
            "COALESCE(last_name, ''), subscription_status, subscription_verified_at "
            "FROM users "
            f"WHERE telegram_id IN ({placeholders})",
            tuple(wanted),
        )
        return {
            int(row[0]): {
                "telegram_id": int(row[0]),
                "username": str(row[1] or ""),
                "first_name": str(row[2] or ""),
                "last_name": str(row[3] or ""),
                "subscription_status": row[4],
                "subscription_verified_at": row[5],
            }
            for row in rows
        }

    # -------------------------------------------------------------- documents
    async def document_add(
        self,
        user_id: int,
        *,
        file_msg_ref: str,
        title: str,
        source_type: str,
        page_count: int,
        sha256: str,
        text: str,
        chunks_json: str,
        library_item_id: int | None = None,
    ) -> tuple[int, bool]:
        """Store one extracted document; returns ``(id, created)``.

        ``created=False`` means this exact file (same ``sha256``) was already
        ingested by this user, so the caller reuses the stored extraction
        instead of paying for a second download/parse.
        """
        user_id = int(user_id)
        existing = await self.document_by_hash(user_id, sha256)
        if existing is not None:
            return int(existing["id"]), False
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO documents(user_id, file_msg_ref, title, source_type, "
                "page_count, sha256, text, chunks_json, library_item_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, sha256) DO NOTHING",
                (
                    user_id,
                    str(file_msg_ref)[:200],
                    str(title)[:200],
                    str(source_type),
                    int(page_count),
                    str(sha256),
                    str(text),
                    str(chunks_json),
                    int(library_item_id) if library_item_id else None,
                    iso(utc_now()),
                ),
            )
            created = cursor.rowcount == 1
            row_id = int(cursor.lastrowid or 0)
        if not created:
            again = await self.document_by_hash(user_id, sha256)
            if again is None:  # pragma: no cover - conflict then vanished
                raise RuntimeError("document insert conflicted but row is missing")
            return int(again["id"]), False
        return row_id, True

    async def document_by_hash(self, user_id: int, sha256: str) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT id, user_id, file_msg_ref, title, source_type, page_count, "
            "sha256, text, chunks_json, library_item_id, created_at FROM documents "
            "WHERE user_id = ? AND sha256 = ?",
            (int(user_id), str(sha256)),
        )
        return dict(rows[0]) if rows else None

    async def document_get(self, user_id: int, document_id: int) -> dict[str, Any] | None:
        """Owner-scoped document lookup: a foreign id is invisible."""
        rows = await _rows(
            self.read_db,
            "SELECT id, user_id, file_msg_ref, title, source_type, page_count, "
            "sha256, text, chunks_json, library_item_id, created_at FROM documents "
            "WHERE id = ? AND user_id = ?",
            (int(document_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

    async def document_by_library_item(self, user_id: int, item_id: int) -> dict[str, Any] | None:
        """Resolve a mirror to its full, locator-preserving cached document."""
        rows = await _rows(
            self.read_db,
            "SELECT id FROM documents WHERE user_id = ? AND library_item_id = ? LIMIT 1",
            (int(user_id), int(item_id)),
        )
        return dict(rows[0]) if rows else None

    async def documents_list(
        self, user_id: int, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Library listing without the (potentially huge) extracted text."""
        rows = await _rows(
            self.read_db,
            "SELECT id, user_id, file_msg_ref, title, source_type, page_count, "
            "sha256, library_item_id, created_at FROM documents "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (int(user_id), int(limit), int(offset)),
        )
        return [dict(row) for row in rows]

    async def document_set_library_item(
        self, document_id: int, library_item_id: int
    ) -> None:
        """Link a document to its mirrored library item (created lazily)."""
        await self._write(
            "UPDATE documents SET library_item_id = ? WHERE id = ?",
            (int(library_item_id), int(document_id)),
        )

    async def document_update_reference(self, user_id: int, document_id: int, ref: str) -> None:
        """Use the latest upload after deduplication, including its library mirror."""
        if not ref:
            return
        try:
            chat_id, message_id = (int(part) for part in ref.split(":", 1))
        except (ValueError, TypeError):
            return
        async with self._transaction():
            await self.db.execute(
                "UPDATE documents SET file_msg_ref = ? WHERE id = ? AND user_id = ?",
                (ref[:200], int(document_id), int(user_id)),
            )
            await self.db.execute(
                "UPDATE library_items SET chat_id = ?, message_id = ?, unavailable = 0 "
                "WHERE item_id = (SELECT library_item_id FROM documents "
                "WHERE id = ? AND user_id = ?)",
                (chat_id, message_id, int(document_id), int(user_id)),
            )

    async def document_ensure_library_item(
        self, user_id: int, document_id: int, content_text: str
    ) -> int:
        """Create exactly one owner-scoped library mirror, even on concurrent uploads."""
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT library_item_id, file_msg_ref, title FROM documents "
                "WHERE id = ? AND user_id = ?",
                (int(document_id), int(user_id)),
            )
            if not rows:
                raise ValueError("document not found")
            if rows[0][0] is not None:
                return int(rows[0][0])
            try:
                chat_id, message_id = (int(part) for part in str(rows[0][1]).split(":", 1))
            except (ValueError, TypeError):
                chat_id, message_id = 0, 0
            cursor = await self.db.execute(
                "INSERT INTO library_items(user_id, kind, chat_id, message_id, title, "
                "created_at, content_text) VALUES (?, 'booklet', ?, ?, ?, ?, ?)",
                (int(user_id), chat_id, message_id, f"📄 {str(rows[0][2])[:120]}",
                 iso(utc_now()), content_text),
            )
            item_id = int(cursor.lastrowid or 0)
            await self.db.execute(
                "UPDATE documents SET library_item_id = ? WHERE id = ? AND user_id = ?",
                (item_id, int(document_id), int(user_id)),
            )
            return item_id

    async def document_count(self, user_id: int) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM documents WHERE user_id = ?",
            (int(user_id),),
        )
        return int(rows[0][0]) if rows else 0

    # ----------------------------------------------------------- integrity
    #: Every column in the schema that must reference ``users(telegram_id)``.
    #: The integrity check below walks them all, so a new table that forgets the
    #: reference shows up as a violation instead of as a silent orphan.
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
    # Learning and lot-allocation children reach users through their parent.
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
        ("ledger", "payment_id", "payments", "payment_id"),
        ("documents", "library_item_id", "library_items", "item_id"),
        ("learning_charges", "debit_id", "ledger", "id"),
    )

    async def user_reference_integrity(self) -> list[str]:
        """Check declarations *and* rows, including indirect learning/billing links."""
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
            if not any(
                row[2] == parent and row[3] == column and row[4] == parent_column
                for row in fks
            ):
                problems.append(f"missing foreign key: {table}.{column} -> {parent}.{parent_column}")
            orphans = await _rows(
                self.read_db,
                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL "
                f"AND {column} NOT IN (SELECT {parent_column} FROM {parent})",
            )
            count = int(orphans[0][0]) if orphans else 0
            if count:
                problems.append(f"orphan rows: {table}.{column} = {count}")
        info = await _rows(self.read_db, "PRAGMA table_info(users)")
        if not any(row[1] == "telegram_id" and str(row[2]).upper() == "INTEGER" and row[5] for row in info):
            problems.append("users.telegram_id is not an INTEGER primary key")
        affinity = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM users WHERE typeof(telegram_id) != 'integer'",
        )
        if affinity and int(affinity[0][0]):
            problems.append(f"users.telegram_id not stored as INTEGER: {affinity[0][0]}")
        return problems

    async def assert_user_reference_integrity(self) -> None:
        """Strict deployment/CI check of every user and learning/billing link."""
        problems = await self.user_reference_integrity()
        if problems:
            raise RuntimeError("SQLite reference integrity failed: " + "; ".join(problems))

    # ----------------------------------------------------- learning: flashcards
    async def deck_count(self, user_id: int) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM flashcard_decks WHERE user_id = ?",
            (int(user_id),),
        )
        return int(rows[0][0]) if rows else 0

    async def deck_create(
        self,
        user_id: int,
        item_id: int | None,
        title: str,
        source: str,
    ) -> int:
        if source not in ("ai", "manual"):
            raise ValueError(f"invalid deck source {source!r}")
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO flashcard_decks(user_id, item_id, title, source, "
                "study_cursor, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (int(user_id), item_id, str(title), source, iso(utc_now())),
            )
            return int(cursor.lastrowid or 0)

    async def deck_create_with_cards(
        self,
        user_id: int,
        item_id: int,
        title: str,
        cards: list[dict[str, str]],
        *,
        free_claim_id: str | None = None,
        paid_charge_id: str | None = None,
    ) -> int:
        """Insert an AI deck, its cards and its learning budget atomically."""
        if not cards:
            raise ValueError("a deck needs at least one card")
        now = iso(utc_now())
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO flashcard_decks(user_id, item_id, title, source, "
                "study_cursor, created_at) VALUES (?, ?, ?, 'ai', 0, ?)",
                (int(user_id), int(item_id), str(title), now),
            )
            deck_id = int(cursor.lastrowid or 0)
            for idx, card in enumerate(cards):
                await self.db.execute(
                    "INSERT INTO flashcards(deck_id, idx, front, back, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        deck_id,
                        idx,
                        str(card["front"]),
                        str(card["back"]),
                        str(card.get("source", ""))[:120],
                    ),
                )
            await self._finalize_free_use_locked(user_id, "deck", free_claim_id)
            await self._finalize_learning_charge_locked(user_id, paid_charge_id)
            return deck_id

    async def manual_deck_get_or_create(self, user_id: int) -> int:
        """The single per-user manual deck, created once and reused."""
        rows = await _rows(
            self.read_db,
            "SELECT deck_id FROM flashcard_decks WHERE user_id = ? "
            "AND source = 'manual' ORDER BY deck_id LIMIT 1",
            (int(user_id),),
        )
        if rows:
            return int(rows[0][0])
        return await self.deck_create(
            user_id, None, "فلش‌کارت‌های دستی من", "manual"
        )

    async def deck_add_card(self, deck_id: int, front: str, back: str) -> int:
        """Append a card and return the deck's new card count."""
        async with self._transaction():
            count_rows = await _rows(self.db,
                "SELECT COUNT(*) FROM flashcards WHERE deck_id = ?",
                (int(deck_id),),
            )
            idx = int(count_rows[0][0]) if count_rows else 0
            await self.db.execute(
                "INSERT INTO flashcards(deck_id, idx, front, back) "
                "VALUES (?, ?, ?, ?)",
                (int(deck_id), idx, str(front), str(back)),
            )
            return idx + 1

    async def deck_get(self, deck_id: int, user_id: int) -> dict[str, Any] | None:
        """Fetch one deck only when it belongs to ``user_id``."""
        rows = await _rows(
            self.read_db,
            "SELECT deck_id, user_id, item_id, title, source, study_cursor, "
            "created_at FROM flashcard_decks WHERE deck_id = ? AND user_id = ?",
            (int(deck_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

    async def deck_cards(self, deck_id: int) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT card_id, deck_id, idx, front, back, source FROM flashcards "
            "WHERE deck_id = ? ORDER BY idx",
            (int(deck_id),),
        )
        return [dict(row) for row in rows]

    async def deck_card(self, deck_id: int, card_id: int) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT card_id, idx, front, back, source FROM flashcards "
            "WHERE deck_id = ? AND card_id = ?",
            (int(deck_id), int(card_id)),
        )
        return dict(rows[0]) if rows else None

    async def deck_set_cursor(self, deck_id: int, cursor: int) -> None:
        await self._write(
            "UPDATE flashcard_decks SET study_cursor = ? WHERE deck_id = ?",
            (max(0, int(cursor)), int(deck_id)),
        )

    async def deck_delete(self, deck_id: int, user_id: int) -> bool:
        """Delete a deck; cards cascade via the foreign-key action."""
        async with self._transaction():
            cursor = await self.db.execute(
                "DELETE FROM flashcard_decks WHERE deck_id = ? AND user_id = ?",
                (int(deck_id), int(user_id)),
            )
            return cursor.rowcount == 1

    async def decks_list(
        self, user_id: int, *, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Newest decks first, each annotated with its card count."""
        rows = await _rows(
            self.read_db,
            "SELECT d.deck_id, d.item_id, d.title, d.source, d.study_cursor, "
            "d.created_at, (SELECT COUNT(*) FROM flashcards c WHERE c.deck_id = d.deck_id) "
            "AS card_count FROM flashcard_decks d WHERE d.user_id = ? "
            "ORDER BY d.deck_id DESC LIMIT ? OFFSET ?",
            (int(user_id), max(1, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]

    async def library_list_kind(
        self, user_id: int, kind: str, *, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        """One newest-first page of one artifact kind (flashcard picker)."""
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable FROM library_items "
            "WHERE user_id = ? AND kind = ? "
            "ORDER BY item_id DESC LIMIT ? OFFSET ?",
            (int(user_id), str(kind), max(1, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]

    async def library_count_kind(self, user_id: int, kind: str) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM library_items WHERE user_id = ? AND kind = ?",
            (int(user_id), str(kind)),
        )
        return int(rows[0][0]) if rows else 0

    # ------------------------------------------------------- admin reporting
    #: Terminal statuses that mean the user actually received the raw
    #: transcript. ``partial`` delivered it too, with a failed booklet on top.
    DELIVERED_STATUSES = ("done", "partial")
    #: Terminal statuses that mean the user received nothing.
    UNDELIVERED_STATUSES = ("failed", "rejected")
    #: Longest single series (days) one report returns, so an "all time"
    #: selection can never build an unbounded list on a shared host.
    REPORT_SERIES_MAX_DAYS = 93

    @staticmethod
    def _period_clause(period_start: str | None, column: str = "created_at") -> tuple[str, tuple]:
        """A ``created_at`` lower bound, or nothing at all for "all history".

        ``created_at`` values are written by :func:`iso` as UTC ISO-8601 with an
        explicit ``+00:00`` offset and a fixed width, so a lexicographic
        comparison is exactly a chronological one. The predicate is *omitted*
        when there is no window: spelling it as ``(COALESCE(?, '') = '' OR
        created_at >= ?)`` made it non-sargable, so SQLite could not use the
        reporting index and every window scanned the whole table.
        """
        if not period_start:
            return "", ()
        return f"{column} >= ?", (period_start,)

    async def _period_duration(
        self, expression: str, where: str, params: tuple
    ) -> dict[str, float | int | None]:
        """Count/average/median/p95/max of one SQL duration expression.

        Percentiles are computed inside SQLite (row numbers over the ordered
        sample) so the rows never travel to Python: one aggregate query per
        series instead of loading every job of the period.

        ``expression`` and ``where`` are internal SQL fragments, never user
        input; every value is bound as a parameter.
        """
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
            # No samples is "not measured", not a zero-second duration.
            return {
                "count": 0, "avg_seconds": None, "median_seconds": None,
                "p95_seconds": None, "max_seconds": None,
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
        """Storage and last-success facts for the system screen.

        Deliberately two cheap statements: the health screen must stay
        responsive even when a period report over a large ledger would not.
        ``last_success`` is ``None`` when nothing has ever completed, which the
        screens render as "not recorded" rather than as an epoch or a zero.
        """
        last_rows = await _rows(self.read_db,
            "SELECT MAX(finished_at) FROM submissions WHERE status IN ('done','partial')"
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
        """Every figure the admin dashboard shows, from authoritative rows.

        The scope of each number is explicit: ``period`` figures filter on the
        UTC ``created_at`` of the row, ``snapshot`` figures are the current
        state (pending reviews, expiring credit), and lifetime totals are named
        as such. Nothing is estimated and a metric that cannot be measured is
        returned as ``None``, never as zero.

        One statement per figure (≈14 indexed aggregates) keeps the dashboard
        responsive without an N+1 pattern; a caller that shows several sections
        should reuse the one returned dict.

        ``with_timings=False`` skips the three duration series (the costliest
        part: they sort every finished row of the window). The timing keys are
        then ``None`` — explicitly "not measured here", which is different from
        a series that was measured and had no rows. Screens that do not display
        timings must pass ``False``.
        """
        now = now or utc_now()
        now_iso = iso(now)
        series_floor = iso(now - timedelta(days=self.REPORT_SERIES_MAX_DAYS))
        series_since = max(period_start, series_floor) if period_start else series_floor
        period_where, period_params = self._period_clause(period_start)
        where = period_where or "1 = 1"

        # ---------------------------------------------------------- users
        new_users_where, new_users_params = self._period_clause(period_start, "joined_at")
        user_rows = await _rows(self.read_db,
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

        # ----------------------------------------------------- processing
        status_rows = await _rows(self.read_db,
            f"SELECT status, COUNT(*) FROM submissions WHERE {where} GROUP BY status",
            period_params,
        )
        by_status = {str(row[0]): int(row[1]) for row in status_rows}

        kind_rows = await _rows(self.read_db,
            f"SELECT kind, COUNT(*) FROM submissions WHERE {where} GROUP BY kind",
            period_params,
        )
        by_kind = {str(row[0]): int(row[1]) for row in kind_rows}

        delivered = sum(by_status.get(status, 0) for status in self.DELIVERED_STATUSES)
        undelivered = sum(by_status.get(status, 0) for status in self.UNDELIVERED_STATUSES)
        terminal = delivered + undelivered

        error_rows = await _rows(self.read_db,
            # Group by the expression, not the alias: ``kind`` is also a real
            # column of ``submissions`` and SQLite resolves the bare name to it
            # (which silently grouped every failure under its media kind).
            "SELECT COALESCE(error_kind, 'unclassified') AS error_kind, COUNT(*) "
            f"FROM submissions WHERE status IN ('failed','rejected') AND {where} "
            "GROUP BY COALESCE(error_kind, 'unclassified') "
            "ORDER BY COUNT(*) DESC LIMIT 12",
            period_params,
        )

        day_rows = await _rows(self.read_db,
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*), "
            "SUM(CASE WHEN status IN ('done','partial') THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status IN ('failed','rejected') THEN 1 ELSE 0 END) "
            f"FROM submissions WHERE created_at >= ? AND {where} "
            "GROUP BY day ORDER BY day DESC LIMIT ?",
            (series_since, *period_params, self.REPORT_SERIES_MAX_DAYS),
        )
        new_user_rows = await _rows(self.read_db,
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

        # ------------------------------------------------------- payments
        payment_rows = await _rows(self.read_db,
            "SELECT status, COUNT(*), COALESCE(SUM(COALESCE(purchased_price, 0)), 0) "
            f"FROM payments WHERE {where} GROUP BY status",
            period_params,
        )
        payments_by_status = {
            str(row[0]): {"count": int(row[1]), "toman": int(row[2] or 0)}
            for row in payment_rows
        }
        pending_rows = await _rows(self.read_db,
            "SELECT COUNT(*) FROM payments "
            "WHERE status IN ('pending_review','pending') AND receipt_path IS NOT NULL"
        )
        future = iso(now + timedelta(days=max(1, expiring_within_days)))
        expiring_rows = await _rows(self.read_db,
            "SELECT COUNT(*), COALESCE(SUM(seconds), 0) FROM ledger "
            "WHERE kind = 'credit' AND expires_at IS NOT NULL "
            "AND julianday(expires_at) > julianday(?) AND julianday(expires_at) <= julianday(?)",
            (now_iso, future),
        )

        # --------------------------------------------------------- health
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
                    {"kind": str(row[0]), "count": int(row[1])} for row in error_rows
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
                    "seconds": int(expiring_rows[0][1] or 0) if expiring_rows else 0,
                },
            },
            "health": health,
        }

    async def admin_stt_engines(
        self, *, period_start: str | None
    ) -> list[dict[str, Any]]:
        """Which STT engine produced each transcript of the period.

        ``submissions.stt_engine`` is written next to the transcript, so this
        survives the transcript-retention sweep (which would otherwise erase
        the only record of provider usage).
        """
        period_where, params = self._period_clause(period_start)
        rows = await _rows(self.read_db,
            "SELECT COALESCE(stt_engine, 'unrecorded') AS engine, COUNT(*) "
            f"FROM submissions WHERE {period_where or '1 = 1'} "
            "GROUP BY COALESCE(stt_engine, 'unrecorded') "
            "ORDER BY COUNT(*) DESC",
            params,
        )
        return [{"engine": str(row[0]), "count": int(row[1])} for row in rows]

    async def recover_interrupted(self) -> int:
        """Startup recovery: fail interrupted work and release its credit holds.

        Returns the number of submissions recovered. A job is either fully
        recorded or fully failed — never silently dropped with a ``processing``
        row and a live hold left behind.
        """
        now = iso(utc_now())
        async with self._transaction():
            recovered = await _rows(self.db,
                "SELECT submission_id, user_id FROM submissions "
                "WHERE status IN ('pending', 'processing')"
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
            rows = await _rows(self.db,
                "SELECT is_unlimited, banned FROM users WHERE telegram_id = ?", (user_id,)
            )
            if rows and rows[0][1]:
                raise UserBannedError("⛔ دسترسی شما به ربات مسدود شده است.")
            owner = await _rows(self.db,
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
            rows = await _rows(self.db,
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
                allocations = await _rows(self.db,
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

    async def spend_seconds(
        self, user_id: int, seconds: int, note: str, *, operation_id: str | None = None
    ) -> bool:
        """Charge credit for a hold-free feature (quiz/AI flashcard generation).

        Returns ``True`` when a lot-aware debit was written and ``False`` for an
        unlimited user. If ``operation_id`` is supplied, the debit and a durable
        pending learning charge are inserted in the same transaction. The caller
        must finalize it alongside the artifact or refund it on failure.

        Credit lots are consumed earliest-expiry-first, with every consumed
        second backed by a ``debit_allocations`` row.
        """
        if seconds <= 0:
            raise ValueError("spend must be positive")
        async with self._transaction():
            rows = await _rows(self.db,
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
            now = iso(utc_now())
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
            # The balance check and the allocation run under one write lock; a
            # shortfall here is still a defensive guard, never an expected path.
            if remaining:
                raise InsufficientBalanceError(seconds - remaining, seconds)
            if operation_id is not None:
                if not operation_id:
                    raise ValueError("learning charge operation_id must not be empty")
                await self.db.execute(
                    "INSERT INTO learning_charges(operation_id, user_id, debit_id, seconds, "
                    "note, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (operation_id, int(user_id), cursor.lastrowid, int(seconds), note[:200], now, now),
                )
            return True

    async def _finalize_learning_charge_locked(
        self, user_id: int, operation_id: str | None
    ) -> None:
        """Finalize a paid generation inside the artifact's transaction."""
        if operation_id is None:
            return
        cursor = await self.db.execute(
            "UPDATE learning_charges SET status = 'finalized', updated_at = ? "
            "WHERE operation_id = ? AND user_id = ? AND status = 'pending'",
            (iso(utc_now()), operation_id, int(user_id)),
        )
        if cursor.rowcount != 1:
            # Never save an artifact against a missing, refunded, or other
            # user's charge. Raising rolls back both the artifact and charge.
            raise ValueError("learning charge is not pending for this user")

    async def refund_learning_charge(self, user_id: int, operation_id: str) -> bool:
        """Refund the exact pending paid attempt once, even across retries."""
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT seconds, note FROM learning_charges "
                "WHERE operation_id = ? AND user_id = ? AND status = 'pending'",
                (operation_id, int(user_id)),
            )
            if not rows:
                return False
            now = iso(utc_now())
            await self.db.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, created_at) "
                "VALUES (?, ?, 'credit', ?, ?)",
                (int(user_id), int(rows[0][0]), "refund: " + str(rows[0][1])[:180], now),
            )
            await self.db.execute(
                "UPDATE learning_charges SET status = 'refunded', updated_at = ? "
                "WHERE operation_id = ? AND user_id = ? AND status = 'pending'",
                (now, operation_id, int(user_id)),
            )
            return True

    async def recover_pending_learning_charges(self) -> int:
        """Refund unsaved paid generations after acquiring the instance lock.

        Insertion of the credit and the status transition share one transaction.
        A saved artifact finalized its charge in its own transaction, so it is
        never eligible for this recovery, including after an interrupted COMMIT.
        """
        async with self._transaction():
            now = iso(utc_now())
            cursor = await self.db.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, created_at) "
                "SELECT user_id, seconds, 'credit', 'refund: ' || substr(note, 1, 180), ? "
                "FROM learning_charges WHERE status = 'pending'",
                (now,),
            )
            recovered = max(0, cursor.rowcount)
            await self.db.execute(
                "UPDATE learning_charges SET status = 'refunded', updated_at = ? "
                "WHERE status = 'pending'",
                (now,),
            )
            return recovered

    async def refund_seconds(
        self, user_id: int, seconds: int, note: str
    ) -> None:
        """Return a failed feature charge as a non-expiring credit lot.

        A refund deliberately has no expiry even when it consumed expiring
        credit: the user is made whole, and the ``refund:`` note keeps the two
        grant kinds distinguishable in the ledger and audit tooling.
        """
        if seconds <= 0:
            return
        await self.add_credit(
            user_id,
            int(seconds),
            note="refund: " + note[:180],
            validity_days=None,
        )

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
        rows = await _rows(self.db,
            "SELECT code, hours, price, validity_days, is_custom, active FROM plans "
            "WHERE active = 1 ORDER BY price"
        )
        return [dict(row) for row in rows]

    async def plan(self, code: str) -> dict[str, Any] | None:
        rows = await _rows(self.db,
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
            open_rows = await _rows(self.db,
                "SELECT payment_id, plan_code FROM payments "
                "WHERE user_id = ? AND status IN ('pending','awaiting_payment','pending_review') "
                "ORDER BY payment_id LIMIT 1",
                (user_id,),
            )
            if open_rows:
                if str(open_rows[0][1]) == plan_code:
                    return int(open_rows[0][0])
                raise OpenPaymentError(str(open_rows[0][1]))
            plan = await _rows(self.db,
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
            rows = await _rows(self.db,
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments WHERE payment_id = ?",
                (payment_id,),
            )
            return dict(rows[0]) if rows else None

    async def open_awaiting_payment_for_user(self, user_id: int) -> dict[str, Any] | None:
        """Return the user's open unsubmitted order awaiting a receipt, if any."""
        async with self._write_lock:
            rows = await _rows(self.db,
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments "
                "WHERE user_id = ? AND status IN ('awaiting_payment', 'pending') "
                "AND receipt_path IS NULL ORDER BY payment_id DESC LIMIT 1",
                (user_id,),
            )
            return dict(rows[0]) if rows else None

    async def pending_payments(self) -> list[dict[str, Any]]:
        # For backward compat, includes legacy 'pending' and new explicit states.
        async with self._write_lock:
            rows = await _rows(self.db,
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments WHERE status IN ('pending','awaiting_payment','pending_review') "
                "ORDER BY payment_id"
            )
            return [dict(row) for row in rows]

    async def awaiting_payments(self) -> list[dict[str, Any]]:
        async with self._write_lock:
            rows = await _rows(self.db,
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
            rows = await _rows(self.db, query, params)
            return [dict(row) for row in rows]

    async def count_pending_review_payments(self) -> int:
        """Count reviewable orders without loading an unbounded backlog."""
        async with self._write_lock:
            rows = await _rows(self.db,
                "SELECT COUNT(*) FROM payments "
                "WHERE status IN ('pending_review','pending') AND receipt_path IS NOT NULL"
            )
            return int(rows[0][0]) if rows else 0

    async def all_payments(
        self, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Newest orders first, optionally narrowed to one status (audit filter)."""
        where = ""
        params: tuple[Any, ...] = (int(limit), int(offset))
        if status:
            where = "WHERE status = ? "
            params = (str(status), int(limit), int(offset))
        async with self._write_lock:
            rows = await _rows(self.db,
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments {where}"
                "ORDER BY payment_id DESC LIMIT ? OFFSET ?",
                params,
            )
            return [dict(row) for row in rows]

    async def payment_history_paginated(self, user_id: int, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        async with self._write_lock:
            rows = await _rows(self.db,
                f"SELECT {self._PAYMENT_COLUMNS} FROM payments WHERE user_id = ? ORDER BY payment_id DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            )
            return [dict(row) for row in rows]

    async def count_payments_by_status(self) -> dict[str, int]:
        async with self._write_lock:
            rows = await _rows(self.db, "SELECT status, COUNT(*) FROM payments GROUP BY status")
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
            rows = await _rows(self.db,
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
            rows = await _rows(self.db,
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

    async def recent_audit(self, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        async with self._write_lock:
            rows = await _rows(self.db,
                "SELECT admin_id, action, detail, subject_user, created_at FROM audit "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
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
    SUPPORT_TRANSITIONS: dict[str, dict[str | None, str]] = {
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
            rows = await _rows(self.db,
                "SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)
            )
            return dict(rows[0]) if rows else None

    async def get_ticket_by_public_id(self, public_id: str) -> dict[str, Any] | None:
        async with self._write_lock:
            rows = await _rows(self.db,
                "SELECT * FROM support_tickets WHERE public_ticket_id = ?", (public_id,)
            )
            return dict(rows[0]) if rows else None

    async def list_user_tickets(self, user_id: int, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        async with self._write_lock:
            rows = await _rows(self.db,
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
            rows = await _rows(self.db,
                f"SELECT * FROM support_tickets WHERE {clause} "
                "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            return [dict(row) for row in rows]

    async def count_tickets_by_status(self) -> dict[str, int]:
        async with self._write_lock:
            rows = await _rows(self.db,
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
            rows = await _rows(self.db,
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
            rows = await _rows(self.db,
                "SELECT * FROM (SELECT * FROM support_messages WHERE ticket_id = ? "
                "ORDER BY id DESC LIMIT ? OFFSET ?) ORDER BY id ASC",
                (ticket_id, limit, offset),
            )
            return [dict(row) for row in rows]

    async def count_ticket_messages(self, ticket_id: int) -> int:
        async with self._write_lock:
            rows = await _rows(self.db,
                "SELECT COUNT(*) FROM support_messages WHERE ticket_id = ?", (ticket_id,)
            )
            return int(rows[0][0]) if rows else 0

    async def close_ticket(self, ticket_id: int, actor_id: int, *, admin: bool) -> bool:
        """Close a ticket. Returns False when it is already closed."""
        now = iso(utc_now())
        async with self._transaction():
            rows = await _rows(self.db,
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
            rows = await _rows(self.db,
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
