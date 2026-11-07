"""SQLite connection management, WAL/read-only handles, migrations, and shared rows (SRP / DIP).

Separates low-level SQLite lifecycle, permission hardening (R10), serialized
``BEGIN IMMEDIATE`` transactions (R4), and schema migrations from domain-level
repositories (:mod:`gamas_bot.db_users`, :mod:`gamas_bot.db_billing`, etc.).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
import random
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

import aiosqlite

from .metrics import METRICS

logger = logging.getLogger("gamas_bot.db.core")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "v3"

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
        "library_items",
        "quiz_sessions",
        "quiz_questions",
        "quiz_answers",
        "flashcard_decks",
        "flashcards",
        "free_uses",
        "documents",
        "learning_charges",
        "pending_gate_actions",
        "referrals",
        "referral_rewards",
    }
)

TERMINAL_SUBMISSION_STATUSES = ("done", "partial", "failed", "rejected")

ADD_COLUMN_RE = re.compile(
    r"(?i)^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+"
    r"[\"'`]?([A-Za-z_][A-Za-z0-9_]*)[\"'`]?"
)


async def _rows(
    connection: aiosqlite.Connection,
    sql: str,
    parameters: Iterable[Any] | None = None,
) -> list[sqlite3.Row]:
    """Fetch rows, retrying transient SQLite read contention twice."""
    params = tuple(parameters) if parameters is not None else None
    for attempt in range(3):
        try:
            return list(await connection.execute_fetchall(sql, params))
        except sqlite3.Error as exc:
            METRICS.record_db_error()
            if (not isinstance(exc, sqlite3.OperationalError)
                    or not any(word in str(exc).lower() for word in ("locked", "busy"))
                    or attempt == 2):
                raise
            delay = (0.05, 0.15)[attempt] * random.uniform(0.9, 1.1)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


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


class TicketStateError(RuntimeError):
    """An event that the ticket's current status does not allow."""

    def __init__(self, event: str, current: str | None):
        self.event = event
        self.current = current
        super().__init__(f"ticket event {event!r} not allowed in status {current!r}")


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


class DatabaseCore:
    """Connection lifecycle, migrations, serialized write lock, and clock seam."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._read_db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._clock = clock

    def _now(self) -> datetime:
        """Current UTC timestamp from injected clock or ``gamas_bot.db.utc_now``."""
        if self._clock is not None:
            return self._clock()
        facade = sys.modules.get("gamas_bot.db")
        clock_fn = getattr(facade, "utc_now", None)
        if callable(clock_fn):
            result = clock_fn()
            if isinstance(result, datetime):
                return result
        return utc_now()

    def _migrations_dir(self) -> Path:
        facade = sys.modules.get("gamas_bot.db")
        candidate = getattr(facade, "MIGRATIONS_DIR", None)
        if isinstance(candidate, Path):
            return candidate
        return MIGRATIONS_DIR

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

    async def user_reference_integrity(self) -> list[str]:
        return []

    async def _backfill_allocations(self) -> None:
        return None

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
        # Bound the writer cache and retained WAL on the 2 GB host.
        await self._db.execute("PRAGMA cache_size=-8000")
        await self._db.execute("PRAGMA journal_size_limit=33554432")
        await self._apply_migrations()
        await self._db.commit()
        await self._db.execute("PRAGMA foreign_keys=ON")
        problems = await self.user_reference_integrity()
        if problems:
            logger.warning("SQLite reference integrity: %s", "; ".join(problems[:20]))
        await self._backfill_allocations()
        if str(self.path) != ":memory:":
            self._read_db = await aiosqlite.connect(self.path)
            self._read_db.row_factory = aiosqlite.Row
            await self._read_db.execute("PRAGMA query_only=ON")
            await self._read_db.execute("PRAGMA temp_store=MEMORY")
            await self._read_db.execute("PRAGMA busy_timeout=5000")
            # Bound the read cache and mapping on the 2 GB host.
            await self._read_db.execute("PRAGMA cache_size=-16000")
            await self._read_db.execute("PRAGMA mmap_size=134217728")
            await self._read_db.execute("PRAGMA journal_size_limit=33554432")
        self._secure_file_permissions()

    def _secure_file_permissions(self) -> None:
        """Best-effort ``0600`` on the database file and its WAL sidecars (R10)."""
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
        migrations_dir = self._migrations_dir()
        if not migrations_dir.is_dir():
            raise RuntimeError(f"Missing migrations directory: {migrations_dir}")
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            row[0]
            for row in await _rows(self.db, "SELECT name FROM schema_migrations")
        }
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in applied:
                continue
            logger.info("Applying migration %s", path.name)
            raw_sql = path.read_text(encoding="utf-8")
            raw_sql = await self._without_applied_columns(raw_sql)
            await self.db.executescript(raw_sql)
            await self.db.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (path.name, iso(self._now())),
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
    async def _transaction(self) -> AsyncIterator[None]:
        """Serialize writers, including across connections; rollback partial work."""
        async with self._write_lock:
            if self.db.in_transaction:
                await self.db.rollback()
                logger.warning("discarded uncommitted transaction before BEGIN IMMEDIATE")
                METRICS.record_db_error()
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                yield
                await self.db.commit()
            except BaseException as exc:
                await self.db.rollback()
                if isinstance(exc, sqlite3.Error):
                    METRICS.record_db_error()
                raise

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self._transaction():
            await self.db.execute(sql, params)

    async def _write_many(
        self, statements: list[tuple[str, tuple[Any, ...]]]
    ) -> None:
        async with self._transaction():
            for sql, params in statements:
                await self.db.execute(sql, params)
