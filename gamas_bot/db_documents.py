"""Owner-scoped extracted study documents repository (SRP / D1)."""

from __future__ import annotations

from typing import Any

from .db_core import DatabaseCore, _rows, iso


class DocumentsRepositoryMixin(DatabaseCore):
    """Single-responsibility persistence slice for extracted study documents."""

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
                    iso(self._now()),
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

    async def document_by_hash(
        self, user_id: int, sha256: str
    ) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT id, user_id, file_msg_ref, title, source_type, page_count, "
            "sha256, text, chunks_json, library_item_id, created_at FROM documents "
            "WHERE user_id = ? AND sha256 = ?",
            (int(user_id), str(sha256)),
        )
        return dict(rows[0]) if rows else None

    async def document_meta(self, user_id: int, document_id: int) -> dict[str, Any] | None:
        """Return bounded screen metadata without materializing document bodies."""
        rows = await _rows(
            self.read_db,
            "SELECT id, user_id, file_msg_ref, title, source_type, page_count, "
            "sha256, library_item_id, created_at, length(text) AS text_chars, "
            "substr(text, 1, 1024) AS text_preview FROM documents "
            "WHERE id = ? AND user_id = ?",
            (int(document_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

    async def document_get(
        self, user_id: int, document_id: int
    ) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT id, user_id, file_msg_ref, title, source_type, page_count, "
            "sha256, text, chunks_json, library_item_id, created_at FROM documents "
            "WHERE id = ? AND user_id = ?",
            (int(document_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

    async def document_by_library_item(
        self, user_id: int, item_id: int
    ) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT id FROM documents WHERE user_id = ? AND library_item_id = ? LIMIT 1",
            (int(user_id), int(item_id)),
        )
        return dict(rows[0]) if rows else None

    async def documents_list(
        self, user_id: int, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT id, user_id, file_msg_ref, title, source_type, page_count, "
            "sha256, library_item_id, created_at FROM documents "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (int(user_id), int(limit), int(offset)),
        )
        return [dict(row) for row in rows]

    async def document_update_reference(
        self, user_id: int, document_id: int, ref: str
    ) -> None:
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
        self,
        user_id: int,
        document_id: int,
        content_text: str,
        *,
        title: str | None = None,
    ) -> int:
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
                chat_id, message_id = (
                    int(part) for part in str(rows[0][1]).split(":", 1)
                )
                source_unavailable = False
            except (ValueError, TypeError):
                chat_id, message_id = 0, 0
                source_unavailable = True
            display_title = title if title else f"📄 {str(rows[0][2])[:120]}"
            cursor = await self.db.execute(
                "INSERT INTO library_items(user_id, kind, chat_id, message_id, title, "
                "unavailable, created_at, content_text) VALUES (?, 'booklet', ?, ?, ?, ?, ?, ?)",
                (
                    int(user_id),
                    chat_id,
                    message_id,
                    str(display_title)[:200],
                    int(source_unavailable),
                    iso(self._now()),
                    content_text,
                ),
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
