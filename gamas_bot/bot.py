from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import MessageMediaWebPage

from .config import Settings
from .database import Database
from .docx_export import (
    DocumentMeta,
    build_notes_docx,
    build_plain_docx,
    build_raw_text_document,
    notes_docx_filename,
    plain_docx_filename,
    raw_text_filename,
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
from .stt import transcribe
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
USER_VISIBLE_ERRORS = (ValueError, PresentationError)
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
    "در پایگاه‌داده بمانند. لطفاً فایل خیلی حساس نفرستید."
)


def main_menu(is_admin: bool = False):
    rows = [
        [Button.inline("📎 ساخت جزوه", b"menu:create")],
        [
            Button.inline("📚 راهنما", b"menu:help"),
            Button.inline("🧰 قالب‌ها", b"menu:formats"),
        ],
        [Button.inline("🔐 حریم خصوصی", b"menu:privacy")],
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
            Button.inline("📣 پیام همگانی", b"admin:broadcast"),
            Button.inline("🚫 مسدودسازی", b"admin:ban"),
        ],
        [
            Button.inline("✅ رفع مسدودیت", b"admin:unban"),
            Button.inline("↩️ منوی اصلی", b"menu:home"),
        ],
    ]


def _error_reference(submission_id: int) -> str:
    return f"GMS-{submission_id:06d}"


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


class StudyBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        # Telethon opens its SQLite session in the constructor, before start().
        settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(settings.session_path), settings.telegram_api_id, settings.telegram_api_hash,
            proxy=settings.telegram_proxy,
        )
        self.client.parse_mode = None
        self._tasks: set[asyncio.Task] = set()
        self._job_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._pending_admin_actions: dict[int, str] = {}

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
        self._register_handlers()
        await self.client.start(bot_token=self.settings.telegram_bot_token)
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
                removed += not leftover.exists()
        if removed:
            logger.info("Removed stale temporary job folders count=%s", removed)

    async def run(self) -> None:
        try:
            await self.start()
            await self.client.run_until_disconnected()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._tasks:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
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

        if not data.startswith("admin:"):
            await event.answer("این دکمه معتبر نیست.", alert=True)
            return
        if not is_admin:
            await event.answer("این بخش فقط برای مدیر ربات است.", alert=True)
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
        changed = await self.db.set_banned(target_id, action == "ban")
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
        if command == "/start":
            self._pending_admin_actions.pop(telegram_id, None)
            await event.reply(WELCOME_TEXT, buttons=main_menu(is_admin))
            return
        if command in {"/help", "/cancel"}:
            self._pending_admin_actions.pop(telegram_id, None)
            await event.reply(HELP_TEXT, buttons=back_menu(is_admin))
            return
        if command in {"/users", "/stats", "/broadcast", "/ban", "/unban"}:
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
        task = asyncio.create_task(
            self._process_submission(
                event,
                submission_id,
                filename,
                kind,
                progress=progress,
                is_admin=int(user["telegram_id"]) in self.settings.admin_ids,
            ),
            name=f"{kind}-submission-{submission_id}",
        )
        self._track_task(task)

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
        task = asyncio.create_task(
            self._process_presentation(
                event,
                submission_id,
                filename,
                kind,
                progress=progress,
                is_admin=int(user["telegram_id"]) in self.settings.admin_ids,
            ),
            name=f"presentation-submission-{submission_id}",
        )
        self._track_task(task)

    async def _process_presentation(
        self,
        event,
        submission_id: int,
        filename: str | None,
        kind: str,
        *,
        progress: JobProgress | None = None,
        is_admin: bool = False,
    ) -> None:
        workdir: Path | None = None
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
                    result = await transcribe(prepared.path, self.settings)
                    await progress.update(80, "✍️ متن صدا آماده شد")
                    transcript_text = result.text
                    engine = result.engine

                await progress.update(85, "📚 دارم مطالب را به شکل جزوه مرتب می‌کنم")
                notes: StructuredNotes | None = None
                try:
                    notes = await structure_presentation(
                        outline, transcript_text, self.settings
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
                await self.db.set_submission_status(submission_id, "done")
                await progress.complete()
                logger.info(
                    "Presentation submission completed submission_id=%s engine=%s slides=%s",
                    submission_id,
                    engine,
                    content.slide_count,
                )
        except asyncio.CancelledError:
            await self.db.set_submission_status(
                submission_id, "failed", "پردازش هنگام خاموش‌شدن متوقف شد"
            )
            raise
        except Exception as exc:
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
    ) -> None:
        workdir: Path | None = None
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

                await progress.update(55, "🎙️ دارم صدا را به متن تبدیل می‌کنم")
                result = await transcribe(audio_path, self.settings)
                await progress.update(80, "✍️ متن آماده شد؛ دارم آن را به شکل جزوه مرتب می‌کنم")
                notes: StructuredNotes | None = None
                try:
                    notes = await structure_transcript(result.text, self.settings)
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
                await self.db.set_submission_status(submission_id, "done")
                await progress.complete()
                logger.info(
                    "Media submission completed submission_id=%s kind=%s engine=%s",
                    submission_id,
                    kind,
                    result.engine,
                )
        except asyncio.CancelledError:
            await self.db.set_submission_status(submission_id, "failed", "پردازش هنگام خاموش‌شدن متوقف شد")
            raise
        except Exception as exc:
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
                docx_bytes = build_notes_docx(
                    notes, font=self.settings.docx_font, meta=meta
                )
                docx_path = workdir / notes_docx_filename(notes, reference)
                docx_path.write_bytes(docx_bytes)
            except Exception:
                logger.exception(
                    "Word document generation failed; falling back to a chat message reference=%s",
                    reference,
                )
                docx_path = None
        else:
            try:
                docx_bytes = build_plain_docx(
                    plain_title, plain_text, font=self.settings.docx_font, meta=meta
                )
                docx_path = workdir / plain_docx_filename(plain_title, reference)
                docx_path.write_bytes(docx_bytes)
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
            changed = await self.db.set_banned(target_id, command == "/ban")
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
