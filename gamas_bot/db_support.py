"""Support ticket state machine and message repository (SRP)."""

from __future__ import annotations

import logging
from typing import Any

from .db_core import DatabaseCore, TicketStateError, _rows, iso

logger = logging.getLogger("gamas_bot.db.support")


class SupportRepositoryMixin(DatabaseCore):
    """Single-responsibility persistence slice for user/admin support tickets."""

    SUPPORT_CATEGORIES = ("payment", "technical", "subscription", "other")
    SUPPORT_STATUSES = ("open", "awaiting_admin", "awaiting_user", "closed")
    SUPPORT_SUBJECT_MAX = 400
    SUPPORT_MESSAGE_MAX = 4000

    SUPPORT_TRANSITIONS: dict[str, dict[str | None, str]] = {
        "create": {None: "open"},
        "user_message": {
            "open": "awaiting_admin",
            "awaiting_admin": "awaiting_admin",
            "awaiting_user": "awaiting_admin",
        },
        "admin_message": {
            "open": "awaiting_user",
            "awaiting_admin": "awaiting_user",
            "awaiting_user": "awaiting_user",
        },
        "close": {
            "open": "closed",
            "awaiting_admin": "closed",
            "awaiting_user": "closed",
        },
        "reopen": {"closed": "open"},
    }

    SUPPORT_FILTERS = {
        "active": "status != 'closed'",
        "open": "status IN ('open', 'awaiting_admin')",
        "wait": "status = 'awaiting_admin'",
        "user": "status = 'awaiting_user'",
        "closed": "status = 'closed'",
        "all": "1 = 1",
    }

    @classmethod
    def _transition(cls, event: str, current: str | None) -> str:
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
        if category not in self.SUPPORT_CATEGORIES:
            raise ValueError(f"invalid category {category}")
        subject = " ".join(subject.split())
        if not 2 <= len(subject) <= self.SUPPORT_SUBJECT_MAX:
            raise ValueError("subject length out of range")
        if initial_message is not None:
            initial_message = initial_message.strip()
            if not 1 <= len(initial_message) <= self.SUPPORT_MESSAGE_MAX:
                raise ValueError("message length out of range")
        now = iso(self._now())
        status = self._transition("create", None)
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO support_ticket_seq DEFAULT VALUES"
            )
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
            "id": ticket_id,
            "public_ticket_id": public_id,
            "user_id": user_id,
            "category": category,
            "subject": subject,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
        }

    async def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT * FROM support_tickets WHERE id = ?",
            (ticket_id,),
        )
        return dict(rows[0]) if rows else None

    async def get_ticket_by_public_id(
        self, public_id: str
    ) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT * FROM support_tickets WHERE public_ticket_id = ?",
            (public_id,),
        )
        return dict(rows[0]) if rows else None

    async def list_user_tickets(
        self, user_id: int, limit: int = 10, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT * FROM support_tickets WHERE user_id = ? "
            "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
        return [dict(row) for row in rows]

    async def list_tickets(
        self,
        *,
        view: str = "active",
        category: str | None = None,
        limit: int = 10,
        offset: int = 0,
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
        rows = await _rows(
            self.read_db,
            f"SELECT * FROM support_tickets WHERE {clause} "
            "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [dict(row) for row in rows]

    async def count_tickets_by_status(self) -> dict[str, int]:
        rows = await _rows(
            self.read_db,
            "SELECT status, COUNT(*) FROM support_tickets GROUP BY status",
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
        if sender_role not in ("user", "admin"):
            raise ValueError("invalid role")
        message_text = message_text.strip()
        if not 1 <= len(message_text) <= self.SUPPORT_MESSAGE_MAX:
            raise ValueError("message length out of range")
        now = iso(self._now())
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT user_id, status FROM support_tickets WHERE id = ?",
                (ticket_id,),
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
                (
                    ticket_id,
                    sender_id,
                    sender_role,
                    message_text,
                    telegram_message_id,
                    now,
                ),
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
            "id": message_id,
            "ticket_id": ticket_id,
            "sender_id": sender_id,
            "sender_role": sender_role,
            "message_text": message_text,
            "created_at": now,
            "status": target,
            "owner_id": owner_id,
        }

    async def ticket_messages(
        self, ticket_id: int, *, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT * FROM (SELECT * FROM support_messages WHERE ticket_id = ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?) ORDER BY id ASC",
            (ticket_id, limit, offset),
        )
        return [dict(row) for row in rows]

    async def count_ticket_messages(self, ticket_id: int) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM support_messages WHERE ticket_id = ?",
            (ticket_id,),
        )
        return int(rows[0][0]) if rows else 0

    async def close_ticket(
        self, ticket_id: int, actor_id: int, *, admin: bool
    ) -> bool:
        now = iso(self._now())
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT user_id, status FROM support_tickets WHERE id = ?",
                (ticket_id,),
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

    async def reopen_ticket(
        self, ticket_id: int, actor_id: int, *, admin: bool
    ) -> bool:
        now = iso(self._now())
        async with self._transaction():
            rows = await _rows(
                self.db,
                "SELECT user_id, status FROM support_tickets WHERE id = ?",
                (ticket_id,),
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
