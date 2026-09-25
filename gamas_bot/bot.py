from __future__ import annotations

import asyncio
import html
import logging
import re
import tempfile
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from .config import Settings
from .database import Database
from .stt import STTError, transcribe
from .structuring import structure_transcript

logger = logging.getLogger(__name__)
AUDIO_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".wma", ".webm"
}
MESSAGE_CHUNK_SIZE = 3800


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = MESSAGE_CHUNK_SIZE) -> list[str]:
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
        pages.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pages.append(text)
    return pages or [""]


def markdown_to_telegram_html(text: str) -> str:
    """Convert the small Markdown subset used by the LLM to Telegram-safe HTML."""
    lines = text.splitlines()
    result: list[str] = []
    table_header: list[str] | None = None
    in_code = False

    def inline(value: str) -> str:
        value = html.escape(value, quote=False)
        # Inline code first so its contents are not treated as Markdown.
        value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
        value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
        value = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", value)
        value = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"<i>\1</i>", value)
        return value

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            result.append(inline(line))
            continue
        if "|" in stripped and stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            if table_header is None:
                table_header = cells
                continue
            pairs = [
                f"<b>{inline(table_header[index])}:</b> {inline(cell)}"
                for index, cell in enumerate(cells)
                if cell and index < len(table_header)
            ]
            result.append(" · ".join(pairs) if pairs else inline(" | ".join(cells)))
            continue
        if table_header is not None:
            table_header = None
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if heading:
            result.append(f"<b>{inline(heading.group(1).strip())}</b>")
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            item = re.sub(r"^\s*[-*+]\s+", "", line)
            result.append("• " + inline(item))
            continue
        if re.match(r"^\s*\d+[.)]\s+", line):
            result.append(inline(line.strip()))
            continue
        result.append(inline(line))
    return "\n".join(result).strip()


def _audio_metadata(message) -> tuple[bool, str | None, str | None, float | None, str | None]:
    file_obj = message.file
    filename = getattr(file_obj, "name", None)
    mime_type = getattr(file_obj, "mime_type", None)
    extension = Path(filename).suffix.lower() if filename else ""
    is_voice = bool(message.voice)
    is_audio = is_voice or bool(message.audio) or (
        message.document is not None
        and ((mime_type or "").lower().startswith("audio/") or extension in AUDIO_EXTENSIONS)
    )
    duration = getattr(file_obj, "duration", None)
    if duration is None and is_voice:
        duration = getattr(message.voice, "duration", None)
    document = message.document
    file_id = str(getattr(document, "id", None) or message.id)
    return is_audio, filename, mime_type, float(duration) if duration else None, file_id


class StudyBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.client = TelegramClient(
            str(settings.session_path), settings.telegram_api_id, settings.telegram_api_hash,
        )
        self.client.parse_mode = None
        self._tasks: set[asyncio.Task] = set()
        self._job_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)

    async def start(self) -> None:
        self.settings.validate_runtime()
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        await self.db.open()
        self._register_handlers()
        await self.client.start(bot_token=self.settings.telegram_bot_token)
        me = await self.client.get_me()
        logger.info("Persian study assistant is online as @%s", me.username)

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
        @self.client.on(events.NewMessage(incoming=True))
        async def handle_message(event):
            try:
                await self._handle_message(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled incoming message failure")
                try:
                    await event.reply("متأسفانه خطایی رخ داد. لطفاً کمی بعد دوباره تلاش کنید.")
                except Exception:
                    logger.exception("Could not notify the user about a handler error")

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
            await event.reply(
                "سلام! به دستیار جزوه‌ساز خوش آمدید. 🌱\n\n"
                "پیام صوتی یا فایل صوتی کلاس را بفرستید؛ متن آن پیاده‌سازی و به جزوه‌ای مرتب "
                "همراه با نکته‌های مهم تبدیل می‌شود. فایل‌های طولانی هم پذیرفته می‌شوند.\n\n"
                "برای راهنما، دستور /help را بفرستید."
            )
            return
        if command == "/help":
            await event.reply(
                "راهنمای دستیار جزوه‌ساز 📚\n\n"
                "• یک پیام صوتی یا فایل صوتی ارسال کنید.\n"
                "• پس از دریافت فایل، پردازش در پس‌زمینه انجام می‌شود و نتیجه برایتان می‌آید.\n"
                "• فایل‌های صوتی رایج مانند MP3، M4A، WAV، OGG و FLAC پشتیبانی می‌شوند.\n\n"
                "دستورهای مدیر: /users، /stats، /broadcast، /ban و /unban"
            )
            return
        if command in {"/users", "/stats", "/broadcast", "/ban", "/unban"}:
            if not is_admin:
                await event.reply("این دستور فقط برای مدیر ربات فعال است.")
                return
            await self._handle_admin_command(event, command, text)
            return

        is_audio, filename, mime_type, duration, file_id = _audio_metadata(event.message)
        if is_audio:
            await self._accept_audio(event, user, filename, mime_type, duration, file_id)

    async def _accept_audio(self, event, user, filename, mime_type, duration, file_id) -> None:
        file_obj = event.message.file
        size = getattr(file_obj, "size", None)
        if size is not None and size > self.settings.max_file_size:
            await event.reply("حجم فایل از محدودیت فعلی ربات بیشتر است و امکان پردازش آن وجود ندارد.")
            logger.warning("Rejected oversized audio from user %s: %s bytes", user["telegram_id"], size)
            return
        submission_id = await self.db.create_submission(
            user["id"], file_id, duration, filename, mime_type
        )
        task = asyncio.create_task(
            self._process_submission(event, submission_id, filename),
            name=f"audio-submission-{submission_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await event.reply("فایل صوتی دریافت شد ✅\nپردازش و آماده‌سازی جزوه در پس‌زمینه آغاز شد؛ نتیجه را همین‌جا می‌فرستم.")

    async def _process_submission(self, event, submission_id: int, filename: str | None) -> None:
        temp_path: Path | None = None
        try:
            async with self._job_semaphore:
                await self.db.set_submission_status(submission_id, "processing")
                suffix = Path(filename).suffix.lower() if filename else ".ogg"
                if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
                    suffix = ".audio"
                with tempfile.NamedTemporaryFile(
                    prefix=f"submission-{submission_id}-", suffix=suffix,
                    dir=self.settings.temp_dir, delete=False,
                ) as temp_file:
                    temp_path = Path(temp_file.name)
                downloaded = await event.message.download_media(file=str(temp_path))
                if not downloaded or not temp_path.exists():
                    raise RuntimeError("دانلود فایل از تلگرام ناموفق بود.")
                actual_size = temp_path.stat().st_size
                if actual_size > self.settings.max_file_size:
                    raise ValueError("حجم فایل از محدودیت پردازش ربات بیشتر است.")
                result = await transcribe(temp_path, self.settings)
                try:
                    structured = await structure_transcript(result.text, self.settings)
                except Exception:
                    logger.exception("LLM structuring unavailable; returning raw transcript")
                    structured = "## متن پیاده‌سازی‌شده\n\n" + result.text
                    notice = "\n\nتوجه: مرتب‌سازی خودکار جزوه موقتاً انجام نشد؛ متن پیاده‌سازی‌شده در ادامه آمده است."
                else:
                    notice = ""
                await self.db.save_transcription(
                    submission_id, result.engine, result.text, structured
                )
                await self.db.set_submission_status(submission_id, "done")
                confidence = (
                    f" (اطمینان تقریبی موتور: {result.confidence:.0%})"
                    if result.confidence is not None else ""
                )
                header = f"جزوهٔ شما آماده است 📖\nموتور تبدیل گفتار: {result.engine}{confidence}\n\n"
                await self._send_long_message(event, header + structured + notice)
        except asyncio.CancelledError:
            await self.db.set_submission_status(submission_id, "failed", "پردازش هنگام خاموش‌شدن متوقف شد")
            raise
        except Exception as exc:
            logger.exception("Audio submission %s failed", submission_id)
            try:
                await self.db.set_submission_status(submission_id, "failed", str(exc)[:1000])
            except Exception:
                logger.exception("Could not store submission failure")
            try:
                message = (
                    "متأسفانه پردازش فایل انجام نشد. لطفاً کیفیت فایل را بررسی کنید و دوباره بفرستید."
                    if not isinstance(exc, ValueError)
                    else str(exc)
                )
                await event.reply(message)
            except Exception:
                logger.exception("Could not notify the user about processing failure")
        finally:
            if temp_path:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("Could not remove temporary audio file")

    async def _send_long_message(self, event, text: str) -> None:
        pages = split_message(text)
        for index, page in enumerate(pages, start=1):
            if index > 1:
                await asyncio.sleep(1.05)
            if len(pages) > 1:
                page = f"بخش {index} از {len(pages)}\n\n" + page
            await event.respond(markdown_to_telegram_html(page), parse_mode="html")

    async def _handle_admin_command(self, event, command: str, text: str) -> None:
        admin_id = int((await event.get_sender()).id)
        if command == "/users":
            users = await self.db.user_summaries(limit=50)
            if not users:
                await event.reply("هنوز کاربری در ربات ثبت نشده است.")
                return
            lines = ["فهرست کاربران (حداکثر ۵۰ کاربر اخیر):"]
            for user in users:
                name = f"@{user['username']}" if user["username"] else "بدون نام کاربری"
                ban = " | مسدود" if user["is_banned"] else ""
                joined = str(user["first_seen"])[:10]
                lines.append(
                    f"• {name} | شناسه: {user['telegram_id']} | عضویت: {joined} | "
                    f"فایل‌ها: {user['submission_count']}{ban}"
                )
            await self._send_long_message(event, "\n".join(lines))
            return
        if command == "/stats":
            stats = await self.db.stats()
            await event.reply(
                "آمار ربات 📊\n"
                f"کاربران ثبت‌شده: {stats['users']}\n"
                f"کاربران غیرمسدود: {stats['unbanned_users']}\n"
                f"کاربران دارای ارسال در ۳۰ روز اخیر: {stats['active_30d']}\n"
                f"کل فایل‌های صوتی: {stats['submissions']}\n"
                f"پردازش‌های موفق: {stats['done']}\n"
                f"پردازش‌های ناموفق: {stats['failed']}"
            )
            return
        if command in {"/ban", "/unban"}:
            args = text.split(maxsplit=1)
            if len(args) != 2 or not args[1].strip().lstrip("+").isdigit():
                await event.reply(f"روش استفاده: {command} شناسه_عددی_کاربر")
                return
            target_id = int(args[1].strip())
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

    async def _broadcast(self, admin_id: int, message: str) -> int:
        recipients = await self.db.user_ids(include_banned=False)
        sent = 0
        for index, telegram_id in enumerate(recipients):
            if index:
                await asyncio.sleep(1.05)
            try:
                for page_index, page in enumerate(split_message(message)):
                    if page_index:
                        await asyncio.sleep(1.05)
                    await self.client.send_message(telegram_id, page, parse_mode=None)
                sent += 1
            except FloodWaitError as exc:
                logger.warning("Telegram flood wait of %s seconds during broadcast", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
                try:
                    for page_index, page in enumerate(split_message(message)):
                        if page_index:
                            await asyncio.sleep(1.05)
                        await self.client.send_message(telegram_id, page, parse_mode=None)
                    sent += 1
                except Exception:
                    logger.exception("Broadcast retry failed for user %s", telegram_id)
            except Exception:
                logger.exception("Broadcast failed for user %s", telegram_id)
        await self.db.add_broadcast(admin_id, message, sent)
        return sent
