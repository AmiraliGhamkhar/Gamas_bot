"""Submissions, transcriptions, and Telegram-reference library repository (SRP)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import timedelta
from typing import Any

from .db_core import (
    TERMINAL_SUBMISSION_STATUSES,
    DatabaseCore,
    SubmissionRow,
    TooManyActiveJobsError,
    _rows,
    iso,
)


class LibraryRepositoryMixin(DatabaseCore):
    """Single-responsibility persistence slice for submissions and library references."""

    async def create_submission(
        self,
        user_id: int,
        kind: str,
        filename: str | None,
        *,
        active_limit: int | None = None,
    ) -> int:
        async with self._transaction():
            if active_limit is not None:
                rows = await _rows(
                    self.db,
                    "SELECT COUNT(*) FROM submissions WHERE user_id = ? "
                    "AND status IN ('pending', 'processing')",
                    (user_id,),
                )
                if rows and int(rows[0][0]) >= int(active_limit):
                    raise TooManyActiveJobsError(int(active_limit))
            cursor = await self.db.execute(
                "INSERT INTO submissions(user_id, kind, filename, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (user_id, kind, filename, iso(self._now())),
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
        now = iso(self._now())
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
                status,
                status,
                error,
                error_kind,
                duration_seconds,
                status,
                started,
                finished,
                submission_id,
            ),
        )

    async def save_transcription(
        self, submission_id: int, engine: str, text: str, notes_json: str
    ) -> None:
        await self._write_many(
            [
                (
                    "INSERT INTO transcriptions(submission_id, engine, text, notes_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(submission_id) DO UPDATE SET engine = excluded.engine, "
                    "text = excluded.text, notes_json = excluded.notes_json",
                    (submission_id, engine, text, notes_json, iso(self._now())),
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
        if older_than_days <= 0:
            return 0
        cutoff = iso(self._now() - timedelta(days=older_than_days))
        rows = await _rows(
            self.read_db,
            "SELECT t.submission_id FROM transcriptions t "
            "JOIN submissions s ON s.submission_id = t.submission_id "
            "WHERE t.created_at < ? AND s.status IN "
            "('done', 'partial', 'failed', 'rejected') "
            "ORDER BY t.created_at LIMIT ?",
            (cutoff, min(500, max(1, batch))),
        )
        ids = [row[0] for row in rows]
        if not ids:
            return 0
        async with self._transaction():
            placeholders = ",".join("?" for _ in ids)
            cursor = await self.db.execute(
                f"DELETE FROM transcriptions WHERE submission_id IN ({placeholders}) "
                "AND created_at < ? AND EXISTS (SELECT 1 FROM submissions s "
                "WHERE s.submission_id = transcriptions.submission_id "
                "AND s.status IN ('done', 'partial', 'failed', 'rejected'))",
                (*ids, cutoff),
            )
            return max(0, cursor.rowcount)

    async def transcription(self, submission_id: int) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT engine, text, notes_json FROM transcriptions "
            "WHERE submission_id = ?",
            (submission_id,),
        )
        if not rows:
            return None
        engine, text, notes_json = rows[0]
        return {"engine": engine, "text": text, "notes_json": notes_json}

    async def submission(self, submission_id: int) -> SubmissionRow | None:
        rows = await _rows(
            self.read_db,
            "SELECT submission_id, user_id, kind, filename, status, error, "
            "duration_seconds, created_at, finished_at FROM submissions "
            "WHERE submission_id = ?",
            (submission_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return SubmissionRow(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]
        )

    async def user_submissions(
        self, user_id: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT submission_id, kind, filename, status, duration_seconds, created_at, "
            "finished_at FROM submissions WHERE user_id = ? "
            "ORDER BY submission_id DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(row) for row in rows]

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
                    iso(self._now()),
                ),
            )
            return int(cursor.lastrowid or 0)

    async def library_list(
        self, user_id: int, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable FROM library_items WHERE user_id = ? "
            "ORDER BY item_id DESC LIMIT ? OFFSET ?",
            (int(user_id), max(0, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]

    async def library_item(self, user_id: int, item_id: int) -> dict[str, Any] | None:
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
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable, content_text FROM library_items "
            "WHERE user_id = ? AND item_id = ?",
            (int(user_id), int(item_id)),
        )
        return dict(rows[0]) if rows else None

    async def library_set_content(self, item_id: int, content_text: str) -> None:
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
        async with self._transaction():
            cursor = await self.db.execute(
                "UPDATE library_items SET unavailable = 1 WHERE item_id = ?",
                (int(item_id),),
            )
            return cursor.rowcount == 1

    async def library_mark_available(self, item_id: int) -> None:
        await self._write(
            "UPDATE library_items SET unavailable = 0 WHERE item_id = ?",
            (int(item_id),),
        )

    async def library_prune(
        self, user_id: int, keep: int, *, protected_item_ids: Collection[int] = ()
    ) -> int:
        keep = max(0, int(keep))
        protected = tuple(int(item_id) for item_id in protected_item_ids)
        exclusion = (
            " AND item_id NOT IN (" + ",".join("?" for _ in protected) + ")"
            if protected else ""
        )
        async with self._transaction():
            cursor = await self.db.execute(
                "DELETE FROM library_items WHERE user_id = ? AND item_id NOT IN ("
                "SELECT item_id FROM library_items WHERE user_id = ? "
                "ORDER BY item_id DESC LIMIT ?) "
                "AND NOT EXISTS (SELECT 1 FROM quiz_sessions q "
                "WHERE q.user_id = library_items.user_id AND q.item_id = library_items.item_id "
                "AND q.status = 'active')" + exclusion,
                (int(user_id), int(user_id), keep, *protected),
            )
            return max(0, int(cursor.rowcount))

    async def library_list_kind(
        self, user_id: int, kind: str, *, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
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

