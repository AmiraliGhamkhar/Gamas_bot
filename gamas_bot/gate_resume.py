"""Resume a request that was blocked by the mandatory-channel gate.

The gate no longer runs before every interaction. A file that arrives while the
user is not a member is remembered (database, one row per user) and re-entered
through the normal intake path once ``sub:check`` confirms membership. Telethon
still holds the original message, so the user never resubmits the file.

:class:`ResumedEvent` is the smallest object the existing handlers accept: the
original message plus reply/edit helpers that send into the same chat. It is not
a second handler stack; it only bridges the stored request back into the bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ResumedEvent:
    """A stored Telegram message replayed as an event after joining."""

    client: Any
    chat_id: int
    sender_id: int
    message: Any
    data: bytes | None = None
    is_private: bool = True

    @property
    def message_id(self) -> int | None:
        return getattr(self.message, "id", None)

    async def reply(self, text: str, buttons: Any = None, **kwargs: Any) -> Any:
        return await self.client.send_message(
            self.chat_id, text, buttons=buttons, **kwargs
        )

    async def respond(self, text: str, buttons: Any = None, **kwargs: Any) -> Any:
        return await self.client.send_message(
            self.chat_id, text, buttons=buttons, **kwargs
        )

    async def edit(self, text: str, buttons: Any = None, **kwargs: Any) -> Any:
        return await self.client.send_message(
            self.chat_id, text, buttons=buttons, **kwargs
        )

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        """Callback acknowledgements have no visible equivalent here."""

    async def get_sender(self) -> None:
        return None
