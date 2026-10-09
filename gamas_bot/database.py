from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .billing import (
    FREE_PLAN_CODE,
    LEGACY_FREE_PLAN_CODE,
    MAX_PLAN_HOURS,
    MAX_PLAN_PRICE_TOMAN,
    MAX_PLAN_VALIDITY_DAYS,
    PLAN_CODE_PATTERN,
    SECONDS_PER_HOUR,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
logger = logging.getLogger(__name__)

#: Reason recorded on every reservation/ledger row a special user produces; the
#: billing engine never touches an entitlement for these accounts.
UNLIMITED_USAGE_REASON = "Unlimited special user"
#: Audit reason used when an administrator removes the special flag from a user.
UNLIMITED_REVOKED_REASON = "Special-user access removed"
_PLAN_CODE_RE = re.compile(PLAN_CODE_PATTERN)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: Persian text relies on the zero-width non-joiner/joiner: they are format
#: characters (``str.isprintable()`` is False) but stripping them would turn
#: "هم‌خوانی" into "همخوانی". Only genuinely unwanted control characters go.
_PERSIAN_FORMAT_CHARACTERS = frozenset({"\u200c", "\u200d"})


def clean_human_text(value: str | None, *, limit: int = 500) -> str | None:
    """Strip control/non-printable characters while keeping Persian formatting."""
    if value is None:
        return None
    cleaned = "".join(
        character
        for character in str(value)
        if character.isprintable() or character in _PERSIAN_FORMAT_CHARACTERS
    ).strip()
    cleaned = cleaned[:limit]
    return cleaned or None


def split_sql_statements(script: str) -> list[str]:
    """Split a migration file into statements, ignoring comments and strings."""
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(script):
        char = script[index]
        pair = script[index : index + 2]
        if quote is None and pair == "--":
            current.append(" ")
            end = script.find("\n", index)
            index = len(script) if end == -1 else end
            continue
        if quote is None and pair == "/*":
            current.append(" ")
            end = script.find("*/", index + 2)
            index = len(script) if end == -1 else end + 2
            continue
        if quote is not None and char == quote:
            quote = None
        elif quote is None and char in "'\"`[":
            quote = "]" if char == "[" else char
        if char == ";" and quote is None and sqlite3.complete_statement("".join(current) + ";"):
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    def _restrict_sqlite_permissions(self) -> None:
        """Keep private accounting/credential data owner-readable on POSIX."""
        if os.name != "posix":
            return
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{self.path}{suffix}")
                try:
                    candidate.chmod(0o600)
                except FileNotFoundError:
                    continue
        except OSError as exc:
            raise RuntimeError(
                "Could not restrict SQLite database and sidecar permissions to owner-only."
            ) from exc

    async def open(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restrict_sqlite_permissions()
        self._conn = await aiosqlite.connect(self.path)
        try:
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._apply_migrations()
            interrupted = await self._recover_interrupted_submissions()
            self._restrict_sqlite_permissions()
            logger.info(
                "Database opened path=%s interrupted_jobs_marked_failed=%s",
                self.path,
                interrupted,
            )
        except BaseException:
            await self.close()
            raise

    async def _apply_migrations(self) -> None:
        """Apply every migration file once, in filename order."""
        db = self._db()
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await db.commit()
        cursor = await db.execute("SELECT name FROM schema_migrations")
        applied = {str(row[0]) for row in await cursor.fetchall()}
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if migration.name in applied:
                continue
            await db.execute("BEGIN")
            try:
                for statement in split_sql_statements(migration.read_text(encoding="utf-8")):
                    try:
                        await db.execute(statement)
                    except sqlite3.OperationalError as exc:
                        # "ALTER TABLE ... ADD COLUMN" has no IF NOT EXISTS form, so a
                        # migration interrupted halfway (or a database created before
                        # the bookkeeping table existed) must not wedge every restart.
                        if "duplicate column name" not in str(exc).lower():
                            raise
                        logger.warning(
                            "Migration statement already applied name=%s detail=%s",
                            migration.name,
                            exc,
                        )
                await db.execute(
                    "INSERT OR REPLACE INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    (migration.name, utc_now()),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            logger.info("Database migration applied name=%s", migration.name)

    async def _recover_interrupted_submissions(self) -> int:
        """Release any reservation held by work that cannot survive a restart."""
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT id FROM audio_submissions WHERE status IN ('pending', 'processing') "
                "ORDER BY id"
            )
            rows = await cursor.fetchall()
            for row in rows:
                await self._release_reservation_in_transaction(
                    db, int(row["id"]), "پردازش با راه‌اندازی مجدد متوقف شد"
                )
            await db.execute(
                "UPDATE audio_submissions SET status='failed', "
                "error_message='پردازش با راه‌اندازی مجدد متوقف شد' "
                "WHERE status IN ('pending', 'processing')"
            )
            return len(rows)

    async def _release_reservation_in_transaction(
        self, db: aiosqlite.Connection, submission_id: int, reason: str
    ) -> int:
        cursor = await db.execute(
            "SELECT id, user_id, reserved_seconds, status FROM usage_reservations "
            "WHERE submission_id=?",
            (submission_id,),
        )
        reservation = await cursor.fetchone()
        if not reservation or reservation["status"] != "reserved":
            return 0
        reservation_id = int(reservation["id"])
        user_id = int(reservation["user_id"])
        reserved = int(reservation["reserved_seconds"])
        cursor = await db.execute(
            "SELECT entitlement_id, reserved_seconds FROM usage_ledger "
            "WHERE reservation_id=? AND event_type='reserve' ORDER BY id",
            (reservation_id,),
        )
        allocations = await cursor.fetchall()
        if sum(int(row["reserved_seconds"]) for row in allocations) != reserved:
            raise RuntimeError("Usage reservation allocation ledger is inconsistent")
        now = utc_now()
        released_total = 0
        for allocation in allocations:
            seconds = int(allocation["reserved_seconds"])
            entitlement_id = int(allocation["entitlement_id"])
            if seconds:
                await db.execute(
                    "UPDATE entitlements SET remaining_seconds=remaining_seconds+?, updated_at=? "
                    "WHERE id=?",
                    (seconds, now, entitlement_id),
                )
                await db.execute(
                    "INSERT INTO usage_ledger "
                    "(reservation_id, entitlement_id, submission_id, user_id, event_type, "
                    "released_seconds, reason, created_at) VALUES (?, ?, ?, ?, 'release', ?, ?, ?)",
                    (reservation_id, entitlement_id, submission_id, user_id, seconds, reason, now),
                )
                released_total += seconds
        await db.execute(
            "UPDATE usage_reservations SET status='released', released_seconds=?, reason=?, "
            "finalized_at=? WHERE id=? AND status='reserved'",
            (released_total, reason[:500], now, reservation_id),
        )
        return released_total

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open")
        return self._conn

    @asynccontextmanager
    async def _transaction(self, *, immediate: bool = False):
        """Serialize writes; IMMEDIATE transactions also coordinate other processes."""
        async with self._lock:
            db = self._db()
            await db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def upsert_user(self, telegram_id: int, username: str | None) -> dict[str, Any]:
        """Create/update a Telegram user and award the lifetime grant only once."""
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            )
            row = await cursor.fetchone()
            created = row is None
            now = utc_now()
            if created:
                cursor = await db.execute(
                    "INSERT INTO users(telegram_id, username, first_seen) VALUES (?, ?, ?)",
                    (telegram_id, username, now),
                )
                user_id = int(cursor.lastrowid)
                await self._grant_free_entitlement_in_transaction(db, user_id, now)
            else:
                user_id = int(row["id"])
                await db.execute(
                    "UPDATE users SET username=? WHERE id=?", (username, user_id)
                )
            cursor = await db.execute("SELECT * FROM users WHERE id=?", (user_id,))
            return dict(await cursor.fetchone())

    @staticmethod
    async def _free_plan_row(db: aiosqlite.Connection) -> Any:
        """Resolve the lifetime free plan under either canonical code spelling."""
        cursor = await db.execute(
            "SELECT id, included_seconds FROM plans WHERE code IN (?, ?) AND enabled=1 "
            "ORDER BY CASE WHEN code=? THEN 0 ELSE 1 END, id LIMIT 1",
            (FREE_PLAN_CODE, LEGACY_FREE_PLAN_CODE, FREE_PLAN_CODE),
        )
        return await cursor.fetchone()

    async def _grant_free_entitlement_in_transaction(
        self, db: aiosqlite.Connection, user_id: int, now: str
    ) -> None:
        plan = await self._free_plan_row(db)
        if not plan:
            raise RuntimeError("Canonical lifetime free plan is missing")
        await db.execute(
            "INSERT OR IGNORE INTO entitlements "
            "(user_id, plan_id, granted_seconds, remaining_seconds, starts_at, expires_at, "
            "status, source, reason, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, 'active', 'free_lifetime', ?, ?, ?)",
            (
                user_id,
                int(plan["id"]),
                int(plan["included_seconds"]),
                int(plan["included_seconds"]),
                now,
                "One-time lifetime free plan",
                now,
                now,
            ),
        )
        cursor = await db.execute(
            "SELECT id, granted_seconds FROM entitlements WHERE user_id=? AND source='free_lifetime'",
            (user_id,),
        )
        entitlement = await cursor.fetchone()
        if entitlement:
            await db.execute(
                "INSERT OR IGNORE INTO usage_ledger "
                "(entitlement_id, user_id, event_type, reserved_seconds, reason, created_at) "
                "VALUES (?, ?, 'grant', ?, 'Lifetime free plan grant', ?)",
                (int(entitlement["id"]), user_id, int(entitlement["granted_seconds"]), now),
            )

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_banned(
        self, telegram_id: int, banned: bool, *, admin_id: int | None = None
    ) -> bool:
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "UPDATE users SET is_banned=? WHERE telegram_id=?",
                (int(banned), telegram_id),
            )
            changed = cursor.rowcount > 0
            if changed and admin_id is not None:
                await self._insert_audit(
                    db,
                    admin_id,
                    "user_ban" if banned else "user_unban",
                    "user",
                    str(telegram_id),
                    {},
                )
            return changed

    @staticmethod
    async def _user_is_unlimited_in_transaction(
        db: aiosqlite.Connection, user_id: int
    ) -> bool:
        cursor = await db.execute(
            "SELECT is_unlimited FROM users WHERE id=?", (int(user_id),)
        )
        row = await cursor.fetchone()
        return bool(row and int(row["is_unlimited"]))

    async def is_unlimited_user(self, user_id: int) -> bool:
        """True when the account is a special user that is never billed."""
        async with self._lock:
            return await self._user_is_unlimited_in_transaction(self._db(), int(user_id))

    async def set_user_unlimited(
        self,
        telegram_id: int,
        unlimited: bool,
        admin_id: int,
        reason: str,
    ) -> dict[str, Any] | None:
        """Grant or revoke the unlimited flag; every change is audited.

        Returns ``None`` when the user does not exist, otherwise the new state
        with ``changed=False`` when it already matched the request.
        """
        reason = clean_human_text(reason) or ""
        if not reason:
            raise ValueError("برای تغییر وضعیت کاربر ویژه، ثبت دلیل الزامی است.")
        now = utc_now()
        flag = int(bool(unlimited))
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT id, is_unlimited FROM users WHERE telegram_id=?",
                (int(telegram_id),),
            )
            user = await cursor.fetchone()
            if not user:
                return None
            if int(user["is_unlimited"]) == flag:
                return {
                    "telegram_id": int(telegram_id),
                    "is_unlimited": bool(flag),
                    "changed": False,
                    "reason": reason,
                }
            await db.execute(
                "UPDATE users SET is_unlimited=?, unlimited_reason=?, unlimited_granted_by=?, "
                "unlimited_granted_at=? WHERE id=?",
                (
                    flag,
                    reason if flag else None,
                    int(admin_id) if flag else None,
                    now if flag else None,
                    int(user["id"]),
                ),
            )
            await self._insert_audit(
                db,
                admin_id,
                "special_user_granted" if flag else "special_user_revoked",
                "user",
                str(telegram_id),
                {"reason": reason},
            )
            return {
                "telegram_id": int(telegram_id),
                "is_unlimited": bool(flag),
                "changed": True,
                "reason": reason,
            }

    async def unlimited_users(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Every special user, newest grant first, with their media usage count."""
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT u.telegram_id, u.username, u.is_banned, u.unlimited_reason, "
                "u.unlimited_granted_at, "
                "(SELECT COUNT(*) FROM audio_submissions s WHERE s.user_id=u.id) "
                "AS submission_count, "
                "(SELECT COUNT(*) FROM usage_reservations r WHERE r.user_id=u.id "
                "AND r.reason=?) AS unbilled_jobs "
                "FROM users u WHERE u.is_unlimited=1 "
                "ORDER BY u.unlimited_granted_at DESC, u.id DESC LIMIT ?",
                (UNLIMITED_USAGE_REASON, max(1, min(limit, 200))),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def create_submission(
        self,
        user_id: int,
        file_id: str,
        duration: float | None,
        filename: str | None,
        mime_type: str | None,
        source_type: str = "audio",
    ) -> int:
        async with self._transaction() as db:
            cursor = await db.execute(
                "INSERT INTO audio_submissions "
                "(user_id, file_id, duration, received_at, status, original_filename, "
                "mime_type, source_type) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                (user_id, file_id, duration, utc_now(), filename, mime_type, source_type),
            )
            return int(cursor.lastrowid)

    async def submission_user_id(self, submission_id: int) -> int:
        """Return the registered user who owns a queued submission."""
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT user_id FROM audio_submissions WHERE id=?", (submission_id,)
            )
            row = await cursor.fetchone()
            if not row:
                raise ValueError("Unknown submission")
            return int(row["user_id"])

    async def save_presentation_details(
        self,
        submission_id: int,
        slide_count: int,
        clips: list[dict[str, Any]],
        media_duration: float | None = None,
    ) -> None:
        """Store deck statistics and one row per extracted media clip."""
        async with self._transaction() as db:
            await db.execute(
                "UPDATE audio_submissions SET slide_count=?, clip_count=?, "
                "media_duration=?, duration=COALESCE(?, duration) WHERE id=?",
                (slide_count, len(clips), media_duration, media_duration, submission_id),
            )
            await db.execute(
                "DELETE FROM presentation_clips WHERE submission_id=?", (submission_id,)
            )
            if clips:
                await db.executemany(
                    "INSERT INTO presentation_clips "
                    "(submission_id, slide_number, part_name, kind, duration, included, skip_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            submission_id,
                            clip.get("slide_number"),
                            str(clip.get("part_name", "")),
                            str(clip.get("kind", "audio")),
                            clip.get("duration"),
                            int(bool(clip.get("included", True))),
                            clip.get("skip_reason"),
                        )
                        for clip in clips
                    ],
                )

    async def presentation_clips(self, submission_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT slide_number, part_name, kind, duration, included, skip_reason "
                "FROM presentation_clips WHERE submission_id=? ORDER BY id",
                (submission_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def set_submission_status(
        self, submission_id: int, status: str, error_message: str | None = None
    ) -> None:
        async with self._transaction() as db:
            await db.execute(
                "UPDATE audio_submissions SET status=?, error_message=? WHERE id=?",
                (status, error_message, submission_id),
            )

    async def save_transcription(
        self,
        submission_id: int,
        engine: str,
        raw_text: str,
        structured_text: str,
    ) -> None:
        async with self._transaction() as db:
            await db.execute(
                "INSERT INTO transcriptions "
                "(submission_id, stt_engine, raw_transcript, structured_text, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(submission_id) DO UPDATE SET "
                "stt_engine=excluded.stt_engine, raw_transcript=excluded.raw_transcript, "
                "structured_text=excluded.structured_text, created_at=excluded.created_at",
                (submission_id, engine, raw_text, structured_text, utc_now()),
            )

    async def user_summaries(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT u.telegram_id, u.username, u.first_seen, u.is_banned, "
                "u.is_unlimited, COUNT(s.id) AS submission_count FROM users u "
                "LEFT JOIN audio_submissions s ON s.user_id=u.id "
                "GROUP BY u.id ORDER BY u.first_seen DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def user_ids(self, include_banned: bool = False) -> list[int]:
        query = "SELECT telegram_id FROM users"
        if not include_banned:
            query += " WHERE is_banned=0"
        async with self._lock:
            cursor = await self._db().execute(query)
            return [int(row[0]) for row in await cursor.fetchall()]

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT (SELECT COUNT(*) FROM users) AS users, "
                "(SELECT COUNT(*) FROM users WHERE is_banned=0) AS unbanned_users, "
                "(SELECT COUNT(*) FROM users WHERE is_unlimited=1) AS unlimited_users, "
                "(SELECT COUNT(*) FROM audio_submissions) AS submissions, "
                "(SELECT COUNT(*) FROM audio_submissions WHERE status='done') AS done, "
                "(SELECT COUNT(*) FROM audio_submissions WHERE status='failed') AS failed, "
                "(SELECT COUNT(*) FROM audio_submissions WHERE source_type='video') AS videos, "
                "(SELECT COUNT(*) FROM audio_submissions WHERE source_type='pptx') AS presentations, "
                "(SELECT COALESCE(SUM(clip_count), 0) FROM audio_submissions "
                "WHERE source_type='pptx') AS presentation_clips, "
                "(SELECT COUNT(DISTINCT user_id) FROM audio_submissions "
                "WHERE datetime(substr(received_at, 1, 19)) >= datetime('now', '-30 days')) AS active_30d, "
                "(SELECT COUNT(*) FROM payment_requests WHERE status='pending') AS pending_payments, "
                "(SELECT COUNT(*) FROM payment_requests WHERE status='approved') AS approved_payments, "
                "(SELECT COALESCE(SUM(amount_toman), 0) FROM payment_requests "
                "WHERE status='approved') AS revenue_toman"
            )
            return dict(await cursor.fetchone())

    async def add_broadcast(
        self, admin_id: int, message: str, recipient_count: int
    ) -> None:
        async with self._transaction() as db:
            now = utc_now()
            cursor = await db.execute(
                "INSERT INTO admin_broadcasts(admin_id, message, sent_at, recipient_count) "
                "VALUES (?, ?, ?, ?)",
                (admin_id, message, now, recipient_count),
            )
            await self._insert_audit(
                db, admin_id, "broadcast", "broadcast", str(cursor.lastrowid),
                {"recipient_count": recipient_count, "message_characters": len(message)},
            )

    async def sync_plan_catalog(self, plans: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> None:
        """Upsert the canonical catalogue; existing entitlements are immutable snapshots.

        Rows an administrator created or edited from the plan panel are marked
        ``is_custom`` and are never overwritten, and the ``enabled`` flag of an
        existing row is always preserved: disabling a plan (or taking ownership
        of its price) is an operator decision that must survive a restart.
        """
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            for plan in plans:
                seconds = int(plan["included_seconds"])
                price = int(plan["price_toman"])
                validity = plan.get("validity_days")
                if seconds <= 0 or price < 0 or (validity is not None and int(validity) <= 0):
                    raise ValueError("Invalid canonical plan values")
                await db.execute(
                    "INSERT INTO plans(code, name, included_seconds, price_toman, validity_days, "
                    "is_free, sort_order, enabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
                    "included_seconds=excluded.included_seconds, price_toman=excluded.price_toman, "
                    "validity_days=excluded.validity_days, is_free=excluded.is_free, "
                    "sort_order=excluded.sort_order, updated_at=excluded.updated_at "
                    "WHERE plans.is_custom = 0",
                    (
                        str(plan["code"]), str(plan["name"]), seconds, price,
                        int(validity) if validity is not None else None,
                        int(bool(plan.get("is_free", False))), int(plan.get("sort_order", 0)),
                        now, now,
                    ),
                )

    async def list_plans(
        self, *, paid_only: bool = False, include_disabled: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM plans WHERE 1=1"
        if not include_disabled:
            query += " AND enabled=1"
        if paid_only:
            query += " AND is_free=0"
        query += " ORDER BY sort_order, id"
        async with self._lock:
            cursor = await self._db().execute(query)
            return [dict(row) for row in await cursor.fetchall()]

    async def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute("SELECT * FROM plans WHERE id=?", (plan_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    def _validate_plan_values(
        *, name: str | None, included_seconds: int, price_toman: int, validity_days: int | None
    ) -> tuple[str | None, int, int, int | None]:
        """Validate one plan tariff; raises ``ValueError`` with Persian copy."""
        cleaned_name = clean_human_text(name, limit=60) if name is not None else None
        if name is not None and not cleaned_name:
            raise ValueError("نام طرح نمی‌تواند خالی باشد.")
        seconds = int(included_seconds)
        price = int(price_toman)
        validity = None if validity_days is None else int(validity_days)
        if seconds <= 0 or seconds % SECONDS_PER_HOUR:
            raise ValueError("مدت طرح باید تعداد صحیحی از ساعت باشد.")
        if not 1 <= seconds // SECONDS_PER_HOUR <= MAX_PLAN_HOURS:
            raise ValueError(f"مدت طرح باید بین ۱ و {MAX_PLAN_HOURS} ساعت باشد.")
        if not 1 <= price <= MAX_PLAN_PRICE_TOMAN:
            raise ValueError(f"قیمت طرح باید بین ۱ و {MAX_PLAN_PRICE_TOMAN} تومان باشد.")
        if validity is not None and not 1 <= validity <= MAX_PLAN_VALIDITY_DAYS:
            raise ValueError(
                f"اعتبار طرح باید بین ۱ و {MAX_PLAN_VALIDITY_DAYS} روز باشد (۰ برای بدون انقضا)."
            )
        return cleaned_name, seconds, price, validity

    async def create_plan(
        self,
        *,
        code: str,
        name: str,
        hours: int,
        price_toman: int,
        validity_days: int | None,
        admin_id: int,
    ) -> dict[str, Any]:
        """Create an administrator-owned plan; it is never sold twice for a code."""
        cleaned_code = clean_human_text(code, limit=40) or ""
        cleaned_code = cleaned_code.strip().lower()
        if not _PLAN_CODE_RE.fullmatch(cleaned_code):
            raise ValueError(
                "کد طرح باید با حرف لاتین شروع شود و فقط شامل حروف کوچک، رقم و _ باشد."
            )
        cleaned_name, seconds, price, validity = self._validate_plan_values(
            name=name,
            included_seconds=int(hours) * SECONDS_PER_HOUR,
            price_toman=price_toman,
            validity_days=validity_days,
        )
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute("SELECT id FROM plans WHERE code=?", (cleaned_code,))
            if await cursor.fetchone():
                raise ValueError("طرحی با این کد از قبل وجود دارد؛ کد دیگری انتخاب کنید.")
            cursor = await db.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM plans")
            sort_order = int((await cursor.fetchone())[0])
            cursor = await db.execute(
                "INSERT INTO plans(code, name, included_seconds, price_toman, validity_days, "
                "is_free, sort_order, enabled, is_custom, created_by_admin_id, "
                "updated_by_admin_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, 1, 1, ?, ?, ?, ?)",
                (cleaned_code, cleaned_name, seconds, price, validity, sort_order,
                 admin_id, admin_id, now, now),
            )
            plan_id = int(cursor.lastrowid)
            await self._insert_audit(
                db, admin_id, "plan_created", "plan", str(plan_id),
                {
                    "code": cleaned_code,
                    "included_seconds": seconds,
                    "price_toman": price,
                    "validity_days": validity,
                },
            )
            return {
                "id": plan_id,
                "code": cleaned_code,
                "name": cleaned_name,
                "included_seconds": seconds,
                "price_toman": price,
                "validity_days": validity,
                "sort_order": sort_order,
                "is_custom": 1,
                "enabled": 1,
            }

    async def update_plan(
        self, plan_id: int, changes: dict[str, Any], admin_id: int
    ) -> dict[str, Any] | None:
        """Apply an administrator edit; the row becomes administrator-owned."""
        unknown = set(changes) - {"name", "hours", "price_toman", "validity_days"}
        if unknown or not changes:
            raise ValueError("فیلدهای ویرایش طرح معتبر نیستند.")
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute("SELECT * FROM plans WHERE id=?", (int(plan_id),))
            row = await cursor.fetchone()
            if not row:
                return None
            if int(row["is_free"]):
                raise ValueError("طرح رایگان از این پنل قابل ویرایش نیست.")
            hours = (
                int(changes["hours"])
                if "hours" in changes
                else int(row["included_seconds"]) // SECONDS_PER_HOUR
            )
            validity = (
                changes["validity_days"] if "validity_days" in changes else row["validity_days"]
            )
            name = changes["name"] if "name" in changes else str(row["name"])
            price = int(changes["price_toman"]) if "price_toman" in changes else int(row["price_toman"])
            cleaned_name, seconds, price, validity = self._validate_plan_values(
                name=name,
                included_seconds=hours * SECONDS_PER_HOUR,
                price_toman=price,
                validity_days=None if validity is None else int(validity),
            )
            await db.execute(
                "UPDATE plans SET name=?, included_seconds=?, price_toman=?, validity_days=?, "
                "is_custom=1, updated_by_admin_id=?, updated_at=? WHERE id=?",
                (cleaned_name, seconds, price, validity, admin_id, now, int(plan_id)),
            )
            await self._insert_audit(
                db, admin_id, "plan_updated", "plan", str(plan_id),
                {
                    "code": str(row["code"]),
                    "included_seconds": seconds,
                    "price_toman": price,
                    "validity_days": validity,
                },
            )
            cursor = await db.execute("SELECT * FROM plans WHERE id=?", (int(plan_id),))
            return dict(await cursor.fetchone())

    async def set_plan_enabled(self, plan_id: int, enabled: bool, admin_id: int) -> bool:
        """Enable/disable a plan. The free plan must stay available for signup."""
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT code, is_free, enabled FROM plans WHERE id=?", (int(plan_id),)
            )
            row = await cursor.fetchone()
            if not row:
                return False
            if int(row["is_free"]) and not enabled:
                raise ValueError(
                    "طرح رایگان را نمی‌توان غیرفعال کرد؛ ثبت‌نام کاربران جدید به آن وابسته است."
                )
            if int(row["enabled"]) == int(bool(enabled)):
                return False
            await db.execute(
                "UPDATE plans SET enabled=?, is_custom=1, updated_by_admin_id=?, updated_at=? "
                "WHERE id=?",
                (int(bool(enabled)), admin_id, now, int(plan_id)),
            )
            await self._insert_audit(
                db, admin_id, "plan_enabled" if enabled else "plan_disabled", "plan",
                str(plan_id), {"code": str(row["code"])},
            )
            return True

    async def delete_plan(self, plan_id: int, admin_id: int) -> bool:
        """Delete an administrator-created plan that no accounting row refers to."""
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT code, is_free, is_custom FROM plans WHERE id=?", (int(plan_id),)
            )
            row = await cursor.fetchone()
            if not row:
                return False
            if int(row["is_free"]):
                raise ValueError("طرح رایگان قابل حذف نیست.")
            if not int(row["is_custom"]):
                raise ValueError(
                    "طرح‌های پیش‌فرض قابل حذف نیستند؛ برای حذف آن‌ها را غیرفعال کنید "
                    "(با راه‌اندازی مجدد برمی‌گردند)."
                )
            for table in ("entitlements", "payment_requests"):
                cursor = await db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE plan_id=?", (int(plan_id),)
                )
                if int((await cursor.fetchone())[0]):
                    raise ValueError(
                        "این طرح سابقهٔ خرید یا اعتبار دارد و حذف نمی‌شود؛ آن را غیرفعال کنید."
                    )
            await db.execute("DELETE FROM plans WHERE id=?", (int(plan_id),))
            await self._insert_audit(
                db, admin_id, "plan_deleted", "plan", str(plan_id), {"code": str(row["code"])}
            )
            return True

    async def _expire_entitlements_in_transaction(
        self, db: aiosqlite.Connection, user_id: int, now: str
    ) -> None:
        cursor = await db.execute(
            "SELECT id, remaining_seconds FROM entitlements WHERE user_id=? AND status='active' "
            "AND expires_at IS NOT NULL AND datetime(expires_at)<=datetime(?)",
            (user_id, now),
        )
        rows = await cursor.fetchall()
        for row in rows:
            entitlement_id = int(row["id"])
            cursor = await db.execute(
                "UPDATE entitlements SET status='expired', updated_at=? "
                "WHERE id=? AND status='active'",
                (now, entitlement_id),
            )
            if cursor.rowcount and int(row["remaining_seconds"]):
                await db.execute(
                    "INSERT INTO usage_ledger "
                    "(entitlement_id, user_id, event_type, released_seconds, reason, created_at) "
                    "SELECT id, user_id, 'adjustment', remaining_seconds, "
                    "'Paid entitlement expired', ? FROM entitlements WHERE id=?",
                    (now, entitlement_id),
                )

    async def _available_seconds_in_transaction(
        self, db: aiosqlite.Connection, user_id: int, now: str
    ) -> int:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(remaining_seconds), 0) FROM entitlements "
            "WHERE user_id=? AND status='active' AND datetime(starts_at)<=datetime(?) "
            "AND (expires_at IS NULL OR datetime(expires_at)>datetime(?))",
            (user_id, now, now),
        )
        return int((await cursor.fetchone())[0])

    async def user_balance(self, user_id: int) -> dict[str, Any]:
        """Available active seconds and their expiry-ordered source entitlements."""
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            await self._expire_entitlements_in_transaction(db, user_id, now)
            cursor = await db.execute(
                "SELECT e.id, e.granted_seconds, e.remaining_seconds, e.starts_at, e.expires_at, "
                "e.source, e.status, p.code AS plan_code, p.name AS plan_name "
                "FROM entitlements e JOIN plans p ON p.id=e.plan_id WHERE e.user_id=? "
                "AND e.status='active' AND datetime(e.starts_at)<=datetime(?) "
                "AND (e.expires_at IS NULL OR datetime(e.expires_at)>datetime(?)) "
                "ORDER BY CASE WHEN e.expires_at IS NULL THEN 1 ELSE 0 END, e.expires_at, e.id",
                (user_id, now, now),
            )
            entitlements = [dict(row) for row in await cursor.fetchall()]
            return {
                "available_seconds": sum(int(row["remaining_seconds"]) for row in entitlements),
                "entitlements": entitlements,
            }

    async def create_payment_request(self, user_id: int, plan_code: str) -> dict[str, Any]:
        """Create one manual-payment intent; an open request is never duplicated."""
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT id, name, included_seconds, price_toman, validity_days, is_free "
                "FROM plans WHERE code=? AND enabled=1",
                (plan_code,),
            )
            plan = await cursor.fetchone()
            if not plan or plan["is_free"] or int(plan["price_toman"]) <= 0:
                raise ValueError("The selected paid plan is unavailable")
            cursor = await db.execute(
                "SELECT id FROM payment_requests WHERE user_id=? "
                "AND status IN ('awaiting_receipt', 'pending') ORDER BY id DESC LIMIT 1",
                (user_id,),
            )
            current = await cursor.fetchone()
            if current:
                request_id = int(current["id"])
            else:
                cursor = await db.execute(
                    "INSERT INTO payment_requests(user_id, plan_id, amount_toman, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'awaiting_receipt', ?, ?)",
                    (user_id, int(plan["id"]), int(plan["price_toman"]), now, now),
                )
                request_id = int(cursor.lastrowid)
            cursor = await db.execute(
                "SELECT pr.id, pr.user_id, pr.amount_toman, pr.status, pr.created_at, "
                "pr.receipt_submitted_at, p.code AS plan_code, p.name AS plan_name, "
                "p.included_seconds, p.validity_days FROM payment_requests pr "
                "JOIN plans p ON p.id=pr.plan_id WHERE pr.id=?",
                (request_id,),
            )
            return dict(await cursor.fetchone())

    async def get_awaiting_payment(self, user_id: int) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT pr.id, pr.status, pr.amount_toman, pr.created_at, "
                "p.code AS plan_code, p.name AS plan_name, p.included_seconds "
                "FROM payment_requests pr JOIN plans p ON p.id=pr.plan_id "
                "WHERE pr.user_id=? AND pr.status='awaiting_receipt' ORDER BY pr.id DESC LIMIT 1",
                (user_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def current_payment_request(self, user_id: int) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT pr.id, pr.status, pr.amount_toman, pr.created_at, pr.receipt_submitted_at, "
                "p.code AS plan_code, p.name AS plan_name, p.included_seconds "
                "FROM payment_requests pr JOIN plans p ON p.id=pr.plan_id "
                "WHERE pr.user_id=? AND pr.status IN ('awaiting_receipt','pending') "
                "ORDER BY pr.id DESC LIMIT 1",
                (user_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def cancel_payment_intent(self, payment_id: int, user_id: int) -> bool:
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "UPDATE payment_requests SET status='cancelled', updated_at=? "
                "WHERE id=? AND user_id=? AND status='awaiting_receipt'",
                (now, payment_id, user_id),
            )
            return cursor.rowcount == 1

    async def submit_payment_receipt(
        self,
        payment_id: int,
        user_id: int,
        receipt_path: str,
        message_id: int,
        *,
        receipt_file_id: str | None = None,
    ) -> bool:
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "UPDATE payment_requests SET status='pending', receipt_path=?, "
                "receipt_message_id=?, receipt_file_id=?, receipt_submitted_at=?, updated_at=? "
                "WHERE id=? AND user_id=? AND status='awaiting_receipt'",
                (receipt_path, message_id, receipt_file_id, now, now, payment_id, user_id),
            )
            return cursor.rowcount == 1

    async def list_pending_payments(self, *, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT pr.id, pr.amount_toman, pr.status, pr.created_at, pr.receipt_submitted_at, "
                "u.telegram_id, u.username, p.code AS plan_code, p.name AS plan_name, "
                "p.included_seconds FROM payment_requests pr "
                "JOIN users u ON u.id=pr.user_id JOIN plans p ON p.id=pr.plan_id "
                "WHERE pr.status='pending' ORDER BY pr.created_at, pr.id LIMIT ? OFFSET ?",
                (max(1, min(limit, 50)), max(0, offset)),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def payment_detail(self, payment_id: int) -> dict[str, Any] | None:
        """Admin-only handler data, including the private local receipt path."""
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT pr.id, pr.amount_toman, pr.status, pr.created_at, pr.receipt_submitted_at, "
                "pr.receipt_path, pr.receipt_message_id, pr.receipt_file_id, pr.reviewed_at, "
                "pr.reviewer_telegram_id, pr.rejection_reason, pr.admin_note, "
                "u.id AS user_id, u.telegram_id, u.username, "
                "p.code AS plan_code, p.name AS plan_name, p.included_seconds, p.validity_days "
                "FROM payment_requests pr JOIN users u ON u.id=pr.user_id "
                "JOIN plans p ON p.id=pr.plan_id WHERE pr.id=?",
                (payment_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def payment_history(self, user_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT pr.id, pr.amount_toman, pr.status, pr.created_at, pr.receipt_submitted_at, "
                "p.code AS plan_code, p.name AS plan_name, p.included_seconds "
                "FROM payment_requests pr JOIN plans p ON p.id=pr.plan_id "
                "WHERE pr.user_id=? ORDER BY pr.id DESC LIMIT ?",
                (user_id, max(1, min(limit, 50))),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def approve_payment(
        self, payment_id: int, admin_id: int, *, admin_note: str | None = None
    ) -> dict[str, Any] | None:
        """Atomically approve once and grant the plan snapshot exactly once."""
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        note = self._clean_note(admin_note)
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT pr.id, pr.user_id, pr.amount_toman, pr.status, u.telegram_id, "
                "p.id AS plan_id, p.code AS plan_code, p.name AS plan_name, "
                "p.included_seconds, p.validity_days FROM payment_requests pr "
                "JOIN users u ON u.id=pr.user_id JOIN plans p ON p.id=pr.plan_id "
                "WHERE pr.id=?",
                (payment_id,),
            )
            request = await cursor.fetchone()
            if not request or request["status"] != "pending":
                return None
            cursor = await db.execute(
                "UPDATE payment_requests SET status='approved', reviewed_at=?, "
                "reviewer_telegram_id=?, admin_note=?, updated_at=? "
                "WHERE id=? AND status='pending'",
                (now, admin_id, note, now, payment_id),
            )
            if cursor.rowcount != 1:
                return None
            seconds = int(request["included_seconds"])
            validity = request["validity_days"]
            expires = (
                (now_dt + timedelta(days=int(validity))).isoformat()
                if validity is not None else None
            )
            cursor = await db.execute(
                "INSERT INTO entitlements "
                "(user_id, plan_id, payment_id, granted_seconds, remaining_seconds, starts_at, "
                "expires_at, status, source, granted_by_admin_id, reason, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 'payment', ?, ?, ?, ?)",
                (
                    int(request["user_id"]), int(request["plan_id"]), payment_id,
                    seconds, seconds, now, expires, admin_id,
                    f"Approved payment {payment_id}", now, now,
                ),
            )
            entitlement_id = int(cursor.lastrowid)
            await db.execute(
                "INSERT INTO usage_ledger "
                "(entitlement_id, user_id, event_type, reserved_seconds, admin_telegram_id, reason, created_at) "
                "VALUES (?, ?, 'grant', ?, ?, ?, ?)",
                (entitlement_id, int(request["user_id"]), seconds, admin_id,
                 f"Approved payment {payment_id}", now),
            )
            await self._insert_audit(
                db, admin_id, "payment_approved", "payment", str(payment_id),
                {
                    "user_telegram_id": int(request["telegram_id"]),
                    "plan_code": str(request["plan_code"]),
                    "granted_seconds": seconds,
                    "amount_toman": int(request["amount_toman"]),
                    "entitlement_id": entitlement_id,
                },
            )
            return {
                "payment_id": payment_id,
                "user_telegram_id": int(request["telegram_id"]),
                "plan_code": str(request["plan_code"]),
                "plan_name": str(request["plan_name"]),
                "granted_seconds": seconds,
                "amount_toman": int(request["amount_toman"]),
                "expires_at": expires,
            }

    @staticmethod
    def _clean_note(note: str | None) -> str | None:
        """Sanitize an administrator note; it is stored, never trusted as markup."""
        return clean_human_text(note)

    async def reject_payment(
        self, payment_id: int, admin_id: int, reason: str, *, admin_note: str | None = None
    ) -> bool:
        reason = self._clean_note(reason) or ""
        if not reason:
            raise ValueError("A rejection reason is required")
        note = self._clean_note(admin_note) or reason
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT id, user_id, amount_toman, status FROM payment_requests WHERE id=?",
                (payment_id,),
            )
            request = await cursor.fetchone()
            if not request or request["status"] != "pending":
                return False
            cursor = await db.execute(
                "UPDATE payment_requests SET status='rejected', rejection_reason=?, reviewed_at=?, "
                "reviewer_telegram_id=?, admin_note=?, updated_at=? WHERE id=? AND status='pending'",
                (reason, now, admin_id, note, now, payment_id),
            )
            if cursor.rowcount != 1:
                return False
            cursor = await db.execute(
                "SELECT telegram_id FROM users WHERE id=?", (int(request["user_id"]),)
            )
            telegram_id = int((await cursor.fetchone())[0])
            await self._insert_audit(
                db, admin_id, "payment_rejected", "payment", str(payment_id),
                {"user_telegram_id": telegram_id, "amount_toman": int(request["amount_toman"]), "reason": reason},
            )
            return True

    async def add_admin_credit(
        self, telegram_id: int, seconds: int, admin_id: int, reason: str
    ) -> dict[str, Any] | None:
        seconds = int(seconds)
        reason = clean_human_text(reason) or ""
        if seconds <= 0 or seconds > 10_000_000 or not reason:
            raise ValueError("Credit seconds or audit reason is invalid")
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,))
            user = await cursor.fetchone()
            if not user:
                return None
            user_id = int(user["id"])
            plan = await self._free_plan_row(db)
            if not plan:
                raise RuntimeError("Canonical plan catalogue is missing")
            cursor = await db.execute(
                "INSERT INTO entitlements "
                "(user_id, plan_id, granted_seconds, remaining_seconds, starts_at, expires_at, "
                "status, source, granted_by_admin_id, reason, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, 'active', 'admin_credit', ?, ?, ?, ?)",
                (user_id, int(plan["id"]), seconds, seconds, now, admin_id, reason, now, now),
            )
            entitlement_id = int(cursor.lastrowid)
            await db.execute(
                "INSERT INTO usage_ledger "
                "(entitlement_id, user_id, event_type, reserved_seconds, admin_telegram_id, reason, created_at) "
                "VALUES (?, ?, 'grant', ?, ?, ?, ?)",
                (entitlement_id, user_id, seconds, admin_id, reason, now),
            )
            await self._insert_audit(
                db, admin_id, "credit_granted", "user", str(telegram_id),
                {"seconds": seconds, "reason": reason, "entitlement_id": entitlement_id},
            )
            return {"telegram_id": telegram_id, "seconds": seconds, "entitlement_id": entitlement_id}

    async def _reserve_unlimited_in_transaction(
        self,
        db: aiosqlite.Connection,
        user_id: int,
        submission_id: int,
        seconds: int,
        now: str,
    ) -> dict[str, Any]:
        """Record a special user's media without touching any entitlement.

        The row is written as already consumed because nothing is billed and
        therefore nothing can be refunded. It is idempotent by ``submission_id``
        exactly like the billed path, and it always answers ``ok`` so a special
        user can never be blocked by the balance check.
        """
        cursor = await db.execute(
            "SELECT id, required_seconds, reserved_seconds, status FROM usage_reservations "
            "WHERE submission_id=?",
            (submission_id,),
        )
        existing = await cursor.fetchone()
        if existing:
            return {
                "ok": True,
                "unlimited": True,
                "reservation_id": int(existing["id"]),
                "required_seconds": int(existing["required_seconds"]),
                "reserved_seconds": int(existing["reserved_seconds"]),
                "existing": True,
            }
        cursor = await db.execute(
            "INSERT INTO usage_reservations "
            "(submission_id, user_id, required_seconds, reserved_seconds, consumed_seconds, "
            "status, reason, created_at, finalized_at) "
            "VALUES (?, ?, ?, 0, ?, 'consumed', ?, ?, ?)",
            (submission_id, user_id, seconds, seconds, UNLIMITED_USAGE_REASON, now, now),
        )
        reservation_id = int(cursor.lastrowid)
        await db.execute(
            "INSERT INTO usage_ledger "
            "(reservation_id, submission_id, user_id, event_type, requested_seconds, "
            "consumed_seconds, reason, created_at) "
            "VALUES (?, ?, ?, 'consume', ?, ?, ?, ?)",
            (reservation_id, submission_id, user_id, seconds, seconds,
             UNLIMITED_USAGE_REASON, now),
        )
        return {
            "ok": True,
            "unlimited": True,
            "reservation_id": reservation_id,
            "required_seconds": seconds,
            "reserved_seconds": 0,
            "existing": False,
        }

    async def reserve_usage(self, user_id: int, submission_id: int, seconds: int) -> dict[str, Any]:
        """Reserve earliest-expiring credits under a SQLite IMMEDIATE transaction.

        A special (unlimited) user is never billed: their media is recorded in
        the same tables for the audit trail, but no entitlement is touched and
        the answer always carries ``unlimited=True``.
        """
        seconds = int(seconds)
        if seconds <= 0:
            raise ValueError("Reserved seconds must be positive")
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT user_id FROM audio_submissions WHERE id=?", (submission_id,)
            )
            submission = await cursor.fetchone()
            if not submission or int(submission["user_id"]) != user_id:
                raise ValueError("Submission does not belong to this user")
            if await self._user_is_unlimited_in_transaction(db, user_id):
                return await self._reserve_unlimited_in_transaction(
                    db, user_id, submission_id, seconds, now
                )
            cursor = await db.execute(
                "SELECT id, required_seconds, reserved_seconds, status FROM usage_reservations "
                "WHERE submission_id=?",
                (submission_id,),
            )
            existing = await cursor.fetchone()
            if existing:
                if existing["status"] == "reserved" and int(existing["required_seconds"]) == seconds:
                    available = await self._available_seconds_in_transaction(db, user_id, now)
                    return {
                        "ok": True, "reservation_id": int(existing["id"]),
                        "available_seconds": available, "required_seconds": seconds,
                        "reserved_seconds": int(existing["reserved_seconds"]), "existing": True,
                    }
                return {
                    "ok": False, "reservation_id": int(existing["id"]),
                    "available_seconds": await self._available_seconds_in_transaction(db, user_id, now),
                    "required_seconds": seconds, "reason": "already_finalized",
                }
            await self._expire_entitlements_in_transaction(db, user_id, now)
            available = await self._available_seconds_in_transaction(db, user_id, now)
            if available < seconds:
                cursor = await db.execute(
                    "INSERT INTO usage_reservations "
                    "(submission_id, user_id, required_seconds, reserved_seconds, status, reason, created_at) "
                    "VALUES (?, ?, ?, 0, 'insufficient', 'Insufficient available balance', ?)",
                    (submission_id, user_id, seconds, now),
                )
                reservation_id = int(cursor.lastrowid)
                await db.execute(
                    "INSERT INTO usage_ledger "
                    "(reservation_id, submission_id, user_id, event_type, requested_seconds, "
                    "available_seconds, reason, created_at) "
                    "VALUES (?, ?, ?, 'denied', ?, ?, 'Insufficient available balance', ?)",
                    (reservation_id, submission_id, user_id, seconds, available, now),
                )
                return {
                    "ok": False, "reservation_id": reservation_id,
                    "available_seconds": available, "required_seconds": seconds,
                    "reason": "insufficient",
                }
            cursor = await db.execute(
                "INSERT INTO usage_reservations "
                "(submission_id, user_id, required_seconds, reserved_seconds, status, created_at) "
                "VALUES (?, ?, ?, ?, 'reserved', ?)",
                (submission_id, user_id, seconds, seconds, now),
            )
            reservation_id = int(cursor.lastrowid)
            cursor = await db.execute(
                "SELECT id, remaining_seconds FROM entitlements WHERE user_id=? AND status='active' "
                "AND datetime(starts_at)<=datetime(?) "
                "AND (expires_at IS NULL OR datetime(expires_at)>datetime(?)) "
                "AND remaining_seconds>0 "
                "ORDER BY CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END, expires_at, id",
                (user_id, now, now),
            )
            entitlements = await cursor.fetchall()
            remaining = seconds
            for entitlement in entitlements:
                if remaining <= 0:
                    break
                entitlement_id = int(entitlement["id"])
                allocation = min(remaining, int(entitlement["remaining_seconds"]))
                cursor = await db.execute(
                    "UPDATE entitlements SET remaining_seconds=remaining_seconds-?, updated_at=? "
                    "WHERE id=? AND status='active' AND remaining_seconds>=?",
                    (allocation, now, entitlement_id, allocation),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Entitlement changed while reserving usage")
                await db.execute(
                    "INSERT INTO usage_ledger "
                    "(reservation_id, entitlement_id, submission_id, user_id, event_type, "
                    "requested_seconds, reserved_seconds, available_seconds, reason, created_at) "
                    "VALUES (?, ?, ?, ?, 'reserve', ?, ?, ?, 'Media-duration reservation', ?)",
                    (reservation_id, entitlement_id, submission_id, user_id, seconds,
                     allocation, available - seconds, now),
                )
                remaining -= allocation
            if remaining:
                raise RuntimeError("Balance changed while reserving usage")
            return {
                "ok": True, "reservation_id": reservation_id,
                "available_seconds": available - seconds, "required_seconds": seconds,
                "reserved_seconds": seconds, "existing": False,
            }

    async def finalize_usage(self, submission_id: int, consumed_seconds: int) -> bool:
        """Consume actual integer seconds and return every unused reserved second."""
        consumed_seconds = int(consumed_seconds)
        if consumed_seconds < 0:
            raise ValueError("Consumed seconds cannot be negative")
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT id, user_id, reserved_seconds, status FROM usage_reservations WHERE submission_id=?",
                (submission_id,),
            )
            reservation = await cursor.fetchone()
            if not reservation or reservation["status"] != "reserved":
                return False
            reservation_id = int(reservation["id"])
            reserved = int(reservation["reserved_seconds"])
            user_id = int(reservation["user_id"])
            if consumed_seconds > reserved:
                raise ValueError("Consumed seconds exceed the reserved amount")
            cursor = await db.execute(
                "SELECT entitlement_id, reserved_seconds FROM usage_ledger "
                "WHERE reservation_id=? AND event_type='reserve' ORDER BY id",
                (reservation_id,),
            )
            allocations = await cursor.fetchall()
            if sum(int(row["reserved_seconds"]) for row in allocations) != reserved:
                raise RuntimeError("Usage reservation allocation ledger is inconsistent")
            consume_left = consumed_seconds
            consumed_total = 0
            released_total = 0
            for row in allocations:
                entitlement_id = int(row["entitlement_id"])
                allocation = int(row["reserved_seconds"])
                used = min(consume_left, allocation)
                refund = allocation - used
                consume_left -= used
                if used:
                    await db.execute(
                        "INSERT INTO usage_ledger "
                        "(reservation_id, entitlement_id, submission_id, user_id, event_type, "
                        "consumed_seconds, reason, created_at) "
                        "VALUES (?, ?, ?, ?, 'consume', ?, 'Completed media processing', ?)",
                        (reservation_id, entitlement_id, submission_id, user_id, used, now),
                    )
                    consumed_total += used
                if refund:
                    await db.execute(
                        "UPDATE entitlements SET remaining_seconds=remaining_seconds+?, updated_at=? WHERE id=?",
                        (refund, now, entitlement_id),
                    )
                    await db.execute(
                        "INSERT INTO usage_ledger "
                        "(reservation_id, entitlement_id, submission_id, user_id, event_type, "
                        "released_seconds, reason, created_at) "
                        "VALUES (?, ?, ?, ?, 'release', ?, 'Unused reservation returned', ?)",
                        (reservation_id, entitlement_id, submission_id, user_id, refund, now),
                    )
                    released_total += refund
            if consume_left:
                raise RuntimeError("Usage allocation ledger is inconsistent")
            status = (
                "consumed" if consumed_total == reserved
                else "released" if consumed_total == 0
                else "partially_consumed"
            )
            await db.execute(
                "UPDATE usage_reservations SET consumed_seconds=?, released_seconds=?, status=?, "
                "reason='Processing completed', finalized_at=? WHERE id=? AND status='reserved'",
                (consumed_total, released_total, status, now, reservation_id),
            )
            return True

    async def release_usage(self, submission_id: int, reason: str) -> int:
        reason = clean_human_text(reason) or ""
        async with self._transaction(immediate=True) as db:
            return await self._release_reservation_in_transaction(db, submission_id, reason or "Processing failed")

    async def usage_reservation(self, submission_id: int) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM usage_reservations WHERE submission_id=?", (submission_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def usage_ledger(self, user_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT l.*, e.source AS entitlement_source FROM usage_ledger l "
                "LEFT JOIN entitlements e ON e.id=l.entitlement_id "
                "WHERE l.user_id=? ORDER BY l.id DESC LIMIT ?",
                (user_id, max(1, min(limit, 200))),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def admin_user_credit_overview(self, telegram_id: int) -> dict[str, Any] | None:
        """Everything the admin credit screen shows for one user.

        Read-only and index-backed: the free/paid split comes from the same
        entitlement rows the reservation engine consumes, so the screen can
        never disagree with billing.
        """
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT id, telegram_id, username, is_banned, first_seen, is_unlimited, "
                "unlimited_reason, unlimited_granted_at FROM users WHERE telegram_id=?",
                (telegram_id,),
            )
            user = await cursor.fetchone()
            if not user:
                return None
            user_id = int(user["id"])
            await self._expire_entitlements_in_transaction(db, user_id, now)
            cursor = await db.execute(
                "SELECT e.id, e.granted_seconds, e.remaining_seconds, e.starts_at, e.expires_at, "
                "e.status, e.source, p.code AS plan_code, p.name AS plan_name "
                "FROM entitlements e JOIN plans p ON p.id=e.plan_id WHERE e.user_id=? "
                "ORDER BY e.id DESC LIMIT 50",
                (user_id,),
            )
            entitlements = [dict(row) for row in await cursor.fetchall()]
            free_seconds = sum(
                int(row["remaining_seconds"])
                for row in entitlements
                if row["status"] == "active" and row["source"] == "free_lifetime"
            )
            paid_seconds = sum(
                int(row["remaining_seconds"])
                for row in entitlements
                if row["status"] == "active" and row["source"] != "free_lifetime"
            )
            cursor = await db.execute(
                "SELECT l.id, l.event_type, l.submission_id, l.reserved_seconds, "
                "l.consumed_seconds, l.released_seconds, l.requested_seconds, l.reason, "
                "l.created_at FROM usage_ledger l WHERE l.user_id=? ORDER BY l.id DESC LIMIT 15",
                (user_id,),
            )
            usage = [dict(row) for row in await cursor.fetchall()]
            cursor = await db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done, "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
                "FROM audio_submissions WHERE user_id=?",
                (user_id,),
            )
            submissions = dict(await cursor.fetchone())
            return {
                "user_id": user_id,
                "telegram_id": int(user["telegram_id"]),
                "username": user["username"],
                "is_banned": bool(user["is_banned"]),
                "is_unlimited": bool(user["is_unlimited"]),
                "unlimited_reason": user["unlimited_reason"],
                "unlimited_granted_at": user["unlimited_granted_at"],
                "first_seen": user["first_seen"],
                "available_seconds": free_seconds + paid_seconds,
                "free_seconds": free_seconds,
                "paid_seconds": paid_seconds,
                "entitlements": entitlements,
                "usage": usage,
                "submissions": {key: int(value or 0) for key, value in submissions.items()},
            }

    async def admin_audit(self, *, limit: int = 30) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT id, admin_telegram_id, action, target_type, target_id, details_json, created_at "
                "FROM admin_audit_log ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            )
            rows = []
            for row in await cursor.fetchall():
                item = dict(row)
                try:
                    item["details"] = json.loads(item.pop("details_json"))
                except (TypeError, ValueError):
                    item["details"] = {}
                rows.append(item)
            return rows

    async def admin_usage_ledger(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Latest integer-second accounting events for the private admin audit view."""
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT l.id, l.user_id, u.telegram_id, l.event_type, l.submission_id, "
                "l.requested_seconds, l.reserved_seconds, l.consumed_seconds, "
                "l.released_seconds, l.available_seconds, l.admin_telegram_id, "
                "l.reason, l.created_at FROM usage_ledger l "
                "JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def _insert_audit(
        self,
        db: aiosqlite.Connection,
        admin_id: int,
        action: str,
        target_type: str,
        target_id: str | None,
        details: dict[str, Any],
    ) -> None:
        safe_details = json.dumps(details, ensure_ascii=False, separators=(",", ":"))[:4000]
        await db.execute(
            "INSERT INTO admin_audit_log(admin_telegram_id, action, target_type, target_id, "
            "details_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_id, action[:80], target_type[:80], target_id, safe_details, utc_now()),
        )

    async def audit(self, admin_id: int, action: str, target_type: str, target_id: str | None, details: dict[str, Any]) -> None:
        async with self._transaction(immediate=True) as db:
            await self._insert_audit(db, admin_id, action, target_type, target_id, details)

    async def audit_entries(self, *, limit: int = 30) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT id, admin_telegram_id, action, target_type, target_id, details_json, created_at "
                "FROM admin_audit_log ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            )
            result = []
            for row in await cursor.fetchall():
                item = dict(row)
                try:
                    item["details"] = json.loads(item.pop("details_json"))
                except (TypeError, ValueError):
                    item["details"] = {}
                result.append(item)
            return result

    async def add_provider_credential(
        self,
        *,
        service: str,
        provider: str,
        label: str,
        ciphertext: str,
        last4: str,
        base_url: str | None,
        model: str | None,
        priority: int,
        admin_id: int,
        enabled: bool = True,
        free_only: bool | None = None,
        paid_allowed: bool | None = None,
        key_type: str = "api_key",
    ) -> int:
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "INSERT INTO provider_credentials "
                "(service, provider, label, secret_ciphertext, secret_last4, base_url, model, "
                "priority, enabled, key_type, free_only, paid_allowed, billing_state, "
                "created_by_admin_id, updated_by_admin_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (service, provider, label, ciphertext, last4, base_url, model, int(priority),
                 int(enabled), key_type,
                 None if free_only is None else int(free_only),
                 None if paid_allowed is None else int(paid_allowed), "unknown",
                 admin_id, admin_id, now, now),
            )
            credential_id = int(cursor.lastrowid)
            await self._insert_audit(
                db, admin_id, "provider_credential_added", "provider_credential", str(credential_id),
                {"service": service, "provider": provider, "label": label, "masked": f"••••{last4}"},
            )
            return credential_id

    async def replace_provider_credential_secret(
        self, credential_id: int, *, ciphertext: str, last4: str, admin_id: int
    ) -> bool:
        """Rotate one credential's secret; audit keeps only the masked form."""
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT label, secret_last4 FROM provider_credentials WHERE id=?",
                (credential_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return False
            await db.execute(
                "UPDATE provider_credentials SET secret_ciphertext=?, secret_last4=?, "
                "quarantined_at=NULL, quarantine_reason=NULL, cooldown_until=NULL, failure_streak=0, last_error=NULL, "
                "updated_by_admin_id=?, updated_at=? WHERE id=?",
                (ciphertext, last4, admin_id, now, credential_id),
            )
            await self._insert_audit(
                db, admin_id, "provider_credential_secret_replaced", "provider_credential",
                str(credential_id),
                {"label": row["label"], "masked": f"••••{last4}"},
            )
            return True

    async def update_provider_credential_metadata(
        self,
        credential_id: int,
        *,
        label: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        admin_id: int,
    ) -> bool:
        """Edit label/base URL/model of one credential (validated like add)."""
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT service, provider, label, base_url, model FROM provider_credentials WHERE id=?",
                (credential_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return False
            new_label = row["label"] if label is None else label
            new_base = row["base_url"] if base_url is None else (base_url or None)
            new_model = row["model"] if model is None else (model or None)
            if not str(new_label).strip():
                raise ValueError("label must not be empty")
            await db.execute(
                "UPDATE provider_credentials SET label=?, base_url=?, model=?, "
                "updated_by_admin_id=?, updated_at=? WHERE id=?",
                (new_label, new_base, new_model, admin_id, now, credential_id),
            )
            await self._insert_audit(
                db, admin_id, "provider_credential_metadata", "provider_credential",
                str(credential_id),
                {
                    "service": row["service"], "provider": row["provider"],
                    "label": new_label, "base_url": new_base, "model": new_model,
                },
            )
            return True

    async def set_provider_credential_billing_flags(
        self,
        credential_id: int,
        *,
        free_only: bool | None = None,
        paid_allowed: bool | None = None,
        admin_id: int,
    ) -> bool:
        """Mark a key free-only / paid-allowed (NULL = unmarked/unknown)."""
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT service, provider, label, free_only, paid_allowed "
                "FROM provider_credentials WHERE id=?",
                (credential_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return False
            new_free = row["free_only"] if free_only is None else int(free_only)
            new_paid = row["paid_allowed"] if paid_allowed is None else int(paid_allowed)
            await db.execute(
                "UPDATE provider_credentials SET free_only=?, paid_allowed=?, "
                "updated_by_admin_id=?, updated_at=? WHERE id=?",
                (new_free, new_paid, admin_id, now, credential_id),
            )
            await self._insert_audit(
                db, admin_id, "provider_credential_billing_flags", "provider_credential",
                str(credential_id),
                {
                    "service": row["service"], "provider": row["provider"],
                    "label": row["label"], "free_only": new_free, "paid_allowed": new_paid,
                },
            )
            return True

    async def set_provider_credential_billing_attestation(
        self, credential_id: int, *, state: str, admin_id: int
    ) -> bool:
        """Auditable account-level billing attestation for a single key."""
        if state not in {"unknown", "free", "paid"}:
            raise ValueError("billing state must be unknown, free, or paid")
        now = utc_now()
        attested_at = None if state == "unknown" else now
        attested_by = None if state == "unknown" else int(admin_id)
        free_only = 1 if state == "free" else (0 if state == "paid" else None)
        paid_allowed = 0 if state == "free" else (1 if state == "paid" else None)
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT service, provider, label, billing_state FROM provider_credentials WHERE id=?",
                (int(credential_id),),
            )
            row = await cursor.fetchone()
            if not row:
                return False
            await db.execute(
                "UPDATE provider_credentials SET billing_state=?, billing_attested_at=?, "
                "billing_attested_by_admin_id=?, free_only=?, paid_allowed=?, "
                "updated_by_admin_id=?, updated_at=? WHERE id=?",
                (state, attested_at, attested_by, free_only, paid_allowed,
                 int(admin_id), now, int(credential_id)),
            )
            await self._insert_audit(
                db, int(admin_id), "provider_credential_billing_attestation",
                "provider_credential", str(credential_id),
                {
                    "service": str(row["service"]),
                    "provider": str(row["provider"]),
                    "label": str(row["label"]),
                    "previous_state": row["billing_state"] or "unknown",
                    "billing_state": state,
                    "attested_at": attested_at,
                },
            )
            return True

    async def set_provider_credential_primary(self, credential_id: int, admin_id: int) -> bool:
        """Make one credential the first (priority 10) of its own pool."""
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT id, service, provider, label FROM provider_credentials WHERE id=?",
                (credential_id,),
            )
            target = await cursor.fetchone()
            if not target:
                return False
            cursor = await db.execute(
                "SELECT id FROM provider_credentials WHERE service=? AND provider=? "
                "ORDER BY priority, id",
                (target["service"], target["provider"]),
            )
            ordered = [int(r["id"]) for r in await cursor.fetchall()]
            if int(credential_id) in ordered:
                ordered.remove(int(credential_id))
            ordered.insert(0, int(credential_id))
            for position, item_id in enumerate(ordered, start=1):
                await db.execute(
                    "UPDATE provider_credentials SET priority=?, updated_by_admin_id=?, "
                    "updated_at=? WHERE id=?",
                    (position * 10, admin_id, now, item_id),
                )
            await self._insert_audit(
                db, admin_id, "provider_credential_primary", "provider_credential",
                str(credential_id),
                {
                    "service": str(target["service"]), "provider": str(target["provider"]),
                    "label": str(target["label"]),
                },
            )
            return True

    async def provider_credential_records(
        self, service: str, provider: str
    ) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM provider_credentials WHERE service=? AND provider=? "
                "AND enabled=1 ORDER BY priority, id",
                (service, provider),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def provider_credential_record(self, credential_id: int) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM provider_credentials WHERE id=?", (credential_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def provider_credentials_exist(self, service: str, provider: str) -> bool:
        """Whether a provider has any stored key, including disabled keys."""
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT 1 FROM provider_credentials WHERE service=? AND provider=? LIMIT 1",
                (service, provider),
            )
            return await cursor.fetchone() is not None

    async def provider_credential_summaries(self) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT id, service, provider, label, secret_last4, base_url, model, priority, "
                "enabled, quarantined_at, quarantine_reason, cooldown_until, last_status_code, last_success_at, "
                "last_failure_at, last_used_at, last_error, created_at, "
                "key_type, free_only, paid_allowed, billing_state, billing_attested_at, "
                "billing_attested_by_admin_id, expires_at, notes, failure_streak, last_quota_json "
                "FROM provider_credentials ORDER BY service, provider, priority, id"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def record_provider_credential_result(
        self,
        credential_id: int,
        *,
        status_code: int | None,
        result: str,
        retry_after_seconds: float | None = None,
        safe_error: str | None = None,
    ) -> None:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        async with self._transaction(immediate=True) as db:
            if result == "success":
                await db.execute(
                    "UPDATE provider_credentials SET last_status_code=?, last_success_at=?, "
                    "last_used_at=?, last_error=NULL, cooldown_until=NULL, quarantined_at=NULL, quarantine_reason=NULL, "
                    "failure_streak=0, updated_at=? WHERE id=?",
                    (status_code, now, now, now, credential_id),
                )
            elif result == "cooldown":
                delay_value = 30.0 if retry_after_seconds is None else float(retry_after_seconds)
                delay = max(1, min(int(delay_value), 604_800))
                until = (now_dt + timedelta(seconds=delay)).isoformat()
                await db.execute(
                    "UPDATE provider_credentials SET last_status_code=?, last_failure_at=?, "
                    "last_used_at=?, last_error=?, cooldown_until=?, failure_streak=failure_streak+1, "
                    "updated_at=? WHERE id=?",
                    (status_code, now, now, safe_error or f"HTTP {status_code}", until, now, credential_id),
                )
            elif result == "quarantined":
                await db.execute(
                    "UPDATE provider_credentials SET last_status_code=?, last_failure_at=?, "
                    "last_used_at=?, last_error=?, quarantined_at=?, quarantine_reason=?, failure_streak=failure_streak+1, "
                    "updated_at=? WHERE id=?",
                    (status_code, now, now, safe_error or f"HTTP {status_code}", now, f"http_{int(status_code or 0)}", now, credential_id),
                )
            else:
                await db.execute(
                    "UPDATE provider_credentials SET last_status_code=?, last_failure_at=?, "
                    "last_used_at=?, last_error=?, failure_streak=failure_streak+1, updated_at=? WHERE id=?",
                    (status_code, now, now, safe_error, now, credential_id),
                )

    async def set_provider_credential_enabled(
        self, credential_id: int, enabled: bool, admin_id: int
    ) -> bool:
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "UPDATE provider_credentials SET enabled=?, quarantined_at=NULL, quarantine_reason=NULL, cooldown_until=NULL, "
                "updated_by_admin_id=?, updated_at=? WHERE id=?",
                (int(enabled), admin_id, now, credential_id),
            )
            if cursor.rowcount:
                await self._insert_audit(
                    db, admin_id,
                    "provider_credential_enabled" if enabled else "provider_credential_disabled",
                    "provider_credential", str(credential_id), {},
                )
            return cursor.rowcount == 1

    async def delete_provider_credential(self, credential_id: int, admin_id: int) -> bool:
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT service, provider, label, secret_last4 FROM provider_credentials WHERE id=?",
                (credential_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return False
            await db.execute("DELETE FROM provider_credentials WHERE id=?", (credential_id,))
            await self._insert_audit(
                db, admin_id, "provider_credential_deleted", "provider_credential", str(credential_id),
                {"service": row["service"], "provider": row["provider"], "label": row["label"],
                 "masked": f"••••{row['secret_last4']}"},
            )
            return True

    async def add_audit_entry(
        self,
        admin_id: int,
        action: str,
        target_type: str,
        target_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Public, secret-free audit insert for administrative actions."""
        async with self._transaction(immediate=True) as db:
            await self._insert_audit(
                db, admin_id, action, target_type, target_id, details or {}
            )
            cursor = await db.execute("SELECT last_insert_rowid() AS id")
            return int((await cursor.fetchone())["id"])

    async def reorder_provider_credential(
        self, credential_id: int, direction: str, admin_id: int
    ) -> bool:
        """Move one credential up/down inside its own provider pool.

        Priorities are renumbered densely (10, 20, 30, ...) in one transaction so
        selection order stays deterministic even when several keys share the
        default priority. Credentials from other providers are untouched.
        """
        if direction not in {"up", "down"}:
            raise ValueError("Unsupported credential reorder direction")
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT id, service, provider, label, priority FROM provider_credentials "
                "WHERE id=?",
                (credential_id,),
            )
            target = await cursor.fetchone()
            if not target:
                return False
            cursor = await db.execute(
                "SELECT id FROM provider_credentials WHERE service=? AND provider=? "
                "ORDER BY priority, id",
                (target["service"], target["provider"]),
            )
            ordered = [int(row["id"]) for row in await cursor.fetchall()]
            try:
                index = ordered.index(int(credential_id))
            except ValueError:  # pragma: no cover - the row exists by construction
                return False
            swap_with = index - 1 if direction == "up" else index + 1
            if swap_with < 0 or swap_with >= len(ordered):
                return False
            ordered[index], ordered[swap_with] = ordered[swap_with], ordered[index]
            for position, item_id in enumerate(ordered, start=1):
                await db.execute(
                    "UPDATE provider_credentials SET priority=?, updated_by_admin_id=?, updated_at=? "
                    "WHERE id=?",
                    (position * 10, admin_id, now, item_id),
                )
            await self._insert_audit(
                db, admin_id, "provider_credential_priority", "provider_credential",
                str(credential_id),
                {
                    "service": str(target["service"]),
                    "provider": str(target["provider"]),
                    "label": str(target["label"]),
                    "direction": direction,
                    "position": swap_with + 1,
                },
            )
            return True

    async def receipt_cleanup_candidates(self, cutoff: str) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT id, receipt_path FROM payment_requests WHERE status IN ('approved','rejected','cancelled') "
                "AND receipt_path IS NOT NULL AND reviewed_at IS NOT NULL "
                "AND datetime(reviewed_at)<=datetime(?) ORDER BY id",
                (cutoff,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def mark_receipt_deleted(self, payment_id: int, receipt_path: str) -> bool:
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "UPDATE payment_requests SET receipt_path=NULL, receipt_message_id=NULL, updated_at=? "
                "WHERE id=? AND receipt_path=?",
                (utc_now(), payment_id, receipt_path),
            )
            return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # AI provider platform: models, routes, usage, quota, events
    # ------------------------------------------------------------------

    async def ai_models_list(
        self, provider: str, *, include_unavailable: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM ai_models WHERE provider=?"
        params: tuple = (provider,)
        if not include_unavailable:
            query += " AND available=1"
        query += " ORDER BY deprecated, id"
        async with self._lock:
            cursor = await self._db().execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def ai_model_get(self, provider: str, model: str) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM ai_models WHERE provider=? AND model=?", (provider, model)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def ai_model_latest_sync(self, provider: str) -> str | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT MAX(source_last_verified_at) FROM ai_models WHERE provider=? AND "
                "source LIKE 'live%'",
                (provider,),
            )
            row = await cursor.fetchone()
            return str(row[0]) if row and row[0] else None

    async def ai_models_upsert_discovery(self, provider: str, rows: list[dict[str, Any]]) -> dict:
        """Persist a live catalog snapshot; vanished models become unavailable."""
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            seen: list[str] = []
            for row in rows:
                seen.append(str(row["model"]))
                await db.execute(
                    "INSERT INTO ai_models (provider, model, display_name, context_window, "
                    "max_output_tokens, capabilities_json, free_status, free_until, "
                    "commercial_use_allowed, region_restriction, deprecated, deprecation_date, "
                    "available, free_endpoint, source, source_last_verified_at, quality_score, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(provider, model) DO UPDATE SET "
                    "display_name=excluded.display_name, "
                    "context_window=CASE WHEN excluded.context_window>0 THEN excluded.context_window "
                    "  ELSE ai_models.context_window END, "
                    "max_output_tokens=CASE WHEN excluded.max_output_tokens>0 "
                    "  THEN excluded.max_output_tokens ELSE ai_models.max_output_tokens END, "
                    "capabilities_json=excluded.capabilities_json, "
                    "free_status=excluded.free_status, free_until=excluded.free_until, "
                    "available=1, deprecated=0, "
                    "free_endpoint=CASE WHEN excluded.free_endpoint=1 THEN 1 "
                    "  ELSE ai_models.free_endpoint END, "
                    "source=excluded.source, source_last_verified_at=excluded.source_last_verified_at, "
                    "updated_at=excluded.updated_at",
                    (
                        provider, row["model"], row.get("display_name") or "",
                        int(row.get("context_window") or 0), int(row.get("max_output_tokens") or 0),
                        row.get("capabilities_json") or "{}", row.get("free_status") or "unknown",
                        row.get("free_until"), int(row.get("commercial_use_allowed", 1)),
                        row.get("region_restriction") or "", int(row.get("deprecated", 0)),
                        row.get("deprecation_date"), int(row.get("available", 1)),
                        int(row.get("free_endpoint", 0)),
                        row.get("source") or "live", row.get("source_last_verified_at") or now,
                        row.get("quality_score"), now, now,
                    ),
                )
            # Anything live-tracked but absent from this snapshot is unavailable now.
            cursor = await db.execute(
                "SELECT model FROM ai_models WHERE provider=? AND available=1 AND source LIKE 'live%'",
                (provider,),
            )
            existing = {str(r["model"]) for r in await cursor.fetchall()}
            deactivated = 0
            for stale_model in sorted(existing - set(seen)):
                await db.execute(
                    "UPDATE ai_models SET available=0, updated_at=? WHERE provider=? AND model=?",
                    (now, provider, stale_model),
                )
                deactivated += 1
            return {"synced": len(rows), "deactivated": deactivated}

    async def ai_models_get_by_id(self, row_id: int) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM ai_models WHERE id=?", (int(row_id),)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def ai_models_set_deprecated(self, provider: str, model: str, *, date: str | None) -> None:
        async with self._transaction(immediate=True) as db:
            await db.execute(
                "UPDATE ai_models SET deprecated=1, deprecation_date=?, updated_at=? "
                "WHERE provider=? AND model=?",
                (date, utc_now(), provider, model),
            )

    async def ai_model_set_requires_paid_billing(
        self, provider: str, model: str, *, required: bool
    ) -> None:
        """Mark a model as needing a billable plan (spec §17).

        Some catalogs (Cloudflare Workers AI in particular) list frontier
        models that cannot run on the free allocation at all. Flagging them
        here makes FREE_ONLY reject the model instead of discovering the
        restriction through a 402/429 after the quota is already spent.
        """
        import json as _json

        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "SELECT capabilities_json FROM ai_models WHERE provider=? AND model=?",
                (provider, model),
            )
            row = await cursor.fetchone()
            current: dict = {}
            if row and row[0]:
                try:
                    loaded = _json.loads(row[0])
                    if isinstance(loaded, dict):
                        current = loaded
                except (ValueError, TypeError):
                    current = {}
            current["requires_paid_billing"] = bool(required)
            await db.execute(
                "UPDATE ai_models SET capabilities_json=?, updated_at=? "
                "WHERE provider=? AND model=?",
                (_json.dumps(current, sort_keys=True), utc_now(), provider, model),
            )

    async def ai_model_set_quality(self, provider: str, model: str, score: float) -> None:
        async with self._transaction(immediate=True) as db:
            await db.execute(
                "UPDATE ai_models SET quality_score=?, last_benchmarked_at=?, updated_at=? "
                "WHERE provider=? AND model=?",
                (float(score), utc_now(), utc_now(), provider, model),
            )

    # -- provider settings ---------------------------------------------------

    async def ai_provider_settings_get(self, provider: str) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM ai_provider_settings WHERE provider=?", (provider,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def ai_provider_settings_all(self) -> dict[str, dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute("SELECT * FROM ai_provider_settings")
            return {str(row["provider"]): dict(row) for row in await cursor.fetchall()}

    async def ai_provider_settings_upsert(
        self, provider: str, *, admin_id: int | None = None, **fields
    ) -> None:
        allowed = {
            "enabled",
            "free_only_blocked",
            "experimental_unlocked",
            "plan_label",
            "notes",
            # migration 008: account entitlement, extra-pass overrides,
            # non-token metering budget and region/quota metadata.
            "account_entitlement_attested_at",
            "account_entitlement_attested_by_admin_id",
            "outline_enabled",
            "repair_enabled",
            "final_compile_enabled",
            "neuron_budget_daily",
            "region",
            "deployment_scope",
            "quota_expires_at",
        }
        columns = [name for name in fields if name in allowed]
        if not columns:
            return
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            await db.execute(
                "INSERT INTO ai_provider_settings (provider, updated_by_admin_id, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(provider) DO NOTHING",
                (provider, admin_id, now),
            )
            assignments = ", ".join(f"{name}=?" for name in columns)
            await db.execute(
                f"UPDATE ai_provider_settings SET {assignments}, updated_by_admin_id=?, updated_at=? "
                "WHERE provider=?",
                tuple(int(fields[name]) if isinstance(fields[name], bool) else fields[name] for name in columns)
                + (admin_id, now, provider),
            )

    # -- routes ----------------------------------------------------------------

    async def ai_routes_list(self, service: str, task_type: str) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM ai_provider_routes WHERE service=? AND task_type=? "
                "ORDER BY route_index, id",
                (service, task_type),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def ai_routes_replace(
        self, service: str, task_type: str, providers: list[dict[str, Any]], admin_id: int | None
    ) -> None:
        now = utc_now()
        async with self._transaction(immediate=True) as db:
            await db.execute(
                "DELETE FROM ai_provider_routes WHERE service=? AND task_type=?",
                (service, task_type),
            )
            for index, entry in enumerate(providers):
                await db.execute(
                    "INSERT INTO ai_provider_routes (service, task_type, route_index, provider, "
                    "enabled, free_only, model, updated_by_admin_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        service, task_type, index, str(entry["provider"]),
                        int(entry.get("enabled", 1)), int(entry.get("free_only", 1)),
                        entry.get("model"), admin_id, now,
                    ),
                )
            if admin_id is not None:
                await self._insert_audit(
                    db, int(admin_id), "ai_route_updated", "ai_provider_routes",
                    f"{service}/{task_type}",
                    {"order": [str(e["provider"]) for e in providers]},
                )

    async def ai_route_enabled(self, service: str, task_type: str, provider: str) -> bool | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT enabled FROM ai_provider_routes WHERE service=? AND task_type=? AND provider=?",
                (service, task_type, provider),
            )
            row = await cursor.fetchone()
            return None if row is None else bool(row[0])

    # -- usage ledger & rollups -------------------------------------------------

    async def ai_usage_insert(self, record: dict[str, Any]) -> int:
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "INSERT INTO ai_usage_records (created_at, finished_at, service, provider, canonical, "
                "credential_id, credential_label, model, request_type, route_position, attempt, "
                "job_id, submission_id, latency_ms, http_status, estimated_input_tokens, "
                "estimated_output_tokens, actual_input_tokens, actual_output_tokens, total_tokens, "
                "finish_reason, retry_after_seconds, quota_headers_json, json_strategy, result, "
                "error_class, free_class, request_id, neurons_estimated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.get("created_at") or utc_now(), record.get("finished_at") or utc_now(),
                    str(record.get("service") or "notes"), str(record.get("provider") or ""),
                    str(record.get("canonical") or record.get("provider") or ""),
                    record.get("credential_id"), record.get("credential_label"),
                    record.get("model"), record.get("request_type"),
                    int(record.get("route_position") or 0), int(record.get("attempt") or 1),
                    record.get("job_id"), record.get("submission_id"),
                    int(record.get("latency_ms") or 0), record.get("http_status"),
                    record.get("estimated_input_tokens"), record.get("estimated_output_tokens"),
                    record.get("actual_input_tokens"), record.get("actual_output_tokens"),
                    record.get("total_tokens"),
                    record.get("finish_reason"), record.get("retry_after_seconds"),
                    record.get("quota_headers_json"), record.get("json_strategy"),
                    str(record.get("result") or "unknown"), record.get("error_class"),
                    record.get("free_class") or "unknown", record.get("request_id"),
                    record.get("neurons_estimated"),
                ),
            )
            row_id = int(cursor.lastrowid)
            # Same-transaction daily rollup keeps aggregates consistent.
            day = (record.get("created_at") or utc_now())[:10]
            provider = str(record.get("provider") or "")
            model = str(record.get("model") or "")
            result = str(record.get("result") or "")
            error_class = str(record.get("error_class") or "")
            await db.execute(
                "INSERT INTO ai_usage_daily (day, provider, model, requests, successes, failures, "
                "rate_limit_hits, server_errors, client_errors, fallbacks, input_tokens, "
                "output_tokens, latency_ms_total, paid_block_events, neurons_estimated) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(day, provider, model) DO UPDATE SET "
                "requests=requests+1, successes=successes+?, failures=failures+?, "
                "rate_limit_hits=rate_limit_hits+?, server_errors=server_errors+?, "
                "client_errors=client_errors+?, fallbacks=fallbacks+?, "
                "input_tokens=input_tokens+?, output_tokens=output_tokens+?, "
                "latency_ms_total=latency_ms_total+?, paid_block_events=paid_block_events+?, "
                "neurons_estimated=neurons_estimated+?",
                (
                    day, provider, model,
                    1 if result == "success" else 0, 1 if result == "failure" else 0,
                    1 if error_class in {"rate_limited", "quota_exhausted"} else 0,
                    1 if error_class in {"server", "transient"} else 0,
                    1 if error_class in {"auth", "bad_request", "model_unavailable", "billing_required"} else 0,
                    1 if int(record.get("route_position") or 0) > 0 else 0,
                    int(record.get("actual_input_tokens") or 0),
                    int(record.get("actual_output_tokens") or 0),
                    int(record.get("latency_ms") or 0),
                    1 if error_class == "billing_required" else 0,
                    int(record.get("neurons_estimated") or 0),
                    1 if result == "success" else 0, 1 if result == "failure" else 0,
                    1 if error_class in {"rate_limited", "quota_exhausted"} else 0,
                    1 if error_class in {"server", "transient"} else 0,
                    1 if error_class in {"auth", "bad_request", "model_unavailable", "billing_required"} else 0,
                    1 if int(record.get("route_position") or 0) > 0 else 0,
                    int(record.get("actual_input_tokens") or 0),
                    int(record.get("actual_output_tokens") or 0),
                    int(record.get("latency_ms") or 0),
                    1 if error_class == "billing_required" else 0,
                    int(record.get("neurons_estimated") or 0),
                ),
            )
            return row_id

    async def ai_usage_today_count(self, provider: str, *, day: str | None = None) -> int:
        day = day or utc_now()[:10]
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT COALESCE(SUM(requests),0) FROM ai_usage_daily WHERE day=? AND provider=?",
                (day, provider),
            )
            row = await cursor.fetchone()
            return int(row[0] or 0)

    async def ai_usage_window_stats(self, canonical: str, since: str) -> dict[str, int]:
        """Requests/tokens for a canonical provider (including legacy aliases)."""
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(COALESCE(NULLIF(actual_input_tokens,0), estimated_input_tokens,0) "
                "+ COALESCE(NULLIF(actual_output_tokens,0), estimated_output_tokens,0)),0) "
                "FROM ai_usage_records WHERE COALESCE(NULLIF(canonical,''),provider)=? "
                "AND created_at>=?",
                (canonical, since),
            )
            row = await cursor.fetchone()
            return {"requests": int(row[0] or 0), "tokens": int(row[1] or 0)}

    async def ai_usage_today_canonical_count(self, canonical: str, *, day: str | None = None) -> int:
        day = day or utc_now()[:10]
        next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT COUNT(*) FROM ai_usage_records "
                "WHERE COALESCE(NULLIF(canonical,''),provider)=? AND created_at>=? AND created_at<?",
                (canonical, f"{day}T00:00:00", f"{next_day}T00:00:00"),
            )
            row = await cursor.fetchone()
            return int(row[0] or 0)

    async def ai_usage_today_token_count(self, canonical: str, *, day: str | None = None) -> int:
        day = day or utc_now()[:10]
        next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT COALESCE(SUM(COALESCE(NULLIF(actual_input_tokens,0), estimated_input_tokens,0) "
                "+ COALESCE(NULLIF(actual_output_tokens,0), estimated_output_tokens,0)),0) "
                "FROM ai_usage_records WHERE COALESCE(NULLIF(canonical,''),provider)=? "
                "AND created_at>=? AND created_at<?",
                (canonical, f"{day}T00:00:00", f"{next_day}T00:00:00"),
            )
            row = await cursor.fetchone()
            return int(row[0] or 0)

    async def ai_usage_today_units(self, canonical: str, *, day: str | None = None) -> int:
        """Estimated provider units (e.g. Neurons) consumed today.

        Only rows that carry an estimate are counted, so a token-metered
        provider reads zero and never trips the unit guard.
        """
        day = day or utc_now()[:10]
        next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT COALESCE(SUM(neurons_estimated),0) FROM ai_usage_records "
                "WHERE COALESCE(NULLIF(canonical,''),provider)=? "
                "AND created_at>=? AND created_at<?",
                (canonical, f"{day}T00:00:00", f"{next_day}T00:00:00"),
            )
            row = await cursor.fetchone()
            return int(row[0] or 0)

    async def ai_usage_summary(self, *, days: int = 7) -> list[dict[str, Any]]:
        cutoff = utc_now()[:10]
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT day, provider, model, requests, successes, failures, rate_limit_hits, "
                "server_errors, fallbacks, input_tokens, output_tokens, latency_ms_total, "
                "paid_block_events FROM ai_usage_daily WHERE day >= date(?, ?) "
                "ORDER BY day DESC, provider",
                (cutoff, f"-{max(1, int(days))} days"),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def ai_usage_recent(
        self,
        *,
        limit: int = 20,
        provider: str | None = None,
        failures_only: bool = False,
        credential_id: int | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM ai_usage_records WHERE 1=1"
        params: list = []
        if provider:
            query += " AND provider=?"
            params.append(provider)
        if credential_id is not None:
            query += " AND credential_id=?"
            params.append(int(credential_id))
        if failures_only:
            query += " AND result='failure'"
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        async with self._lock:
            cursor = await self._db().execute(query, tuple(params))
            return [dict(row) for row in await cursor.fetchall()]

    async def ai_event_metrics(self, *, days: int = 1) -> dict[str, int]:
        """Event counts grouped by name (repair/compile/quota observability)."""
        cutoff = utc_now()[:10]
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT event, COUNT(*) AS n FROM ai_events "
                "WHERE created_at >= datetime(?, ?) GROUP BY event",
                (f"{cutoff}T00:00:00", f"-{max(1, int(days))} days"),
            )
            return {str(row["event"]): int(row["n"] or 0) for row in await cursor.fetchall()}

    async def ai_quota_latest_all(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Latest quota snapshot per provider (usage panel)."""
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT q.* FROM ai_quota_snapshots q "
                "JOIN (SELECT provider, MAX(observed_at) AS mx FROM ai_quota_snapshots "
                "GROUP BY provider) latest ON latest.provider=q.provider "
                "AND latest.mx=q.observed_at ORDER BY q.provider LIMIT ?",
                (max(1, int(limit)),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def ai_model_benchmark_scores(self, *, limit: int = 12) -> list[dict[str, Any]]:
        """Models with a stored Gamas quality score (benchmark panel)."""
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT provider, model, display_name, quality_score, last_benchmarked_at, "
                "free_status, available FROM ai_models "
                "WHERE quality_score IS NOT NULL ORDER BY quality_score DESC LIMIT ?",
                (max(1, int(limit)),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def ai_usage_metrics(self, *, days: int = 1) -> dict[str, Any]:
        cutoff = utc_now()[:10]
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) AS ok, "
                "SUM(CASE WHEN result='failure' THEN 1 ELSE 0 END) AS bad, "
                "SUM(CASE WHEN error_class IN ('rate_limited','quota_exhausted') THEN 1 ELSE 0 END) AS rl, "
                "SUM(CASE WHEN error_class IN ('server','transient') THEN 1 ELSE 0 END) AS srv, "
                "SUM(actual_input_tokens), SUM(actual_output_tokens), SUM(total_tokens), "
                "SUM(latency_ms), SUM(CASE WHEN route_position>0 THEN 1 ELSE 0 END) AS fb "
                "FROM ai_usage_records WHERE created_at >= datetime(?, ?)",
                (f"{cutoff}T00:00:00", f"-{max(1, int(days))} days"),
            )
            row = await cursor.fetchone()
            keys = ["requests", "successes", "failures", "rate_limited", "server_errors",
                    "input_tokens", "output_tokens", "total_tokens", "latency_ms_total", "fallbacks"]
            return {k: int(row[i] or 0) for i, k in enumerate(keys)}

    async def ai_usage_latency_p95(self, *, days: int = 1) -> int:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT latency_ms FROM ai_usage_records WHERE latency_ms IS NOT NULL "
                "AND created_at >= datetime(?, ?) ORDER BY latency_ms",
                (utc_now(), f"-{max(1, int(days)) * 24} hours"),
            )
            values = [int(r[0]) for r in await cursor.fetchall() if r[0] is not None]
        if not values:
            return 0
        index = min(len(values) - 1, int(len(values) * 0.95))
        return values[index]

    # -- quota snapshots ---------------------------------------------------------

    async def ai_quota_upsert(
        self,
        provider: str,
        credential_id: int | None,
        model: str,
        window: str,
        remaining: str | None,
        reset_at: str | None,
        *,
        source: str = "headers",
    ) -> None:
        async with self._transaction(immediate=True) as db:
            await db.execute(
                "INSERT INTO ai_quota_snapshots (provider, credential_id, model, window, remaining, "
                "reset_at, observed_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, credential_id, model, window) DO UPDATE SET "
                "remaining=excluded.remaining, reset_at=excluded.reset_at, "
                "observed_at=excluded.observed_at, source=excluded.source",
                (provider, credential_id, model or "", window, remaining, reset_at, utc_now(), source),
            )

    async def ai_usage_daily_delete_provider_day(
        self, provider: str, *, day: str | None = None
    ) -> int:
        """Operator reset of one provider's daily counters (quota gates)."""
        day = day or utc_now()[:10]
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "DELETE FROM ai_usage_daily WHERE provider=? AND day=?", (provider, day)
            )
        return int(cursor.rowcount or 0)

    async def ai_usage_daily_delete_model(
        self, provider: str, model: str, *, day: str | None = None
    ) -> int:
        day = day or utc_now()[:10]
        async with self._transaction(immediate=True) as db:
            cursor = await db.execute(
                "DELETE FROM ai_usage_daily WHERE provider=? AND model=? AND day=?",
                (provider, model, day),
            )
        return int(cursor.rowcount or 0)

    async def ai_quota_latest(self, provider: str) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM ai_quota_snapshots WHERE provider=? ORDER BY observed_at DESC LIMIT 20",
                (provider,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def ai_quota_live_remaining(self, provider: str) -> int | None:
        """Remaining free-daily requests from a *today's* entitlement probe.

        Only ``source='key_endpoint'`` snapshots (e.g. OpenRouter
        ``GET /api/v1/key``) are authoritative enough to gate routing; header
        snapshots carry opaque per-provider semantics and are display-only.
        Returns ``None`` when no fresh probe exists.
        """
        today = utc_now()[:10]
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT remaining, observed_at FROM ai_quota_snapshots "
                "WHERE provider=? AND source='key_endpoint' AND window='day' "
                "ORDER BY observed_at DESC LIMIT 1",
                (provider,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        observed = str(row["observed_at"] or "")
        if observed[:10] != today:
            return None
        match = re.search(r"remaining=(\d+)", str(row["remaining"] or ""))
        if not match:
            return None
        return int(match.group(1))

    # -- event log (admin Logs panel) ----------------------------------------------

    async def ai_event_insert(self, event: dict[str, Any], *, prune_to: int = 2000) -> None:
        async with self._transaction(immediate=True) as db:
            await db.execute(
                "INSERT INTO ai_events (created_at, level, event, service, provider, canonical, "
                "model, request_type, route_position, http_status, latency_ms, error_class, "
                "detail, job_id, check_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.get("created_at") or utc_now(), str(event.get("level") or "info"),
                    str(event.get("event") or ""), str(event.get("service") or "notes"),
                    event.get("provider"), event.get("canonical"), event.get("model"),
                    event.get("request_type"), event.get("route_position"),
                    event.get("http_status"), event.get("latency_ms"),
                    event.get("error_class"), (event.get("detail") or "")[:400],
                    event.get("job_id"), event.get("check_type"),
                ),
            )
            if prune_to:
                await db.execute(
                    "DELETE FROM ai_events WHERE id NOT IN "
                    "(SELECT id FROM ai_events ORDER BY id DESC LIMIT ?)",
                    (int(prune_to),),
                )

    async def ai_events_list(
        self,
        *,
        limit: int = 30,
        provider: str | None = None,
        event: str | None = None,
        model: str | None = None,
        request_type: str | None = None,
        error_class: str | None = None,
        job_id: str | None = None,
        since: str | None = None,
        http_status: int | None = None,
    ) -> list[dict[str, Any]]:
        """Structured-log query for the admin Logs panel (spec §35).

        Every filter is metadata-only: the table never stores prompts,
        transcripts, model output or secrets, so no filter can surface them.
        """
        query = "SELECT * FROM ai_events WHERE 1=1"
        params: list = []
        if provider:
            query += " AND provider=?"
            params.append(provider)
        if event:
            query += " AND event=?"
            params.append(event)
        if model:
            query += " AND model=?"
            params.append(model)
        if request_type:
            query += " AND request_type=?"
            params.append(request_type)
        if error_class:
            query += " AND error_class=?"
            params.append(error_class)
        if job_id:
            query += " AND job_id=?"
            params.append(job_id)
        if since:
            query += " AND created_at>=?"
            params.append(since)
        if http_status is not None:
            # Status-class filter: 400 -> 4xx, 502 -> that exact status.
            if http_status < 100:
                low, high = http_status * 100, http_status * 100 + 99
                query += " AND http_status>=? AND http_status<=?"
                params.extend([low, high])
            else:
                query += " AND http_status=?"
                params.append(http_status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        async with self._lock:
            cursor = await self._db().execute(query, tuple(params))
            return [dict(row) for row in await cursor.fetchall()]

