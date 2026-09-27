from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            end = script.find("\n", index)
            index = len(script) if end == -1 else end
            continue
        if quote is None and pair == "/*":
            end = script.find("*/", index + 2)
            index = len(script) if end == -1 else end + 2
            continue
        if quote is not None and char == quote:
            quote = None
        elif quote is None and char in "'\"":
            quote = char
        if char == ";" and quote is None:
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

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._apply_migrations()
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT COUNT(*) FROM audio_submissions WHERE status IN ('pending', 'processing')"
            )
            interrupted = int((await cursor.fetchone())[0])
            await self._conn.execute(
                "UPDATE audio_submissions SET status='failed', "
                "error_message='پردازش با راه‌اندازی مجدد متوقف شد' "
                "WHERE status IN ('pending', 'processing')"
            )
            await self._conn.commit()
        logger.info(
            "Database opened path=%s interrupted_jobs_marked_failed=%s",
            self.path,
            interrupted,
        )

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
            logger.info("Database migration applied name=%s", migration.name)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open")
        return self._conn

    async def upsert_user(self, telegram_id: int, username: str | None) -> dict[str, Any]:
        async with self._lock:
            db = self._db()
            await db.execute(
                "INSERT INTO users(telegram_id, username, first_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username",
                (telegram_id, username, utc_now()),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            )
            row = await cursor.fetchone()
            return dict(row)

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_banned(self, telegram_id: int, banned: bool) -> bool:
        async with self._lock:
            db = self._db()
            cursor = await db.execute(
                "UPDATE users SET is_banned=? WHERE telegram_id=?",
                (int(banned), telegram_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def create_submission(
        self,
        user_id: int,
        file_id: str,
        duration: float | None,
        filename: str | None,
        mime_type: str | None,
        source_type: str = "audio",
    ) -> int:
        async with self._lock:
            db = self._db()
            cursor = await db.execute(
                "INSERT INTO audio_submissions "
                "(user_id, file_id, duration, received_at, status, original_filename, "
                "mime_type, source_type) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                (user_id, file_id, duration, utc_now(), filename, mime_type, source_type),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def save_presentation_details(
        self,
        submission_id: int,
        slide_count: int,
        clips: list[dict[str, Any]],
        media_duration: float | None = None,
    ) -> None:
        """Store deck statistics and one row per extracted media clip."""
        async with self._lock:
            db = self._db()
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
            await db.commit()

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
        async with self._lock:
            db = self._db()
            await db.execute(
                "UPDATE audio_submissions SET status=?, error_message=? WHERE id=?",
                (status, error_message, submission_id),
            )
            await db.commit()

    async def save_transcription(
        self,
        submission_id: int,
        engine: str,
        raw_text: str,
        structured_text: str,
    ) -> None:
        async with self._lock:
            db = self._db()
            await db.execute(
                "INSERT INTO transcriptions "
                "(submission_id, stt_engine, raw_transcript, structured_text, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(submission_id) DO UPDATE SET "
                "stt_engine=excluded.stt_engine, raw_transcript=excluded.raw_transcript, "
                "structured_text=excluded.structured_text, created_at=excluded.created_at",
                (submission_id, engine, raw_text, structured_text, utc_now()),
            )
            await db.commit()

    async def user_summaries(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT u.telegram_id, u.username, u.first_seen, u.is_banned, "
                "COUNT(s.id) AS submission_count FROM users u "
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
                "(SELECT COUNT(*) FROM audio_submissions) AS submissions, "
                "(SELECT COUNT(*) FROM audio_submissions WHERE status='done') AS done, "
                "(SELECT COUNT(*) FROM audio_submissions WHERE status='failed') AS failed, "
                "(SELECT COUNT(*) FROM audio_submissions WHERE source_type='video') AS videos, "
                "(SELECT COUNT(*) FROM audio_submissions WHERE source_type='pptx') AS presentations, "
                "(SELECT COALESCE(SUM(clip_count), 0) FROM audio_submissions "
                "WHERE source_type='pptx') AS presentation_clips, "
                "(SELECT COUNT(DISTINCT user_id) FROM audio_submissions "
                "WHERE datetime(substr(received_at, 1, 19)) >= datetime('now', '-30 days')) AS active_30d"
            )
            return dict(await cursor.fetchone())

    async def add_broadcast(
        self, admin_id: int, message: str, recipient_count: int
    ) -> None:
        async with self._lock:
            db = self._db()
            await db.execute(
                "INSERT INTO admin_broadcasts(admin_id, message, sent_at, recipient_count) "
                "VALUES (?, ?, ?, ?)",
                (admin_id, message, utc_now(), recipient_count),
            )
            await db.commit()
