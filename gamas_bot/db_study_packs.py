"""Database persistence slice for Study Packs, Study Assets, and Reward Points."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StudyPackRepository:
    """Single-responsibility persistence slice for Study Packs & Reward Points."""

    def __init__(self, core: Any) -> None:
        self._core = core

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

    async def study_pack_list(
        self, user_id: int, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await self._core.db.execute_fetchall(
            """
            SELECT p.pack_id, p.user_id, p.title, p.source_type, p.status,
                   p.created_at, p.updated_at,
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

    async def study_pack_get(
        self, user_id: int, pack_id: int
    ) -> dict[str, Any] | None:
        rows = await self._core.db.execute_fetchall(
            """
            SELECT pack_id, user_id, title, source_type, status, created_at, updated_at
            FROM study_packs
            WHERE pack_id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (int(pack_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

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
    ) -> int:
        now = _utc_now_iso()
        clean_title = (title or "").strip()[:200] or "فایل"
        cur = await self._core.db.execute(
            """
            INSERT INTO study_assets(
                pack_id, user_id, kind, title, storage_path,
                mime_type, byte_size, sha256, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                   a.storage_path, a.mime_type, a.byte_size, a.sha256, a.created_at
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
                   a.storage_path, a.mime_type, a.byte_size, a.sha256, a.created_at
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
