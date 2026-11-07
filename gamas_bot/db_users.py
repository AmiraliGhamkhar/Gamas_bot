"""User identity, moderation, subscription observability, and free-gift repository (SRP)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .db_core import DatabaseCore, UserRow, _rows, iso


class UsersRepositoryMixin(DatabaseCore):
    """Single-responsibility persistence slice for user accounts and free tries."""

    FREE_USE_COLUMNS: dict[str, str] = {
        "deck": "free_deck_used",
        "quiz": "free_quiz_used",
    }
    FREE_CLAIM_COLUMNS: dict[str, str] = {"deck": "deck_claim", "quiz": "quiz_claim"}

    async def user_exists(self, telegram_id: int) -> bool:
        """Whether this Telegram account has ever used the bot."""
        rows = await _rows(
            self.read_db,
            "SELECT 1 FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return bool(rows)

    async def ensure_user(
        self,
        telegram_id: int,
        first_name: str | None = None,
        *,
        username: str | None = None,
        last_name: str | None = None,
    ) -> UserRow:
        """Upsert the user and refresh the identity fields we were given."""
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int):
            raise TypeError("Telegram user IDs must be exact integers")
        if not -(1 << 63) <= telegram_id < (1 << 63):
            raise ValueError("Telegram user ID is outside SQLite's int64 range")
        now = iso(self._now())
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
        row = await _rows(
            self.read_db,
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
        """Refresh the latest-known identity and stamp ``last_seen_at``."""
        sets = ["last_seen_at = ?"]
        params: list[Any] = [iso(self._now())]
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
        now = iso(self._now())
        async with self._transaction():
            await self.db.execute(
                "UPDATE users SET banned = ? WHERE telegram_id = ?",
                (int(banned), telegram_id),
            )
            if admin_id is not None:
                await self.db.execute(
                    "INSERT INTO audit(admin_id, action, detail, subject_user, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        admin_id,
                        "user_banned" if banned else "user_unbanned",
                        "ban flag changed",
                        telegram_id,
                        now,
                    ),
                )

    async def is_banned(self, telegram_id: int) -> bool:
        rows = await _rows(
            self.read_db,
            "SELECT banned FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return bool(rows and rows[0][0])

    async def set_subscription_state(
        self,
        telegram_id: int,
        status: str,
        *,
        verified_at: str | None = None,
        checked_at: str | None = None,
    ) -> None:
        """Record the last observed membership status (observability only)."""
        await self._write(
            "UPDATE users SET subscription_status = ?, subscription_verified_at = ?, "
            "subscription_checked_at = COALESCE(?, subscription_checked_at) "
            "WHERE telegram_id = ?",
            (str(status), verified_at, checked_at, telegram_id),
        )

    async def users_for_membership_sweep(
        self, limit: int, *, active_since: str
    ) -> list[int]:
        """Active, unbanned users ordered by the least-recently-checked first.

        ``subscription_checked_at`` makes the sweep resumable: after a restart
        it continues with the users whose membership has gone longest without
        a fresh check instead of starting over at the first id.
        """
        rows = await _rows(
            self.read_db,
            "SELECT telegram_id FROM users "
            "WHERE banned = 0 AND last_seen_at >= ? "
            "ORDER BY (subscription_checked_at IS NOT NULL), "
            "subscription_checked_at ASC, telegram_id ASC LIMIT ?",
            (active_since, max(1, int(limit))),
        )
        return [int(row[0]) for row in rows]

    async def save_pending_gate_action(
        self,
        user_id: int,
        *,
        chat_id: int,
        message_id: int,
        kind: str,
        callback_data: str = "",
    ) -> None:
        """Remember one blocked request; a newer blocked request replaces it."""
        await self._write(
            "INSERT INTO pending_gate_actions"
            "(user_id, chat_id, message_id, kind, callback_data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, "
            "message_id=excluded.message_id, kind=excluded.kind, "
            "callback_data=excluded.callback_data, created_at=excluded.created_at",
            (user_id, chat_id, message_id, kind, callback_data, iso(self._now())),
        )

    async def take_pending_gate_action(self, user_id: int) -> dict[str, Any] | None:
        """Return and remove the user's pending blocked request, if any."""
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT user_id, chat_id, message_id, kind, callback_data, created_at "
                "FROM pending_gate_actions WHERE user_id = ?",
                (user_id,),
            )
            if not rows:
                return None
            row = rows[0]
            await self.db.execute(
                "DELETE FROM pending_gate_actions WHERE user_id = ?", (user_id,)
            )
        return {
            "user_id": int(row[0]),
            "chat_id": int(row[1]),
            "message_id": int(row[2]),
            "kind": str(row[3]),
            "callback_data": str(row[4] or ""),
            "created_at": str(row[5]),
        }

    async def clear_expired_pending_gate_actions(self, cutoff: str) -> int:
        """Drop requests older than the resume window; return how many."""
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM pending_gate_actions WHERE created_at < ?",
            (cutoff,),
        )
        count = int(rows[0][0]) if rows else 0
        if count:
            await self._write(
                "DELETE FROM pending_gate_actions WHERE created_at < ?", (cutoff,)
            )
        return count

    async def user_ids_after(self, last_id: int, limit: int = 500) -> list[int]:
        rows = await _rows(
            self.read_db,
            "SELECT telegram_id FROM users WHERE banned = 0 AND broadcast_hard_fails < 3 "
            "AND telegram_id > ? ORDER BY telegram_id LIMIT ?",
            (int(last_id), min(500, max(1, int(limit)))),
        )
        return [int(row[0]) for row in rows]

    async def count_broadcast_targets(self, admin_id: int, *, after_id: int = 0) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM users WHERE banned=0 AND broadcast_hard_fails < 3 "
            "AND telegram_id > ? AND telegram_id != ?",
            (int(after_id), int(admin_id)),
        )
        return int(rows[0][0])

    async def broadcast_create_run(self, admin_id: int, text_hash: str) -> int:
        now = iso(self._now())
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO broadcast_runs(admin_id,text_hash,pages,created_at,updated_at) "
                "VALUES(?,?,1,?,?)", (int(admin_id), text_hash, now, now),
            )
            return int(cursor.lastrowid)

    async def broadcast_get_run(self, run_id: int) -> dict[str, Any] | None:
        rows = await _rows(self.read_db, "SELECT * FROM broadcast_runs WHERE id=?", (int(run_id),))
        return dict(rows[0]) if rows else None

    async def broadcast_checkpoint(self, run_id: int, last_id: int, sent: int, failed: int,
                                   outcomes: list[tuple[int, str]], status: str = "running") -> None:
        if status not in ("running", "done", "stopped"):
            raise ValueError("invalid broadcast status")
        async with self._transaction():
            for user_id, outcome in outcomes:
                if outcome == "success":
                    await self.db.execute("UPDATE users SET broadcast_hard_fails=0 WHERE telegram_id=?", (int(user_id),))
                elif outcome == "hard":
                    await self.db.execute("UPDATE users SET broadcast_hard_fails=broadcast_hard_fails+1 WHERE telegram_id=?", (int(user_id),))
            await self.db.execute(
                "UPDATE broadcast_runs SET last_telegram_id=?, sent=?, failed=?, status=?, updated_at=? WHERE id=?",
                (int(last_id), int(sent), int(failed), status, iso(self._now()), int(run_id)),
            )

    async def broadcast_recover_runs(self) -> list[dict[str, Any]]:
        async with self._transaction():
            rows = await _rows(self.db, "SELECT * FROM broadcast_runs WHERE status='running' ORDER BY id")
            if rows:
                await self.db.execute("UPDATE broadcast_runs SET status='stopped', updated_at=? WHERE status='running'", (iso(self._now()),))
            return [dict(row) for row in rows]

    async def broadcast_resume_run(self, run_id: int, admin_id: int, text_hash: str) -> bool:
        async with self._transaction():
            cursor = await self.db.execute(
                "UPDATE broadcast_runs SET status='running', updated_at=? "
                "WHERE id=? AND admin_id=? AND text_hash=? AND status='stopped'",
                (iso(self._now()), int(run_id), int(admin_id), text_hash),
            )
            return bool(cursor.rowcount)

    async def user_ids(self) -> list[int]:
        rows = await _rows(
            self.read_db, "SELECT telegram_id FROM users ORDER BY telegram_id"
        )
        return [int(row[0]) for row in rows]

    async def list_users(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        now = iso(self._now())
        # Repair only stale/missing rows on this page before the indexed join.
        stale = await _rows(
            self.read_db,
            "SELECT page.telegram_id FROM (SELECT telegram_id FROM users "
            "ORDER BY joined_at DESC LIMIT ? OFFSET ?) page "
            "LEFT JOIN user_balances b ON b.user_id=page.telegram_id "
            "LEFT JOIN balance_dirty d ON d.user_id=page.telegram_id "
            "WHERE b.user_id IS NULL OR d.user_id IS NOT NULL "
            "OR julianday(b.updated_at) > julianday(?) "
            "OR (b.next_transition IS NOT NULL AND julianday(b.next_transition) <= julianday(?))",
            (limit, offset, now, now),
        )
        if stale:
            async with self._transaction():
                for row in stale:
                    await self._refresh_balance_locked(int(row[0]))
        rows = await _rows(
            self.read_db,
            "SELECT u.telegram_id, u.joined_at, u.banned, u.is_unlimited, "
            "COALESCE(u.username, '') AS username, "
            "COALESCE(u.first_name, '') AS first_name, "
            "COALESCE(u.last_name, '') AS last_name, "
            "COALESCE(b.available_seconds, 0) AS available_seconds "
            "FROM users u LEFT JOIN user_balances b ON b.user_id=u.telegram_id "
            "ORDER BY u.joined_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
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

    async def blocked_users(
        self, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
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

    def _free_use_column(self, kind: str) -> str:
        column = self.FREE_USE_COLUMNS.get(str(kind))
        if column is None:
            raise ValueError(f"unknown free-use kind {kind!r}")
        return column

    async def free_use_state(self, user_id: int) -> dict[str, bool]:
        rows = await _rows(
            self.read_db,
            "SELECT free_deck_used, free_quiz_used FROM free_uses WHERE user_id = ?",
            (int(user_id),),
        )
        if not rows:
            return {"deck": True, "quiz": True}
        return {"deck": not int(rows[0][0]), "quiz": not int(rows[0][1])}

    async def consume_free_use(
        self,
        user_id: int,
        kind: str,
        *,
        claim_id: str | None = None,
        require_limited: bool = False,
    ) -> bool:
        column = self._free_use_column(kind)
        claim_column = self.FREE_CLAIM_COLUMNS[kind]
        now = iso(self._now())
        async with self._transaction():
            await self.db.execute(
                "INSERT OR IGNORE INTO free_uses(user_id, free_deck_used, "
                "free_quiz_used, updated_at) VALUES (?, 0, 0, ?)",
                (int(user_id), now),
            )
            guard = (
                " AND EXISTS (SELECT 1 FROM users WHERE telegram_id = ? "
                "AND is_unlimited = 0 AND banned = 0)"
                if require_limited
                else ""
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
        column = self._free_use_column(kind)
        claim_column = self.FREE_CLAIM_COLUMNS[kind]
        async with self._transaction():
            cursor = await self.db.execute(
                f"UPDATE free_uses SET {column} = 0, {claim_column} = NULL, updated_at = ? "
                f"WHERE user_id = ? AND {column} = 1 AND {claim_column} IS ?",
                (iso(self._now()), int(user_id), claim_id),
            )
            return cursor.rowcount == 1

    async def _finalize_free_use_locked(
        self, user_id: int, kind: str, claim_id: str | None
    ) -> None:
        if claim_id is None:
            return
        column = self._free_use_column(kind)
        claim_column = self.FREE_CLAIM_COLUMNS[kind]
        await self.db.execute(
            f"UPDATE free_uses SET {claim_column} = NULL, updated_at = ? "
            f"WHERE user_id = ? AND {column} = 1 AND {claim_column} = ?",
            (iso(self._now()), int(user_id), claim_id),
        )

    async def recover_pending_free_uses(self) -> int:
        recovered = 0
        async with self._transaction():
            for kind, column in self.FREE_USE_COLUMNS.items():
                claim_column = self.FREE_CLAIM_COLUMNS[kind]
                cursor = await self.db.execute(
                    f"UPDATE free_uses SET {column} = 0, {claim_column} = NULL, "
                    f"updated_at = ? WHERE {claim_column} IS NOT NULL",
                    (iso(self._now()),),
                )
                recovered += max(0, cursor.rowcount)
        return recovered

    async def reset_free_uses(
        self, user_id: int, *, admin_id: int, detail: str = "free gift reset"
    ) -> None:
        now = iso(self._now())
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
        rows = await _rows(
            self.read_db,
            "SELECT is_unlimited FROM users WHERE telegram_id = ?",
            (user_id,),
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
        now = iso(self._now())
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
                (
                    admin_id,
                    "unlimited_on" if enabled else "unlimited_off",
                    reason or "-",
                    user_id,
                    now,
                ),
            )

    async def unlimited_users(self) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT telegram_id, unlimited_reason, unlimited_granted_by, unlimited_granted_at "
            "FROM users WHERE is_unlimited = 1 ORDER BY unlimited_granted_at DESC, telegram_id",
        )
        return [dict(row) for row in rows]

    async def count_users(self) -> int:
        row = await _rows(self.read_db, "SELECT COUNT(*) FROM users")
        return int(row[0][0]) if row else 0

    async def user_identity(self, user_id: int) -> dict[str, Any] | None:
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
        identity: dict[str, Any] = {
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

    async def identities_for(
        self, user_ids: Iterable[int]
    ) -> dict[int, dict[str, Any]]:
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
