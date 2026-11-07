"""User and administrator support ticket workflows (SRP).

Encapsulates ticket creation, paginated history viewing, replies, status
transitions, and operator inbox filtering.
"""

from __future__ import annotations

import logging
from typing import Any

from telethon import Button

from . import texts
from .bot_ui import (
    CATEGORY_FILTER_LABELS,
    SUPPORT_CATEGORY_BUTTONS,
    SUPPORT_STATUS_LABELS,
    VIEW_LABELS,
)
from .db import TicketStateError
from .db_support import TicketLimitError
from .intake import _to_int
from .metrics import METRICS
from .progress import to_persian_digits
from .telegram_text import split_message

logger = logging.getLogger("gamas_bot.bot.support")


class SupportWorkflowMixin:
    """User and admin support ticket screens and message handlers."""

    db: Any
    settings: Any
    client: Any
    _support_states: dict[int, tuple[str, object, float]]
    _support_admin_states: dict[int, tuple[int, float]]

    SUPPORT_PAGE_SIZE = 8
    SUPPORT_HISTORY_PAGE = 6

    @staticmethod
    def _support_when(value: str | None) -> str:
        if not value:
            return "—"
        return to_persian_digits(value[:16].replace("T", " "))

    @staticmethod
    def _support_status_label(status: str) -> str:
        return SUPPORT_STATUS_LABELS.get(status, status)

    @staticmethod
    def _support_category_label(category: str) -> str:
        return SUPPORT_CATEGORY_BUTTONS.get(category, category)

    def _support_history_text(
        self, messages: list[dict], total: int, offset: int, *, viewer: str = "user"
    ) -> str:
        """Render complete messages; Telegram-sized chunks are handled later."""
        lines: list[str] = []
        if total > offset + len(messages):
            lines.append(texts.SUPPORT_OLDER_MESSAGES)
        for message in messages:
            if message["sender_role"] == "admin":
                who = texts.SUPPORT_WHO_STAFF
            elif viewer == "admin":
                who = texts.SUPPORT_WHO_USER_TEMPLATE.format(
                    sender_id=message["sender_id"]
                )
            else:
                who = texts.SUPPORT_WHO_YOU
            lines.append(
                f"{who} · {self._support_when(message['created_at'])}\n"
                f"{message['message_text']}"
            )
        return "\n\n".join(lines) if lines else texts.SUPPORT_EMPTY_THREAD

    async def _show_ticket_screen(self, event: Any, text: str, buttons: Any) -> None:
        """Send every character of a history without exceeding Telegram limits."""
        pages = split_message(text)
        await self._edit_or_reply(  # type: ignore[attr-defined]
            event, pages[0], buttons if len(pages) == 1 else None
        )
        for index, page in enumerate(pages[1:], start=1):
            await event.respond(
                page, buttons=buttons if index == len(pages) - 1 else None
            )

    async def _notify_admins(self, text: str, buttons: Any = None) -> None:
        """Tell every administrator something happened. Failures are logged, never raised."""
        for admin_id in sorted(self.settings.admin_ids):
            try:
                await self.client.send_message(admin_id, text, buttons=buttons)
            except Exception:
                logger.warning("support notification not delivered to an administrator")

    async def _notify_user(
        self, user_id: int, text: str, buttons: Any = None
    ) -> None:
        try:
            await self.client.send_message(user_id, text, buttons=buttons)
        except Exception:
            logger.warning("support notification not delivered to the account")

    async def _show_support_home(self, event: Any) -> None:
        text = texts.SUPPORT_MENU
        buttons = [
            [Button.inline(texts.BTN_NEW_TICKET, b"sup:new")],
            [Button.inline(texts.BTN_MY_TICKETS, b"sup:list")],
            [Button.inline(texts.BTN_BACK_MAIN, b"menu:main")],
        ]
        await self._edit_or_reply(event, text, buttons)  # type: ignore[attr-defined]

    async def _show_support_categories(self, event: Any) -> None:
        buttons = [
            [Button.inline(label, f"sup:cat:{key}".encode())]
            for key, label in SUPPORT_CATEGORY_BUTTONS.items()
        ]
        buttons.append([Button.inline(texts.BTN_BACK, b"support")])
        await self._edit_or_reply(  # type: ignore[attr-defined]
            event, texts.SUPPORT_CHOOSE_CATEGORY, buttons
        )

    async def _show_user_tickets(
        self, event: Any, user_id: int, page: int = 0
    ) -> None:
        page = max(0, page)
        rows = await self.db.list_user_tickets(
            user_id,
            limit=self.SUPPORT_PAGE_SIZE + 1,
            offset=page * self.SUPPORT_PAGE_SIZE,
        )
        has_more = len(rows) > self.SUPPORT_PAGE_SIZE
        rows = rows[: self.SUPPORT_PAGE_SIZE]
        if not rows and page == 0:
            text = texts.SUPPORT_NO_TICKETS
        else:
            text = texts.SUPPORT_LIST_TITLE_TEMPLATE.format(
                page=to_persian_digits(page + 1)
            )
        buttons = [
            [
                Button.inline(
                    f"{row['public_ticket_id']} · {self._support_status_label(row['status'])}",
                    f"sup:v:{row['id']}".encode(),
                )
            ]
            for row in rows
        ]
        nav = []
        if page > 0:
            nav.append(Button.inline(texts.BTN_PREV, f"sup:lp:{page - 1}".encode()))
        if has_more:
            nav.append(Button.inline(texts.BTN_NEXT, f"sup:lp:{page + 1}".encode()))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline(texts.BTN_NEW_TICKET, b"sup:new")])
        buttons.append([Button.inline(texts.BTN_BACK, b"support")])
        await self._edit_or_reply(event, text, buttons)  # type: ignore[attr-defined]

    async def _owned_ticket(self, user_id: int, ticket_id: int) -> dict | None:
        """The ticket only when the signed-in user owns it (never by id alone)."""
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None or int(ticket["user_id"]) != int(user_id):
            return None
        return ticket

    async def _show_user_ticket(
        self, event: Any, user_id: int, ticket_id: int, page: int = 0
    ) -> None:
        ticket = await self._owned_ticket(user_id, ticket_id)
        if ticket is None:
            await self._ack(event, texts.SUPPORT_TICKET_NOT_FOUND, alert=True)  # type: ignore[attr-defined]
            await self._show_user_tickets(event, user_id)
            return
        page = max(0, page)
        offset = page * self.SUPPORT_HISTORY_PAGE
        total = await self.db.count_ticket_messages(ticket_id)
        messages = await self.db.ticket_messages(
            ticket_id, limit=self.SUPPORT_HISTORY_PAGE, offset=offset
        )
        head = texts.SUPPORT_TICKET_HEADER_TEMPLATE.format(
            ticket_id=ticket["public_ticket_id"],
            category=self._support_category_label(ticket["category"]),
            status=self._support_status_label(ticket["status"]),
            subject=ticket["subject"],
            created=self._support_when(ticket["created_at"]),
        ) + "\n"
        history = self._support_history_text(messages, total, offset)
        text = f"{head}\n{history}"
        buttons = []
        if ticket["status"] != "closed":
            buttons.append(
                [Button.inline(texts.BTN_REPLY_STAFF, f"sup:r:{ticket_id}".encode())]
            )
            buttons.append(
                [Button.inline(texts.BTN_CLOSE_TICKET, f"sup:c:{ticket_id}".encode())]
            )
        else:
            buttons.append(
                [Button.inline(texts.BTN_REOPEN_TICKET, f"sup:o:{ticket_id}".encode())]
            )
        if total > offset + self.SUPPORT_HISTORY_PAGE:
            buttons.append(
                [
                    Button.inline(
                        texts.BTN_OLDER_MESSAGES,
                        f"sup:v:{ticket_id}:{page + 1}".encode(),
                    )
                ]
            )
        if page > 0:
            buttons.append(
                [
                    Button.inline(
                        texts.BTN_NEWER_MESSAGES,
                        f"sup:v:{ticket_id}:{page - 1}".encode(),
                    )
                ]
            )
        buttons.append([Button.inline(texts.BTN_BACK_SUPPORT, b"sup:list")])
        await self._show_ticket_screen(event, text, buttons)

    async def _handle_support_callback(
        self, event: Any, user_id: int, data: str
    ) -> None:
        parts = data.split(":")
        if data in ("support", "sup:home"):
            await self._show_support_home(event)
            return
        if data == "sup:new":
            await self._show_support_categories(event)
            return
        if data.startswith("sup:cat:"):
            category = parts[2] if len(parts) == 3 else ""
            if category not in SUPPORT_CATEGORY_BUTTONS:
                await self._ack(event, texts.SUPPORT_BAD_CATEGORY, alert=True)  # type: ignore[attr-defined]
                return
            self._support_states[user_id] = ("new", category, self.deps.monotonic())
            await event.respond(
                f"{SUPPORT_CATEGORY_BUTTONS[category]}\n\n{texts.SUPPORT_SUBJECT_PROMPT}",
                buttons=[[Button.inline(texts.BTN_BACK, b"sup:new")]],
            )
            return
        if data == "sup:list":
            await self._show_user_tickets(event, user_id)
            return
        if data.startswith("sup:lp:"):
            await self._show_user_tickets(event, user_id, _to_int(parts[2]) or 0)
            return
        ticket_id = _to_int(parts[2]) if len(parts) >= 3 else None
        if ticket_id is None:
            await self._ack(event, texts.SUPPORT_INVALID_REQUEST, alert=True)  # type: ignore[attr-defined]
            return
        if parts[1] == "v":
            page = (_to_int(parts[3]) or 0) if len(parts) > 3 else 0
            await self._show_user_ticket(event, user_id, ticket_id, page)
        elif parts[1] == "r":
            ticket = await self._owned_ticket(user_id, ticket_id)
            if ticket is None or ticket["status"] == "closed":
                await self._ack(  # type: ignore[attr-defined]
                    event,
                    texts.SUPPORT_TICKET_NOT_REPLYABLE,
                    alert=True,
                )
                return
            self._support_states[user_id] = ("reply", ticket_id, self.deps.monotonic())
            await event.respond(
                texts.SUPPORT_REPLY_PROMPT_TEMPLATE.format(
                    ticket_id=ticket["public_ticket_id"]
                ),
                buttons=[[Button.inline(texts.BTN_BACK, f"sup:v:{ticket_id}".encode())]],
            )
        elif parts[1] == "c":
            ticket = await self._owned_ticket(user_id, ticket_id)
            if ticket is None:
                await self._ack(event, texts.SUPPORT_TICKET_NOT_FOUND, alert=True)  # type: ignore[attr-defined]
                return
            if ticket["status"] == "closed":
                await self._show_user_ticket(event, user_id, ticket_id)
                return
            await self._edit_or_reply(
                event, texts.SUPPORT_CONFIRM_CLOSE,
                [[Button.inline(texts.BTN_CONFIRM_APPROVE, f"sup:cy:{ticket_id}".encode())],
                 [Button.inline(texts.BTN_BACK, f"sup:v:{ticket_id}".encode())]],
            )
        elif parts[1] == "cy":
            ticket = await self._owned_ticket(user_id, ticket_id)
            if ticket is None:
                await self._ack(event, texts.SUPPORT_TICKET_NOT_FOUND, alert=True)  # type: ignore[attr-defined]
                return
            try:
                closed = await self.db.close_ticket(ticket_id, user_id, admin=False)
            except (ValueError, PermissionError, TicketStateError):
                closed = False
            if closed:
                owner = await self._identity_for_id(user_id)  # type: ignore[attr-defined]
                await self._notify_admins(
                    texts.SUPPORT_CLOSED_BY_USER_TEMPLATE.format(
                        owner=owner, ticket_id=ticket["public_ticket_id"]
                    )
                )
                logger.info("support ticket closed by user ticket=%s", ticket_id)
            await self._show_user_ticket(event, user_id, ticket_id)
        elif parts[1] == "o":
            ticket = await self._owned_ticket(user_id, ticket_id)
            if ticket is None:
                await self._ack(event, texts.SUPPORT_TICKET_NOT_FOUND, alert=True)  # type: ignore[attr-defined]
                return
            try:
                reopened = await self.db.reopen_ticket(ticket_id, user_id, admin=False)
            except (ValueError, PermissionError, TicketStateError):
                reopened = False
            if reopened:
                owner = await self._identity_for_id(user_id)  # type: ignore[attr-defined]
                await self._notify_admins(
                    texts.SUPPORT_REOPENED_BY_USER_TEMPLATE.format(
                        owner=owner, ticket_id=ticket["public_ticket_id"]
                    )
                )
                logger.info("support ticket reopened by user ticket=%s", ticket_id)
            await self._show_user_ticket(event, user_id, ticket_id)
        else:
            await self._ack(event, texts.SUPPORT_INVALID_REQUEST, alert=True)  # type: ignore[attr-defined]

    async def _handle_support_text(
        self, event: Any, user_id: int, state: tuple, text: str
    ) -> None:
        """The next plain message of a user who opened a support prompt."""
        kind, value, _armed_at = state
        if not text:
            await event.reply(texts.SUPPORT_TEXT_ONLY)
            return
        if kind == "new":
            if len(text) > self.db.SUPPORT_MESSAGE_MAX:
                await event.reply(texts.SUPPORT_MESSAGE_TOO_LONG)
                return
            subject = " ".join(text.splitlines()[0].split())[:self.db.SUPPORT_SUBJECT_MAX]
            try:
                ticket = await self.db.create_ticket(
                    user_id,
                    value,
                    subject,
                    initial_message=text,
                    telegram_message_id=getattr(event.message, "id", None),
                    max_open_tickets=self.settings.support_max_open_tickets,
                    cooldown_seconds=self.settings.support_ticket_cooldown_seconds,
                )
            except TicketLimitError as exc:
                if exc.reason == "open":
                    await event.reply(texts.SUPPORT_OPEN_LIMIT.format(count=to_persian_digits(exc.limit)))
                else:
                    await event.reply(texts.SUPPORT_COOLDOWN)
                return
            except ValueError:
                await event.reply(texts.SUPPORT_SUBJECT_LENGTH)
                return
            self._support_states.pop(user_id, None)
            logger.info(
                "support ticket created ticket=%s category=%s", ticket["id"], value
            )
            await event.reply(
                texts.SUPPORT_CREATED.format(ticket_id=ticket["public_ticket_id"]),
                buttons=[
                    [
                        Button.inline(
                            texts.BTN_VIEW_TICKET, f"sup:v:{ticket['id']}".encode()
                        )
                    ]
                ],
            )
            owner = await self._identity_for_id(user_id)  # type: ignore[attr-defined]
            await self._notify_admins(
                texts.SUPPORT_ADMIN_NEW_TICKET.format(
                    ticket_id=ticket["public_ticket_id"],
                    owner=owner,
                    category=self._support_category_label(value),
                    subject=ticket["subject"],
                ),
                buttons=[
                    [
                        Button.inline(
                            texts.BTN_REVIEW_TICKET, f"adm:sv:{ticket['id']}".encode()
                        )
                    ]
                ],
            )
            return
        if len(text) > self.db.SUPPORT_MESSAGE_MAX:
            await event.reply(texts.SUPPORT_MESSAGE_TOO_LONG)
            return
        try:
            await self.db.add_ticket_message(
                value,
                user_id,
                "user",
                text,
                telegram_message_id=getattr(event.message, "id", None),
            )
        except (ValueError, PermissionError, TicketStateError):
            self._support_states.pop(user_id, None)
            await event.reply(texts.SUPPORT_TICKET_NOT_REPLYABLE)
            return
        self._support_states.pop(user_id, None)
        refreshed_ticket = await self.db.get_ticket(value)
        public_id = (
            refreshed_ticket["public_ticket_id"] if refreshed_ticket else str(value)
        )
        await event.reply(
            texts.SUPPORT_MESSAGE_SAVED,
            buttons=[
                [Button.inline(texts.BTN_VIEW_TICKET, f"sup:v:{value}".encode())]
            ],
        )
        owner = await self._identity_for_id(user_id)  # type: ignore[attr-defined]
        await self._notify_admins(
            texts.SUPPORT_NEW_MESSAGE_ADMIN.format(
                ticket_id=public_id, owner=owner
            ),
            buttons=[
                [Button.inline(texts.BTN_REVIEW_TICKET, f"adm:sv:{value}".encode())]
            ],
        )

    async def _handle_admin_support_text(
        self, event: Any, admin_id: int, ticket_id: int, text: str
    ) -> None:
        if not text:
            await event.reply(texts.SUPPORT_REPLY_NEEDS_TEXT)
            return
        if len(text) > self.db.SUPPORT_MESSAGE_MAX:
            await event.reply(texts.SUPPORT_MESSAGE_TOO_LONG)
            return
        try:
            await self.db.add_ticket_message(
                ticket_id,
                admin_id,
                "admin",
                text,
                telegram_message_id=getattr(event.message, "id", None),
            )
        except (ValueError, PermissionError, TicketStateError):
            METRICS.record_support_reply_failure()
            self._support_admin_states.pop(admin_id, None)
            await event.reply(
                texts.SUPPORT_TICKET_NOT_REPLYABLE,
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        self._support_admin_states.pop(admin_id, None)
        ticket = await self.db.get_ticket(ticket_id)
        public_id = ticket["public_ticket_id"] if ticket else str(ticket_id)
        logger.info("support admin reply ticket=%s", ticket_id)
        await event.reply(
            texts.SUPPORT_REPLY_SAVED_ADMIN.format(ticket_id=public_id),
            buttons=[
                [Button.inline(texts.BTN_REVIEW_TICKET, f"adm:sv:{ticket_id}".encode())]
            ],
        )
        if ticket is not None:
            await self._notify_user(
                int(ticket["user_id"]),
                texts.SUPPORT_REPLY_NOTICE_USER.format(ticket_id=public_id),
                buttons=[
                    [Button.inline(texts.BTN_VIEW_REPLY, f"sup:v:{ticket_id}".encode())]
                ],
            )

    async def _show_admin_support(
        self,
        event: Any,
        view: str = "active",
        category: str = "all",
        page: int = 0,
    ) -> None:
        counts = await self.db.count_tickets_by_status()
        page = max(0, page)
        cat = None if category == "all" else category
        rows = await self.db.list_tickets(
            view=view,
            category=cat,
            limit=self.SUPPORT_PAGE_SIZE + 1,
            offset=page * self.SUPPORT_PAGE_SIZE,
        )
        has_more = len(rows) > self.SUPPORT_PAGE_SIZE
        rows = rows[: self.SUPPORT_PAGE_SIZE]
        open_count = (
            counts.get("open", 0)
            + counts.get("awaiting_admin", 0)
            + counts.get("awaiting_user", 0)
        )
        new_open_count = counts.get("open", 0) + counts.get("awaiting_admin", 0)
        text = texts.SUPPORT_ADMIN_HEADER_TEMPLATE.format(
            open_count=to_persian_digits(open_count),
            new_count=to_persian_digits(new_open_count),
            waiting_count=to_persian_digits(counts.get("awaiting_admin", 0)),
            closed_count=to_persian_digits(counts.get("closed", 0)),
            view=VIEW_LABELS.get(view, view),
            category=CATEGORY_FILTER_LABELS.get(category, category),
        )
        buttons = [
            [
                Button.inline(
                    VIEW_LABELS["active"], f"adm:sl:active:{category}:0".encode()
                ),
                Button.inline(
                    VIEW_LABELS["open"], f"adm:sl:open:{category}:0".encode()
                ),
                Button.inline(
                    VIEW_LABELS["wait"], f"adm:sl:wait:{category}:0".encode()
                ),
            ],
            [
                Button.inline(
                    VIEW_LABELS["user"], f"adm:sl:user:{category}:0".encode()
                ),
                Button.inline(
                    VIEW_LABELS["closed"], f"adm:sl:closed:{category}:0".encode()
                ),
            ],
            [Button.inline(texts.BTN_ALL_CATEGORIES, f"adm:sl:{view}:all:0".encode())],
            [
                Button.inline(label, f"adm:sl:{view}:{key}:0".encode())
                for key, label in SUPPORT_CATEGORY_BUTTONS.items()
            ][:2],
            [
                Button.inline(label, f"adm:sl:{view}:{key}:0".encode())
                for key, label in SUPPORT_CATEGORY_BUTTONS.items()
            ][2:],
        ]
        identities = await self.db.identities_for(int(row["user_id"]) for row in rows)
        for row in rows:
            identity = identities.get(int(row["user_id"]), {})
            owner = self._identity_line(  # type: ignore[attr-defined]
                username=identity.get("username"),
                telegram_id=row["user_id"],
                display_name=self._user_display_name(  # type: ignore[attr-defined]
                    identity.get("first_name", ""), identity.get("last_name", "")
                ),
            )
            text += f"\n• {row['public_ticket_id']} · {owner}"
            buttons.append(
                [
                    Button.inline(
                        f"{row['public_ticket_id']} · {self._support_category_label(row['category'])} · "
                        f"{self._support_status_label(row['status'])}",
                        f"adm:sv:{row['id']}".encode(),
                    )
                ]
            )
        nav = []
        if page > 0:
            nav.append(
                Button.inline(
                    texts.BTN_PREV, f"adm:sl:{view}:{category}:{page - 1}".encode()
                )
            )
        if has_more:
            nav.append(
                Button.inline(
                    texts.BTN_NEXT, f"adm:sl:{view}:{category}:{page + 1}".encode()
                )
            )
        if nav:
            buttons.append(nav)
        if not rows:
            text += texts.SUPPORT_EMPTY_LIST
        buttons.append([Button.inline(texts.BTN_BACK_ADMIN, b"adm:home")])
        await self._edit_or_reply(event, text, buttons)  # type: ignore[attr-defined]

    async def _show_admin_ticket(
        self, event: Any, ticket_id: int, page: int = 0
    ) -> None:
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            await self._ack(event, texts.SUPPORT_TICKET_NOT_FOUND, alert=True)  # type: ignore[attr-defined]
            await self._show_admin_support(event)
            return
        page = max(0, page)
        total = await self.db.count_ticket_messages(ticket_id)
        offset = page * self.SUPPORT_HISTORY_PAGE
        messages = await self.db.ticket_messages(
            ticket_id,
            limit=self.SUPPORT_HISTORY_PAGE,
            offset=offset,
        )
        owner = await self._identity_for_id(int(ticket["user_id"]))  # type: ignore[attr-defined]
        head = texts.SUPPORT_ADMIN_TICKET_TEMPLATE.format(
            ticket_id=ticket["public_ticket_id"],
            owner=owner,
            category=self._support_category_label(ticket["category"]),
            status=self._support_status_label(ticket["status"]),
            subject=ticket["subject"],
            created=self._support_when(ticket["created_at"]),
            updated=self._support_when(ticket["updated_at"]),
        ) + "\n"
        text = (
            f"{head}\n"
            f"{self._support_history_text(messages, total, offset, viewer='admin')}"
        )
        buttons = []
        if ticket["status"] != "closed":
            buttons.append(
                [Button.inline(texts.BTN_REPLY_USER, f"adm:sr:{ticket_id}".encode())]
            )
            buttons.append(
                [Button.inline(texts.BTN_CLOSE_TICKET, f"adm:sc:{ticket_id}".encode())]
            )
        else:
            buttons.append(
                [Button.inline(texts.BTN_REOPEN_TICKET, f"adm:so:{ticket_id}".encode())]
            )
        if total > offset + self.SUPPORT_HISTORY_PAGE:
            buttons.append(
                [
                    Button.inline(
                        texts.BTN_OLDER_MESSAGES,
                        f"adm:sv:{ticket_id}:{page + 1}".encode(),
                    )
                ]
            )
        if page > 0:
            buttons.append(
                [
                    Button.inline(
                        texts.BTN_NEWER_MESSAGES,
                        f"adm:sv:{ticket_id}:{page - 1}".encode(),
                    )
                ]
            )
        buttons.append([Button.inline(texts.BTN_BACK_ADMIN_SUPPORT, b"adm:sup")])
        await self._show_ticket_screen(event, text, buttons)

    async def _handle_admin_support_callback(
        self, event: Any, admin_id: int, data: str
    ) -> None:
        parts = data.split(":")
        if data == "adm:sup":
            await self._show_admin_support(event)
            return
        if data.startswith("adm:sl:") and len(parts) == 5:
            view, category = parts[2], parts[3]
            if view not in VIEW_LABELS or (
                category != "all" and category not in SUPPORT_CATEGORY_BUTTONS
            ):
                await self._ack(event, texts.SUPPORT_BAD_LIST, alert=True)  # type: ignore[attr-defined]
                return
            await self._show_admin_support(
                event, view, category, _to_int(parts[4]) or 0
            )
            return
        ticket_id = _to_int(parts[2]) if len(parts) >= 3 else None
        if ticket_id is None:
            await self._ack(event, texts.SUPPORT_INVALID_REQUEST, alert=True)  # type: ignore[attr-defined]
            return
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            await self._ack(event, texts.SUPPORT_TICKET_NOT_FOUND, alert=True)  # type: ignore[attr-defined]
            return
        if parts[1] == "sv":
            await self._show_admin_ticket(
                event,
                ticket_id,
                (_to_int(parts[3]) or 0) if len(parts) > 3 else 0,
            )
        elif parts[1] == "sr":
            if ticket["status"] == "closed":
                await self._ack(  # type: ignore[attr-defined]
                    event,
                    texts.SUPPORT_TICKET_CLOSED,
                    alert=True,
                )
                return
            self._support_admin_states[admin_id] = (ticket_id, self.deps.monotonic())
            await event.respond(
                texts.SUPPORT_ADMIN_REPLY_PROMPT.format(
                    ticket_id=ticket["public_ticket_id"]
                ),
                buttons=[[Button.inline(texts.BTN_BACK, f"adm:sv:{ticket_id}".encode())]],
            )
        elif parts[1] == "sc":
            try:
                closed = await self.db.close_ticket(ticket_id, admin_id, admin=True)
            except (ValueError, PermissionError):
                closed = False
            if closed:
                await self._notify_user(
                    int(ticket["user_id"]),
                    texts.SUPPORT_CLOSED_NOTICE.format(
                        ticket_id=ticket["public_ticket_id"]
                    ),
                    buttons=[
                        [
                            Button.inline(
                                texts.BTN_VIEW_TICKET, f"sup:v:{ticket_id}".encode()
                            )
                        ]
                    ],
                )
            await self._show_admin_ticket(event, ticket_id)
        elif parts[1] == "so":
            try:
                reopened = await self.db.reopen_ticket(
                    ticket_id, admin_id, admin=True
                )
            except (ValueError, PermissionError):
                reopened = False
            if reopened:
                await self._notify_user(
                    int(ticket["user_id"]),
                    texts.SUPPORT_REOPENED_NOTICE.format(
                        ticket_id=ticket["public_ticket_id"]
                    ),
                    buttons=[
                        [
                            Button.inline(
                                texts.BTN_VIEW_TICKET, f"sup:v:{ticket_id}".encode()
                            )
                        ]
                    ],
                )
            await self._show_admin_ticket(event, ticket_id)
        else:
            await self._ack(event, texts.SUPPORT_INVALID_REQUEST, alert=True)  # type: ignore[attr-defined]
