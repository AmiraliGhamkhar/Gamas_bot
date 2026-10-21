"""Gamas Bot v3 — Telegram handlers, job pipeline, delivery.

One file, three concerns, in order:

* **Intake** — classify an upload (audio / video / presentation), record it,
  enqueue it on the bounded queue with explicit back-pressure.
* **The pipeline** — download → media worker → STT → raw-text engine →
  **deliver the transcript immediately** → note generation → RTL Word booklet.
  The transcript is delivered before note generation starts: an LLM outage can
  delay the booklet, never the raw text.
* **The screens** — two separated panels with their own navigation. The
  **user panel** is the root for every account: one button for the main
  flow, one account screen (balance, plans, payment), help. The **admin
  panel** (overview, users, payments, broadcast, audit) is a separate
  screen tree entered from its own button — admins get both panels, the
  user menu never turns into the admin menu, and admin-only callbacks are
  enforced server-side.

Everything provider-related lives in ``stt.py`` / ``llm.py``; everything text
lives in ``transcript.py`` / ``notes.py``; everything layout in
``docx_export.py``. This module only orchestrates.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import secrets
import stat
import shutil
import tempfile
import time
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
from .config import Settings, format_payment_card, note_routes
from .db import (
    Database,
    InsufficientBalanceError,
    OpenPaymentError,
    PaymentSnapshotError,
    TooManyActiveJobsError,
)
from .docx_export import (
    OFFICE_HEADING_OFFSET,
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
from .notes import (
    StructuredNotes,
    build_presentation_document,
    structure_presentation,
    structure_transcript,
)
from .pdf_export import build_office_pdf
from .office import (
    FORMAT_EMOJI,
    FORMAT_LABELS,
    OFFICE_FORMATS,
    OFFICE_MODE_LABELS,
    TEXT_FILE_EXTENSIONS,
    OfficeError,
    OfficeSession,
    build_office_document,
    build_office_pptx,
    build_office_xlsx,
    fallback_slides,
    office_filename,
    office_title_from_filename,
    read_text_file,
)
from .office_ai import infer_table, plan_deck
from .presentations import (
    PresentationError,
    align_narration_to_slides,
    classify_presentation,
    load_presentation,
    prepare_audio,
    slides_outline,
)
from .progress import JobProgress, to_persian_digits
from .stt import largest_configured_upload
from .stt import STTError, STTConfigurationError, transcribe
from .telegram_text import button_label, render_pages, split_message
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

class UploadTooLargeError(ValueError):
    """A Telegram transfer exceeded its configured byte ceiling."""


class DownloadStalledError(ValueError):
    """A Telegram download produced no bytes for the configured stall window."""


class DiskSpaceError(ValueError):
    """The temporary filesystem cannot hold the job's files."""


USER_VISIBLE_ERRORS = (
    ValueError,
    PresentationError,
    STTConfigurationError,
    MediaToolError,
    OfficeError,
)

RAW_PREVIEW_CHARS = 800
MAX_RAW_PREVIEW_LINES = 12

#: Telegram's documented caption limit for a media message (UTF-16 code units).
TELEGRAM_CAPTION_LIMIT = 1024
#: Upper bound on the paginated text fallback when a file upload fails. The raw
#: transcript is the product's core artifact, so a delivery-size failure is
#: recovered in the chat rather than thrown away — but never unboundedly. The
#: bound is generous enough for a multi-hour lecture (~40 pages ≈ 150 000
#: characters); reaching it means the file is pathological, not a lecture.
RAW_FALLBACK_MAX_PAGES = 40

#: Shown when a user already has the configured number of active submissions.
#: The balance check at intake cannot see a job queued a moment earlier, so the
#: cap is enforced where the row is created (``db.create_submission``).
ACTIVE_LIMIT_MESSAGE = (
    "⏳ شما هم‌اکنون {limit} درخواست در حال پردازش دارید. "
    "بگذارید یکی از آن‌ها تمام شود و بعد این فایل را بفرستید."
)

#: Maximum length of a user-controlled string that is logged verbatim. Longer
#: values are truncated to avoid log-bloat / log-injection.
MAX_LOGGED_STRING_CHARS = 200


def _sanitize_log_value(value: object, *, limit: int = MAX_LOGGED_STRING_CHARS) -> str:
    """Return a single-line, truncated representation safe for logs."""
    text = " ".join(str(value).split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text


#: Delivery-size boundaries tried for one raw-transcript part, in order. A part
#: is cut at the last of these that falls in its second half, so a boundary is
#: never mid-word and the parts stay balanced.
_RAW_PART_BOUNDARIES = ("\n\n", "\n", "؟ ", ". ", "، ", " ")


def raw_parts(
    text: str, *, max_chars: int, max_parts: int
) -> list[str]:
    """Split a raw transcript into delivery-sized parts, or refuse.

    The single ``.txt`` upload is the normal path; when *it* fails, the same
    document is re-sent in parts rather than truncated. A multi-hour lecture
    (150k–300k characters) does not fit the paginated text fallback at all, so
    before this split the whole job was refunded with nothing delivered.

    Cuts land on a paragraph, then a line, then a sentence, then a word
    boundary — never inside a character — and the number of parts is bounded.
    An empty list means the text cannot be delivered within ``max_parts``: the
    caller refuses it instead of shipping a truncated transcript.
    """
    body = text.strip("\n")
    if not body.strip():
        return []
    if max_chars <= 0 or max_parts <= 0:
        return []
    if len(body) <= max_chars:
        return [body]
    parts: list[str] = []
    rest = body
    while rest:
        if len(rest) <= max_chars:
            parts.append(rest)
            break
        if len(parts) + 1 >= max_parts:
            # One more cut would leave a remainder with no part left to carry
            # it: refuse rather than lose the tail silently.
            return []
        window = rest[:max_chars]
        cut = max_chars
        for boundary in _RAW_PART_BOUNDARIES:
            index = window.rfind(boundary)
            if index >= max_chars // 2:
                cut = index + len(boundary)
                break
        # The part keeps its boundary characters and the remainder drops only
        # the newlines the boundary already ended with: rejoining the parts
        # reproduces the transcript exactly, including between words.
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return [part for part in parts if part.strip()]


def _is_safe_receipt_path(receipt_dir: Path, receipt_path: str | Path) -> bool:
    """True when *receipt_path* is a single-link regular file in *receipt_dir*.

    Resolve and constrain the path, reject symlinks and hard links, and require
    a regular file. Rejecting ``st_nlink > 1`` prevents a receipt-directory
    name from aliasing an inode also reachable outside the private directory.
    """
    try:
        base = receipt_dir.resolve(strict=True)
        raw = Path(receipt_path)
        raw_stat = raw.lstat()
        if not stat.S_ISREG(raw_stat.st_mode) or raw_stat.st_nlink != 1:
            return False
        target = raw.resolve(strict=True)
        target.relative_to(base)
        resolved_stat = target.stat()
    except (OSError, RuntimeError, ValueError):
        return False
    return stat.S_ISREG(resolved_stat.st_mode) and resolved_stat.st_nlink == 1


def _command_name(text: str) -> str:
    """Return an exact slash-command token; never strip an arbitrary @target."""
    token = text.split(maxsplit=1)[0] if text.strip() else ""
    if not token.startswith("/") or "@" in token:
        return ""
    return token.lower()


def _parse_telegram_user_id(raw: str) -> int | None:
    """Parse a Telegram user ID from admin command input robustly.

    Rejects non-decimal digits (e.g. superscript ``²``) that ``isdigit()``
    accepts but ``int()`` rejects, and bounds to Telegram's 64-bit ID range.
    Returns None on failure.
    """
    cleaned = raw.strip()
    if not cleaned.isdecimal():
        return None
    try:
        value = int(cleaned)
    except ValueError:
        return None
    if not 0 < value < 2**63:
        return None
    return value


MAX_RECEIPT_IMAGE_PIXELS = 12_000_000


def _normalize_receipt_image(path: Path, *, max_bytes: int) -> bool:
    """Validate and re-encode a JPEG/PNG/WebP receipt as a clean JPEG.

    Magic bytes alone accept truncated images and polyglots. Pillow verifies
    and fully decodes a bounded-size image, then re-encoding strips trailing
    payloads and unneeded metadata before the receipt is stored or forwarded.
    The validation fails closed if Pillow is unavailable.
    """
    temp_path: Path | None = None
    try:
        size = path.stat().st_size
        if size < 8 or size > max_bytes:
            return False
        with path.open("rb") as source:
            header = source.read(16)
        is_jpeg = header.startswith(b"\xff\xd8\xff")
        is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
        is_webp = header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WEBP"
        if not (is_jpeg or is_png or is_webp):
            return False

        from PIL import Image

        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                return False
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_RECEIPT_IMAGE_PIXELS:
                return False
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_RECEIPT_IMAGE_PIXELS:
                return False
            image.load()
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                clean = Image.new("RGB", rgba.size, "white")
                clean.paste(rgba, mask=rgba.getchannel("A"))
            else:
                clean = image.convert("RGB")
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".jpg.tmp", dir=path.parent
            )
            os.close(fd)
            temp_path = Path(temp_name)
            clean.save(temp_path, format="JPEG", quality=90, optimize=True)
            os.chmod(temp_path, 0o600)
        if temp_path.stat().st_size > max_bytes:
            return False
        os.replace(temp_path, path)
        temp_path = None
        os.chmod(path, 0o600)
        return True
    except (ImportError, OSError, ValueError, SyntaxError):
        return False
    except Exception:
        # Corrupt images and Pillow's decompression-bomb errors fail closed.
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _ensure_disk_headroom(
    directory: Path, required_bytes: int, *, reserve_bytes: int = 0
) -> None:
    """Refuse a download that would leave the temp filesystem below its floor.

    A long lecture briefly needs the downloaded source *and* the prepared audio
    copy, so the estimate is twice the upload. ``TEMP_MIN_FREE_BYTES`` is the
    operator's floor: it protects the other jobs' temporary directories and the
    SQLite database from a full disk. Without this check a full disk surfaced as
    an opaque provider failure after the whole multi-gigabyte download had been
    written (audit F-5).
    """
    free = shutil.disk_usage(directory).free
    if free < required_bytes + reserve_bytes:
        logger.warning(
            "temp disk headroom refused: free=%d required=%d reserve=%d dir=%s",
            free, required_bytes, reserve_bytes, directory,
        )
        raise DiskSpaceError(
            "فضای موقت سرور برای این فایل کافی نیست؛ لطفاً کمی بعد دوباره بفرستید."
        )


async def _download_media_limited(
    message, target: Path, limit_bytes: int, *, stall_seconds: float = 0.0
) -> Path:
    """Download Telegram media with live abort if it exceeds *limit_bytes*.

    Telegram may report ``file.size`` as None or as a thumbnail size, so
    pre-check alone is insufficient. The progress callback aborts the download
    as soon as the declared total or the running total exceeds the limit,
    preventing a multi-GB disk-fill before the post-download stat check.

    A download that produces no bytes for ``stall_seconds`` is also aborted
    (``0`` disables the watchdog). There is no job-level timeout by design, so
    a half-open socket used to hold a worker — and its temporary directory —
    indefinitely on a multi-gigabyte lecture.
    """
    if limit_bytes <= 0:
        raise ValueError("download limit must be positive")
    last_progress = [time.monotonic()]

    def _guard(received: int, total: int) -> None:
        # Telethon reports (received, total); total can be zero for an unknown
        # size. Check both the declaration and running byte count.
        last_progress[0] = time.monotonic()
        if total and total > limit_bytes:
            raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
        if received > limit_bytes:
            raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")

    async def _download():
        return await message.download_media(
            file=str(target), progress_callback=_guard
        )

    task = asyncio.create_task(_download())
    try:
        if stall_seconds > 0:
            tick = max(1.0, min(stall_seconds / 4, 15.0))
            while True:
                done, _pending = await asyncio.wait({task}, timeout=tick)
                if done:
                    break
                idle = time.monotonic() - last_progress[0]
                if idle > stall_seconds:
                    raise DownloadStalledError(
                        "دانلود فایل از تلگرام متوقف شد؛ لطفاً دوباره بفرستید."
                    )
        downloaded = await task
        if not downloaded or not target.is_file():
            raise RuntimeError("download did not produce a file")
        if target.stat().st_size > limit_bytes:
            raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
        return target
    except BaseException:
        # This includes cancellation, a stalled transfer and progress-callback
        # failures: never keep a partial upload in private storage.
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
        self._media_semaphore = asyncio.Semaphore(settings.media_worker_concurrency)
        self._receipt_cleanup_task: asyncio.Task | None = None
        self._pending_payments: dict[int, int] = {}  # user_id -> payment_id awaiting receipt
        self._pending_broadcast: set[int] = set()
        # admin_id -> payment_id awaiting a rejection reason
        self._pending_reject_reason: dict[int, int] = {}
        self._admin_action_times: dict[tuple[int, str], float] = {}
        self._broadcast_running: set[int] = set()
        self._started_at = time.time()
        # Free Office: the in-flight request of every user, and the last
        # document each of them built (so «افزودن متن» can extend it).
        self._office_sessions: dict[int, OfficeSession] = {}
        # user_id -> (kind, text, title, built_at monotonic timestamp)
        self._office_last: dict[int, tuple[str, str, str, float]] = {}
        # user_id -> monotonic time of the last *accepted* build request. The
        # section is free and may call the note model, so it needs its own
        # per-user throttle (``OFFICE_MIN_INTERVAL_SECONDS``).
        self._office_request_times: dict[int, float] = {}
        settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(settings.session_path), settings.api_id, settings.api_hash
        )
        self.client.parse_mode = None
        self._http: aiohttp.ClientSession | None = None
        self._register_handlers()

    # ------------------------------------------------------------------ boot
    def _prepare_private_dirs(self) -> None:
        """Create the bot's private directories and restrict them to 0700.

        ``docs/SECURITY.md`` promises the receipt directory is private and
        verified at startup; this is that check (audit F-20 / SECURITY drift).
        Whether the directory sits outside a web root is a deployment question
        the preflight script answers.
        """
        for directory in (self.settings.temp_dir, self.settings.receipt_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                os.chmod(directory, 0o700)
            except OSError:
                logger.warning("could not prepare private directory %s", directory)

    async def run(self) -> None:
        self._prepare_private_dirs()
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
        self._sweep_stale_temp_dirs()
        self.queue.ensure_workers()
        self._receipt_cleanup_task = asyncio.create_task(self._receipt_cleanup_loop())
        await self.client.start(bot_token=self.settings.bot_token)
        logger.info("Gamas Bot v3 started admins=%d", len(self.settings.admin_ids))
        if not self.settings.payment_card_number:
            logger.warning(
                "PAYMENT_CARD_NUMBER is not set; card-to-card payments are disabled"
            )
        await self.client.run_until_disconnected()

    def _sweep_stale_temp_dirs(self) -> None:
        """Remove work directories left behind by a crash (audit F-07).

        Only this process's own naming scheme is touched, only directories, and
        only ones older than the process start: a live sibling instance's
        directory (possible before the instance lock is taken) is never swept.
        """
        root = self.settings.temp_dir
        if not root.is_dir():
            return
        removed = 0
        for entry in root.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            if not entry.name.startswith(("submission-", "deck-", "office-")):
                continue
            try:
                if entry.stat().st_mtime >= self._started_at:
                    continue
                shutil.rmtree(entry)
                removed += 1
            except OSError:
                logger.warning("could not remove stale temp dir %s", entry.name)
        if removed:
            logger.info("removed %d stale temp dir(s) at startup", removed)

    async def shutdown(self) -> None:
        if self._receipt_cleanup_task is not None:
            self._receipt_cleanup_task.cancel()

        async def _fail_queued(job) -> None:
            # Audit F-H04: queued jobs never reached a worker; mark them failed
            # so recover_interrupted is not the only path and users are told.
            try:
                await self.db.set_submission_status(
                    job.submission_id, "failed", error="interrupted by restart"
                )
                if job.message is not None:
                    try:
                        await job.message.reply(
                            "⏸ ربات در حال راه‌اندازی مجدد است؛ کار شما لغو شد. لطفاً دوباره بفرستید."
                        )
                    except Exception:
                        pass
            except Exception:
                logger.warning("failed to mark queued job as failed submission=%s", job.submission_id)

        await self.queue.shutdown(on_reject=_fail_queued)
        if self._http is not None:
            await self._http.close()
            self._http = None
        await self.client.disconnect()
        await self.db.close()

    def _http_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            connector = aiohttp.TCPConnector(
                limit=self.settings.llm_connector_limit,
                limit_per_host=self.settings.llm_connector_limit_per_host,
                ttl_dns_cache=self.settings.llm_connector_ttl_dns_cache,
                keepalive_timeout=float(self.settings.llm_connector_keepalive),
            )
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_read=self.settings.stt_job_timeout),
                connector=connector,
            )
            if self.settings.llm_prewarm:
                self._schedule_llm_prewarm()
        return self._http

    def _schedule_llm_prewarm(self) -> None:
        """Warm one connection to the first note route's host (no quota used)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._prewarm_llm())

    async def _prewarm_llm(self) -> None:
        session = self._http
        if session is None:
            return
        try:
            routes = note_routes(self.settings)
            if not routes:
                return
            url = routes[0].base_url
            logger.debug("pre-warming LLM connection host=%s", url)
            async with session.head(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                await response.read()
        except Exception:
            logger.debug("LLM connection pre-warm failed (ignored)", exc_info=True)

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
    def _user_menu(self, *, is_admin: bool = False) -> list:
        """The user panel — the root screen every account starts from.

        Admins get one extra entry row to their own panel; the admin panel
        never replaces this menu, so both panels stay separately reachable.
        """
        make_label = (
            "📎 ساخت جزوه" if self.settings.note_configured else "📎 تبدیل فایل به متن"
        )
        rows = [
            [Button.inline(make_label, b"make")],
        ]
        if self.settings.office_enabled:
            rows.append([Button.inline("📄 آفیس رایگان", b"office")])
        rows.append(
            [
                Button.inline("💳 اعتبار و پرداخت", b"account"),
                Button.inline("❓ راهنما", b"help"),
            ]
        )
        if is_admin:
            rows.append([Button.inline("🛠 پنل مدیریت", b"adm:home")])
        return rows

    def _admin_menu(self) -> list:
        """The admin panel: section tabs plus the way back to the user panel.

        Rendered on every admin screen so the panel behaves like a tab bar —
        one tap from any section to any other, and one tap home.
        """
        return [
            [Button.inline("📊 آمار و وضعیت", b"adm:home")],
            [
                Button.inline("👥 کاربران", b"adm:users"),
                Button.inline("🧾 پرداخت‌ها", b"adm:pay"),
            ],
            [
                Button.inline("📣 ارسال پیام", b"adm:bcast"),
                Button.inline("🛡 گزارش فعالیت‌ها", b"adm:audit"),
            ],
            [Button.inline("🔙 پنل کاربری", b"menu:main")],
        ]

    async def _show_main(self, event, *, first_start: bool = False) -> None:
        user_id = event.sender_id
        await self.db.ensure_user(user_id)
        if await self.db.grant_free_plan_once(
            user_id, self._free_plan_seconds()
        ):
            logger.info("free plan granted user=%s", user_id)
        greeting = "سلام! به گاماس خوش آمدید 🌱" if first_start else "منوی اصلی"
        if self.settings.note_configured:
            introduction = (
                "برای ساخت جزوه، «📎 ساخت جزوه» را انتخاب کنید و فایل صوتی، ویدیویی یا "
                "پاورپوینت کلاس را بفرستید. ابتدا متن گفتار و سپس جزوهٔ Word برایتان ارسال می‌شود.\n"
            )
        else:
            introduction = (
                "برای تبدیل فایل کلاس به متن، «📎 تبدیل فایل به متن» را انتخاب کنید و فایل صوتی، "
                "ویدیویی یا پاورپوینت را بفرستید. متن گفتار برایتان ارسال می‌شود.\n"
                "ساخت جزوهٔ Word در حال حاضر روی این سرور فعال نیست.\n"
            )
        text = f"{greeting}\n\n{introduction}"
        if self.settings.office_enabled:
            text += (
                "برای تبدیل متن دلخواهتان به فایل Word، Excel، PowerPoint یا PDF، از "
                "«📄 آفیس رایگان» استفاده کنید. این بخش رایگان است و از اعتبار شما کم نمی‌کند.\n"
            )
        text += f"\n⏳ اعتبار باقی‌مانده: {await self._balance_text(user_id)}"
        await self._edit_or_reply(event, text, self._user_menu(is_admin=self._is_admin(user_id)))

    async def _edit_or_reply(self, event, text: str, buttons=None) -> None:
        """Render a screen: edit when triggered by a button, reply when by a message."""
        if hasattr(event, "data"):  # a CallbackQuery event
            await self._edit(event, text, buttons)
            return
        await event.reply(text, buttons=buttons)

    def _is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.settings.admin_ids

    async def _balance_text(self, user_id: int) -> str:
        """The balance as users see it: a special user has no limit to show."""
        if await self.db.is_unlimited(user_id):
            return "نامحدود ⭐ (کاربر ویژه)"
        return format_duration(await self.db.available_seconds(user_id))

    def _free_plan_seconds(self) -> int:
        for plan in plan_catalog(self.settings.tariff_values()):
            if plan.is_free:
                return plan.included_seconds
        return 3600

    # ------------------------------------------------------------- free office
    async def _show_office_menu(self, event, *, notice: str = "") -> None:
        """The Free Office screen: the four formats, and what they cost.

        The section is free, and the screen says so: a user who does not know
        the bot's credit rules must not wonder whether a document is going to
        spend their balance.
        """
        text = (
            "📄 آفیس رایگان\n\n"
            "متن خودتان را بفرستید؛ من همان متن را به یک فایل واقعی تبدیل "
            "می‌کنم — Word، Excel، PowerPoint یا PDF. متن شما عیناً در فایل می‌آید.\n"
            "رایگان است و از اعتبار شما کم نمی‌کند.\n\n"
            "قالب را انتخاب کنید:"
        )
        if notice:
            text = f"{notice}\n\n{text}"
        buttons = [
            [Button.inline(f"{FORMAT_EMOJI['word']} {FORMAT_LABELS['word']}", b"office:word")],
            [Button.inline(f"{FORMAT_EMOJI['excel']} {FORMAT_LABELS['excel']}", b"office:excel")],
            [Button.inline(f"{FORMAT_EMOJI['ppt']} {FORMAT_LABELS['ppt']}", b"office:ppt")],
            [Button.inline(f"{FORMAT_EMOJI['pdf']} {FORMAT_LABELS['pdf']}", b"office:pdf")],
            [Button.inline("🔙 بازگشت", b"menu:main")],
        ]
        await self._edit_or_reply(event, text, buttons)

    def _office_session(self, user_id: int, *, create: bool = False) -> OfficeSession | None:
        """The user's live Office session, or ``None``.

        A session that has been idle for longer than the configured window is
        dropped: the content of a request is not kept for the user to find
        hours later.
        """
        session = self._office_sessions.get(user_id)
        if session is not None:
            age = time.monotonic() - session.updated_at
            if age > self.settings.office_session_ttl_minutes * 60:
                self._office_sessions.pop(user_id, None)
                self._office_last.pop(user_id, None)
                return None
        if session is None and create:
            session = OfficeSession()
            self._office_sessions[user_id] = session
        if session is not None:
            session.updated_at = time.monotonic()
        return session

    async def _handle_office_action(self, event, action: str) -> None:
        """A button of the Free Office section."""
        user_id = event.sender_id
        if user_id is None:
            return
        if action == "cancel":
            self._office_sessions.pop(user_id, None)
            await event.respond(
                "لغو شد. هر وقت سندی خواستی، از «📄 آفیس رایگان» شروع کن."
            )
            return
        if action == "more":
            # Extend the document that was just built: the previous content
            # stays, and whatever arrives next is added to it.
            previous = self._office_last.get(user_id)
            if previous is not None:
                kind, text, title, built_at = previous
                age = time.monotonic() - built_at
                if age > self.settings.office_session_ttl_minutes * 60:
                    # The document is too old to extend; it is forgotten with
                    # the same window as the rest of the request.
                    self._office_last.pop(user_id, None)
                    previous = None
            if previous is None:
                await self._show_office_menu(event, notice="سندی برای ادامه ندارم.")
                return
            kind, text, title, _built_at = previous
            session = self._office_session(user_id, create=True) or OfficeSession()
            session.kind = kind
            session.parts = [text]
            session.title = title
            await event.respond(
                "متن تازه را بفرستید؛ به سند قبلی اضافه می‌شود و سند تازه‌ای "
                "برایتان می‌سازم.",
                buttons=[[Button.inline("↩️ انصراف", b"office:cancel")]],
            )
            return
        if action not in OFFICE_FORMATS:
            return
        session = self._office_session(user_id, create=True) or OfficeSession()
        session.kind = action
        if session.text.strip():
            # The content arrived first: the document can be built right away.
            await self._start_office_job(event, user_id, session)
            return
        await event.respond(
            "متن سند را بفرستید (یا یک فایل ‎.txt / .md / .markdown / .csv بفرستید).\n"
            "محتوای متن شما تغییر نمی‌کند؛ فقط نوشتار یکسان‌سازی می‌شود "
            "(حروف عربی به فارسی و فاصله‌های اضافی). سطرهای جدول را با Tab جدا کنید.\n"
            "حداکثر {limit} نویسه در هر سند پذیرفته می‌شود؛ متن بلندتر را در چند "
            "سند بفرستید. سند خودکار به بخش‌های منسجم تقسیم می‌شود.\n"
            "اگر متن بیشتری دارید، بعد از ساخت دکمهٔ «➕ افزودن متن» را بزنید.".format(
                limit=to_persian_digits(self.settings.office_max_chars)
            ),
            buttons=[[Button.inline("↩️ انصراف", b"office:cancel")]],
        )

    async def _handle_office_message(self, event, user_id: int, text: str) -> bool:
        """Route an incoming message into the Free Office section.

        Returns ``True`` when the message *was* the section's content, so the
        caller must not treat it as anything else. A lecture upload is never
        office content, and a command is never content either.
        """
        if not self.settings.office_enabled:
            return False
        message = event.message
        if message is None:
            return False
        # A lecture upload belongs to the lecture pipeline, not here.
        if _presentation_metadata(message)[0] is not None:
            return False
        if _media_metadata(message)[0] is not None:
            return False
        filename = getattr(message.file, "name", None) if message.file else None
        extension = Path(filename or "").suffix.lower()
        is_text_file = message.document is not None and extension in TEXT_FILE_EXTENSIONS
        if is_text_file:
            # Size before bytes: a document is downloaded by the Telethon task,
            # outside the bounded job queue, so an unchecked 2 GB upload would
            # bypass MAX_QUEUED_JOBS entirely (audit F-10).
            reported = getattr(message.file, "size", None)
            if isinstance(reported, int) and reported > self.settings.office_max_input_bytes:
                await event.reply(
                    "❌ فایل متنی از حد مجاز بزرگ‌تر است "
                    f"({self.settings.office_max_input_bytes} بایت)."
                )
                return True
        if not is_text_file:
            # Any other attachment (a photo, a sticker) is not office content:
            # only a message and a text file are.
            if (
                getattr(message, "photo", None) is not None
                or getattr(message, "sticker", None) is not None
                or getattr(message, "gif", None) is not None
            ):
                return False
            if not text or text.startswith("/"):
                return False
        session = self._office_session(user_id, create=True) or OfficeSession()
        if is_text_file:
            try:
                content = await self._download_office_text(message, filename or "")
            except OfficeError as exc:
                await event.reply(f"❌ {exc}")
                return True
            if len(session.text) + len(content) > self.settings.office_max_chars:
                # Checked *after* reading: the byte ceiling of an upload and the
                # character ceiling of a document are different limits, and the
                # old flow accepted a 1 MB file only to reject it later forever
                # (the session was never cleared, so every retry failed too).
                await event.reply(
                    "❌ متن این فایل از حد مجاز سند بیشتر است.\n"
                    f"حداکثر {to_persian_digits(self.settings.office_max_chars)} نویسه؛ "
                    "فایل را کوتاه‌تر کنید یا در چند سند بفرستید."
                )
                self._office_sessions.pop(user_id, None)
                return True
            # Appending (not replacing) keeps text the user typed before the
            # file arrived; a file used to silently discard it.
            session.add(content)
            session.filename = filename
            # A short caption is a better title than the file name.
            caption = (message.message or "").strip()
            session.title = (
                caption[:120] if 0 < len(caption) <= 90
                else office_title_from_filename(filename)
            )
        else:
            if len(session.text) + len(text) > self.settings.office_max_chars:
                await event.reply(
                    "❌ متن شما از حد مجاز بیشتر است.\n"
                    f"حداکثر {self.settings.office_max_chars} نویسه در هر سند؛ "
                    "متن را کوتاه‌تر کنید یا در چند سند بفرستید."
                )
                return True
            session.add(text)
        session.message = message
        if session.kind:
            await self._start_office_job(event, user_id, session)
        else:
            await self._show_office_menu(
                event, notice="متن شما آماده است؛ حالا قالب فایل را انتخاب کنید."
            )
        return True

    async def _download_office_text(self, message, filename: str) -> str | None:
        """Download an uploaded text file and read it.

        The file is removed as soon as it has been read: the content lives in
        the session, and the bot's promise is that temporary files do not
        outlive the work.
        """
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(6)
        target = self.settings.temp_dir / f"office-{token}{Path(filename).suffix.lower()}"
        try:
            await _download_media_limited(
                message, target, self.settings.office_max_input_bytes
            )
            return read_text_file(
                target, max_bytes=self.settings.office_max_input_bytes
            )
        except OfficeError:
            raise
        except ValueError as exc:
            raise OfficeError(
                f"حجم فایل بیش از حد مجاز است (حداکثر "
                f"{self.settings.office_max_input_bytes // 1024} کیلوبایت)."
            ) from exc
        except Exception as exc:
            logger.warning("office upload could not be read: %s", exc)
            raise OfficeError("فایل ارسالی خوانده نشد.") from exc
        finally:
            try:
                target.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - defensive
                pass

    async def _start_office_job(self, event, user_id: int, session: OfficeSession) -> None:
        """Queue the build of a Free Office document.

        The document is built by the job queue, exactly like a lecture: the
        chat handler only records the request and returns. The section never
        reserves credit — it is free — so no hold is created and nothing is
        charged when the build succeeds or fails.
        """
        content = session.text
        kind = session.kind or "word"
        if not content.strip():
            await event.reply("متنی برای ساخت سند نیست؛ متن را بفرستید.")
            return
        if len(content) > self.settings.office_max_chars:
            await event.reply(
                "❌ متن شما از حد مجاز بیشتر است "
                f"({self.settings.office_max_chars} نویسه)."
            )
            # The session must be closed: otherwise every later message keeps
            # appending to a document that can never be built.
            self._office_sessions.pop(user_id, None)
            return
        # Free does not mean unmetered: the xlsx/pptx build may call the note
        # model, so a single user cannot drive it in a loop.
        interval = self.settings.office_min_interval_seconds
        if interval > 0:
            now = time.monotonic()
            last = self._office_request_times.get(user_id)
            if last is not None and now - last < interval:
                wait = int(math.ceil(interval - (now - last)))
                await event.reply(
                    "⏳ برای ساخت سند بعدی کمی صبر کنید "
                    f"({to_persian_digits(max(1, wait))} ثانیه)."
                )
                return
            self._office_request_times[user_id] = now
            for stale in [
                key for key, value in self._office_request_times.items()
                if now - value > max(interval * 10, 600.0)
            ]:
                self._office_request_times.pop(stale, None)
        # A title the user gave (a file caption) wins; otherwise the document
        # itself finds one from the text when the job runs.
        title = session.title or ""
        try:
            submission_id = await self.db.create_submission(
                user_id,
                "office",
                session.filename or f"office-{kind}",
                active_limit=self.settings.max_active_jobs_per_user,
            )
        except TooManyActiveJobsError as exc:
            await event.reply(
                ACTIVE_LIMIT_MESSAGE.format(limit=to_persian_digits(exc.limit))
            )
            return
        anchor = session.message or getattr(event, "message", None)
        progress = await JobProgress.create(anchor, submission_id=submission_id)
        job = Job(
            submission_id=submission_id,
            user_id=user_id,
            kind="office",
            filename=session.filename,
            message=anchor,
            is_admin=self._is_admin(user_id),
            meta={
                "progress": progress,
                "office_kind": kind,
                "office_title": title,
                "office_text": content,
            },
        )
        try:
            self.queue.submit(job)
        except QueueFullError as exc:
            await self.db.set_submission_status(submission_id, "rejected", error=str(exc))
            await progress.fail(str(submission_id))
            await event.reply(f"⏸ {exc}")
            return
        # The request is now the job's business; the session is closed so a
        # later, unrelated message is never mistaken for more content.
        self._office_sessions.pop(user_id, None)
        logger.info(
            "accepted office request submission=%s user=%s kind=%s chars=%d",
            submission_id, user_id, kind, len(content),
        )

    # ------------------------------------------------------------- callbacks
    async def _handle_callback(self, event) -> None:
        data = (event.data or b"").decode("utf-8", "replace").strip()
        user_id = event.sender_id
        if user_id is None:
            await self._ack(event)
            return
        # Ignore callbacks outside private chats before touching per-user state.
        # CallbackQuery may report None on older Telethon builds; treating only
        # literal False as group context would let those events through.
        if not getattr(event, "is_private", False):
            await self._ack(event)
            return
        # The ban gate is enforced on callbacks as well as messages: a blocked
        # user must not be able to act on a stale keyboard (audit F-05).
        if await self.db.is_banned(user_id):
            await self._ack(event, "⛔ دسترسی شما به ربات مسدود شده است.", alert=True)
            return
        if not self._is_admin(user_id) and data.startswith(("adm:", "payv:")):
            await self._ack(event, "دسترسی به این بخش فقط برای مدیران مجاز است.", alert=True)
            return
        # Opening any panel screen disarms a half-finished broadcast prompt,
        # so a stray "انصراف"/navigation tap can never send stale text later.
        if data.startswith("adm:") and data != "adm:bcast":
            self._pending_broadcast.discard(user_id)
            self._pending_reject_reason.pop(user_id, None)
        # Likewise, navigating away from the payment flow drops a waiting
        # receipt upload — a photo sent later belongs to the new screen.
        if not data.startswith("payr:"):
            self._pending_payments.pop(user_id, None)

        if data == "make":
            await self._ack(event)
            if self.settings.note_configured:
                result_guide = "ابتدا متن گفتار و سپس جزوهٔ Word برایتان ارسال می‌شود."
            else:
                result_guide = (
                    "متن گفتار برایتان ارسال می‌شود؛ ساخت جزوهٔ Word در حال حاضر فعال نیست."
                )
            prompt_title = (
                "📎 ارسال فایل برای ساخت جزوه"
                if self.settings.note_configured
                else "📎 ارسال فایل برای تبدیل به متن"
            )
            await event.respond(
                f"{prompt_title}\n\n"
                "فایل صوتی یا ویدیویی کلاس، پیام صوتی یا ویدیویی تلگرام، یا فایل پاورپوینت را "
                "همین‌جا بفرستید. قالب‌های رایج مانند MP3، MP4، WAV، OGG، M4A و PPTX/PPT پشتیبانی می‌شوند.\n"
                "صدای ضبط‌شدهٔ داخل اسلایدهای پاورپوینت نیز قابل پردازش است.\n\n"
                f"{result_guide}",
                buttons=[[Button.inline("🔙 منوی اصلی", b"menu:main")]],
            )
        elif data == "help":
            await self._ack(event)
            make_label = (
                "📎 ساخت جزوه" if self.settings.note_configured else "📎 تبدیل فایل به متن"
            )
            task_label = "ساخت جزوه:" if self.settings.note_configured else "تبدیل فایل به متن:"
            guide = [
                "❓ راهنمای استفاده",
                "",
                task_label,
                f"۱. «{make_label}» را انتخاب کنید.",
                "۲. فایل صوتی یا ویدیویی، پیام صوتی یا ویدیویی تلگرام یا پاورپوینت درس را بفرستید.",
                "۳. ابتدا متن گفتار برایتان ارسال می‌شود.",
            ]
            if self.settings.note_configured:
                guide.append("۴. سپس جزوهٔ Word آماده و ارسال می‌شود.")
            else:
                guide.append("ساخت جزوهٔ Word در حال حاضر روی این سرور فعال نیست.")
            guide.extend(
                [
                    "",
                    "اعتبار بر اساس مدت فایل صوتی و پس از ارسال متن خام محاسبه می‌شود. "
                    "اگر ارسال متن خام ناموفق باشد، اعتبار مصرف نمی‌شود. "
                    "پس از دریافت متن خام، خطای ساخت جزوه باعث برگشت اعتبار نمی‌شود.",
                ]
            )
            if self.settings.office_enabled:
                guide.extend(
                    [
                        "",
                        "آفیس رایگان:",
                        "«📄 آفیس رایگان» را انتخاب کنید، قالب موردنظر را بزنید و متن یا فایل متنی "
                        "خود را بفرستید. این بخش رایگان است و از اعتبار شما کم نمی‌کند.",
                    ]
                )
            await event.respond(
                "\n".join(guide),
                buttons=[[Button.inline("🔙 منوی اصلی", b"menu:main")]],
            )
        elif data == "menu:main":
            await self._ack(event)
            await self._show_main(event)
        elif data == "office":
            await self._ack(event)
            if not self.settings.office_enabled:
                await event.respond("این بخش روی این سرور فعال نیست.")
                return
            await self._show_office_menu(event)
        elif data.startswith("office:"):
            await self._ack(event)
            if not self.settings.office_enabled:
                await event.respond("این بخش روی این سرور فعال نیست.")
                return
            await self._handle_office_action(event, data.split(":", 1)[1])
        elif data == "account":
            await self._ack(event)
            await self._show_account(event)
        elif data.startswith("plan:"):
            await self._ack(event)
            await self._start_payment(event, data.split(":", 1)[1])
        elif data.startswith(("payr:", "payc:", "payopen:")):
            await self._ack(event)
            try:
                payment_id = int(data.split(":", 1)[1])
            except ValueError:
                await event.respond("درخواست نامعتبر است.")
                return
            if data.startswith("payc:"):
                await self._cancel_payment(event, payment_id)
            elif data.startswith("payopen:"):
                await self._show_payment(event, payment_id)
            else:
                await self._await_receipt(event, payment_id)
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
                "📣 ارسال پیام به کاربران\n\n"
                "متن موردنظر را در پیام بعدی بفرستید؛ همان متن برای همهٔ کاربران ارسال می‌شود.\n"
                "برای لغو، /cancel بفرستید یا دکمهٔ زیر را انتخاب کنید.",
                buttons=[[Button.inline("↩️ لغو", b"adm:home")]],
            )
        elif data == "adm:audit":
            await self._ack(event)
            await self._show_admin_audit(event)
        elif data.startswith("adm:rcpt:"):
            await self._ack(event)
            await self._show_receipt(event, data)
        else:
            await self._ack(event)

    # -------------------------------------------------------------- messages
    async def _handle_message(self, event) -> None:
        if event.message is None or not event.is_private:
            return
        user_id = event.sender_id
        if user_id is None:
            return
        user = await self.db.ensure_user(user_id)
        if user.banned:
            await event.reply("⛔ دسترسی شما به ربات مسدود شده است.")
            return
        text = (event.message.message or "").strip()
        command = _command_name(text)

        # Admin conversational states.
        if self._is_admin(user_id) and user_id in self._pending_reject_reason:
            payment_id = self._pending_reject_reason.pop(user_id, None)
            if payment_id is not None:
                if command == "/cancel":
                    await event.reply("رد پرداخت لغو شد.")
                    await self._show_admin_payments(event)
                    return
                if not text:
                    # A photo or sticker is not a reason: keep waiting.
                    self._pending_reject_reason[user_id] = payment_id
                    await event.reply("لطفاً دلیل رد را در یک پیام متنی بنویسید؛ برای لغو /cancel بفرستید.")
                    return
                if text.startswith("/"):
                    self._pending_reject_reason[user_id] = payment_id
                    await event.reply("دستور نامعتبر است؛ دلیل را بنویسید یا برای لغو /cancel بفرستید.")
                    return
                await self._reject_payment_with_reason(event, payment_id, text)
                return
        if self._is_admin(user_id) and user_id in self._pending_broadcast:
            if command == "/cancel":
                self._pending_broadcast.discard(user_id)
                await event.reply("پیام همگانی لغو شد.")
                return
            if not text:
                await event.reply("پیام همگانی باید متن داشته باشد؛ متن را بفرستید یا /cancel بزنید.")
                return
            if text.startswith("/"):
                await event.reply("دستور نامعتبر است؛ متن پیام را بفرستید یا /cancel بزنید.")
                return
            self._pending_broadcast.discard(user_id)
            await self._broadcast(event, text)
            return
        if self._is_admin(user_id) and command == "/grant":
            await self._admin_grant(event, text)
            return
        if self._is_admin(user_id) and command == "/unlimited":
            await self._admin_unlimited(event, text)
            return
        if self._is_admin(user_id) and command in {"/ban", "/unban"}:
            await self._admin_ban(event, text)
            return

        # Payment receipt pending for this user?
        if user_id in self._pending_payments and _is_receipt_image(event.message):
            await self._handle_receipt_upload(event, user_id)
            return

        if command == "/start":
            await self._show_main(event, first_start=True)
            return
        if command == "/cancel":
            self._pending_payments.pop(user_id, None)
            self._office_sessions.pop(user_id, None)
            await event.reply("ارسال لغو شد. برای لغو درخواست خرید از بخش اعتبار و پرداخت اقدام کنید.")
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

        # Free Office: the user's own text becomes a document.
        if await self._handle_office_message(event, user_id, text):
            return

        if _is_unsupported_attachment(event.message):
            if self.settings.office_enabled:
                await event.reply(
                    "⚠️ این نوع فایل را پشتیبانی نمی‌کنم. صدا، ویدیو یا پاورپوینت "
                    "بفرستید — یا برای ساخت سند، یک فایل ‎.txt/‎.csv بفرستید."
                )
            else:
                await event.reply(
                    "⚠️ این نوع فایل را پشتیبانی نمی‌کنم. صدا، ویدیو یا پاورپوینت بفرستید."
                )
            return
        if text:
            # The Free Office section already had its chance above: it either
            # took the text (and asked for a format) or is switched off.
            await event.reply(
                "برای ساخت جزوه، فایل صوتی یا ویدیویی درس را بفرستید؛ "
                "یا در منوی اصلی «📎 ساخت جزوه» را انتخاب کنید."
            )

    # ---------------------------------------------------------------- intake
    async def _accept_media(
        self, event, user_id: int, kind: str, filename: str | None, duration: float | None
    ) -> None:
        # Defense-in-depth: Telegram reports file.size before download; reject
        # obviously oversized uploads immediately without consuming disk/time
        # (audit F-M05). The post-download check remains as a second gate.
        reported_size = getattr(getattr(event.message, "file", None), "size", None)
        if isinstance(reported_size, int) and reported_size > self.settings.max_file_size:
            await event.reply(
                "❌ حجم فایل ارسالی از سقف مجاز ربات بیشتر است. "
                f"حد مجاز: {self.settings.max_file_size // (1024*1024)}MB"
            )
            return
        available = await self.db.available_seconds(user_id)
        # ``duration is None`` is not an exemption: an unknown duration used to
        # skip the balance check entirely, so a 2 GiB upload could be downloaded
        # from the provider with zero credit. The conservative floor is checked
        # exactly like a measured duration (audit F-4).
        needed = self._reserve_estimate(duration)
        unlimited = await self.db.is_unlimited(user_id)
        if not unlimited and available < needed:
            measured = (
                f"مدت فایل: {format_duration(duration)}"
                if duration is not None and duration > 0
                else "مدت فایل نامشخص است"
            )
            await event.reply(
                f"⏳ اعتبار شما برای این فایل کافی نیست.\n"
                f"{measured} — لازم (دست‌کم): {format_duration(needed)} — "
                f"اعتبار فعلی: {format_duration(available)}\n"
                "از «💳 اعتبار و پرداخت» می‌توانید اعتبار بگیرید."
            )
            return
        try:
            submission_id = await self.db.create_submission(
                user_id,
                kind,
                filename,
                active_limit=self.settings.max_active_jobs_per_user,
            )
        except TooManyActiveJobsError as exc:
            await event.reply(
                ACTIVE_LIMIT_MESSAGE.format(limit=to_persian_digits(exc.limit))
            )
            return
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
            submission_id, _sanitize_log_value(kind), user_id, duration,
        )

    async def _accept_presentation(
        self, event, user_id: int, kind: str, filename: str | None
    ) -> None:
        if kind == "unsupported":
            # Classified as a presentation we cannot read (ODP/OTP). Refusing
            # here keeps a queued job from failing later with a misleading
            # "no slide text and no audio" message.
            await event.reply(
                "⚠️ فرمت ODP/OTP پشتیبانی نمی‌شود؛ لطفاً ارائه را با PowerPoint "
                "به‌صورت pptx ذخیره و دوباره بفرستید."
            )
            return
        # Pre-size check mirrors _accept_media (audit F-M05)
        reported_size = getattr(getattr(event.message, "file", None), "size", None)
        if isinstance(reported_size, int) and reported_size > self.settings.max_file_size:
            await event.reply(
                "❌ حجم فایل ارسالی از سقف مجاز ربات بیشتر است. "
                f"حد مجاز: {self.settings.max_file_size // (1024*1024)}MB"
            )
            return
        # Admission control for presentations: estimate cost up-front (audit F-H02)
        # Presentations have no duration hint at intake, so we use a
        # conservative floor (600s) and the max total duration cap as worst-case.
        # This prevents a zero-credit user from queuing a multi-hour deck for
        # free and consuming media resources before the in-worker reserve.
        if not await self.db.is_unlimited(user_id):
            available = await self.db.available_seconds(user_id)
            # Use the same floor as _reserve_estimate for unknown duration.
            needed = self._reserve_estimate(None)
            if available < needed:
                await event.reply(
                    f"⏳ اعتبار شما برای این ارائه کافی نیست.\n"
                    f"لازم (حداقل برآورد): {format_duration(needed)} — "
                    f"اعتبار فعلی: {format_duration(available)}\n"
                    "از «💳 اعتبار و پرداخت» می‌توانید اعتبار بگیرید."
                )
                return
        try:
            submission_id = await self.db.create_submission(
                user_id,
                "presentation",
                filename,
                active_limit=self.settings.max_active_jobs_per_user,
            )
        except TooManyActiveJobsError as exc:
            await event.reply(
                ACTIVE_LIMIT_MESSAGE.format(limit=to_persian_digits(exc.limit))
            )
            return
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
            if await self.db.is_banned(job.user_id):
                await self._release(job, "user banned before processing")
                await self.db.set_submission_status(job.submission_id, "rejected", error="user banned")
                await self._reply(job, "⛔ درخواست صف‌شده به دلیل مسدودی حساب لغو شد.")
                return
            await self.db.set_submission_status(job.submission_id, "processing")
            self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
            workdir = Path(tempfile.mkdtemp(
                prefix=f"submission-{job.submission_id}-", dir=self.settings.temp_dir
            ))
            if job.kind == "presentation":
                await self._process_presentation(job, workdir, progress)
            elif job.kind == "office":
                await self._process_office(job, workdir, progress)
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
                "از «💳 اعتبار و پرداخت» اعتبار بگیرید."
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
        reported_size = getattr(getattr(job.message, "file", None), "size", None)
        upload_bytes = (
            reported_size
            if isinstance(reported_size, int) and reported_size > 0
            else self.settings.max_file_size
        )
        # The download and the prepared audio can both exist at once.
        _ensure_disk_headroom(
            self.settings.temp_dir,
            upload_bytes * 2,
            reserve_bytes=self.settings.temp_min_free_bytes,
        )
        await progress.update(20, "📥 دارم فایل را از تلگرام می‌گیرم…")
        try:
            await _download_media_limited(
                job.message,
                source_path,
                self.settings.max_file_size,
                stall_seconds=self.settings.download_stall_seconds,
            )
        except UploadTooLargeError as exc:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.") from exc
        if source_path.stat().st_size > self.settings.max_file_size:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")

        await progress.update(35, "🔍 فایل رسید؛ دارم صدا را بررسی می‌کنم")
        audio_path = await self._run_media_operation(
            self._prepare_audio, source_path, workdir, job.filename, job.kind
        )
        if audio_path.stat().st_size > self.settings.max_file_size:
            raise ValueError("صدای آماده‌شده از سقف حجم ربات بزرگ‌تر است.")

        info = await self._run_media_operation(probe_media, audio_path, self.settings)
        duration = info.duration or job.duration_hint
        if not duration or duration <= 0:
            # Billing.md promises an unmeasurable duration is refused, not
            # reserved at a guess and settled at zero (audit F-21).
            raise ValueError(
                "مدت این فایل مشخص نشد؛ نمی‌توانم آن را برای پیاده‌سازی بپذیرم."
            )
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
        # Persist before delivering: a Telegram outage must not lose a finished
        # transcription (the work is done, the row is what an operator disputes).
        await self.db.save_transcription(
            job.submission_id, engine=raw.engine, text=document.plain(), notes_json=""
        )
        raw_path = workdir / raw_text_filename(reference)
        raw_path.write_text(raw_text, encoding="utf-8", newline="\n")
        await self._deliver_raw(job, raw_text, raw_path, reference)
        await self._send_preview(job, raw_text)

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
        # Media preparation and the STT upload are separated by the failover
        # chain, which skips any provider whose ceiling the file exceeds.
        # Sizing the prepared audio to the *largest configured* ceiling means a
        # compact Opus track is produced when a WAV would only be skipped later.
        ceiling = largest_configured_upload(self.settings)
        budget = (
            self.settings.max_file_size
            if ceiling is None
            else min(self.settings.max_file_size, ceiling)
        )
        try:
            oversized = source_path.stat().st_size > budget
        except OSError:
            oversized = False
        if kind == "video" or needs_transcode(filename, info) or oversized:
            # Forward the measured duration so a long recording can fall back to
            # a compact Opus track instead of an oversized WAV (see
            # ``media.choose_merge_format``), and cap the result so at least one
            # configured provider can actually accept it.
            return await extract_audio_track(
                source_path,
                workdir,
                self.settings,
                total_duration=info.duration,
                max_output_bytes=budget,
            )
        return source_path

    async def _run_media_operation(self, operation, *args, **kwargs):
        """Run one CPU/disk-heavy media stage under the configured process cap."""
        async with self._media_semaphore:
            return await operation(*args, **kwargs)

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
        try:
            await _download_media_limited(
                job.message, source_path, self.settings.max_file_size
            )
        except UploadTooLargeError as exc:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.") from exc
        if source_path.stat().st_size > self.settings.max_file_size:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")
        if source_path.stat().st_size > self.settings.presentation_max_temp_bytes:
            raise PresentationError("حجم فایل موقت ارائه از سقف تعیین‌شده بیشتر است.")

        conversion_warnings: list[str] = []
        if job.meta.get("presentation_kind") == "legacy":
            await progress.update(30, "🔁 دارم قالب قدیمی را تبدیل می‌کنم…")
            source_path = await self._run_media_operation(
                convert_to_pptx,
                source_path,
                workdir,
                self.settings,
                warnings_out=conversion_warnings,
            )
            if source_path.stat().st_size > self.settings.max_file_size:
                raise PresentationError("ارائهٔ تبدیل‌شده از سقف حجم فایل بزرگ‌تر است.")

        await progress.update(40, "🔍 دارم اسلایدها را می‌خوانم")
        content = await self._run_media_operation(
            load_presentation, source_path, workdir, self.settings
        )
        outline = slides_outline(content.slides)
        # The note model is only as good as this input, so record what was
        # actually read from the deck: a deck whose text silently extracted as
        # empty is otherwise indistinguishable from a deck without text.
        logger.info(
            "presentation read submission=%s slides=%d outline_chars=%d clips=%d",
            job.submission_id, len(content.slides), len(outline), len(content.clips),
        )
        skipped: list[str] = []
        if conversion_warnings:
            # Partial media loss during a legacy conversion used to be invisible
            # whenever *some* clip survived: the user got a booklet with silent
            # slides and no explanation.
            skipped.append(
                "در تبدیل قالب قدیمی این موارد از ارائه حذف شدند: "
                + "، ".join(dict.fromkeys(conversion_warnings))
            )
        prepared = await self._run_media_operation(
            prepare_audio, content, workdir, self.settings, skipped_out=skipped
        )
        if prepared is not None:
            logger.info(
                "presentation audio prepared submission=%s source_seconds=%.3f "
                "output_seconds=%.3f clips=%d merged=%s skipped=%d",
                job.submission_id,
                prepared.source_duration,
                prepared.output_duration,
                len(prepared.clips),
                prepared.merged,
                len(prepared.skipped),
            )
        # Legacy .ppt conversion can drop embedded media (ppt2pptx warns
        # MEDIA_ACTION_OMITTED). If the deck converted but has no audio clips,
        # surface that as a skip reason so the user understands why narration
        # is missing.
        if (
            job.meta.get("presentation_kind") == "legacy"
            and not content.clips
            and outline.strip()
        ):
            # Only add the generic note when we have no other explanation.
            if not skipped:
                skipped.append(
                    "برخی رسانه‌های قالب قدیمی در تبدیل به pptx حذف شدند (MEDIA_ACTION_OMITTED)؛ "
                    "برای حفظ صدا، فایل را با PowerPoint جدید به‌صورت pptx ذخیره و دوباره بفرستید."
                )

        raw_text_parts: list[str] = []
        if outline.strip():
            raw_text_parts.append("متن اسلایدها:\n\n" + outline.strip())
        document = None
        raw_engine: str | None = None
        duration: float | None = None
        narration_text = ""
        if prepared is not None:
            # prepare_audio has already measured the merged output and retained
            # exact per-clip spans, so another probe would only add latency and
            # could return a codec/container duration that includes padding.
            if prepared.path.stat().st_size > self.settings.max_file_size:
                raise ValueError("صدای آماده‌شده از سقف حجم ربات بزرگ‌تر است.")
            duration = prepared.source_duration  # synthetic silence is not billed
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
            raw_engine = raw.engine
            alignment = align_narration_to_slides(raw, prepared, content.slides)
            narration_text = alignment.as_text()
            logger.info(
                "presentation narration aligned submission=%s aligned=%s slides=%d "
                "unassigned=%s",
                job.submission_id,
                alignment.aligned,
                len(alignment.slide_text),
                bool(alignment.unassigned_text),
            )
            raw_text_parts.append(
                "متن پیاده‌سازی‌شدهٔ صدای ارائه:\n\n" + narration_text
            )
            # Record source-audio duration early. Synthetic inter-clip gaps are
            # included in STT timestamps/output limits, not in the user's bill.
            await self.db.set_submission_status(
                job.submission_id, "processing", duration_seconds=duration
            )
        elif not outline.strip():
            raise ValueError("در این ارائه نه متن اسلایدی پیدا شد و نه صدای ضبط‌شده.")

        reference = f"GMS-{job.submission_id}"
        meta = DocumentMeta(reference=reference, source_name=job.filename, created_at=datetime.now())

        # ---- PROGRESSIVE DELIVERY ----
        # The raw text is persisted and delivered before the credit is settled,
        # exactly like the media pipeline: a delivery failure must not leave a
        # billed job without its transcript in the user's chat.
        await progress.update(70, "📝 متن خام را می‌فرستم")
        raw_text = "\n\n\n".join(part for part in raw_text_parts if part.strip())
        # Include skip reasons when any clip was dropped, so the user knows
        # why a narration file did not make it into the transcript.
        if skipped:
            raw_text += "\n\n---\nنکته: این موارد در پردازش نادیده گرفته شدند:\n" + "\n".join(f"• {reason}" for reason in skipped)
        engine_label = raw_engine or ("slides" if prepared is None else "stt")
        await self.db.save_transcription(
            job.submission_id,
            engine=engine_label,
            text=raw_text,
            notes_json="",
        )
        raw_path = workdir / raw_text_filename(reference)
        raw_path.write_text(raw_text, encoding="utf-8", newline="\n")
        await self._deliver_raw(job, raw_text, raw_path, reference)
        await self._send_preview(job, raw_text)

        # Settle only after the transcript has actually reached the user. The
        # source audio is measured by the worker; synthetic silence is excluded.
        if prepared is not None:
            await self._settle(job, prepared.source_duration)
        else:
            # A slide-only deck has no audio, so there is no hold and nothing
            # to charge — but the delivery milestone still has to be recorded,
            # otherwise the admin stats and the delivered/partial mapping miss
            # every text-only deck.
            await self._settle(job, 0)

        await progress.update(85, "✍️ دارم جزوهٔ ساختارمند را می‌سازم")
        transcript_text = narration_text
        presentation_text = build_presentation_document(outline, transcript_text)
        notes = await self._generate_presentation_notes(
            outline, transcript_text, job, progress, document=presentation_text
        )
        await self.db.save_transcription(
            job.submission_id,
            engine=engine_label,
            text=raw_text,
            notes_json=notes.to_json() if notes else "",
        )
        await self._deliver_booklet(
            job,
            workdir,
            notes,
            document,
            reference,
            meta,
            progress,
            plain_title="جزوهٔ ارائه",
            plain_text=presentation_text,
        )

    # ------------------------------------------------------- free office job
    async def _process_office(self, job: Job, workdir: Path, progress) -> None:
        """Build and deliver a Free Office document.

        This path never touches the credit ledger: the section is free, so a
        document costs the user nothing, and a failed build costs them
        nothing either. The only thing recorded is the submission row, so the
        admin panel can see how many documents were made.
        """
        assert progress is not None
        await progress.update(15, "📄 دارم سند شما را می‌سازم")
        kind = str(job.meta.get("office_kind") or "word")
        content = str(job.meta.get("office_text") or "")
        title = str(job.meta.get("office_title") or "")
        if not content.strip():
            raise OfficeError("متنی برای ساخت سند نبود.")
        document = build_office_document(content, title=title)
        reference = f"GMS-{job.submission_id}"
        meta = DocumentMeta(
            reference=reference,
            source_name=job.filename,
            created_at=datetime.now(),
        )

        if kind == "excel":
            payload = await self._build_office_excel(document, content)
        elif kind == "ppt":
            payload = await self._build_office_pptx(document, content)
        elif kind == "pdf":
            # The PDF path is fully deterministic: no model call, no network,
            # so it stays as fast and reliable as the Word path.
            payload = await asyncio.to_thread(
                build_office_pdf,
                document,
                meta=meta,
                mode_label=OFFICE_MODE_LABELS["pdf"],
            )
        else:
            payload = await asyncio.to_thread(
                build_plain_docx,
                document.title,
                document.text,
                fonts=resolve_fonts(),
                design=resolve_design(),
                meta=meta,
                mode_label=OFFICE_MODE_LABELS["word"],
                heading_offset=OFFICE_HEADING_OFFSET,
            )

        await progress.update(80, "📤 دارم فایل را می‌فرستم")
        path = workdir / office_filename(document.title, kind, reference)
        path.write_bytes(payload)
        caption = (
            f"{FORMAT_EMOJI.get(kind, '📄')} سند {FORMAT_LABELS.get(kind, '')} — {reference}"
        )
        await self._send_file(job, path, caption=caption)
        await self.db.set_submission_status(job.submission_id, "done")
        # Remember the document so «➕ افزودن متن» can extend it.
        self._office_last[job.user_id] = (kind, content, document.title, time.monotonic())
        self._trim_office_history()
        await progress.complete("سند شما آماده شد ✅", "🎉 سند آماده شد! 🎉")
        await self._offer_office_more(job)

    async def _offer_office_more(self, job: Job) -> None:
        """Offer to extend the document that was just delivered."""
        if job.message is None:
            return
        try:
            await job.message.reply(
                "می‌خواهید متن دیگری هم به همین سند اضافه کنم؟",
                buttons=[[Button.inline("➕ افزودن متن", b"office:more")]],
            )
        except Exception:
            logger.debug("could not offer the 'add more' button", exc_info=True)

    def _trim_office_history(self) -> None:
        """Keep the «last document» cache bounded.

        The cache holds the text of the last document per user, so it is
        dropped for the oldest users once it grows past the limit.
        """
        limit = 500
        while len(self._office_last) > limit:
            oldest = next(iter(self._office_last))
            self._office_last.pop(oldest, None)

    async def _build_office_excel(self, document, content: str) -> bytes:
        """The sheet: the user's own table, or a table the model arranges."""
        tables = list(document.tables)
        if not tables:
            # No grid in the text: the model may arrange the prose into one.
            # Every cell it proposes is checked against the source, and a
            # rejected plan falls back to a plain one-column sheet.
            table = await infer_table(
                content, settings=self.settings, session=self._http_session()
            )
            if table is not None:
                tables = [table]
        return await asyncio.to_thread(build_office_xlsx, document, tables)

    async def _build_office_pptx(self, document, content: str) -> bytes:
        """The deck: the model's slide order, or the user's own headings."""
        slides = await plan_deck(
            content,
            settings=self.settings,
            session=self._http_session(),
            max_sections=self.settings.office_max_slides,
        )
        if not slides:
            slides = fallback_slides(
                document, max_slides=self.settings.office_max_slides
            )
        return await asyncio.to_thread(
            build_office_pptx,
            document,
            slides,
            max_slides=self.settings.office_max_slides,
        )

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
        self,
        outline: str,
        transcript_text: str,
        job: Job,
        progress,
        *,
        document: str | None = None,
    ) -> StructuredNotes | None:
        if not self.settings.note_configured:
            return None
        if not outline.strip() and not transcript_text.strip():
            return None
        try:
            return await structure_presentation(
                outline,
                transcript_text,
                self.settings,
                self._http_session(),
                mode=self.settings.note_mode,
                document=document,
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
        plain_text: str | None = None,
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
            fallback_text = plain_text
            if fallback_text is None:
                fallback_text = document.llm_document() if document is not None else ""
            try:
                docx_bytes = await asyncio.to_thread(
                    build_plain_docx, plain_title, fallback_text,
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

        await self.db.set_submission_status(
            job.submission_id, "partial" if notice else "done", error=notice.strip() or None
        )
        if notice:
            await progress.complete("متن خام ارسال شد. " + notice.strip())
        else:
            await progress.complete()

    async def _send_file(self, job: Job, path: Path, *, caption: str = "") -> None:
        """Deliver one generated artifact as a Telegram document.

        ``TelegramClient.send_file`` is the supported API for a captioned file.
        ``Message.reply``/``send_message`` forward ``file`` to it but have **no**
        ``caption`` parameter — the caption of a file message is the ``message``
        argument — so passing ``caption`` there raises ``TypeError`` on a real
        client (the production incident this replaced).

        The destination is the upload's own chat and ``reply_to`` is that
        message's id — the same reference ``Message.reply`` passes — so the
        artifact stays threaded to the user's upload in a private chat and in a
        group alike.

        A job with no message cannot deliver anything: that is an error, not a
        silent success, because the caller settles the credit for a job only
        after its raw text has actually been delivered.
        """
        if job.message is None:
            raise RuntimeError("پیام کاربر برای ارسال فایل در دسترس نیست.")
        await self.client.send_file(
            await job.message.get_input_chat(),
            str(path),
            caption=caption[:TELEGRAM_CAPTION_LIMIT] or None,
            force_document=True,
            reply_to=job.message.id,
        )

    async def _deliver_raw(
        self, job: Job, raw_text: str, raw_path: Path, reference: str
    ) -> None:
        """Deliver the raw transcript: the ``.txt`` file first, then the text.

        The transcript is the product's core artifact, so a single per-file
        upload failure must not discard finished (and paid-for) work: the text is
        re-sent as paginated messages that respect Telegram's real message
        limits. The fallback is bounded and single-pass — the same artifact is
        never re-uploaded repeatedly — and if both paths fail the job fails with
        its reservation released, with the transcript already in the database.
        """
        try:
            await self._send_file(job, raw_path, caption=f"📝 متن خام — {reference}")
            return
        except Exception:
            logger.exception(
                "raw .txt delivery failed; falling back to text submission=%s",
                job.submission_id,
            )
        if job.message is None:
            raise RuntimeError("ارسال فایل متن خام ناموفق بود.")
        parts = raw_parts(
            raw_text,
            max_chars=self.settings.raw_fallback_file_chars,
            max_parts=self.settings.raw_fallback_max_files,
        )
        if len(parts) > 1:
            await self._deliver_raw_parts(job, parts, raw_path, reference)
            return
        if not parts:
            raise RuntimeError(
                "متن خام برای ارسال در چند قطعه بیش از حد بزرگ است؛ اعتبار مصرف نشد."
            )
        rendered = render_pages(raw_text)
        pages = [page for page in rendered[:RAW_FALLBACK_MAX_PAGES] if page.strip()]
        # Truncation and filtering are two different things: a blank rendered
        # page is not a delivery failure, but dropping whole pages past the cap
        # is. Comparing the *filtered* count with ``len(rendered)`` conflated
        # them and reported a complete delivery as failed.
        truncated = len(rendered) > RAW_FALLBACK_MAX_PAGES
        if not pages:
            raise RuntimeError("ارسال فایل متن خام ناموفق بود.")
        delivered = 0
        for page in pages:
            try:
                await self.client.send_message(
                    await job.message.get_input_chat(),
                    page,
                    parse_mode="html",
                    reply_to=job.message.id,
                )
            except Exception:
                logger.warning(
                    "raw text fallback page %d/%d failed submission=%s",
                    delivered + 1, len(pages), job.submission_id,
                    exc_info=True,
                )
                break
            delivered += 1
        if not delivered or delivered != len(pages) or truncated:
            # Partial text is not the billable delivery milestone. The stored
            # transcript remains available for support, but no debit is taken.
            raise RuntimeError("ارسال کامل متن خام ناموفق بود؛ اعتبار مصرف نشد.")
        logger.info(
            "delivered raw transcript as %d text message(s) submission=%s",
            delivered, job.submission_id,
        )

    async def _deliver_raw_parts(
        self, job: Job, parts: list[str], raw_path: Path, reference: str
    ) -> None:
        """Re-send the raw transcript as bounded ``.txt`` parts.

        Every part must arrive: a partially delivered transcript is not the
        billable milestone, and the caller refunds the job instead. Each part is
        written next to the original file and removed again immediately, so the
        workdir holds at most one extra copy at a time.
        """
        if job.message is None:
            raise RuntimeError("پیام کاربر برای ارسال فایل در دسترس نیست.")
        delivered = 0
        for index, part in enumerate(parts, start=1):
            part_path = raw_path.with_name(f"{raw_path.stem}.{index}{raw_path.suffix}")
            try:
                part_path.write_text(part, encoding="utf-8", newline="\n")
                await self._send_file(
                    job,
                    part_path,
                    caption=(
                        f"📝 متن خام ({index}/{len(parts)}) — {reference}"
                    ),
                )
            except Exception:
                logger.warning(
                    "raw text fallback part %d/%d failed submission=%s",
                    index, len(parts), job.submission_id,
                    exc_info=True,
                )
                break
            finally:
                part_path.unlink(missing_ok=True)
            delivered += 1
        if delivered != len(parts):
            # Partial text is not the billable delivery milestone.
            raise RuntimeError("ارسال کامل متن خام ناموفق بود؛ اعتبار مصرف نشد.")
        logger.info(
            "delivered raw transcript as %d file part(s) submission=%s",
            delivered, job.submission_id,
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
        balance = await self._balance_text(user_id)
        history = await self.db.payment_history(user_id, limit=5)
        plans = await self.db.plans()
        lines = [
            "💳 اعتبار و پرداخت",
            "",
            f"⏳ اعتبار باقی‌مانده: {balance}",
            "",
            "برای افزایش اعتبار، یکی از بسته‌های زیر را انتخاب کنید:",
        ]
        plan_labels = {
            plan["code"]: format_duration(int(round(float(plan["hours"]) * 3600)))
            for plan in plans
        }
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
                    "approved": "✅ تأیید شد",
                    "rejected": "❌ رد شد",
                    "cancelled": "لغو شد",
                }.get(item["status"], item["status"])
                lines.append(
                    f"• {plan_labels.get(item['plan_code'], item['plan_code'])} — {status}"
                )
                if item["status"] == "pending":
                    pending_buttons = [Button.inline(
                        "ادامهٔ درخواست باز", f"payopen:{item['payment_id']}".encode()
                    )]
                    if not item["receipt_path"]:
                        pending_buttons.append(Button.inline(
                            "لغو درخواست خرید", f"payc:{item['payment_id']}".encode()
                        ))
                    buttons.insert(0, pending_buttons)
        lines.append("")
        lines.append(f"اعتبار اولیهٔ «{FREE_PLAN_NAME}» برای همهٔ کاربران فعال می‌شود.")
        buttons.append([Button.inline("🔙 منوی اصلی", b"menu:main")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _start_payment(self, event, plan_code: str) -> None:
        if not self.settings.payment_card_number:
            # Never create a payable request the user cannot pay for: without
            # a configured card there is nowhere to send the money.
            await event.respond(
                "پرداخت کارت‌به‌کارت در حال حاضر فعال نیست؛ لطفاً بعداً دوباره امتحان کنید."
            )
            return
        try:
            payment_id = await self.db.create_payment(event.sender_id, plan_code)
        except OpenPaymentError:
            await event.respond(
                "⚠️ شما یک درخواست پرداخت باز دارید؛ از بخش اعتبار و پرداخت آن را ادامه دهید "
                "یا پیش از ارسال رسید، درخواست را لغو کنید."
            )
            return
        except ValueError:
            await event.respond("این طرح در دسترس نیست.")
            return
        await self._show_payment(event, payment_id)

    async def _show_payment(self, event, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if (payment is None or payment["user_id"] != event.sender_id
                or payment["status"] != "pending"):
            await event.respond("این درخواست فعال نیست.")
            return
        if not self.settings.payment_card_number:
            await event.respond("پرداخت کارت‌به‌کارت در حال حاضر فعال نیست؛ با مدیر تماس بگیرید.")
            return
        if not payment["purchased_seconds"] or payment["purchased_price"] is None:
            await event.respond("اطلاعات خرید کامل نیست؛ لطفاً با مدیر تماس بگیرید.")
            return
        # The request snapshot, not the mutable catalog, is the quote.
        plan = {"hours": payment["purchased_seconds"] / 3600,
                "price": payment["purchased_price"],
                "validity_days": payment["purchased_validity_days"]}
        card = format_payment_card(self.settings.payment_card_number)
        hours = plan["hours"]
        hours_text = to_persian_digits(int(hours) if float(hours).is_integer() else hours)
        text = (
            f"🧾 خرید بستهٔ {hours_text} ساعته\n\n"
            f"مبلغ قابل پرداخت: {format_toman(plan['price'])}\n"
            f"اعتبار این بسته تا {format_validity(plan['validity_days'])} معتبر است.\n\n"
            "اطلاعات واریز کارت‌به‌کارت:\n"
            f"💳 شماره کارت: {card}\n"
            f"👤 به نام: {self.settings.payment_card_holder}\n"
            f"🏦 بانک: {self.settings.payment_bank_name}\n\n"
            "پس از واریز، «واریز کردم؛ ارسال رسید» را انتخاب کنید و تصویر رسید را بفرستید. "
            "پس از بررسی و تأیید مدیر، اعتبار به حساب شما اضافه می‌شود. بررسی ممکن است چند ساعت طول بکشد."
        )
        if payment["receipt_path"]:
            text += "\n\n✅ رسید ثبت شده و در انتظار بررسی است؛ نیازی به واریز مجدد نیست."
            buttons = []
        else:
            buttons = [[Button.inline("📤 واریز کردم؛ ارسال رسید", f"payr:{payment_id}".encode())]]
        if not payment["receipt_path"]:
            buttons.append([Button.inline("لغو درخواست خرید", f"payc:{payment_id}".encode())])
        buttons.append([Button.inline("🔙 بازگشت", b"account")])
        await event.respond(text, buttons=buttons)

    async def _cancel_payment(self, event, payment_id: int) -> None:
        changed = await self.db.cancel_payment(payment_id, event.sender_id)
        self._pending_payments.pop(event.sender_id, None)
        await event.respond(
            "درخواست خرید لغو شد." if changed else
            "این درخواست قابل لغو نیست؛ رسید ارسال‌شده باید توسط مدیر بررسی شود."
        )
        await self._show_account(event)

    async def _await_receipt(self, event, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if (
            payment is None
            or payment["status"] != "pending"
            or payment["user_id"] != event.sender_id
        ):
            await event.respond("این درخواست فعال نیست.")
            return
        self._pending_payments[event.sender_id] = payment_id
        await event.respond(
            "📷 لطفاً تصویر خوانای رسید را به‌صورت عکس، همین‌جا در گفت‌وگو بفرستید.",
            buttons=[[Button.inline("↩️ بازگشت (فقط لغو ارسال تصویر)", b"account")]],
        )

    async def _handle_receipt_upload(self, event, user_id: int) -> None:
        payment_id = self._pending_payments.pop(user_id, None)
        if payment_id is None:
            return
        payment = await self.db.payment(payment_id)
        if (
            payment is None
            or payment["status"] != "pending"
            or payment["user_id"] != user_id
        ):
            await event.respond("این درخواست فعال نیست.")
            return
        if event.message.photo is None:
            return
        size = getattr(event.message.file, "size", None)
        if isinstance(size, int) and size > self.settings.max_payment_receipt_bytes:
            await event.respond("حجم تصویر رسید زیاد است؛ فشرده‌تر بفرستید.")
            self._pending_payments[user_id] = payment_id
            return
        self.settings.receipt_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        receipt_path = self.settings.receipt_dir / f"receipt-{payment_id}-{token}.jpg"
        try:
            await _download_media_limited(
                event.message, receipt_path, self.settings.max_payment_receipt_bytes
            )
        except ValueError:
            self._pending_payments[user_id] = payment_id
            await event.respond("حجم تصویر رسید زیاد است؛ فشرده‌تر بفرستید.")
            return
        except Exception:
            self._pending_payments[user_id] = payment_id
            await event.respond("دریافت رسید ناموفق بود؛ لطفاً دوباره ارسال کنید.")
            return
        # The bounded downloader checks both the live transfer and final size.
        try:
            # Decode and re-encode so an image/polyglot is never persisted as
            # an executable-looking payload, regardless of its original format.
            if not _normalize_receipt_image(
                receipt_path, max_bytes=self.settings.max_payment_receipt_bytes
            ):
                receipt_path.unlink(missing_ok=True)
                await event.respond("فرمت رسید نامعتبر است؛ فقط JPEG/PNG/WebP پذیرفته می‌شود.")
                self._pending_payments[user_id] = payment_id
                return
            receipt_path.chmod(0o600)
        except OSError:
            receipt_path.unlink(missing_ok=True)
            self._pending_payments[user_id] = payment_id
            await event.respond("ذخیرهٔ رسید ناموفق بود؛ لطفاً دوباره ارسال کنید.")
            return
        if not await self.db.set_payment_receipt(payment_id, str(receipt_path)):
            receipt_path.unlink(missing_ok=True)
            await event.respond("این درخواست دیگر فعال نیست.")
            return
        await event.respond(
            "✅ رسید ثبت شد. پس از بررسی و تأیید مدیر، اعتبار به حسابتان اضافه می‌شود."
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
            "برای بررسی رسید، وارد پنل مدیریت شوید و بخش «پرداخت‌ها» را باز کنید."
        )
        for admin_id in self.settings.admin_ids:
            try:
                await self.client.send_message(
                    admin_id, text,
                    buttons=[[Button.inline("🧾 بررسی پرداخت", b"adm:pay")]],
                )
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
        if not approved:
            # A rejection must carry a reason, so ask for it before changing
            # any row (audit F-15).
            self._pending_reject_reason[event.sender_id] = payment_id
            await self._ack(event, "برای رد پرداخت، واردکردن دلیل الزامی است.", alert=True)
            await event.respond(
                f"✍️ دلیل رد پرداخت #{to_persian_digits(payment_id)} را در پیام بعدی بنویسید. "
                "با ارسال دلیل، پرداخت رد می‌شود. برای لغو /cancel بفرستید یا دکمهٔ زیر را انتخاب کنید.",
                buttons=[[Button.inline("↩️ لغو", b"adm:pay")]],
            )
            return
        try:
            changed = await self.db.review_payment(payment_id, True, event.sender_id)
        except PaymentSnapshotError:
            logger.error("payment approval without a snapshot payment=%s", payment_id)
            await self._ack(
                event,
                "اطلاعات خرید این پرداخت کامل نیست؛ تأیید انجام نشد. لطفاً جزئیات طرح را بررسی کنید.",
                alert=True,
            )
            return
        if not changed:
            await self._ack(event, "این پرداخت قبلاً بررسی شده است.", alert=True)
        else:
            await self._ack(event, "ثبت شد.")
            # The credited amount is the purchase snapshot, never the live plan.
            seconds = int(payment["purchased_seconds"] or 0)
            user_text = (
                f"✅ پرداخت شما تأیید شد؛ {format_duration(seconds)} اعتبار به حسابتان اضافه شد."
            )
            try:
                await self.client.send_message(payment["user_id"], user_text)
            except Exception:
                logger.warning("could not notify user=%s", payment["user_id"])
        await self._show_admin_payments(event)

    async def _reject_payment_with_reason(
        self, event, payment_id: int, reason: str
    ) -> None:
        reason = reason.strip()
        if not reason:
            await event.reply("برای رد پرداخت، واردکردن دلیل الزامی است.", buttons=self._admin_menu())
            return
        payment = await self.db.payment(payment_id)
        changed = await self.db.review_payment(
            payment_id, False, event.sender_id, reason=reason
        )
        if not changed:
            await event.reply("این پرداخت قبلاً بررسی شده است.")
        else:
            await event.reply(f"پرداخت #{to_persian_digits(payment_id)} رد شد.")
            if payment is not None:
                try:
                    await self.client.send_message(
                        payment["user_id"],
                        "❌ پرداخت شما تأیید نشد. برای پیگیری، لطفاً با پشتیبانی تماس بگیرید.",
                    )
                except Exception:
                    logger.warning("could not notify user=%s", payment["user_id"])
        await self._show_admin_payments(event)

    # ----------------------------------------------------------------- admin
    async def _show_admin_overview(self, event) -> None:
        stats = await self.db.stats()
        pending = len(await self.db.pending_payments())
        text = (
            "🛠 پنل مدیریت | آمار و وضعیت\n\n"
            f"👥 کاربران ثبت‌شده: {to_persian_digits(stats['users'])}\n"
            f"✅ درخواست‌های تکمیل‌شده: {to_persian_digits(stats['submissions_done'])}\n"
            f"❌ درخواست‌های ناموفق / ناقص: {to_persian_digits(stats['submissions_failed'])}\n"
            f"📝 متن تحویل‌شده با جزوهٔ ناقص: {to_persian_digits(stats['submissions_partial'])}\n"
            f"⏳ در حال پردازش یا منتظر در صف: "
            f"{to_persian_digits(stats['submissions_active'])}\n"
            f"⏱ مدت کل فایل‌های پیاده‌سازی‌شده: "
            f"{format_duration(stats['seconds_transcribed'])}\n"
            f"💰 مجموع مبالغ پرداختیِ تأییدشده: "
            f"{format_toman(stats['toman_approved'])}\n"
            f"🧾 پرداخت‌های نیازمند بررسی: {to_persian_digits(pending)}\n\n"
            f"📥 درخواست‌های منتظر در صف فعلی: {to_persian_digits(self.queue.pending)}"
        )
        await self._edit_or_reply(event, text, self._admin_menu())

    async def _show_admin_users(self, event) -> None:
        users = await self.db.list_users(limit=25)
        total = await self.db.count_users()
        heading = (
            f"👥 فهرست کاربران — {to_persian_digits(len(users))} از "
            f"{to_persian_digits(total)} نفر"
        )
        if total > len(users):
            heading += " (۲۵ کاربر اخیر)"
        lines = [
            heading,
            "",
            "وضعیت حساب: 🚫 مسدود | ⭐ ویژه (اعتبار نامحدود) | 👤 عادی",
            "",
        ]
        for user in users:
            if user["banned"]:
                status = "🚫 مسدود"
            elif user["is_unlimited"]:
                status = "⭐ ویژه"
            else:
                status = "👤 عادی"
            balance = (
                "نامحدود" if user["is_unlimited"]
                else format_duration(user["available_seconds"])
            )
            joined = (user["joined_at"] or "")[:10] or "—"
            lines.append(
                f"{status} | شناسه: {user['telegram_id']} | اعتبار: {balance} | عضویت: {joined}"
            )
        lines.extend(
            [
                "",
                "دستورهای مدیریت (در همین گفت‌وگو ارسال کنید):",
                "• افزودن اعتبار برحسب ساعت: /grant <شناسه کاربر> <ساعت>",
                "  مثال: /grant 123456789 2",
                "• فعال‌سازی وضعیت ویژه (بدون محدودیت اعتبار؛ دلیل الزامی است):",
                "  /unlimited <شناسه کاربر> on <دلیل>",
                "• لغو وضعیت ویژه: /unlimited <شناسه کاربر> off",
                "• مسدودکردن کاربر: /ban <شناسه کاربر>",
                "• رفع مسدودی: /unban <شناسه کاربر>",
                "شناسهٔ هر کاربر در فهرست بالا نمایش داده شده است.",
            ]
        )
        await self._edit_or_reply(event, "\n".join(lines), self._admin_menu())

    async def _show_admin_payments(self, event) -> None:
        payments = await self.db.pending_payments()
        if not payments:
            await self._edit_or_reply(
                event,
                "🧾 پرداخت‌ها\n\nدر حال حاضر پرداختی برای بررسی وجود ندارد.",
                self._admin_menu(),
            )
            return
        buttons = []
        lines = [
            f"🧾 پرداخت‌های در انتظار بررسی — {to_persian_digits(len(payments))} مورد",
            "",
            "در صورت نیاز رسید را مشاهده کنید؛ سپس «تأیید پرداخت» یا «رد پرداخت» را انتخاب کنید.",
            "برای رد پرداخت، در پیام بعدی واردکردن دلیل الزامی است.",
            "",
        ]
        for payment in payments:
            number = to_persian_digits(payment["payment_id"])
            receipt_row = [Button.inline(
                "📷 مشاهدهٔ رسید", f"adm:rcpt:{payment['payment_id']}".encode()
            )]
            receipt_status = "ثبت شده ✅" if payment["receipt_path"] else "ثبت نشده ❌"
            purchased_seconds = payment.get("purchased_seconds")
            purchased_price = payment.get("purchased_price")
            package = (
                format_duration(int(purchased_seconds))
                if purchased_seconds is not None
                else payment["plan_code"]
            )
            price = (
                format_toman(int(purchased_price))
                if purchased_price is not None
                else "نامشخص"
            )
            lines.append(
                f"#{number} | شناسهٔ کاربر: {payment['user_id']} | "
                f"بسته: {package} | مبلغ: {price} | رسید: {receipt_status}"
            )
            buttons.append([
                Button.inline("✅ تأیید پرداخت", f"payv:{payment['payment_id']}:a".encode()),
                Button.inline("❌ رد پرداخت", f"payv:{payment['payment_id']}:r".encode()),
            ])
            if payment["receipt_path"]:
                buttons.append(receipt_row)
        buttons.append([Button.inline("🔙 بازگشت به پنل مدیریت", b"adm:home")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _show_receipt(self, event, data: str) -> None:
        """Send a pending payment's receipt image to the reviewing admin."""
        try:
            payment_id = int(data.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await self._ack(event, "درخواست نامعتبر است.", alert=True)
            return
        payment = await self.db.payment(payment_id)
        if payment is None:
            await self._ack(event, "این پرداخت پیدا نشد.", alert=True)
            return
        path = payment.get("receipt_path")
        # Enforce the RECEIPT_DIR boundary: a compromised payment row must not
        # make the bot serve an arbitrary server file (audit F-H01). Resolve
        # symlinks, verify the target is strictly inside receipt_dir, and
        # re-check the magic bytes before forwarding it (audit F-20).
        if (
            not path
            or not _is_safe_receipt_path(self.settings.receipt_dir, path)
            or not _normalize_receipt_image(
                Path(path), max_bytes=self.settings.max_payment_receipt_bytes
            )
        ):
            await self._ack(event, "برای این پرداخت رسید معتبری ثبت نشده است.", alert=True)
            return
        await self._ack(event)
        number = to_persian_digits(payment_id)
        buttons: list[list] = []
        if payment["status"] == "pending":
            buttons.append([
                Button.inline("✅ تأیید پرداخت", f"payv:{payment_id}:a".encode()),
                Button.inline("❌ رد پرداخت", f"payv:{payment_id}:r".encode()),
            ])
        buttons.append([Button.inline("🔙 فهرست پرداخت‌ها", b"adm:pay")])
        # The same supported file API as every other delivery, as a photo
        # (no ``force_document``): receipts are viewed in the app, not filed.
        await self.client.send_file(
            await event.get_input_chat(),
            path,
            caption=(
                f"📷 رسید پرداخت #{number}\n"
                f"شناسهٔ کاربر: {payment['user_id']} — طرح: {payment['plan_code']}"
            )[:TELEGRAM_CAPTION_LIMIT],
            buttons=buttons,
        )

    async def _show_admin_audit(self, event) -> None:
        rows = await self.db.recent_audit(limit=15)
        action_labels = {
            "grant_credit": "افزودن اعتبار",
            "unlimited_on": "فعال‌کردن کاربر ویژه",
            "unlimited_off": "لغو وضعیت کاربر ویژه",
            "user_banned": "مسدودکردن کاربر",
            "user_unbanned": "رفع مسدودی کاربر",
            "payment_approved": "تأیید پرداخت",
            "payment_rejected": "رد پرداخت",
            "broadcast": "ارسال پیام همگانی",
        }
        lines = ["🛡 گزارش فعالیت مدیران", "", "آخرین ۱۵ اقدام ثبت‌شده:"]
        if not rows:
            lines.append("هنوز فعالیتی ثبت نشده است.")
        for row in rows:
            action = action_labels.get(row["action"], row["action"].replace("_", " "))
            subject = (
                f" — شناسهٔ کاربر: {row['subject_user']}"
                if row["subject_user"] is not None
                else ""
            )
            lines.append(
                f"• {row['created_at']} — مدیر {row['admin_id']} — {action}{subject}"
            )
        await self._edit_or_reply(event, "\n".join(lines), self._admin_menu())

    ADMIN_COMMAND_COOLDOWN_SECONDS = 30.0
    BROADCAST_COOLDOWN_SECONDS = 60.0
    MAX_BROADCAST_CHARS = 20_000

    async def _check_admin_cooldown(
        self, event, action: str, cooldown_seconds: float
    ) -> bool:
        """Apply a per-admin/action cooldown to reduce accidental mass writes."""
        admin_id = event.sender_id
        if admin_id is None:
            return False
        now = time.monotonic()
        key = (admin_id, action)
        previous = self._admin_action_times.get(key)
        if previous is not None:
            remaining = cooldown_seconds - (now - previous)
            if remaining > 0:
                await event.reply(
                    f"این دستور موقتاً محدود شده است؛ {math.ceil(remaining)} ثانیه دیگر تلاش کنید.",
                    buttons=self._admin_menu(),
                )
                return False
        self._admin_action_times[key] = now
        if len(self._admin_action_times) > 1024:
            cutoff = now - max(cooldown_seconds, self.BROADCAST_COOLDOWN_SECONDS) * 2
            self._admin_action_times = {
                cached_key: timestamp
                for cached_key, timestamp in self._admin_action_times.items()
                if timestamp >= cutoff
            }
        return True

    async def _admin_grant(self, event, text: str) -> None:
        parts = text.split()
        if _command_name(text) != "/grant" or len(parts) != 3:
            await event.reply(
                "قالب درست برای افزودن اعتبار:\n"
                "/grant <شناسه کاربر> <ساعت>\n"
                "مثال: /grant 123456789 2",
                buttons=self._admin_menu(),
            )
            return
        target = _parse_telegram_user_id(parts[1])
        hours = _parse_telegram_user_id(parts[2])
        if target is None or hours is None:
            await event.reply(
                "قالب درست برای افزودن اعتبار:\n"
                "/grant <شناسه کاربر> <ساعت>\n"
                "مثال: /grant 123456789 2",
                buttons=self._admin_menu(),
            )
            return
        if self._is_admin(target):
            # No separation-of-duties escape: an admin cannot mint credit for a
            # peer (audit F-06, docs/SECURITY.md).
            await event.reply(
                "برای حفظ تفکیک دسترسی‌ها، امکان افزودن اعتبار به حساب مدیر دیگر وجود ندارد.",
                buttons=self._admin_menu(),
            )
            return
        if not (0 < hours <= 10_000):
            await event.reply(
                "تعداد ساعت باید بین ۱ و ۱۰٬۰۰۰ باشد.",
                buttons=self._admin_menu(),
            )
            return
        if not await self._check_admin_cooldown(
            event, "grant", self.ADMIN_COMMAND_COOLDOWN_SECONDS
        ):
            return
        await self.db.ensure_user(target)
        await self.db.add_credit(
            target, hours * 3600, note=f"admin grant by {event.sender_id}",
            validity_days=None, admin_id=event.sender_id,
        )
        await event.reply(
            f"✅ {format_duration(hours * 3600)} اعتبار به حساب کاربر {target} اضافه شد.",
            buttons=self._admin_menu(),
        )

    async def _admin_ban(self, event, text: str) -> None:
        """``/ban <user_id>`` / ``/unban <user_id>`` — audited, admin-only."""
        parts = text.split()
        if _command_name(text) not in {"/ban", "/unban"} or len(parts) != 2:
            await event.reply(
                "قالب درست:\n"
                "مسدودکردن: /ban <شناسه کاربر>\n"
                "رفع مسدودی: /unban <شناسه کاربر>",
                buttons=self._admin_menu(),
            )
            return
        target = _parse_telegram_user_id(parts[1])
        if target is None:
            await event.reply(
                "قالب درست:\n"
                "مسدودکردن: /ban <شناسه کاربر>\n"
                "رفع مسدودی: /unban <شناسه کاربر>",
                buttons=self._admin_menu(),
            )
            return
        banning = _command_name(text) == "/ban"
        if self._is_admin(target):
            await event.reply("حساب مدیران را نمی‌توان مسدود کرد.", buttons=self._admin_menu())
            return
        action = "ban" if banning else "unban"
        if not await self._check_admin_cooldown(
            event, action, self.ADMIN_COMMAND_COOLDOWN_SECONDS
        ):
            return
        await self.db.ensure_user(target)
        await self.db.set_banned(target, banning, admin_id=event.sender_id)
        logger.info(
            "ban flag changed user=%s banned=%s by=%s", target, banning, event.sender_id
        )
        await event.reply(
            f"🚫 حساب کاربر {target} مسدود شد."
            if banning
            else f"✅ مسدودی حساب کاربر {target} برداشته شد.",
            buttons=self._admin_menu(),
        )

    async def _admin_unlimited(self, event, text: str) -> None:
        """``/unlimited <user_id> on <reason>`` or ``/unlimited <user_id> off``."""
        parts = text.split(maxsplit=3)
        if (
            _command_name(text) != "/unlimited"
            or len(parts) < 3
            or parts[2].lower() not in ("on", "off")
        ):
            await event.reply(
                "قالب درست:\n"
                "فعال‌سازی: /unlimited <شناسه کاربر> on <دلیل>\n"
                "غیرفعال‌سازی: /unlimited <شناسه کاربر> off\n"
                "برای فعال‌سازی، دلیل را هم وارد کنید.",
                buttons=self._admin_menu(),
            )
            return
        target = _parse_telegram_user_id(parts[1])
        if target is None:
            await event.reply(
                "قالب درست:\n"
                "فعال‌سازی: /unlimited <شناسه کاربر> on <دلیل>\n"
                "غیرفعال‌سازی: /unlimited <شناسه کاربر> off\n"
                "برای فعال‌سازی، دلیل را هم وارد کنید.",
                buttons=self._admin_menu(),
            )
            return
        enable = parts[2].lower() == "on"
        reason = parts[3].strip() if len(parts) == 4 else ""
        if enable and not reason:
            await event.reply(
                "برای فعال‌کردن وضعیت کاربر ویژه، واردکردن دلیل الزامی است.",
                buttons=self._admin_menu(),
            )
            return
        if self._is_admin(target):
            # Admins are never special users; keep the list free of operators.
            await event.reply(
                "فعال‌کردن وضعیت کاربر ویژه برای حساب مدیران مجاز نیست.",
                buttons=self._admin_menu(),
            )
            return
        action = "unlimited_on" if enable else "unlimited_off"
        if not await self._check_admin_cooldown(
            event, action, self.ADMIN_COMMAND_COOLDOWN_SECONDS
        ):
            return
        await self.db.ensure_user(target)
        await self.db.set_unlimited(target, enable, admin_id=event.sender_id, reason=reason)
        logger.info("unlimited set user=%s enabled=%s by=%s", target, enable, event.sender_id)
        if enable:
            message = f"⭐ کاربر {target} به فهرست کاربران ویژه افزوده شد؛ اعتبار او نامحدود است."
        else:
            message = f"وضعیت ویژهٔ کاربر {target} لغو شد؛ از این پس استفاده از ربات به اعتبار او وابسته است."
        await event.reply(message, buttons=self._admin_menu())

    BROADCAST_THROTTLE_SECONDS = 0.05  # ~20 msg/sec (audit F-M02)
    BROADCAST_BATCH_PROGRESS_EVERY = 100

    async def _broadcast(self, event, text: str) -> None:
        text = text.strip()
        if not text:
            await event.reply("پیام همگانی خالی است؛ متن را بفرستید.", buttons=self._admin_menu())
            return
        if len(text) > self.MAX_BROADCAST_CHARS:
            await event.reply(
                f"پیام همگانی بیش از حد طولانی است (حداکثر {self.MAX_BROADCAST_CHARS} نویسه).",
                buttons=self._admin_menu(),
            )
            return
        admin_id = event.sender_id
        if admin_id is None:
            return
        if admin_id in self._broadcast_running:
            await event.reply("ارسال پیام همگانی دیگری در حال انجام است؛ کمی صبر کنید.", buttons=self._admin_menu())
            return
        if not await self._check_admin_cooldown(
            event, "broadcast", self.BROADCAST_COOLDOWN_SECONDS
        ):
            return
        self._broadcast_running.add(admin_id)
        try:
            # Ids only: the per-user balance work in list_users is unnecessary and
            # made a broadcast O(users) round-trips before the first message
            # (audit F-18).
            user_ids = await self.db.user_ids()
            # Broadcasts are plain text, not user-controlled HTML/Markdown. This
            # both preserves literal text such as ``<b>`` and removes parse-mode
            # failure/fallback ambiguity.
            pages = split_message(text)
            sent = 0
            for idx, user_id in enumerate(user_ids, 1):
                try:
                    for page in pages:
                        await self.client.send_message(user_id, page, parse_mode=None)
                        # Limit message rate per page, not merely per recipient.
                        await asyncio.sleep(self.BROADCAST_THROTTLE_SECONDS)
                    sent += 1
                except FloodWaitError as exc:
                    logger.warning("broadcast flood wait seconds=%s", exc.seconds)
                    await asyncio.sleep(min(exc.seconds, 60))
                except Exception:
                    logger.debug("broadcast to %s failed", user_id)
                if idx % self.BROADCAST_BATCH_PROGRESS_EVERY == 0:
                    logger.info("broadcast progress sent=%d/%d", sent, len(user_ids))
            await self.db.audit(admin_id, "broadcast", f"text:{len(text)} users:{sent}")
            await event.reply(
                f"📣 پیام به {to_persian_digits(sent)} کاربر رسید.",
                buttons=self._admin_menu(),
            )
        finally:
            self._broadcast_running.discard(admin_id)
            # A long-running broadcast must also wait the full cooldown after
            # completion; otherwise its start-time cooldown could expire while
            # it is still sending and allow overlapping fan-out jobs.
            self._admin_action_times[(admin_id, "broadcast")] = time.monotonic()

    # --------------------------------------------------------------- cleanup
    async def _receipt_cleanup_loop(self) -> None:
        """Delete reviewed receipts after the retention window."""
        while True:
            try:
                # Sweep first: a bot that restarts at least daily would
                # otherwise reset the 24-hour timer forever and never delete
                # an expired receipt (audit F-08).
                await self._cleanup_expired_receipts()
                await asyncio.sleep(24 * 3600)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("receipt cleanup failed", exc_info=True)

    async def _cleanup_expired_receipts(self) -> None:
        if not self.settings.receipt_dir.is_dir():
            return
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=self.settings.receipt_retention_days)
        removed: list[str] = []
        for path in self.settings.receipt_dir.glob("receipt-*.jpg"):
            # Safety: only touch files strictly inside receipt_dir (audit F-H01)
            try:
                if not _is_safe_receipt_path(self.settings.receipt_dir, path):
                    logger.warning("skipping unsafe receipt path=%s", _sanitize_log_value(path))
                    continue
                if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                    path.unlink(missing_ok=True)
                    removed.append(str(path))
            except OSError:
                continue
        if removed:
            # A deleted file must not stay referenced: the admin's «مشاهدهٔ
            # رسید» button would open nothing, and the deletion left no trace.
            cleared = await self.db.forget_receipts(removed)
            logger.info(
                "removed %d expired receipts (%d payment references cleared)",
                len(removed), cleared,
            )


# ---------------------------------------------------------------------------
# Module-level helpers (kept importable for tests)
# ---------------------------------------------------------------------------


