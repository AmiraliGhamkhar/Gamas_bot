from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import secrets
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import MessageMediaWebPage

from .config import Settings
from .database import Database
from .billing import format_duration, format_toman, plan_catalog
from .docx_export import (
    DocxPaginationError,
    DocumentMeta,
    build_notes_docx,
    build_plain_docx,
    build_raw_text_document,
    notes_docx_filename,
    plain_docx_filename,
    raw_text_filename,
    resolve_design,
    resolve_fonts,
)
from .logging_config import log_job_id
from .media import (
    MediaToolError,
    check_media_worker,
    convert_to_pptx,
    extract_audio_track,
    needs_transcode,
    probe_media,
)
from .presentations import (
    PresentationError,
    classify_presentation,
    load_presentation,
    prepare_audio,
    slides_outline,
)
from .progress import JobProgress
from .provider_credentials import (
    CredentialStoreError,
    PROVIDER_CHOICES,
    ProviderCredentialManager,
    use_provider_credentials,
)
from .stt import STTConfigurationError, transcribe
from .structuring import (
    StructuredNotes,
    StructuringError,
    structure_presentation,
    structure_transcript,
)

logger = logging.getLogger(__name__)
AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".amr", ".au", ".flac", ".m4a", ".m4b", ".mp3",
    ".oga", ".ogg", ".opus", ".ra", ".wav", ".wma", ".webm",
}
VIDEO_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
    ".mpg", ".ts", ".webm", ".wmv",
}
MESSAGE_CHUNK_SIZE = 3800
# Telegram refuses messages whose *parsed* text is longer than 4096 UTF-16
# code units. HTML tags do not count, but the renderer below can still grow the
# visible text (table headers are repeated on every row), so the rendered page
# is what has to be measured before sending.
TELEGRAM_TEXT_LIMIT = 4096
MIN_CHUNK_SIZE = 400
TAG_PATTERN = re.compile(r"<[^>]+>")
# A configuration error (unsupported language/model for the selected engine)
# is actionable for the operator, so it is reported verbatim. Transient provider
# failures are not: the tracking reference is what the user needs for those.
USER_VISIBLE_ERRORS = (
    ValueError,
    PresentationError,
    STTConfigurationError,
    DocxPaginationError,
)


class InsufficientBalanceError(ValueError):
    """A safe user-facing billing denial with auditable available/required seconds."""

    def __init__(self, available_seconds: int, required_seconds: int):
        self.available_seconds = int(available_seconds)
        self.required_seconds = int(required_seconds)
        super().__init__(
            "اعتبار زمانی شما کافی نیست. "
            f"موجود: {format_duration(self.available_seconds)}؛ "
            f"موردنیاز: {format_duration(self.required_seconds)}. "
            "از منوی «خرید اعتبار» یکی از طرح‌ها را انتخاب کنید."
        )


UNSUPPORTED_FILE_MESSAGE = (
    "این نوع فایل را نمی‌توانم پردازش کنم. 🤔\n\n"
    "یکی از این‌ها را بفرستید:\n"
    "• پیام صوتی یا فایل صوتی\n"
    "• ویدیوی کلاس\n"
    "• فایل PowerPoint\n\n"
    "فهرست دقیق پسوندها در بخش «قالب‌ها» است."
)
UNSUPPORTED_PRESENTATION_MESSAGE = (
    "فایل‌های ODP/OTP پشتیبانی نمی‌شوند. ♻️\n\n"
    "لطفاً ارائه را در برنامهٔ PowerPoint (یا LibreOffice) با پسوند pptx ذخیره "
    "و دوباره ارسال کنید."
)
WELCOME_TEXT = (
    "سلام! 👋\n\n"
    "فایل صوتی، ویدیوی کلاس یا PowerPoint را بفرستید؛ من محتوایش را به یک جزوهٔ "
    "مرتب تبدیل می‌کنم و روند کار را همین‌جا نشان می‌دهم.\n\n"
    "برای شروع، فایل را بفرستید یا روی «ساخت جزوه» بزنید."
)
HELP_TEXT = (
    "چطور جزوه بسازم؟ 📚\n\n"
    "۱) فایل صوتی، ویدیو یا PowerPoint را بفرستید.\n"
    "۲) پیشرفت کار را در همان پیام دنبال کنید.\n"
    "۳) جزوه به شکل یک فایل Word مرتب (راست‌به‌چپ) به‌همراه متن خام پیاده‌شده "
    "ارسال می‌شود.\n\n"
    "PowerPoint بدون صدا هم قابل استفاده است؛ در این حالت جزوه از متن و یادداشت "
    "اسلایدها ساخته می‌شود. اگر ساخت جزوهٔ هوشمند موقتاً در دسترس نباشد، متن خام را "
    "از دست نمی‌دهید و همان را در قالب فایل تحویل می‌گیرید."
)
FORMATS_TEXT = (
    "چه فایل‌هایی می‌توانم بفرستم؟ 🧰\n\n"
    "• صوت: MP3، M4A، WAV، OGG، OPUS، FLAC، WMA و AMR\n"
    "• ویدیو: MP4، MKV، MOV، AVI، WEBM و ویدیوی گرد تلگرام\n"
    "• ارائه: PPTX، PPTM، PPSX، PPSM، POTX، POTM، PPT، PPS، POT\n\n"
    "قالب‌های صوتی و ویدیویی رایج دیگر هم معمولاً قابل پردازش‌اند. فایل‌های "
    "ODP/OTP پشتیبانی نمی‌شوند؛ آن‌ها را با پسوند pptx ذخیره کنید."
)
PRIVACY_TEXT = (
    "حریم خصوصی 🔐\n\n"
    "صدای فایل برای تبدیل به متن و متن به‌دست‌آمده برای ساخت جزوه به سرویس‌های ربات "
    "فرستاده می‌شوند. فایل‌های موقت پس از پردازش حذف می‌شوند، اما متن و جزوه ممکن است "
    "در پایگاه‌داده بمانند. رسید پرداخت فقط برای مدیران قابل مشاهده است و طبق مهلت "
    "نگهداری تنظیم‌شده از فضای خصوصی سرور حذف می‌شود. لطفاً فایل خیلی حساس نفرستید."
)


def main_menu(is_admin: bool = False):
    rows = [
        [Button.inline("📎 ساخت جزوه", b"menu:create")],
        [
            Button.inline("📚 راهنما", b"menu:help"),
            Button.inline("🧰 قالب‌ها", b"menu:formats"),
        ],
        [Button.inline("🔐 حریم خصوصی", b"menu:privacy")],
        [
            Button.inline("💳 خرید اعتبار", b"billing:plans"),
            Button.inline("💰 موجودی", b"billing:balance"),
        ],
    ]
    if is_admin:
        rows.append([Button.inline("⚙️ پنل مدیریت", b"admin:home")])
    return rows


def back_menu(is_admin: bool = False):
    return [[Button.inline("↩️ بازگشت به منو", b"menu:home")]] + (
        [[Button.inline("⚙️ پنل مدیریت", b"admin:home")]] if is_admin else []
    )


def admin_menu():
    return [
        [
            Button.inline("📊 آمار", b"admin:stats"),
            Button.inline("👥 کاربران", b"admin:users"),
        ],
        [
            Button.inline("💳 پرداخت‌های در انتظار", b"admin:payments"),
            Button.inline("🔐 کلیدهای سرویس", b"admin:credentials"),
        ],
        [
            Button.inline("📜 گزارش مدیر", b"admin:audit"),
            Button.inline("📣 پیام همگانی", b"admin:broadcast"),
        ],
        [
            Button.inline("🚫 مسدودسازی", b"admin:ban"),
            Button.inline("✅ رفع مسدودیت", b"admin:unban"),
        ],
        [Button.inline("↩️ منوی اصلی", b"menu:home")],
    ]


def _error_reference(submission_id: int) -> str:
    return f"GMS-{submission_id:06d}"


def _cooldown_active(until: object) -> bool:
    if not until:
        return False
    try:
        deadline = (
            until
            if isinstance(until, datetime)
            else datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        )
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline > datetime.now(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return False


def _format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "نامشخص"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ساعت و {minutes} دقیقه"
    if minutes:
        return f"{minutes} دقیقه و {secs} ثانیه"
    return f"{secs} ثانیه"


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = MESSAGE_CHUNK_SIZE) -> list[str]:
    if limit <= 0:
        raise ValueError("Message limit must be positive")
    text = text.strip()
    pages: list[str] = []
    while _utf16_length(text) > limit:
        used = 0
        safe_end = 0
        for index, character in enumerate(text):
            width = 2 if ord(character) > 0xFFFF else 1
            if used + width > limit:
                break
            used += width
            safe_end = index + 1
        cut = text.rfind("\n\n", 0, safe_end)
        if cut < safe_end // 2:
            cut = text.rfind("\n", 0, safe_end)
        if cut < safe_end // 2:
            cut = text.rfind(" ", 0, safe_end)
        if cut < safe_end // 2:
            cut = safe_end
        # A zero-width cut would loop forever on the same text.
        cut = max(cut, 1)
        pages.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pages.append(text)
    return pages or [""]


def _plain_length(rendered: str) -> int:
    """Length Telegram counts for an HTML message: tags off, entities decoded."""
    return _utf16_length(html.unescape(TAG_PATTERN.sub("", rendered)))


def _split_rendered(rendered: str, budget: int) -> list[str]:
    """Split already rendered HTML on line boundaries, never inside a tag.

    ``markdown_to_telegram_html`` renders every source line independently, so a
    tag never spans a newline and cutting there keeps the markup valid.
    """
    pages: list[str] = []
    current: list[str] = []
    used = 0
    for line in rendered.split("\n"):
        length = _plain_length(line)
        if length > budget:
            if current:
                pages.append("\n".join(current))
                current, used = [], 0
            # One oversized line: drop its markup so it can be cut anywhere.
            plain = html.unescape(TAG_PATTERN.sub("", line))
            pages.extend(html.escape(page, quote=False) for page in split_message(plain, budget))
            continue
        if current and used + length + 1 > budget:
            pages.append("\n".join(current))
            current, used = [line], length
            continue
        current.append(line)
        used += length + 1
    if current:
        pages.append("\n".join(current))
    return [page for page in pages if page.strip()]


def render_pages(text: str, reserve: int = 64) -> list[str]:
    """Render Markdown once, then paginate the HTML for Telegram.

    Rendering before splitting matters: a table cut in half would otherwise
    lose its header and the next page's first data row would be mistaken for
    one. Paginating the rendered text also measures what Telegram counts,
    because the renderer repeats table headers and can grow the text well past
    the length of its Markdown source.
    """
    budget = max(MIN_CHUNK_SIZE, TELEGRAM_TEXT_LIMIT - reserve)
    return _split_rendered(markdown_to_telegram_html(text), budget) or [""]


def _emphasis_is_nested(value: str) -> bool:
    """True when every <b>/<i> tag closes in the reverse order it opened."""
    stack: list[str] = []
    for tag in re.findall(r"</?[bi]>", value):
        if not tag.startswith("</"):
            stack.append(tag[1])
        elif not stack or stack.pop() != tag[2]:
            return False
    return not stack


def _parse_user_id(value: str) -> int | None:
    """A Telegram user id typed by an admin (ASCII or Persian digits), else None."""
    value = value.strip().lstrip("+")
    # isdecimal() rejects characters such as "²" that isdigit() accepts but int() cannot read.
    if not value.isdecimal() or len(value) > 15:
        return None
    return int(value)


def markdown_to_telegram_html(text: str) -> str:
    """Convert the small Markdown subset used by the LLM to Telegram-safe HTML."""
    # NUL is not valid Telegram text and must not impersonate our code placeholders.
    lines = text.replace("\x00", "").splitlines()
    result: list[str] = []
    table_header: list[str] | None = None
    table_rows = 0
    in_code = False

    def inline(value: str) -> str:
        value = html.escape(value, quote=False)
        # Stash inline code first so its contents are not treated as Markdown.
        stashed: list[str] = []

        def stash(match: re.Match) -> str:
            stashed.append(match.group(1))
            return f"\x00{len(stashed) - 1}\x00"

        value = re.sub(r"`([^`]+)`", stash, value)
        plain = value
        value = re.sub(r"\*\*(?!\s)(.+?)(?<!\s)\*\*", r"<b>\1</b>", value)
        # Emphasis markers must hug their text and sit outside a word, so
        # "2 * 3 * 4" and identifiers such as @a_b_c survive untouched.
        value = re.sub(r"(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", value)
        value = re.sub(r"(?<![\w_])_(?!\s)([^_]+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", value)
        if not _emphasis_is_nested(value):
            # Crossed markers such as "**a *b** c*" would yield <b><i></b></i>,
            # which Telegram rejects as a whole ("can't parse entities").
            # Showing the markers literally beats losing the message.
            value = plain
        return re.sub(
            r"\x00(\d+)\x00",
            lambda match: f"<code>{stashed[int(match.group(1))]}</code>",
            value,
        )

    def strong(value: str) -> str:
        """Wrap already-rendered inline HTML in <b> without nesting a bold tag.

        Telegram rejects the *whole* message when the same formatting tag
        appears inside itself ("<b><b>x</b></b>"), so a heading or table header
        that is itself bold ("# **title**") must not gain a second <b>.
        """
        if "<b>" in value or "</b>" in value:
            return value
        return f"<b>{value}</b>"

    def strong_label(value: str, suffix: str = ":") -> str:
        """``strong(value)`` keeping ``suffix`` inside the bold run when possible."""
        if "<b>" in value or "</b>" in value:
            return value + suffix
        return f"<b>{value}{suffix}</b>"

    def flush_table() -> None:
        """Emit a table header that never received a data row."""
        nonlocal table_header, table_rows
        if table_header is not None and not table_rows:
            cells = [inline(cell) for cell in table_header if cell.strip()]
            if cells:
                result.append(" · ".join(strong(cell) for cell in cells))
        table_header = None
        table_rows = 0

    for line in lines:
        stripped = line.strip()
        in_table = not in_code and stripped.startswith("|") and "|" in stripped[1:]
        if not in_table:
            flush_table()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            # Code lines are escaped verbatim; Markdown does not apply inside fences.
            result.append(html.escape(line, quote=False))
            continue
        if in_table:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            # Markdown only requires one dash per separator cell (|--|-:|).
            if cells and all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            if table_header is None:
                table_header = cells
                table_rows = 0
                continue
            # A row may carry more cells than the header (malformed tables are
            # common in generated Markdown). Extra cells keep their value so no
            # content is ever dropped, and an empty header label is omitted
            # instead of rendering a stray colon.
            pairs = []
            for index, cell in enumerate(cells):
                if not cell:
                    continue
                label = table_header[index].strip() if index < len(table_header) else ""
                pairs.append(
                    f"{strong_label(inline(label))} {inline(cell)}" if label else inline(cell)
                )
            result.append(" · ".join(pairs) if pairs else inline(" | ".join(cells)))
            table_rows += 1
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if heading:
            result.append(strong(inline(heading.group(1).strip())))
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            item = re.sub(r"^\s*[-*+]\s+", "", line)
            result.append("• " + inline(item))
            continue
        if re.match(r"^\s*\d+[.)]\s+", line):
            result.append(inline(line.strip()))
            continue
        result.append(inline(line))
    flush_table()
    return "\n".join(result).strip()


def _media_metadata(message) -> tuple[str | None, str | None, str | None, float | None, str | None]:
    """Classify an incoming message as ('audio' | 'video' | None, name, mime, duration, id)."""
    file_obj = message.file
    filename = getattr(file_obj, "name", None)
    mime_type = getattr(file_obj, "mime_type", None)
    extension = Path(filename).suffix.lower() if filename else ""
    normalized_mime = (mime_type or "").lower()

    kind: str | None = None
    if message.voice or message.audio:
        kind = "audio"
    elif getattr(message, "gif", None):
        # Animated GIFs are silent video/mp4; treating them as lectures is noise.
        kind = None
    elif getattr(message, "video_note", None) or getattr(message, "video", None):
        kind = "video"
    elif message.document is not None:
        if normalized_mime.startswith("audio/"):
            kind = "audio"
        elif normalized_mime.startswith("video/"):
            kind = "video"
        elif extension in AUDIO_EXTENSIONS:
            kind = "audio"
        elif extension in VIDEO_EXTENSIONS:
            kind = "video"
    if kind is None:
        return None, None, None, None, None

    duration = getattr(file_obj, "duration", None)
    if duration is None and message.voice:
        duration = getattr(message.voice, "duration", None)
    file_id = str(getattr(message.document, "id", None) or message.id)
    return kind, filename, mime_type, float(duration) if duration else None, file_id


def _is_unsupported_attachment(message) -> bool:
    """True for a real file the bot cannot use — never for link previews or text."""
    media = getattr(message, "media", None)
    if media is None or isinstance(media, MessageMediaWebPage):
        return False
    if getattr(message, "sticker", None) is not None or getattr(message, "gif", None) is not None:
        # Stickers and animations are conversational, not failed uploads.
        return False
    return getattr(message, "document", None) is not None or getattr(message, "photo", None) is not None


def _presentation_metadata(message) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (kind, filename, mime_type, file_id) for PowerPoint documents."""
    if message.document is None or message.voice or message.audio:
        return None, None, None, None
    file_obj = message.file
    filename = getattr(file_obj, "name", None)
    mime_type = getattr(file_obj, "mime_type", None)
    kind = classify_presentation(filename, mime_type)
    if kind is None:
        return None, None, None, None
    file_id = str(getattr(message.document, "id", None) or message.id)
    return kind, filename, mime_type, file_id


@dataclass(frozen=True)
class QueuedJob:
    """One accepted upload waiting for (or running in) a worker.

    ``run`` is a factory, not a coroutine, so a job can be queued, cancelled or
    rejected without ever being started.
    """

    submission_id: int
    kind: str
    run: Callable[[], Awaitable[None]]
    event: Any = None
    progress: Any = None
    is_admin: bool = False


class StudyBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.credential_manager = ProviderCredentialManager(self.db, settings)
        self._receipt_cleanup_task: asyncio.Task | None = None
        self._receipt_root: Path | None = None
        self._pending_credential_setup: dict[int, dict[str, str | None]] = {}
        # Telethon opens its SQLite session in the constructor, before start().
        settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(settings.session_path), settings.telegram_api_id, settings.telegram_api_hash,
            proxy=settings.telegram_proxy,
        )
        self.client.parse_mode = None
        self._tasks: set[asyncio.Task] = set()
        # Second, defensive bound on *active* jobs: the worker pool already
        # limits concurrency, and this keeps the same guarantee for a job that
        # is ever started outside a worker.
        self._job_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        # Bounded pending work: a semaphore alone only limits *active* jobs,
        # so a burst of uploads could otherwise pile up thousands of waiting
        # tasks in memory. ``MAX_PENDING_JOBS`` bounds the queue itself and a
        # fixed pool of workers drains it; anything beyond the capacity is
        # rejected with back-pressure instead of being accepted and forgotten.
        self._queue: asyncio.Queue[QueuedJob] | None = None
        self._workers: list[asyncio.Task] = []
        self._pending_admin_actions: dict[int, str] = {}

    # -- job queue ---------------------------------------------------------

    def _job_queue(self) -> asyncio.Queue:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.settings.max_pending_jobs)
        return self._queue

    def _ensure_workers(self) -> None:
        """Start the fixed worker pool once (idempotent)."""
        if self._workers:
            return
        count = max(1, self.settings.max_concurrent_jobs)
        for index in range(count):
            self._workers.append(
                asyncio.create_task(self._worker(index), name=f"job-worker-{index}")
            )
        logger.info(
            "Job workers started workers=%s max_pending_jobs=%s",
            count,
            self.settings.max_pending_jobs,
        )

    async def _worker(self, index: int) -> None:
        """Run queued jobs until cancelled; one job at a time."""
        queue = self._job_queue()
        while True:
            job = await queue.get()
            try:
                await job.run()
            except asyncio.CancelledError:
                # Shutdown: the job coroutine records its own state.
                raise
            except Exception:
                # A worker must never die because one job misbehaved.
                logger.exception(
                    "Unhandled job failure submission_id=%s worker=%s",
                    job.submission_id,
                    index,
                )
            finally:
                queue.task_done()

    def _enqueue(self, job: QueuedJob) -> bool:
        """Accept a job into the bounded queue; False when it is full."""
        self._ensure_workers()
        try:
            self._job_queue().put_nowait(job)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "Job queue is full submission_id=%s capacity=%s; rejecting with back-pressure",
                job.submission_id,
                self.settings.max_pending_jobs,
            )
            return False

    async def _reject_job(self, job: QueuedJob, reason: str) -> None:
        """Fail a job that will never run (queue overflow or shutdown)."""
        reference = _error_reference(job.submission_id)
        try:
            await self.db.set_submission_status(job.submission_id, "failed", reason)
        except Exception:
            logger.exception(
                "Could not store rejected job state reference=%s", reference
            )
        if job.progress is not None:
            try:
                await job.progress.fail(reference)
            except Exception:
                logger.debug("Progress report failed for rejected job", exc_info=True)
            try:
                await job.progress.aclose()
            except Exception:
                logger.debug("Closing progress failed for rejected job", exc_info=True)
        if job.event is not None:
            try:
                await job.event.reply(
                    f"{reason}\n\nکد پیگیری: {reference}",
                    buttons=main_menu(job.is_admin),
                )
            except Exception:
                logger.exception(
                    "Could not notify the user about a rejected job reference=%s",
                    reference,
                )

    async def _drain_queue(self, reason: str) -> None:
        """Reject everything still waiting when the bot shuts down."""
        if self._queue is None:
            return
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self._reject_job(job, reason)
            finally:
                self._queue.task_done()

    def _track_task(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "Background task escaped its error task=%s",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    async def start(self) -> None:
        self.settings.validate_runtime()
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self._clean_stale_workdirs()
        await self.db.open()
        await self.db.sync_plan_catalog(
            [plan.as_record() for plan in plan_catalog()]
        )
        self._ensure_secure_receipt_directory()
        await self._cleanup_expired_receipts()
        self._register_handlers()
        await self.client.start(bot_token=self.settings.telegram_bot_token)
        self._receipt_cleanup_task = asyncio.create_task(
            self._receipt_cleanup_loop(), name="receipt-retention-cleanup"
        )
        me = await self.client.get_me()
        logger.info(
            "Persian study assistant is online username=@%s note_provider=%s stt_primary=%s "
            "max_concurrent_jobs=%s",
            me.username,
            self.settings.note_api_provider,
            self.settings.stt_primary,
            self.settings.max_concurrent_jobs,
        )
        try:
            summary = await check_media_worker()
            logger.info("Media worker ready %s", summary)
        except MediaToolError as exc:
            # Non-fatal, like the old binary checks: media jobs will fail with
            # an explicit error until the Python dependencies are repaired.
            logger.warning("Media worker self-check failed: %s", exc)

    def _ensure_secure_receipt_directory(self) -> Path:
        path = self.settings.receipt_dir.resolve()
        web_roots = {"public_html", "www", "htdocs", "httpdocs"}
        if any(part.lower() in web_roots for part in path.parts):
            raise ValueError("RECEIPT_DIR must be outside the web document root.")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise ValueError("RECEIPT_DIR permissions could not be restricted to owner-only.") from exc
        self._receipt_root = path
        return path

    async def _cleanup_expired_receipts(self) -> None:
        root = getattr(self, "_receipt_root", None) or self._ensure_secure_receipt_directory()
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.settings.receipt_retention_days
        )
        candidates = await self.db.receipt_cleanup_candidates(
            cutoff.isoformat(timespec="seconds")
        )
        for item in candidates:
            payment_id = int(item["id"])
            raw_path = str(item["receipt_path"])
            path = Path(raw_path).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                # Never delete a path outside the private receipt directory.
                logger.error("Receipt path escaped private storage payment_id=%s", payment_id)
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Expired receipt could not be deleted payment_id=%s", payment_id)
                continue
            await self.db.mark_receipt_deleted(payment_id, raw_path)

    async def _receipt_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(24 * 60 * 60)
            try:
                await self._cleanup_expired_receipts()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Receipt-retention cleanup failed")

    def _clean_stale_workdirs(self) -> None:
        """Delete job folders left behind by a crash or a hard restart.

        No job can be running at startup, so anything matching the job prefixes
        is garbage that would otherwise sit in ``data/tmp`` forever.
        """
        removed = 0
        for prefix in ("submission-", "deck-"):
            for leftover in self.settings.temp_dir.glob(f"{prefix}*"):
                if not leftover.is_dir():
                    continue
                shutil.rmtree(leftover, ignore_errors=True)
                if not leftover.exists():
                    removed += 1
        if removed:
            logger.info("Removed stale temporary job folders count=%s", removed)

    async def run(self) -> None:
        try:
            await self.start()
            self._ensure_workers()
            await self.client.run_until_disconnected()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        # Order matters: stop accepting work first, then stop the workers, so a
        # job is either fully recorded as failed or fully processed — never
        # silently dropped with a "processing" row left behind.
        if self._receipt_cleanup_task is not None:
            self._receipt_cleanup_task.cancel()
            await asyncio.gather(self._receipt_cleanup_task, return_exceptions=True)
            self._receipt_cleanup_task = None
        await self._drain_queue("ربات خاموش شد و این کار از صف خارج شد")
        if self._tasks:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers = []
        if self.client.is_connected():
            await self.client.disconnect()
        await self.db.close()

    def _register_handlers(self) -> None:
        @self.client.on(events.CallbackQuery())
        async def handle_callback(event):
            try:
                await self._handle_callback(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled callback query failure")
                try:
                    await event.answer("خطایی رخ داد؛ دوباره تلاش کنید.", alert=True)
                except Exception:
                    logger.exception("Could not answer the failed callback")

        @self.client.on(events.NewMessage(incoming=True))
        async def handle_message(event):
            try:
                await self._handle_message(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled incoming message failure")
                try:
                    await event.reply("یک مشکل پیش آمد. لطفاً چند لحظه دیگر دوباره امتحان کنید.")
                except Exception:
                    logger.exception("Could not notify the user about a handler error")

    async def _edit_callback(self, event, text: str, buttons=None) -> None:
        try:
            await event.answer()
        except Exception:
            logger.debug("Callback answer failed", exc_info=True)
        try:
            await event.edit(text, buttons=buttons, parse_mode=None)
        except MessageNotModifiedError:
            return
        except Exception:
            # Editing can fail when the originating message is old. A fresh
            # response keeps the menu usable without losing the button flow.
            logger.debug("Callback edit failed; sending a new message", exc_info=True)
            await event.respond(text, buttons=buttons, parse_mode=None)

    async def _billing_balance_text(self, user_id: int) -> str:
        balance = await self.db.user_balance(user_id)
        lines = [
            "موجودی اعتبار ⏱️",
            f"قابل استفاده: {format_duration(balance['available_seconds'])}",
        ]
        if balance["entitlements"]:
            lines.append("\nاعتبارهای فعال:")
            for entitlement in balance["entitlements"]:
                expiry = (
                    "بدون انقضا"
                    if not entitlement["expires_at"]
                    else f"تا {str(entitlement['expires_at'])[:10]}"
                )
                lines.append(
                    f"• {entitlement['plan_name']}: "
                    f"{format_duration(entitlement['remaining_seconds'])} ({expiry})"
                )
        else:
            lines.append("اعتبار فعالی ندارید؛ از دکمهٔ خرید اعتبار برای انتخاب طرح استفاده کنید.")
        return "\n".join(lines)

    async def _billing_plans(self) -> tuple[str, list]:
        plans = await self.db.list_plans(paid_only=True)
        if not plans:
            return "در حال حاضر طرح پرداختی فعالی نیست.", [[Button.inline("↩️ منوی اصلی", b"menu:home")]]
        lines = ["طرح‌های اعتبار پیش‌پرداختی 💳"]
        buttons = []
        for plan in plans:
            lines.append(
                f"• {plan['name']} — {format_duration(plan['included_seconds'])}، "
                f"{format_toman(plan['price_toman'])}، {plan['validity_days']} روز"
            )
            buttons.append(
                [
                    Button.inline(
                        f"خرید {plan['name']} — {format_toman(plan['price_toman'])}",
                        f"billing:buy:{plan['code']}",
                    )
                ]
            )
        buttons.append([Button.inline("↩️ منوی اصلی", b"menu:home")])
        return "\n".join(lines), buttons

    async def _start_payment(self, event, user_id: int, plan_code: str) -> None:
        try:
            request = await self.db.create_payment_request(user_id, plan_code)
        except ValueError:
            await event.reply("این طرح پرداختی در دسترس نیست.")
            return
        if request["status"] == "pending":
            text = (
                f"درخواست پرداخت شمارهٔ {request['id']} برای «{request['plan_name']}» "
                "ثبت شده و منتظر بررسی دستی مدیر است. تا آن زمان درخواست دیگری ثبت نمی‌شود."
            )
            await event.reply(text, buttons=main_menu())
            return
        text = (
            f"طرح انتخاب‌شده: {request['plan_name']}\n"
            f"زمان: {format_duration(request['included_seconds'])}\n"
            f"مبلغ: {format_toman(request['amount_toman'])}\n\n"
            "مبلغ را کارت‌به‌کارت کنید و تصویر رسید را در همین گفت‌وگوی خصوصی بفرستید. "
            "ارسال رسید به‌تنهایی اعتبار ایجاد نمی‌کند؛ اعتبار فقط پس از تأیید دستی مدیر افزوده می‌شود.\n\n"
            f"بانک: {self.settings.payment_bank_name}\n"
            f"شماره کارت: {self.settings.payment_card_display}\n"
            f"به نام: {self.settings.payment_card_holder}\n\n"
            f"شماره درخواست: {request['id']}"
        )
        await event.reply(
            text,
            buttons=[
                [Button.inline("لغو درخواست", f"billing:cancel:{request['id']}")],
                [Button.inline("بازگشت", b"billing:plans")],
            ],
        )

    def _safe_receipt_path(self, raw_path: str | None) -> Path | None:
        if not raw_path:
            return None
        root = self._receipt_root or self.settings.receipt_dir.resolve()
        path = Path(raw_path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return None
        return path

    async def _handle_receipt_upload(self, event, user: dict, payment: dict) -> None:
        if not getattr(event, "is_private", True):
            await event.reply("برای امنیت، رسید را فقط در گفت‌وگوی خصوصی ربات بفرستید.")
            return
        message = event.message
        file_obj = getattr(message, "file", None)
        mime = (getattr(file_obj, "mime_type", None) or "").lower()
        is_photo = getattr(message, "photo", None) is not None
        extension_by_mime = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        if is_photo and not mime:
            mime = "image/jpeg"
        extension = extension_by_mime.get(mime)
        if extension is None:
            await event.reply("رسید باید تصویر JPEG، PNG یا WebP باشد.")
            return
        size = getattr(file_obj, "size", None)
        if size is not None and size > self.settings.max_receipt_size_bytes:
            await event.reply("حجم تصویر رسید از سقف مجاز بیشتر است.")
            return

        root = self._receipt_root or self._ensure_secure_receipt_directory()
        path = root / f"receipt-{secrets.token_hex(16)}{extension}"
        try:
            path.touch(mode=0o600, exist_ok=False)
            downloaded = await message.download_media(file=str(path))
            if not downloaded or not path.is_file():
                raise OSError("download failed")
            path.chmod(0o600)
            if path.stat().st_size > self.settings.max_receipt_size_bytes:
                raise ValueError("رسید از سقف حجم مجاز بیشتر است.")
            with path.open("rb") as stream:
                signature = stream.read(16)
            valid_signature = (
                (mime == "image/jpeg" and signature.startswith(b"\xff\xd8\xff"))
                or (mime == "image/png" and signature.startswith(b"\x89PNG\r\n\x1a\n"))
                or (mime == "image/webp" and signature[:4] == b"RIFF" and signature[8:12] == b"WEBP")
            )
            if not valid_signature:
                raise ValueError("محتوای فایل با قالب تصویر اعلام‌شده هماهنگ نیست.")
            accepted = await self.db.submit_payment_receipt(
                int(payment["id"]),
                int(user["id"]),
                str(path),
                int(message.id),
            )
            if not accepted:
                raise ValueError("درخواست پرداخت دیگر منتظر رسید نیست؛ /balance را بررسی کنید.")
        except Exception as exc:
            path.unlink(missing_ok=True)
            if isinstance(exc, ValueError):
                await event.reply(str(exc))
            else:
                logger.warning("Private receipt upload failed payment_id=%s", payment["id"])
                await event.reply("ذخیرهٔ امن رسید انجام نشد؛ لطفاً بعداً دوباره تلاش کنید.")
            return

        self._pending_admin_actions.pop(int(user["telegram_id"]), None)
        await event.reply(
            "تصویر رسید به‌صورت خصوصی دریافت شد و برای بررسی دستی مدیر ارسال می‌شود. "
            "تا پیش از تأیید، اعتباری افزوده نشده است.",
            buttons=main_menu(int(user["telegram_id"]) in self.settings.admin_ids),
        )
        await self._notify_admins_of_payment(int(payment["id"]), path)

    async def _notify_admins_of_payment(self, payment_id: int, receipt_path: Path) -> None:
        detail = await self.db.payment_detail(payment_id)
        if not detail:
            return
        caption = (
            f"رسید پرداخت در انتظار بررسی دستی — درخواست {payment_id}\n"
            f"کاربر: {detail['telegram_id']}"
            + (f" (@{detail['username']})" if detail["username"] else "")
            + f"\nطرح: {detail['plan_name']}\nمبلغ: {format_toman(detail['amount_toman'])}"
        )
        buttons = [
            [
                Button.inline("تأیید و افزودن اعتبار", f"admin:payment:approve:{payment_id}"),
                Button.inline("رد رسید", f"admin:payment:reject:{payment_id}"),
            ]
        ]
        for admin_id in self.settings.admin_ids:
            try:
                await self.client.send_file(
                    admin_id,
                    str(receipt_path),
                    caption=caption,
                    force_document=True,
                    buttons=buttons,
                    parse_mode=None,
                )
            except Exception:
                # Never include a receipt path, card data, or receipt contents in logs.
                logger.warning("Private receipt notification failed payment_id=%s admin_id=%s", payment_id, admin_id)

    async def _show_pending_payments(self, event) -> None:
        payments = await self.db.list_pending_payments(limit=20)
        if not payments:
            await self._edit_callback(
                event,
                "پرداختِ منتظر بررسی وجود ندارد.",
                [[Button.inline("↩️ پنل مدیریت", b"admin:home")]],
            )
            return
        await event.answer(f"{len(payments)} پرداخت در انتظار بررسی")
        await self._send_pending_payment_rows(event, payments)

    async def _send_pending_payment_rows(self, event, payments: list[dict]) -> None:
        for payment in payments:
            detail = await self.db.payment_detail(int(payment["id"]))
            if not detail:
                continue
            caption = (
                f"پرداخت در انتظار بررسی دستی — درخواست {detail['id']}\n"
                f"کاربر: {detail['telegram_id']}"
                + (f" (@{detail['username']})" if detail["username"] else "")
                + f"\nطرح: {detail['plan_name']}\nمبلغ: {format_toman(detail['amount_toman'])}"
            )
            buttons = [
                [
                    Button.inline("تأیید و افزودن اعتبار", f"admin:payment:approve:{detail['id']}"),
                    Button.inline("رد رسید", f"admin:payment:reject:{detail['id']}"),
                ]
            ]
            receipt = self._safe_receipt_path(detail.get("receipt_path"))
            if receipt is not None and receipt.is_file():
                await event.respond(
                    caption,
                    file=str(receipt),
                    force_document=True,
                    buttons=buttons,
                    parse_mode=None,
                )
            else:
                await event.respond(caption + "\nتصویر رسید در فضای خصوصی در دسترس نیست.", buttons=buttons)

    async def _payment_callback(self, event, admin_id: int, action: str, payment_id: int) -> None:
        detail = await self.db.payment_detail(payment_id)
        if not detail:
            await event.answer("درخواست پرداخت پیدا نشد.", alert=True)
            return
        if action == "receipt":
            receipt = self._safe_receipt_path(detail.get("receipt_path"))
            if receipt is None or not receipt.is_file():
                await event.answer("رسید در فضای خصوصی در دسترس نیست.", alert=True)
                return
            await event.answer()
            await event.respond(
                f"رسید خصوصی درخواست {payment_id} — {detail['telegram_id']}",
                file=str(receipt),
                force_document=True,
                parse_mode=None,
            )
            return
        if action == "approve":
            approved = await self.db.approve_payment(payment_id, admin_id)
            if approved is None:
                await event.answer("این درخواست قبلاً بررسی شده یا دیگر منتظر تأیید نیست.", alert=True)
                return
            try:
                await self.client.send_message(
                    int(approved["user_telegram_id"]),
                    f"پرداخت شما تأیید شد؛ {format_duration(approved['granted_seconds'])} "
                    f"اعتبار برای {approved['plan_name']} افزوده شد. از /balance موجودی را ببینید.",
                    parse_mode=None,
                )
            except Exception:
                logger.warning("Payment approval notification failed payment_id=%s", payment_id)
            await self._cleanup_expired_receipts()
            await self._edit_callback(
                event,
                f"پرداخت {payment_id} تأیید شد و اعتبار به‌صورت اتمیک افزوده شد.",
                [[Button.inline("پرداخت‌های در انتظار", b"admin:payments")]],
            )
            return
        if action == "reject":
            if detail["status"] != "pending":
                await event.answer("این درخواست قبلاً بررسی شده است.", alert=True)
                return
            self._pending_admin_actions[admin_id] = f"payment_reject:{payment_id}"
            await self._edit_callback(
                event,
                "دلیل رد رسید را بفرستید؛ هیچ اعتباری افزوده نمی‌شود. برای انصراف لغو را بزنید.",
                [[Button.inline("لغو", b"admin:payments")]],
            )
            return
        await event.answer("عملیات پرداخت معتبر نیست.", alert=True)

    async def _show_admin_audit(self, event) -> None:
        rows = await self.db.admin_audit(limit=30)
        usage = await self.db.admin_usage_ledger(limit=30)
        if not rows and not usage:
            text = "هنوز رویداد مدیریتی یا حسابداری ثبت نشده است."
        else:
            lines = []
            if rows:
                lines.append("آخرین رویدادهای مدیریتی:")
                for row in rows:
                    details = row.get("details") or {}
                    summary = ", ".join(f"{key}={value}" for key, value in details.items())
                    lines.append(
                        f"• {row['created_at']} | مدیر {row['admin_telegram_id']} | "
                        f"{row['action']} | {row.get('target_type')} {row.get('target_id') or ''}"
                        + (f" | {summary}" if summary else "")
                    )
            if usage:
                lines.extend(["", "آخرین رویدادهای ریزحساب اعتبار (ثانیهٔ صحیح):"])
                for row in usage:
                    quantities = []
                    event_type = str(row["event_type"])
                    if event_type == "grant" and int(row.get("reserved_seconds") or 0):
                        quantities.append(
                            f"افزوده {format_duration(int(row['reserved_seconds']))}"
                        )
                    else:
                        for label, key in (
                            ("درخواست", "requested_seconds"),
                            ("رزرو", "reserved_seconds"),
                            ("مصرف", "consumed_seconds"),
                            ("آزادسازی", "released_seconds"),
                        ):
                            value = row.get(key)
                            if value is not None and int(value) != 0:
                                quantities.append(f"{label} {format_duration(int(value))}")
                    available = row.get("available_seconds")
                    if available is not None:
                        quantities.append(f"موجود هنگام ثبت {format_duration(int(available))}")
                    details = "، ".join(quantities) or "بدون مقدار"
                    submission = (
                        f" | ارسال {_error_reference(int(row['submission_id']))}"
                        if row.get("submission_id") is not None
                        else ""
                    )
                    admin = (
                        f" | مدیر {row['admin_telegram_id']}"
                        if row.get("admin_telegram_id") is not None
                        else ""
                    )
                    reason = f" | {row['reason']}" if row.get("reason") else ""
                    lines.append(
                        f"• {row['created_at']} | کاربر {row['telegram_id']} | "
                        f"{row['event_type']} | {details}{submission}{admin}{reason}"
                    )
            text = "\n".join(lines)
        answer = getattr(event, "answer", None)
        if callable(answer):
            await answer()
        await self._send_long_message(event, text, is_admin=True)

    @staticmethod
    def _masked_key(secret: str | None) -> str:
        return "تنظیم نشده" if not secret else "••••••••" + secret[-4:]

    async def _show_provider_credentials(self, event) -> None:
        summaries = await self.credential_manager.list_summaries()
        lines = [
            "کلیدها و آخرین پاسخ providerها 🔐",
            "این صفحه فقط آخرین پاسخ ثبت‌شده را نشان می‌دهد؛ health check زنده اجرا نمی‌شود.",
            "Master key رمزگذاری: "
            + ("تنظیم شده" if self.credential_manager.encryption_configured else "تنظیم نشده"),
            "\nکلیدهای محیطی:",
        ]
        environment = [
            ("stt", "speechmatics", self.settings.speechmatics_api_key),
            ("stt", "deepgram", self.settings.deepgram_api_key),
            ("stt", "openai_compatible", self.settings.stt_openai_api_key),
            ("notes", self.settings.note_api_provider, self.settings.effective_note_api_key),
        ]
        health = self.credential_manager.environment_health()
        for service, provider, secret in environment:
            if not secret:
                continue
            state = health.get((service, provider), {})
            last = f"؛ آخرین HTTP {state['last_status_code']}" if state.get("last_status_code") else ""
            cool = "؛ در cooldown" if _cooldown_active(state.get("cooldown_until")) else ""
            quarantine = "؛ قرنطینه" if state.get("quarantined_at") else ""
            lines.append(
                f"• {service}/{provider}: {self._masked_key(secret)}{last}{cool}{quarantine}"
            )
        buttons = []
        if summaries:
            lines.append("\nکلیدهای رمزگذاری‌شده در پایگاه‌داده:")
            for item in summaries:
                state = "غیرفعال" if not item["enabled"] else "فعال"
                if item["enabled"] and item["quarantined_at"]:
                    state = "قرنطینه (401/403)"
                elif item["enabled"] and _cooldown_active(item["cooldown_until"]):
                    state = f"cooldown تا {str(item['cooldown_until'])[:19]}"
                elif item["enabled"] and item["cooldown_until"]:
                    state = "فعال؛ cooldown پایان‌یافته"
                last = f"؛ HTTP {item['last_status_code']}" if item["last_status_code"] else ""
                lines.append(
                    f"• #{item['id']} {item['service']}/{item['provider']} — {item['label']} "
                    f"{self._masked_key(item['secret_last4'])}{last}؛ {state}"
                )
                toggle = "enable" if not item["enabled"] else "disable"
                buttons.append(
                    [
                        Button.inline(
                            f"{('فعال‌سازی' if toggle == 'enable' else 'غیرفعال‌سازی')} #{item['id']}",
                            f"admin:credential:{toggle}:{item['id']}",
                        ),
                        Button.inline("حذف", f"admin:credential:delete:{item['id']}"),
                    ]
                )
        buttons.extend(
            [
                [
                    Button.inline("افزودن کلید STT", b"admin:credential:add:stt"),
                    Button.inline("افزودن کلید جزوه", b"admin:credential:add:notes"),
                ],
                [Button.inline("↩️ پنل مدیریت", b"admin:home")],
            ]
        )
        await self._edit_callback(event, "\n".join(lines), buttons)

    async def _begin_credential_add(self, event, admin_id: int, service: str) -> None:
        if not self.credential_manager.encryption_configured:
            await self._edit_callback(
                event,
                "ابتدا PROVIDER_CREDENTIALS_ENCRYPTION_KEY را در environment سرور تنظیم کنید؛ "
                "بدون آن کلید جدید ذخیره نمی‌شود.",
                [[Button.inline("↩️ پنل مدیریت", b"admin:credentials")]],
            )
            return
        providers = ", ".join(sorted(PROVIDER_CHOICES[service]))
        self._pending_admin_actions[admin_id] = f"credential_meta:{service}"
        self._pending_credential_setup.pop(admin_id, None)
        await self._edit_callback(
            event,
            "ابتدا metadata را با قالب زیر بفرستید (با | جدا شود):\n"
            f"provider | label | base_url اختیاری | model اختیاری\n"
            f"providerهای مجاز: {providers}\n\n"
            "بعداً کلید را جداگانه می‌فرستید؛ پیام کلید پس از دریافت حذف می‌شود. "
            "فقط در گفت‌وگوی خصوصی ادامه دهید.",
            [[Button.inline("لغو", b"admin:credentials")]],
        )

    async def _handle_callback(self, event) -> None:
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        telegram_id = int(sender.id)
        user = await self.db.upsert_user(telegram_id, getattr(sender, "username", None))
        is_admin = telegram_id in self.settings.admin_ids
        data = bytes(event.data or b"").decode("utf-8", "replace")

        if user["is_banned"] and not is_admin:
            await event.answer("دسترسی شما به ربات محدود شده است.", alert=True)
            return
        if data.startswith("admin:") and not getattr(event, "is_private", True):
            await event.answer("لطفاً پنل مدیریت را در گفت‌وگوی خصوصی ربات باز کنید.", alert=True)
            return
        if data.startswith("menu:") and getattr(event, "is_private", True):
            self._pending_admin_actions.pop(telegram_id, None)
        if data == "menu:home":
            self._pending_admin_actions.pop(telegram_id, None)
            await self._edit_callback(event, WELCOME_TEXT, main_menu(is_admin))
            return
        if data == "menu:create":
            await self._edit_callback(
                event,
                "فایلتان را بفرستید 📎\n\n"
                "صوت، ویدیو یا PowerPoint فرقی ندارد؛ بعد از دریافت فایل، پیشرفت کار را "
                "مرحله‌به‌مرحله می‌بینید.",
                back_menu(is_admin),
            )
            return
        if data == "menu:help":
            await self._edit_callback(event, HELP_TEXT, back_menu(is_admin))
            return
        if data == "menu:formats":
            await self._edit_callback(event, FORMATS_TEXT, back_menu(is_admin))
            return
        if data == "menu:privacy":
            await self._edit_callback(event, PRIVACY_TEXT, back_menu(is_admin))
            return
        if data.startswith("billing:"):
            if not getattr(event, "is_private", True):
                await event.answer("اطلاعات پرداخت و موجودی فقط در گفت‌وگوی خصوصی نمایش داده می‌شود.", alert=True)
                return
            if data == "billing:balance":
                await self._edit_callback(
                    event,
                    await self._billing_balance_text(int(user["id"])),
                    [[Button.inline("💳 خرید اعتبار", b"billing:plans")], [Button.inline("↩️ منو", b"menu:home")]],
                )
                return
            if data == "billing:plans":
                text, buttons = await self._billing_plans()
                await self._edit_callback(event, text, buttons)
                return
            if data.startswith("billing:buy:"):
                await self._start_payment(event, int(user["id"]), data.rsplit(":", 1)[-1])
                return
            if data.startswith("billing:cancel:"):
                try:
                    payment_id = int(data.rsplit(":", 1)[-1])
                except ValueError:
                    await event.answer("درخواست پرداخت معتبر نیست.", alert=True)
                    return
                cancelled = await self.db.cancel_payment_intent(payment_id, int(user["id"]))
                await self._edit_callback(
                    event,
                    "درخواست پرداخت لغو شد." if cancelled else "این درخواست قابل لغو نیست یا قبلاً رسید آن ثبت شده است.",
                    [[Button.inline("طرح‌ها", b"billing:plans"), Button.inline("منو", b"menu:home")]],
                )
                return
            await event.answer("این دکمهٔ پرداخت معتبر نیست.", alert=True)
            return

        if not data.startswith("admin:"):
            await event.answer("این دکمه معتبر نیست.", alert=True)
            return
        if not is_admin:
            await event.answer("این بخش فقط برای مدیر ربات است.", alert=True)
            return
        if data == "admin:payments":
            await self._show_pending_payments(event)
            return
        if data.startswith("admin:payment:"):
            parts = data.split(":")
            if len(parts) != 4:
                await event.answer("دکمهٔ پرداخت معتبر نیست.", alert=True)
                return
            try:
                payment_id = int(parts[3])
            except ValueError:
                await event.answer("شمارهٔ درخواست معتبر نیست.", alert=True)
                return
            await self._payment_callback(event, telegram_id, parts[2], payment_id)
            return
        if data == "admin:audit":
            await self._show_admin_audit(event)
            return
        if data == "admin:credentials":
            await self._show_provider_credentials(event)
            return
        if data.startswith("admin:credential:add:"):
            service = data.rsplit(":", 1)[-1]
            if service not in PROVIDER_CHOICES:
                await event.answer("سرویس معتبر نیست.", alert=True)
                return
            await self._begin_credential_add(event, telegram_id, service)
            return
        if data.startswith("admin:credential:"):
            parts = data.split(":")
            if len(parts) != 4 or parts[2] not in {"enable", "disable", "delete"}:
                await event.answer("عملیات کلید معتبر نیست.", alert=True)
                return
            try:
                credential_id = int(parts[3])
            except ValueError:
                await event.answer("شناسهٔ کلید معتبر نیست.", alert=True)
                return
            if parts[2] == "enable":
                changed = await self.credential_manager.enable(credential_id, telegram_id)
            elif parts[2] == "disable":
                changed = await self.credential_manager.disable(credential_id, telegram_id)
            else:
                changed = await self.credential_manager.delete(credential_id, telegram_id)
            await self._edit_callback(
                event,
                "تغییر ذخیره شد." if changed else "کلید پیدا نشد یا تغییر نکرد.",
                [[Button.inline("بازگشت به کلیدها", b"admin:credentials")]],
            )
            return
        if data == "admin:home":
            self._pending_admin_actions.pop(telegram_id, None)
            await self._edit_callback(event, "پنل مدیریت ربات ⚙️", admin_menu())
            return
        if data == "admin:stats":
            stats = await self.db.stats()
            text = self._stats_text(stats)
            await self._edit_callback(
                event, text, [[Button.inline("↩️ پنل مدیریت", b"admin:home")]]
            )
            return
        if data == "admin:users":
            users = await self.db.user_summaries(limit=50)
            text = self._users_text(users)
            # The list can exceed Telegram's callback edit limit, so send it as
            # paginated messages and keep the current menu intact.
            await event.answer("فهرست کاربران ارسال شد.")
            await self._send_long_message(event, text, is_admin=True)
            return
        action_prompts = {
            "admin:broadcast": ("broadcast", "پیامی را که می‌خواهید برای همه برود بفرستید."),
            "admin:ban": ("ban", "شناسهٔ عددی کاربر را بفرستید."),
            "admin:unban": ("unban", "شناسهٔ عددی کاربر را بفرستید."),
        }
        if data in action_prompts:
            action, prompt = action_prompts[data]
            self._pending_admin_actions[telegram_id] = action
            await self._edit_callback(
                event,
                prompt + "\n\nبرای انصراف دکمهٔ زیر را بزنید.",
                [[Button.inline("لغو", b"admin:home")]],
            )
            return
        await event.answer("این دکمه معتبر نیست.", alert=True)

    async def _handle_pending_admin_input(
        self, event, telegram_id: int, action: str, text: str
    ) -> None:
        if action.startswith("payment_reject:"):
            reason = "".join(character for character in text if character.isprintable()).strip()[:500]
            if not reason:
                await event.reply("دلیل رد نمی‌تواند خالی باشد؛ دوباره بفرستید.")
                return
            try:
                payment_id = int(action.split(":", 1)[1])
                changed = await self.db.reject_payment(payment_id, telegram_id, reason)
            except (ValueError, TypeError):
                changed = False
                payment_id = -1
            self._pending_admin_actions.pop(telegram_id, None)
            if not changed:
                await event.reply("این درخواست پیدا نشد یا قبلاً بررسی شده است.", buttons=admin_menu())
                return
            detail = await self.db.payment_detail(payment_id)
            if detail:
                try:
                    await self.client.send_message(
                        int(detail["telegram_id"]),
                        f"رسید درخواست پرداخت {payment_id} تأیید نشد؛ هیچ اعتباری افزوده نشده است. "
                        f"دلیل: {reason}",
                        parse_mode=None,
                    )
                except Exception:
                    logger.warning("Payment rejection notification failed payment_id=%s", payment_id)
            await self._cleanup_expired_receipts()
            await event.reply("رسید رد شد؛ اعتبار افزوده نشد و رویداد ثبت شد.", buttons=admin_menu())
            return

        if action.startswith("credential_meta:"):
            service = action.split(":", 1)[1]
            parts = [part.strip() for part in text.split("|")]
            if len(parts) < 2 or len(parts) > 4 or not parts[0] or not parts[1]:
                await event.reply("قالب نادرست است؛ provider | label | base_url اختیاری | model اختیاری را بفرستید.")
                return
            provider = parts[0].lower()
            if service not in PROVIDER_CHOICES or provider not in PROVIDER_CHOICES[service]:
                await event.reply("provider برای این سرویس پشتیبانی نمی‌شود؛ metadata را دوباره بفرستید.")
                return
            base_url = parts[2] if len(parts) >= 3 and parts[2] not in {"-", "—"} else None
            model = parts[3] if len(parts) >= 4 and parts[3] not in {"-", "—"} else None
            self._pending_credential_setup[telegram_id] = {
                "service": service,
                "provider": provider,
                "label": parts[1],
                "base_url": base_url,
                "model": model,
            }
            self._pending_admin_actions[telegram_id] = "credential_secret"
            await event.reply(
                "حالا کلید API را به‌تنهایی بفرستید. پیام آن پیش از ذخیره حذف می‌شود؛ "
                "اگر حذف پیام ممکن نباشد، کلید ذخیره نخواهد شد."
            )
            return

        if action == "credential_secret":
            secret = text.strip()
            metadata = self._pending_credential_setup.get(telegram_id)
            if not metadata or not secret:
                await event.reply("تنظیم کلید ناقص است؛ دوباره از پنل کلیدها شروع کنید.")
                self._pending_admin_actions.pop(telegram_id, None)
                self._pending_credential_setup.pop(telegram_id, None)
                return
            try:
                delete_message = getattr(event.message, "delete", None)
                if not callable(delete_message):
                    raise RuntimeError("message deletion unavailable")
                await delete_message()
            except Exception:
                self._pending_admin_actions.pop(telegram_id, None)
                self._pending_credential_setup.pop(telegram_id, None)
                await event.respond(
                    "پیام کلید حذف نشد؛ کلید ذخیره نشد. آن را دستی از گفت‌وگو پاک و دوباره تلاش کنید.",
                    buttons=admin_menu(),
                )
                return
            self._pending_admin_actions.pop(telegram_id, None)
            self._pending_credential_setup.pop(telegram_id, None)
            try:
                credential_id = await self.credential_manager.add_credential(
                    service=str(metadata["service"]),
                    provider=str(metadata["provider"]),
                    label=str(metadata["label"]),
                    secret=secret,
                    admin_id=telegram_id,
                    base_url=metadata.get("base_url"),
                    model=metadata.get("model"),
                )
            except CredentialStoreError as exc:
                await event.respond(str(exc), buttons=admin_menu())
            except Exception as exc:
                logger.warning(
                    "Provider credential save failed service=%s provider=%s error_type=%s",
                    metadata.get("service"), metadata.get("provider"), type(exc).__name__,
                )
                await event.respond("ذخیرهٔ امن کلید انجام نشد؛ تنظیمات و نام برچسب را بررسی کنید.", buttons=admin_menu())
            else:
                await event.respond(
                    f"کلید #{credential_id} با رمزگذاری ذخیره شد؛ فقط چهار رقم پایانی در پنل نمایش داده می‌شود.",
                    buttons=admin_menu(),
                )
            finally:
                secret = ""
            return

        if action == "broadcast":
            if not text:
                await event.reply("پیام خالی است؛ لطفاً متن را دوباره بفرستید.")
                return
            self._pending_admin_actions.pop(telegram_id, None)
            status = await event.reply("دارم پیام را برای کاربران می‌فرستم…")
            sent = await self._broadcast(telegram_id, text)
            result = f"انجام شد؛ پیام به {sent} کاربر رسید."
            if status is not None and callable(getattr(status, "edit", None)):
                await status.edit(result, buttons=admin_menu())
            else:
                await event.respond(result, buttons=admin_menu())
            return

        target_id = _parse_user_id(text)
        if target_id is None:
            await event.reply("شناسه باید عددی باشد؛ لطفاً دوباره بفرستید.")
            return
        if target_id in self.settings.admin_ids:
            await event.reply("نمی‌توانید دسترسی مدیر ربات را تغییر دهید.")
            return
        self._pending_admin_actions.pop(telegram_id, None)
        changed = await self.db.set_banned(
            target_id, action == "ban", admin_id=telegram_id
        )
        if not changed:
            result = "این شناسه در فهرست کاربران ربات پیدا نشد."
        else:
            result = "کاربر مسدود شد." if action == "ban" else "مسدودیت کاربر برداشته شد."
        await event.reply(result, buttons=admin_menu())

    async def _handle_message(self, event) -> None:
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        telegram_id = int(sender.id)
        user = await self.db.upsert_user(telegram_id, getattr(sender, "username", None))
        is_admin = telegram_id in self.settings.admin_ids
        text = (event.raw_text or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""

        if user["is_banned"] and not is_admin:
            await event.reply("دسترسی شما به ربات محدود شده است.")
            return
        pending_action = (
            self._pending_admin_actions.get(telegram_id)
            if getattr(event, "is_private", True) else None
        )
        if is_admin and pending_action and (
            pending_action.startswith("credential_")
            or pending_action.startswith("credential_meta:")
            or pending_action.startswith("payment_reject:")
        ):
            if command == "/cancel":
                self._pending_admin_actions.pop(telegram_id, None)
                self._pending_credential_setup.pop(telegram_id, None)
                await event.reply("عملیات لغو شد.", buttons=admin_menu())
                return
            if getattr(event.message, "media", None) is not None:
                self._pending_admin_actions.pop(telegram_id, None)
                self._pending_credential_setup.pop(telegram_id, None)
                await event.reply("ورودی رسانه‌ای پذیرفته نشد؛ عملیات مدیریتی لغو شد.")
            elif text and not command:
                await self._handle_pending_admin_input(
                    event, telegram_id, pending_action, text
                )
                return
            elif command:
                # Never mistake a bot command for a provider key or rejection
                # reason. Drop the pending prompt before handling the command.
                self._pending_admin_actions.pop(telegram_id, None)
                self._pending_credential_setup.pop(telegram_id, None)
        if command == "/start":
            self._pending_admin_actions.pop(telegram_id, None)
            self._pending_credential_setup.pop(telegram_id, None)
            await event.reply(WELCOME_TEXT, buttons=main_menu(is_admin))
            return
        if command in {"/help", "/cancel"}:
            self._pending_admin_actions.pop(telegram_id, None)
            self._pending_credential_setup.pop(telegram_id, None)
            await event.reply(HELP_TEXT, buttons=back_menu(is_admin))
            return
        if command in {"/balance", "/buy", "/history", "/cancelpayment"}:
            if not getattr(event, "is_private", True):
                await event.reply("موجودی و پرداخت را فقط در گفت‌وگوی خصوصی ربات بررسی کنید.")
                return
            if command == "/balance":
                await event.reply(await self._billing_balance_text(int(user["id"])), buttons=main_menu(is_admin))
            elif command == "/buy":
                plan_text, plan_buttons = await self._billing_plans()
                await event.reply(plan_text, buttons=plan_buttons)
            elif command == "/history":
                history = await self.db.payment_history(int(user["id"]))
                statuses = {
                    "awaiting_receipt": "منتظر رسید",
                    "pending": "منتظر بررسی دستی",
                    "approved": "تأیید شد",
                    "rejected": "رد شد",
                    "cancelled": "لغو شد",
                }
                lines = ["سوابق پرداخت:"]
                lines.extend(
                    f"• #{item['id']} {item['plan_name']} — {format_toman(item['amount_toman'])} — "
                    f"{statuses.get(item['status'], item['status'])}"
                    for item in history
                )
                await event.reply("\n".join(lines) if history else "سابقهٔ پرداختی ندارید.", buttons=main_menu(is_admin))
            else:
                payment = await self.db.get_awaiting_payment(int(user["id"]))
                cancelled = bool(payment and await self.db.cancel_payment_intent(int(payment["id"]), int(user["id"])))
                await event.reply(
                    "درخواستِ منتظر رسید لغو شد." if cancelled else "درخواستِ منتظر رسیدی برای لغو ندارید.",
                    buttons=main_menu(is_admin),
                )
            return
        if command in {"/users", "/stats", "/broadcast", "/ban", "/unban", "/payments", "/credit", "/audit"}:
            if not is_admin:
                await event.reply("این دستور فقط برای مدیر ربات فعال است.")
                return
            if not getattr(event, "is_private", True):
                await event.reply("دستورهای مدیریت فقط در گفت‌وگوی خصوصی ربات قابل استفاده‌اند.")
                return
            self._pending_admin_actions.pop(telegram_id, None)
            await self._handle_admin_command(event, command, text)
            return
        pending_action = (
            self._pending_admin_actions.get(telegram_id)
            if getattr(event, "is_private", True) else None
        )
        # A captioned upload is a file, not an answer to the admin prompt: the
        # caption must never be broadcast (or read as a user id) while the file
        # itself is silently dropped.
        if is_admin and pending_action and getattr(event.message, "media", None) is not None:
            self._pending_admin_actions.pop(telegram_id, None)
            pending_action = None
        if is_admin and pending_action and text and not command:
            await self._handle_pending_admin_input(
                event, telegram_id, pending_action, text
            )
            return

        if getattr(event, "is_private", True) and (
            getattr(event.message, "photo", None) is not None
            or getattr(event.message, "document", None) is not None
        ):
            awaiting = await self.db.get_awaiting_payment(int(user["id"]))
            if awaiting:
                await self._handle_receipt_upload(event, user, awaiting)
                return

        media_kind, filename, mime_type, duration, file_id = _media_metadata(event.message)
        if media_kind:
            await self._accept_media(
                event, user, media_kind, filename, mime_type, duration, file_id
            )
            return

        deck_kind, deck_name, deck_mime, deck_file_id = _presentation_metadata(event.message)
        if deck_kind:
            await self._accept_presentation(
                event, user, deck_kind, deck_name, deck_mime, deck_file_id
            )
            return

        if _is_unsupported_attachment(event.message):
            await event.reply(UNSUPPORTED_FILE_MESSAGE, buttons=main_menu(is_admin))

    async def _billable_media_seconds(
        self, path: Path, duration_hint: float | None = None
    ) -> int:
        duration = None
        try:
            info = await probe_media(path, self.settings)
            duration = info.duration if info is not None else None
        except MediaToolError:
            duration = None
        if duration is None:
            duration = duration_hint
        try:
            duration_value = float(duration)
        except (TypeError, ValueError, OverflowError):
            duration_value = 0.0
        if not math.isfinite(duration_value) or duration_value <= 0:
            raise ValueError(
                "مدت واقعی صوت برای محاسبهٔ اعتبار مشخص نشد؛ فایل بدون کسر اعتبار متوقف شد."
            )
        return max(1, math.ceil(duration_value))

    async def _reserve_submission_usage(
        self,
        submission_id: int,
        user_id: int | None,
        audio_path: Path,
        duration_hint: float | None,
    ) -> int | None:
        if user_id is None:
            # Backwards-compatible direct worker invocation; production queue
            # closures always pass the registered user id.
            user_id = await self.db.submission_user_id(submission_id)
        seconds = await self._billable_media_seconds(audio_path, duration_hint)
        reservation = await self.db.reserve_usage(user_id, submission_id, seconds)
        if not reservation.get("ok"):
            raise InsufficientBalanceError(
                int(reservation.get("available_seconds", 0)), seconds
            )
        return seconds

    async def _release_submission_usage(self, submission_id: int, reason: str) -> bool:
        try:
            await self.db.release_usage(submission_id, reason)
            return True
        except Exception:
            logger.exception("Could not release usage reservation submission_id=%s", submission_id)
            return False

    async def _accept_media(
        self, event, user, kind: str, filename, mime_type, duration, file_id
    ) -> None:
        file_obj = event.message.file
        size = getattr(file_obj, "size", None)
        if size is not None and size > self.settings.max_file_size:
            await event.reply("این فایل از سقف حجم ربات بزرگ‌تر است.")
            logger.warning(
                "Rejected oversized %s from user %s: %s bytes", kind, user["telegram_id"], size
            )
            return
        submission_id = await self.db.create_submission(
            user["id"], file_id, duration, filename, mime_type, source_type=kind
        )
        progress = await JobProgress.create(
            event,
            submission_id=submission_id,
            stage="🎬 ویدیو رسید و در صف است" if kind == "video" else "🎧 فایل صوتی رسید و در صف است",
            animate=self.settings.progress_animation,
        )
        job = QueuedJob(
            submission_id=submission_id,
            kind=kind,
            run=lambda: self._process_submission(
                event,
                submission_id,
                filename,
                kind,
                progress=progress,
                is_admin=int(user["telegram_id"]) in self.settings.admin_ids,
                user_id=int(user["id"]),
                duration_hint=duration,
            ),
            event=event,
            progress=progress,
            is_admin=int(user["telegram_id"]) in self.settings.admin_ids,
        )
        if not self._enqueue(job):
            await self._reject_job(
                job,
                "ربات هم‌اکنون چند کار دیگر را انجام می‌دهد و ظرفیت صف پر است؛ "
                "چند دقیقه دیگر دوباره این فایل را بفرستید.",
            )

    async def _accept_presentation(
        self, event, user, kind: str, filename, mime_type, file_id
    ) -> None:
        if not self.settings.presentation_enabled:
            await event.reply("فعلاً امکان ساخت جزوه از PowerPoint فعال نیست.")
            return
        if kind == "unsupported":
            await event.reply(UNSUPPORTED_PRESENTATION_MESSAGE)
            logger.info("Rejected unsupported presentation format filename=%s", filename)
            return
        if kind == "legacy" and not self.settings.presentation_legacy_enabled:
            await event.reply(
                "این فایل PowerPoint قدیمی است. لطفاً آن را با پسوند pptx ذخیره و دوباره ارسال کنید."
            )
            return
        size = getattr(event.message.file, "size", None)
        if size is not None and size > self.settings.max_file_size:
            await event.reply("این فایل از سقف حجم ربات بزرگ‌تر است.")
            logger.warning(
                "Rejected oversized deck from user %s: %s bytes", user["telegram_id"], size
            )
            return
        submission_id = await self.db.create_submission(
            user["id"], file_id, None, filename, mime_type, source_type="pptx"
        )
        progress = await JobProgress.create(
            event,
            submission_id=submission_id,
            stage="📊 PowerPoint رسید و در صف است",
            animate=self.settings.progress_animation,
        )
        job = QueuedJob(
            submission_id=submission_id,
            kind="pptx",
            run=lambda: self._process_presentation(
                event,
                submission_id,
                filename,
                kind,
                progress=progress,
                is_admin=int(user["telegram_id"]) in self.settings.admin_ids,
                user_id=int(user["id"]),
            ),
            event=event,
            progress=progress,
            is_admin=int(user["telegram_id"]) in self.settings.admin_ids,
        )
        if not self._enqueue(job):
            await self._reject_job(
                job,
                "ربات هم‌اکنون چند کار دیگر را انجام می‌دهد و ظرفیت صف پر است؛ "
                "چند دقیقه دیگر دوباره این فایل را بفرستید.",
            )

    async def _process_presentation(
        self,
        event,
        submission_id: int,
        filename: str | None,
        kind: str,
        *,
        progress: JobProgress | None = None,
        is_admin: bool = False,
        user_id: int | None = None,
    ) -> None:
        workdir: Path | None = None
        reservation_active = False
        billable_seconds: int | None = None
        progress = progress or JobProgress(event, submission_id=submission_id)
        log_token = log_job_id.set(_error_reference(submission_id))
        try:
            async with self._job_semaphore:
                await progress.update(5, "🚀 شروع کردم")
                await self.db.set_submission_status(submission_id, "processing")
                self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
                workdir = Path(
                    tempfile.mkdtemp(prefix=f"deck-{submission_id}-", dir=self.settings.temp_dir)
                )
                suffix = Path(filename).suffix.lower() if filename else (
                    ".pptx" if kind == "native" else ".ppt"
                )
                if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
                    suffix = ".pptx" if kind == "native" else ".ppt"
                deck_path = workdir / f"deck{suffix}"
                await progress.update(10, "📥 دارم فایل را از تلگرام می‌گیرم…")
                downloaded = await event.message.download_media(file=str(deck_path))
                if not downloaded or not deck_path.exists():
                    raise RuntimeError("دانلود فایل از تلگرام ناموفق بود.")
                if deck_path.stat().st_size > self.settings.max_file_size:
                    raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")
                await progress.update(25, "🔍 فایل رسید؛ دارم بررسی‌اش می‌کنم")

                if kind == "legacy":
                    await progress.update(
                        32,
                        "♻️ این فایل قدیمی است و اول به pptx تبدیل می‌شود",
                    )
                    deck_path = await convert_to_pptx(
                        deck_path, workdir / "converted", self.settings
                    )

                await progress.update(40, "🗂️ دارم متن و صدای اسلایدها را بیرون می‌کشم")
                content = await load_presentation(
                    deck_path, workdir / "media", self.settings
                )
                await progress.update(50, "🎚️ دارم صداهای اسلایدها را آماده می‌کنم")
                deck_skips: list[str] = []
                prepared = await prepare_audio(
                    content, workdir, self.settings, skipped_out=deck_skips
                )
                await self._store_presentation_details(submission_id, content, prepared)

                slide_note = f"{content.slide_count} اسلاید" if content.slide_count else "بدون متن اسلاید"
                outline = slides_outline(content.slides)
                if prepared is None:
                    if not outline.strip():
                        raise ValueError(
                            "داخل این PowerPoint متن یا صدای قابل استفاده‌ای پیدا نکردم."
                        )
                    await progress.update(
                        70,
                        "📝 صدایی در این ارائه پیدا نشد؛ جزوه را از متن اسلایدها می‌سازم",
                        slide_note,
                    )
                    transcript_text = ""
                    engine = "slides-only"
                else:
                    await progress.update(
                        60,
                        "🎙️ صداها آماده شدند؛ دارم آن‌ها را به متن تبدیل می‌کنم",
                        f"{slide_note}، {len(prepared.clips)} فایل صوتی، "
                        f"مجموعاً {_format_duration(prepared.total_duration)}",
                    )
                    if prepared.path.stat().st_size > self.settings.max_file_size:
                        raise ValueError("صدای این PowerPoint از سقف حجم ربات بزرگ‌تر است.")
                    billable_seconds = await self._reserve_submission_usage(
                        submission_id, user_id, prepared.path, prepared.total_duration
                    )
                    reservation_active = billable_seconds is not None
                    try:
                        result = await transcribe(
                            prepared.path, self.settings, credentials=self.credential_manager
                        )
                    except BaseException:
                        if reservation_active and await self._release_submission_usage(
                            submission_id, "Transcription failed or was cancelled"
                        ):
                            reservation_active = False
                        raise
                    await progress.update(80, "✍️ متن صدا آماده شد")
                    transcript_text = result.text
                    engine = result.engine

                await progress.update(85, "📚 دارم مطالب را به شکل جزوه مرتب می‌کنم")
                notes: StructuredNotes | None = None
                try:
                    with use_provider_credentials(self.credential_manager):
                        notes = await structure_presentation(
                            outline, transcript_text, self.settings, mode=self.settings.note_mode
                        )
                    notice = ""
                except Exception as exc:
                    if isinstance(exc, StructuringError):
                        logger.warning(
                            "Note structuring unavailable provider=%s; returning raw material: %s",
                            self.settings.note_api_provider,
                            exc,
                        )
                    else:
                        logger.exception("Unexpected note structuring failure; returning raw material")
                    notice = (
                        "\n\nنکته: این بار نتوانستم متن را به شکل جزوهٔ ساختارمند دربیاورم؛ "
                        "همان مطالب خام را در قالب فایل فرستادم."
                    )
                # Without a prepared track the reasons live on the deck itself;
                # either way the user should learn what was left out.
                skipped = prepared.skipped if prepared is not None else tuple(deck_skips)
                if skipped:
                    notice += "\n\nموارد نادیده‌گرفته‌شده: " + "؛ ".join(skipped[:10])

                await progress.update(94, "📑 جزوه آماده است؛ دارم نتیجه را نهایی می‌کنم")
                await self.db.save_transcription(
                    submission_id,
                    engine,
                    transcript_text,
                    notes.to_json() if notes is not None else self._fallback_presentation_text(outline, transcript_text),
                )
                await progress.update(97, "📤 دارم جزوه را می‌فرستم")
                await self._deliver_result_documents(
                    event,
                    workdir,
                    notes=notes,
                    plain_text=self._fallback_presentation_text(outline, transcript_text),
                    plain_title="جزوهٔ PowerPoint",
                    reference=_error_reference(submission_id),
                    source_name=filename,
                    engine=None if engine == "slides-only" else engine,
                    raw_sections=(
                        [("متن اسلایدها", outline)] if outline.strip() else []
                    )
                    + (
                        [("متن پیاده‌سازی‌شدهٔ صدای ارائه", transcript_text)]
                        if transcript_text.strip()
                        else []
                    ),
                    notice=notice,
                    is_admin=is_admin,
                )
                if reservation_active:
                    if billable_seconds is None:
                        raise RuntimeError("Usage reservation has no billable duration")
                    finalized = await self.db.finalize_usage(
                        submission_id, billable_seconds
                    )
                    if not finalized:
                        raise RuntimeError("Usage reservation was not finalized")
                    reservation_active = False
                await self.db.set_submission_status(submission_id, "done")
                await progress.complete()
                logger.info(
                    "Presentation submission completed submission_id=%s engine=%s slides=%s",
                    submission_id,
                    engine,
                    content.slide_count,
                )
        except asyncio.CancelledError:
            if reservation_active and await self._release_submission_usage(
                submission_id, "Processing cancelled"
            ):
                reservation_active = False
            await self.db.set_submission_status(
                submission_id, "failed", "پردازش هنگام خاموش‌شدن متوقف شد"
            )
            raise
        except Exception as exc:
            if reservation_active and await self._release_submission_usage(
                submission_id, "Processing failed before transcript was accepted"
            ):
                reservation_active = False
            reference = _error_reference(submission_id)
            logger.exception(
                "Presentation submission failed submission_id=%s reference=%s",
                submission_id,
                reference,
            )
            try:
                await self.db.set_submission_status(submission_id, "failed", str(exc)[:1000])
            except Exception:
                logger.exception("Could not store submission failure reference=%s", reference)
            try:
                await progress.fail(reference)
                message = (
                    str(exc)
                    if isinstance(exc, USER_VISIBLE_ERRORS)
                    else "متأسفم، نتوانستم این PowerPoint را پردازش کنم. لطفاً فایل را بررسی و دوباره ارسال کنید."
                )
                await event.reply(
                    f"{message}\n\nکد پیگیری: {reference}",
                    buttons=main_menu(is_admin),
                )
            except Exception:
                logger.exception(
                    "Could not notify the user about processing failure reference=%s",
                    reference,
                )
        finally:
            if reservation_active and await self._release_submission_usage(
                submission_id, "Final processing cleanup"
            ):
                reservation_active = False
            await progress.aclose()
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)
            log_job_id.reset(log_token)

    async def _store_presentation_details(self, submission_id, content, prepared) -> None:
        used = {clip.part_name: clip for clip in (prepared.clips if prepared else ())}
        rows = [
            {
                "slide_number": clip.slide_number,
                "part_name": clip.part_name,
                "kind": clip.kind,
                "duration": (used[clip.part_name].duration if clip.part_name in used else None),
                "included": clip.part_name in used,
                "skip_reason": None if clip.part_name in used else "در ادغام صدا استفاده نشد",
            }
            for clip in content.clips
        ]
        try:
            await self.db.save_presentation_details(
                submission_id,
                content.slide_count,
                rows,
                prepared.total_duration if prepared else None,
            )
        except Exception:
            logger.exception("Could not store presentation details for %s", submission_id)

    @staticmethod
    def _fallback_presentation_text(outline: str, transcript: str) -> str:
        parts = []
        if outline.strip():
            parts.append("## متن اسلایدها\n\n" + outline.strip())
        if transcript.strip():
            parts.append("## متن پیاده‌سازی‌شدهٔ صدای ارائه\n\n" + transcript.strip())
        return "\n\n".join(parts)

    async def _process_submission(
        self,
        event,
        submission_id: int,
        filename: str | None,
        kind: str = "audio",
        *,
        progress: JobProgress | None = None,
        is_admin: bool = False,
        user_id: int | None = None,
        duration_hint: float | None = None,
    ) -> None:
        workdir: Path | None = None
        reservation_active = False
        billable_seconds: int | None = None
        progress = progress or JobProgress(event, submission_id=submission_id)
        log_token = log_job_id.set(_error_reference(submission_id))
        try:
            async with self._job_semaphore:
                await progress.update(5, "🚀 شروع کردم")
                await self.db.set_submission_status(submission_id, "processing")
                self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
                workdir = Path(
                    tempfile.mkdtemp(
                        prefix=f"submission-{submission_id}-", dir=self.settings.temp_dir
                    )
                )
                suffix = Path(filename).suffix.lower() if filename else (
                    ".mp4" if kind == "video" else ".ogg"
                )
                if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
                    suffix = ".media"
                source_path = workdir / f"source{suffix}"
                await progress.update(10, "📥 دارم فایل را از تلگرام می‌گیرم…")
                downloaded = await event.message.download_media(file=str(source_path))
                if not downloaded or not source_path.exists():
                    raise RuntimeError("دانلود فایل از تلگرام ناموفق بود.")
                if source_path.stat().st_size > self.settings.max_file_size:
                    raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")
                await progress.update(30, "🔍 فایل رسید؛ دارم صدا را بررسی می‌کنم")

                audio_path = await self._ensure_transcribable(
                    event, source_path, workdir, filename, kind, progress=progress
                )
                if audio_path.stat().st_size > self.settings.max_file_size:
                    raise ValueError("صدای آماده‌شده از سقف حجم ربات بزرگ‌تر است.")

                billable_seconds = await self._reserve_submission_usage(
                    submission_id, user_id, audio_path, duration_hint
                )
                reservation_active = billable_seconds is not None
                await progress.update(55, "🎙️ دارم صدا را به متن تبدیل می‌کنم")
                try:
                    result = await transcribe(
                        audio_path, self.settings, credentials=self.credential_manager
                    )
                except BaseException:
                    if reservation_active and await self._release_submission_usage(
                        submission_id, "Transcription failed or was cancelled"
                    ):
                        reservation_active = False
                    raise
                await progress.update(80, "✍️ متن آماده شد؛ دارم آن را به شکل جزوه مرتب می‌کنم")
                notes: StructuredNotes | None = None
                try:
                    with use_provider_credentials(self.credential_manager):
                        notes = await structure_transcript(
                            result.text, self.settings, mode=self.settings.note_mode
                        )
                except Exception as exc:
                    if isinstance(exc, StructuringError):
                        logger.warning(
                            "Note structuring unavailable provider=%s; returning raw transcript: %s",
                            self.settings.note_api_provider,
                            exc,
                        )
                    else:
                        logger.exception("Unexpected note structuring failure; returning raw transcript")
                    notice = (
                        "\n\nنکته: این بار نتوانستم متن را به شکل جزوهٔ ساختارمند دربیاورم؛ "
                        "همان متن خام را در قالب فایل فرستادم."
                    )
                else:
                    notice = ""
                await progress.update(94, "📑 جزوه آماده است؛ دارم نتیجه را نهایی می‌کنم")
                await self.db.save_transcription(
                    submission_id,
                    result.engine,
                    result.text,
                    notes.to_json() if notes is not None else "## متن پیاده‌سازی‌شده\n\n" + result.text,
                )
                await progress.update(97, "📤 دارم جزوه را می‌فرستم")
                await self._deliver_result_documents(
                    event,
                    workdir,
                    notes=notes,
                    plain_text="## متن پیاده‌سازی‌شده\n\n" + result.text,
                    plain_title="جزوهٔ کلاس",
                    reference=_error_reference(submission_id),
                    source_name=filename,
                    engine=result.engine,
                    raw_sections=[("متن پیاده‌سازی‌شده", result.text)],
                    notice=notice,
                    is_admin=is_admin,
                )
                if reservation_active:
                    if billable_seconds is None:
                        raise RuntimeError("Usage reservation has no billable duration")
                    finalized = await self.db.finalize_usage(
                        submission_id, billable_seconds
                    )
                    if not finalized:
                        raise RuntimeError("Usage reservation was not finalized")
                    reservation_active = False
                await self.db.set_submission_status(submission_id, "done")
                await progress.complete()
                logger.info(
                    "Media submission completed submission_id=%s kind=%s engine=%s",
                    submission_id,
                    kind,
                    result.engine,
                )
        except asyncio.CancelledError:
            if reservation_active and await self._release_submission_usage(
                submission_id, "Processing cancelled"
            ):
                reservation_active = False
            await self.db.set_submission_status(submission_id, "failed", "پردازش هنگام خاموش‌شدن متوقف شد")
            raise
        except Exception as exc:
            if reservation_active and await self._release_submission_usage(
                submission_id, "Processing failed before transcript was accepted"
            ):
                reservation_active = False
            reference = _error_reference(submission_id)
            logger.exception(
                "Media submission failed submission_id=%s reference=%s",
                submission_id,
                reference,
            )
            try:
                await self.db.set_submission_status(submission_id, "failed", str(exc)[:1000])
            except Exception:
                logger.exception("Could not store submission failure reference=%s", reference)
            try:
                await progress.fail(reference)
                message = (
                    str(exc)
                    if isinstance(exc, USER_VISIBLE_ERRORS)
                    else "متأسفم، نتوانستم این فایل را پردازش کنم. لطفاً فایل را بررسی و دوباره ارسال کنید."
                )
                await event.reply(
                    f"{message}\n\nکد پیگیری: {reference}",
                    buttons=main_menu(is_admin),
                )
            except Exception:
                logger.exception(
                    "Could not notify the user about processing failure reference=%s",
                    reference,
                )
        finally:
            if reservation_active and await self._release_submission_usage(
                submission_id, "Final processing cleanup"
            ):
                reservation_active = False
            await progress.aclose()
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)
            log_job_id.reset(log_token)

    async def _ensure_transcribable(
        self,
        event,
        source_path: Path,
        workdir: Path,
        filename: str | None,
        kind: str,
        *,
        progress: JobProgress | None = None,
    ) -> Path:
        """Return a file the STT providers accept, transcoding only when needed."""
        progress = progress or JobProgress(event)
        info = None
        try:
            info = await probe_media(source_path, self.settings)
        except MediaToolError as exc:
            logger.warning("Probing the upload failed: %s", exc)
        if info is not None and not info.has_audio:
            raise ValueError(
                "داخل این فایل صدایی پیدا نکردم. لطفاً نسخه‌ای را بفرستید که صدا داشته باشد."
            )
        if kind == "video":
            await progress.update(40, "🎚️ دارم صدای ویدیو را جدا می‌کنم…")
            return await extract_audio_track(
                source_path,
                workdir,
                self.settings,
                total_duration=info.duration if info else None,
                stem="extracted-audio",
            )
        # The stored copy carries the resolved suffix, so unnamed uploads such
        # as Telegram voice notes (.ogg/opus) are no longer re-encoded blindly.
        if needs_transcode(filename or source_path.name, info):
            await progress.update(40, "🎚️ دارم فایل صوتی را برای تبدیل آماده می‌کنم…")
            return await extract_audio_track(
                source_path,
                workdir,
                self.settings,
                total_duration=info.duration if info else None,
                stem="normalised-audio",
            )
        return source_path

    async def _send_long_message(self, event, text: str, *, is_admin: bool = False) -> None:
        pages = render_pages(text)
        for index, rendered in enumerate(pages, start=1):
            if index > 1:
                await asyncio.sleep(1.05)
            if len(pages) > 1:
                rendered = f"بخش {index} از {len(pages)}\n\n" + rendered
            buttons = main_menu(is_admin) if index == len(pages) else None
            try:
                await event.respond(rendered, parse_mode="html", buttons=buttons)
            except FloodWaitError as exc:
                logger.warning(
                    "Telegram flood wait while delivering result seconds=%s page=%s/%s",
                    exc.seconds,
                    index,
                    len(pages),
                )
                await asyncio.sleep(exc.seconds + 1)
                await event.respond(rendered, parse_mode="html", buttons=buttons)

    async def _send_document(
        self, event, path: Path, caption: str, *, buttons=None
    ) -> None:
        """Send one file as a Telegram document, surviving flood waits."""
        try:
            await event.reply(caption, file=str(path), force_document=True, buttons=buttons)
        except FloodWaitError as exc:
            logger.warning(
                "Telegram flood wait while sending document seconds=%s file=%s",
                exc.seconds,
                path.name,
            )
            await asyncio.sleep(exc.seconds + 1)
            await event.reply(caption, file=str(path), force_document=True, buttons=buttons)

    async def _deliver_result_documents(
        self,
        event,
        workdir: Path,
        *,
        notes: StructuredNotes | None,
        plain_text: str,
        plain_title: str,
        reference: str,
        source_name: str | None,
        engine: str | None,
        raw_sections: list[tuple[str, str]],
        notice: str,
        is_admin: bool = False,
    ) -> None:
        """Deliver the polished RTL Word document plus the raw-text file.

        The Word document is the primary deliverable. When it cannot be
        generated (for example a broken python-docx installation) the notes
        fall back to an in-chat text message so no content is ever lost; the
        raw-text file is always sent.
        """
        meta = DocumentMeta(
            reference=reference,
            source_name=source_name,
            engine=engine,
            created_at=datetime.now(),
        )
        docx_path: Path | None = None
        docx_title = plain_title
        if notes is not None:
            docx_title = notes.display_title
            try:
                docx_bytes = await asyncio.to_thread(
                    build_notes_docx,
                    notes,
                    fonts=resolve_fonts(self.settings.docx_fonts),
                    design=resolve_design(self.settings.docx_design),
                    meta=meta,
                )
                docx_path = workdir / notes_docx_filename(notes, reference)
                docx_path.write_bytes(docx_bytes)
            except DocxPaginationError:
                raise
            except Exception:
                logger.exception(
                    "Word document generation failed; falling back to a chat message reference=%s",
                    reference,
                )
                docx_path = None
        else:
            try:
                docx_bytes = await asyncio.to_thread(
                    build_plain_docx,
                    plain_title,
                    plain_text,
                    fonts=resolve_fonts(self.settings.docx_fonts),
                    design=resolve_design(self.settings.docx_design),
                    meta=meta,
                )
                docx_path = workdir / plain_docx_filename(plain_title, reference)
                docx_path.write_bytes(docx_bytes)
            except DocxPaginationError:
                raise
            except Exception:
                logger.exception(
                    "Plain Word document generation failed; falling back to a chat message reference=%s",
                    reference,
                )
                docx_path = None

        if docx_path is not None:
            caption = f"📖 جزوهٔ «{docx_title}» — فایل Word" + notice
            # Telegram captions are capped at 1024 characters.
            caption = caption[:1000]
            await self._send_document(
                event, docx_path, caption, buttons=main_menu(is_admin)
            )
        else:
            body = (notes.to_markdown() if notes is not None else plain_text) + notice
            await self._send_long_message(event, body, is_admin=is_admin)

        txt_path = workdir / raw_text_filename(reference)
        txt_path.write_text(
            build_raw_text_document(
                title=f"متن خام — {docx_title}", sections=raw_sections, meta=meta
            ),
            encoding="utf-8",
        )
        await self._send_document(event, txt_path, "📄 متن خام پیاده‌سازی‌شده.")

    @staticmethod
    def _users_text(users: list[dict]) -> str:
        if not users:
            return "هنوز کاربری در ربات ثبت نشده است."
        lines = ["فهرست کاربران (حداکثر ۵۰ کاربر اخیر):"]
        for user in users:
            name = f"@{user['username']}" if user["username"] else "بدون نام کاربری"
            ban = " | مسدود" if user["is_banned"] else ""
            joined = str(user["first_seen"])[:10]
            lines.append(
                f"• {name} | شناسه: {user['telegram_id']} | عضویت: {joined} | "
                f"فایل‌ها: {user['submission_count']}{ban}"
            )
        return "\n".join(lines)

    @staticmethod
    def _stats_text(stats: dict) -> str:
        return (
            "آمار ربات 📊\n"
            f"کاربران ثبت‌شده: {stats['users']}\n"
            f"کاربران غیرمسدود: {stats['unbanned_users']}\n"
            f"کاربران دارای ارسال در ۳۰ روز اخیر: {stats['active_30d']}\n"
            f"کل فایل‌های دریافتی: {stats['submissions']}\n"
            f"فایل‌های تصویری: {stats['videos']}\n"
            f"فایل‌های ارائه (PowerPoint): {stats['presentations']}\n"
            f"کلیپ‌های صوتی استخراج‌شده از ارائه‌ها: {stats['presentation_clips']}\n"
            f"پردازش‌های موفق: {stats['done']}\n"
            f"پردازش‌های ناموفق: {stats['failed']}"
        )

    async def _handle_admin_command(self, event, command: str, text: str) -> None:
        admin_id = int((await event.get_sender()).id)
        if command == "/payments":
            payments = await self.db.list_pending_payments(limit=20)
            if not payments:
                await event.reply("پرداختِ منتظر بررسی وجود ندارد.", buttons=admin_menu())
            else:
                await event.reply(f"{len(payments)} پرداخت در انتظار بررسی است؛ رسیدها ارسال می‌شوند.")
                await self._send_pending_payment_rows(event, payments)
            return
        if command == "/audit":
            await self._show_admin_audit(event)
            return
        if command == "/credit":
            args = text.split(maxsplit=3)
            target_id = _parse_user_id(args[1]) if len(args) > 1 else None
            seconds = _parse_user_id(args[2]) if len(args) > 2 else None
            reason = args[3].strip() if len(args) > 3 else ""
            if target_id is None or seconds is None or not reason:
                await event.reply("روش استفاده: /credit شناسه_کاربر ثانیه دلیل")
                return
            if target_id in self.settings.admin_ids:
                await event.reply("برای ایمنی، اعتبار مدیریتی به مدیر دیگری افزوده نمی‌شود.")
                return
            try:
                credit = await self.db.add_admin_credit(target_id, seconds, admin_id, reason)
            except ValueError as exc:
                await event.reply(str(exc))
                return
            if credit is None:
                await event.reply("این کاربر در پایگاه‌داده پیدا نشد.")
                return
            await event.reply(
                f"{format_duration(credit['seconds'])} اعتبار به کاربر {target_id} افزوده شد و ثبت حسابرسی شد.",
                buttons=admin_menu(),
            )
            return
        if command == "/users":
            users = await self.db.user_summaries(limit=50)
            await self._send_long_message(event, self._users_text(users), is_admin=True)
            return
        if command == "/stats":
            stats = await self.db.stats()
            await event.reply(self._stats_text(stats), buttons=admin_menu())
            return
        if command in {"/ban", "/unban"}:
            args = text.split(maxsplit=1)
            target_id = _parse_user_id(args[1]) if len(args) == 2 else None
            if target_id is None:
                await event.reply(f"روش استفاده: {command} شناسه_عددی_کاربر")
                return
            if target_id in self.settings.admin_ids:
                await event.reply("امکان مسدودسازی یا رفع مسدودیت مدیران از این دستور وجود ندارد.")
                return
            changed = await self.db.set_banned(
                target_id, command == "/ban", admin_id=admin_id
            )
            if not changed:
                await event.reply("این شناسه در فهرست کاربران ربات پیدا نشد.")
                return
            await event.reply(
                "کاربر مسدود شد." if command == "/ban" else "مسدودیت کاربر برداشته شد."
            )
            return
        if command == "/broadcast":
            args = text.split(maxsplit=1)
            if len(args) != 2 or not args[1].strip():
                await event.reply("روش استفاده: /broadcast متن پیام")
                return
            await event.reply("ارسال پیام همگانی آغاز شد؛ نتیجه پس از پایان اعلام می‌شود.")
            sent = await self._broadcast(admin_id, args[1].strip())
            await event.respond(f"ارسال همگانی پایان یافت. پیام به {sent} کاربر رسید.")

    async def _send_broadcast_pages(self, telegram_id: int, pages: list[str]) -> None:
        for page_index, page in enumerate(pages):
            if page_index:
                await asyncio.sleep(1.05)
            try:
                await self.client.send_message(telegram_id, page, parse_mode=None)
            except FloodWaitError as exc:
                logger.warning("Telegram flood wait during broadcast seconds=%s", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
                # Retry this page, not every previously delivered page.
                await self.client.send_message(telegram_id, page, parse_mode=None)

    async def _broadcast(self, admin_id: int, message: str) -> int:
        recipients = await self.db.user_ids(include_banned=False)
        pages = split_message(message)
        sent = 0
        for index, telegram_id in enumerate(recipients):
            if index:
                await asyncio.sleep(1.05)
            try:
                await self._send_broadcast_pages(telegram_id, pages)
                sent += 1
            except Exception:
                logger.exception("Broadcast failed for user %s", telegram_id)
        await self.db.add_broadcast(admin_id, message, sent)
        return sent
