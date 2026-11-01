"""Message intake classification, receipt validation, and failure taxonomy (SRP / OCP).

Separates pure inspection/validation helpers — attachment MIME/extension
classifiers, receipt image normalization & directory containment, transcript
part splitting, and extensible failure categorization — from stateful bot
handlers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Any

from telethon.tl.types import MessageMediaWebPage

from .db import InsufficientBalanceError
from .doc_extract import classify_document, classify_image
from .media import MediaToolError
from .office import OfficeError, classify_text_file
from .presentations import PresentationError, classify_presentation
from .stt import STTConfigurationError, STTError

AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".amr",
    ".au",
    ".flac",
    ".m4a",
    ".m4b",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".ra",
    ".wav",
    ".wma",
    ".webm",
}
VIDEO_EXTENSIONS = {
    ".3gp",
    ".asf",
    ".avi",
    ".flv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
    ".wmv",
}

USER_VISIBLE_ERRORS = (
    ValueError,
    PresentationError,
    STTConfigurationError,
    MediaToolError,
    OfficeError,
)

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

_FAILURE_KIND_BY_STATE: dict[str, str] = {
    "downloading": "download",
    "extracting": "media",
    "transcribing": "stt",
    "generating_notes": "notes",
    "uploading": "delivery",
}


@dataclass(frozen=True, slots=True)
class FailureClassificationRule:
    """Maps an exception hierarchy to a reporting failure kind (OCP)."""

    exception_types: tuple[type[BaseException], ...]
    kind: str


FAILURE_CLASSIFICATION_RULES: list[FailureClassificationRule] = [
    FailureClassificationRule((InsufficientBalanceError,), "billing"),
    FailureClassificationRule((asyncio.CancelledError,), "cancelled"),
    FailureClassificationRule((STTError,), "stt"),
    FailureClassificationRule((MediaToolError,), "media"),
    FailureClassificationRule((sqlite3.Error,), "database"),
    FailureClassificationRule((ValueError, PresentationError, OfficeError), "input"),
]


def register_failure_rule(
    exception_types: tuple[type[BaseException], ...],
    kind: str,
    *,
    prepend: bool = True,
) -> None:
    """Register an exception-to-failure-kind mapping rule (OCP extension point)."""
    rule = FailureClassificationRule(exception_types=exception_types, kind=kind)
    if prepend:
        FAILURE_CLASSIFICATION_RULES.insert(0, rule)
    else:
        FAILURE_CLASSIFICATION_RULES.append(rule)


def classify_failure(state: str, exc: BaseException) -> str:
    """Map a failed job to one reporting category (never raises)."""
    for rule in FAILURE_CLASSIFICATION_RULES:
        if isinstance(exc, rule.exception_types):
            return rule.kind
    return _FAILURE_KIND_BY_STATE.get(state, "internal")


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


MAX_LOGGED_STRING_CHARS = 200


def _sanitize_log_value(value: object, *, limit: int = MAX_LOGGED_STRING_CHARS) -> str:
    """Return a single-line, truncated representation safe for logs."""
    text = " ".join(str(value).split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text


_RAW_PART_BOUNDARIES = ("\n\n", "\n", "؟ ", ". ", "، ", " ")


def raw_parts(text: str, *, max_chars: int, max_parts: int) -> list[str]:
    """Split a raw transcript into delivery-sized parts, or refuse."""
    body = text
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
            return []
        window = rest[:max_chars]
        cut = max_chars
        for boundary in _RAW_PART_BOUNDARIES:
            index = window.rfind(boundary)
            if index >= max_chars // 2:
                cut = index + len(boundary)
                break
        parts.append(rest[:cut])
        rest = rest[cut:]
    return parts


def _is_safe_receipt_path(receipt_dir: Path, receipt_path: str | Path) -> bool:
    """True when *receipt_path* is a single-link regular file in *receipt_dir*."""
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
    """Parse a Telegram user ID from admin command input robustly."""
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
    """Validate and re-encode a JPEG/PNG/WebP receipt as a clean JPEG."""
    temp_path: Path | None = None
    try:
        size = path.stat().st_size
        if size < 8 or size > max_bytes:
            return False
        with path.open("rb") as source:
            header = source.read(16)
        is_jpeg = header.startswith(b"\xff\xd8\xff")
        is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
        is_webp = (
            header.startswith(b"RIFF")
            and len(header) >= 12
            and header[8:12] == b"WEBP"
        )
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
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _media_metadata(message: Any) -> tuple[str | None, str | None, float | None]:
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
        kind = None
    elif getattr(message, "video_note", None) or getattr(message, "video", None):
        kind = "video"
    elif message.document is not None:
        if normalized_mime.startswith("audio/"):
            kind = "audio"
        elif normalized_mime.startswith("video/"):
            kind = "video"
        elif extension in VIDEO_EXTENSIONS:
            kind = "video"
        elif extension in AUDIO_EXTENSIONS:
            kind = "audio"
    if kind is None:
        return None, None, None
    duration = getattr(file_obj, "duration", None)
    if duration is None and message.voice:
        duration = getattr(message.voice, "duration", None)
    return kind, filename, float(duration) if duration else None


def _presentation_metadata(message: Any) -> tuple[str | None, str | None]:
    """Return (kind, filename) for PowerPoint documents."""
    if message.document is None or message.voice or message.audio:
        return None, None
    filename = getattr(message.file, "name", None)
    mime_type = getattr(message.file, "mime_type", None)
    kind = classify_presentation(filename, mime_type)
    if kind is None:
        return None, None
    return kind, filename


def _document_metadata(message: Any) -> tuple[str | None, str | None]:
    """Return the PDF/Word/PowerPoint document type and original filename."""
    if message.document is None or message.voice or message.audio:
        return None, None
    filename = getattr(message.file, "name", None)
    mime_type = getattr(message.file, "mime_type", None)
    source_type = classify_document(filename, mime_type)
    if source_type not in {"pdf", "docx", "pptx"}:
        return None, None
    return source_type, filename


def _text_file_metadata(message: Any) -> tuple[str | None, str | None]:
    """Return an allowed Free Office text suffix and original filename."""
    if message.document is None or message.voice or message.audio:
        return None, None
    filename = getattr(message.file, "name", None)
    mime_type = getattr(message.file, "mime_type", None)
    suffix = classify_text_file(filename, mime_type)
    return (suffix, filename) if suffix is not None else (None, filename)


def _image_metadata(message: Any) -> tuple[str | None, str | None]:
    """``("image", name)`` for a photo or image file the OCR flow can read."""
    if (
        getattr(message, "sticker", None) is not None
        or getattr(message, "gif", None) is not None
    ):
        return None, None
    if (
        getattr(message, "voice", None) is not None
        or getattr(message, "audio", None) is not None
    ):
        return None, None
    file_obj = getattr(message, "file", None)
    name = getattr(file_obj, "name", None)
    mime = getattr(file_obj, "mime_type", None)
    if getattr(message, "photo", None) is not None:
        if mime and not str(mime).lower().startswith("image/"):
            return None, None
        return "image", name
    if getattr(message, "document", None) is None:
        return None, None
    return ("image", name) if classify_image(name, mime) else (None, None)


def _is_unsupported_attachment(message: Any) -> bool:
    """True for a real file the bot cannot use — never for links or text."""
    media = getattr(message, "media", None)
    if media is None or isinstance(media, MessageMediaWebPage):
        return False
    if (
        getattr(message, "sticker", None) is not None
        or getattr(message, "gif", None) is not None
    ):
        return False
    return (
        getattr(message, "document", None) is not None
        or getattr(message, "photo", None) is not None
    )


OPEN_PAYMENT_STATUSES = frozenset({"awaiting_payment", "pending_review", "pending"})


def is_open_payment(status: object) -> bool:
    return str(status) in OPEN_PAYMENT_STATUSES


def is_pending_payment_review(payment: dict[str, Any]) -> bool:
    """Whether an order has a receipt and can receive an admin decision."""
    return str(payment.get("status")) in {"pending_review", "pending"} and bool(
        payment.get("receipt_path")
    )


RECEIPT_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def _is_receipt_image(message: Any) -> bool:
    """Accept Telegram photos and image documents supported by the normalizer."""
    if (
        getattr(message, "sticker", None) is not None
        or getattr(message, "gif", None) is not None
    ):
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
