"""Gamas Bot v3 — Telegram handlers, job pipeline, delivery.

One file, three concerns, in order:

* **Intake** — classify an upload (audio / video / presentation), record it,
  enqueue it on the bounded queue with explicit back-pressure.
* **The pipeline** — download → media worker → STT → raw-text engine →
  **deliver the transcript immediately** → note generation → RTL Word booklet.
  The transcript is delivered before note generation starts: an LLM outage can
  delay the booklet, never the raw text.
* **The screens** — one button for the main flow, one account screen
  (balance, plans, payment), one small admin panel (overview, users,
  payments, broadcast, audit).

Everything provider-related lives in ``stt.py`` / ``llm.py``; everything text
lives in ``transcript.py`` / ``notes.py``; everything layout in
``docx_export.py``. This module only orchestrates.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import aiohttp
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import MessageMediaWebPage

from .billing import (
    FREE_PLAN_NAME,
    format_duration,
    format_toman,
    format_validity,
    plan_catalog,
)
from .config import Settings, format_payment_card
from .db import Database, InsufficientBalanceError
from .docx_export import (
    DocumentMeta,
    build_notes_docx,
    build_plain_docx,
    notes_docx_filename,
    plain_docx_filename,
    raw_text_filename,
    resolve_design,
    resolve_fonts,
)
from .jobs import Job, JobQueue, QueueFullError
from .logging_config import log_job_id
from .media import (
    MediaToolError,
    convert_to_pptx,
    extract_audio_track,
    needs_transcode,
    probe_media,
)
from .notes import StructuredNotes, structure_presentation, structure_transcript
from .presentations import (
    PresentationError,
    classify_presentation,
    load_presentation,
    prepare_audio,
    slides_outline,
)
from .progress import JobProgress, to_persian_digits
from .stt import STTError, STTConfigurationError, transcribe
from .telegram_text import button_label, render_pages
from .transcript import build_document, render as render_transcript
from .textnorm import normalize_display

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".amr", ".au", ".flac", ".m4a", ".m4b", ".mp3",
    ".oga", ".ogg", ".opus", ".ra", ".wav", ".wma", ".webm",
}
VIDEO_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
    ".mpg", ".ts", ".webm", ".wmv",
}
PRESENTATION_EXTENSIONS = {".ppt", ".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm", ".pps", ".pot"}

USER_VISIBLE_ERRORS = (ValueError, PresentationError, STTConfigurationError, MediaToolError)

RAW_PREVIEW_CHARS = 800
MAX_RAW_PREVIEW_LINES = 12


# ---------------------------------------------------------------------------
# Intake classification helpers
# ---------------------------------------------------------------------------


def _media_metadata(message) -> tuple[str | None, str | None, str | None, float | None]:
    """Classify an incoming message as ('audio' | 'video' | None, name, mime, duration)."""
    file_obj = message.file
    filename = getattr(file_obj, "name", None)
    mime_type = getattr(file_obj, "mime_type", None)
    extension = Path(filename).suffix.lower() if filename else ""
    normalized_mime = (mime_type or "").lower()

    kind: str | None = None
    if message.voice or message.audio:
        kind = "audio"
    elif getattr(message, "gif", None):
        kind = None  # animated stickers are not lectures
    elif getattr(message, "video_note", None) or getattr(message, "video", None):
        kind = "video"
    elif message.document is not None:
        if normalized_mime.startswith("audio/"):
            kind = "audio"
        elif normalized_mime.startswith("video/"):
            kind = "video"
        elif extension in VIDEO_EXTENSIONS:
            # ``.webm`` belongs to both lists; only the video path always
            # extracts the audio track, and a video-only .webm must not be
            # sent to the speech engine as a whole container.
            kind = "video"
        elif extension in AUDIO_EXTENSIONS:
            kind = "audio"
    if kind is None:
        return None, None, None, None
    duration = getattr(file_obj, "duration", None)
    if duration is None and message.voice:
        duration = getattr(message.voice, "duration", None)
    return kind, filename, mime_type, float(duration) if duration else None


def _presentation_metadata(message) -> tuple[str | None, str | None]:
    """Return (kind, filename) for PowerPoint documents."""
    if message.document is None or message.voice or message.audio:
        return None, None
    filename = getattr(message.file, "name", None)
    mime_type = getattr(message.file, "mime_type", None)
    kind = classify_presentation(filename, mime_type)
    if kind is None:
        return None, None
    return kind, filename


def _is_unsupported_attachment(message) -> bool:
    """True for a real file the bot cannot use — never for links or text."""
    media = getattr(message, "media", None)
    if media is None or isinstance(media, MessageMediaWebPage):
        return False
    if getattr(message, "sticker", None) is not None or getattr(message, "gif", None) is not None:
        return False
    return getattr(message, "document", None) is not None or getattr(message, "photo", None) is not None


def _is_receipt_image(message) -> bool:
    if getattr(message, "photo", None) is None:
        return False
    mime = getattr(message.file, "mime_type", "") or ""
    return mime.startswith("image/")


# ---------------------------------------------------------------------------
# The bot
# ---------------------------------------------------------------------------


class GamasBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.queue = JobQueue(
            max_queued=settings.max_queued_jobs,
            max_active=settings.max_active_jobs,
            worker=self._run_job,
        )
        self._receipt_cleanup_task: asyncio.Task | None = None
        self._pending_payments: dict[int, int] = {}  # user_id -> payment_id awaiting receipt
        self._pending_broadcast: set[int] = set()
        settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(settings.session_path), settings.api_id, settings.api_hash
        )
        self.client.parse_mode = None
        self._http: aiohttp.ClientSession | None = None
        self._register_handlers()

    # ------------------------------------------------------------------ boot
    async def run(self) -> None:
        await self.db.connect()
        await self.db.sync_plans(
            [
                {
                    "code": plan.code,
                    "hours": plan.included_seconds / 3600,
                    "price": plan.price_toman,
                    "validity_days": plan.validity_days,
                }
                for plan in plan_catalog(self.settings.tariff_values())
                if not plan.is_free
            ]
        )
        await self.db.recover_interrupted()
        self.queue.ensure_workers()
        self._receipt_cleanup_task = asyncio.create_task(self._receipt_cleanup_loop())
        await self.client.start(bot_token=self.settings.bot_token)
        logger.info("Gamas Bot v3 started admins=%d", len(self.settings.admin_ids))
        await self.client.run_until_disconnected()

    async def shutdown(self) -> None:
        if self._receipt_cleanup_task is not None:
            self._receipt_cleanup_task.cancel()
        await self.queue.shutdown()
        if self._http is not None:
            await self._http.close()
            self._http = None
        await self.client.disconnect()
        await self.db.close()

    def _http_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_read=self.settings.stt_job_timeout)
            )
        return self._http

    # -------------------------------------------------------------- handlers
    def _register_handlers(self) -> None:
        client = self.client

        @client.on(events.CallbackQuery)
        async def handle_callback(event):
            try:
                await self._handle_callback(event)
            except MessageNotModifiedError:
                pass
            except Exception:
                logger.exception("callback failed data=%r", event.data)
                await self._ack(event, "خطایی رخ داد؛ دوباره امتحان کنید.", alert=True)

        @client.on(events.NewMessage)
        async def handle_message(event):
            try:
                await self._handle_message(event)
            except FloodWaitError:
                raise
            except Exception:
                logger.exception("message handler failed")
                try:
                    await event.reply("⚠️ خطای غیرمنتظره‌ای رخ داد. لطفاً دوباره امتحان کنید.")
                except Exception:
                    pass

    async def _ack(self, event, text: str | None = None, *, alert: bool = False) -> None:
        try:
            await event.answer(text, alert=alert)
        except Exception:
            logger.debug("callback answer failed", exc_info=True)

    async def _edit(self, event, text: str, buttons=None) -> None:
        """Edit a callback message; fall back to sending a new one."""
        try:
            await event.edit(text, buttons=buttons)
        except MessageNotModifiedError:
            pass
        except Exception:
            logger.debug("callback edit failed; responding instead", exc_info=True)
            try:
                await event.respond(text, buttons=buttons)
            except Exception:
                logger.warning("callback screen could not be delivered", exc_info=True)

    # ----------------------------------------------------------------- menus
    def _main_menu(self) -> list:
        return [
            [Button.inline("📎 ساخت جزوه", b"make")],
            [Button.inline("💳 حساب و شارژ", b"account"), Button.inline("❓ راهنما", b"help")],
        ]

    def _admin_menu(self) -> list:
        return [
            [Button.inline("📊 نمای کلی", b"adm:home")],
            [
                Button.inline("👥 کاربران", b"adm:users"),
                Button.inline("🧾 پرداخت‌ها", b"adm:pay"),
            ],
            [
                Button.inline("📣 پیام همگانی", b"adm:bcast"),
                Button.inline("🛡 آخرین اقدام‌ها", b"adm:audit"),
            ],
            [Button.inline("🔙 بازگشت", b"menu:main")],
        ]

    def _root_menu(self, is_admin: bool) -> list:
        return self._admin_menu() if is_admin else self._main_menu()

    async def _show_main(self, event, *, first_start: bool = False) -> None:
        user_id = event.sender_id
        await self.db.ensure_user(user_id)
        if await self.db.grant_free_plan_once(
            user_id, self._free_plan_seconds()
        ):
            logger.info("free plan granted user=%s", user_id)
        available = await self.db.available_seconds(user_id)
        greeting = "سلام! من گاماس هستم 🌱" if first_start else "منوی اصلی"
        text = (
            f"{greeting}\n\n"
            "جزوهٔ کلاس‌تان را از یک فایل صوتی، ویدیویی یا پاورپوینت می‌سازم؛ "
            "متن خام پیاده‌سازی‌شده را هم همان لحظه تحویل می‌دهم.\n\n"
            f"⏳ اعتبار باقی‌مانده: {format_duration(available)}"
        )
        await self._edit_or_reply(event, text, self._root_menu(self._is_admin(user_id)))

    async def _edit_or_reply(self, event, text: str, buttons=None) -> None:
        """Render a screen: edit when triggered by a button, reply when by a message."""
        if hasattr(event, "data"):  # a CallbackQuery event
            await self._edit(event, text, buttons)
            return
        await event.reply(text, buttons=buttons)

    def _is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.settings.admin_ids

    def _free_plan_seconds(self) -> int:
        for plan in plan_catalog(self.settings.tariff_values()):
            if plan.is_free:
                return plan.included_seconds
        return 3600

    # ------------------------------------------------------------- callbacks
    async def _handle_callback(self, event) -> None:
        data = (event.data or b"").decode("utf-8", "replace").strip()
        user_id = event.sender_id
        if user_id is None:
            await self._ack(event)
            return
        if not self._is_admin(user_id) and data.startswith("adm:"):
            await self._ack(event, "این بخش ویژهٔ مدیران است.", alert=True)
            return

        if data == "make":
            await self._ack(event)
            await event.respond(
                "📎 فایل درس را بفرستید:\n"
                "• صوت یا ویدیوی کلاس (MP3، MP4، WAV، OGG، M4A، …)\n"
                "• ویس یا ویدیو-پیام تلگرام\n"
                "• فایل پاورپوینت (PPTX/PPT) — صدای ضبط‌شدهٔ داخل اسلایدها هم پشتیبانی می‌شود\n\n"
                "متن خام پیاده‌سازی را همان اول می‌گیرید؛ جزوهٔ Word چند لحظه بعد می‌رسد."
            )
        elif data == "help":
            await self._ack(event)
            await event.respond(
                "❓ راهنما\n\n"
                "۱) دکمهٔ «ساخت جزوه» را بزنید و فایل درس را بفرستید.\n"
                "۲) اول فایل «متن خام» می‌رسد — همان چیزی که از صدا پیاده شده.\n"
                "۳) بعد جزوهٔ Word ساختارمند (RTL، جدول، شماره صفحه) ارسال می‌شود.\n\n"
                "قانون حساب: مصرف بر پایهٔ مدت صوت است و کار ناموفق اعتبار نمی‌سوزاند.\n"
                "محدودیت‌ها: هر فایل تا ۲ گیگابایت؛ هم‌زمان ۳ کار فعال و ۸ کار در صف."
            )
        elif data == "menu:main":
            await self._ack(event)
            await self._show_main(event)
        elif data == "account":
            await self._ack(event)
            await self._show_account(event)
        elif data.startswith("plan:"):
            await self._ack(event)
            await self._start_payment(event, data.split(":", 1)[1])
        elif data.startswith("payr:"):
            # user reports having paid -> upload receipt
            await self._ack(event)
            await self._await_receipt(event, int(data.split(":", 1)[1]))
        elif data.startswith("payv:"):
            await self._review_payment(event, data)
        elif data == "adm:home":
            await self._ack(event)
            await self._show_admin_overview(event)
        elif data == "adm:users":
            await self._ack(event)
            await self._show_admin_users(event)
        elif data == "adm:pay":
            await self._ack(event)
            await self._show_admin_payments(event)
        elif data == "adm:bcast":
            await self._ack(event)
            self._pending_broadcast.add(user_id)
            await event.respond(
                "📣 متن پیام همگانی را بفرستید. برای لغو، /cancel بفرستید."
            )
        elif data == "adm:audit":
            await self._ack(event)
            await self._show_admin_audit(event)
        else:
            await self._ack(event)

    # -------------------------------------------------------------- messages
    async def _handle_message(self, event) -> None:
        if event.message is None or not event.is_private:
            return
        user_id = event.sender_id
        if user_id is None:
            return
        await self.db.ensure_user(user_id)
        text = (event.message.message or "").strip()

        # Admin conversational states.
        if self._is_admin(user_id) and user_id in self._pending_broadcast:
            if text.lower() == "/cancel":
                self._pending_broadcast.discard(user_id)
                await event.reply("پیام همگانی لغو شد.")
                return
            self._pending_broadcast.discard(user_id)
            await self._broadcast(event, text)
            return
        if self._is_admin(user_id) and text.startswith("/grant"):
            await self._admin_grant(event, text)
            return

        # Payment receipt pending for this user?
        if user_id in self._pending_payments and _is_receipt_image(event.message):
            await self._handle_receipt_upload(event, user_id)
            return

        if text.startswith("/start"):
            await self._show_main(event, first_start=True)
            return
        if text.startswith("/cancel"):
            self._pending_payments.pop(user_id, None)
            await event.reply("لغو شد.")
            return

        # Presentations.
        pkind, pfilename = _presentation_metadata(event.message)
        if pkind is not None:
            await self._accept_presentation(event, user_id, pkind, pfilename)
            return

        # Media.
        kind, filename, mime, duration = _media_metadata(event.message)
        if kind is not None:
            await self._accept_media(event, user_id, kind, filename, duration)
            return

        if _is_unsupported_attachment(event.message):
            await event.reply(
                "⚠️ این نوع فایل را پشتیبانی نمی‌کنم. صدا، ویدیو یا پاورپوینت بفرستید."
            )
            return
        if text:
            await event.reply(
                "برای ساخت جزوه، فایل صوتی/تصویری درس را بفرستید یا از منوی شروعه "
                "دکمهٔ «📎 ساخت جزوه» را بزنید."
            )

    # ---------------------------------------------------------------- intake
    async def _accept_media(
        self, event, user_id: int, kind: str, filename: str | None, duration: float | None
    ) -> None:
        available = await self.db.available_seconds(user_id)
        needed = self._reserve_estimate(duration)
        if duration is not None and available < needed:
            await event.reply(
                f"⏳ اعتبار شما برای این فایل کافی نیست.\n"
                f"مدت فایل: {format_duration(duration)} — اعتبار فعلی: {format_duration(available)}\n"
                "از «💳 حساب و شارژ» می‌توانید اعتبار بگیرید."
            )
            return
        submission_id = await self.db.create_submission(user_id, kind, filename)
        progress = await JobProgress.create(event, submission_id=submission_id)
        job = Job(
            submission_id=submission_id,
            user_id=user_id,
            kind=kind,
            filename=filename,
            message=event.message,
            is_admin=self._is_admin(user_id),
            duration_hint=duration,
            meta={"progress": progress},
        )
        try:
            self.queue.submit(job)
        except QueueFullError as exc:
            await self.db.set_submission_status(submission_id, "rejected", error=str(exc))
            await progress.fail(str(submission_id))
            await event.reply(f"⏸ {exc}")
            return
        logger.info(
            "accepted media submission=%s kind=%s user=%s duration=%s",
            submission_id, kind, user_id, duration,
        )

    async def _accept_presentation(
        self, event, user_id: int, kind: str, filename: str | None
    ) -> None:
        submission_id = await self.db.create_submission(user_id, "presentation", filename)
        progress = await JobProgress.create(event, submission_id=submission_id)
        job = Job(
            submission_id=submission_id,
            user_id=user_id,
            kind="presentation",
            filename=filename,
            message=event.message,
            is_admin=self._is_admin(user_id),
            meta={"presentation_kind": kind, "progress": progress},
        )
        try:
            self.queue.submit(job)
        except QueueFullError as exc:
            await self.db.set_submission_status(submission_id, "rejected", error=str(exc))
            await progress.fail(str(submission_id))
            await event.reply(f"⏸ {exc}")
            return
        logger.info("accepted presentation submission=%s user=%s", submission_id, user_id)

    @staticmethod
    def _reserve_estimate(duration: float | None) -> int:
        if duration is None or duration <= 0:
            return 600  # unknown duration: reserve a conservative floor
        return int(math.ceil(duration))

    # ------------------------------------------------------------ job worker
    async def _run_job(self, job: Job) -> None:
        progress: JobProgress | None = job.meta.get("progress")
        log_token = log_job_id.set(f"GMS-{job.submission_id}")
        workdir: Path | None = None
        try:
            await self.db.set_submission_status(job.submission_id, "processing")
            self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
            workdir = Path(tempfile.mkdtemp(
                prefix=f"submission-{job.submission_id}-", dir=self.settings.temp_dir
            ))
            if job.kind == "presentation":
                await self._process_presentation(job, workdir, progress)
            else:
                await self._process_media(job, workdir, progress)
        except InsufficientBalanceError as exc:
            await self.db.set_submission_status(
                job.submission_id, "failed",
                error=f"insufficient balance: {exc}",
            )
            if progress:
                await progress.fail(str(job.submission_id))
            await self._reply(job, (
                f"⏳ اعتبار شما برای این فایل کافی نیست.\n"
                f"لازم: {format_duration(exc.required_seconds)} — "
                f"موجودی: {format_duration(exc.available_seconds)}\n"
                "از «💳 حساب و شارژ» اعتبار بگیرید."
            ))
        except USER_VISIBLE_ERRORS as exc:
            await self._release(job, "user-visible failure")
            await self.db.set_submission_status(
                job.submission_id, "failed", error=str(exc)[:500]
            )
            if progress:
                await progress.fail(str(job.submission_id))
            await self._reply(job, f"❌ {exc}")
        except STTError as exc:
            await self._release(job, "stt failure")
            await self.db.set_submission_status(
                job.submission_id, "failed", error=str(exc)[:500]
            )
            if progress:
                await progress.fail(str(job.submission_id))
            await self._reply(job, f"❌ تبدیل گفتار به متن نشد: {exc}")
        except asyncio.CancelledError:
            await self._release(job, "cancelled")
            await self.db.set_submission_status(
                job.submission_id, "failed", error="cancelled"
            )
            raise
        except Exception as exc:
            await self._release(job, f"unexpected: {type(exc).__name__}")
            await self.db.set_submission_status(
                job.submission_id, "failed", error=f"{type(exc).__name__}: {exc}"[:500]
            )
            logger.exception("job failed submission=%s", job.submission_id)
            if progress:
                await progress.fail(str(job.submission_id))
            await self._reply(
                job, "❌ متأسفم، کار کامل نشد. لطفاً بعداً دوباره امتحان کنید."
            )
        finally:
            if progress:
                await progress.aclose()
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
            log_job_id.reset(log_token)

    async def _release(self, job: Job, reason: str) -> bool:
        return await self.db.release_hold(job.submission_id, reason)

    async def _reply(self, job: Job, text: str) -> None:
        if job.message is None:
            return
        try:
            await job.message.reply(text)
        except Exception:
            logger.warning("could not reply to user submission=%s", job.submission_id)

    # ------------------------------------------------------- media pipeline
    async def _process_media(self, job: Job, workdir: Path, progress) -> None:
        assert progress is not None
        await progress.update(10, "🚀 شروع کردم")
        suffix = Path(job.filename).suffix.lower() if job.filename else (
            ".mp4" if job.kind == "video" else ".ogg"
        )
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ".media"
        source_path = workdir / f"source{suffix}"
        await progress.update(20, "📥 دارم فایل را از تلگرام می‌گیرم…")
        downloaded = await job.message.download_media(file=str(source_path))
        if not downloaded or not source_path.exists():
            raise RuntimeError("دانلود فایل از تلگرام ناموفق بود.")
        if source_path.stat().st_size > self.settings.max_file_size:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")

        await progress.update(35, "🔍 فایل رسید؛ دارم صدا را بررسی می‌کنم")
        audio_path = await self._prepare_audio(source_path, workdir, job.filename, job.kind)
        if audio_path.stat().st_size > self.settings.max_file_size:
            raise ValueError("صدای آماده‌شده از سقف حجم ربات بزرگ‌تر است.")

        info = await probe_media(audio_path, self.settings)
        duration = info.duration or job.duration_hint
        needed = self._reserve_estimate(duration)
        await self.db.reserve(job.user_id, job.submission_id, needed)

        await progress.update(50, "🎙️ دارم صدا را به متن تبدیل می‌کنم")
        try:
            raw = await transcribe(audio_path, self.settings, session=self._http_session())
        except BaseException:
            await self._release(job, "transcription failed")
            raise
        document = build_document(raw)

        await progress.update(75, "📝 متن آماده شد؛ فایل خام را می‌فرستم")
        await self.db.set_submission_status(
            job.submission_id, "processing", duration_seconds=duration
        )
        reference = f"GMS-{job.submission_id}"
        meta = DocumentMeta(
            reference=reference,
            source_name=job.filename,
            created_at=datetime.now(),
        )
        # ---- PROGRESSIVE DELIVERY: the raw text goes out before the notes. ----
        raw_text = render_transcript(document, self.settings.raw_text_profile)
        raw_path = workdir / raw_text_filename(reference)
        raw_path.write_text(raw_text, encoding="utf-8", newline="\n")
        await self._send_file(
            job, raw_path,
            caption=f"📝 متن خام — {reference}" ,
        )
        await self._send_preview(job, raw_text)

        await self.db.save_transcription(
            job.submission_id, engine=raw.engine, text=document.plain(), notes_json=""
        )
        await self._settle(job, duration)

        await progress.update(85, "✍️ دارم جزوهٔ ساختارمند را می‌سازم")
        notes = await self._generate_notes(document.llm_document(), job, progress)

        await self.db.save_transcription(
            job.submission_id, engine=raw.engine, text=document.plain(),
            notes_json=notes.to_json() if notes else "",
        )
        await self._deliver_booklet(job, workdir, notes, document, reference, meta, progress)

    async def _prepare_audio(
        self, source_path: Path, workdir: Path, filename: str | None, kind: str
    ) -> Path:
        info = await probe_media(source_path, self.settings)
        if not info.has_audio:
            raise ValueError("این فایل شاخهٔ صوتی ندارد؛ چیزی برای پیاده‌سازی نیست.")
        if kind == "video" or needs_transcode(filename, info):
            return await extract_audio_track(source_path, workdir, self.settings)
        return source_path

    async def _settle(self, job: Job, duration: float | None) -> None:
        measured = int(math.ceil(duration)) if duration else 0
        await self.db.settle_hold(job.submission_id, measured)

    # ------------------------------------------------- presentation pipeline
    async def _process_presentation(self, job: Job, workdir: Path, progress) -> None:
        assert progress is not None
        await progress.update(10, "🚀 شروع کردم")
        suffix = Path(job.filename).suffix.lower() if job.filename else ".pptx"
        source_path = workdir / f"source{suffix}"
        await progress.update(20, "📥 دارم فایل را از تلگرام می‌گیرم…")
        downloaded = await job.message.download_media(file=str(source_path))
        if not downloaded or not source_path.exists():
            raise RuntimeError("دانلود فایل از تلگرام ناموفق بود.")

        if job.meta.get("presentation_kind") == "legacy":
            await progress.update(30, "🔁 دارم قالب قدیمی را تبدیل می‌کنم…")
            source_path = await convert_to_pptx(source_path, workdir, self.settings)

        await progress.update(40, "🔍 دارم اسلایدها را می‌خوانم")
        content = await load_presentation(source_path, workdir, self.settings)
        outline = slides_outline(content.slides)
        skipped: list[str] = []
        prepared = await prepare_audio(content, workdir, self.settings, skipped_out=skipped)

        raw_text_parts: list[str] = []
        if outline.strip():
            raw_text_parts.append("متن اسلایدها:\n\n" + outline.strip())
        document = None
        if prepared is not None:
            info = await probe_media(prepared.path, self.settings)
            duration = info.duration or prepared.total_duration
            needed = self._reserve_estimate(duration)
            await self.db.reserve(job.user_id, job.submission_id, needed)
            await progress.update(55, "🎙️ دارم صدای ارائه را به متن تبدیل می‌کنم")
            try:
                raw = await transcribe(
                    prepared.path, self.settings, session=self._http_session()
                )
            except BaseException:
                await self._release(job, "transcription failed")
                raise
            document = build_document(raw)
            raw_text_parts.append("متن پیاده‌سازی‌شدهٔ صدای ارائه:\n\n" + document.plain())
            await self._settle(job, duration)
        elif not outline.strip():
            raise ValueError("در این ارائه نه متن اسلایدی پیدا شد و نه صدای ضبط‌شده.")

        reference = f"GMS-{job.submission_id}"
        meta = DocumentMeta(reference=reference, source_name=job.filename, created_at=datetime.now())

        # ---- PROGRESSIVE DELIVERY ----
        await progress.update(70, "📝 متن خام را می‌فرستم")
        raw_text = "\n\n\n".join(part for part in raw_text_parts if part.strip())
        raw_path = workdir / raw_text_filename(reference)
        raw_path.write_text(raw_text, encoding="utf-8", newline="\n")
        await self._send_file(job, raw_path, caption=f"📝 متن خام — {reference}")
        await self._send_preview(job, raw_text)

        await self.db.save_transcription(
            job.submission_id, engine="stt", text=raw_text, notes_json=""
        )

        await progress.update(85, "✍️ دارم جزوهٔ ساختارمند را می‌سازم")
        transcript_text = document.llm_document() if document is not None else ""
        notes = await self._generate_presentation_notes(outline, transcript_text, job, progress)
        await self.db.save_transcription(
            job.submission_id, engine="stt", text=raw_text,
            notes_json=notes.to_json() if notes else "",
        )
        await self._deliver_booklet(job, workdir, notes, document, reference, meta, progress,
                                    plain_title="جزوهٔ ارائه")

    # ----------------------------------------------------------- notes + docx
    async def _generate_notes(self, document_text: str, job: Job, progress) -> StructuredNotes | None:
        if not self.settings.note_configured:
            logger.info("note provider not configured; delivering raw only")
            return None
        try:
            return await structure_transcript(
                document_text, self.settings, self._http_session(),
                mode=self.settings.note_mode,
            )
        except Exception as exc:
            logger.warning("note generation failed; raw already delivered: %s", exc)
            await self._reply(
                job,
                "ℹ️ این بار نتوانستم متن را به جزوهٔ ساختارمند دربیاورم؛ متن خام را دریافت کردید.",
            )
            return None

    async def _generate_presentation_notes(
        self, outline: str, transcript_text: str, job: Job, progress
    ) -> StructuredNotes | None:
        if not self.settings.note_configured:
            return None
        if not outline.strip() and not transcript_text.strip():
            return None
        try:
            return await structure_presentation(
                outline, transcript_text, self.settings, self._http_session(),
                mode=self.settings.note_mode,
            )
        except Exception as exc:
            logger.warning("presentation note generation failed: %s", exc)
            await self._reply(
                job,
                "ℹ️ این بار نتوانستم متن را به جزوهٔ ساختارمند دربیاورم؛ متن خام را دریافت کردید.",
            )
            return None

    async def _deliver_booklet(
        self,
        job: Job,
        workdir: Path,
        notes: StructuredNotes | None,
        document,
        reference: str,
        meta: DocumentMeta,
        progress,
        *,
        plain_title: str = "جزوهٔ کلاس",
    ) -> None:
        await progress.update(94, "📑 جزوه آماده است؛ دارم فایل Word را می‌سازم")
        notice = ""
        docx_path: Path | None = None
        if notes is not None:
            try:
                docx_bytes = await asyncio.to_thread(
                    build_notes_docx, notes,
                    fonts=resolve_fonts(),
                    design=resolve_design(),
                    meta=meta,
                )
                docx_path = workdir / notes_docx_filename(notes, reference)
                docx_path.write_bytes(docx_bytes)
            except Exception:
                logger.exception("docx generation failed; falling back to chat message")
                docx_path = None
        if docx_path is None:
            plain_text = document.llm_document() if document is not None else ""
            try:
                docx_bytes = await asyncio.to_thread(
                    build_plain_docx, plain_title, plain_text,
                    fonts=resolve_fonts(), design=resolve_design(), meta=meta,
                )
                docx_path = workdir / plain_docx_filename(plain_title, reference)
                docx_path.write_bytes(docx_bytes)
            except Exception:
                logger.exception("plain docx generation failed too")
                notice = "\n\nنکته: فایل Word ساخته نشد؛ متن خام ارسال شد."

        if docx_path is not None:
            caption = f"📖 جزوهٔ Word — {reference}"
            try:
                await self._send_file(job, docx_path, caption=caption)
            except Exception:
                logger.exception("docx delivery failed")
                notice = "\n\nنکته: ارسال فایل Word ناموفق بود؛ متن خام ارسال شد."

        await self.db.set_submission_status(job.submission_id, "done")
        if notice:
            await progress.complete("متن خام ارسال شد. " + notice.strip())
        else:
            await progress.complete()

    async def _send_file(self, job: Job, path: Path, *, caption: str = "") -> None:
        if job.message is None:
            return
        await job.message.reply(
            file=str(path), caption=caption[:1000] or None, force_document=True
        )

    async def _send_preview(self, job: Job, raw_text: str) -> None:
        text = normalize_display(raw_text)
        lines: list[str] = []
        used = 0
        for line in text.splitlines():
            if used + len(line) + 1 > RAW_PREVIEW_CHARS or len(lines) >= MAX_RAW_PREVIEW_LINES:
                lines.append("…")
                break
            lines.append(line)
            used += len(line) + 1
        preview = "\n".join(lines).strip()
        if not preview:
            return
        try:
            await self._reply(job, f"👀 پیش‌نمایش متن خام:\n\n{preview}")
        except Exception:
            logger.debug("preview send failed", exc_info=True)

    # ------------------------------------------------------------ accounting
    async def _show_account(self, event) -> None:
        user_id = event.sender_id
        available = await self.db.available_seconds(user_id)
        history = await self.db.payment_history(user_id, limit=5)
        plans = await self.db.plans()
        lines = [
            "💳 حساب شما",
            "",
            f"⏳ اعتبار باقی‌مانده: {format_duration(available)}",
            "",
            "شارژ:",
        ]
        buttons = []
        for plan in plans:
            if plan["price"] <= 0:
                continue
            hours = plan["hours"]
            hours_text = to_persian_digits(int(hours) if float(hours).is_integer() else hours)
            label = button_label(f"{hours_text} ساعت — {format_toman(plan['price'])}")
            buttons.append([Button.inline(label, f"plan:{plan['code']}".encode())])
        if history:
            lines.append("")
            lines.append("آخرین پرداخت‌ها:")
            for item in history:
                status = {
                    "pending": "⏳ در انتظار بررسی",
                    "approved": "✅ تایید شد",
                    "rejected": "❌ رد شد",
                }.get(item["status"], item["status"])
                lines.append(f"• {item['plan_code']} — {status}")
        lines.append("")
        lines.append(FREE_PLAN_NAME + " برای همه در شروع فعال است.")
        buttons.append([Button.inline("🔙 بازگشت", b"menu:main")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _start_payment(self, event, plan_code: str) -> None:
        plan = await self.db.plan(plan_code)
        if plan is None or plan["price"] <= 0:
            await event.respond("این طرح در دسترس نیست.")
            return
        card = format_payment_card(self.settings.payment_card_number)
        hours = plan["hours"]
        hours_text = to_persian_digits(int(hours) if float(hours).is_integer() else hours)
        text = (
            f"🧾 خرید {hours_text} ساعت اعتبار\n\n"
            f"مبلغ: {format_toman(plan['price'])}\n"
            f"اعتبار تا {format_validity(plan['validity_days'])} معتبر است.\n\n"
            "کارت‌به‌کارت:\n"
            f"💳 شماره کارت: {card}\n"
            f"👤 به نام: {self.settings.payment_card_holder}\n"
            f"🏦 بانک: {self.settings.payment_bank_name}\n\n"
            "پس از واریز، دکمهٔ زیر را بزنید و تصویر رسید را بفرستید. "
            "تا تایید مدیر معمولاً چند ساعت طول می‌کشد."
        )
        payment_id = await self.db.create_payment(event.sender_id, plan_code)
        await event.respond(text, buttons=[
            [Button.inline("📤 واریز کردم؛ ارسال رسید", f"payr:{payment_id}".encode())],
            [Button.inline("🔙 بازگشت", b"account")],
        ])

    async def _await_receipt(self, event, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if payment is None or payment["status"] != "pending":
            await event.respond("این درخواست دیگر فعال نیست.")
            return
        self._pending_payments[event.sender_id] = payment_id
        await event.respond("📷 تصویر رسید را بفرستید (همین‌جا در چت).")

    async def _handle_receipt_upload(self, event, user_id: int) -> None:
        payment_id = self._pending_payments.pop(user_id, None)
        if payment_id is None:
            return
        payment = await self.db.payment(payment_id)
        if payment is None or payment["status"] != "pending":
            await event.respond("این درخواست دیگر فعال نیست.")
            return
        if event.message.photo is None:
            return
        size = getattr(event.message.file, "size", 0) or 0
        if size > self.settings.max_payment_receipt_bytes:
            await event.respond("حجم تصویر رسید زیاد است؛ فشرده‌تر بفرستید.")
            self._pending_payments[user_id] = payment_id
            return
        self.settings.receipt_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        receipt_path = self.settings.receipt_dir / f"receipt-{payment_id}-{token}.jpg"
        await event.message.download_media(file=str(receipt_path))
        try:
            receipt_path.chmod(0o600)
        except OSError:
            pass
        await self.db.set_payment_receipt(payment_id, str(receipt_path))
        await event.respond(
            "✅ رسید ثبت شد. پس از تایید مدیر، اعتبار به حسابتان اضافه می‌شود."
        )
        await self._notify_admins_of_payment(payment_id)

    async def _notify_admins_of_payment(self, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if payment is None:
            return
        text = (
            f"🧾 پرداخت جدید شمارهٔ {to_persian_digits(payment_id)}\n"
            f"کاربر: {payment['user_id']}\n"
            f"طرح: {payment['plan_code']}\n"
            "برای بررسی: پنل مدیریت → 🧾 پرداخت‌ها"
        )
        for admin_id in self.settings.admin_ids:
            try:
                await self.client.send_message(admin_id, text)
            except Exception:
                logger.warning("could not notify admin=%s", admin_id)

    async def _review_payment(self, event, data: str) -> None:
        # payv:<id>:a | payv:<id>:r
        try:
            _, raw_id, verdict = data.split(":", 2)
            payment_id = int(raw_id)
        except ValueError:
            await self._ack(event, "درخواست نامعتبر است.", alert=True)
            return
        approved = verdict == "a"
        payment = await self.db.payment(payment_id)
        if payment is None or payment["status"] != "pending":
            await self._ack(event, "این پرداخت قبلاً بررسی شده است.", alert=True)
            await self._show_admin_payments(event)
            return
        plan = await self.db.plan(payment["plan_code"])
        hours = plan["hours"] if plan else 0
        days = plan["validity_days"] if plan else None
        changed = await self.db.review_payment(
            payment_id, approved, event.sender_id,
            plan_hours=hours, plan_validity_days=days,
        )
        if not changed:
            await self._ack(event, "این پرداخت قبلاً بررسی شده است.", alert=True)
        else:
            await self._ack(event, "ثبت شد." if approved else "رد شد.")
            user_text = (
                f"✅ پرداخت شما تایید شد: {to_persian_digits(hours)} ساعت اعتبار اضافه شد."
                if approved
                else "❌ پرداخت شما تایید نشد. در صورت اعتراض با پشتیبانی تماس بگیرید."
            )
            try:
                await self.client.send_message(payment["user_id"], user_text)
            except Exception:
                logger.warning("could not notify user=%s", payment["user_id"])
        await self._show_admin_payments(event)

    # ----------------------------------------------------------------- admin
    async def _show_admin_overview(self, event) -> None:
        stats = await self.db.stats()
        text = (
            "📊 نمای کلی\n\n"
            f"👥 کاربران: {to_persian_digits(stats['users'])}\n"
            f"✅ کارهای موفق: {to_persian_digits(stats['submissions_done'])}\n"
            f"❌ کارهای ناموفق: {to_persian_digits(stats['submissions_failed'])}\n"
            f"⏳ در حال پردازش/صف: {to_persian_digits(stats['submissions_active'])}\n"
            f"⏱ مجموع صوت پیاده‌شده: {format_duration(stats['seconds_transcribed'])}\n"
            f"💰 درآمد تاییدشده: {format_toman(stats['toman_approved'])}\n\n"
            f"صف فعلی: {to_persian_digits(self.queue.pending)} در انتظار"
        )
        await self._edit_or_reply(event, text, self._admin_menu())

    async def _show_admin_users(self, event) -> None:
        users = await self.db.list_users(limit=25)
        lines = ["👥 آخرین کاربران\n"]
        for user in users:
            flag = "🚫 " if user["banned"] else ""
            lines.append(
                f"{flag}{user['telegram_id']} — {format_duration(user['available_seconds'])}"
            )
        lines.append("")
        lines.append(
            "برای اهدای اعتبار بفرستید:\n/grant <user_id> <hours>"
        )
        await self._edit_or_reply(event, "\n".join(lines), self._admin_menu())

    async def _show_admin_payments(self, event) -> None:
        payments = await self.db.pending_payments()
        if not payments:
            await self._edit_or_reply(
                event, "🧾 پرداخت‌ها\n\nپرداخت در انتظار بررسی نیست.", self._admin_menu()
            )
            return
        buttons = []
        lines = ["🧾 پرداخت‌های در انتظار\n"]
        for payment in payments:
            lines.append(
                f"#{payment['payment_id']} — کاربر {payment['user_id']} — "
                f"{payment['plan_code']} — رسید: {'دارد ✅' if payment['receipt_path'] else 'ندارد ❌'}"
            )
            buttons.append([
                Button.inline("✅ تایید", f"payv:{payment['payment_id']}:a".encode()),
                Button.inline("❌ رد", f"payv:{payment['payment_id']}:r".encode()),
            ])
        buttons.append([Button.inline("🔙 بازگشت", b"adm:home")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _show_admin_audit(self, event) -> None:
        rows = await self.db.recent_audit(limit=15)
        lines = ["🛡 آخرین اقدام‌های مدیران\n"]
        if not rows:
            lines.append("چیزی ثبت نشده است.")
        for row in rows:
            lines.append(f"• {row['created_at']} — {row['admin_id']} — {row['action']}")
        await self._edit_or_reply(event, "\n".join(lines), self._admin_menu())

    async def _admin_grant(self, event, text: str) -> None:
        parts = text.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await event.reply("قالب درست: /grant <user_id> <hours>")
            return
        target, hours = int(parts[1]), int(parts[2])
        if not (0 < hours <= 10_000):
            await event.reply("ساعت باید بین ۱ و ۱۰۰۰۰ باشد.")
            return
        await self.db.ensure_user(target)
        await self.db.add_credit(
            target, hours * 3600, note=f"admin grant by {event.sender_id}",
            validity_days=None, admin_id=event.sender_id,
        )
        await event.reply(f"✅ {format_duration(hours * 3600)} اعتبار به {target} اضافه شد.")

    async def _broadcast(self, event, text: str) -> None:
        users = await self.db.list_users(limit=100_000)
        pages = render_pages(text)
        sent = 0
        for user in users:
            try:
                for page in pages:
                    await self.client.send_message(user["telegram_id"], page)
                sent += 1
            except FloodWaitError as exc:
                logger.warning("broadcast flood wait seconds=%s", exc.seconds)
                await asyncio.sleep(min(exc.seconds, 60))
            except Exception:
                logger.debug("broadcast to %s failed", user["telegram_id"])
        await self.db.audit(event.sender_id, "broadcast", f"text:{len(text)} users:{sent}")
        await event.reply(f"📣 پیام به {to_persian_digits(sent)} کاربر رسید.")

    # --------------------------------------------------------------- cleanup
    async def _receipt_cleanup_loop(self) -> None:
        """Delete reviewed receipts after the retention window."""
        while True:
            try:
                await asyncio.sleep(24 * 3600)
                await self._cleanup_expired_receipts()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("receipt cleanup failed", exc_info=True)

    async def _cleanup_expired_receipts(self) -> None:
        if not self.settings.receipt_dir.is_dir():
            return
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=self.settings.receipt_retention_days)
        removed = 0
        for path in self.settings.receipt_dir.glob("receipt-*.jpg"):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info("removed %d expired receipts", removed)


# ---------------------------------------------------------------------------
# Module-level helpers (kept importable for tests)
# ---------------------------------------------------------------------------


