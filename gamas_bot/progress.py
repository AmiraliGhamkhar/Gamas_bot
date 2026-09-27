"""Telegram job progress messages.

The reporter edits one status message whenever Telethon returned a message
object.  Tests and unusual Telegram clients that do not expose ``edit`` fall
back to sending stage updates, so reporting can never break the actual job.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)


def progress_bar(percent: int, width: int = 10) -> str:
    """Render a compact fixed-width progress bar suitable for Telegram."""
    value = max(0, min(100, int(percent)))
    filled = round(value * width / 100)
    return "█" * filled + "░" * (width - filled)


def progress_text(percent: int, stage: str, detail: str = "") -> str:
    value = max(0, min(100, int(percent)))
    text = (
        "⏳ دارم جزوه‌تان را آماده می‌کنم\n\n"
        f"{progress_bar(value)}  {value}٪\n"
        f"{stage.strip()}"
    )
    if detail.strip():
        text += "\n" + detail.strip()
    return text


class JobProgress:
    """Best-effort, monotonic progress reporting for one background job."""

    def __init__(self, event: Any, message: Any = None, *, submission_id: int | None = None):
        self.event = event
        self.message = message
        self.submission_id = submission_id
        self.percent = 0
        self._last_text = ""
        self._lock = asyncio.Lock()

    @classmethod
    async def create(
        cls, event: Any, *, submission_id: int | None = None, stage: str = "فایل رسید و در صف است"
    ) -> "JobProgress":
        text = progress_text(0, stage, "کمی صبر کنید؛ به‌زودی شروع می‌کنم.")
        try:
            message = await event.reply(text)
        except Exception:
            logger.exception(
                "Could not create progress message submission_id=%s", submission_id
            )
            message = None
        reporter = cls(event, message, submission_id=submission_id)
        reporter._last_text = text
        return reporter

    async def update(self, percent: int, stage: str, detail: str = "") -> None:
        value = max(self.percent, min(100, int(percent)))
        self.percent = value
        await self._publish(progress_text(value, stage, detail))

    async def complete(self, detail: str = "جزوه را پایین همین پیام می‌بینید.") -> None:
        self.percent = 100
        text = (
            "✅ جزوه آماده شد!\n\n"
            f"{progress_bar(100)}  ۱۰۰٪\n"
            f"{detail.strip()}"
        )
        await self._publish(text)

    async def fail(self, reference: str) -> None:
        text = (
            "❌ متأسفم، کار کامل نشد.\n\n"
            f"{progress_bar(self.percent)}  {self.percent}٪\n"
            f"کد پیگیری: {reference}"
        )
        await self._publish(text)

    async def _publish(self, text: str) -> None:
        if text == self._last_text:
            return
        async with self._lock:
            if text == self._last_text:
                return
            try:
                if self.message is not None and callable(getattr(self.message, "edit", None)):
                    await self.message.edit(text)
                else:
                    await self.event.respond(text)
                self._last_text = text
                logger.info(
                    "Job progress updated submission_id=%s percent=%s",
                    self.submission_id,
                    self.percent,
                )
            except FloodWaitError as exc:
                # A progress update is not worth blocking a worker for a long
                # Telegram flood wait. Retry only short waits; the job continues.
                if exc.seconds <= 5:
                    await asyncio.sleep(exc.seconds + 0.2)
                    try:
                        if self.message is not None and callable(
                            getattr(self.message, "edit", None)
                        ):
                            await self.message.edit(text)
                        else:
                            await self.event.respond(text)
                        self._last_text = text
                    except Exception:
                        logger.warning(
                            "Progress retry failed submission_id=%s",
                            self.submission_id,
                            exc_info=True,
                        )
                else:
                    logger.warning(
                        "Progress update skipped due to flood wait submission_id=%s seconds=%s",
                        self.submission_id,
                        exc.seconds,
                    )
            except Exception:
                logger.warning(
                    "Could not update progress submission_id=%s",
                    self.submission_id,
                    exc_info=True,
                )
