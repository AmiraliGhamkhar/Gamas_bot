"""Database persistence slice for Study Packs, Study Assets, and Reward Points.

A Study Pack is the durable source object of one accepted upload. It is created
with the processing pipeline (not by an independent API call), owns the assets
that pipeline produced, and keeps the terminal outcome of the job that built it.

Assets never copy bytes that already exist somewhere else in the system: a
``source_kind``/``source_ref`` pair points at the row that owns them (see
migration 039 and :mod:`gamas_bot.webapp_api` for the read side).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("gamas_bot.db.study_packs")

#: A pack is ``processing`` while its job runs and reaches one terminal value.
PACK_STATUSES: frozenset[str] = frozenset({"processing", "ready", "partial", "failed"})

#: Asset kinds produced by the pipelines. ``original_source`` is metadata only:
#: the original upload is referenced through the submission/document row.
ASSET_KIND_TRANSCRIPT = "transcript"
ASSET_KIND_ORIGINAL = "original_source"
ASSET_KIND_PDF = "pdf"
ASSET_KIND_DOCX = "docx"
ASSET_KIND_REVIEW_HTML = "review_html"
ASSET_KIND_QUIZ = "quiz_html"

#: Reference asset sources. ``file`` keeps the pre-039 private-filesystem layout.
SOURCE_KIND_FILE = "file"
SOURCE_KIND_ARTIFACT = "artifact"
SOURCE_KIND_TRANSCRIPTION = "transcription"
SOURCE_KIND_DOCUMENT_TEXT = "document_text"
SOURCE_KIND_LIBRARY_BLOB = "library_blob"


#: Resolves ``quiz_sessions.pack_id`` for sessions that predate the pack link.
#: The same statement is the data half of ``migrations/v3/040_review_pack_links.sql``;
#: it is re-run by :meth:`StudyPackRepository.backfill_from_library` so an operator
#: who imports historical packs can complete the chain in one step.
LINK_SESSION_PACKS_SQL = """
UPDATE quiz_sessions
SET pack_id = (
    SELECT p.pack_id
    FROM study_packs p
    JOIN library_items i
      ON i.item_id = quiz_sessions.item_id
     AND i.user_id = p.user_id
    WHERE p.user_id = quiz_sessions.user_id
      AND p.deleted_at IS NULL
      AND (
            p.source_item_id = i.item_id
         OR (p.submission_id IS NOT NULL AND p.submission_id = i.submission_id)
         OR (p.document_id IS NOT NULL AND p.document_id IN (
                SELECT d.id FROM documents d
                WHERE d.library_item_id = i.item_id AND d.user_id = p.user_id
            ))
      )
    ORDER BY p.pack_id DESC
    LIMIT 1
)
WHERE pack_id IS NULL
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StudyPackRepository:
    """Single-responsibility persistence slice for Study Packs & Reward Points."""

    def __init__(self, core: Any) -> None:
        self._core = core

    # ------------------------------------------------------------------ packs
    async def study_pack_create(
        self,
        user_id: int,
        title: str,
        *,
        source_type: str = "upload",
        status: str = "ready",
    ) -> int:
        now = _utc_now_iso()
        clean_title = (title or "").strip()[:200] or "بستهٔ مطالعاتی"
        cur = await self._core.db.execute(
            """
            INSERT INTO study_packs(user_id, title, source_type, status, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (int(user_id), clean_title, source_type, status, now, now),
        )
        await self._core.db.commit()
        return int(cur.lastrowid)

    async def study_pack_ensure(
        self,
        user_id: int,
        *,
        title: str,
        source_type: str = "upload",
        submission_id: int | None = None,
        document_id: int | None = None,
        source_filename: str = "",
        status: str = "processing",
    ) -> int:
        """Return the pack of one source, creating it at most once.

        Idempotency is enforced twice: the lookup below and the partial unique
        indexes ``idx_study_packs_submission`` / ``idx_study_packs_document``.
        A retried delivery therefore updates the existing pack instead of
        producing a second one.
        """
        if status not in PACK_STATUSES:
            raise ValueError(f"invalid study pack status {status!r}")
        if submission_id is None and document_id is None:
            raise ValueError("a study pack needs a submission or a document")
        now = _utc_now_iso()
        clean_title = (title or "").strip()[:200] or "بستهٔ مطالعاتی"
        clean_name = (source_filename or "").strip()[:255]
        async with self._core._transaction():
            existing = await self._find_pack_locked(
                int(user_id), submission_id=submission_id, document_id=document_id
            )
            if existing is not None:
                pack_id = int(existing["pack_id"])
                # Keep the label current without ever regressing a terminal
                # status back to ``processing`` on a retried delivery.
                await self._core.db.execute(
                    """
                    UPDATE study_packs
                    SET title = ?, updated_at = ?,
                        source_filename = CASE WHEN ? <> '' THEN ? ELSE source_filename END
                    WHERE pack_id = ? AND user_id = ?
                    """,
                    (clean_title, now, clean_name, clean_name, pack_id, int(user_id)),
                )
                return pack_id
            cur = await self._core.db.execute(
                """
                INSERT INTO study_packs(
                    user_id, title, source_type, status, created_at, updated_at,
                    submission_id, document_id, source_filename
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    clean_title,
                    source_type,
                    status,
                    now,
                    now,
                    int(submission_id) if submission_id is not None else None,
                    int(document_id) if document_id is not None else None,
                    clean_name,
                ),
            )
            return int(cur.lastrowid)

    async def _find_pack_locked(
        self,
        user_id: int,
        *,
        submission_id: int | None = None,
        document_id: int | None = None,
    ) -> dict[str, Any] | None:
        if submission_id is not None:
            rows = await self._core.db.execute_fetchall(
                "SELECT pack_id, status FROM study_packs "
                "WHERE user_id = ? AND submission_id = ? AND deleted_at IS NULL",
                (int(user_id), int(submission_id)),
            )
            if rows:
                return dict(rows[0])
        if document_id is not None:
            rows = await self._core.db.execute_fetchall(
                "SELECT pack_id, status FROM study_packs "
                "WHERE user_id = ? AND document_id = ? AND deleted_at IS NULL",
                (int(user_id), int(document_id)),
            )
            if rows:
                return dict(rows[0])
        return None

    async def study_pack_set_source_item(
        self, user_id: int, pack_id: int, item_id: int
    ) -> bool:
        """Attach (or repoint) the durable library row this package came from."""
        cur = await self._core.db.execute(
            "UPDATE study_packs SET source_item_id = ?, updated_at = ? "
            "WHERE pack_id = ? AND user_id = ? AND deleted_at IS NULL",
            (int(item_id), _utc_now_iso(), int(pack_id), int(user_id)),
        )
        await self._core.db.commit()
        return cur.rowcount > 0

    async def study_pack_finalize(
        self,
        user_id: int,
        pack_id: int,
        *,
        status: str,
        error: str = "",
        error_kind: str | None = None,
    ) -> bool:
        """Record the terminal outcome of the job that built this pack."""
        if status not in PACK_STATUSES:
            raise ValueError(f"invalid study pack status {status!r}")
        if status == "processing":
            raise ValueError("a pack cannot be finalized as processing")
        cur = await self._core.db.execute(
            "UPDATE study_packs SET status = ?, error = ?, error_kind = ?, "
            "updated_at = ? WHERE pack_id = ? AND user_id = ? AND deleted_at IS NULL",
            (
                status,
                (error or "")[:500],
                (error_kind or None),
                _utc_now_iso(),
                int(pack_id),
                int(user_id),
            ),
        )
        await self._core.db.commit()
        return cur.rowcount > 0

    async def study_pack_close_after_job(
        self,
        user_id: int,
        submission_id: int,
        *,
        error: str = "",
        error_kind: str | None = None,
    ) -> bool:
        """Fail a pack whose job ended without reaching a terminal outcome.

        Used by the job runner's ``finally``: if the pack is still ``processing``
        when the job is over, the job did not complete, so the pack must not look
        ready. Any asset already attached stays attached.
        """
        cur = await self._core.db.execute(
            "UPDATE study_packs SET status = 'failed', error = ?, error_kind = ?, "
            "updated_at = ? WHERE user_id = ? AND submission_id = ? "
            "AND status = 'processing' AND deleted_at IS NULL",
            (
                (error or "processing did not complete")[:500],
                (error_kind or "processing"),
                _utc_now_iso(),
                int(user_id),
                int(submission_id),
            ),
        )
        await self._core.db.commit()
        return cur.rowcount > 0

    async def study_pack_get(
        self, user_id: int, pack_id: int
    ) -> dict[str, Any] | None:
        rows = await self._core.db.execute_fetchall(
            """
            SELECT pack_id, user_id, title, source_type, status, created_at,
                   updated_at, submission_id, document_id, source_item_id,
                   source_filename, error, error_kind
            FROM study_packs
            WHERE pack_id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (int(pack_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

    async def study_pack_get_for_submission(
        self, user_id: int, submission_id: int
    ) -> dict[str, Any] | None:
        rows = await self._core.db.execute_fetchall(
            "SELECT pack_id FROM study_packs WHERE user_id = ? AND submission_id = ? "
            "AND deleted_at IS NULL",
            (int(user_id), int(submission_id)),
        )
        if not rows:
            return None
        return await self.study_pack_get(int(user_id), int(rows[0]["pack_id"]))

    async def study_pack_get_for_document(
        self, user_id: int, document_id: int
    ) -> dict[str, Any] | None:
        rows = await self._core.db.execute_fetchall(
            "SELECT pack_id FROM study_packs WHERE user_id = ? AND document_id = ? "
            "AND deleted_at IS NULL",
            (int(user_id), int(document_id)),
        )
        if not rows:
            return None
        return await self.study_pack_get(int(user_id), int(rows[0]["pack_id"]))

    async def study_pack_get_for_item(
        self, user_id: int, item_id: int
    ) -> dict[str, Any] | None:
        """Resolve the pack of one library row.

        Delivered rows (the booklet a quiz was generated from) are not the source
        row, so the item's ``submission_id`` is used as the second lookup path.
        """
        rows = await self._core.db.execute_fetchall(
            "SELECT pack_id FROM study_packs WHERE user_id = ? AND source_item_id = ? "
            "AND deleted_at IS NULL ORDER BY pack_id DESC LIMIT 1",
            (int(user_id), int(item_id)),
        )
        if rows:
            return await self.study_pack_get(int(user_id), int(rows[0]["pack_id"]))
        rows = await self._core.db.execute_fetchall(
            """
            SELECT p.pack_id
            FROM study_packs p
            JOIN library_items i ON i.submission_id = p.submission_id
            WHERE p.user_id = ? AND i.user_id = ? AND i.item_id = ?
              AND p.deleted_at IS NULL
            ORDER BY p.pack_id DESC LIMIT 1
            """,
            (int(user_id), int(user_id), int(item_id)),
        )
        if not rows:
            return None
        return await self.study_pack_get(int(user_id), int(rows[0]["pack_id"]))

    async def study_pack_list(
        self, user_id: int, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await self._core.db.execute_fetchall(
            """
            SELECT p.pack_id, p.user_id, p.title, p.source_type, p.status,
                   p.created_at, p.updated_at,
                   p.submission_id, p.document_id, p.source_item_id,
                   p.source_filename, p.error_kind,
                   COUNT(a.asset_id) AS asset_count
            FROM study_packs p
            LEFT JOIN study_assets a ON a.pack_id = p.pack_id
            WHERE p.user_id = ? AND p.deleted_at IS NULL
            GROUP BY p.pack_id
            ORDER BY p.created_at DESC, p.pack_id DESC
            LIMIT ? OFFSET ?
            """,
            (int(user_id), int(limit), int(offset)),
        )
        return [dict(r) for r in rows]

    async def study_pack_delete(self, user_id: int, pack_id: int) -> bool:
        now = _utc_now_iso()
        cur = await self._core.db.execute(
            """
            UPDATE study_packs
            SET deleted_at = ?, updated_at = ?
            WHERE pack_id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (now, now, int(pack_id), int(user_id)),
        )
        await self._core.db.commit()
        return cur.rowcount > 0

    async def study_pack_update_status(
        self, user_id: int, pack_id: int, status: str
    ) -> bool:
        now = _utc_now_iso()
        cur = await self._core.db.execute(
            """
            UPDATE study_packs
            SET status = ?, updated_at = ?
            WHERE pack_id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (status, now, int(pack_id), int(user_id)),
        )
        await self._core.db.commit()
        return cur.rowcount > 0

    # ----------------------------------------------------------------- assets
    async def study_asset_add(
        self,
        pack_id: int,
        user_id: int,
        *,
        kind: str,
        title: str,
        storage_path: str,
        mime_type: str = "application/octet-stream",
        byte_size: int = 0,
        sha256: str = "",
        source_kind: str = SOURCE_KIND_FILE,
        source_ref: str = "",
    ) -> int:
        """Idempotently attach one asset to a pack.

        Filesystem assets deduplicate by content hash; reference assets
        deduplicate by ``(pack_id, kind, source_kind, source_ref)``.
        """
        reference = str(source_ref or "").strip()
        if reference:
            existing = await self._core.db.execute_fetchall(
                "SELECT asset_id FROM study_assets WHERE pack_id = ? AND user_id = ? "
                "AND kind = ? AND source_kind = ? AND source_ref = ? LIMIT 1",
                (int(pack_id), int(user_id), kind, source_kind, reference),
            )
            if existing:
                await self._core.db.execute(
                    "UPDATE study_assets SET title = ?, mime_type = ?, byte_size = ?, "
                    "sha256 = CASE WHEN ? <> '' THEN ? ELSE sha256 END "
                    "WHERE asset_id = ? AND user_id = ?",
                    (
                        (title or "").strip()[:200] or "فایل",
                        mime_type,
                        int(byte_size),
                        sha256,
                        sha256,
                        int(existing[0]["asset_id"]),
                        int(user_id),
                    ),
                )
                await self._core.db.commit()
                return int(existing[0]["asset_id"])
        elif sha256:
            existing = await self._core.db.execute_fetchall(
                """
                SELECT asset_id FROM study_assets
                WHERE pack_id = ? AND user_id = ? AND kind = ? AND sha256 = ?
                LIMIT 1
                """,
                (int(pack_id), int(user_id), kind, sha256),
            )
            if existing:
                return int(existing[0]["asset_id"])
        now = _utc_now_iso()
        clean_title = (title or "").strip()[:200] or "فایل"
        cur = await self._core.db.execute(
            """
            INSERT INTO study_assets(
                pack_id, user_id, kind, title, storage_path,
                mime_type, byte_size, sha256, created_at, source_kind, source_ref
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(pack_id),
                int(user_id),
                kind,
                clean_title,
                storage_path,
                mime_type,
                int(byte_size),
                sha256,
                now,
                source_kind,
                reference,
            ),
        )
        await self._core.db.commit()
        return int(cur.lastrowid)

    async def study_pack_assets(
        self, user_id: int, pack_id: int
    ) -> list[dict[str, Any]]:
        rows = await self._core.db.execute_fetchall(
            """
            SELECT a.asset_id, a.pack_id, a.user_id, a.kind, a.title,
                   a.storage_path, a.mime_type, a.byte_size, a.sha256, a.created_at,
                   a.source_kind, a.source_ref
            FROM study_assets a
            JOIN study_packs p ON p.pack_id = a.pack_id
            WHERE a.pack_id = ? AND a.user_id = ? AND p.deleted_at IS NULL
            ORDER BY a.asset_id ASC
            """,
            (int(pack_id), int(user_id)),
        )
        return [dict(r) for r in rows]

    async def study_asset_get(
        self, user_id: int, asset_id: int
    ) -> dict[str, Any] | None:
        rows = await self._core.db.execute_fetchall(
            """
            SELECT a.asset_id, a.pack_id, a.user_id, a.kind, a.title,
                   a.storage_path, a.mime_type, a.byte_size, a.sha256, a.created_at,
                   a.source_kind, a.source_ref
            FROM study_assets a
            JOIN study_packs p ON p.pack_id = a.pack_id
            WHERE a.asset_id = ? AND a.user_id = ? AND p.deleted_at IS NULL
            """,
            (int(asset_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

    async def reward_points_balance(self, user_id: int) -> int:
        rows = await self._core.db.execute_fetchall(
            "SELECT COALESCE(SUM(delta), 0) AS bal FROM reward_points_ledger WHERE user_id = ?",
            (int(user_id),),
        )
        return int(rows[0]["bal"]) if rows else 0

    async def study_pack_manifest(
        self, user_id: int, pack_id: int
    ) -> dict[str, Any] | None:
        pack = await self.study_pack_get(user_id, pack_id)
        if pack is None:
            return None
        assets = await self.study_pack_assets(user_id, pack_id)
        return {
            "pack_id": pack["pack_id"],
            "title": pack["title"],
            "source_type": pack["source_type"],
            "source_filename": pack.get("source_filename") or "",
            "processing_status": pack["status"],
            "created_at": pack["created_at"],
            "updated_at": pack["updated_at"],
            "assets": [
                {
                    "asset_id": a["asset_id"],
                    "kind": a["kind"],
                    "title": a["title"],
                    "mime_type": a["mime_type"],
                    "byte_size": a["byte_size"],
                    "sha256": a["sha256"],
                    "created_at": a["created_at"],
                }
                for a in assets
            ],
        }

    # --------------------------------------------------------------- backfill
    async def backfill_from_library(self, *, dry_run: bool = True) -> dict[str, int]:
        """Map historical library rows to Study Packs without touching them.

        Two kinds of rows are sources: a ``delivered`` booklet/document, and the
        one ``submission_source`` row each processed submission keeps. The second
        kind is what a pre-Study-Pack database has, so this is also the supported
        way to give historical uploads their pack. The mapping is idempotent: a
        row whose submission already owns a pack (or one whose title already owns
        an unattached pack) is skipped, and afterwards the questions of those
        packs are linked, so running it twice changes nothing the second time.
        """
        rows = await self._core.db.execute_fetchall(
            """
            SELECT li.item_id, li.user_id, li.submission_id, li.kind, li.title,
                   li.created_at, li.artifact_kind, s.kind AS submission_kind
            FROM library_items li
            LEFT JOIN submissions s ON s.submission_id = li.submission_id
            WHERE (li.delivered = 1 AND li.kind IN ('booklet', 'document'))
               OR (li.artifact_kind = 'submission_source'
                   AND li.submission_id IS NOT NULL)
            ORDER BY li.item_id ASC
            """
        )
        scanned = len(rows)
        created = 0
        skipped = 0
        pending: list[dict[str, Any]] = []
        for row in rows:
            user_id = int(row["user_id"])
            submission_id = (
                int(row["submission_id"]) if row["submission_id"] is not None else None
            )
            if submission_id is not None:
                existing = await self.study_pack_get_for_submission(user_id, submission_id)
                if existing is not None:
                    skipped += 1
                    continue
            else:
                existing_pack = await self._core.db.execute_fetchall(
                    "SELECT pack_id FROM study_packs WHERE user_id = ? AND title = ? "
                    "AND submission_id IS NULL AND document_id IS NULL "
                    "AND deleted_at IS NULL LIMIT 1",
                    (user_id, (row["title"] or "بستهٔ مطالعاتی")[:200]),
                )
                if existing_pack:
                    skipped += 1
                    continue
            pending.append(dict(row))
        if dry_run:
            return {
                "scanned": scanned,
                "created": 0,
                "skipped": skipped,
                "pending": len(pending),
                "dry_run": 1,
            }
        for row in pending:
            user_id = int(row["user_id"])
            submission_id = (
                int(row["submission_id"]) if row["submission_id"] is not None else None
            )
            # One submission can own several delivered rows (a booklet and its
            # Word copy). The first row's pack satisfies the rest, so the
            # partial unique index on ``submission_id`` is never violated.
            if submission_id is not None and (
                await self.study_pack_get_for_submission(user_id, submission_id)
                is not None
            ):
                skipped += 1
                continue
            now = _utc_now_iso()
            await self._core.db.execute(
                """
                INSERT INTO study_packs(
                    user_id, title, source_type, status, created_at, updated_at,
                    submission_id, source_item_id
                ) VALUES(?, ?, ?, 'ready', ?, ?, ?, ?)
                """,
                (
                    user_id,
                    (row["title"] or "بستهٔ مطالعاتی")[:200],
                    str(
                        row.get("submission_kind")
                        or row["artifact_kind"]
                        or row["kind"]
                        or "upload"
                    ),
                    str(row["created_at"] or now),
                    now,
                    submission_id,
                    int(row["item_id"]),
                ),
            )
            created += 1
        linked = 0
        if created:
            # A pack without its questions linked would still be invisible to
            # Review, so the two halves always run together.
            cursor = await self._core.db.execute(LINK_SESSION_PACKS_SQL)
            linked = max(0, int(cursor.rowcount))
        if created or linked:
            await self._core.db.commit()
        return {
            "scanned": scanned,
            "created": created,
            "skipped": skipped,
            "linked": linked,
            "pending": 0,
            "dry_run": 0,
        }
