"""Telegram job progress messages.

The reporter edits one status message whenever Telethon returned a message
object.  Tests and unusual Telegram clients that do not expose ``edit`` fall
back to sending stage updates, so reporting can never break the actual job.

The bar is deliberately playful: a rocket rides the leading edge of the bar,
a braille spinner advances on every published frame, and an optional
background ticker keeps that spinner cycling while one stage runs (STT alone
can take hours).  Frames only change when Telegram accepts an edit, and a
flood wait makes the ticker back off, so the animation can never outrun the
API or block the job.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)

#: Spinner rendered next to the percentage; one frame per published update.
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

#: The emoji riding the fill boundary while work is in flight.
DEFAULT_HEAD = "🚀"

#: Head shown on the bar of a failed job.
FAILED_HEAD = "🛑"

#: Story-telling emoji per progress bracket (upper bounds, checked in order).
STAGE_EMOJI: tuple[tuple[int, str], ...] = (
    (9, "📥"),
    (29, "⬇️"),
    (49, "🎚️"),
    (69, "🎙️"),
    (89, "📝"),
    (99, "📖"),
    (100, "📤"),
)

BAR_WIDTH = 12
PERSIAN_DIGIT_TABLE = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

#: How often the ticker re-edits the message while a stage is running.
ANIMATION_INTERVAL_SECONDS = 5.0
#: Flood waits grow the interval up to this multiplier before giving up.
ANIMATION_MAX_BACKOFF = 6
#: Hard stop for the ticker; a job must never leak animation edits forever.
ANIMATION_MAX_RUNTIME_SECONDS = 6 * 3600


def to_persian_digits(value: int | str) -> str:
    """Render digits with Persian numerals, which read naturally in RTL text."""
    return str(value).translate(PERSIAN_DIGIT_TABLE)


def stage_emoji(percent: int) -> str:
    """The story emoji for the current progress bracket."""
    value = max(0, min(100, int(percent)))
    for bound, emoji in STAGE_EMOJI:
        if value <= bound:
            return emoji
    return "📤"


def progress_bar(percent: int, width: int = BAR_WIDTH, head: str = DEFAULT_HEAD) -> str:
    """Render a fixed-width bar whose fill edge is carried by an emoji."""
    value = max(0, min(100, int(percent)))
    if value >= 100:
        return "█" * width
    filled = round(value * width / 100)
    if head:
        # The emoji occupies one cell, so the fill must leave room for it.
        # Without this clamp, 96-99% rounded up to a full bar and pushed the
        # emoji past the fixed width (13 visible cells instead of 12).
        filled = min(filled, width - 1)
        return "█" * filled + head + "░" * (width - filled - 1)
    return "█" * filled + "░" * (width - filled)


def progress_text(
    percent: int,
    stage: str,
    detail: str = "",
    *,
    frame: int = 0,
    head: str = DEFAULT_HEAD,
) -> str:
    """One rendered progress frame: bar, animated spinner, stage and detail."""
    value = max(0, min(100, int(percent)))
    spinner = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
    text = (
        f"⏳ دارم جزوه‌تان را آماده می‌کنم {stage_emoji(value)}\n\n"
        f"{progress_bar(value, head=head)}  {to_persian_digits(value)}٪  {spinner}\n"
        f"{stage.strip()}"
    )
    if detail.strip():
        text += "\n" + detail.strip()
    return text


class JobProgress:
    """Best-effort, monotonic progress reporting for one background job."""

    def __init__(
        self,
        event: Any,
        message: Any = None,
        *,
        submission_id: int | None = None,
        animation_interval: float = ANIMATION_INTERVAL_SECONDS,
    ):
        self.event = event
        self.message = message
        self.submission_id = submission_id
        self.percent = 0
        self._last_text = ""
        self._lock = asyncio.Lock()
        self._frame = 0
        self._stage = ""
        self._detail = ""
        self._done = False
        self._flood_streak = 0
        self._animation_interval = animation_interval
        self._ticker: asyncio.Task | None = None

    @classmethod
    async def create(
        cls,
        event: Any,
        *,
        submission_id: int | None = None,
        stage: str = "فایل رسید و در صف است",
        animate: bool = True,
        animation_interval: float = ANIMATION_INTERVAL_SECONDS,
    ) -> "JobProgress":
        text = progress_text(0, stage, "کمی صبر کنید؛ به‌زودی شروع می‌کنم.")
        try:
            message = await event.reply(text)
        except Exception:
            logger.exception(
                "Could not create progress message submission_id=%s", submission_id
            )
            message = None
        reporter = cls(
            event, message, submission_id=submission_id, animation_interval=animation_interval
        )
        reporter._last_text = text
        reporter._stage = stage
        reporter._detail = "کمی صبر کنید؛ به‌زودی شروع می‌کنم."
        if animate:
            reporter.start_animation()
        return reporter

    # -- animation ---------------------------------------------------------

    def start_animation(self) -> None:
        """Keep the spinner cycling while a long stage is running."""
        if self._done or (self._ticker is not None and not self._ticker.done()):
            return
        try:
            self._ticker = asyncio.create_task(
                self._animate(),
                name=f"progress-animation-{self.submission_id}",
            )
        except RuntimeError:  # no running loop (unit tests, sync callers)
            self._ticker = None

    async def _animate(self) -> None:
        started = time.monotonic()
        try:
            while not self._done:
                await asyncio.sleep(
                    self._animation_interval * min(self._flood_streak + 1, ANIMATION_MAX_BACKOFF)
                )
                if self._done:
                    break
                if time.monotonic() - started > ANIMATION_MAX_RUNTIME_SECONDS:
                    logger.info(
                        "Progress animation reached its runtime cap submission_id=%s",
                        self.submission_id,
                    )
                    break
                self._advance_frame()
                await self._publish(self._render())
        except asyncio.CancelledError:
            pass
        except Exception:
            # The animation is decorative: it must never break a job.
            logger.warning(
                "Progress animation stopped submission_id=%s",
                self.submission_id,
                exc_info=True,
            )

    def _advance_frame(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER_FRAMES)

    def _render(self) -> str:
        return progress_text(
            self.percent, self._stage, self._detail, frame=self._frame
        )

    def _finish(self) -> None:
        self._done = True
        if self._ticker is not None:
            self._ticker.cancel()

    async def aclose(self) -> None:
        """Stop the animation ticker; safe to call more than once."""
        self._finish()
        ticker, self._ticker = self._ticker, None
        if ticker is not None and not ticker.done():
            try:
                await ticker
            except asyncio.CancelledError:
                pass

    # -- job updates -------------------------------------------------------

    async def update(self, percent: int, stage: str, detail: str = "") -> None:
        value = max(self.percent, min(100, int(percent)))
        self.percent = value
        self._stage = stage
        self._detail = detail
        self._advance_frame()
        await self._publish(self._render())

    async def complete(
        self, detail: str = "جزوهٔ Word و متن خام در پیام‌های بعدی می‌رسند."
    ) -> None:
        self._finish()
        self.percent = 100
        text = (
            "🎉 جزوه آماده شد! 🎉\n\n"
            f"{progress_bar(100)}  ۱۰۰٪  ✅\n"
            f"{detail.strip()}"
        )
        await self._publish(text)

    async def fail(self, reference: str) -> None:
        self._finish()
        text = (
            "❌ متأسفم، کار کامل نشد.\n\n"
            f"{progress_bar(self.percent, head=FAILED_HEAD)}  "
            f"{to_persian_digits(self.percent)}٪  💔\n"
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
                await self._edit_or_respond(text)
                self._last_text = text
                self._flood_streak = 0
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
                        await self._edit_or_respond(text)
                        self._last_text = text
                        self._flood_streak = 0
                    except Exception:
                        self._flood_streak += 1
                        logger.warning(
                            "Progress retry failed submission_id=%s",
                            self.submission_id,
                            exc_info=True,
                        )
                else:
                    self._flood_streak += 1
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

    async def _edit_or_respond(self, text: str) -> None:
        if self.message is not None and callable(getattr(self.message, "edit", None)):
            await self.message.edit(text)
        else:
            await self.event.respond(text)
