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
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
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
    complete once every task is done. The callers' guarantees are unchanged:
    the byte ceiling is enforced on the declared size and on the running total,
    a stalled transfer is aborted, and any failure leaves no partial file (the
    caller deletes it).
    """
    size = _message_file_size(message)
    if size is None:
        raise RuntimeError("telegram did not report a file size")
    if size > limit_bytes:
        raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
    ranges = plan_download_ranges(size, connections)
    last_progress = [time.monotonic()]
    written = [0]
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

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
            await asyncio.to_thread(os.pwrite, descriptor, payload, position)
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
        os.close(descriptor)
    if target.stat().st_size != size:
        # A short write would otherwise be "some file that plays".
        raise RuntimeError(
            f"ranged download produced {target.stat().st_size} of {size} bytes"
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
    # ``os.pwrite`` is POSIX-only; on Windows the proven single-stream path runs
    # (the deployment target is cPanel/Linux, this keeps local runs intact).
    ranged = (
        client is not None
        and connections > 1
        and hasattr(os, "pwrite")
        and _message_file_size(message)
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
            target.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 - any surprise means one stream
            # A ranged transfer is an optimisation, never a prerequisite: prove
            # it works on this host, otherwise the proven path runs.
            logger.warning(
                "ranged download failed (%s: %s); retrying single-stream",
                type(exc).__name__, exc,
            )
            target.unlink(missing_ok=True)
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
            tick = max(0.05, min(stall_seconds / 4, 15.0))
            wait_set = (task,)
            while True:
                done, _pending = await asyncio.wait(wait_set, timeout=tick)
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
        self._media_semaphore = asyncio.Semaphore(settings.media_worker_concurrency)
        self._receipt_cleanup_task: asyncio.Task | None = None
        self._transcript_cleanup_task: asyncio.Task | None = None
        self._prewarm_task: asyncio.Task | None = None
        self._pending_payments: dict[int, int] = {}  # user_id -> payment_id awaiting receipt
        self._stt_only_users: set[int] = set()
        self._pending_broadcast: set[int] = set()
        # user_id -> ("new", category) or ("reply", ticket_id) awaiting the next text.
        self._support_states: dict[int, tuple[str, object]] = {}
        # admin_id -> ticket_id awaiting a reply text (set only by a verified admin callback).
        self._support_admin_states: dict[int, int] = {}
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
        rows: list[list] = []
        if self.settings.note_configured:
            rows.append(
                [
                    Button.inline("🎙️ تبدیل صدا به متن", b"stt"),
                    Button.inline("📝 ساخت جزوه", b"make"),
                ]
            )
        else:
            rows.append([Button.inline("🎙️ تبدیل صدا به متن", b"make")])
        if self.settings.office_enabled:
            rows.append([Button.inline("📄 ساخت سند رایگان", b"office")])
        rows.append([Button.inline("💳 خرید اشتراک", b"buy")])
        rows.append(
            [
                Button.inline("📊 اعتبار من", b"account"),
                Button.inline("🧾 پرداخت‌های من", b"payments"),
            ]
        )
        rows.append(
            [
                Button.inline("🎧 پشتیبانی", b"support"),
                Button.inline("ℹ️ راهنما", b"help"),
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
            [Button.inline("↩️ پنل کاربری", b"menu:main")],
        ]

    async def _show_main(self, event, *, first_start: bool = False) -> None:
        user_id = event.sender_id
        await self.db.ensure_user(user_id)
        if await self.db.grant_free_plan_once(
            user_id, self._free_plan_seconds()
        ):
            logger.info("free plan granted user=%s", user_id)
        greeting = "به گاماس خوش آمدید." if first_start else "منوی اصلی"
        if self.settings.note_configured:
            introduction = (
                "برای ساخت جزوه، «📝 ساخت جزوه» را انتخاب کنید تا ابتدا متن گفتار و سپس جزوهٔ ورد "
                "ارسال شود؛ یا برای دریافت متن تنها، «🎙️ تبدیل صدا به متن» را بزنید.\n"
            )
        else:
            introduction = (
                "برای تبدیل صدا به متن، «🎙️ تبدیل صدا به متن» را انتخاب کنید و فایل صوتی، "
                "ویدیویی یا پاورپوینت را بفرستید.\n"
                "ساخت جزوهٔ ورد در حال حاضر فعال نیست.\n"
            )
        text = f"{greeting}\n\n{introduction}"
        if self.settings.office_enabled:
            text += "برای ساخت سند از متن، از «📄 ساخت سند رایگان» استفاده کنید؛ اعتبار مصرف نمی‌شود.\n"
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
            "📄 ساخت سند رایگان\n\n"
            "متن خود را بفرستید تا به فایل Word، Excel، PowerPoint یا PDF تبدیل شود. "
            "متن شما عیناً در فایل قرار می‌گیرد.\n"
            "این بخش رایگان است و از اعتبار شما کم نمی‌کند.\n\n"
            "قالب را انتخاب کنید:"
        )
        if notice:
            text = f"{notice}\n\n{text}"
        buttons = [
            [Button.inline(f"{FORMAT_EMOJI['word']} {FORMAT_LABELS['word']}", b"office:word")],
            [Button.inline(f"{FORMAT_EMOJI['excel']} {FORMAT_LABELS['excel']}", b"office:excel")],
            [Button.inline(f"{FORMAT_EMOJI['ppt']} {FORMAT_LABELS['ppt']}", b"office:ppt")],
            [Button.inline(f"{FORMAT_EMOJI['pdf']} {FORMAT_LABELS['pdf']}", b"office:pdf")],
            [Button.inline("↩️ بازگشت", b"menu:main")],
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
                "لغو شد. برای ساخت سند، از «📄 ساخت سند رایگان» شروع کنید."
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
                    f"(حداکثر {to_persian_digits(self.settings.office_max_input_bytes // 1024)} کیلوبایت). "
                    "فایل کوچک‌تری ارسال کنید."
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
            assert content is not None
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
                    f"حداکثر {to_persian_digits(self.settings.office_max_chars)} نویسه در هر سند؛ "
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
                "حجم فایل بیش از حد مجاز است (حداکثر "
                f"{to_persian_digits(self.settings.office_max_input_bytes // 1024)} کیلوبایت)."
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
                f"({to_persian_digits(self.settings.office_max_chars)} نویسه). "
                "متن کوتاه‌تری ارسال کنید."
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
        # A review action supersedes any older rejection-reason prompt. Only
        # the new rejection action below is allowed to arm that prompt again.
        if data.startswith("payv:"):
            self._pending_reject_reason.pop(user_id, None)
        # Likewise, navigating away from the payment flow drops a waiting
        # receipt upload — a photo sent later belongs to the new screen.
        if not data.startswith(("payr:", "plan:", "payopen:")):
            self._pending_payments.pop(user_id, None)
        if not data.startswith("stt"):
            self._stt_only_users.discard(user_id)
        # Support prompts are disarmed by any navigation except the prompt itself.
        if not data.startswith(("sup:cat:", "sup:r:")):
            self._support_states.pop(user_id, None)
        if not data.startswith("adm:sr:"):
            self._support_admin_states.pop(user_id, None)

        if data == "stt":
            await self._ack(event)
            self._stt_only_users.add(user_id)
            await event.respond(
                "🎙️ ارسال فایل برای تبدیل صدا به متن\n\n"
                "فایل صوتی، ویدیویی یا پاورپوینت خود را همین‌جا ارسال کنید.\n"
                "متن کامل گفتار به‌صورت فایل متنی و پیش‌نمایش ارسال می‌شود.",
                buttons=[[Button.inline("↩️ منوی اصلی", b"menu:main")]],
            )
        elif data == "make":
            await self._ack(event)
            if self.settings.note_configured:
                result_guide = "ابتدا متن گفتار و سپس جزوهٔ ورد برایتان ارسال می‌شود."
            else:
                result_guide = (
                    "متن گفتار برایتان ارسال می‌شود؛ ساخت جزوهٔ ورد در حال حاضر فعال نیست."
                )
            prompt_title = (
                "📝 ارسال فایل برای ساخت جزوه"
                if self.settings.note_configured
                else "🎙️ ارسال فایل برای تبدیل صدا به متن"
            )
            await event.respond(
                f"{prompt_title}\n\n"
                "فایل صوتی یا ویدیویی کلاس، پیام صوتی یا ویدیویی تلگرام، یا فایل پاورپوینت را "
                "همین‌جا بفرستید.\n\n"
                f"{result_guide}",
                buttons=[[Button.inline("↩️ منوی اصلی", b"menu:main")]],
            )
        elif data == "help":
            await self._ack(event)
            make_label = (
                "📝 ساخت جزوه" if self.settings.note_configured else "🎙️ تبدیل صدا به متن"
            )
            task_label = "ساخت جزوه:" if self.settings.note_configured else "تبدیل صدا به متن:"
            guide = [
                "ℹ️ راهنمای استفاده",
                "",
                task_label,
                f"۱. «{make_label}» را انتخاب کنید.",
                "۲. فایل صوتی، ویدیویی یا پاورپوینت درس را بفرستید.",
                "۳. ابتدا متن گفتار برایتان ارسال می‌شود.",
            ]
            if self.settings.note_configured:
                guide.append("۴. سپس جزوهٔ ورد آماده و ارسال می‌شود.")
            else:
                guide.append("ساخت جزوهٔ ورد در حال حاضر فعال نیست.")
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
                        "ساخت سند رایگان:",
                        "«📄 ساخت سند رایگان» را انتخاب کنید، قالب را بزنید و متن یا فایل متنی "
                        "خود را بفرستید. این بخش رایگان است و از اعتبار شما کم نمی‌کند.",
                    ]
                )
            await event.respond(
                "\n".join(guide),
                buttons=[[Button.inline("↩️ منوی اصلی", b"menu:main")]],
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
        elif data == "buy":
            await self._ack(event)
            await self._show_plans(event)
        elif data == "payments" or data.startswith("payments:"):
            await self._ack(event)
            page = _to_int(data.split(":", 1)[1]) if ":" in data else 0
            await self._show_payment_history(event, user_id, max(0, page or 0))
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
        elif data.startswith("adm:rcpt:"):
            await self._ack(event)
            await self._show_receipt(event, data)
        elif data == "adm:sup" or data.startswith(("adm:sl:", "adm:sv:", "adm:sr:", "adm:sc:", "adm:so:")):
            await self._ack(event)
            await self._handle_admin_support_callback(event, user_id, data)
        elif data == "support" or data.startswith("sup:"):
            await self._ack(event)
            await self._handle_support_callback(event, user_id, data)
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
        if command == "/start":
            # A fresh menu is a hard boundary for every conversational flow.
            # In particular, a later photo must not unexpectedly complete an
            # old receipt or ticket prompt after the user pressed /start.
            self._pending_reject_reason.pop(user_id, None)
            self._pending_broadcast.discard(user_id)
            self._pending_payments.pop(user_id, None)
            self._stt_only_users.discard(user_id)
            self._office_sessions.pop(user_id, None)
            self._support_states.pop(user_id, None)
            self._support_admin_states.pop(user_id, None)
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
        # to Office or the paid lecture pipeline. Invalid input keeps the
        # receipt prompt armed so the user can immediately retry.
        if user_id in self._pending_payments and not command:
            await self._handle_receipt_upload(event, user_id)
            return

        if command == "/cancel":
            self._pending_payments.pop(user_id, None)
            self._stt_only_users.discard(user_id)
            self._office_sessions.pop(user_id, None)
            self._support_states.pop(user_id, None)
            self._support_admin_states.pop(user_id, None)
            await event.reply("ارسال لغو شد. برای لغو سفارش، از «🧾 پرداخت‌های من» اقدام کنید.")
            return

        # Presentations.
        pkind, pfilename = _presentation_metadata(event.message)
        if pkind is not None:
            await self._accept_presentation(event, user_id, pkind, pfilename)
            return

        # Media.
        kind, filename, duration = _media_metadata(event.message)
        if kind is not None:
            await self._accept_media(event, user_id, kind, filename, duration)
            return

        # Free Office: the user's own text becomes a document.
        if await self._handle_office_message(event, user_id, text):
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
            if self.settings.office_enabled:
                await event.reply(
                    "⚠️ این نوع فایل پشتیبانی نمی‌شود. فایل صوتی، ویدیویی یا پاورپوینت "
                    "بفرستید، یا در بخش ساخت سند فایل متنی ارسال کنید."
                )
            else:
                await event.reply(
                    "⚠️ این نوع فایل پشتیبانی نمی‌شود. فایل صوتی، ویدیویی یا پاورپوینت بفرستید."
                )
            return
        if text:
            # The Free Office section already had its chance above: it either
            # took the text (and asked for a format) or is switched off.
            await event.reply(
                "برای پردازش فایل درس، از دکمه‌های منوی اصلی استفاده کنید و فایل صوتی، "
                "ویدیویی یا پاورپوینت را بفرستید."
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
                "از «💳 خرید اشتراک» می‌توانید اعتبار بگیرید."
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
        stt_only = user_id in self._stt_only_users
        self._stt_only_users.discard(user_id)
        progress = await JobProgress.create(event, submission_id=submission_id)
        job = Job(
            submission_id=submission_id,
            user_id=user_id,
            kind=kind,
            filename=filename,
            message=event.message,
            is_admin=self._is_admin(user_id),
            duration_hint=duration,
            meta={"progress": progress, "stt_only": stt_only},
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
                    f"⏳ اعتبار شما برای این ارائه کافی نیست.\n"
                    f"لازم (حداقل برآورد): {format_duration(needed)} — "
                    f"اعتبار فعلی: {format_duration(available)}\n"
                    "از «💳 خرید اشتراک» می‌توانید اعتبار بگیرید."
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
        stt_only = user_id in self._stt_only_users
        self._stt_only_users.discard(user_id)
        progress = await JobProgress.create(event, submission_id=submission_id)
        job = Job(
            submission_id=submission_id,
            user_id=user_id,
            kind="presentation",
            filename=filename,
            message=event.message,
            is_admin=self._is_admin(user_id),
            meta={"presentation_kind": kind, "progress": progress, "stt_only": stt_only},
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
                "از «💳 خرید اشتراک» اعتبار بگیرید."
            ))
        except USER_VISIBLE_ERRORS as exc:
            await self._release(job, "user-visible failure")
            await self.db.set_submission_status(
                job.submission_id, "failed", error=str(exc)[:500]
            )
            if progress:
                await progress.fail(str(job.submission_id))
            safe_msg = _safe_user_error_text(
                exc, "درخواست قابل پردازش نبود؛ لطفاً فایل را بررسی و دوباره ارسال کنید."
            )
            await self._reply(job, f"❌ {safe_msg}")
        except STTError as exc:
            await self._release(job, "stt failure")
            await self.db.set_submission_status(
                job.submission_id, "failed", error=str(exc)[:500]
            )
            if progress:
                await progress.fail(str(job.submission_id))
            safe_msg = _safe_user_error_text(
                exc, "خطای موقت در سرویس تبدیل گفتار رخ داد؛ لطفاً دوباره تلاش کنید."
            )
            await self._reply(job, f"❌ تبدیل گفتار به متن انجام نشد: {safe_msg}")
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
                job, "❌ خطای داخلی رخ داد و پردازش تکمیل نشد. لطفاً دوباره تلاش کنید."
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
        await progress.update(10, "آماده‌سازی پردازش")
        suffix = Path(job.filename).suffix.lower() if job.filename else (
            ".mp4" if job.kind == "video" else ".ogg"
        )
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ".media"
        source_path = workdir / f"source{suffix}"
        upload_bytes = _reported_upload_bytes(job.message, self.settings.max_file_size)
        # The download and the prepared audio can both exist at once.
        _ensure_disk_headroom(
            self.settings.temp_dir,
            upload_bytes * 2,
            reserve_bytes=self.settings.temp_min_free_bytes,
        )
        await progress.update(20, "دریافت فایل از تلگرام")
        download_started = time.monotonic()
        try:
            await _download_media_limited(
                job.message,
                source_path,
                self.settings.max_file_size,
                stall_seconds=self.settings.download_stall_seconds,
                client=self.client,
                connections=self.settings.telegram_download_connections,
            )
        except UploadTooLargeError as exc:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.") from exc
        if source_path.stat().st_size > self.settings.max_file_size:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")
        # The download is the only transfer rate this job measures for free, so
        # it seeds the format decision below (``AUDIO_FORMAT_POLICY=auto``).
        download_seconds = max(0.001, time.monotonic() - download_started)
        egress_bytes_per_second = source_path.stat().st_size / download_seconds

        await progress.update(35, "بررسی و آماده‌سازی صدا")
        source_info = await self._run_media_operation(
            probe_media, source_path, self.settings
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
        if audio_path.stat().st_size > self.settings.max_file_size:
            raise ValueError("صدای آماده‌شده از سقف حجم ربات بزرگ‌تر است.")

        if source_info.duration is not None or audio_path == source_path:
            info = source_info
        else:
            info = await self._run_media_operation(
                probe_media, audio_path, self.settings
            )
        duration = info.duration or job.duration_hint
        if not duration or duration <= 0:
            # Billing.md promises an unmeasurable duration is refused, not
            # reserved at a guess and settled at zero (audit F-21).
            raise ValueError(
                "مدت فایل مشخص نشد؛ امکان پردازش آن وجود ندارد."
            )
        needed = self._reserve_estimate(duration)
        await self.db.reserve(job.user_id, job.submission_id, needed)

        await progress.update(50, "تبدیل گفتار به متن")
        try:
            raw = await transcribe(audio_path, self.settings, session=self._http_session())
        except BaseException:
            await self._release(job, "transcription failed")
            raise
        document = build_document(raw)

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
        # Persist before delivering: a Telegram outage must not lose a finished
        # transcription (the work is done, the row is what an operator disputes).
        raw_path = workdir / raw_text_filename(reference)
        await asyncio.gather(
            self.db.save_transcription(
                job.submission_id, engine=raw.engine, text=document.plain(), notes_json=""
            ),
            asyncio.to_thread(
                raw_path.write_text, raw_text, encoding="utf-8", newline="\n"
            ),
        )
        await self._deliver_raw(job, raw_text, raw_path, reference)
        await asyncio.gather(
            self._send_preview(job, raw_text),
            self._settle(job, duration),
        )
        if job.meta.get("stt_only"):
            await self.db.set_submission_status(job.submission_id, "done")
            await progress.complete("متن کامل گفتار ارسال شد.")
            return

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
                egress_bytes_per_second=egress_bytes_per_second,
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
        await progress.update(10, "آماده‌سازی پردازش")
        suffix = Path(job.filename).suffix.lower() if job.filename else ".pptx"
        source_path = workdir / f"source{suffix}"
        upload_bytes = _reported_upload_bytes(job.message, self.settings.max_file_size)
        # The deck and the clips unpacked from it coexist in the workdir.
        _ensure_disk_headroom(
            self.settings.temp_dir,
            upload_bytes * 2,
            reserve_bytes=self.settings.temp_min_free_bytes,
        )
        await progress.update(20, "دریافت فایل از تلگرام")
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
        if source_path.stat().st_size > self.settings.presentation_max_temp_bytes:
            raise PresentationError("حجم فایل موقت ارائه از سقف تعیین‌شده بیشتر است.")

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
            if source_path.stat().st_size > self.settings.max_file_size:
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
            if prepared.path.stat().st_size > self.settings.max_file_size:
                raise ValueError("صدای آماده‌شده از سقف حجم ربات بزرگ‌تر است.")
            duration = prepared.source_duration  # synthetic silence is not billed
            needed = self._reserve_estimate(duration)
            await self.db.reserve(job.user_id, job.submission_id, needed)
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
            asyncio.to_thread(
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
        if job.meta.get("stt_only"):
            await self.db.set_submission_status(job.submission_id, "done")
            await progress.complete("متن کامل ارائه ارسال شد.")
            return

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
        await progress.update(15, "آماده‌سازی سند")
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

        await progress.update(80, "ارسال فایل خروجی")
        path = workdir / office_filename(document.title, kind, reference)
        await asyncio.to_thread(path.write_bytes, payload)
        caption = (
            f"{FORMAT_EMOJI.get(kind, '📄')} سند {FORMAT_LABELS.get(kind, '')} — {reference}"
        )
        await self._send_file(job, path, caption=caption)
        await self.db.set_submission_status(job.submission_id, "done")
        # Remember the document so «➕ افزودن متن» can extend it.
        self._office_last[job.user_id] = (kind, content, document.title, time.monotonic())
        self._trim_office_history()
        await progress.complete("فایل سند ارسال شد.", "✅ سند آماده شد")
        await self._offer_office_more(job)

    async def _offer_office_more(self, job: Job) -> None:
        """Offer to extend the document that was just delivered."""
        if job.message is None:
            return
        try:
            await job.message.reply(
                "در صورت نیاز می‌توانید متن دیگری به همین سند اضافه کنید.",
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
            # Evict the least recently built document, not the first key ever
            # inserted: a dict keeps its insertion position when a value is
            # updated, so "first key" can be a user who just built a document.
            oldest = min(
                self._office_last, key=lambda user_id: self._office_last[user_id][3]
            )
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
                await asyncio.to_thread(path.write_bytes, docx_bytes)
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
                await asyncio.to_thread(docx_path.write_bytes, docx_bytes)
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
                await asyncio.to_thread(docx_path.write_bytes, docx_bytes)
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
                await asyncio.to_thread(
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

    async def _show_account(self, event) -> None:
        """Show balance separately from plans and payment history."""
        user_id = event.sender_id
        balance = await self._balance_text(user_id)
        latest = await self.db.payment_history_paginated(user_id, limit=1)
        buttons = [
            [Button.inline("💳 خرید اشتراک", b"buy")],
            [Button.inline("🧾 پرداخت‌های من", b"payments")],
        ]
        if latest and is_open_payment(latest[0]["status"]):
            order_buttons = [Button.inline(
                "🔎 پیگیری سفارش باز", f"payopen:{latest[0]['payment_id']}".encode()
            )]
            if not latest[0]["receipt_path"]:
                order_buttons.append(Button.inline(
                    "لغو سفارش", f"payc:{latest[0]['payment_id']}".encode()
                ))
            buttons.insert(0, order_buttons)
        buttons.append([Button.inline("↩️ منوی اصلی", b"menu:main")])
        await self._edit_or_reply(
            event,
            f"📊 اعتبار من\n\n⏳ اعتبار باقی‌مانده: {balance}",
            buttons,
        )

    async def _show_plans(self, event) -> None:
        plans = [plan for plan in await self.db.plans() if plan["price"] > 0]
        lines = ["💳 خرید اشتراک", ""]
        buttons: list[list] = []
        if not plans:
            lines.append("در حال حاضر بسته‌ای برای خرید موجود نیست.")
        else:
            lines.append("بسته را انتخاب کنید. اعتبار هر خرید به موجودی شما اضافه می‌شود.")
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
                    buttons.append([Button.inline(label, f"plan:{plan['code']}".encode())])
            if not self.settings.payment_card_number:
                lines.append("\nپرداخت کارت‌به‌کارت در حال حاضر غیرفعال است.")
        buttons.append([Button.inline("🎧 پشتیبانی", b"support")])
        buttons.append([Button.inline("↩️ منوی اصلی", b"menu:main")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _show_payment_history(self, event, user_id: int, page: int = 0) -> None:
        page_size = 6
        rows = await self.db.payment_history_paginated(
            user_id, limit=page_size + 1, offset=page * page_size
        )
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        lines = [f"🧾 پرداخت‌های من — صفحهٔ {to_persian_digits(page + 1)}"]
        buttons: list[list] = []
        if not rows:
            lines.append("\nپرداختی ثبت نشده است." if page == 0 else "\nمورد دیگری وجود ندارد.")
        for item in rows:
            seconds = item.get("purchased_seconds")
            plan = format_duration(int(seconds)) if seconds else item["plan_code"]
            price = item.get("purchased_price")
            amount = format_toman(int(price)) if price is not None else "مبلغ نامشخص"
            lines.append(
                f"\n#{to_persian_digits(item['payment_id'])} | {plan} | {amount}\n"
                f"{self._payment_status_label(item['status'])}"
            )
            if item["status"] == "rejected" and item.get("review_reason"):
                lines.append(f"دلیل: {item['review_reason']}")
            if is_open_payment(item["status"]):
                buttons.append([Button.inline(
                    f"🔎 پیگیری سفارش {to_persian_digits(item['payment_id'])}",
                    f"payopen:{item['payment_id']}".encode(),
                )])
        nav = []
        if page > 0:
            nav.append(Button.inline("◀️ قبلی", f"payments:{page - 1}".encode()))
        if has_more:
            nav.append(Button.inline("بعدی ▶️", f"payments:{page + 1}".encode()))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("💳 خرید اشتراک", b"buy")])
        buttons.append([Button.inline("↩️ منوی اصلی", b"menu:main")])
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
                "⚠️ یک سفارش باز دارید. ابتدا آن را پیگیری یا لغو کنید.",
                buttons=[[Button.inline("🧾 پرداخت‌های من", b"payments")]],
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
        order_ref = to_persian_digits(payment_id)
        summary = (
            f"🧾 سفارش {order_ref}\n"
            f"بسته: {hours_text} ساعت | مبلغ: {format_toman(plan['price'])}\n"
            f"اعتبار بسته: {format_validity(plan['validity_days'])}"
        )
        if payment["receipt_path"]:
            self._pending_payments.pop(event.sender_id, None)
            text = (
                f"{summary}\n\n"
                "🕒 رسید پرداخت شما ثبت شد و در انتظار بررسی است؛ نیازی به واریز مجدد نیست. "
                "پس از بررسی، نتیجه اطلاع‌رسانی می‌شود."
            )
            buttons = []
        else:
            self._pending_payments[event.sender_id] = payment_id
            text = (
                f"{summary}\n\n"
                "اطلاعات واریز:\n"
                f"💳 شماره کارت: {card}\n"
                f"به نام: {self.settings.payment_card_holder}\n"
                f"بانک: {self.settings.payment_bank_name}\n\n"
                "پس از واریز، تصویر رسید را مستقیماً ارسال کنید یا دکمهٔ زیر را بزنید. "
                "اعتبار فقط پس از تأیید مدیر اضافه می‌شود."
            )
            buttons = [
                [Button.inline("📤 ارسال رسید", f"payr:{payment_id}".encode())],
                [Button.inline("لغو سفارش", f"payc:{payment_id}".encode())],
            ]
        buttons.append([Button.inline("↩️ بازگشت", b"payments")])
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
            if not await asyncio.to_thread(
                _normalize_receipt_image,
                receipt_path,
                max_bytes=self.settings.max_payment_receipt_bytes,
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
        try:
            changed = await self.db.set_payment_receipt(
                payment_id, str(receipt_path), user_id=user_id
            )
        except sqlite3.Error:
            receipt_path.unlink(missing_ok=True)
            self._pending_payments[user_id] = payment_id
            logger.exception("payment receipt storage failed payment_id=%s", payment_id)
            await event.respond("ثبت رسید انجام نشد؛ لطفاً دوباره ارسال کنید.")
            return
        if not changed:
            receipt_path.unlink(missing_ok=True)
            await event.respond("این سفارش دیگر برای ارسال رسید فعال نیست.")
            return
        await event.respond(
            "🕒 رسید پرداخت شما ثبت شد و در انتظار بررسی است. "
            "پس از بررسی، نتیجه اطلاع‌رسانی می‌شود."
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
            path and _is_safe_receipt_path(self.settings.receipt_dir, path)
        )
        for admin_id in sorted(self.settings.admin_ids):
            try:
                if receipt_ready:
                    try:
                        await self.client.send_file(
                            admin_id,
                            path,
                            caption=text[:TELEGRAM_CAPTION_LIMIT],
                            buttons=buttons,
                        )
                    except Exception:
                        # A temporary image-upload failure should not suppress
                        # the review alert itself. The stored receipt remains
                        # available from the order-detail screen.
                        logger.warning(
                            "payment receipt delivery failed payment_id=%s admin_id=%s",
                            payment_id,
                            admin_id,
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
                    "payment notification not delivered payment_id=%s admin_id=%s",
                    payment_id,
                    admin_id,
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
                    f"⏳ {format_duration(seconds)} اعتبار اضافه شد.",
                    buttons=[[Button.inline("📊 اعتبار من", b"account")]],
                )
            except Exception:
                logger.warning(
                    "payment approval notification not delivered payment_id=%s user_id=%s",
                    payment_id,
                    payment["user_id"],
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
                        "payment rejection notification not delivered payment_id=%s user_id=%s",
                        payment_id,
                        payment["user_id"],
                    )
        await self._show_admin_payment_detail(event, payment_id)

    # ----------------------------------------------------------------- admin
    async def _show_admin_overview(self, event) -> None:
        stats = await self.db.stats()
        pending = len(await self.db.pending_review_payments())
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
        """List only receipt-backed orders that actually need a decision."""
        payments = await self.db.pending_review_payments()
        buttons: list[list] = [
            [Button.inline("📋 تاریخچه پرداخت‌ها", b"adm:ph:0")]
        ]
        if not payments:
            buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
            await self._edit_or_reply(
                event,
                "💳 پرداخت‌های در انتظار\n\nدر حال حاضر پرداختی برای بررسی وجود ندارد.",
                buttons,
            )
            return
        lines = [
            f"💳 پرداخت‌های در انتظار — {to_persian_digits(len(payments))} مورد",
            "برای مشاهده رسید و تصمیم، سفارش را باز کنید.",
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
            f"رسید: {'ثبت شده' if payment['receipt_path'] else 'ثبت نشده'}",
        ]
        if payment.get("reviewed_at"):
            lines.append(f"بررسی: {payment['reviewed_at']} | مدیر: {payment['reviewed_by']}")
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
            or not _is_safe_receipt_path(self.settings.receipt_dir, path)
            or not await asyncio.to_thread(
                _normalize_receipt_image,
                Path(path),
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
                logger.warning("support notification not delivered admin=%s", admin_id)

    async def _notify_user(self, user_id: int, text: str, buttons=None) -> None:
        try:
            await self.client.send_message(user_id, text, buttons=buttons)
        except Exception:
            # The state change is already stored; a blocked bot must not undo it.
            logger.warning("support notification not delivered user=%s", user_id)

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
        logger.info("support admin reply ticket=%s admin=%s", ticket_id, admin_id)
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
        text = (
            "🎧 مدیریت پشتیبانی\n\n"
            f"فعال: {to_persian_digits(open_count)} | "
            f"جدید: {to_persian_digits(counts.get('open', 0))} | "
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
        if not self.settings.receipt_dir.is_dir():
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
        for path in self.settings.receipt_dir.glob("receipt-*.jpg"):
            # Safety: only touch files strictly inside receipt_dir (audit F-H01)
            try:
                if str(path) in protected:
                    continue
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


