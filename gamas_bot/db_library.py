"""Submissions, transcriptions, and Telegram-reference library repository (SRP)."""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from datetime import timedelta
from typing import Any

from .db_core import (
    BINARY_ARTIFACT_KINDS,
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
            "UPDATE submissions SET status = CASE WHEN ? = 'failed' AND EXISTS ("
            "SELECT 1 FROM transcriptions t WHERE t.submission_id = submissions.submission_id "
            "AND length(trim(COALESCE(t.text, ''))) > 0) THEN 'partial' ELSE ? END, error = ?, "
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
        self,
        submission_id: int,
        engine: str,
        text: str,
        notes_json: str,
        *,
        speaker_turns_json: str | None = None,
        diarization_attempted: bool = False,
        overlap_count: int = 0,
    ) -> None:
        """Persist the transcript and, when available, its anonymous turns.

        ``speaker_turns_json`` is a JSON array produced by
        :meth:`gamas_bot.transcript.TranscriptDocument.turns_to_json`. The
        three 026 columns are included only when present in this database, so a
        bot binary still works against a schema whose migration has not been
        applied yet. The column list is read once per call from SQLite; it is
        never interpolated from caller input.
        """
        schema = await _rows(self.read_db, "PRAGMA table_info(transcriptions)")
        existing = {str(row[1]) for row in schema}
        extra_values: dict[str, Any] = {
            "speaker_turns_json": speaker_turns_json,
            "diarization_attempted": int(bool(diarization_attempted)),
            "overlap_count": max(0, int(overlap_count)),
        }
        extra = [name for name in extra_values if name in existing]
        columns = ("submission_id", "engine", "text", "notes_json", *extra, "created_at")
        values = (
            submission_id,
            engine,
            text,
            notes_json,
            *(extra_values[name] for name in extra),
            iso(self._now()),
        )
        insert_sql = (
            f"INSERT INTO transcriptions({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) "
            "ON CONFLICT(submission_id) DO UPDATE SET engine = excluded.engine, "
            "text = excluded.text, notes_json = excluded.notes_json"
            + "".join(f", {name} = excluded.{name}" for name in extra)
        )
        await self._write_many(
            [
                (insert_sql, values),
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
        # Reads stay compatible with a schema before migration 026: the three
        # speaker columns are selected only when they actually exist.
        schema = await _rows(self.read_db, "PRAGMA table_info(transcriptions)")
        existing = {str(row[1]) for row in schema}
        optional = tuple(
            name
            for name in ("speaker_turns_json", "diarization_attempted", "overlap_count")
            if name in existing
        )
        columns = ("engine", "text", "notes_json", *optional)
        rows = await _rows(
            self.read_db,
            f"SELECT {', '.join(columns)} FROM transcriptions "
            "WHERE submission_id = ?",
            (submission_id,),
        )
        if not rows:
            return None
        record = dict(zip(columns, rows[0], strict=True))
        return {
            "engine": record.get("engine"),
            "text": record.get("text"),
            "notes_json": record.get("notes_json"),
            "speaker_turns_json": record.get("speaker_turns_json"),
            "diarization_attempted": bool(record.get("diarization_attempted")),
            "overlap_count": int(record.get("overlap_count") or 0),
        }

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

    async def user_submission_count(self, user_id: int) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM submissions WHERE user_id = ?",
            (int(user_id),),
        )
        return int(rows[0][0]) if rows else 0

    async def user_submissions(
        self, user_id: int, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT submission_id, kind, filename, status, duration_seconds, created_at, "
            "finished_at FROM submissions WHERE user_id = ? "
            "ORDER BY submission_id DESC LIMIT ? OFFSET ?",
            (int(user_id), max(0, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]

    async def _library_columns(self) -> set[str]:
        rows = await _rows(self.read_db, "PRAGMA table_info(library_items)")
        return {str(row[1]) for row in rows}

    async def library_add(
        self,
        user_id: int,
        kind: str,
        chat_id: int,
        message_id: int,
        submission_id: int | None = None,
        *,
        title: str = "",
        delivered: bool = True,
        artifact_kind: str = "",
        content_text: str | None = None,
    ) -> int:
        cols = await self._library_columns()
        async with self._transaction():
            if "delivered" in cols and "artifact_kind" in cols:
                cursor = await self.db.execute(
                    "INSERT INTO library_items(user_id, submission_id, kind, chat_id, "
                    "message_id, title, created_at, content_text, delivered, artifact_kind) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(user_id),
                        submission_id,
                        kind,
                        int(chat_id),
                        int(message_id),
                        str(title),
                        iso(self._now()),
                        str(content_text) if content_text is not None else None,
                        int(bool(delivered)),
                        str(artifact_kind),
                    ),
                )
            else:
                cursor = await self.db.execute(
                    "INSERT INTO library_items(user_id, submission_id, kind, chat_id, "
                    "message_id, title, created_at, content_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(user_id),
                        submission_id,
                        kind,
                        int(chat_id),
                        int(message_id),
                        str(title),
                        iso(self._now()),
                        str(content_text) if content_text is not None else None,
                    ),
                )
            return int(cursor.lastrowid or 0)

    async def library_record_delivered_file(
        self,
        user_id: int,
        kind: str,
        *,
        title: str,
        chat_id: int,
        message_id: int,
        submission_id: int | None = None,
        artifact_kind: str = "",
        content_text: str | None = None,
        filename: str = "",
        content_type: str = "",
        content: bytes | None = None,
    ) -> int:
        """Persist one bot-delivered file into the user's library (and optional blob)."""
        uid = int(user_id)
        now = iso(self._now())
        payload = bytes(content) if content is not None else None
        digest = hashlib.sha256(payload).hexdigest() if payload else ""
        async with self._transaction():
            # The source text is stored once per submission: delivered artifact
            # rows (PDF/Word/review HTML) used to each carry a full copy,
            # inflating every library payload with duplicates. The first
            # non-empty copy wins; readers already fall back across rows via
            # ``library_submission_source_text``.
            effective_text = str(content_text) if content_text is not None else None
            if effective_text and submission_id is not None:
                existing_copy = await _rows(
                    self.db,
                    "SELECT 1 FROM library_items "
                    "WHERE user_id = ? AND submission_id = ? "
                    "AND content_text IS NOT NULL AND trim(content_text) != '' "
                    "LIMIT 1",
                    (uid, int(submission_id)),
                )
                if existing_copy:
                    effective_text = None
            item_id = 0
            if submission_id is not None and artifact_kind:
                existing = await _rows(
                    self.db,
                    "SELECT item_id FROM library_items "
                    "WHERE user_id = ? AND submission_id = ? AND artifact_kind = ? "
                    "AND delivered = 1 ORDER BY item_id DESC LIMIT 1",
                    (uid, int(submission_id), str(artifact_kind)),
                )
                if existing:
                    item_id = int(existing[0][0])
                    await self.db.execute(
                        "UPDATE library_items SET kind = ?, chat_id = ?, message_id = ?, "
                        "title = ?, created_at = ?, unavailable = 0, "
                        "content_text = COALESCE(?, content_text) "
                        "WHERE item_id = ? AND user_id = ?",
                        (
                            str(kind),
                            int(chat_id),
                            int(message_id),
                            str(title)[:200],
                            now,
                            effective_text,
                            item_id,
                            uid,
                        ),
                    )
            if not item_id:
                cursor = await self.db.execute(
                    "INSERT INTO library_items(user_id, submission_id, kind, chat_id, "
                    "message_id, title, created_at, content_text, delivered, artifact_kind) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (
                        uid,
                        int(submission_id) if submission_id is not None else None,
                        str(kind),
                        int(chat_id),
                        int(message_id),
                        str(title)[:200],
                        now,
                        effective_text,
                        str(artifact_kind),
                    ),
                )
                item_id = int(cursor.lastrowid or 0)
            if payload and filename:
                await self.db.execute(
                    "INSERT INTO library_file_blobs("
                    "item_id, user_id, filename, content_type, content, sha256, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(item_id) DO UPDATE SET "
                    "filename = excluded.filename, content_type = excluded.content_type, "
                    "content = excluded.content, sha256 = excluded.sha256, "
                    "updated_at = excluded.updated_at",
                    (
                        item_id,
                        uid,
                        str(filename)[:200],
                        str(content_type or "application/octet-stream")[:120],
                        payload,
                        digest,
                        now,
                        now,
                    ),
                )
            return item_id

    async def library_file_blob(
        self, user_id: int, item_id: int
    ) -> dict[str, Any] | None:
        """Return one owner's persisted library file blob, never another user's."""
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, filename, content_type, content, sha256, "
            "created_at, updated_at FROM library_file_blobs "
            "WHERE item_id = ? AND user_id = ?",
            (int(item_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

    async def library_delete(self, user_id: int, item_id: int) -> bool:
        """Remove a delivered file from the user's library (cascades its stored blob)."""
        async with self._transaction():
            cursor = await self.db.execute(
                "DELETE FROM library_items WHERE user_id = ? AND item_id = ? AND delivered = 1",
                (int(user_id), int(item_id)),
            )
            return cursor.rowcount == 1

    async def library_submission_source_item(
        self, user_id: int, submission_id: int
    ) -> dict[str, Any] | None:
        """Return an internal/existing booklet source record for one submission."""
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable, content_text, delivered, artifact_kind "
            "FROM library_items "
            "WHERE user_id = ? AND submission_id = ? AND kind = 'booklet' "
            "AND unavailable = 0 ORDER BY delivered ASC, item_id DESC LIMIT 1",
            (int(user_id), int(submission_id)),
        )
        return dict(rows[0]) if rows else None

    async def library_submission_source_text(
        self, user_id: int, submission_id: int
    ) -> str | None:
        """Return cached booklet text for a submission regardless of delivered flag."""
        rows = await _rows(
            self.read_db,
            "SELECT content_text FROM library_items "
            "WHERE user_id = ? AND submission_id = ? "
            "AND content_text IS NOT NULL AND trim(content_text) != '' "
            "ORDER BY item_id DESC LIMIT 1",
            (int(user_id), int(submission_id)),
        )
        return str(rows[0][0]) if rows and rows[0][0] else None

    @staticmethod
    def _category_kinds(category: str) -> tuple[str, ...]:
        mapping: dict[str, tuple[str, ...]] = {
            "booklet": ("booklet",),
            "quiz": ("quiz",),
            # Historical flashcard files (feature removed) remain reachable as
            # generic delivered files; the data is never deleted.
            "other": ("office", "transcript", "flashcards"),
        }
        kinds = mapping.get(category)
        if kinds is None:
            raise ValueError(f"unknown library category: {category!r}")
        return kinds

    async def library_category_counts(self, user_id: int) -> dict[str, int]:
        """Return delivered file counts grouped by user-facing library category."""
        rows = await _rows(
            self.read_db,
            "SELECT kind, COUNT(*) FROM library_items "
            "WHERE user_id = ? AND delivered = 1 GROUP BY kind",
            (int(user_id),),
        )
        by_kind = {str(row[0]): int(row[1]) for row in rows}
        booklet = by_kind.get("booklet", 0)
        quiz = by_kind.get("quiz", 0)
        # Historical flashcard deliveries count towards "other" so they stay
        # visible and downloadable without a Flashcards category.
        other = (
            by_kind.get("office", 0)
            + by_kind.get("transcript", 0)
            + by_kind.get("flashcards", 0)
        )
        return {
            "booklet": booklet,
            "quiz": quiz,
            "other": other,
            "total": booklet + quiz + other,
        }

    async def library_list_category(
        self, user_id: int, category: str, *, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        kinds = self._category_kinds(category)
        placeholders = ",".join("?" for _ in kinds)
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable, delivered, artifact_kind "
            f"FROM library_items WHERE user_id = ? AND delivered = 1 AND kind IN ({placeholders}) "
            "ORDER BY item_id DESC LIMIT ? OFFSET ?",
            (int(user_id), *kinds, max(1, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]

    async def library_count_category(self, user_id: int, category: str) -> int:
        kinds = self._category_kinds(category)
        placeholders = ",".join("?" for _ in kinds)
        rows = await _rows(
            self.read_db,
            f"SELECT COUNT(*) FROM library_items "
            f"WHERE user_id = ? AND delivered = 1 AND kind IN ({placeholders})",
            (int(user_id), *kinds),
        )
        return int(rows[0][0]) if rows else 0

    async def library_source_booklets(
        self, user_id: int, *, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Delivered booklets that carry cached text for quiz generation."""
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable, delivered, artifact_kind "
            "FROM library_items WHERE user_id = ? AND delivered = 1 AND kind = 'booklet' "
            "AND content_text IS NOT NULL AND trim(content_text) != '' "
            "ORDER BY item_id DESC LIMIT ? OFFSET ?",
            (int(user_id), max(1, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]

    async def library_source_booklets_count(self, user_id: int) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM library_items "
            "WHERE user_id = ? AND delivered = 1 AND kind = 'booklet' "
            "AND content_text IS NOT NULL AND trim(content_text) != ''",
            (int(user_id),),
        )
        return int(rows[0][0]) if rows else 0

    async def library_list(
        self, user_id: int, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable, delivered, artifact_kind "
            "FROM library_items WHERE user_id = ? AND delivered = 1 "
            "ORDER BY item_id DESC LIMIT ? OFFSET ?",
            (int(user_id), max(0, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]

    async def library_item(self, user_id: int, item_id: int) -> dict[str, Any] | None:
        cols = await self._library_columns()
        if "delivered" in cols and "artifact_kind" in cols:
            sql = (
                "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
                "title, created_at, unavailable, delivered, artifact_kind FROM library_items "
                "WHERE user_id = ? AND item_id = ?"
            )
        else:
            sql = (
                "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
                "title, created_at, unavailable, 1 AS delivered, '' AS artifact_kind "
                "FROM library_items WHERE user_id = ? AND item_id = ?"
            )
        rows = await _rows(self.read_db, sql, (int(user_id), int(item_id)))
        return dict(rows[0]) if rows else None

    async def library_item_detail(
        self, user_id: int, item_id: int
    ) -> dict[str, Any] | None:
        cols = await self._library_columns()
        if "delivered" in cols and "artifact_kind" in cols:
            sql = (
                "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
                "title, created_at, unavailable, content_text, delivered, artifact_kind "
                "FROM library_items WHERE user_id = ? AND item_id = ?"
            )
        else:
            sql = (
                "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
                "title, created_at, unavailable, content_text, 1 AS delivered, '' AS artifact_kind "
                "FROM library_items WHERE user_id = ? AND item_id = ?"
            )
        rows = await _rows(self.read_db, sql, (int(user_id), int(item_id)))
        return dict(rows[0]) if rows else None

    async def library_set_content(self, item_id: int, content_text: str) -> None:
        await self._write(
            "UPDATE library_items SET content_text = ? WHERE item_id = ?",
            (str(content_text), int(item_id)),
        )

    async def library_set_structured(self, item_id: int, structured_json: str) -> None:
        """Store the canonical structured payload of one booklet item.

        Written once by the pipeline next to the text cache so the stored
        structure matches the delivered booklet. The submission's ``transcriptions.notes_json``
        stays the immutable original; this column is the editable copy. The
        028 column is optional at runtime: a bot binary may run against a
        database whose migration has not been applied yet.
        """
        schema = await _rows(self.read_db, "PRAGMA table_info(library_items)")
        if not any(str(row[1]) == "structured_json" for row in schema):
            return
        await self._write(
            "UPDATE library_items SET structured_json = ? WHERE item_id = ?",
            (str(structured_json), int(item_id)),
        )

    async def library_count(self, user_id: int) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM library_items WHERE user_id = ? AND delivered = 1",
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
        """Drop the oldest delivered files beyond ``keep`` for one user.

        Two rules keep Review intact without changing what retention means:

        * an item that owns quiz sessions, or that is a Study Pack's source row,
          is **content** rather than a delivered artifact — deleting it cascades
          into ``quiz_sessions``/``quiz_questions`` and would silently erase the
          user's review material, so it is never pruned;
        * a delivered file that a pack references through a ``library_blob``
          asset is still pruned, and its asset row is dropped in the same
          transaction, so the pack manifest never points at a missing file.
        """
        keep = max(0, int(keep))
        protected = tuple(int(item_id) for item_id in protected_item_ids)
        exclusion = (
            " AND item_id NOT IN (" + ",".join("?" for _ in protected) + ")"
            if protected else ""
        )
        where = (
            "user_id = ? AND delivered = 1 "
            "AND item_id NOT IN ("
            "SELECT item_id FROM library_items WHERE user_id = ? AND delivered = 1 "
            "ORDER BY item_id DESC LIMIT ?) "
            "AND NOT EXISTS (SELECT 1 FROM quiz_sessions q "
            "WHERE q.user_id = library_items.user_id AND q.item_id = library_items.item_id) "
            "AND NOT EXISTS (SELECT 1 FROM study_packs p "
            "WHERE p.user_id = library_items.user_id "
            "AND p.source_item_id = library_items.item_id AND p.deleted_at IS NULL)"
            + exclusion
        )
        params: tuple[Any, ...] = (int(user_id), int(user_id), keep, *protected)
        async with self._transaction():
            doomed = await _rows(
                self.db,
                f"SELECT item_id FROM library_items WHERE {where}",
                params,
            )
            item_ids = [int(row["item_id"]) for row in doomed]
            if not item_ids:
                return 0
            cursor = await self.db.execute(
                f"DELETE FROM library_items WHERE {where}",
                params,
            )
            removed = max(0, int(cursor.rowcount))
            # Chunked so a large backlog cannot exceed SQLite's parameter cap.
            for start in range(0, len(item_ids), 400):
                chunk = item_ids[start:start + 400]
                await self.db.execute(
                    "DELETE FROM study_assets WHERE user_id = ? "
                    "AND source_kind = 'library_blob' AND source_ref IN ("
                    + ",".join("?" for _ in chunk) + ")",
                    (int(user_id), *[str(item_id) for item_id in chunk]),
                )
            return removed

    async def library_list_kind(
        self, user_id: int, kind: str, *, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT item_id, user_id, submission_id, kind, chat_id, message_id, "
            "title, created_at, unavailable, delivered, artifact_kind "
            "FROM library_items WHERE user_id = ? AND delivered = 1 AND kind = ? "
            "ORDER BY item_id DESC LIMIT ? OFFSET ?",
            (int(user_id), str(kind), max(1, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]

    async def library_count_kind(self, user_id: int, kind: str) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM library_items "
            "WHERE user_id = ? AND delivered = 1 AND kind = ?",
            (int(user_id), str(kind)),
        )
        return int(rows[0][0]) if rows else 0

    # -- Gamas 2.0: central submission artifacts ---------------------------
    async def artifact_upsert(
        self,
        submission_id: int,
        user_id: int,
        kind: str,
        *,
        reference: str = "",
        payload: str | None = None,
        available: bool = True,
    ) -> None:
        """Register one independently downloadable artifact of a submission.

        Each ``(submission_id, kind)`` is stored once and updated in place: the
        completion message can therefore show exactly the buttons that exist,
        and no artifact depends on another one having been generated.
        """
        await self._write(
            "INSERT INTO submission_artifacts(submission_id, user_id, kind, reference, "
            "payload, available, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(submission_id, kind) DO UPDATE SET "
            "reference = excluded.reference, payload = excluded.payload, "
            "available = excluded.available",
            (
                int(submission_id),
                int(user_id),
                str(kind),
                str(reference),
                payload,
                int(bool(available)),
                iso(self._now()),
            ),
        )

    async def artifact_list(self, submission_id: int) -> list[dict[str, Any]]:
        """All lightweight artifact registrations for one submission."""
        rows = await _rows(
            self.read_db,
            "SELECT artifact_id, kind, reference, payload, available, created_at "
            "FROM submission_artifacts WHERE submission_id = ? ORDER BY artifact_id",
            (int(submission_id),),
        )
        return [dict(row) for row in rows]

    async def binary_artifact_upsert(
        self,
        submission_id: int,
        user_id: int,
        kind: str,
        *,
        filename: str,
        content_type: str,
        content: bytes,
        revision_id: int | None = None,
    ) -> None:
        """Persist a generated PDF/DOCX/HTML so callbacks never render it again.

        File bytes intentionally live outside the compact JSON-pointer registry:
        SQLite BLOBs keep the deliverable private, durable across workdir cleanup,
        and atomically tied to the submission.  The digest gives operators a
        direct integrity check without retaining a second file copy.
        ``revision_id`` (migration 028, optional) records which booklet revision
        the bytes were rendered from; the pipeline leaves it ``NULL``.
        """
        if kind not in BINARY_ARTIFACT_KINDS:
            raise ValueError(f"unsupported binary artifact kind: {kind!r}")
        payload = bytes(content)
        if not payload:
            raise ValueError("binary artifact content must not be empty")
        digest = hashlib.sha256(payload).hexdigest()
        now = iso(self._now())
        schema = await _rows(
            self.read_db, "PRAGMA table_info(submission_binary_artifacts)"
        )
        if revision_id is not None and any(
            str(row[1]) == "revision_id" for row in schema
        ):
            await self._write(
                "INSERT INTO submission_binary_artifacts("
                "submission_id, user_id, kind, filename, content_type, content, sha256, "
                "created_at, updated_at, revision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(submission_id, kind) DO UPDATE SET "
                "user_id = excluded.user_id, filename = excluded.filename, "
                "content_type = excluded.content_type, content = excluded.content, "
                "sha256 = excluded.sha256, updated_at = excluded.updated_at, "
                "revision_id = excluded.revision_id",
                (
                    int(submission_id), int(user_id), str(kind), str(filename),
                    str(content_type), payload, digest, now, now, int(revision_id),
                ),
            )
            return
        await self._write(
            "INSERT INTO submission_binary_artifacts("
            "submission_id, user_id, kind, filename, content_type, content, sha256, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(submission_id, kind) DO UPDATE SET "
            "user_id = excluded.user_id, filename = excluded.filename, "
            "content_type = excluded.content_type, content = excluded.content, "
            "sha256 = excluded.sha256, updated_at = excluded.updated_at",
            (
                int(submission_id), int(user_id), str(kind), str(filename),
                str(content_type), payload, digest, now, now,
            ),
        )

    async def binary_artifact(
        self, submission_id: int, user_id: int, kind: str
    ) -> dict[str, Any] | None:
        """Return one owner's persisted booklet artifact, never another user's."""
        schema = await _rows(
            self.read_db, "PRAGMA table_info(submission_binary_artifacts)"
        )
        existing = {str(row[1]) for row in schema}
        columns = [
            "kind", "filename", "content_type", "content", "sha256",
            "created_at", "updated_at",
        ]
        if "revision_id" in existing:
            columns.append("revision_id")
        if "delivery_attempts" in existing:
            columns.extend((
                "delivery_attempts", "delivery_successes", "delivery_failures",
                "last_delivery_attempt_at", "last_delivery_error",
            ))
        selected_columns = ", ".join(columns)
        rows = await _rows(
            self.read_db,
            f"SELECT {selected_columns} FROM submission_binary_artifacts "
            "WHERE submission_id = ? AND user_id = ? AND kind = ?",
            (int(submission_id), int(user_id), str(kind)),
        )
        return dict(rows[0]) if rows else None

    async def binary_artifact_meta(
        self, submission_id: int, user_id: int, kind: str
    ) -> dict[str, Any] | None:
        """Describe one persisted artifact without loading its bytes.

        Study Packs reference these rows instead of copying the file, so they
        only need the size, digest and content type for display.
        """
        rows = await _rows(
            self.read_db,
            "SELECT kind, filename, content_type, sha256, length(content) AS byte_size "
            "FROM submission_binary_artifacts "
            "WHERE submission_id = ? AND user_id = ? AND kind = ?",
            (int(submission_id), int(user_id), str(kind)),
        )
        return dict(rows[0]) if rows else None

    async def binary_artifact_delivery_started(
        self, submission_id: int, user_id: int, kind: str
    ) -> bool:
        """Durably record a user-initiated attempt to fetch a stored file."""
        async with self._transaction():
            cursor = await self.db.execute(
                "UPDATE submission_binary_artifacts "
                "SET delivery_attempts = delivery_attempts + 1, "
                "last_delivery_attempt_at = ?, last_delivery_error = NULL "
                "WHERE submission_id = ? AND user_id = ? AND kind = ?",
                (
                    iso(self._now()), int(submission_id), int(user_id), str(kind),
                ),
            )
            return cursor.rowcount == 1

    async def binary_artifact_delivery_finished(
        self,
        submission_id: int,
        user_id: int,
        kind: str,
        *,
        succeeded: bool,
        error_kind: str | None = None,
    ) -> bool:
        """Finish one outstanding attempt; a crash leaves it visibly unresolved."""
        async with self._transaction():
            cursor = await self.db.execute(
                "UPDATE submission_binary_artifacts SET "
                "delivery_successes = delivery_successes + ?, "
                "delivery_failures = delivery_failures + ?, "
                "last_delivery_error = CASE WHEN ? = 1 THEN NULL ELSE ? END "
                "WHERE submission_id = ? AND user_id = ? AND kind = ? "
                "AND delivery_attempts > delivery_successes + delivery_failures",
                (
                    int(bool(succeeded)), int(not succeeded), int(bool(succeeded)),
                    (str(error_kind or "send_failed")[:100] if not succeeded else None),
                    int(submission_id), int(user_id), str(kind),
                ),
            )
            return cursor.rowcount == 1

    async def binary_artifact_kinds(self, submission_id: int, user_id: int | None = None) -> set[str]:
        """Available persisted binary kinds (PDF, DOCX, HTML), optionally owner-scoped."""
        if user_id is None:
            rows = await _rows(
                self.read_db,
                "SELECT kind FROM submission_binary_artifacts WHERE submission_id = ?",
                (int(submission_id),),
            )
        else:
            rows = await _rows(
                self.read_db,
                "SELECT kind FROM submission_binary_artifacts WHERE submission_id = ? "
                "AND user_id = ?",
                (int(submission_id), int(user_id)),
            )
        return {str(row[0]) for row in rows}

    async def artifact_kinds(self, submission_id: int) -> set[str]:
        """The available kinds across lightweight and persisted file registries."""
        rows = await _rows(
            self.read_db,
            "SELECT kind FROM submission_artifacts "
            "WHERE submission_id = ? AND available = 1",
            (int(submission_id),),
        )
        kinds = {str(row[0]) for row in rows}
        kinds.update(await self.binary_artifact_kinds(submission_id))
        return kinds

    async def artifact(
        self, submission_id: int, kind: str
    ) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT artifact_id, kind, reference, payload, available "
            "FROM submission_artifacts WHERE submission_id = ? AND kind = ?",
            (int(submission_id), str(kind)),
        )
        return dict(rows[0]) if rows else None

    # -- Gamas 2.0: anonymous speaker renames ------------------------------
    async def set_speaker_name(
        self,
        submission_id: int,
        speaker_label: str,
        custom_name: str,
        *,
        role: str | None = None,
    ) -> None:
        """Map one stable speaker label to the name (and role) its owner chose."""
        name = str(custom_name).strip()[:64]
        if not name:
            return
        schema = await _rows(self.read_db, "PRAGMA table_info(speaker_names)")
        has_role = any(str(row[1]) == "role" for row in schema)
        now = iso(self._now())
        if role is not None and has_role:
            clean_role = " ".join(str(role).split())[:64]
            await self._write(
                "INSERT INTO speaker_names(submission_id, speaker_label, custom_name, "
                "role, named_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(submission_id, speaker_label) DO UPDATE SET "
                "custom_name = excluded.custom_name, role = excluded.role, "
                "named_at = excluded.named_at",
                (int(submission_id), str(speaker_label), name, clean_role, now),
            )
            return
        await self._write(
            "INSERT INTO speaker_names(submission_id, speaker_label, custom_name, named_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(submission_id, speaker_label) "
            "DO UPDATE SET custom_name = excluded.custom_name, named_at = excluded.named_at",
            (int(submission_id), str(speaker_label), name, now),
        )

    async def speaker_names(self, submission_id: int) -> dict[str, str]:
        rows = await _rows(
            self.read_db,
            "SELECT speaker_label, custom_name FROM speaker_names WHERE submission_id = ?",
            (int(submission_id),),
        )
        return {str(row[0]): str(row[1]) for row in rows}

