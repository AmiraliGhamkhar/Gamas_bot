"""Gamas Bot v4 — one-action lecture processing, Telegram delivery and library.

One file, three concerns, in order:

* **Intake** — a user sends an audio/video lecture or PowerPoint directly; no
  mode button is required. The upload is recorded and placed on the bounded
  queue with explicit back-pressure.
* **The pipeline** — download → media worker → STT → raw-text engine →
  **deliver the transcript immediately** → note generation → RTL Word booklet.
  The transcript is always the first reward and is delivered before note
  generation starts: an LLM outage can delay the booklet, never the raw text.
* **The screens** — the user panel leads with the 📚 library and 💳 wallet. Each
  delivered Telegram document is saved as a source-message reference in that
  user's library (the file itself stays in Telegram); the admin panel remains a
  separate screen tree with its own server-side callback guard.

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
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.custom.message import Message
from telethon.tl.types import MessageMediaWebPage

from .billing import (
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
    TicketStateError,
    TooManyActiveJobsError,
    utc_now,
)
from .fileio import (
    AsyncFileReader,
    AsyncFileWriter,
    run_file_io,
    run_file_io_resource,
)
from .docx_export import (
    DocumentMeta,
    build_notes_docx,
    build_plain_docx,
    jalali_date,
    notes_docx_filename,
    plain_docx_filename,
    raw_text_filename,
    resolve_design,
    resolve_fonts,
)
from .jobs import Job, JobQueue, QueueFullError
from .learning import (
    FLASHCARD_SCHEMA,
    MAX_MANUAL_BACK_CHARS,
    MAX_MANUAL_FRONT_CHARS,
    MAX_MODEL_INPUT_CHARS,
    MIN_VALID_CARDS,
    MIN_VALID_QUESTIONS,
    OPTION_LABELS,
    QUIZ_SCHEMA,
    bound_content_text,
    flashcard_system_prompt,
    parse_flashcard_payload,
    parse_quiz_payload,
    quiz_system_prompt,
)
from .llm import complete_json
from .logging_config import log_job_id
from .metrics import METRICS
from .media import (
    MediaInfo,
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
from .office import OfficeError
from .presentations import (
    PresentationError,
    align_narration_to_slides,
    classify_presentation,
    load_presentation,
    prepare_audio,
    slides_outline,
)
from .progress import JobProgress, to_persian_digits
from .stt import (
    STTConfigurationError,
    STTError,
    first_configured_stt_url,
    largest_configured_upload,
    transcribe,
)
from .telegram_text import button_label, render_pages, split_message
from .transcript import build_document, render as render_transcript
from .textnorm import normalize_display

logger = logging.getLogger(__name__)


def _max_mtproto_part_size_kb(file_size: int) -> int:
    """Return 512 KiB (MTProto's maximum part size) for all positive file sizes.

    Telethon's default ``get_appropriated_part_size`` uses 32–128 KiB parts for
    files under 400 MB, causing 4x–16x more sequential RPC round-trips during
    media downloads and document uploads.
    """
    if file_size <= 0:
        return 64
    return 512


def _configure_telethon_part_size() -> None:
    try:
        from telethon import utils as telethon_utils

        telethon_utils.get_appropriated_part_size = _max_mtproto_part_size_kb
    except Exception:
        logger.debug("could not configure Telethon MTProto part size", exc_info=True)


_configure_telethon_part_size()

AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".amr", ".au", ".flac", ".m4a", ".m4b", ".mp3",
    ".oga", ".ogg", ".opus", ".ra", ".wav", ".wma", ".webm",
}
VIDEO_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
    ".mpg", ".ts", ".webm", ".wmv",
}


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

#: Bounded failure categories written to ``submissions.error_kind`` (migration
#: 009). The admin dashboard groups a period's failures by these; a value added
#: later without a label is still shown, under its raw key.
FAILURE_KIND_LABELS_FA: dict[str, str] = {
    "input": "فایل یا درخواست نامعتبر",
    "download": "دریافت فایل از تلگرام",
    "media": "پردازش رسانه",
    "stt": "تبدیل گفتار به متن",
    "notes": "ساخت جزوه / فایل Word",
    "delivery": "ارسال به تلگرام",
    "billing": "اعتبار ناکافی",
    "database": "پایگاه‌داده",
    "cancelled": "لغو یا خاموشی",
    "capacity": "تکمیل ظرفیت صف",
    "banned": "حساب مسدود",
    "internal": "خطای داخلی",
}

#: Where a job was when it raised is the most reliable category signal for the
#: failures that do not have their own exception type (a download timeout is a
#: ``ValueError`` exactly like an unsupported file).
_FAILURE_KIND_BY_STATE: dict[str, str] = {
    "downloading": "download",
    "extracting": "media",
    "transcribing": "stt",
    "generating_notes": "notes",
    "uploading": "delivery",
}


def classify_failure(state: str, exc: BaseException) -> str:
    """Map a failed job to one reporting category (never raises).

    Explicit exception types win over the stage, because an ``STTError`` raised
    while uploading is still an STT failure, while a ``ValueError`` is a user
    input problem wherever it happens. Anything unrecognized falls back to the
    stage the job was in and finally to ``internal``.
    """
    if isinstance(exc, InsufficientBalanceError):
        return "billing"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, STTError):
        return "stt"
    if isinstance(exc, MediaToolError):
        return "media"
    if isinstance(exc, sqlite3.Error):
        return "database"
    if isinstance(exc, (ValueError, PresentationError, OfficeError)):
        return "input"
    return _FAILURE_KIND_BY_STATE.get(state, "internal")

RAW_PREVIEW_CHARS = 800
MAX_RAW_PREVIEW_LINES = 12

#: Telegram's documented caption limit for a media message (UTF-16 code units).
TELEGRAM_CAPTION_LIMIT = 1024
#: Upper bound on the paginated text fallback when a file upload fails. The raw
#: transcript is the product's core artifact, so a delivery-size failure is
#: recovered in the chat rather than thrown away — but never unboundedly.
#: This path only handles transcripts that fit one document
#: (``RAW_FALLBACK_FILE_CHARS``); a longer one is re-sent as bounded ``.txt``
#: parts instead, so reaching this cap at all means the text is pathological.
RAW_FALLBACK_MAX_PAGES = 40

#: Shown when a user already has the configured number of active submissions.
#: The balance check at intake cannot see a job queued a moment earlier, so the
#: cap is enforced where the row is created (``db.create_submission``).
ACTIVE_LIMIT_MESSAGE = (
    "⏳ شما هم‌اکنون {limit} درخواست در حال پردازش دارید. "
    "بگذارید یکی از آن‌ها تمام شود و بعد این فایل را بفرستید."
)

SUPPORT_CATEGORY_BUTTONS = {
    "payment": "💳 پرداخت",
    "technical": "⚙️ مشکل فنی",
    "subscription": "📦 اشتراک و اعتبار",
    "other": "📌 سایر موارد",
}

# ------------------------------------------------------------- learning loop
#: Shown on booklets delivered before content archiving existed.
LEARNING_LEGACY_NOTICE = (
    "متن این جزوهٔ قدیمی ذخیره نشده است.\n"
    "🧪 آزمون و 🃏 فلش‌کارت فقط برای جزوه‌های تازه کار می‌کند."
)
#: §6.1 quiz start guidance.
QUIZ_START_GUIDANCE = (
    "سؤال‌های متوسط تا سخت؛ تعداد را انتخاب کنید.\n"
    "هر پاسخ درست ۱۰ امتیاز دارد."
)
#: §6.3 empty leaderboard.
LEADERBOARD_EMPTY = (
    "🏆 لیدربورد\n\n"
    "هنوز امتیازی ثبت نشده است.\n"
    "اولین آزمون خود را از «📚 کتابخانه من» شروع کنید."
)
#: §6.5 no flashcard decks yet.
FLASHCARD_EMPTY = (
    "🃏 فلش‌کارت‌ها\n\n"
    "هنوز مجموعه‌ای نساخته‌اید.\n"
    "از یک جزوه بسازید یا کارت‌های دستی خودتان را اضافه کنید."
)
#: §6.10 the balance denial shared by quiz and AI flashcard generation.
LEARNING_NO_CREDIT = (
    "💳 اعتبار شما کافی نیست.\n"
    "از «🛒 خرید اشتراک» اعتبار بگیرید."
)
#: §6.11 every refunded generation failure.
LEARNING_GENERATION_FAILED = (
    "ساخت محتوا ناموفق بود؛ اعتبار شما کامل برگشت داده شد.\n"
    "کمی بعد دوباره امتحان کنید."
)
#: Appended to booklet captions (§6.12); the stored library title stays clean.
LEARNING_DELIVERY_CTA = (
    "\n🧪 از همین جزوه آزمون بده یا 🃏 فلش‌کارت بساز: «📚 کتابخانه من»"
)
#: Manual flashcard conversation prompts (§6.14).
FLASHCARD_ASK_FRONT = "پرسش کارت را بفرستید:"
FLASHCARD_ASK_BACK = "پاسخ کارت را بفرستید:"
#: Stale learning callbacks (old keyboards, finished sessions, foreign owners).
LEARNING_STALE_ALERT = "این آزمون دیگر معتبر نیست."
LEARNING_BUSY_ALERT = "درخواست قبلی شما در حال پردازش است؛ کمی صبر کنید."
#: Leaderboard display name when a user never exposed a first name.
LEADERBOARD_FALLBACK_NAME = "کاربر گاماس"
QUIZ_COUNTS = (5, 10, 15)
QUIZ_POINTS_PER_ANSWER = 10
SUPPORT_STATUS_LABELS = {
    "open": "🆕 باز",
    "awaiting_admin": "⏳ در انتظار پاسخ پشتیبانی",
    "awaiting_user": "💬 پاسخ پشتیبانی ثبت شد",
    "closed": "✅ بسته",
}
VIEW_LABELS = {
    "active": "🆕 فعال",
    "open": "📂 جدید",
    "wait": "⏳ منتظر پاسخ",
    "user": "💬 منتظر کاربر",
    "closed": "✅ بسته‌شده",
}
CATEGORY_FILTER_LABELS = {"all": "همه دسته‌ها", **SUPPORT_CATEGORY_BUTTONS}
_PERSIAN_CHAR_RE = re.compile(r"[\u0600-\u06FF]")
_SECRET_TOKEN_RE = re.compile(r"(?i)\b(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{8,})")


def _safe_user_error_text(exc: BaseException, fallback: str) -> str:
    """Return a clean, single-block Persian error without raw traces or secrets."""
    raw = str(exc or "").strip()
    if (
        not raw
        or "Traceback (most recent call last)" in raw
        or not _PERSIAN_CHAR_RE.search(raw)
    ):
        return fallback
    cleaned = _SECRET_TOKEN_RE.sub("[حذف شد]", raw)
    return cleaned[:400]


def _to_int(raw: str) -> int | None:
    """A callback-data integer, or ``None`` for anything that is not one."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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


def _reported_upload_bytes(message, fallback: int) -> int:
    """The upload's byte size as Telegram reported it, or *fallback*.

    ``file.size`` is absent or a thumbnail size for some uploads, so the
    configured ceiling is the honest worst case in that situation.
    """
    reported = getattr(getattr(message, "file", None), "size", None)
    return reported if isinstance(reported, int) and reported > 0 else fallback


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


#: One MTProto ``upload.getFile`` request. Telethon clamps ``request_size`` to
#: this ceiling (``telethon/client/downloads.py``), so it is also the unit of
#: the range plan below.
TELEGRAM_DOWNLOAD_CHUNK_BYTES = 512 * 1024


def _message_file_size(message) -> int | None:
    """The declared size of a message's file, or ``None`` when unknown."""
    size = getattr(getattr(message, "file", None), "size", None)
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    return size


def plan_download_ranges(total_bytes: int, connections: int) -> list[tuple[int, int]]:
    """Split a file into ``(offset, chunk_count)`` ranges Telethon fetches directly.

    Telethon takes its *direct* iterator (one request per chunk) only when the
    offset is a multiple of 4 KiB and ``offset % limit == 0``, where ``limit`` is
    the number of chunks — otherwise it falls back to the re-aligning generic
    iterator, which costs extra requests per yield. Every range count here is a
    power of two (so it divides the 512 KiB chunk size) and every offset is a
    multiple of that count, which keeps all of them on the direct path.

    The first range absorbs the remainder so a 10-chunk file split three ways is
    ``(4, 3, 3)`` rather than ``(3, 3, 4)``: offset ``0`` is divisible by any
    count, later offsets only by their own power-of-two count.
    """
    chunk = TELEGRAM_DOWNLOAD_CHUNK_BYTES
    total_chunks = max(1, math.ceil(total_bytes / chunk))
    wanted = max(1, min(int(connections), total_chunks))
    per = 1
    while per * 2 <= max(1, total_chunks // wanted):
        per *= 2
    if wanted == 1:
        return [(0, total_chunks)]
    head = total_chunks - per * (wanted - 1)
    ranges = [(0, head)]
    for index in range(1, wanted):
        ranges.append(((head + (index - 1) * per) * chunk, per))
    return ranges


async def _download_media_ranges(
    client,
    message,
    target: Path,
    limit_bytes: int,
    *,
    connections: int,
    stall_seconds: float,
) -> None:
    """Download one file as concurrent byte ranges, each writing its own region.

    ``os.pwrite`` is positional, so the ranges never coordinate and the file is
    complete once every task is done. Each blocking filesystem operation runs
    in the shared executor; descriptor acquisition and close are cancellation-safe.
    """
    size = _message_file_size(message)
    if size is None:
        raise RuntimeError("telegram did not report a file size")
    if size > limit_bytes:
        raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
    ranges = plan_download_ranges(size, connections)
    last_progress = [time.monotonic()]
    written = [0]
    descriptor = await run_file_io_resource(
        os.open,
        os.close,
        target,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )

    async def fetch(offset: int, chunks: int) -> None:
        position = offset
        async for piece in client.iter_download(
            message.media,
            offset=offset,
            limit=chunks,
            request_size=TELEGRAM_DOWNLOAD_CHUNK_BYTES,
            file_size=size,
        ):
            payload = bytes(piece)
            await run_file_io(os.pwrite, descriptor, payload, position)
            position += len(payload)
            written[0] += len(payload)
            last_progress[0] = time.monotonic()
            if written[0] > limit_bytes:
                raise UploadTooLargeError(
                    f"file exceeds limit ({limit_bytes} bytes)"
                )

    tasks = [
        asyncio.create_task(fetch(offset, chunks)) for offset, chunks in ranges
    ]
    try:
        if stall_seconds > 0:
            tick = max(0.05, min(stall_seconds / 4, 15.0))
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, timeout=tick, return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    error = finished.exception()
                    if error is not None:
                        raise error
                if pending and time.monotonic() - last_progress[0] > stall_seconds:
                    raise DownloadStalledError(
                        "دانلود فایل از تلگرام متوقف شد؛ لطفاً دوباره بفرستید."
                    )
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await run_file_io(os.close, descriptor)
    actual_size = (await run_file_io(target.stat)).st_size
    if actual_size != size:
        # A short write would otherwise be "some file that plays".
        raise RuntimeError(
            f"ranged download produced {actual_size} of {size} bytes"
        )
    logger.info(
        "ranged download done bytes=%d connections=%d ranges=%d",
        size, connections, len(ranges),
    )


async def _download_media_limited(
    message,
    target: Path,
    limit_bytes: int,
    *,
    stall_seconds: float = 0.0,
    client=None,
    connections: int = 1,
) -> Path:
    """Download Telegram media with size, stall and event-loop I/O bounds.

    Known-size files use Telethon's async byte-range iterator on POSIX, even
    with one connection; each disk write runs in the bounded thread pool. If
    that optimization is unavailable or fails, Telethon receives an async
    file-like writer instead of a path, so its normal synchronous ``write``
    path cannot block the event loop. Minimal test clients without
    ``iter_download`` retain the ordinary path API.
    """
    if limit_bytes <= 0:
        raise ValueError("download limit must be positive")
    declared_size = _message_file_size(message)
    if declared_size is not None and declared_size > limit_bytes:
        raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")

    supports_async_download = client is not None and callable(
        getattr(client, "iter_download", None)
    )
    ranged = (
        supports_async_download
        and connections >= 1
        and hasattr(os, "pwrite")
        and declared_size is not None
    )
    if ranged:
        try:
            await _download_media_ranges(
                client,
                message,
                target,
                limit_bytes,
                connections=connections,
                stall_seconds=stall_seconds,
            )
            return target
        except (UploadTooLargeError, DownloadStalledError):
            await run_file_io(target.unlink, missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 - any surprise means one stream
            # A ranged transfer is an optimization, never a prerequisite: prove
            # it works on this host, otherwise the async single-stream path runs.
            logger.warning(
                "ranged download failed (%s: %s); retrying single-stream",
                type(exc).__name__, exc,
            )
            await run_file_io(target.unlink, missing_ok=True)

    last_progress = [time.monotonic()]

    def _guard(received: int, total: int) -> None:
        # Telethon reports (received, total); total can be zero for an unknown
        # size. Check both the declaration and running byte count.
        last_progress[0] = time.monotonic()
        if total and total > limit_bytes:
            raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
        if received > limit_bytes:
            raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")

    writer = await AsyncFileWriter.open(target) if supports_async_download else None
    file_target = writer if writer is not None else str(target)
    task: asyncio.Task | None = None
    try:
        async def _download():
            return await message.download_media(
                file=file_target, progress_callback=_guard
            )

        task = asyncio.create_task(_download())
        if stall_seconds > 0:
            tick = max(0.05, min(stall_seconds / 4, 15.0))
            while True:
                done, _pending = await asyncio.wait((task,), timeout=tick)
                if done:
                    break
                idle = time.monotonic() - last_progress[0]
                if idle > stall_seconds:
                    raise DownloadStalledError(
                        "دانلود فایل از تلگرام متوقف شد؛ لطفاً دوباره بفرستید."
                    )
        downloaded = await task
        try:
            file_stat = await run_file_io(target.stat)
        except OSError as exc:
            raise RuntimeError("download did not produce a file") from exc
        if not downloaded or not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError("download did not produce a file")
        if file_stat.st_size > limit_bytes:
            raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
        return target
    except BaseException:
        # This includes cancellation, a stalled transfer and progress-callback
        # failures: never keep a partial upload in private storage.
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if writer is not None:
            try:
                await writer.aclose()
            except OSError:
                pass
            writer = None
        try:
            await run_file_io(target.unlink, missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if writer is not None:
            await writer.aclose()


# ---------------------------------------------------------------------------
# Intake classification helpers
# ---------------------------------------------------------------------------


def _media_metadata(message) -> tuple[str | None, str | None, float | None]:
    """Classify an incoming message as ('audio' | 'video' | None, name, duration)."""
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
        return None, None, None
    duration = getattr(file_obj, "duration", None)
    if duration is None and message.voice:
        duration = getattr(message.voice, "duration", None)
    return kind, filename, float(duration) if duration else None


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


#: Payment states that still accept a receipt, a cancel or a review. The legacy
#: ``pending`` value is read as an open state for rows written before the split.
OPEN_PAYMENT_STATUSES = frozenset({"awaiting_payment", "pending_review", "pending"})


def is_open_payment(status: object) -> bool:
    return str(status) in OPEN_PAYMENT_STATUSES


def is_pending_payment_review(payment: dict) -> bool:
    """Whether an order has a receipt and can receive an admin decision."""
    return (
        str(payment.get("status")) in {"pending_review", "pending"}
        and bool(payment.get("receipt_path"))
    )


RECEIPT_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def _is_receipt_image(message) -> bool:
    """Accept Telegram photos and image documents supported by the normalizer."""
    if getattr(message, "sticker", None) is not None or getattr(message, "gif", None) is not None:
        return False
    file_obj = getattr(message, "file", None)
    mime = (getattr(file_obj, "mime_type", "") or "").lower()
    if getattr(message, "photo", None) is not None:
        return mime == "" or mime.startswith("image/")
    if getattr(message, "document", None) is None:
        return False
    if mime in {"image/jpeg", "image/png", "image/webp"}:
        return True
    filename = getattr(file_obj, "name", None) or ""
    return mime in {"", "application/octet-stream"} and (
        Path(filename).suffix.lower() in RECEIPT_IMAGE_EXTENSIONS
    )


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
        self._download_semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._media_semaphore = asyncio.Semaphore(settings.media_worker_concurrency)
        self._upload_semaphore = asyncio.Semaphore(settings.max_concurrent_uploads)
        self._seen_messages: dict[tuple[int, int], float] = {}
        self._active_upload_keys: dict[tuple[int, str], str] = {}
        self._receipt_cleanup_task: asyncio.Task | None = None
        self._transcript_cleanup_task: asyncio.Task | None = None
        self._prewarm_task: asyncio.Task | None = None
        self._pending_payments: dict[int, int] = {}  # user_id -> payment_id awaiting receipt
        self._pending_broadcast: set[int] = set()
        # user_id -> ("new", category) or ("reply", ticket_id) awaiting the next text.
        self._support_states: dict[int, tuple[str, object]] = {}
        # admin_id -> ticket_id awaiting a reply text (set only by a verified admin callback).
        self._support_admin_states: dict[int, int] = {}
        # admin_id -> payment_id awaiting a rejection reason
        self._pending_reject_reason: dict[int, int] = {}
        # user_id -> (deck_id, phase) while a manual flashcard conversation is armed
        self._pending_flashcard: dict[int, tuple[int, str]] = {}
        # user_id -> front text waiting for its back in the manual-card flow
        self._pending_flashcard_front: dict[int, str] = {}
        # Per-user generation guards: a second quiz/deck tap while the LLM is
        # running must not deduct twice (the DB session row does not exist yet
        # during generation, so the guard has to live in process memory).
        self._learning_locks: dict[int, asyncio.Lock] = {}
        self._learning_in_flight: set[int] = set()
        self._admin_action_times: dict[tuple[int, str], float] = {}
        self._broadcast_running: set[int] = set()
        self._started_at = time.time()
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
        try:
            self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("could not prepare Telegram session directory")

    async def run(self) -> None:
        await run_file_io(self._prepare_private_dirs)
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
        await run_file_io(self._sweep_stale_temp_dirs)
        self.queue.ensure_workers()
        self._receipt_cleanup_task = asyncio.create_task(self._receipt_cleanup_loop())
        self._transcript_cleanup_task = asyncio.create_task(
            self._transcript_cleanup_loop()
        )
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
            if not entry.name.startswith(("submission-", "deck-")):
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
        background = [
            task
            for task in (
                self._transcript_cleanup_task,
                self._receipt_cleanup_task,
                self._prewarm_task,
            )
            if task is not None
        ]
        for task in background:
            task.cancel()
        if background:
            # Await the cancellation *before* the database closes: a sweep
            # cancelled mid-transaction must finish its rollback against a live
            # connection, not a closed one.
            await asyncio.gather(*background, return_exceptions=True)
        self._transcript_cleanup_task = None
        self._receipt_cleanup_task = None
        self._prewarm_task = None

        async def _fail_queued(job) -> None:
            # Audit F-H04: queued jobs never reached a worker; mark them failed
            # so recover_interrupted is not the only path and users are told.
            try:
                await self.db.set_submission_status(
                    job.submission_id, "failed", error="interrupted by restart",
                    error_kind="cancelled",
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
        """Warm connections to the primary LLM and STT hosts (no quota used)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # Keep a reference: an unreferenced task can be garbage-collected
        # before it runs, and shutdown waits for it explicitly. A warm-up that
        # is still in flight is not started a second time.
        if self._prewarm_task is None or self._prewarm_task.done():
            self._prewarm_task = loop.create_task(self._prewarm_llm())

    async def _prewarm_llm(self) -> None:
        session = self._http
        if session is None:
            return
        urls: list[str] = []
        try:
            routes = note_routes(self.settings)
            if routes:
                urls.append(routes[0].base_url)
            stt_url = first_configured_stt_url(self.settings)
            if stt_url and stt_url not in urls:
                urls.append(stt_url)
        except Exception:
            logger.debug("HTTP pre-warm route lookup failed (ignored)", exc_info=True)
            return
        if not urls:
            return

        async def _warm_url(url: str) -> None:
            try:
                logger.debug("pre-warming HTTP connection host=%s", url)
                async with session.head(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    await response.read()
            except Exception:
                logger.debug("HTTP connection pre-warm failed (ignored)", exc_info=True)

        await asyncio.gather(*(_warm_url(url) for url in urls))

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
                logger.exception("callback failed")
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
        """The user panel's two value loops: saved artifacts and account credit.

        Admins get one extra row to their separate panel; every other user sees
        this same four-button menu, regardless of provider configuration.
        """
        rows = [
            [Button.inline("📚 کتابخانه من", b"library"), Button.inline("🛒 خرید اشتراک", b"wallet")],
            [Button.inline("🏆 لیدربورد", b"board"), Button.inline("🎧 پشتیبانی", b"support")],
            [Button.inline("ℹ️ راهنما", b"help")],
        ]
        if is_admin:
            rows.append([Button.inline("🛠 پنل مدیریت", b"adm:home")])
        return rows

    def _admin_menu(self) -> list:
        """The admin panel: section tabs plus the way back to the user panel.

        Rendered on every admin screen so the panel behaves like a tab bar —
        one tap from any section to any other, and one tap home.
        """
        return [
            [
                Button.inline("📊 آمار و وضعیت", b"adm:home"),
                Button.inline("👥 کاربران", b"adm:users"),
            ],
            [
                Button.inline("💳 پرداخت‌های در انتظار", b"adm:pay"),
                Button.inline("📋 تاریخچه پرداخت‌ها", b"adm:ph:0"),
            ],
            [Button.inline("🎧 مدیریت پشتیبانی", b"adm:sup")],
            [
                Button.inline("📣 ارسال پیام", b"adm:bcast"),
                Button.inline("🛡 گزارش فعالیت‌ها", b"adm:audit"),
            ],
            [Button.inline("🏆 برترین کاربران", b"adm:board")],
            [Button.inline("↩️ پنل کاربری", b"menu:main")],
        ]

    async def _show_main(self, event, *, first_start: bool = False) -> None:
        user_id = event.sender_id
        await self.db.ensure_user(user_id)
        if await self.db.grant_free_plan_once(
            user_id, self._free_plan_seconds()
        ):
            logger.info("free plan granted")
        greeting = "🎓 به گاماس خوش آمدید" if first_start else "منوی اصلی"
        upload_line = "همین‌جا فایل درس‌تان را بفرستید: 🎧 صوت/ویدیو یا 📄 پاورپوینت."
        if self.settings.note_configured:
            delivery = "متن گفتار، سپس 📄 جزوهٔ ورد می‌آید؛ در «📚 کتابخانه من» ذخیره می‌شود."
        else:
            delivery = "متن گفتار می‌آید؛ ساخت جزوهٔ ورد فعلاً فعال نیست."
        text = (
            f"{greeting}\n\n"
            f"{upload_line}\n{delivery}\n\n"
            f"💳 اعتبار باقی‌مانده: {await self._balance_text(user_id)}"
        )
        await self._edit_or_reply(event, text, self._user_menu(is_admin=self._is_admin(user_id)))

    async def _edit_or_reply(self, event, text: str, buttons=None) -> None:
        """Render a screen: edit when triggered by a button, reply when by a message."""
        if hasattr(event, "data"):  # a CallbackQuery event
            await self._edit(event, text, buttons)
            return
        await event.reply(text, buttons=buttons)

    def _is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.settings.admin_ids

    async def _capture_sender_name(self, event) -> None:
        """Best-effort leaderboard name capture; a lookup failure is invisible.

        Telethon only guarantees ``sender_id`` on every event; ``sender`` may
        be uncached and ``get_sender()`` is a network call that can fail. The
        leaderboard shows first names only and falls back to «کاربر گاماس», so
        this path must never break message dispatch (and is skipped entirely on
        test doubles without a sender).
        """
        user_id = event.sender_id
        if user_id is None:
            return
        try:
            sender = getattr(event, "sender", None)
            if sender is None and hasattr(event, "get_sender"):
                sender = await event.get_sender()
            first_name = getattr(sender, "first_name", None)
            if first_name:
                await self.db.set_first_name(user_id, first_name)
        except Exception:
            logger.debug("could not capture sender name", exc_info=True)

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

    async def _show_library(self, event, *, page: int = 0) -> None:
        """Show six newest Telegram artifacts; the user's id scopes every read."""
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 6
        count = await self.db.library_count(user_id)
        page = min(max(0, int(page)), max(0, (count - 1) // page_size))
        items = await self.db.library_list(
            user_id, limit=page_size, offset=page * page_size
        )
        if count == 0:
            text = (
                "📚 کتابخانه من\n\n"
                "کتابخانهٔ شما خالی است.\n"
                "اولین فایل درس را بفرستید؛ متن و جزوه به‌صورت خودکار اینجا ذخیره می‌شوند."
            )
        else:
            lines = [
                "📚 کتابخانه من",
                "",
                f"تعداد موارد: {to_persian_digits(count)}",
            ]
            buttons: list[list] = []
            for item in items:
                title = " ".join(str(item.get("title") or "").split())
                if not title:
                    title = "جزوهٔ Word" if item["kind"] == "booklet" else "متن گفتار"
                label = title if title.startswith("📖 ") else f"📖 {title}"
                buttons.append(
                    [
                        Button.inline(
                            button_label(label),
                            f"lib:it:{item['item_id']}".encode("ascii"),
                        )
                    ]
                )
            nav = []
            if page > 0:
                nav.append(Button.inline("◀️ قبلی", f"lib:{page - 1}".encode("ascii")))
            if (page + 1) * page_size < count:
                nav.append(Button.inline("بعدی ▶️", f"lib:{page + 1}".encode("ascii")))
            if nav:
                buttons.append(nav)
            buttons.extend(
                [
                    [
                        Button.inline("📖 جزوه‌ها", b"library"),
                        Button.inline("🃏 فلش‌کارت‌ها", b"library:fc"),
                    ],
                    [Button.inline("🛒 خرید اشتراک", b"wallet")],
                    [Button.inline("↩️ منوی اصلی", b"menu:main")],
                ]
            )
            await self._edit_or_reply(event, "\n".join(lines), buttons)
            return
        await self._edit_or_reply(
            event,
            text,
            [
                [
                    Button.inline("📖 جزوه‌ها", b"library"),
                    Button.inline("🃏 فلش‌کارت‌ها", b"library:fc"),
                ],
                [Button.inline("🛒 خرید اشتراک", b"wallet")],
                [Button.inline("↩️ منوی اصلی", b"menu:main")],
            ],
        )

    async def _show_library_item(self, event, item_id: int) -> None:
        """Booklet/transcript detail: re-download plus learning entry points."""
        user_id = event.sender_id
        if user_id is None:
            return
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None:
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        title = self._item_display_title(item)
        try:
            date_label = jalali_date(datetime.fromisoformat(item["created_at"]))
        except (TypeError, ValueError):
            date_label = ""
        header = f"📖 {title}" + (f"\n📅 {date_label}" if date_label else "")
        buttons: list[list] = [
            [Button.inline(
                "📥 دریافت مجدد", f"lib:f:{item_id}".encode("ascii")
            )],
        ]
        if item["kind"] == "booklet":
            if item.get("content_text"):
                buttons.append([Button.inline(
                    "🧪 آزمون از این جزوه",
                    f"quiz:new:{item_id}".encode("ascii"),
                )])
                buttons.append([Button.inline(
                    "🃏 فلش‌کارت از این جزوه",
                    f"fc:ai:{item_id}".encode("ascii"),
                )])
            else:
                header += "\n\n" + LEARNING_LEGACY_NOTICE
        buttons.append([Button.inline("↩️ بازگشت", b"lib:0")])
        await self._edit(event, header, buttons)

    async def _forward_library_item(self, event, user_id: int, item_id: int) -> None:
        """Re-forward one artifact only after an owner-scoped lookup."""
        item = await self.db.library_item(user_id, item_id)
        if item is None:
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        try:
            if item["unavailable"]:
                raise RuntimeError("Telegram source was already marked unavailable")
            await self.client.forward_messages(
                event.chat_id,
                [int(item["message_id"])],
                from_peer=int(item["chat_id"]),
            )
        except Exception:
            try:
                await self.db.library_mark_unavailable(item_id)
            except Exception:
                logger.warning(
                    "could not mark unavailable library item=%s", item_id, exc_info=True
                )
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)

    # --------------------------------------------------------------- learning
    def _learning_lock(self, user_id: int) -> asyncio.Lock:
        """The per-user lock serialising quiz/deck generation in this process.

        The instance lock guarantees one bot process, so an in-memory lock is
        sufficient; the database constraints remain the durable defence.
        """
        lock = self._learning_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._learning_locks[user_id] = lock
        return lock

    def _learning_back_button(self, item_id: int):
        return Button.inline(
            "↩️ بازگشت", f"lib:it:{item_id}".encode("ascii")
        )

    async def _quiz_start_screen(self, event, user_id: int, item_id: int) -> None:
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        if not item.get("content_text"):
            await self._edit(
                event,
                "🧪 آزمون از جزوه\n\n" + LEARNING_LEGACY_NOTICE,
                [[Button.inline("↩️ بازگشت", b"library")]],
            )
            return
        per_question = self.settings.quiz_cost_seconds_per_question
        buttons: list[list] = []
        for count in QUIZ_COUNTS:
            label = button_label(
                f"{to_persian_digits(count)} سؤال — "
                f"{format_duration(count * per_question)} اعتبار"
            )
            buttons.append([
                Button.inline(
                    label, f"quiz:go:{item_id}:{count}".encode("ascii")
                )
            ])
        buttons.append([self._learning_back_button(item_id)])
        await self._edit(
            event, "🧪 آزمون از جزوه\n\n" + QUIZ_START_GUIDANCE, buttons
        )

    async def _quiz_generate(
        self, event, user_id: int, item_id: int, count: int
    ) -> None:
        if count not in QUIZ_COUNTS:
            await self._ack(event, "تعداد سؤال نامعتبر است.", alert=True)
            return
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        content = item.get("content_text")
        if not content:
            await self._ack(event, LEARNING_LEGACY_NOTICE, alert=True)
            return
        if not self.settings.note_configured:
            await event.respond("سرویس ساخت آزمون در حال حاضر فعال نیست.")
            return
        # Synchronous gate: no await between the membership test and the add,
        # so two queued callbacks can never both pass before generation ends.
        if user_id in self._learning_in_flight:
            # Second tap while the first generation is still charging/awaiting
            # the model: reject before any second debit.
            await self._ack(event, LEARNING_BUSY_ALERT, alert=True)
            return
        self._learning_in_flight.add(user_id)
        try:
            async with self._learning_lock(user_id):
                await self._quiz_generate_locked(
                    event, user_id, item_id, count, content
                )
        finally:
            # Discard even when cancellation strikes while waiting on the lock.
            self._learning_in_flight.discard(user_id)

    async def _quiz_generate_locked(
        self,
        event,
        user_id: int,
        item_id: int,
        count: int,
        content: str,
    ) -> None:
        reason = await self.db.quiz_admission(
            user_id,
            max_active_hours=self.settings.quiz_max_active_hours,
            max_sessions_per_hour=self.settings.quiz_max_sessions_per_hour,
        )
        if reason == "active":
            await event.respond(
                "یک آزمون فعال دیگر دارید؛ ابتدا آن را تمام کنید یا "
                "دکمهٔ «پایان آزمون» را بزنید."
            )
            return
        if reason == "rate":
            await event.respond(
                "تعداد آزمون‌های شما در یک ساعت اخیر به حداکثر مجاز "
                "رسیده است؛ کمی بعد دوباره تلاش کنید."
            )
            return
        cost = count * self.settings.quiz_cost_seconds_per_question
        note = f"quiz-pending:{item_id}"
        try:
            charged = await self.db.spend_seconds(user_id, cost, note)
        except InsufficientBalanceError:
            await event.respond(
                LEARNING_NO_CREDIT,
                buttons=[[Button.inline("🛒 خرید اشتراک", b"wallet")]],
            )
            return
        placeholder = await event.respond("در حال ساخت سؤال‌ها… ⏳")
        session_id: int | None = None
        try:
            model_input = bound_content_text(
                content, limit=MAX_MODEL_INPUT_CHARS
            )
            payload = await complete_json(
                self._http_session(),
                self.settings,
                system=quiz_system_prompt(count),
                user=model_input,
                schema=QUIZ_SCHEMA,
            )
            questions = parse_quiz_payload(payload, count)
            if len(questions) < MIN_VALID_QUESTIONS:
                raise RuntimeError("quiz generation yielded too few questions")
            session_id = await self.db.quiz_create_session(
                user_id, item_id, questions
            )
        except BaseException as exc:
            # Every failure path after the debit refunds in full: LLM errors,
            # malformed/short output, DB errors and even cancellation during
            # shutdown. Unlimited users were never charged (charged=False).
            if charged:
                try:
                    await self.db.refund_seconds(user_id, cost, note)
                except Exception:
                    logger.exception(
                        "quiz refund failed user=%s cost=%s", user_id, cost
                    )
            try:
                await placeholder.edit(LEARNING_GENERATION_FAILED)
            except Exception:
                logger.warning("could not edit quiz placeholder", exc_info=True)
            if isinstance(
                exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
            logger.warning("quiz generation failed", exc_info=exc)
            return
        await self._render_question_message(placeholder, user_id, session_id)

    def _question_keyboard(self, session_id: int, question_id: int) -> list[list]:
        option_row = [
            Button.inline(
                OPTION_LABELS[index],
                f"quiz:ans:{session_id}:{question_id}:{index}".encode("ascii"),
            )
            for index in range(4)
        ]
        return [
            option_row,
            [Button.inline(
                "⏹ پایان آزمون", f"quiz:quit:{session_id}".encode("ascii")
            )],
        ]

    async def _render_question_message(
        self, message, user_id: int, session_id: int
    ) -> None:
        """Render the current question into an editable Message-like object."""
        question = await self.db.quiz_current_question(session_id)
        if question is None:
            await self._finish_after_question(message, user_id, session_id)
            return
        number = int(question["idx"]) + 1
        lines = [
            f"سؤال {to_persian_digits(number)} از "
            f"{to_persian_digits(question['question_count'])}",
            "",
            str(question["question"]),
            "",
        ]
        for index, option in enumerate(question["options"]):
            lines.append(f"{OPTION_LABELS[index]}) {option}")
        await message.edit(
            "\n".join(lines),
            buttons=self._question_keyboard(session_id, int(question["question_id"])),
        )

    async def _finish_after_question(
        self, message, user_id: int, session_id: int
    ) -> None:
        session = await self.db.quiz_get_session(session_id, user_id)
        if session is None:
            await message.edit(LEARNING_STALE_ALERT)
            return
        await self._show_finish_screen(message, user_id, session)

    async def _show_finish_screen(self, message, user_id: int, session) -> None:
        """The §6.2 celebration screen for a quiz answered to its last question."""
        rank = await self.db.leaderboard_rank(user_id)
        correct = int(session["score"]) // QUIZ_POINTS_PER_ANSWER
        text = (
            "🎉 آزمون تمام شد!\n\n"
            f"پاسخ‌های درست: {to_persian_digits(correct)} از "
            f"{to_persian_digits(session['question_count'])}\n"
            f"امتیاز کسب‌شده: {to_persian_digits(session['score'])}\n"
            "رتبهٔ شما در لیدربورد: "
            f"{to_persian_digits(rank['rank'])}"
        )
        await message.edit(text, buttons=self._learning_done_buttons())

    def _learning_done_buttons(self) -> list[list]:
        return [
            [Button.inline("🏆 لیدربورد", b"board")],
            [Button.inline("📚 کتابخانه من", b"library")],
            [Button.inline("↩️ منوی اصلی", b"menu:main")],
        ]

    async def _quiz_answer(
        self,
        event,
        user_id: int,
        session_id: int,
        question_id: int,
        option: int,
    ) -> None:
        session = await self.db.quiz_get_session(session_id, user_id)
        if session is None:
            await self._ack(event, LEARNING_STALE_ALERT, alert=True)
            return
        result = await self.db.quiz_record_answer(session_id, question_id, option)
        if result["status"] == "stale":
            await self._ack(event, LEARNING_STALE_ALERT, alert=True)
            return
        if result["status"] == "already":
            # Friendly re-ack on a double tap: no second answer row, no points.
            await self._ack(event, "پاسخ شما ثبت شده بود.")
            return
        correct_text = result["options"][result["correct_index"]]
        if result["correct"]:
            lines = [f"✅ پاسخ درست: {correct_text}"]
        else:
            lines = [
                "❌ پاسخ شما درست نبود.",
                f"✅ پاسخ درست: {correct_text}",
            ]
        if result["explanation"]:
            lines.append(f"💡 {result['explanation']}")
        next_label = (
            "مشاهدهٔ نتیجه ➡️" if result["finished"] else "سؤال بعدی ➡️"
        )
        await self._edit(
            event,
            "\n".join(lines),
            [[Button.inline(
                next_label, f"quiz:next:{session_id}".encode("ascii")
            )]],
        )

    async def _quiz_next(self, event, user_id: int, session_id: int) -> None:
        session = await self.db.quiz_get_session(session_id, user_id)
        if session is None:
            await self._ack(event, LEARNING_STALE_ALERT, alert=True)
            return
        if session["status"] == "done":
            await self._show_finish_screen(event, user_id, session)
            return
        if session["status"] != "active":
            # quit/expired: the partial summary, points already earned stay.
            await self._show_quiz_summary(event, user_id, session)
            return
        await self._render_question_message(event, user_id, session_id)

    async def _quiz_quit(self, event, user_id: int, session_id: int) -> None:
        session = await self.db.quiz_get_session(session_id, user_id)
        if session is None:
            await self._ack(event, LEARNING_STALE_ALERT, alert=True)
            return
        if session["status"] == "active":
            await self.db.quiz_finish(session_id, "quit")
            session = await self.db.quiz_get_session(session_id, user_id)
        await self._show_quiz_summary(event, user_id, session)

    async def _show_quiz_summary(self, event, user_id: int, session) -> None:
        """Partial (quit) or terminal summary; earned points are never clawed back."""
        answered = min(int(session["current_index"]), int(session["question_count"]))
        correct = int(session["score"]) // QUIZ_POINTS_PER_ANSWER
        rank = await self.db.leaderboard_rank(user_id)
        text = (
            "⏹ آزمون پایان یافت.\n\n"
            f"پاسخ‌های درست: {to_persian_digits(correct)} از "
            f"{to_persian_digits(answered)}\n"
            f"امتیاز کسب‌شده: {to_persian_digits(session['score'])}\n"
            f"رتبهٔ شما در لیدربورد: {to_persian_digits(rank['rank'])}"
        )
        await self._edit(event, text, self._learning_done_buttons())

    # -------------------------------------------------------------- leaderboard
    async def _show_leaderboard(self, event) -> None:
        """All-time top 20 with the caller's own rank always visible."""
        user_id = event.sender_id
        if user_id is None:
            return
        rows = await self.db.leaderboard_top(20)
        if not rows:
            await self._edit_or_reply(event, LEADERBOARD_EMPTY, [
                [Button.inline("↩️ منوی اصلی", b"menu:main")],
            ])
            return
        medals = ("🥇", "🥈", "🥉")
        lines = ["🏆 لیدربورد", ""]
        for index, row in enumerate(rows):
            rank_label = medals[index] if index < 3 else to_persian_digits(index + 1)
            name = row["first_name"] or LEADERBOARD_FALLBACK_NAME
            lines.append(
                f"{rank_label} {name} — "
                f"{to_persian_digits(row['quiz_points'])} امتیاز"
            )
        rank = await self.db.leaderboard_rank(user_id)
        lines.append("")
        lines.append(
            f"رتبهٔ شما: {to_persian_digits(rank['rank'])} با "
            f"{to_persian_digits(rank['points'])} امتیاز"
        )
        await self._edit_or_reply(event, "\n".join(lines), [
            [Button.inline("↩️ منوی اصلی", b"menu:main")],
        ])

    async def _admin_board(self, event, *, notice: str = "") -> None:
        """Top 5 earners with one-tap free-credit gift entry points."""
        rows = await self.db.leaderboard_admin_top(5)
        lines = ["🏆 برترین کاربران", ""]
        buttons: list[list] = []
        if not rows:
            lines.append("هنوز کاربری امتیازی کسب نکرده است.")
        for index, row in enumerate(rows):
            name = row["first_name"] or LEADERBOARD_FALLBACK_NAME
            lines.append(
                f"{to_persian_digits(index + 1)}. {name} — "
                f"{to_persian_digits(row['quiz_points'])} امتیاز"
            )
            buttons.append([
                Button.inline(
                    button_label(
                        f"{to_persian_digits(index + 1)}. {name} — "
                        f"{to_persian_digits(row['quiz_points'])} امتیاز"
                    ),
                    b"adm:board",
                ),
                Button.inline(
                    "🎁 هدیه",
                    f"adm:bg:{row['telegram_id']}".encode("ascii"),
                ),
            ])
        if notice:
            lines.extend(["", notice])
        buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
        await self._edit(event, "\n".join(lines), buttons)

    async def _admin_gift_pick(self, event, target_id: int) -> None:
        """Choose which paid plan's credit to gift (free plan is not offered)."""
        identity = await self.db.user_identity(target_id)
        if identity is None or identity["banned"]:
            await self._ack(event, "این کاربر در دسترس نیست.", alert=True)
            return
        plans = [plan for plan in await self.db.plans() if plan["price"] > 0]
        lines = [
            "🎁 هدیه به کاربر",
            f"کاربر: {identity['first_name'] or LEADERBOARD_FALLBACK_NAME}",
            "",
            "یکی از بسته‌های اعتباری را انتخاب کنید:",
        ]
        buttons: list[list] = []
        for plan in plans:
            hours = float(plan["hours"])
            hours_text = to_persian_digits(
                int(hours) if hours.is_integer() else str(hours)
            )
            buttons.append([
                Button.inline(
                    button_label(f"{hours_text} ساعت — {format_duration(int(round(hours * 3600)))}"),
                    f"adm:bg:{target_id}:{plan['code']}".encode("utf-8"),
                )
            ])
        buttons.append([Button.inline("↩️ بازگشت", b"adm:board")])
        await self._edit(event, "\n".join(lines), buttons)

    async def _admin_gift_confirm(
        self, event, target_id: int, plan_code: str
    ) -> None:
        plan = await self._gift_plan(target_id, plan_code)
        identity = await self.db.user_identity(target_id)
        if plan is None or identity is None or identity["banned"]:
            await self._ack(event, "این هدیه دیگر معتبر نیست.", alert=True)
            return
        name = identity["first_name"] or LEADERBOARD_FALLBACK_NAME
        seconds = int(round(float(plan["hours"]) * 3600))
        text = (
            "هدیه به "
            f"{name} با اعتبار {format_duration(seconds)} ثبت شود؟"
        )
        await self._edit(event, text, [
            [Button.inline(
                "✅ تأیید هدیه",
                f"adm:bgok:{target_id}:{plan_code}".encode("utf-8"),
            )],
            [Button.inline("↩️ بازگشت", f"adm:bg:{target_id}".encode("utf-8"))],
        ])

    async def _gift_plan(self, target_id: int, plan_code: str) -> dict | None:
        """Validate a gift target/plan pair exactly as execution will see it."""
        identity = await self.db.user_identity(target_id)
        if identity is None or identity["banned"]:
            return None
        for plan in await self.db.plans():
            if plan["code"] == plan_code and plan["price"] > 0:
                return plan
        return None

    async def _admin_gift_execute(
        self, event, admin_id: int, target_id: int, plan_code: str
    ) -> None:
        if not await self._check_admin_cooldown(
            event, "gift", self.ADMIN_COMMAND_COOLDOWN_SECONDS
        ):
            return
        plan = await self._gift_plan(target_id, plan_code)
        if plan is None:
            await self._ack(event, "این هدیه دیگر معتبر نیست.", alert=True)
            return
        # Separation of duties, matching /grant: an admin cannot mint credit
        # for a peer through the leaderboard either.
        if self._is_admin(target_id):
            await event.respond(
                "برای حفظ تفکیک دسترسی‌ها، امکان افزودن اعتبار به حساب مدیر دیگر وجود ندارد."
            )
            return
        seconds = int(round(float(plan["hours"]) * 3600))
        await self.db.add_credit(
            target_id,
            seconds,
            note="leaderboard gift",
            validity_days=None,
            admin_id=admin_id,
        )
        message = (
            "🎁 تبریک! شما جزو ۵ کاربر برتر گاماس هستید و "
            f"{format_duration(seconds)} اعتبار رایگان به حساب شما اضافه شد."
        )
        try:
            await self.client.send_message(target_id, message)
        except Exception:
            logger.warning("gift DM failed user=%s", target_id, exc_info=True)
        await self._admin_board(event, notice="✅ هدیه ثبت شد.")

    # --------------------------------------------------------------- flashcards
    @staticmethod
    def _deck_stale_alert() -> str:
        return "این فلش‌کارت دیگر معتبر نیست."

    def _item_display_title(self, item: dict) -> str:
        """A clean one-line title for learning screens and generated decks."""
        raw = " ".join(str(item.get("title") or "").split())
        if not raw:
            return "جزوهٔ Word" if item.get("kind") == "booklet" else "متن گفتار"
        return raw

    async def _flashcard_start_screen(
        self, event, user_id: int, item_id: int
    ) -> None:
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        if not item.get("content_text"):
            await self._edit(
                event,
                "🃏 فلش‌کارت\n\n" + LEARNING_LEGACY_NOTICE,
                [[Button.inline("↩️ بازگشت", b"library")]],
            )
            return
        size = self.settings.flashcard_ai_deck_size
        cost = self.settings.flashcard_ai_cost_seconds
        text = (
            "ساخت فلش‌کارت با هوش مصنوعی از این جزوه. مجموعه‌ای از "
            f"{to_persian_digits(size)} پرسش و پاسخ ساخته می‌شود.\n"
            f"هزینه: {format_duration(cost)} اعتبار"
        )
        await self._edit(event, text, [
            [Button.inline(
                "🤖 ساخت فلش‌کارت", f"fc:go:{item_id}".encode("ascii")
            )],
            [self._learning_back_button(item_id)],
        ])

    async def _flashcard_generate(
        self, event, user_id: int, item_id: int
    ) -> None:
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        content = item.get("content_text")
        if not content:
            await self._ack(event, LEARNING_LEGACY_NOTICE, alert=True)
            return
        if not self.settings.note_configured:
            await event.respond("سرویس ساخت فلش‌کارت در حال حاضر فعال نیست.")
            return
        if await self.db.deck_count(user_id) >= self.settings.flashcard_max_decks_per_user:
            await event.respond(
                "به حداکثر تعداد مجموعه‌های فلش‌کارت رسیده‌اید؛ برای ساخت مجموعهٔ جدید "
                "یک مجموعهٔ قدیمی را حذف کنید."
            )
            return
        if user_id in self._learning_in_flight:
            await self._ack(event, LEARNING_BUSY_ALERT, alert=True)
            return
        self._learning_in_flight.add(user_id)
        try:
            async with self._learning_lock(user_id):
                await self._flashcard_generate_locked(
                    event, user_id, item_id, content,
                    self._item_display_title(item),
                )
        finally:
            # Discard even when cancellation strikes while waiting on the lock.
            self._learning_in_flight.discard(user_id)

    async def _flashcard_generate_locked(
        self,
        event,
        user_id: int,
        item_id: int,
        content: str,
        item_title: str,
    ) -> None:
        cost = self.settings.flashcard_ai_cost_seconds
        size = self.settings.flashcard_ai_deck_size
        note = f"fc-pending:{item_id}"
        try:
            charged = await self.db.spend_seconds(user_id, cost, note)
        except InsufficientBalanceError:
            await event.respond(
                LEARNING_NO_CREDIT,
                buttons=[[Button.inline("🛒 خرید اشتراک", b"wallet")]],
            )
            return
        placeholder = await event.respond("در حال ساخت فلش‌کارت‌ها… ⏳")
        try:
            model_input = bound_content_text(
                content, limit=MAX_MODEL_INPUT_CHARS
            )
            payload = await complete_json(
                self._http_session(),
                self.settings,
                system=flashcard_system_prompt(size),
                user=model_input,
                schema=FLASHCARD_SCHEMA,
            )
            cards = parse_flashcard_payload(payload, size)
            if len(cards) < MIN_VALID_CARDS:
                raise RuntimeError("flashcard generation yielded too few cards")
            title = item_title + " — فلش‌کارت"
            deck_id = await self.db.deck_create_with_cards(
                user_id, item_id, title[:200], cards
            )
        except BaseException as exc:
            if charged:
                try:
                    await self.db.refund_seconds(user_id, cost, note)
                except Exception:
                    logger.exception(
                        "flashcard refund failed user=%s cost=%s", user_id, cost
                    )
            try:
                await placeholder.edit(LEARNING_GENERATION_FAILED)
            except Exception:
                logger.warning("could not edit flashcard placeholder", exc_info=True)
            if isinstance(
                exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
            logger.warning("flashcard generation failed", exc_info=exc)
            return
        deck = await self.db.deck_get(deck_id, user_id)
        await placeholder.edit(
            f"🃏 مجموعهٔ فلش‌کارت ساخته شد — {title[:200]}",
        )
        if deck is None:
            await event.respond("مجموعه ساخته شد؛ از «کتابخانه من» بازش کنید.")
            return
        await event.respond(
            self._deck_screen_text(deck, len(cards)),
            buttons=self._deck_keyboard(deck_id, bool(cards)),
        )

    async def _show_flashcard_hub(self, event, *, page: int = 0) -> None:
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 6
        count = await self.db.deck_count(user_id)
        page = min(max(0, int(page)), max(0, (count - 1) // page_size))
        decks = await self.db.decks_list(
            user_id, limit=page_size, offset=page * page_size
        )
        if count == 0:
            text = FLASHCARD_EMPTY
            buttons: list[list] = []
        else:
            text = (
                "🃏 فلش‌کارت‌ها\n\n"
                f"تعداد مجموعه‌ها: {to_persian_digits(count)}"
            )
            buttons = []
            for deck in decks:
                label = button_label(
                    f"{deck['title']} ({to_persian_digits(deck['card_count'])} کارت)"
                )
                buttons.append([
                    Button.inline(
                        label, f"fc:deck:{deck['deck_id']}".encode("ascii")
                    )
                ])
            nav = []
            if page > 0:
                nav.append(Button.inline(
                    "◀️ قبلی", f"fc:hub:{page - 1}".encode("ascii")
                ))
            if (page + 1) * page_size < count:
                nav.append(Button.inline(
                    "بعدی ▶️", f"fc:hub:{page + 1}".encode("ascii")
                ))
            if nav:
                buttons.append(nav)
        buttons.extend([
            [Button.inline("✍️ ساخت دستی", b"fc:man")],
            [Button.inline("🤖 از یک جزوه بساز", b"fc:pick:0")],
            [
                Button.inline("📖 جزوه‌ها", b"library"),
                Button.inline("↩️ منوی اصلی", b"menu:main"),
            ],
        ])
        await self._edit_or_reply(event, text, buttons)

    async def _show_booklet_picker(self, event, *, page: int = 0) -> None:
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 6
        count = await self.db.library_count_kind(user_id, "booklet")
        page = min(max(0, int(page)), max(0, (count - 1) // page_size))
        items = await self.db.library_list_kind(
            user_id, "booklet", limit=page_size, offset=page * page_size
        )
        buttons: list[list] = []
        if not items:
            text = (
                "🤖 ساخت فلش‌کارت از جزوه\n\n"
                "هنوز جزوه‌ای در کتابخانهٔ شما ذخیره نشده است."
            )
        else:
            text = "یک جزوه را برای ساخت فلش‌کارت انتخاب کنید:"
            for item in items:
                title = self._item_display_title(item)
                buttons.append([
                    Button.inline(
                        button_label(f"📖 {title}"),
                        f"fc:ai:{item['item_id']}".encode("ascii"),
                    )
                ])
            nav = []
            if page > 0:
                nav.append(Button.inline(
                    "◀️ قبلی", f"fc:pick:{page - 1}".encode("ascii")
                ))
            if (page + 1) * page_size < count:
                nav.append(Button.inline(
                    "بعدی ▶️", f"fc:pick:{page + 1}".encode("ascii")
                ))
            if nav:
                buttons.append(nav)
        buttons.append([Button.inline("↩️ بازگشت", b"fc:hub")])
        await self._edit(event, text, buttons)

    async def _start_manual_deck(self, event, user_id: int) -> None:
        if await self.db.deck_count(user_id) >= self.settings.flashcard_max_decks_per_user:
            await event.respond(
                "به حداکثر تعداد مجموعه‌های فلش‌کارت رسیده‌اید؛ یک مجموعهٔ قدیمی را حذف کنید."
            )
            return
        deck_id = await self.db.manual_deck_get_or_create(user_id)
        self._pending_flashcard[user_id] = (deck_id, "front")
        self._pending_flashcard_front.pop(user_id, None)
        await event.respond(
            FLASHCARD_ASK_FRONT,
            buttons=[[Button.inline("✅ پایان", b"fc:man:done")]],
        )

    async def _handle_flashcard_text(self, event, user_id: int, text: str) -> None:
        """One step of the guided manual-card conversation (armed by fc:man)."""
        deck_id, phase = self._pending_flashcard[user_id]
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            self._pending_flashcard.pop(user_id, None)
            self._pending_flashcard_front.pop(user_id, None)
            await event.reply("این مجموعه دیگر در دسترس نیست.")
            return
        end_button = [[Button.inline("✅ پایان", b"fc:man:done")]]
        if not text:
            await event.reply("لطفاً متن را در یک پیام متنی بفرستید.")
            return
        if phase == "front":
            self._pending_flashcard_front[user_id] = text[:MAX_MANUAL_FRONT_CHARS]
            self._pending_flashcard[user_id] = (deck_id, "back")
            await event.reply(FLASHCARD_ASK_BACK, buttons=end_button)
            return
        front = self._pending_flashcard_front.pop(user_id, "")
        if not front:
            # Defensive: a stale back-phase with no stored front restarts cleanly.
            self._pending_flashcard[user_id] = (deck_id, "front")
            await event.reply(FLASHCARD_ASK_FRONT, buttons=end_button)
            return
        count = await self.db.deck_add_card(
            deck_id, front, text[:MAX_MANUAL_BACK_CHARS]
        )
        self._pending_flashcard[user_id] = (deck_id, "front")
        await event.reply(
            f"کارت اضافه شد ({to_persian_digits(count)} کارت). "
            "پرسش کارت بعدی را بفرستید.",
            buttons=end_button,
        )

    def _deck_screen_text(self, deck: dict, card_count: int) -> str:
        return (
            f"🃏 {deck['title']}\n\n"
            f"تعداد کارت‌ها: {to_persian_digits(card_count)}"
        )

    def _deck_keyboard(self, deck_id: int, has_cards: bool) -> list[list]:
        buttons: list[list] = []
        if has_cards:
            buttons.append([Button.inline(
                "▶️ مرور فلش‌کارت‌ها", f"fc:study:{deck_id}".encode("ascii")
            )])
        buttons.append([Button.inline(
            "📄 خروجی Word", f"fc:exp:{deck_id}".encode("ascii")
        )])
        buttons.append([Button.inline(
            "🗑 حذف دسته", f"fc:del:{deck_id}".encode("ascii")
        )])
        buttons.append([Button.inline("↩️ بازگشت", b"fc:hub")])
        return buttons

    async def _show_deck_screen(self, event, user_id: int, deck_id: int) -> None:
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)
            return
        cards = await self.db.deck_cards(deck_id)
        await self._edit(
            event,
            self._deck_screen_text(deck, len(cards)),
            self._deck_keyboard(deck_id, bool(cards)),
        )

    async def _study_front(
        self, event, user_id: int, deck_id: int, *, restart: bool = False
    ) -> None:
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)
            return
        cards = await self.db.deck_cards(deck_id)
        if not cards:
            await self._ack(event, "این مجموعه هنوز کارتی ندارد.", alert=True)
            return
        cursor = 0 if restart else min(int(deck["study_cursor"]), len(cards) - 1)
        if restart:
            await self.db.deck_set_cursor(deck_id, 0)
        card = cards[cursor]
        await self._edit(event, "پرسش:\n\n" + card["front"], [
            [Button.inline(
                "👁 نمایش پاسخ",
                f"fc:flip:{deck_id}:{card['card_id']}".encode("ascii"),
            )],
            [Button.inline(
                "⏹ پایان مرور", f"fc:done:{deck_id}".encode("ascii")
            )],
        ])

    async def _flip_card(
        self, event, user_id: int, deck_id: int, card_id: int
    ) -> None:
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)
            return
        card = await self.db.deck_card(deck_id, card_id)
        if card is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)
            return
        await self._edit(event, "پاسخ:\n\n" + card["back"], [
            [Button.inline(
                "➡️ کارت بعدی", f"fc:next:{deck_id}".encode("ascii")
            )],
            [Button.inline(
                "⏹ پایان مرور", f"fc:done:{deck_id}".encode("ascii")
            )],
        ])

    async def _next_card(self, event, user_id: int, deck_id: int) -> None:
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)
            return
        total = len(await self.db.deck_cards(deck_id))
        if total == 0:
            await self._ack(event, "این مجموعه هنوز کارتی ندارد.", alert=True)
            return
        new_cursor = int(deck["study_cursor"]) + 1
        await self.db.deck_set_cursor(deck_id, new_cursor)
        if new_cursor >= total:
            await self._edit(event, (
                "مرور تمام شد — "
                f"{to_persian_digits(total)} کارت مرور شد."
            ), [
                [Button.inline(
                    "🔁 از ابتدا", f"fc:restart:{deck_id}".encode("ascii")
                )],
                [Button.inline("↩️ بازگشت", b"fc:hub")],
            ])
            return
        await self._study_front(event, user_id, deck_id)

    async def _export_deck_docx(
        self, event, user_id: int, deck_id: int
    ) -> None:
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)
            return
        cards = await self.db.deck_cards(deck_id)
        if not cards:
            await self._ack(event, "این مجموعه هنوز کارتی ندارد.", alert=True)
            return
        body_parts = [
            f"پرسش {to_persian_digits(index)}: {card['front']}\nپاسخ: {card['back']}"
            for index, card in enumerate(cards, start=1)
        ]
        meta = DocumentMeta(
            reference=f"flashcard-{deck_id}",
            source_name="flashcards",
            created_at=datetime.now(),
        )
        docx_bytes = await asyncio.to_thread(
            build_plain_docx,
            deck["title"],
            "\n\n".join(body_parts),
            fonts=resolve_fonts(),
            design=resolve_design(),
            meta=meta,
        )
        workdir = Path(await run_file_io_resource(
            tempfile.mkdtemp,
            lambda path: shutil.rmtree(path, ignore_errors=True),
            prefix="deck-",
            dir=self.settings.temp_dir,
        ))
        path = workdir / f"flashcard-{deck_id}.docx"
        try:
            await run_file_io(path.write_bytes, docx_bytes)
            await self._send_telegram_file(
                event.chat_id,
                path,
                caption=f"🃏 فلش‌کارت‌ها — {deck['title']}"[:TELEGRAM_CAPTION_LIMIT],
                force_document=True,
            )
        finally:
            await run_file_io(shutil.rmtree, workdir, ignore_errors=True)

    async def _route_flashcard_callback(
        self, event, user_id: int, data: str
    ) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "hub":
            page = _to_int(parts[2]) if len(parts) == 3 else 0
            await self._show_flashcard_hub(event, page=max(0, page or 0))
            return
        if action == "pick":
            page = _to_int(parts[2]) if len(parts) == 3 else 0
            await self._show_booklet_picker(event, page=max(0, page or 0))
            return
        if action == "man" and len(parts) == 2:
            await self._start_manual_deck(event, user_id)
            return
        if action == "man" and len(parts) == 3 and parts[2] == "done":
            state = self._pending_flashcard.pop(user_id, None)
            self._pending_flashcard_front.pop(user_id, None)
            if state is None:
                await self._show_flashcard_hub(event)
            else:
                await event.respond("✅ ساخت کارت پایان یافت.")
                await self._show_deck_screen(event, user_id, state[0])
            return
        if action == "ai" and len(parts) == 3:
            item_id = _to_int(parts[2])
            if item_id is None:
                await self._ack(event, self._deck_stale_alert(), alert=True)
                return
            await self._flashcard_start_screen(event, user_id, item_id)
            return
        if action == "go" and len(parts) == 3:
            item_id = _to_int(parts[2])
            if item_id is None:
                await self._ack(event, self._deck_stale_alert(), alert=True)
                return
            await self._flashcard_generate(event, user_id, item_id)
            return
        if action in {"deck", "study", "exp", "del", "delok", "done",
                      "restart", "next"} and len(parts) == 3:
            deck_id = _to_int(parts[2])
            if deck_id is None:
                await self._ack(event, self._deck_stale_alert(), alert=True)
                return
            if action == "deck":
                await self._show_deck_screen(event, user_id, deck_id)
            elif action == "study":
                await self._study_front(event, user_id, deck_id)
            elif action == "exp":
                await self._export_deck_docx(event, user_id, deck_id)
            elif action == "del":
                deck = await self.db.deck_get(deck_id, user_id)
                if deck is None:
                    await self._ack(event, self._deck_stale_alert(), alert=True)
                    return
                await self._edit(event, "آیا مطمئنید؟", [
                    [Button.inline(
                        "✅ بله، حذف کن", f"fc:delok:{deck_id}".encode("ascii")
                    )],
                    [Button.inline(
                        "↩️ بازگشت", f"fc:deck:{deck_id}".encode("ascii")
                    )],
                ])
            elif action == "delok":
                deleted = await self.db.deck_delete(deck_id, user_id)
                if not deleted:
                    await self._ack(event, self._deck_stale_alert(), alert=True)
                else:
                    await self._show_flashcard_hub(event)
            elif action == "done":
                await self._show_deck_screen(event, user_id, deck_id)
            elif action == "restart":
                await self._study_front(event, user_id, deck_id, restart=True)
            elif action == "next":
                await self._next_card(event, user_id, deck_id)
            return
        if action == "flip" and len(parts) == 4:
            deck_id = _to_int(parts[2])
            card_id = _to_int(parts[3])
            if deck_id is None or card_id is None:
                await self._ack(event, self._deck_stale_alert(), alert=True)
                return
            await self._flip_card(event, user_id, deck_id, card_id)
            return
        await self._ack(event, self._deck_stale_alert(), alert=True)

    async def _route_quiz_callback(self, event, user_id: int, data: str) -> None:
        """Parse and dispatch every ``quiz:`` callback, failing closed."""
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "new" and len(parts) == 3:
            item_id = _to_int(parts[2])
            if item_id is None:
                await self._ack(event, LEARNING_STALE_ALERT, alert=True)
                return
            await self._quiz_start_screen(event, user_id, item_id)
            return
        if action == "go" and len(parts) == 4:
            item_id = _to_int(parts[2])
            count = _to_int(parts[3])
            if item_id is None or count is None:
                await self._ack(event, LEARNING_STALE_ALERT, alert=True)
                return
            await self._quiz_generate(event, user_id, item_id, count)
            return
        if action == "ans" and len(parts) == 5:
            session_id = _to_int(parts[2])
            question_id = _to_int(parts[3])
            option = _to_int(parts[4])
            if session_id is None or question_id is None or option is None:
                await self._ack(event, LEARNING_STALE_ALERT, alert=True)
                return
            await self._quiz_answer(event, user_id, session_id, question_id, option)
            return
        if action in ("next", "quit") and len(parts) == 3:
            session_id = _to_int(parts[2])
            if session_id is None:
                await self._ack(event, LEARNING_STALE_ALERT, alert=True)
                return
            if action == "next":
                await self._quiz_next(event, user_id, session_id)
            else:
                await self._quiz_quit(event, user_id, session_id)
            return
        await self._ack(event, LEARNING_STALE_ALERT, alert=True)

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
        # A review action supersedes any older rejection-reason prompt. Only
        # the new rejection action below is allowed to arm that prompt again.
        if data.startswith("payv:"):
            self._pending_reject_reason.pop(user_id, None)
        # Likewise, navigating away from the payment flow drops a waiting
        # receipt upload — a photo sent later belongs to the new screen.
        if not data.startswith(("payr:", "plan:", "payopen:")):
            self._pending_payments.pop(user_id, None)
        # Support prompts are disarmed by any navigation except the prompt itself.
        if not data.startswith(("sup:cat:", "sup:r:")):
            self._support_states.pop(user_id, None)
        if not data.startswith("adm:sr:"):
            self._support_admin_states.pop(user_id, None)
        # A manual-flashcard conversation likewise ends at any navigation;
        # fc:man re-arms itself inside its own branch.
        self._pending_flashcard.pop(user_id, None)
        self._pending_flashcard_front.pop(user_id, None)

        if data == "help":
            await self._ack(event)
            guide = (
                "ℹ️ راهنمای گاماس\n\n"
                "🎧 فایل صوت/ویدیو یا 📄 پاورپوینت درس را همین‌جا بفرستید.\n"
                "📄 متن گفتار و جزوهٔ ورد می‌آید و در «📚 کتابخانه من» ذخیره می‌شود.\n"
                "🧪 آزمون و 🃏 فلش‌کارت از همان جزوه ساخته می‌شود؛ هر پاسخ درست ۱۰ امتیاز.\n"
                "🛒 خرید اشتراک: بسته‌ها و موجودی. 🎧 پشتیبانی: تیکت بزنید.\n\n"
                "💳 اعتبار بر حسب ثانیه است و پس از ارسال متن خام محاسبه می‌شود.\n"
                "اگر متن خام نرسد، اعتباری کم نمی‌شود.\n"
                "ساخت 🧪 آزمون و 🃏 فلش‌کارت هوش مصنوعی اعتبار کم می‌کند؛ اگر ناموفق بود، اعتبار کامل برمی‌گردد.\n"
                "پس از دریافت متن خام، خطای ساخت جزوه اعتبار را برنمی‌گرداند."
            )
            await event.respond(
                guide,
                buttons=[[Button.inline("↩️ منوی اصلی", b"menu:main")]],
            )
        elif data == "menu:main":
            await self._ack(event)
            await self._show_main(event)
        elif data == "library":
            await self._ack(event)
            await self._show_library(event)
        elif data == "library:fc":
            await self._ack(event)
            await self._show_flashcard_hub(event)
        elif data == "board":
            await self._ack(event)
            await self._show_leaderboard(event)
        elif data.startswith("lib:f:"):
            await self._ack(event)
            item_id = _to_int(data.rsplit(":", 1)[-1])
            if item_id is None:
                await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            else:
                await self._forward_library_item(event, user_id, item_id)
        elif data.startswith("lib:it:"):
            await self._ack(event)
            item_id = _to_int(data.rsplit(":", 1)[-1])
            if item_id is None:
                await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            else:
                await self._show_library_item(event, item_id)
        elif data.startswith("lib:"):
            await self._ack(event)
            page = _to_int(data.split(":", 1)[1])
            if page is None:
                await self._ack(event, "صفحهٔ کتابخانه نامعتبر است.", alert=True)
            else:
                await self._show_library(event, page=max(0, page))
        elif data == "wallet":
            await self._ack(event)
            await self._show_wallet(event)
        elif data.startswith("walletp:"):
            await self._ack(event)
            page = _to_int(data.split(":", 1)[1])
            if page is None:
                await self._ack(event, "صفحهٔ پرداخت‌ها نامعتبر است.", alert=True)
            else:
                await self._show_wallet(event, payments_page=max(0, page))
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
        elif data.startswith("adm:ov"):
            # Dashboard navigation. The window is part of the callback data and
            # is validated here, so a forged or stale value opens the default
            # window instead of an arbitrary one (and never reaches SQL).
            await self._ack(event)
            parts = data.split(":")
            section = parts[1] if len(parts) > 1 else "ov"
            period = parts[2] if len(parts) > 2 else "7d"
            if period not in self.ADMIN_PERIOD_KEYS:
                await self._ack(event, "بازهٔ زمانی نامعتبر است.", alert=True)
                return
            if section == "ov":
                await self._show_admin_overview(event, period)
            elif section == "ovu":
                await self._show_admin_users_report(event, period)
            elif section == "ovp":
                await self._show_admin_processing_report(event, period)
            elif section == "ovpay":
                await self._show_admin_payments_report(event, period)
            elif section == "ovs":
                await self._show_admin_system_report(event, period)
            else:
                await self._ack(event, "بخش گزارش نامعتبر است.", alert=True)
        elif data == "adm:users":
            await self._ack(event)
            await self._show_admin_users(event)
        elif data == "adm:pay" or data.startswith("adm:pay:"):
            await self._ack(event)
            page = 0
            if data != "adm:pay":
                parts = data.split(":")
                parsed_page = _to_int(parts[2]) if len(parts) == 3 else None
                if parsed_page is None:
                    await self._ack(event, "صفحهٔ سفارش‌ها نامعتبر است.", alert=True)
                    return
                page = max(0, parsed_page)
            await self._show_admin_payments(event, page)
        elif data.startswith("adm:ph:"):
            await self._ack(event)
            page = _to_int(data.rsplit(":", 1)[-1])
            await self._show_admin_payment_history(event, max(0, page or 0))
        elif data.startswith("adm:pd:"):
            await self._ack(event)
            detail_payment_id = _to_int(data.rsplit(":", 1)[-1])
            if detail_payment_id is None:
                await self._ack(event, "شناسه سفارش نامعتبر است.", alert=True)
                return
            await self._show_admin_payment_detail(event, detail_payment_id)
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
        elif data == "adm:board":
            await self._ack(event)
            await self._admin_board(event)
        elif data.startswith("adm:bgok:"):
            await self._ack(event)
            parts = data.split(":", 3)
            target_id = _to_int(parts[2]) if len(parts) == 4 else None
            if target_id is None:
                await self._ack(event, "درخواست هدیه نامعتبر است.", alert=True)
            else:
                await self._admin_gift_execute(event, user_id, target_id, parts[3])
        elif data.startswith("adm:bg:"):
            await self._ack(event)
            parts = data.split(":")
            target_id = _to_int(parts[2]) if len(parts) in (3, 4) else None
            if target_id is None:
                await self._ack(event, "درخواست هدیه نامعتبر است.", alert=True)
            elif len(parts) == 3:
                await self._admin_gift_pick(event, target_id)
            else:
                await self._admin_gift_confirm(event, target_id, parts[3])
        elif data.startswith("adm:rcpt:"):
            await self._ack(event)
            await self._show_receipt(event, data)
        elif data == "adm:sup" or data.startswith(("adm:sl:", "adm:sv:", "adm:sr:", "adm:sc:", "adm:so:")):
            await self._ack(event)
            await self._handle_admin_support_callback(event, user_id, data)
        elif data == "support" or data.startswith("sup:"):
            await self._ack(event)
            await self._handle_support_callback(event, user_id, data)
        elif data.startswith("quiz:"):
            await self._ack(event)
            await self._route_quiz_callback(event, user_id, data)
        elif data.startswith("fc:"):
            await self._ack(event)
            await self._route_flashcard_callback(event, user_id, data)
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
        await self._capture_sender_name(event)
        text = (event.message.message or "").strip()
        command = _command_name(text)
        if command == "/start":
            # A fresh menu is a hard boundary for every conversational flow.
            # In particular, a later photo must not unexpectedly complete an
            # old receipt or ticket prompt after the user pressed /start.
            self._pending_reject_reason.pop(user_id, None)
            self._pending_broadcast.discard(user_id)
            self._pending_payments.pop(user_id, None)
            self._support_states.pop(user_id, None)
            self._support_admin_states.pop(user_id, None)
            self._pending_flashcard.pop(user_id, None)
            self._pending_flashcard_front.pop(user_id, None)
            await self._show_main(event, first_start=True)
            return

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
        if self._is_admin(user_id) and user_id in self._support_admin_states and not command:
            ticket_id = self._support_admin_states[user_id]
            await self._handle_admin_support_text(event, user_id, ticket_id, text)
            return
        if self._is_admin(user_id) and command:
            # Any command leaves the conversational reply flow. In particular,
            # /start must not leave a stale ticket id armed for the next text.
            self._support_admin_states.pop(user_id, None)
            # Disarm a manual flashcard prompt before the early-returning admin
            # commands below, so /grant or /ban cannot leave it armed to swallow
            # the admin's next plain message.
            self._pending_flashcard.pop(user_id, None)
            self._pending_flashcard_front.pop(user_id, None)
        if self._is_admin(user_id) and command == "/grant":
            await self._admin_grant(event, text)
            return
        if self._is_admin(user_id) and command == "/unlimited":
            await self._admin_unlimited(event, text)
            return
        if self._is_admin(user_id) and command in {"/ban", "/unban"}:
            await self._admin_ban(event, text)
            return

        if user_id in self._support_states and not command:
            state = self._support_states[user_id]
            await self._handle_support_text(event, user_id, state, text)
            return
        if user_id in self._support_states and command:
            # Commands, including /start, always disarm the old prompt.
            self._support_states.pop(user_id, None)

        # While a receipt is expected, no ordinary text/media may fall through
        # to the paid lecture pipeline. Invalid input keeps the
        # receipt prompt armed so the user can immediately retry.
        if user_id in self._pending_payments and not command:
            await self._handle_receipt_upload(event, user_id)
            return

        if command == "/cancel":
            self._pending_payments.pop(user_id, None)
            self._support_states.pop(user_id, None)
            self._support_admin_states.pop(user_id, None)
            self._pending_flashcard.pop(user_id, None)
            self._pending_flashcard_front.pop(user_id, None)
            await event.reply("ارسال لغو شد. لغو سفارش از «🛒 خرید اشتراک».")
            return

        # Manual flashcard conversation: armed by fc:man and disarmed by any
        # command or navigation callback. It must sit before media intake so a
        # plain text answer can never fall through to the lecture pipeline.
        if user_id in self._pending_flashcard:
            if command:
                self._pending_flashcard.pop(user_id, None)
                self._pending_flashcard_front.pop(user_id, None)
            else:
                await self._handle_flashcard_text(event, user_id, text)
                return

        # Presentations.
        pkind, pfilename = _presentation_metadata(event.message)
        if pkind is not None:
            if await self._is_duplicate_upload(event, user_id):
                return
            await self._accept_presentation(event, user_id, pkind, pfilename)
            return

        # Media.
        kind, filename, duration = _media_metadata(event.message)
        if kind is not None:
            if await self._is_duplicate_upload(event, user_id):
                return
            await self._accept_media(event, user_id, kind, filename, duration)
            return

        # Direct/post-restart receipt image fallback when an open unpaid order exists.
        if _is_receipt_image(event.message):
            open_payment = await self.db.open_awaiting_payment_for_user(user_id)
            if open_payment is not None:
                await self._handle_receipt_upload(
                    event, user_id, payment_id=int(open_payment["payment_id"])
                )
                return

        if _is_unsupported_attachment(event.message):
            await event.reply(
                "⚠️ این نوع فایل پشتیبانی نمی‌شود. فایل صوتی، ویدیویی یا پاورپوینت بفرستید."
            )
            return
        if text:
            await event.reply(
                "همین‌جا فایل 🎧 صوت/ویدیو یا 📄 پاورپوینت درس را بفرستید؛ متن و جزوه می‌آید.",
                buttons=self._user_menu(is_admin=self._is_admin(user_id)),
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
            max_mb = to_persian_digits(self.settings.max_file_size // (1024 * 1024))
            await event.reply(
                f"❌ حجم فایل ارسالی از سقف مجاز ربات بیشتر است. حد مجاز: {max_mb} مگابایت"
            )
            return
        if duration is not None and duration > self.settings.max_media_duration_seconds:
            limit_minutes = int(self.settings.max_media_duration_seconds // 60)
            await event.reply(
                "❌ مدت فایل صوتی یا ویدیویی از سقف مجاز ربات بیشتر است "
                f"(حداکثر {to_persian_digits(limit_minutes)} دقیقه)."
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
                f"💳 اعتبار شما کافی نیست.\n"
                f"{measured} | لازم: {format_duration(needed)} | شما: {format_duration(available)}\n"
                "از «🛒 خرید اشتراک» اعتبار بگیرید."
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
            await self.db.set_submission_status(
                submission_id, "rejected", error=str(exc), error_kind="capacity"
            )
            await progress.fail(str(submission_id))
            await event.reply(f"⏸ {exc}")
            return
        logger.info(
            "accepted media submission=%s kind=%s duration=%s",
            submission_id, _sanitize_log_value(kind), duration,
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
            max_mb = to_persian_digits(self.settings.max_file_size // (1024 * 1024))
            await event.reply(
                f"❌ حجم فایل ارسالی از سقف مجاز ربات بیشتر است. حد مجاز: {max_mb} مگابایت"
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
                    f"💳 اعتبار شما کافی نیست.\n"
                    f"لازم: {format_duration(needed)} | شما: {format_duration(available)}\n"
                    "از «🛒 خرید اشتراک» اعتبار بگیرید."
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
            await self.db.set_submission_status(
                submission_id, "rejected", error=str(exc), error_kind="capacity"
            )
            await progress.fail(str(submission_id))
            await event.reply(f"⏸ {exc}")
            return
        logger.info("accepted presentation submission=%s", submission_id)

    def _prune_dedup_maps(self, now: float) -> None:
        expired = [key for key, exp in self._seen_messages.items() if exp <= now]
        for key in expired:
            self._seen_messages.pop(key, None)
        while len(self._seen_messages) > 2048:
            oldest = next(iter(self._seen_messages))
            self._seen_messages.pop(oldest, None)

    @staticmethod
    def _upload_fingerprint(message: object) -> str | None:
        if message is None:
            return None
        doc = getattr(message, "document", None)
        doc_id = getattr(doc, "id", None) if doc is not None else None
        if isinstance(doc_id, int) and doc_id > 0:
            return f"doc:{doc_id}"
        photo = getattr(message, "photo", None)
        photo_id = getattr(photo, "id", None) if photo is not None else None
        if isinstance(photo_id, int) and photo_id > 0:
            return f"photo:{photo_id}"
        return None

    async def _is_duplicate_upload(self, event, user_id: int) -> bool:
        window = float(self.settings.dedup_window_seconds)
        if window <= 0:
            return False
        now = time.monotonic()
        self._prune_dedup_maps(now)
        msg = getattr(event, "message", None)
        msg_id = getattr(msg, "id", None)
        chat_id = getattr(event, "chat_id", None) or user_id
        if isinstance(msg_id, int) and msg_id > 0:
            msg_key = (int(chat_id), msg_id)
            if self._seen_messages.get(msg_key, 0.0) > now:
                METRICS.record_deduplicated()
                logger.info("deduplicated repeated telegram update")
                return True
            self._seen_messages[msg_key] = now + window

        fp = self._upload_fingerprint(msg)
        if fp is not None and (user_id, fp) in self._active_upload_keys:
            METRICS.record_deduplicated()
            logger.info(
                "deduplicated concurrent identical file existing_job=%s",
                self._active_upload_keys[(user_id, fp)],
            )
            await event.reply(
                "⏳ این فایل هم‌اکنون در صف پردازش شما قرار دارد؛ لطفاً تا پایان پردازش منتظر بمانید."
            )
            return True
        return False

    def runtime_metrics(self) -> dict[str, Any]:
        """Point-in-time snapshot combining pipeline telemetry and live queue state."""
        snap = METRICS.snapshot()
        snap["queue_pending"] = self.queue.pending
        snap["queue_active"] = self.queue.active_count
        snap["in_flight_jobs"] = self.queue.active_snapshot()
        return snap

    @staticmethod
    def _reserve_estimate(duration: float | None) -> int:
        if duration is None or duration <= 0:
            return 600  # unknown duration: reserve a conservative floor
        return int(math.ceil(duration))

    # ------------------------------------------------------------ job worker
    async def _run_job(self, job: Job) -> None:
        progress: JobProgress | None = job.meta.get("progress")
        log_token = log_job_id.set(job.job_id)
        fp = self._upload_fingerprint(job.message)
        if fp is not None:
            self._active_upload_keys[(job.user_id, fp)] = job.job_id
        workdir: Path | None = None
        try:
            if await self.db.is_banned(job.user_id):
                job.transition_to("cancelled")
                await self._release(job, "user banned before processing")
                await self.db.set_submission_status(
                    job.submission_id,
                    "rejected",
                    error="user banned",
                    error_kind="banned",
                )
                if progress:
                    await progress.fail(str(job.submission_id))
                await self._reply(job, "⛔ درخواست صف‌شده به دلیل مسدودی حساب لغو شد.")
                return
            await self.db.set_submission_status(job.submission_id, "processing")
            await run_file_io(
                self.settings.temp_dir.mkdir, parents=True, exist_ok=True
            )
            workdir = Path(await run_file_io_resource(
                tempfile.mkdtemp,
                lambda path: shutil.rmtree(path, ignore_errors=True),
                prefix=f"submission-{job.submission_id}-",
                dir=self.settings.temp_dir,
            ))
            if job.kind == "presentation":
                await self._process_presentation(job, workdir, progress)
            else:
                await self._process_media(job, workdir, progress)
            job.transition_to("completed")
        except InsufficientBalanceError as exc:
            job.transition_to("failed")
            await self.db.set_submission_status(
                job.submission_id, "failed",
                error=f"insufficient balance: {exc}",
                error_kind="billing",
            )
            if progress:
                await progress.fail(str(job.submission_id))
            await self._reply(job, (
                f"💳 اعتبار شما کافی نیست.\n"
                f"لازم: {format_duration(exc.required_seconds)} | شما: {format_duration(exc.available_seconds)}\n"
                "از «🛒 خرید اشتراک» اعتبار بگیرید."
            ))
        except USER_VISIBLE_ERRORS as exc:
            kind = classify_failure(job.state, exc)
            job.transition_to("failed")
            await self._release(job, "user-visible failure")
            await self.db.set_submission_status(
                job.submission_id, "failed", error=str(exc)[:500], error_kind=kind
            )
            if progress:
                await progress.fail(str(job.submission_id))
            safe_msg = _safe_user_error_text(
                exc, "درخواست قابل پردازش نبود؛ لطفاً فایل را بررسی و دوباره ارسال کنید."
            )
            await self._reply(job, f"❌ {safe_msg}")
        except STTError as exc:
            job.transition_to("failed")
            await self._release(job, "stt failure")
            await self.db.set_submission_status(
                job.submission_id, "failed", error=str(exc)[:500], error_kind="stt"
            )
            if progress:
                await progress.fail(str(job.submission_id))
            safe_msg = _safe_user_error_text(
                exc, "خطای موقت در سرویس تبدیل گفتار رخ داد؛ لطفاً دوباره تلاش کنید."
            )
            await self._reply(job, f"❌ تبدیل گفتار به متن انجام نشد: {safe_msg}")
        except asyncio.CancelledError:
            job.transition_to("cancelled")
            await self._release(job, "cancelled")
            await self.db.set_submission_status(
                job.submission_id, "failed", error="cancelled", error_kind="cancelled"
            )
            raise
        except Exception as exc:
            kind = classify_failure(job.state, exc)
            job.transition_to("failed")
            await self._release(job, f"unexpected: {type(exc).__name__}")
            await self.db.set_submission_status(
                job.submission_id, "failed",
                error=f"{type(exc).__name__}: {exc}"[:500],
                error_kind=kind,
            )
            logger.exception("job failed job_id=%s submission=%s", job.job_id, job.submission_id)
            if progress:
                await progress.fail(str(job.submission_id))
            await self._reply(
                job, "❌ خطای داخلی رخ داد و پردازش تکمیل نشد. لطفاً دوباره تلاش کنید."
            )
        finally:
            if fp is not None:
                self._active_upload_keys.pop((job.user_id, fp), None)
            try:
                if progress:
                    await progress.aclose()
            finally:
                try:
                    if workdir is not None:
                        await run_file_io(shutil.rmtree, workdir, True)
                except OSError:
                    logger.warning("could not remove job work directory")
                finally:
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
        await progress.update(10, "آماده‌سازی پردازش")
        suffix = Path(job.filename).suffix.lower() if job.filename else (
            ".mp4" if job.kind == "video" else ".ogg"
        )
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ".media"
        source_path = workdir / f"source{suffix}"
        job.transition_to("downloading")
        await progress.update(20, "دریافت فایل از تلگرام")
        download_started = time.monotonic()
        try:
            await self._download_telegram_media(
                job.message, source_path, self.settings.max_file_size
            )
        except asyncio.TimeoutError as exc:
            raise ValueError(
                "مهلت دریافت فایل از تلگرام به پایان رسید؛ لطفاً دوباره تلاش کنید."
            ) from exc
        except UploadTooLargeError as exc:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.") from exc
        source_size = (await run_file_io(source_path.stat)).st_size
        if source_size > self.settings.max_file_size:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")
        # The download is the only transfer rate this job measures for free, so
        # it seeds the format decision below (``AUDIO_FORMAT_POLICY=auto``).
        download_seconds = max(0.001, time.monotonic() - download_started)
        egress_bytes_per_second = source_size / download_seconds

        job.transition_to("extracting")
        await progress.update(35, "بررسی و آماده‌سازی صدا")
        source_info = await self._run_media_operation(
            probe_media, source_path, self.settings
        )
        if (
            source_info.duration is not None
            and source_info.duration > self.settings.max_media_duration_seconds
        ):
            limit_minutes = int(self.settings.max_media_duration_seconds // 60)
            raise ValueError(
                "مدت فایل صوتی یا ویدیویی از سقف مجاز ربات بیشتر است "
                f"(حداکثر {to_persian_digits(limit_minutes)} دقیقه)."
            )
        audio_path = await self._run_media_operation(
            self._prepare_audio,
            source_path,
            workdir,
            job.filename,
            job.kind,
            info=source_info,
            egress_bytes_per_second=egress_bytes_per_second,
        )
        audio_size = (await run_file_io(audio_path.stat)).st_size
        if audio_size > self.settings.max_file_size:
            raise ValueError("صدای آماده‌شده از سقف حجم ربات بزرگ‌تر است.")

        if source_info.duration is not None or audio_path == source_path:
            info = source_info
        else:
            info = await self._run_media_operation(
                probe_media, audio_path, self.settings
            )
        duration = info.duration or job.duration_hint
        if not duration or duration <= 0:
            raise ValueError(
                "مدت فایل مشخص نشد؛ امکان پردازش آن وجود ندارد."
            )
        if duration > self.settings.max_media_duration_seconds:
            limit_minutes = int(self.settings.max_media_duration_seconds // 60)
            raise ValueError(
                "مدت فایل صوتی یا ویدیویی از سقف مجاز ربات بیشتر است "
                f"(حداکثر {to_persian_digits(limit_minutes)} دقیقه)."
            )
        needed = self._reserve_estimate(duration)
        await self.db.reserve(job.user_id, job.submission_id, needed)

        job.transition_to("transcribing")
        await progress.update(50, "تبدیل گفتار به متن")
        try:
            raw = await transcribe(audio_path, self.settings, session=self._http_session())
        except BaseException:
            await self._release(job, "transcription failed")
            raise
        document = build_document(raw)

        job.transition_to("uploading")
        await progress.update(75, "ارسال متن گفتار")
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
        await asyncio.gather(
            self.db.save_transcription(
                job.submission_id, engine=raw.engine, text=document.plain(), notes_json=""
            ),
            run_file_io(
                raw_path.write_text, raw_text, encoding="utf-8", newline="\n"
            ),
        )
        await self._deliver_raw(job, raw_text, raw_path, reference)
        await asyncio.gather(
            self._send_preview(job, raw_text),
            self._settle(job, duration),
        )
        if not self.settings.note_configured:
            await self.db.set_submission_status(job.submission_id, "done")
            await progress.complete("متن کامل گفتار ارسال شد.")
            return
        job.transition_to("generating_notes")
        await progress.update(85, "تولید جزوهٔ ساختارمند")
        notes = await self._generate_notes(
            document.llm_document(),
            job,
            progress,
            workdir=workdir,
            reference=reference,
            meta=meta,
        )

        await self.db.save_transcription(
            job.submission_id, engine=raw.engine, text=document.plain(),
            notes_json=notes.to_json() if notes else "",
        )
        job.transition_to("uploading")
        await self._deliver_booklet(job, workdir, notes, document, reference, meta, progress)

    async def _prepare_audio(
        self,
        source_path: Path,
        workdir: Path,
        filename: str | None,
        kind: str,
        *,
        info: MediaInfo | None = None,
        egress_bytes_per_second: float | None = None,
    ) -> Path:
        if info is None:
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
            oversized = (await run_file_io(source_path.stat)).st_size > budget
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
                egress_bytes_per_second=egress_bytes_per_second,
            )
        return source_path

    async def _run_media_operation(self, operation, *args, **kwargs):
        """Run one CPU/disk-heavy media stage under the configured process cap."""
        async with self._media_semaphore:
            return await operation(*args, **kwargs)

    async def _download_telegram_media(
        self, message, target: Path, limit_bytes: int
    ) -> Path:
        """Apply the shared download gate, disk check, transfer and deadline."""
        target = Path(target)
        await run_file_io(target.parent.mkdir, parents=True, exist_ok=True)
        async with self._download_semaphore:
            reported_bytes = _reported_upload_bytes(message, limit_bytes)
            if reported_bytes > limit_bytes:
                raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
            await run_file_io(
                _ensure_disk_headroom,
                target.parent,
                reported_bytes * 2,
                reserve_bytes=self.settings.temp_min_free_bytes,
            )
            return await asyncio.wait_for(
                _download_media_limited(
                    message,
                    target,
                    limit_bytes,
                    stall_seconds=self.settings.download_stall_seconds,
                    client=self.client,
                    connections=self.settings.telegram_download_connections,
                ),
                timeout=self.settings.download_timeout,
            )

    async def _settle(self, job: Job, duration: float | None) -> None:
        measured = int(math.ceil(duration)) if duration else 0
        await self.db.settle_hold(job.submission_id, measured)

    # ------------------------------------------------- presentation pipeline
    async def _process_presentation(self, job: Job, workdir: Path, progress) -> None:
        assert progress is not None
        await progress.update(10, "آماده‌سازی پردازش")
        suffix = Path(job.filename).suffix.lower() if job.filename else ".pptx"
        source_path = workdir / f"source{suffix}"
        job.transition_to("downloading")
        await progress.update(20, "دریافت فایل از تلگرام")
        try:
            await self._download_telegram_media(
                job.message, source_path, self.settings.max_file_size
            )
        except asyncio.TimeoutError as exc:
            raise ValueError(
                "مهلت دریافت فایل از تلگرام به پایان رسید؛ لطفاً دوباره تلاش کنید."
            ) from exc
        except UploadTooLargeError as exc:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.") from exc
        deck_size = (await run_file_io(source_path.stat)).st_size
        if deck_size > self.settings.max_file_size:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")
        if deck_size > self.settings.presentation_max_temp_bytes:
            raise PresentationError("حجم فایل موقت ارائه از سقف تعیین‌شده بیشتر است.")

        job.transition_to("extracting")
        conversion_warnings: list[str] = []
        if job.meta.get("presentation_kind") == "legacy":
            await progress.update(30, "تبدیل قالب قدیمی ارائه")
            source_path = await self._run_media_operation(
                convert_to_pptx,
                source_path,
                workdir,
                self.settings,
                warnings_out=conversion_warnings,
            )
            if (await run_file_io(source_path.stat)).st_size > self.settings.max_file_size:
                raise PresentationError("ارائهٔ تبدیل‌شده از سقف حجم فایل بزرگ‌تر است.")

        await progress.update(40, "بررسی اسلایدها")
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
        # Only add the generic note when no other explanation is present.
        if (
            job.meta.get("presentation_kind") == "legacy"
            and not content.clips
            and outline.strip()
            and not skipped
        ):
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
            if (await run_file_io(prepared.path.stat)).st_size > self.settings.max_file_size:
                raise ValueError("صدای آماده‌شده از سقف حجم ربات بزرگ‌تر است.")
            duration = prepared.source_duration  # synthetic silence is not billed
            needed = self._reserve_estimate(duration)
            await self.db.reserve(job.user_id, job.submission_id, needed)
            job.transition_to("transcribing")
            await progress.update(55, "تبدیل صدای ارائه به متن")
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
        job.transition_to("uploading")
        await progress.update(70, "ارسال متن خام")
        raw_text = "\n\n\n".join(part for part in raw_text_parts if part.strip())
        # Include skip reasons when any clip was dropped, so the user knows
        # why a narration file did not make it into the transcript.
        if skipped:
            raw_text += "\n\n---\nنکته: این موارد در پردازش نادیده گرفته شدند:\n" + "\n".join(f"• {reason}" for reason in skipped)
        engine_label = raw_engine or ("slides" if prepared is None else "stt")
        raw_path = workdir / raw_text_filename(reference)
        await asyncio.gather(
            self.db.save_transcription(
                job.submission_id,
                engine=engine_label,
                text=raw_text,
                notes_json="",
            ),
            run_file_io(
                raw_path.write_text, raw_text, encoding="utf-8", newline="\n"
            ),
        )
        await self._deliver_raw(job, raw_text, raw_path, reference)

        # Settle only after the transcript has actually reached the user. The
        # source audio is measured by the worker; synthetic silence is excluded.
        # A slide-only deck has no audio, so there is no hold and nothing
        # to charge — but the delivery milestone still has to be recorded,
        # otherwise the admin stats and the delivered/partial mapping miss
        # every text-only deck.
        settled_duration = prepared.source_duration if prepared is not None else 0
        await asyncio.gather(
            self._send_preview(job, raw_text),
            self._settle(job, settled_duration),
        )
        if not self.settings.note_configured:
            await self.db.set_submission_status(job.submission_id, "done")
            await progress.complete("متن کامل ارائه ارسال شد.")
            return
        job.transition_to("generating_notes")
        await progress.update(85, "تولید جزوهٔ ساختارمند")
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
        job.transition_to("uploading")
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

    # ----------------------------------------------------------- notes + docx
    def _progressive_booklet_sender(
        self,
        job: Job,
        workdir: Path,
        reference: str,
        meta: DocumentMeta,
    ):
        """A callback that delivers the deterministic merge as a draft booklet.

        The editorial compile is a second full round trip; delivering the
        audited merge first takes it off the user's critical path. A failure
        here is logged and ignored — the compiled booklet is still delivered by
        the normal path, so this can never lose a job.
        """

        async def send(merged: StructuredNotes) -> None:
            try:
                docx_bytes = await asyncio.to_thread(
                    build_notes_docx,
                    merged,
                    fonts=resolve_fonts(),
                    design=resolve_design(),
                    meta=meta,
                )
                path = workdir / f"{reference}-draft.docx"
                await run_file_io(path.write_bytes, docx_bytes)
                await self._send_file(
                    job, path, caption=f"📄 نسخهٔ نخست جزوه — {reference}"
                )
                logger.info("progressive booklet delivered reference=%s", reference)
            except Exception:  # noqa: BLE001 - the final booklet still follows
                logger.warning(
                    "progressive booklet delivery failed reference=%s",
                    reference,
                    exc_info=True,
                )

        return send

    async def _generate_notes(
        self,
        document_text: str,
        job: Job,
        progress,
        *,
        workdir: Path | None = None,
        reference: str = "",
        meta: DocumentMeta | None = None,
    ) -> StructuredNotes | None:
        if not self.settings.note_configured:
            logger.info("note provider not configured; delivering raw only")
            return None
        on_merged = None
        if (
            self.settings.note_progressive_booklet
            and workdir is not None
            and reference
            and meta is not None
        ):
            on_merged = self._progressive_booklet_sender(job, workdir, reference, meta)
        try:
            return await structure_transcript(
                document_text, self.settings, self._http_session(),
                mode=self.settings.note_mode,
                on_merged=on_merged,
            )
        except Exception as exc:
            logger.warning("note generation failed; raw already delivered: %s", exc)
            await self._reply(
                job,
                "ℹ️ تولید جزوهٔ ساختارمند انجام نشد؛ فایل سادهٔ Word و متن خام ارسال می‌شود.",
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
                "ℹ️ تولید جزوهٔ ساختارمند انجام نشد؛ فایل سادهٔ Word و متن خام ارسال می‌شود.",
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
        await progress.update(94, "آماده‌سازی فایل Word")
        notice = ""
        notice_kind = ""
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
                await run_file_io(docx_path.write_bytes, docx_bytes)
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
                await run_file_io(docx_path.write_bytes, docx_bytes)
            except Exception:
                logger.exception("plain docx generation failed too")
                notice = "\n\nنکته: فایل Word ساخته نشد؛ متن خام ارسال شد."
                notice_kind = "notes"

        if docx_path is not None:
            # The library stores the clean one-line title; the learning CTA
            # lives only in the Telegram caption (which _send_file caps at the
            # 1024-char limit, truncating the trailing guide before the title).
            title = f"📖 جزوهٔ Word — {reference}"
            caption = (
                title + "\n📚 در کتابخانهٔ شما ذخیره شد" + LEARNING_DELIVERY_CTA
            )
            # Presentations pass their built document as plain_text and have no
            # transcript object; media jobs carry the LLM-source transcript.
            learning_source = (
                plain_text
                if plain_text is not None
                else (document.llm_document() if document is not None else "")
            )
            learning_text = bound_content_text(learning_source)
            try:
                message = await self._send_file(job, docx_path, caption=caption)
                if message is not None:
                    await self._save_library_item(
                        job,
                        "booklet",
                        message,
                        title,
                        content_text=learning_text or None,
                    )
            except Exception:
                logger.exception("docx delivery failed")
                notice = "\n\nنکته: ارسال فایل Word ناموفق بود؛ متن خام ارسال شد."
                notice_kind = "delivery"

        await self.db.set_submission_status(
            job.submission_id, "partial" if notice else "done",
            error=notice.strip() or None,
            error_kind=notice_kind or None,
        )
        if notice:
            job.meta["outcome"] = "partial"
            await progress.complete("متن خام ارسال شد. " + notice.strip())
        else:
            await progress.complete()

    async def _send_telegram_file(
        self, entity, path: Path, **kwargs
    ) -> Message | None:
        """Send one path through Telethon's awaitable-read stream interface."""
        path = Path(path)
        file_size = (await run_file_io(path.stat)).st_size
        async with self._upload_semaphore:
            stream = await AsyncFileReader.open(path)
            try:
                return await asyncio.wait_for(
                    self.client.send_file(
                        entity, stream, file_size=file_size, **kwargs
                    ),
                    timeout=self.settings.upload_timeout,
                )
            finally:
                await stream.aclose()

    async def _save_library_item(
        self,
        job: Job,
        kind: str,
        message: Message,
        title: str,
        *,
        content_text: str | None = None,
    ) -> None:
        """Best-effort library index: a database hiccup can never fail delivery.

        Telegram owns the file; this stores only its message reference. Pruning
        follows insertion and is deliberately inside the same best-effort
        boundary, so a successful transcript/booklet always completes billing
        and progress even if either write is temporarily unavailable. The
        bounded learning text (quiz/flashcard source) is archived in the same
        boundary; legacy rows simply keep ``content_text = NULL``.
        """
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            logger.warning(
                "delivered library item has no Telegram message reference submission=%s",
                job.submission_id,
            )
            return
        try:
            item_id = await self.db.library_add(
                job.user_id,
                kind,
                chat_id,
                message_id,
                job.submission_id,
                title=title,
            )
            if content_text:
                await self.db.library_set_content(item_id, content_text)
            await self.db.library_prune(
                job.user_id, self.settings.library_max_items
            )
        except Exception:
            logger.warning(
                "could not save delivered artifact to the library submission=%s kind=%s",
                job.submission_id,
                kind,
                exc_info=True,
            )

    async def _send_file(
        self, job: Job, path: Path, *, caption: str = ""
    ) -> Message | None:
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
        target = await job.message.get_input_chat()
        caption_arg = caption[:TELEGRAM_CAPTION_LIMIT] or None
        for attempt in range(2):
            try:
                return await self._send_telegram_file(
                    target,
                    path,
                    caption=caption_arg,
                    force_document=True,
                    reply_to=job.message.id,
                )
            except FloodWaitError as exc:
                wait_seconds = float(getattr(exc, "seconds", 0) or 0)
                if attempt == 0 and 0 < wait_seconds <= 15.0:
                    logger.warning(
                        "Telegram FloodWait %.1fs on send_file; retrying once",
                        wait_seconds,
                    )
                    await asyncio.sleep(wait_seconds)
                    continue
                raise
        return None

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
            caption = f"📝 متن خام — {reference}"
            message = await self._send_file(job, raw_path, caption=caption)
            if message is not None:
                await self._save_library_item(
                    job,
                    "transcript",
                    message,
                    caption,
                    content_text=bound_content_text(raw_text) or None,
                )
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
                await run_file_io(
                    part_path.write_text, part, encoding="utf-8", newline="\n"
                )
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
                await run_file_io(part_path.unlink, missing_ok=True)
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
    @staticmethod
    def _payment_status_label(status: str) -> str:
        return {
            "pending": "⏳ منتظر پرداخت",
            "awaiting_payment": "⏳ منتظر پرداخت",
            "pending_review": "🕒 در انتظار بررسی",
            "approved": "✅ تأیید شده",
            "rejected": "❌ رد شده",
            "cancelled": "لغو شده",
        }.get(status, "نامشخص")

    async def _show_wallet(self, event, *, payments_page: int = 0) -> None:
        """One scrollable purchase view: plans, then balance, then history.

        CR-1: the purchase block leads the screen; balance and recent payments
        stay on the same single screen, only moved below the plans.
        """
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 5
        payments_page = max(0, int(payments_page))
        lines = [
            "🛒 خرید اشتراک",
            "",
        ]
        buttons: list[list] = []

        # The active order is the first keyboard row so a pending purchase can
        # be resumed or cancelled without paging through the history.
        latest = await self.db.payment_history_paginated(user_id, limit=1)
        if latest and is_open_payment(latest[0]["status"]):
            payment = latest[0]
            order_buttons = [
                Button.inline(
                    "🔎 پیگیری سفارش باز",
                    f"payopen:{payment['payment_id']}".encode("ascii"),
                )
            ]
            if not payment["receipt_path"]:
                order_buttons.append(
                    Button.inline(
                        "لغو سفارش",
                        f"payc:{payment['payment_id']}".encode("ascii"),
                    )
                )
            buttons.append(order_buttons)

        plans = [plan for plan in await self.db.plans() if plan["price"] > 0]
        if not plans:
            lines.append("فعلاً بسته‌ای برای خرید موجود نیست.")
        else:
            lines.append("بسته را انتخاب کنید؛ اعتبار خرید به موجودی شما اضافه می‌شود.")
            for plan in plans:
                hours = float(plan["hours"])
                hours_text = to_persian_digits(
                    int(hours) if hours.is_integer() else str(hours)
                )
                duration = format_duration(int(round(hours * 3600)))
                validity = format_validity(plan["validity_days"])
                lines.append(
                    f"\n• {duration}\n  اعتبار: {validity} | مبلغ: {format_toman(plan['price'])}"
                )
                if self.settings.payment_card_number:
                    label = button_label(
                        f"{hours_text} ساعت — {format_toman(plan['price'])}"
                    )
                    buttons.append(
                        [Button.inline(label, f"plan:{plan['code']}".encode("utf-8"))]
                    )
            if not self.settings.payment_card_number:
                lines.append("\n🛒 پرداخت کارت‌به‌کارت فعلاً غیرفعال است.")

        # Balance and history follow the plans (purchase-first ordering).
        lines.extend([
            "",
            f"💳 اعتبار باقی‌مانده: {await self._balance_text(user_id)}",
            "",
            "🧾 پرداخت‌های اخیر",
        ])
        payments = await self.db.payment_history_paginated(
            user_id, limit=page_size + 1, offset=payments_page * page_size
        )
        has_more = len(payments) > page_size
        payments = payments[:page_size]
        if not payments:
            lines.append("پرداختی ثبت نشده است." if payments_page == 0 else "مورد دیگری وجود ندارد.")
        for item in payments:
            seconds = item.get("purchased_seconds")
            plan_text = format_duration(int(seconds)) if seconds else item["plan_code"]
            price = item.get("purchased_price")
            amount = format_toman(int(price)) if price is not None else "مبلغ نامشخص"
            lines.append(
                f"\n#{to_persian_digits(item['payment_id'])} | {plan_text} | {amount}\n"
                f"{self._payment_status_label(item['status'])}"
            )
            if item["status"] == "rejected" and item.get("review_reason"):
                lines.append(f"دلیل: {item['review_reason']}")

        nav = []
        if payments_page > 0:
            nav.append(
                Button.inline("◀️ قبلی", f"walletp:{payments_page - 1}".encode("ascii"))
            )
        if has_more:
            nav.append(
                Button.inline("بعدی ▶️", f"walletp:{payments_page + 1}".encode("ascii"))
            )
        if nav:
            buttons.append(nav)
        buttons.extend(
            [
                [Button.inline("🎧 پشتیبانی", b"support")],
                [Button.inline("↩️ منوی اصلی", b"menu:main")],
            ]
        )
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _start_payment(self, event, plan_code: str) -> None:
        if not self.settings.payment_card_number:
            # Never create a payable request the user cannot pay for: without
            # a configured card there is nowhere to send the money.
            await event.respond(
                "🛒 پرداخت کارت‌به‌کارت فعلاً فعال نیست؛ کمی بعد دوباره امتحان کنید."
            )
            return
        try:
            payment_id = await self.db.create_payment(event.sender_id, plan_code)
        except OpenPaymentError:
            await event.respond(
                "⚠️ یک سفارش باز دارید؛ اول آن را پیگیری یا لغو کنید.",
                buttons=[[Button.inline("🛒 خرید اشتراک", b"wallet")]],
            )
            return
        except ValueError:
            await event.respond("این طرح در دسترس نیست.")
            return
        await self._show_payment(event, payment_id)

    async def _show_payment(self, event, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if (payment is None or payment["user_id"] != event.sender_id
                or not is_open_payment(payment["status"])):
            await event.respond(
                "این سفارش فعال نیست. وضعیت را در «🛒 خرید اشتراک» ببینید.",
                buttons=[[Button.inline("🛒 خرید اشتراک", b"wallet")]],
            )
            return
        if not payment["purchased_seconds"] or payment["purchased_price"] is None:
            await event.respond(
                "اطلاعات این سفارش ناقص است؛ لطفاً درخواست پشتیبانی ثبت کنید.",
                buttons=[[Button.inline("🎧 پشتیبانی", b"support")]],
            )
            return
        # The request snapshot, not the mutable catalog, is the quote.
        plan = {"hours": payment["purchased_seconds"] / 3600,
                "price": payment["purchased_price"],
                "validity_days": payment["purchased_validity_days"]}
        hours = plan["hours"]
        hours_text = to_persian_digits(int(hours) if float(hours).is_integer() else hours)
        order_ref = to_persian_digits(payment_id)
        summary = (
            f"🧾 سفارش {order_ref}\n"
            f"بسته: {hours_text} ساعت | مبلغ: {format_toman(plan['price'])}\n"
            f"اعتبار بسته: {format_validity(plan['validity_days'])}"
        )
        if payment["receipt_path"]:
            # Review status remains available even if new purchases are later
            # disabled; removing the card configuration must not hide it.
            self._pending_payments.pop(event.sender_id, None)
            text = (
                f"{summary}\n\n"
                "🕒 رسید پرداخت شما ثبت شد و در انتظار بررسی است.\n"
                "نیازی به واریز مجدد نیست؛ پس از بررسی مدیر، نتیجه اطلاع‌رسانی می‌شود."
            )
            buttons = []
        elif not self.settings.payment_card_number:
            self._pending_payments.pop(event.sender_id, None)
            text = (
                f"{summary}\n\n"
                "پرداخت کارت‌به‌کارت فعلاً در دسترس نیست؛ سفارش را لغو کنید یا بعداً امتحان کنید."
            )
            buttons = [[Button.inline("لغو سفارش", f"payc:{payment_id}".encode())]]
        else:
            self._pending_payments[event.sender_id] = payment_id
            card = format_payment_card(self.settings.payment_card_number)
            text = (
                f"{summary}\n\n"
                "اطلاعات واریز:\n"
                f"💳 شماره کارت: {card}\n"
                f"به نام: {self.settings.payment_card_holder}\n"
                f"بانک: {self.settings.payment_bank_name}\n\n"
                "پس از واریز، تصویر رسید را بفرستید. اعتبار فقط پس از تأیید مدیر اضافه می‌شود."
            )
            buttons = [
                [Button.inline("📤 ارسال رسید", f"payr:{payment_id}".encode())],
                [Button.inline("لغو سفارش", f"payc:{payment_id}".encode())],
            ]
        buttons.append([Button.inline("🛒 خرید اشتراک", b"wallet")])
        await event.respond(text, buttons=buttons)

    async def _cancel_payment(self, event, payment_id: int) -> None:
        changed = await self.db.cancel_payment(payment_id, event.sender_id)
        self._pending_payments.pop(event.sender_id, None)
        await event.respond(
            "سفارش لغو شد." if changed else
            "این سفارش قابل لغو نیست؛ رسید ارسال‌شده باید توسط مدیر بررسی شود."
        )
        await self._show_wallet(event)

    async def _await_receipt(self, event, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if (
            payment is None
            or payment["status"] not in ("awaiting_payment", "pending")
            or payment["receipt_path"]
            or payment["user_id"] != event.sender_id
        ):
            await event.respond("این سفارش برای ارسال رسید فعال نیست.")
            return
        self._pending_payments[event.sender_id] = payment_id
        await event.respond(
            "📷 تصویر خوانای رسید را به‌صورت عکس یا فایل تصویری بفرستید.",
            buttons=[[Button.inline("↩️ بازگشت", f"payopen:{payment_id}".encode())]],
        )

    async def _handle_receipt_upload(
        self, event, user_id: int, *, payment_id: int | None = None
    ) -> None:
        if payment_id is None:
            payment_id = self._pending_payments.pop(user_id, None)
        else:
            self._pending_payments.pop(user_id, None)
        if payment_id is None:
            return
        payment = await self.db.payment(payment_id)
        if (
            payment is None
            or payment["status"] not in ("awaiting_payment", "pending")
            or payment["receipt_path"]
            or payment["user_id"] != user_id
        ):
            self._pending_payments.pop(user_id, None)
            await event.respond("این سفارش برای ارسال رسید فعال نیست.")
            return
        if not _is_receipt_image(event.message):
            self._pending_payments[user_id] = payment_id
            await event.respond("رسید باید عکس یا فایل تصویری JPEG/PNG/WebP باشد.")
            return
        size = getattr(event.message.file, "size", None)
        if isinstance(size, int) and size > self.settings.max_payment_receipt_bytes:
            await event.respond("حجم تصویر رسید زیاد است؛ فشرده‌تر بفرستید.")
            self._pending_payments[user_id] = payment_id
            return
        await run_file_io(
            self.settings.receipt_dir.mkdir, parents=True, exist_ok=True
        )
        token = secrets.token_hex(8)
        receipt_path = self.settings.receipt_dir / f"receipt-{payment_id}-{token}.jpg"
        try:
            await self._download_telegram_media(
                event.message, receipt_path, self.settings.max_payment_receipt_bytes
            )
        except DiskSpaceError:
            self._pending_payments[user_id] = payment_id
            await event.respond("فضای موقت سرور کافی نیست؛ رسید را کمی بعد دوباره بفرستید.")
            return
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
            if not await run_file_io(
                _normalize_receipt_image,
                receipt_path,
                max_bytes=self.settings.max_payment_receipt_bytes,
            ):
                await run_file_io(receipt_path.unlink, missing_ok=True)
                await event.respond("فرمت رسید نامعتبر است؛ فقط JPEG/PNG/WebP پذیرفته می‌شود.")
                self._pending_payments[user_id] = payment_id
                return
            await run_file_io(receipt_path.chmod, 0o600)
        except OSError:
            await run_file_io(receipt_path.unlink, missing_ok=True)
            self._pending_payments[user_id] = payment_id
            await event.respond("ذخیرهٔ رسید ناموفق بود؛ لطفاً دوباره ارسال کنید.")
            return
        try:
            changed = await self.db.set_payment_receipt(
                payment_id, str(receipt_path), user_id=user_id
            )
        except sqlite3.Error:
            await run_file_io(receipt_path.unlink, missing_ok=True)
            self._pending_payments[user_id] = payment_id
            logger.exception("payment receipt storage failed payment_id=%s", payment_id)
            await event.respond("ثبت رسید انجام نشد؛ لطفاً دوباره ارسال کنید.")
            return
        if not changed:
            await run_file_io(receipt_path.unlink, missing_ok=True)
            await event.respond("این سفارش دیگر برای ارسال رسید فعال نیست.")
            return
        await event.respond(
            "🕒 رسید پرداخت شما ثبت شد و در انتظار بررسی است.\n"
            "پس از بررسی مدیر، نتیجه اطلاع‌رسانی می‌شود."
        )
        await self._notify_admins_of_payment(payment_id)

    async def _notify_admins_of_payment(self, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if payment is None:
            return
        seconds = int(payment.get("purchased_seconds") or 0)
        price = payment.get("purchased_price")
        text = (
            f"🧾 رسید سفارش {to_persian_digits(payment_id)}\n"
            f"کاربر: {payment['user_id']}\n"
            f"بسته: {format_duration(seconds) if seconds else payment['plan_code']}\n"
            f"مبلغ: {format_toman(int(price)) if price is not None else 'نامشخص'}"
        )
        buttons = [[Button.inline(
            "🔎 جزئیات سفارش", f"adm:pd:{payment_id}".encode()
        )]]
        path = payment.get("receipt_path")
        receipt_ready = bool(
            path
            and await run_file_io(
                _is_safe_receipt_path, self.settings.receipt_dir, path
            )
        )
        for admin_id in sorted(self.settings.admin_ids):
            try:
                if receipt_ready:
                    try:
                        await self._send_telegram_file(
                            admin_id,
                            Path(str(path)),
                            caption=text[:TELEGRAM_CAPTION_LIMIT],
                            buttons=buttons,
                        )
                    except Exception:
                        # A temporary image-upload failure should not suppress
                        # the review alert itself. The stored receipt remains
                        # available from the order-detail screen.
                        logger.warning(
                            "payment receipt delivery failed payment_id=%s",
                            payment_id,
                        )
                        await self.client.send_message(
                            admin_id,
                            text + "\n⚠️ رسید تصویری ارسال نشد؛ آن را از جزئیات سفارش ببینید.",
                            buttons=buttons,
                        )
                else:
                    await self.client.send_message(
                        admin_id,
                        text + "\n⚠️ فایل رسید در دسترس نیست.",
                        buttons=buttons,
                    )
            except Exception:
                logger.warning(
                    "payment notification not delivered payment_id=%s",
                    payment_id,
                )

    REJECTION_REASON_PRESETS = {
        "blur": "تصویر رسید ناخوانا است؛ تصویر واضح‌تری ارسال کنید.",
        "amount": "مبلغ واریزی با مبلغ بسته مطابقت ندارد.",
        "invalid": "اطلاعات تراکنش ناقص یا تکراری است.",
    }

    async def _review_payment(self, event, data: str) -> None:
        """Handle payv:<id>:a, payv:<id>:a:yes, payv:<id>:r and payv:<id>:rp:<code > safely."""
        parts = data.split(":")
        if (
            len(parts) not in (3, 4)
            or parts[0] != "payv"
            or parts[2] not in ("a", "r", "rp")
            or (len(parts) == 4 and parts[2] == "a" and parts[3] != "yes")
            or (len(parts) == 4 and parts[2] == "rp" and parts[3] not in self.REJECTION_REASON_PRESETS)
            or (len(parts) == 3 and parts[2] == "rp")
            or (len(parts) == 4 and parts[2] == "r")
        ):
            await self._ack(event, "درخواست نامعتبر است.", alert=True)
            return
        payment_id = _to_int(parts[1])
        if payment_id is None:
            await self._ack(event, "شناسه سفارش نامعتبر است.", alert=True)
            return
        verdict = parts[2]
        payment = await self.db.payment(payment_id)
        if payment is None or not is_pending_payment_review(payment):
            await self._ack(
                event,
                "سفارش قابل بررسی نیست؛ ابتدا رسید باید ثبت شود یا سفارش قبلاً بررسی شده است.",
                alert=True,
            )
            await self._show_admin_payment_detail(event, payment_id)
            return
        if verdict == "rp":
            await self._ack(event)
            preset_reason = self.REJECTION_REASON_PRESETS[parts[3]]
            await self._reject_payment_with_reason(event, payment_id, preset_reason)
            return
        if verdict == "r":
            self._pending_reject_reason[event.sender_id] = payment_id
            await self._ack(event, "برای رد پرداخت، دلیل لازم است.", alert=True)
            await event.respond(
                f"دلیل رد سفارش {to_persian_digits(payment_id)} را انتخاب کنید یا در یک پیام بنویسید.\n"
                "برای لغو، /cancel بفرستید.",
                buttons=[
                    [Button.inline("📷 تصویر رسید ناخوانا", f"payv:{payment_id}:rp:blur".encode())],
                    [Button.inline("💰 عدم تطابق مبلغ", f"payv:{payment_id}:rp:amount".encode())],
                    [Button.inline("⚠️ اطلاعات ناقص یا تکراری", f"payv:{payment_id}:rp:invalid".encode())],
                    [Button.inline("↩️ بازگشت", f"adm:pd:{payment_id}".encode())],
                ],
            )
            return
        if len(parts) == 3:
            # The first approval tap only renders a confirmation; it never
            # changes payment or credit state.
            await self._ack(event)
            seconds = int(payment.get("purchased_seconds") or 0)
            price = payment.get("purchased_price")
            await self._edit_or_reply(
                event,
                f"✅ تأیید سفارش {to_persian_digits(payment_id)}\n\n"
                f"کاربر: {payment['user_id']}\n"
                f"اعتبار: {format_duration(seconds)}\n"
                f"مبلغ: {format_toman(int(price)) if price is not None else 'نامشخص'}\n\n"
                "پس از تأیید نهایی، اعتبار به حساب کاربر اضافه می‌شود.",
                [
                    [Button.inline("✅ تأیید نهایی", f"payv:{payment_id}:a:yes".encode())],
                    [Button.inline("↩️ بازگشت", f"adm:pd:{payment_id}".encode())],
                ],
            )
            return
        try:
            changed = await self.db.review_payment(payment_id, True, event.sender_id)
        except PaymentSnapshotError:
            logger.error("payment approval without a valid snapshot payment_id=%s", payment_id)
            await self._ack(
                event,
                "اطلاعات بسته یا مبلغ ناقص است؛ تأیید انجام نشد.",
                alert=True,
            )
            return
        except sqlite3.Error:
            # review_payment is atomic. The order remains pending_review and
            # the same confirmation can be retried without duplicating credit.
            logger.exception("payment activation failed payment_id=%s", payment_id)
            await self._ack(
                event,
                "فعال‌سازی انجام نشد. سفارش همچنان در انتظار بررسی است؛ بعداً دوباره تلاش کنید.",
                alert=True,
            )
            return
        if not changed:
            await self._ack(event, "این سفارش قبلاً بررسی شده است.", alert=True)
        else:
            await self._ack(event, "پرداخت تأیید و اعتبار فعال شد.")
            seconds = int(payment["purchased_seconds"] or 0)
            try:
                await self.client.send_message(
                    payment["user_id"],
                    "✅ پرداخت شما تأیید شد. اشتراک شما فعال است.\n"
                    f"💳 {format_duration(seconds)} اعتبار اضافه شد.",
                    buttons=[[Button.inline("🛒 خرید اشتراک", b"wallet")]],
                )
            except Exception:
                logger.warning(
                    "payment approval notification not delivered payment_id=%s",
                    payment_id,
                )
        await self._show_admin_payment_detail(event, payment_id)

    async def _reject_payment_with_reason(
        self, event, payment_id: int, reason: str
    ) -> None:
        send_reply = event.reply if hasattr(event, "reply") else event.respond
        reason = reason.strip()
        if not reason:
            await send_reply(
                "برای رد پرداخت، واردکردن دلیل الزامی است.", buttons=self._admin_menu()
            )
            return
        payment = await self.db.payment(payment_id)
        try:
            changed = await self.db.review_payment(
                payment_id, False, event.sender_id, reason=reason
            )
        except sqlite3.Error:
            logger.exception("payment rejection failed payment_id=%s", payment_id)
            await send_reply("رد پرداخت ثبت نشد؛ لطفاً دوباره تلاش کنید.")
            self._pending_reject_reason[event.sender_id] = payment_id
            return
        if not changed:
            await send_reply("این سفارش قبلاً بررسی شده است یا رسید ندارد.")
        else:
            await send_reply(f"❌ سفارش {to_persian_digits(payment_id)} رد شد.")
            if payment is not None:
                try:
                    await self.client.send_message(
                        payment["user_id"],
                        f"❌ پرداخت تأیید نشد.\nدلیل: {reason[:400]}\n"
                        "برای پیگیری می‌توانید درخواست پشتیبانی ثبت کنید.",
                        buttons=[[Button.inline("🎧 پشتیبانی", b"support")]],
                    )
                except Exception:
                    logger.warning(
                        "payment rejection notification not delivered payment_id=%s",
                        payment_id,
                    )
        await self._show_admin_payment_detail(event, payment_id)

    # ----------------------------------------------------------------- admin
    #: Reporting windows the dashboard offers. "7d"/"30d" are rolling windows
    #: ending now, "today" is the current UTC day and "all" is the whole history;
    #: every screen names the window it is showing.
    ADMIN_PERIODS: tuple[tuple[str, str], ...] = (
        ("today", "امروز"),
        ("7d", "۷ روز"),
        ("30d", "۳۰ روز"),
        ("all", "کل"),
    )
    ADMIN_PERIOD_DAYS: dict[str, int] = {"7d": 7, "30d": 30}
    #: Rows of the day chart one screen may render (Telegram messages are bounded).
    ADMIN_SERIES_LIMIT = 10
    #: p95 over fewer samples than this is noise; the screens show the sample
    #: count and the median instead of a misleading tail figure.
    ADMIN_P95_MIN_SAMPLES = 20
    #: Clock the dashboard uses for its windows. Reporting is UTC end to end:
    #: rows store UTC ISO-8601, so a "day" is a UTC day everywhere.
    ADMIN_PERIOD_KEYS = frozenset(key for key, _label in ADMIN_PERIODS)

    def _admin_period_start(self, period: str) -> str | None:
        """UTC ISO start of one reporting window, or ``None`` for all history."""
        now = utc_now()
        if period == "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(
                timespec="seconds"
            )
        days = self.ADMIN_PERIOD_DAYS.get(period)
        if days is None:
            return None
        return (now - timedelta(days=days)).isoformat(timespec="seconds")

    def _admin_period_label(self, period: str, report: dict) -> str:
        """Human window label; the dashboard is UTC everywhere, so it says so."""
        title = dict(self.ADMIN_PERIODS).get(period, "کل")
        start = report.get("period", {}).get("start")
        if not start:
            return f"{title} (از آغاز، UTC)"
        try:
            return f"{title} — از {jalali_date(datetime.fromisoformat(start))} (UTC)"
        except ValueError:
            return f"{title} (UTC)"

    def _admin_period_row(self, prefix: str, period: str) -> list:
        """The window selector shown on every dashboard screen."""
        row = []
        for key, label in self.ADMIN_PERIODS:
            marker = "• " if key == period else ""
            row.append(Button.inline(f"{marker}{label}", f"{prefix}:{key}".encode()))
        return row

    @staticmethod
    def _rate_text(rate: float | None) -> str:
        if rate is None:
            return "—"
        return to_persian_digits(f"{round(rate * 100)}٪")

    @staticmethod
    def _duration_text(seconds: float | None) -> str:
        if seconds is None:
            return "—"
        return format_duration(int(round(seconds)))

    def _timing_line(self, label: str, summary: dict) -> str:
        """One average/median/p95 line that always states its sample count."""
        count = int(summary.get("count") or 0)
        if count <= 0:
            return f"{label}: داده‌ای ثبت نشده"
        parts = [f"میانگین {self._duration_text(summary.get('avg_seconds'))}"]
        if summary.get("median_seconds") is not None:
            parts.append(f"میانه {self._duration_text(summary['median_seconds'])}")
        if count >= self.ADMIN_P95_MIN_SAMPLES and summary.get("p95_seconds") is not None:
            parts.append(f"p95 {self._duration_text(summary['p95_seconds'])}")
        return f"{label}: " + " | ".join(parts) + f" (n={to_persian_digits(count)})"

    @staticmethod
    def _day_label(day: str) -> str:
        try:
            return jalali_date(datetime.fromisoformat(day))
        except ValueError:
            return day

    @staticmethod
    def _series_bar(value: int, maximum: int, width: int = 5) -> str:
        if value <= 0 or maximum <= 0:
            return "·"
        filled = max(1, round(width * min(value, maximum) / maximum))
        return "▇" * filled

    @staticmethod
    def _bytes_text(value: int) -> str:
        size = float(max(0, int(value)))
        if size == 0:
            # A zero byte count is a byte count, not a fractional kilobyte.
            # ``size`` is the clamped value, so a negative input stays "۰ بایت".
            return f"{to_persian_digits(int(size))} بایت"
        for unit in ("KB", "MB", "GB"):
            size /= 1024.0
            if size < 1024 or unit == "GB":
                return f"{to_persian_digits(f'{size:.1f}')} {unit}"
        return f"{to_persian_digits(int(value))} بایت"

    @staticmethod
    def _timestamp_text(value: str | None) -> str:
        if not value:
            return "ثبت نشده"
        try:
            at = datetime.fromisoformat(value)
        except ValueError:
            return value
        return f"{jalali_date(at)} — {to_persian_digits(at.strftime('%H:%M'))} UTC"

    def _admin_error_label(self, kind: str) -> str:
        return FAILURE_KIND_LABELS_FA.get(kind, kind)

    def _admin_attention_lines(self, report: dict, runtime: dict) -> list[str]:
        """What needs an administrator now, in priority order.

        Each item is derived from a count the dashboard already computed; a
        line only appears when the count is non-zero, so an empty list means
        "nothing to do", not "not measured".
        """
        payments = report["payments"]
        processing = report["processing"]
        items: list[str] = []
        if payments["pending_review"]:
            items.append(
                f"🧾 {to_persian_digits(payments['pending_review'])} سفارش در انتظار بررسی رسید"
            )
        if processing["undelivered"]:
            top = processing["errors"][0] if processing["errors"] else None
            suffix = f" — بیشترین: {self._admin_error_label(top['kind'])}" if top else ""
            items.append(
                f"❌ {to_persian_digits(processing['undelivered'])} درخواست ناموفق در این بازه{suffix}"
            )
        expiring = payments["expiring"]
        if expiring["count"]:
            items.append(
                f"⏳ {to_persian_digits(expiring['count'])} بستهٔ اعتبار تا "
                f"{to_persian_digits(expiring['within_days'])} روز آینده منقضی می‌شود "
                f"({format_duration(expiring['seconds'])})"
            )
        db_errors = int(runtime.get("db_errors", 0) or 0)
        if db_errors:
            items.append(
                f"🗄 {to_persian_digits(db_errors)} خطای پایگاه‌داده از شروع اجرای این نسخه"
            )
        transient = 0
        providers = runtime.get("providers", {}) or {}
        for layer in ("stt", "llm"):
            for stats in (providers.get(layer, {}) or {}).values():
                transient += sum(
                    int(stats.get(key, 0) or 0)
                    for key in ("timeouts", "rate_limits", "server_errors")
                )
        if transient:
            items.append(
                f"🌐 {to_persian_digits(transient)} خطای موقت ارائه‌دهنده "
                "(timeout / محدودیت نرخ / خطای سرور) از شروع اجرا"
            )
        if not items:
            items.append("✅ مورد فوری‌ای در انتظار نیست.")
        return items

    async def _show_admin_overview(self, event, period: str = "7d") -> None:
        """The dashboard: three questions answered before any drill-down."""
        period = period if period in self.ADMIN_PERIOD_KEYS else "7d"
        report = await self.db.admin_report(period_start=self._admin_period_start(period))
        processing = report["processing"]
        users = report["users"]
        payments = report["payments"]
        approved = payments["by_status"].get("approved", {"count": 0, "toman": 0})
        rejected = payments["by_status"].get("rejected", {"count": 0, "toman": 0})
        total_jobs = sum(processing["by_status"].values())
        active_now = self.queue.active_count
        queued_now = self.queue.pending
        lines = [
            "🛠 پنل مدیریت — نمای کلی",
            f"🗓 بازه: {self._admin_period_label(period, report)}",
            "",
            "👥 کاربران",
            f"• کل ثبت‌شده: {to_persian_digits(users['total'])}"
            f" | جدید در بازه: {to_persian_digits(users['new'])}"
            f" | فعال در بازه: {to_persian_digits(users['active'])}",
            "",
            "⚙️ پردازش",
            f"• درخواست در بازه: {to_persian_digits(total_jobs)}"
            f" | تحویل‌شده: {to_persian_digits(processing['delivered'])}"
            f" | ناموفق: {to_persian_digits(processing['undelivered'])}"
            f" | نرخ موفقیت: {self._rate_text(processing['success_rate'])}",
            "• " + self._timing_line("زمان پاسخ", processing["end_to_end"]),
            f"• اکنون: {to_persian_digits(active_now)} فعال"
            f" | {to_persian_digits(queued_now)} در صف",
            "",
            "💳 پرداخت و اعتبار",
            f"• در انتظار بررسی: {to_persian_digits(payments['pending_review'])}"
            f" | تأیید در بازه: {to_persian_digits(approved['count'])}"
            f" | رد در بازه: {to_persian_digits(rejected['count'])}",
            f"• مبلغ تأییدشده در بازه: {format_toman(approved['toman'])}",
            "",
            "🚨 نیازمند توجه",
        ]
        lines.extend(f"• {item}" for item in self._admin_attention_lines(report, self.runtime_metrics()))
        buttons = [self._admin_period_row("adm:ov", period)]
        buttons.append([
            Button.inline("👥 جزئیات کاربران", f"adm:ovu:{period}".encode()),
            Button.inline("⚙️ جزئیات پردازش", f"adm:ovp:{period}".encode()),
        ])
        buttons.append([
            Button.inline("💳 پرداخت و اعتبار", f"adm:ovpay:{period}".encode()),
            Button.inline("🩺 سلامت سیستم", f"adm:ovs:{period}".encode()),
        ])
        buttons.extend(self._admin_menu())
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    ADMIN_STATUS_LABELS_FA: dict[str, str] = {
        "pending": "در انتظار",
        "awaiting_payment": "در انتظار پرداخت",
        "pending_review": "در انتظار بررسی",
        "processing": "در حال پردازش",
        "done": "موفق",
        "partial": "ناقص (متن تحویل شد)",
        "failed": "ناموفق",
        "rejected": "ردشده",
        "approved": "تأییدشده",
    }
    ADMIN_KIND_LABELS_FA: dict[str, str] = {
        "audio": "صدا",
        "video": "ویدیو",
        "presentation": "ارائه / پاورپوینت",
        "office": "سند رایگان",
        "text": "متن",
    }
    ADMIN_ENGINE_LABELS_FA: dict[str, str] = {
        "speechmatics": "Speechmatics",
        "deepgram": "Deepgram",
        "openai_compatible": "OpenAI-compatible",
        "slides": "فقط اسلاید (بدون صدا)",
        "stt": "STT (بدون برچسب موتور)",
        "unrecorded": "ثبت‌نشده",
    }
    ADMIN_STAGE_LABELS_FA: dict[str, str] = {
        "downloading": "دریافت فایل",
        "extracting": "استخراج و آماده‌سازی صدا",
        "transcribing": "تبدیل گفتار",
        "generating_notes": "ساخت جزوه",
        "uploading": "ساخت و ارسال فایل",
    }

    def _admin_status_label(self, status: str) -> str:
        return self.ADMIN_STATUS_LABELS_FA.get(status, status)

    def _admin_kind_label(self, kind: str) -> str:
        return self.ADMIN_KIND_LABELS_FA.get(kind, kind)

    def _admin_engine_label(self, engine: str) -> str:
        return self.ADMIN_ENGINE_LABELS_FA.get(engine, engine)

    def _admin_screen_header(self, title: str, period: str, report: dict) -> list[str]:
        return [title, f"🗓 بازه: {self._admin_period_label(period, report)}", ""]

    def _admin_series_lines(self, series: list[dict], *, limit: int | None = None) -> list[str]:
        """A bounded day chart: one line per day, newest last."""
        rows = series[-int(limit or self.ADMIN_SERIES_LIMIT):]
        if not rows:
            return ["📈 در این بازه فعالیتی ثبت نشده است."]
        maximum = max(
            [max(int(row["jobs"]), int(row["new_users"])) for row in rows] + [1]
        )
        lines = ["📈 روند روزانه (UTC):"]
        for row in rows:
            lines.append(
                f"{self._day_label(row['day'])} {self._series_bar(int(row['jobs']), maximum)} "
                f"درخواست {to_persian_digits(int(row['jobs']))} "
                f"(✅ {to_persian_digits(int(row['delivered']))} / "
                f"❌ {to_persian_digits(int(row['undelivered']))}) "
                f"| کاربر جدید {to_persian_digits(int(row['new_users']))}"
            )
        return lines

    async def _show_admin_users_report(self, event, period: str = "7d") -> None:
        period = period if period in self.ADMIN_PERIOD_KEYS else "7d"
        report = await self.db.admin_report(
            period_start=self._admin_period_start(period), with_timings=False
        )
        users = report["users"]
        lines = self._admin_screen_header("👥 کاربران — جزئیات", period, report)
        lines.extend(
            [
                f"• کل ثبت‌شده: {to_persian_digits(users['total'])}",
                f"• جدید در بازه: {to_persian_digits(users['new'])}",
                f"• فعال در بازه (دست‌کم یک درخواست): {to_persian_digits(users['active'])}",
                f"• تحویل‌گرفته در بازه (دست‌کم یک خروجی): {to_persian_digits(users['delivered'])}",
                f"• مسدود: {to_persian_digits(users['banned'])}"
                f" | ویژه (بدون محدودیت): {to_persian_digits(users['unlimited'])}",
                "",
            ]
        )
        lines.extend(self._admin_series_lines(report["processing"]["series"]))
        lines.extend(
            [
                "",
                "ℹ️ «فعال» = کاربری که در بازه دست‌کم یک درخواست ثبت کرده است.",
                "ℹ️ «تحویل‌گرفته» = کاربری که در بازه دست‌کم یک خروجی موفق داشته است.",
                "ℹ️ درآمد و مصرف هر کاربر در بخش «پرداخت و اعتبار» است.",
            ]
        )
        buttons = [
            self._admin_period_row("adm:ovu", period),
            [Button.inline("↩️ نمای کلی", b"adm:home")],
        ]
        buttons.extend(self._admin_menu())
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _show_admin_processing_report(self, event, period: str = "7d") -> None:
        period = period if period in self.ADMIN_PERIOD_KEYS else "7d"
        period_start = self._admin_period_start(period)
        report = await self.db.admin_report(period_start=period_start)
        engines = await self.db.admin_stt_engines(period_start=period_start)
        processing = report["processing"]
        runtime = self.runtime_metrics()
        lines = self._admin_screen_header("⚙️ پردازش — جزئیات", period, report)

        total_jobs = sum(processing["by_status"].values())
        lines.extend(
            [
                "📦 حجم",
                f"• کل در بازه: {to_persian_digits(total_jobs)}"
                f" | تحویل‌شده: {to_persian_digits(processing['delivered'])}"
                f" | ناموفق/ردشده: {to_persian_digits(processing['undelivered'])}"
                f" | نرخ موفقیت: {self._rate_text(processing['success_rate'])}",
            ]
        )
        if processing["by_status"]:
            statuses = " | ".join(
                f"{self._admin_status_label(status)}: {to_persian_digits(count)}"
                for status, count in sorted(processing["by_status"].items())
            )
            lines.append(f"• وضعیت‌ها: {statuses}")
        if processing["by_kind"]:
            kinds = " | ".join(
                f"{self._admin_kind_label(kind)}: {to_persian_digits(count)}"
                for kind, count in sorted(processing["by_kind"].items())
            )
            lines.append(f"• نوع: {kinds}")

        lines.extend(
            [
                "",
                "⏱ زمان (فقط کارهای پایان‌یافته)",
                "• " + self._timing_line("پایان به پایان", processing["end_to_end"]),
                "• " + self._timing_line("پردازش خالص", processing["processing_time"]),
                "• " + self._timing_line("انتظار در صف", processing["queue_wait"]),
            ]
        )
        if 0 < processing["end_to_end"]["count"] < self.ADMIN_P95_MIN_SAMPLES:
            lines.append(
                f"ℹ️ برای p95 دست‌کم {to_persian_digits(self.ADMIN_P95_MIN_SAMPLES)} "
                "نمونه لازم است؛ تا آن حد فقط میانه نمایش داده می‌شود."
            )

        stage_lines = []
        stages = runtime.get("stages", {}) or {}
        for stage_key, label in self.ADMIN_STAGE_LABELS_FA.items():
            summary = (stages.get(stage_key, {}) or {})
            count = int(summary.get("count", 0) or 0)
            if count <= 0:
                continue
            stage_lines.append(
                f"• {label}: میانه {self._duration_text(summary.get('p50_seconds'))}"
                f" | p95 {self._duration_text(summary.get('p95_seconds'))}"
                f" (n={to_persian_digits(count)})"
            )
        if stage_lines:
            lines.extend(
                ["", "🧩 زمان مرحله‌ها (از شروع اجرای این نسخه):", *stage_lines]
            )

        if processing["errors"]:
            lines.extend(["", "🚨 خطاهای بازه (بر اساس دستهٔ ثبت‌شده):"])
            for item in processing["errors"]:
                lines.append(
                    f"• {self._admin_error_label(str(item['kind']))}: "
                    f"{to_persian_digits(int(item['count']))}"
                )
        else:
            lines.extend(["", "🚨 در این بازه خطای ثبت‌شده‌ای وجود ندارد."])

        if engines:
            usage = " | ".join(
                f"{self._admin_engine_label(str(item['engine']))}: "
                f"{to_persian_digits(int(item['count']))}"
                for item in engines
                if item["count"]
            )
            if usage:
                lines.extend(["", f"🎙 موتور تبدیل گفتار در بازه: {usage}"])

        failovers = {"stt": 0, "llm": 0}
        providers = runtime.get("providers", {}) or {}
        for layer in failovers:
            for stats in (providers.get(layer, {}) or {}).values():
                failovers[layer] += int(stats.get("failovers_in", 0) or 0)
        lines.append(
            "🔁 جایگزینی خودکار (از شروع اجرا): "
            f"STT {to_persian_digits(failovers['stt'])}"
            f" | LLM {to_persian_digits(failovers['llm'])}"
        )
        lines.append("")
        lines.extend(self._admin_series_lines(processing["series"]))
        buttons = [
            self._admin_period_row("adm:ovp", period),
            [Button.inline("↩️ نمای کلی", b"adm:home")],
        ]
        buttons.extend(self._admin_menu())
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _show_admin_payments_report(self, event, period: str = "7d") -> None:
        period = period if period in self.ADMIN_PERIOD_KEYS else "7d"
        report = await self.db.admin_report(
            period_start=self._admin_period_start(period), with_timings=False
        )
        payments = report["payments"]
        by_status = payments["by_status"]
        approved = by_status.get("approved", {"count": 0, "toman": 0})
        rejected = by_status.get("rejected", {"count": 0, "toman": 0})
        created = sum(int(entry["count"]) for entry in by_status.values())
        decided = int(approved["count"]) + int(rejected["count"])
        approval_rate = (int(approved["count"]) / decided) if decided else None
        lines = self._admin_screen_header("💳 پرداخت و اعتبار — جزئیات", period, report)
        lines.extend(
            [
                f"• در انتظار بررسی رسید (اکنون): {to_persian_digits(payments['pending_review'])}",
                f"• سفارش‌های ایجادشده در بازه: {to_persian_digits(created)}",
                f"• تأییدشده در بازه: {to_persian_digits(approved['count'])}"
                f" — {format_toman(int(approved['toman']))}",
                f"• ردشده در بازه: {to_persian_digits(rejected['count'])}",
                f"• نرخ تأیید در بازه: {self._rate_text(approval_rate)}",
            ]
        )
        expiring = payments["expiring"]
        lines.append(
            f"• اعتبار در حال انقضا (تا {to_persian_digits(expiring['within_days'])} روز آینده): "
            f"{to_persian_digits(expiring['count'])} بسته"
            f" — {format_duration(expiring['seconds'])}"
        )
        open_statuses = {
            status: entry
            for status, entry in by_status.items()
            if status not in ("approved", "rejected")
        }
        if open_statuses:
            lines.append(
                "• سفارش‌های باز در بازه: "
                + " | ".join(
                    f"{self._admin_status_label(status)}: {to_persian_digits(entry['count'])}"
                    for status, entry in sorted(open_statuses.items())
                )
            )
        lines.extend(
            [
                "",
                "ℹ️ مبلغ فقط از سفارش‌های تأییدشده محاسبه می‌شود؛ رسیدهای در انتظار",
                "   در هیچ عددی به‌عنوان درآمد شمرده نمی‌شوند.",
                "ℹ️ «اعتبار در حال انقضا» بسته‌های اعتباری دارای تاریخ انقضاست.",
                "   بستهٔ بدون انقضا هرگز در این شمارش نیست.",
            ]
        )
        buttons = [
            self._admin_period_row("adm:ovpay", period),
            [
                Button.inline("💳 صف بررسی", b"adm:pay"),
                Button.inline("📋 تاریخچه", b"adm:ph:0"),
            ],
            [Button.inline("↩️ نمای کلی", b"adm:home")],
        ]
        buttons.extend(self._admin_menu())
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _show_admin_system_report(self, event, period: str = "7d") -> None:
        period = period if period in self.ADMIN_PERIOD_KEYS else "7d"
        runtime = self.runtime_metrics()
        health = await self.db.admin_health()
        uptime = max(0.0, time.time() - self._started_at)
        lines = [
            "🩺 سلامت سیستم",
            "🗓 وضعیت اکنون (آمار پردازش در نمای کلی و بخش پردازش است)",
            "",
            f"• زمان اجرای این نسخه: {format_duration(int(uptime))}",
            f"• کارها: {to_persian_digits(self.queue.active_count)} فعال"
            f" (اوج {to_persian_digits(int(runtime.get('peak_active_jobs', 0) or 0))})"
            f" | {to_persian_digits(self.queue.pending)} در صف"
            f" (اوج {to_persian_digits(int(runtime.get('peak_queued_jobs', 0) or 0))})",
            f"• حافظهٔ اوج: {self._bytes_text(int(int(runtime.get('peak_rss_mb', 0) or 0) * 1024 * 1024))}",
            f"• پایگاه‌داده: {self._bytes_text(int(health['db_bytes']))}"
            f" | WAL: {self._bytes_text(int(health['wal_bytes']))}",
            f"• خطاهای پایگاه‌داده از شروع اجرا: "
            f"{to_persian_digits(int(runtime.get('db_errors', 0) or 0))}",
            f"• آخرین پردازش موفق: {self._timestamp_text(health['last_success'])}",
            f"• به‌روزرسانی‌های تکراری حذف‌شده: "
            f"{to_persian_digits(int(runtime.get('deduplicated_updates', 0) or 0))}",
        ]
        providers = runtime.get("providers", {}) or {}
        provider_lines: list[str] = []
        for layer, layer_label in (("stt", "STT"), ("llm", "LLM")):
            entries = providers.get(layer, {}) or {}
            for slug, stats in sorted(entries.items()):
                provider_lines.append(
                    f"• {layer_label} {slug}: در حال اجرا "
                    f"{to_persian_digits(int(stats.get('in_flight', 0) or 0))}/"
                    f"{to_persian_digits(int(stats.get('peak_in_flight', 0) or 0))} اوج"
                    f" | موفقیت {self._rate_text(stats.get('success_rate'))}"
                    f" | timeout {to_persian_digits(int(stats.get('timeouts', 0) or 0))}"
                    f" | rate-limit {to_persian_digits(int(stats.get('rate_limits', 0) or 0))}"
                    f" | خطای سرور {to_persian_digits(int(stats.get('server_errors', 0) or 0))}"
                )
        if provider_lines:
            lines.extend(["", "🌐 ارائه‌دهنده‌ها (از شروع اجرا):", *provider_lines])
        else:
            lines.extend(["", "🌐 از شروع اجرا هنوز درخواستی به ارائه‌دهنده‌ها نرفته است."])
        lines.extend(
            [
                "",
                "🧹 نگهداری",
                f"• رسید پرداخت: {to_persian_digits(self.settings.receipt_retention_days)} روز",
                "• رونوشت گفتار: "
                + (
                    "غیرفعال"
                    if self.settings.transcript_retention_days <= 0
                    else f"{to_persian_digits(self.settings.transcript_retention_days)} روز"
                ),
                "• فایل‌های موقت هر کار پس از پایان همان کار پاک می‌شوند.",
            ]
        )
        buttons = [
            self._admin_period_row("adm:ovs", period),
            [Button.inline("↩️ نمای کلی", b"adm:home")],
        ]
        buttons.extend(self._admin_menu())
        await self._edit_or_reply(event, "\n".join(lines), buttons)

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

    PAYMENT_ADMIN_PAGE_SIZE = 8

    async def _show_admin_payments(self, event, page: int = 0) -> None:
        """Show a bounded page of receipt-backed orders awaiting a decision."""
        page = max(0, int(page))
        page_size = self.PAYMENT_ADMIN_PAGE_SIZE
        payments = await self.db.pending_review_payments(
            limit=page_size + 1, offset=page * page_size
        )
        has_more = len(payments) > page_size
        payments = payments[:page_size]
        total = await self.db.count_pending_review_payments()
        buttons: list[list] = [
            [Button.inline("📋 تاریخچه پرداخت‌ها", b"adm:ph:0")]
        ]
        if not payments:
            if page > 0 and total:
                # A stale «next» button after another admin cleared the queue
                # must lead back to real orders, not a dead-end empty page.
                await self._show_admin_payments(event, 0)
                return
            buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
            await self._edit_or_reply(
                event,
                "💳 پرداخت‌های در انتظار\n\nدر حال حاضر پرداختی برای بررسی وجود ندارد.",
                buttons,
            )
            return
        lines = [
            f"💳 پرداخت‌های در انتظار — {to_persian_digits(total)} مورد",
            f"صفحهٔ {to_persian_digits(page + 1)}؛ برای بررسی، سفارش را باز کنید.",
        ]
        for payment in payments:
            number = to_persian_digits(payment["payment_id"])
            seconds = payment.get("purchased_seconds")
            price = payment.get("purchased_price")
            package = format_duration(int(seconds)) if seconds is not None else payment["plan_code"]
            amount = format_toman(int(price)) if price is not None else "نامشخص"
            lines.append(
                f"\n#{number} | کاربر: {payment['user_id']}\n{package} | {amount}"
            )
            buttons.append([Button.inline(
                f"🔎 جزئیات سفارش {number}", f"adm:pd:{payment['payment_id']}".encode()
            )])
        nav = []
        if page > 0:
            nav.append(Button.inline("◀️ قبلی", f"adm:pay:{page - 1}".encode()))
        if has_more:
            nav.append(Button.inline("بعدی ▶️", f"adm:pay:{page + 1}".encode()))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _show_admin_payment_history(self, event, page: int = 0) -> None:
        page_size = 8
        payments = await self.db.all_payments(
            limit=page_size + 1, offset=page * page_size
        )
        has_more = len(payments) > page_size
        payments = payments[:page_size]
        lines = [f"📋 تاریخچه پرداخت‌ها — صفحهٔ {to_persian_digits(page + 1)}"]
        buttons: list[list] = []
        if not payments:
            lines.append("\nسفارشی ثبت نشده است." if page == 0 else "\nمورد دیگری وجود ندارد.")
        for payment in payments:
            number = to_persian_digits(payment["payment_id"])
            lines.append(
                f"\n#{number} | کاربر: {payment['user_id']} | "
                f"{self._payment_status_label(payment['status'])}"
            )
            buttons.append([Button.inline(
                f"🔎 جزئیات سفارش {number}", f"adm:pd:{payment['payment_id']}".encode()
            )])
        nav = []
        if page > 0:
            nav.append(Button.inline("◀️ قبلی", f"adm:ph:{page - 1}".encode()))
        if has_more:
            nav.append(Button.inline("بعدی ▶️", f"adm:ph:{page + 1}".encode()))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("💳 پرداخت‌های در انتظار", b"adm:pay")])
        buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _show_admin_payment_detail(self, event, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if payment is None:
            await self._ack(event, "سفارش پیدا نشد.", alert=True)
            await self._show_admin_payments(event)
            return
        seconds = payment.get("purchased_seconds")
        price = payment.get("purchased_price")
        validity_days = payment.get("purchased_validity_days")
        if payment["receipt_path"]:
            receipt_state = "ثبت شده"
        elif payment["status"] in {"awaiting_payment", "pending", "cancelled"}:
            receipt_state = "ثبت نشده"
        elif payment["status"] in {"approved", "rejected", "pending_review"}:
            # A reviewed order whose receipt file was removed by retention is
            # not "never submitted"; the prior audit separated those for the
            # user copy and the admin detail must not conflate them either.
            receipt_state = "پاسخ‌شده؛ رسید موجود نیست"
        else:
            receipt_state = "در دسترس نیست"
        lines = [
            f"🔎 سفارش {to_persian_digits(payment_id)}",
            f"شناسه سفارش: #{to_persian_digits(payment_id)}",
            f"کاربر: {payment['user_id']}",
            f"طرح: {payment['plan_code']}",
            f"بسته: {format_duration(int(seconds)) if seconds is not None else payment['plan_code']}",
            f"مدت اعتبار: {format_validity(validity_days)}",
            f"مبلغ: {format_toman(int(price)) if price is not None else 'نامشخص'}",
            f"وضعیت: {self._payment_status_label(payment['status'])}",
            f"ثبت: {to_persian_digits(str(payment['created_at'])[:16].replace('T', ' '))}",
            f"رسید: {receipt_state}",
        ]
        if payment.get("reviewed_at"):
            lines.append(
                f"بررسی: {self._support_when(payment['reviewed_at'])} | مدیر: {payment['reviewed_by']}"
            )
        if payment.get("review_reason"):
            lines.append(f"دلیل: {payment['review_reason']}")
        if payment["status"] == "approved":
            credit_state = "ثبت شده" if payment.get("credit_ledger_id") else "سابقهٔ قدیمی"
            lines.append(f"اعتبار متصل به سفارش: {credit_state}")
        buttons: list[list] = []
        if payment["receipt_path"]:
            buttons.append([Button.inline(
                "📷 مشاهده رسید", f"adm:rcpt:{payment_id}".encode()
            )])
        if is_pending_payment_review(payment):
            buttons.append([
                Button.inline("✅ تأیید پرداخت", f"payv:{payment_id}:a".encode()),
                Button.inline("❌ رد پرداخت", f"payv:{payment_id}:r".encode()),
            ])
        buttons.append([Button.inline("💳 پرداخت‌های در انتظار", b"adm:pay")])
        buttons.append([Button.inline("📋 تاریخچه پرداخت‌ها", b"adm:ph:0")])
        buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
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
            or not await run_file_io(
                _is_safe_receipt_path, self.settings.receipt_dir, path
            )
            or not await run_file_io(
                _normalize_receipt_image,
                Path(str(path)),
                max_bytes=self.settings.max_payment_receipt_bytes,
            )
        ):
            await self._ack(event, "برای این پرداخت رسید معتبری ثبت نشده است.", alert=True)
            return
        await self._ack(event)
        number = to_persian_digits(payment_id)
        buttons: list[list] = []
        if is_pending_payment_review(payment):
            buttons.append([
                Button.inline("✅ تأیید پرداخت", f"payv:{payment_id}:a".encode()),
                Button.inline("❌ رد پرداخت", f"payv:{payment_id}:r".encode()),
            ])
        buttons.append([Button.inline("↩️ جزئیات سفارش", f"adm:pd:{payment_id}".encode())])
        # The same supported file API as every other delivery, as a photo
        # (no ``force_document``): receipts are viewed in the app, not filed.
        await self._send_telegram_file(
            await event.get_input_chat(),
            Path(str(path)),
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
            "ticket_closed": "بستن درخواست پشتیبانی",
            "ticket_reopened": "بازگشایی درخواست پشتیبانی",
            "receipt_expired": "حذف رسید منقضی‌شده",
            "transcript_expired": "حذف متن منقضی‌شده",
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
        logger.info("ban flag changed banned=%s", banning)
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
        logger.info("unlimited access updated enabled=%s", enable)
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
                    logger.debug("broadcast delivery failed")
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

    # ------------------------------------------------------------- support
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

    async def _show_ticket_screen(self, event, text: str, buttons) -> None:
        """Send every character of a history without exceeding Telegram limits."""
        pages = split_message(text)
        await self._edit_or_reply(
            event, pages[0], buttons if len(pages) == 1 else None
        )
        for index, page in enumerate(pages[1:], start=1):
            await event.respond(
                page, buttons=buttons if index == len(pages) - 1 else None
            )

    async def _notify_admins(self, text: str, buttons=None) -> None:
        """Tell every administrator something happened. Failures are logged, never raised."""
        for admin_id in sorted(self.settings.admin_ids):
            try:
                await self.client.send_message(admin_id, text, buttons=buttons)
            except Exception:
                logger.warning("support notification not delivered to an administrator")

    async def _notify_user(self, user_id: int, text: str, buttons=None) -> None:
        try:
            await self.client.send_message(user_id, text, buttons=buttons)
        except Exception:
            # The state change is already stored; a blocked bot must not undo it.
            logger.warning("support notification not delivered to the account")

    async def _show_support_home(self, event) -> None:
        text = (
            "🎧 پشتیبانی\n\n"
            "برای ثبت درخواست جدید یا پیگیری درخواست‌های قبلی، یکی از گزینه‌ها را انتخاب کنید."
        )
        buttons = [
            [Button.inline("➕ درخواست جدید", b"sup:new")],
            [Button.inline("📋 درخواست‌های من", b"sup:list")],
            [Button.inline("↩️ منوی اصلی", b"menu:main")],
        ]
        await self._edit_or_reply(event, text, buttons)

    async def _show_support_categories(self, event) -> None:
        buttons = [
            [Button.inline(label, f"sup:cat:{key}".encode())]
            for key, label in SUPPORT_CATEGORY_BUTTONS.items()
        ]
        buttons.append([Button.inline("↩️ بازگشت", b"support")])
        await self._edit_or_reply(event, "دسته‌بندی درخواست را انتخاب کنید:", buttons)

    async def _show_user_tickets(self, event, user_id: int, page: int = 0) -> None:
        page = max(0, page)
        rows = await self.db.list_user_tickets(
            user_id, limit=self.SUPPORT_PAGE_SIZE + 1, offset=page * self.SUPPORT_PAGE_SIZE
        )
        has_more = len(rows) > self.SUPPORT_PAGE_SIZE
        rows = rows[: self.SUPPORT_PAGE_SIZE]
        if not rows and page == 0:
            text = "شما هنوز درخواستی ثبت نکرده‌اید."
        else:
            text = f"📋 درخواست‌های شما — صفحهٔ {to_persian_digits(page + 1)}"
        buttons = [
            [Button.inline(
                f"{row['public_ticket_id']} · {self._support_status_label(row['status'])}",
                f"sup:v:{row['id']}".encode(),
            )]
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
        await self._edit_or_reply(event, text, buttons)

    async def _owned_ticket(self, user_id: int, ticket_id: int) -> dict | None:
        """The ticket only when the signed-in user owns it (never by id alone)."""
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None or int(ticket["user_id"]) != int(user_id):
            return None
        return ticket

    async def _show_user_ticket(self, event, user_id: int, ticket_id: int, page: int = 0) -> None:
        ticket = await self._owned_ticket(user_id, ticket_id)
        if ticket is None:
            await self._ack(event, "این درخواست پیدا نشد.", alert=True)
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
            buttons.append([Button.inline("✍️ پاسخ به پشتیبانی", f"sup:r:{ticket_id}".encode())])
            buttons.append([Button.inline("✅ بستن درخواست", f"sup:c:{ticket_id}".encode())])
        else:
            buttons.append([Button.inline("🔁 باز کردن دوباره", f"sup:o:{ticket_id}".encode())])
        if total > offset + self.SUPPORT_HISTORY_PAGE:
            buttons.append([Button.inline("پیام‌های قدیمی‌تر ▶️", f"sup:v:{ticket_id}:{page + 1}".encode())])
        if page > 0:
            buttons.append([Button.inline("◀️ پیام‌های جدیدتر", f"sup:v:{ticket_id}:{page - 1}".encode())])
        buttons.append([Button.inline("↩️ درخواست‌های من", b"sup:list")])
        await self._show_ticket_screen(event, text, buttons)

    async def _handle_support_callback(self, event, user_id: int, data: str) -> None:
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
                await self._ack(event, "این دسته معتبر نیست.", alert=True)
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
            await self._ack(event, "درخواست نامعتبر است.", alert=True)
            return
        if parts[1] == "v":
            page = (_to_int(parts[3]) or 0) if len(parts) > 3 else 0
            await self._show_user_ticket(event, user_id, ticket_id, page)
        elif parts[1] == "r":
            ticket = await self._owned_ticket(user_id, ticket_id)
            if ticket is None or ticket["status"] == "closed":
                await self._ack(event, "این درخواست پاسخ‌پذیر نیست؛ ابتدا آن را باز کنید.", alert=True)
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
                await self._ack(event, "این درخواست پیدا نشد.", alert=True)
                return
            try:
                closed = await self.db.close_ticket(ticket_id, user_id, admin=False)
            except (ValueError, PermissionError, TicketStateError):
                closed = False
            if closed:
                await self._notify_admins(
                    f"🔒 کاربر {user_id} درخواست {ticket['public_ticket_id']} را بست."
                )
                logger.info("support ticket closed by user ticket=%s", ticket_id)
            await self._show_user_ticket(event, user_id, ticket_id)
        elif parts[1] == "o":
            ticket = await self._owned_ticket(user_id, ticket_id)
            if ticket is None:
                await self._ack(event, "این درخواست پیدا نشد.", alert=True)
                return
            try:
                reopened = await self.db.reopen_ticket(ticket_id, user_id, admin=False)
            except (ValueError, PermissionError, TicketStateError):
                reopened = False
            if reopened:
                await self._notify_admins(
                    f"🔁 کاربر {user_id} درخواست {ticket['public_ticket_id']} را دوباره باز کرد."
                )
                logger.info("support ticket reopened by user ticket=%s", ticket_id)
            await self._show_user_ticket(event, user_id, ticket_id)
        else:
            await self._ack(event, "درخواست نامعتبر است.", alert=True)

    async def _handle_support_text(self, event, user_id: int, state: tuple, text: str) -> None:
        """The next plain message of a user who opened a support prompt."""
        kind, value = state
        if not text:
            await event.reply("لطفاً پیام خود را به‌صورت متن بفرستید؛ برای لغو /cancel بفرستید.")
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
            logger.info("support ticket created ticket=%s category=%s", ticket["id"], value)
            await event.reply(
                "✅ درخواست شما ثبت شد.\n"
                f"شماره پیگیری: {ticket['public_ticket_id']}\n"
                "از همین بخش می‌توانید پاسخ‌ها را پیگیری کنید.",
                buttons=[[Button.inline("📋 مشاهدهٔ درخواست", f"sup:v:{ticket['id']}".encode())]],
            )
            await self._notify_admins(
                "🎧 درخواست پشتیبانی جدید\n"
                f"شماره: {ticket['public_ticket_id']}\n"
                f"کاربر: {user_id}\n"
                f"دسته: {self._support_category_label(value)}\n"
                f"موضوع: {ticket['subject']}",
                buttons=[[Button.inline("🔎 بررسی درخواست", f"adm:sv:{ticket['id']}".encode())]],
            )
            return
        # kind == "reply": the ticket id came from a verified ownership check.
        if len(text) > self.db.SUPPORT_MESSAGE_MAX:
            await event.reply("پیام باید حداکثر ۴۰۰۰ نویسه باشد؛ کوتاه‌تر بنویسید.")
            return
        try:
            await self.db.add_ticket_message(
                value, user_id, "user", text,
                telegram_message_id=getattr(event.message, "id", None),
            )
        except (ValueError, PermissionError, TicketStateError):
            self._support_states.pop(user_id, None)
            await event.reply("این درخواست دیگر پاسخ‌پذیر نیست؛ ابتدا آن را باز کنید.")
            return
        self._support_states.pop(user_id, None)
        refreshed_ticket = await self.db.get_ticket(value)
        public_id = refreshed_ticket["public_ticket_id"] if refreshed_ticket else str(value)
        await event.reply(
            "✅ پیام شما ثبت شد.",
            buttons=[[Button.inline("📋 مشاهدهٔ درخواست", f"sup:v:{value}".encode())]],
        )
        await self._notify_admins(
            f"💬 پیام جدیدی در درخواست {public_id} ثبت شد.\nکاربر: {user_id}",
            buttons=[[Button.inline("🔎 بررسی درخواست", f"adm:sv:{value}".encode())]],
        )

    async def _handle_admin_support_text(self, event, admin_id: int, ticket_id: int, text: str) -> None:
        if not text:
            await event.reply("پاسخ باید متن داشته باشد؛ دوباره متن را بفرستید یا /cancel بزنید.")
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
            await event.reply("این درخواست دیگر پاسخ‌پذیر نیست؛ ابتدا آن را باز کنید.", buttons=self._admin_menu())
            return
        self._support_admin_states.pop(admin_id, None)
        ticket = await self.db.get_ticket(ticket_id)
        public_id = ticket["public_ticket_id"] if ticket else str(ticket_id)
        logger.info("support admin reply ticket=%s", ticket_id)
        await event.reply(
            f"✅ پاسخ به درخواست {public_id} ثبت شد.",
            buttons=[[Button.inline("🔎 مشاهدهٔ درخواست", f"adm:sv:{ticket_id}".encode())]],
        )
        if ticket is not None:
            await self._notify_user(
                int(ticket["user_id"]),
                f"📩 پاسخ جدیدی برای درخواست {public_id} ثبت شده است.",
                buttons=[[Button.inline("📋 مشاهدهٔ پاسخ", f"sup:v:{ticket_id}".encode())]],
            )

    async def _show_admin_support(self, event, view: str = "active", category: str = "all", page: int = 0) -> None:
        counts = await self.db.count_tickets_by_status()
        page = max(0, page)
        cat = None if category == "all" else category
        rows = await self.db.list_tickets(
            view=view, category=cat,
            limit=self.SUPPORT_PAGE_SIZE + 1, offset=page * self.SUPPORT_PAGE_SIZE,
        )
        has_more = len(rows) > self.SUPPORT_PAGE_SIZE
        rows = rows[: self.SUPPORT_PAGE_SIZE]
        open_count = counts.get("open", 0) + counts.get("awaiting_admin", 0) + counts.get("awaiting_user", 0)
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
                Button.inline(VIEW_LABELS["active"], f"adm:sl:active:{category}:0".encode()),
                Button.inline(VIEW_LABELS["open"], f"adm:sl:open:{category}:0".encode()),
                Button.inline(VIEW_LABELS["wait"], f"adm:sl:wait:{category}:0".encode()),
            ],
            [
                Button.inline(VIEW_LABELS["user"], f"adm:sl:user:{category}:0".encode()),
                Button.inline(VIEW_LABELS["closed"], f"adm:sl:closed:{category}:0".encode()),
            ],
            [Button.inline("📂 همه دسته‌ها", f"adm:sl:{view}:all:0".encode())],
            [Button.inline(label, f"adm:sl:{view}:{key}:0".encode())
             for key, label in SUPPORT_CATEGORY_BUTTONS.items()][:2],
            [Button.inline(label, f"adm:sl:{view}:{key}:0".encode())
             for key, label in SUPPORT_CATEGORY_BUTTONS.items()][2:],
        ]
        for row in rows:
            buttons.append([Button.inline(
                f"{row['public_ticket_id']} · {self._support_category_label(row['category'])} · "
                f"{self._support_status_label(row['status'])}",
                f"adm:sv:{row['id']}".encode(),
            )])
        nav = []
        if page > 0:
            nav.append(Button.inline("◀️ قبلی", f"adm:sl:{view}:{category}:{page - 1}".encode()))
        if has_more:
            nav.append(Button.inline("بعدی ▶️", f"adm:sl:{view}:{category}:{page + 1}".encode()))
        if nav:
            buttons.append(nav)
        if not rows:
            text += "\n\nدرخواستی در این فهرست نیست."
        buttons.append([Button.inline("↩️ پنل مدیریت", b"adm:home")])
        await self._edit_or_reply(event, text, buttons)

    async def _show_admin_ticket(self, event, ticket_id: int, page: int = 0) -> None:
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            await self._ack(event, "این درخواست پیدا نشد.", alert=True)
            await self._show_admin_support(event)
            return
        page = max(0, page)
        total = await self.db.count_ticket_messages(ticket_id)
        offset = page * self.SUPPORT_HISTORY_PAGE
        messages = await self.db.ticket_messages(
            ticket_id, limit=self.SUPPORT_HISTORY_PAGE, offset=offset,
        )
        head = (
            f"🎫 درخواست {ticket['public_ticket_id']}\n"
            f"کاربر: {ticket['user_id']}\n"
            f"دسته: {self._support_category_label(ticket['category'])}\n"
            f"وضعیت: {self._support_status_label(ticket['status'])}\n"
            f"موضوع: {ticket['subject']}\n"
            f"ثبت: {self._support_when(ticket['created_at'])} | "
            f"آخرین تغییر: {self._support_when(ticket['updated_at'])}\n"
        )
        text = f"{head}\n{self._support_history_text(messages, total, offset, viewer='admin')}"
        buttons = []
        if ticket["status"] != "closed":
            buttons.append([Button.inline("✍️ پاسخ به کاربر", f"adm:sr:{ticket_id}".encode())])
            buttons.append([Button.inline("✅ بستن درخواست", f"adm:sc:{ticket_id}".encode())])
        else:
            buttons.append([Button.inline("🔁 باز کردن دوباره", f"adm:so:{ticket_id}".encode())])
        if total > offset + self.SUPPORT_HISTORY_PAGE:
            buttons.append([Button.inline("پیام‌های قدیمی‌تر ▶️", f"adm:sv:{ticket_id}:{page + 1}".encode())])
        if page > 0:
            buttons.append([Button.inline("◀️ پیام‌های جدیدتر", f"adm:sv:{ticket_id}:{page - 1}".encode())])
        buttons.append([Button.inline("↩️ فهرست پشتیبانی", b"adm:sup")])
        await self._show_ticket_screen(event, text, buttons)

    async def _handle_admin_support_callback(self, event, admin_id: int, data: str) -> None:
        parts = data.split(":")
        if data == "adm:sup":
            await self._show_admin_support(event)
            return
        if data.startswith("adm:sl:") and len(parts) == 5:
            view, category = parts[2], parts[3]
            if view not in VIEW_LABELS or (category != "all" and category not in SUPPORT_CATEGORY_BUTTONS):
                await self._ack(event, "فهرست نامعتبر است.", alert=True)
                return
            await self._show_admin_support(event, view, category, _to_int(parts[4]) or 0)
            return
        ticket_id = _to_int(parts[2]) if len(parts) >= 3 else None
        if ticket_id is None:
            await self._ack(event, "درخواست نامعتبر است.", alert=True)
            return
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            await self._ack(event, "این درخواست پیدا نشد.", alert=True)
            return
        if parts[1] == "sv":
            await self._show_admin_ticket(event, ticket_id, (_to_int(parts[3]) or 0) if len(parts) > 3 else 0)
        elif parts[1] == "sr":
            if ticket["status"] == "closed":
                await self._ack(event, "این درخواست بسته است؛ ابتدا آن را باز کنید.", alert=True)
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
                    buttons=[[Button.inline("📋 مشاهدهٔ درخواست", f"sup:v:{ticket_id}".encode())]],
                )
            await self._show_admin_ticket(event, ticket_id)
        elif parts[1] == "so":
            try:
                reopened = await self.db.reopen_ticket(ticket_id, admin_id, admin=True)
            except (ValueError, PermissionError):
                reopened = False
            if reopened:
                await self._notify_user(
                    int(ticket["user_id"]),
                    f"🔁 درخواست {ticket['public_ticket_id']} دوباره باز شد.",
                    buttons=[[Button.inline("📋 مشاهدهٔ درخواست", f"sup:v:{ticket_id}".encode())]],
                )
            await self._show_admin_ticket(event, ticket_id)
        else:
            await self._ack(event, "درخواست نامعتبر است.", alert=True)

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

    #: Batches one transcript-retention sweep may delete (bounded work per run).
    TRANSCRIPT_CLEANUP_BATCHES = 25

    async def _transcript_cleanup_loop(self) -> None:
        """Delete finished transcripts after the retention window (audit F-10)."""
        if self.settings.transcript_retention_days <= 0:
            return
        while True:
            try:
                # Sweep first: a restart must not reset the timer forever.
                await self._cleanup_expired_transcripts()
                await asyncio.sleep(24 * 3600)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("transcript cleanup failed", exc_info=True)

    async def _cleanup_expired_transcripts(self) -> int:
        """Delete expired transcript rows in bounded batches; returns the count."""
        days = self.settings.transcript_retention_days
        if days <= 0:
            return 0
        removed = 0
        for _ in range(self.TRANSCRIPT_CLEANUP_BATCHES):
            batch = await self.db.expire_transcriptions(older_than_days=days)
            removed += batch
            if batch == 0:
                break
        if removed:
            # The deletion is recorded: an operator reading the audit trail must
            # be able to tell why a transcript is gone.
            await self.db.audit(
                0, "transcript_expired", f"deleted:{removed} days:{days}"
            )
            logger.info(
                "removed %d expired transcriptions (retention %d days)", removed, days
            )
        return removed

    async def _cleanup_expired_receipts(self) -> None:
        if not await run_file_io(self.settings.receipt_dir.is_dir):
            return
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=self.settings.receipt_retention_days)
        # Retention applies to reviewed receipts. A slow admin review must never
        # delete the only evidence and leave an unverifiable pending order.
        protected = {
            payment["receipt_path"]
            for payment in await self.db.pending_payments()
            if payment.get("receipt_path")
        }
        removed: list[str] = []
        paths = await run_file_io(
            lambda: list(self.settings.receipt_dir.glob("receipt-*.jpg"))
        )
        for path in paths:
            # Safety: only touch files strictly inside receipt_dir (audit F-H01)
            try:
                if str(path) in protected:
                    continue
                if not await run_file_io(
                    _is_safe_receipt_path, self.settings.receipt_dir, path
                ):
                    logger.warning("skipping unsafe receipt path=%s", _sanitize_log_value(path))
                    continue
                if datetime.fromtimestamp((await run_file_io(path.stat)).st_mtime) < cutoff:
                    await run_file_io(path.unlink, missing_ok=True)
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


