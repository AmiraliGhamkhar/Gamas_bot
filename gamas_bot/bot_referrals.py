"""Invite-a-friend screen and administrator referral report (SRP)."""

from __future__ import annotations

import logging
from typing import Any

from telethon import Button

from . import texts
from .db_referrals import REFERRAL_JOIN_SECONDS, REFERRAL_PAYMENT_SECONDS
from .billing import format_seconds_as_credits

logger = logging.getLogger("gamas_bot.bot.referrals")


class ReferralWorkflowMixin:
    """User invite screen and read-only operator referral statistics."""

    db: Any
    client: Any

    def _invite_link(self, username: str | None, code: str) -> str:
        handle = (username or "").strip().lstrip("@")
        if not handle:
            return ""
        return f"https://t.me/{handle}?start=ref_{code}"

    async def _apply_referral(self, event: Any, user_id: int, code: str) -> None:
        """Attribute a first start and tell the invitee when a reward was paid."""
        status, seconds = await self.db.attribute_referral(user_id, code)
        if status == "rewarded" and seconds:
            await event.reply(
                texts.REFERRAL_JOIN_REWARD_TEMPLATE.format(
                    seconds=format_seconds_as_credits(seconds)
                )
            )
        elif status == "self":
            await event.reply(texts.REFERRALS_SELF)
        elif status == "duplicate":
            await event.reply(texts.REFERRALS_DUPLICATE)

    async def _show_referrals(self, event: Any, user_id: int) -> None:
        await self.db.ensure_user(user_id)
        code = await self.db.ensure_referral_code(user_id)
        summary = await self.db.referral_summary(user_id)
        link = ""
        try:
            me = await self.client.get_me()
            link = self._invite_link(getattr(me, "username", None), code)
        except Exception:  # noqa: BLE001 - a missing username only hides the link
            logger.debug("could not read the bot username", exc_info=True)
        lines = [
            texts.REFERRALS_TITLE,
            "",
            texts.REFERRALS_RULES.format(
                signup_credits=format_seconds_as_credits(REFERRAL_JOIN_SECONDS),
                purchase_credits=format_seconds_as_credits(REFERRAL_PAYMENT_SECONDS),
            ),
            "",
        ]
        if link:
            lines.append(texts.REFERRALS_LINK_TEMPLATE.format(link=link))
        else:
            lines.append(texts.REFERRALS_CODE_TEMPLATE.format(code=code))
        lines.extend(
            [
                "",
                texts.REFERRALS_STATS_TEMPLATE.format(
                    invited=summary["invited"],
                    seconds=format_seconds_as_credits(summary["seconds"]),
                ),
            ]
        )
        buttons: list[list[Any]] = []
        if link:
            share = (
                "https://t.me/share/url?url="
                + link.replace(":", "%3A").replace("/", "%2F").replace("?", "%3F")
                + "&text="
                + "به گاماس بپیوند"
            )
            buttons.append([Button.url(texts.BTN_SHARE_INVITE, share)])
        buttons.append([Button.inline(texts.BTN_BACK_MAIN, b"menu:main")])
        await self._edit_or_reply(  # type: ignore[attr-defined]
            event, "\n".join(lines), buttons
        )

    async def _show_admin_referrals(self, event: Any) -> None:
        totals = await self.db.referral_totals()
        rows = await self.db.top_referrers(10)
        lines = [
            texts.ADMIN_REFERRALS_HEADER,
            "",
            texts.ADMIN_REFERRALS_TOTALS_TEMPLATE.format(
                rewards=totals["rewards"],
                seconds=format_seconds_as_credits(totals["seconds"]),
            ),
            "",
        ]
        if not rows:
            lines.append(texts.ADMIN_REFERRALS_EMPTY)
        for rank, row in enumerate(rows, start=1):
            who = await self._identity_for_id(int(row["referrer_id"]))  # type: ignore[attr-defined]
            lines.append(
                texts.ADMIN_REFERRALS_ROW_TEMPLATE.format(
                    rank=rank,
                    who=who,
                    invited=row["invited"],
                    seconds=format_seconds_as_credits(row["seconds"]),
                )
            )
        await self._edit_or_reply(  # type: ignore[attr-defined]
            event,
            "\n".join(lines),
            [[Button.inline(texts.BTN_ADMIN_BACK_USER, b"menu:main")]],
        )
