"""User and administrator support ticket workflows (SRP).

Encapsulates ticket creation, paginated history viewing, replies, status
transitions, and operator inbox filtering.
"""

from __future__ import annotations

import logging
from typing import Any

from telethon import Button

from .bot_ui import (
    CATEGORY_FILTER_LABELS,
    SUPPORT_CATEGORY_BUTTONS,
    SUPPORT_STATUS_LABELS,
    VIEW_LABELS,
)
from .db import TicketStateError
from .intake import _to_int
from .progress import to_persian_digits
from .telegram_text import split_message

logger = logging.getLogger("gamas_bot.bot.support")


class SupportWorkflowMixin:
    """User and admin support ticket screens and message handlers."""

    db: Any
    settings: Any
    client: Any
    _support_states: dict[int, tuple[str, object]]
    _support_admin_states: dict[int, int]

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
            lines.append("… پیام‌های قدیمی‌تر در صفحهٔ بعدی")
        for message in messages:
            if message["sender_role"] == "admin":
                who = "🎧 پشتیبانی"
            elif viewer == "admin":
                who = f"👤 کاربر {message['sender_id']}"
            else:
                who = "👤 شما"
            lines.append(
                f"{who} · {self._support_when(message['created_at'])}\n"
                f"{message['message_text']}"
            )
        return "\n\n".join(lines) if lines else "هنوز پیامی ثبت نشده است."

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
        text = (
            "🎧 پشتیبانی\n\n"
            "برای ثبت درخواست جدید یا پیگیری درخواست‌های قبلی، یکی از گزینه‌ها را انتخاب کنید."
        )
        buttons = [
            [Button.inline("➕ درخواست جدید", b"sup:new")],
            [Button.inline("📋 درخواست‌های من", b"sup:list")],
            [Button.inline("↩️ منوی اصلی", b"menu:main")],
        ]
        await self._edit_or_reply(event, text, buttons)  # type: ignore[attr-defined]

    async def _show_support_categories(self, event: Any) -> None:
        buttons = [
            [Button.inline(label, f"sup:cat:{key}".encode())]
            for key, label in SUPPORT_CATEGORY_BUTTONS.items()
        ]
        buttons.append([Button.inline("↩️ بازگشت", b"support")])
        await self._edit_or_reply(  # type: ignore[attr-defined]
            event, "دسته‌بندی درخواست را انتخاب کنید:", buttons
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
            text = "شما هنوز درخواستی ثبت نکرده‌اید."
        else:
            text = f"📋 درخواست‌های شما — صفحهٔ {to_persian_digits(page + 1)}"
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
            nav.append(Button.inline("◀️ قبلی", f"sup:lp:{page - 1}".encode()))
        if has_more:
            nav.append(Button.inline("بعدی ▶️", f"sup:lp:{page + 1}".encode()))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("➕ درخواست جدید", b"sup:new")])
        buttons.append([Button.inline("↩️ بازگشت", b"support")])
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
            await self._ack(event, "این درخواست پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            await self._show_user_tickets(event, user_id)
            return
        page = max(0, page)
        offset = page * self.SUPPORT_HISTORY_PAGE
        total = await self.db.count_ticket_messages(ticket_id)
        messages = await self.db.ticket_messages(
            ticket_id, limit=self.SUPPORT_HISTORY_PAGE, offset=offset
        )
        head = (
            f"🎫 درخواست {ticket['public_ticket_id']}\n"
            f"دسته: {self._support_category_label(ticket['category'])}\n"
            f"وضعیت: {self._support_status_label(ticket['status'])}\n"
            f"موضوع: {ticket['subject']}\n"
            f"ثبت: {self._support_when(ticket['created_at'])}\n"
        )
        history = self._support_history_text(messages, total, offset)
        text = f"{head}\n{history}"
        buttons = []
        if ticket["status"] != "closed":
            buttons.append(
                [Button.inline("✍️ پاسخ به پشتیبانی", f"sup:r:{ticket_id}".encode())]
            )
            buttons.append(
                [Button.inline("✅ بستن درخواست", f"sup:c:{ticket_id}".encode())]
            )
        else:
            buttons.append(
                [Button.inline("🔁 باز کردن دوباره", f"sup:o:{ticket_id}".encode())]
            )
        if total > offset + self.SUPPORT_HISTORY_PAGE:
            buttons.append(
                [
                    Button.inline(
                        "پیام‌های قدیمی‌تر ▶️",
                        f"sup:v:{ticket_id}:{page + 1}".encode(),
                    )
                ]
            )
        if page > 0:
            buttons.append(
                [
                    Button.inline(
                        "◀️ پیام‌های جدیدتر",
                        f"sup:v:{ticket_id}:{page - 1}".encode(),
                    )
                ]
            )
        buttons.append([Button.inline("↩️ درخواست‌های من", b"sup:list")])
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
                await self._ack(event, "این دسته معتبر نیست.", alert=True)  # type: ignore[attr-defined]
                return
            self._support_states[user_id] = ("new", category)
            await event.respond(
                f"{SUPPORT_CATEGORY_BUTTONS[category]}\n\n"
                "موضوع یا شرح کوتاه مشکل را در یک پیام بفرستید (حداکثر ۴۰۰ نویسه).\n"
                "برای لغو، /cancel بفرستید.",
                buttons=[[Button.inline("🔙 بازگشت", b"sup:new")]],
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
            await self._ack(event, "درخواست نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        if parts[1] == "v":
            page = (_to_int(parts[3]) or 0) if len(parts) > 3 else 0
            await self._show_user_ticket(event, user_id, ticket_id, page)
        elif parts[1] == "r":
            ticket = await self._owned_ticket(user_id, ticket_id)
            if ticket is None or ticket["status"] == "closed":
                await self._ack(  # type: ignore[attr-defined]
                    event,
                    "این درخواست پاسخ‌پذیر نیست؛ ابتدا آن را باز کنید.",
                    alert=True,
                )
                return
            self._support_states[user_id] = ("reply", ticket_id)
            await event.respond(
                f"پیام خود را برای درخواست {ticket['public_ticket_id']} بفرستید.\n"
                "برای لغو، /cancel بفرستید.",
                buttons=[[Button.inline("🔙 بازگشت", f"sup:v:{ticket_id}".encode())]],
            )
        elif parts[1] == "c":
            ticket = await self._owned_ticket(user_id, ticket_id)
            if ticket is None:
                await self._ack(event, "این درخواست پیدا نشد.", alert=True)  # type: ignore[attr-defined]
                return
            try:
                closed = await self.db.close_ticket(ticket_id, user_id, admin=False)
            except (ValueError, PermissionError, TicketStateError):
                closed = False
            if closed:
                owner = await self._identity_for_id(user_id)  # type: ignore[attr-defined]
                await self._notify_admins(
                    f"🔒 کاربر {owner} درخواست {ticket['public_ticket_id']} را بست."
                )
                logger.info("support ticket closed by user ticket=%s", ticket_id)
            await self._show_user_ticket(event, user_id, ticket_id)
        elif parts[1] == "o":
            ticket = await self._owned_ticket(user_id, ticket_id)
            if ticket is None:
                await self._ack(event, "این درخواست پیدا نشد.", alert=True)  # type: ignore[attr-defined]
                return
            try:
                reopened = await self.db.reopen_ticket(ticket_id, user_id, admin=False)
            except (ValueError, PermissionError, TicketStateError):
                reopened = False
            if reopened:
                owner = await self._identity_for_id(user_id)  # type: ignore[attr-defined]
                await self._notify_admins(
                    f"🔁 کاربر {owner} درخواست {ticket['public_ticket_id']} را دوباره باز کرد."
                )
                logger.info("support ticket reopened by user ticket=%s", ticket_id)
            await self._show_user_ticket(event, user_id, ticket_id)
        else:
            await self._ack(event, "درخواست نامعتبر است.", alert=True)  # type: ignore[attr-defined]

    async def _handle_support_text(
        self, event: Any, user_id: int, state: tuple, text: str
    ) -> None:
        """The next plain message of a user who opened a support prompt."""
        kind, value = state
        if not text:
            await event.reply(
                "لطفاً پیام خود را به‌صورت متن بفرستید؛ برای لغو /cancel بفرستید."
            )
            return
        if kind == "new":
            try:
                ticket = await self.db.create_ticket(
                    user_id,
                    value,
                    text,
                    initial_message=text,
                    telegram_message_id=getattr(event.message, "id", None),
                )
            except ValueError:
                await event.reply("موضوع باید بین ۲ تا ۴۰۰ نویسه باشد؛ دوباره بنویسید.")
                return
            self._support_states.pop(user_id, None)
            logger.info(
                "support ticket created ticket=%s category=%s", ticket["id"], value
            )
            await event.reply(
                "✅ درخواست شما ثبت شد.\n"
                f"شماره پیگیری: {ticket['public_ticket_id']}\n"
                "از همین بخش می‌توانید پاسخ‌ها را پیگیری کنید.",
                buttons=[
                    [
                        Button.inline(
                            "📋 مشاهدهٔ درخواست", f"sup:v:{ticket['id']}".encode()
                        )
                    ]
                ],
            )
            owner = await self._identity_for_id(user_id)  # type: ignore[attr-defined]
            await self._notify_admins(
                "🎧 درخواست پشتیبانی جدید\n"
                f"شماره: {ticket['public_ticket_id']}\n"
                f"کاربر: {owner}\n"
                f"دسته: {self._support_category_label(value)}\n"
                f"موضوع: {ticket['subject']}",
                buttons=[
                    [
                        Button.inline(
                            "🔎 بررسی درخواست", f"adm:sv:{ticket['id']}".encode()
                        )
                    ]
                ],
            )
            return
        if len(text) > self.db.SUPPORT_MESSAGE_MAX:
            await event.reply("پیام باید حداکثر ۴۰۰۰ نویسه باشد؛ کوتاه‌تر بنویسید.")
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
            await event.reply(
                "این درخواست دیگر پاسخ‌پذیر نیست؛ ابتدا آن را باز کنید."
            )
            return
        self._support_states.pop(user_id, None)
        refreshed_ticket = await self.db.get_ticket(value)
        public_id = (
            refreshed_ticket["public_ticket_id"] if refreshed_ticket else str(value)
        )
        await event.reply(
            "✅ پیام شما ثبت شد.",
            buttons=[[Button.inline("📋 مشاهدهٔ درخواست", f"sup:v:{value}".encode())]],
        )
        owner = await self._identity_for_id(user_id)  # type: ignore[attr-defined]
        await self._notify_admins(
            f"💬 پیام جدیدی در درخواست {public_id} ثبت شد.\nکاربر: {owner}",
            buttons=[[Button.inline("🔎 بررسی درخواست", f"adm:sv:{value}".encode())]],
        )

    async def _handle_admin_support_text(
        self, event: Any, admin_id: int, ticket_id: int, text: str
    ) -> None:
        if not text:
            await event.reply(
                "پاسخ باید متن داشته باشد؛ دوباره متن را بفرستید یا /cancel بزنید."
            )
            return
        if len(text) > self.db.SUPPORT_MESSAGE_MAX:
            await event.reply("پاسخ باید حداکثر ۴۰۰۰ نویسه باشد؛ کوتاه‌تر بنویسید.")
            return
        try:
            await self.db.add_ticket_message(
                ticket_id,
                admin_id,
                "admin",
                text,
                telegram_message_id=getattr(event.message, "id", None),
            )
        except (ValueError, TicketStateError):
            self._support_admin_states.pop(admin_id, None)
            await event.reply(
                "این درخواست دیگر پاسخ‌پذیر نیست؛ ابتدا آن را باز کنید.",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        self._support_admin_states.pop(admin_id, None)
        ticket = await self.db.get_ticket(ticket_id)
        public_id = ticket["public_ticket_id"] if ticket else str(ticket_id)
        logger.info("support admin reply ticket=%s", ticket_id)
        await event.reply(
            f"✅ پاسخ به درخواست {public_id} ثبت شد.",
            buttons=[
                [Button.inline("🔎 مشاهدهٔ درخواست", f"adm:sv:{ticket_id}".encode())]
            ],
        )
        if ticket is not None:
            await self._notify_user(
                int(ticket["user_id"]),
                f"📩 پاسخ جدیدی برای درخواست {public_id} ثبت شده است.",
                buttons=[
                    [Button.inline("📋 مشاهدهٔ پاسخ", f"sup:v:{ticket_id}".encode())]
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
        text = (
            "🎧 مدیریت پشتیبانی\n\n"
            f"فعال: {to_persian_digits(open_count)} | "
            f"جدید: {to_persian_digits(new_open_count)} | "
            f"منتظر پاسخ شما: {to_persian_digits(counts.get('awaiting_admin', 0))} | "
            f"بسته: {to_persian_digits(counts.get('closed', 0))}\n"
            f"فهرست: {VIEW_LABELS.get(view, view)} — دسته: {CATEGORY_FILTER_LABELS.get(category, category)}"
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
            [Button.inline("📂 همه دسته‌ها", f"adm:sl:{view}:all:0".encode())],
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
                    "◀️ قبلی", f"adm:sl:{view}:{category}:{page - 1}".encode()
                )
            )
        if has_more:
            nav.append(
                Button.inline(
                    "بعدی ▶️", f"adm:sl:{view}:{category}:{page + 1}".encode()
                )
            )
        if nav:
            buttons.append(nav)
        if not rows:
            text += "\n\nدرخواستی در این فهرست نیست."
        buttons.append([Button.inline("↩️ پنل مدیریت", b"adm:home")])
        await self._edit_or_reply(event, text, buttons)  # type: ignore[attr-defined]

    async def _show_admin_ticket(
        self, event: Any, ticket_id: int, page: int = 0
    ) -> None:
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            await self._ack(event, "این درخواست پیدا نشد.", alert=True)  # type: ignore[attr-defined]
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
        head = (
            f"🎫 درخواست {ticket['public_ticket_id']}\n"
            f"کاربر: {owner}\n"
            f"دسته: {self._support_category_label(ticket['category'])}\n"
            f"وضعیت: {self._support_status_label(ticket['status'])}\n"
            f"موضوع: {ticket['subject']}\n"
            f"ثبت: {self._support_when(ticket['created_at'])} | "
            f"آخرین تغییر: {self._support_when(ticket['updated_at'])}\n"
        )
        text = (
            f"{head}\n"
            f"{self._support_history_text(messages, total, offset, viewer='admin')}"
        )
        buttons = []
        if ticket["status"] != "closed":
            buttons.append(
                [Button.inline("✍️ پاسخ به کاربر", f"adm:sr:{ticket_id}".encode())]
            )
            buttons.append(
                [Button.inline("✅ بستن درخواست", f"adm:sc:{ticket_id}".encode())]
            )
        else:
            buttons.append(
                [Button.inline("🔁 باز کردن دوباره", f"adm:so:{ticket_id}".encode())]
            )
        if total > offset + self.SUPPORT_HISTORY_PAGE:
            buttons.append(
                [
                    Button.inline(
                        "پیام‌های قدیمی‌تر ▶️",
                        f"adm:sv:{ticket_id}:{page + 1}".encode(),
                    )
                ]
            )
        if page > 0:
            buttons.append(
                [
                    Button.inline(
                        "◀️ پیام‌های جدیدتر",
                        f"adm:sv:{ticket_id}:{page - 1}".encode(),
                    )
                ]
            )
        buttons.append([Button.inline("↩️ فهرست پشتیبانی", b"adm:sup")])
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
                await self._ack(event, "فهرست نامعتبر است.", alert=True)  # type: ignore[attr-defined]
                return
            await self._show_admin_support(
                event, view, category, _to_int(parts[4]) or 0
            )
            return
        ticket_id = _to_int(parts[2]) if len(parts) >= 3 else None
        if ticket_id is None:
            await self._ack(event, "درخواست نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            await self._ack(event, "این درخواست پیدا نشد.", alert=True)  # type: ignore[attr-defined]
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
                    "این درخواست بسته است؛ ابتدا آن را باز کنید.",
                    alert=True,
                )
                return
            self._support_admin_states[admin_id] = ticket_id
            await event.respond(
                f"پاسخ خود را برای درخواست {ticket['public_ticket_id']} بنویسید.\n"
                "پس از ارسال، کاربر از پاسخ شما مطلع می‌شود. برای لغو /cancel بفرستید.",
                buttons=[[Button.inline("↩️ بازگشت", f"adm:sv:{ticket_id}".encode())]],
            )
        elif parts[1] == "sc":
            try:
                closed = await self.db.close_ticket(ticket_id, admin_id, admin=True)
            except (ValueError, PermissionError):
                closed = False
            if closed:
                await self._notify_user(
                    int(ticket["user_id"]),
                    f"✅ درخواست {ticket['public_ticket_id']} بسته شد. "
                    "اگر مشکل شما هنوز باقی است، می‌توانید درخواست را دوباره باز کنید.",
                    buttons=[
                        [
                            Button.inline(
                                "📋 مشاهدهٔ درخواست", f"sup:v:{ticket_id}".encode()
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
                    f"🔁 درخواست {ticket['public_ticket_id']} دوباره باز شد.",
                    buttons=[
                        [
                            Button.inline(
                                "📋 مشاهدهٔ درخواست", f"sup:v:{ticket_id}".encode()
                            )
                        ]
                    ],
                )
            await self._show_admin_ticket(event, ticket_id)
        else:
            await self._ack(event, "درخواست نامعتبر است.", alert=True)  # type: ignore[attr-defined]
