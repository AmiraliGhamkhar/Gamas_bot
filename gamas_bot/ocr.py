"""API-free Persian/Arabic OCR using a local Tesseract executable.

The Python package does not download models or contact a cloud OCR provider.
On cPanel, the host must provide Tesseract (or the operator must install a
compatible local binary and set TESSERACT_CMD/TESSDATA_PREFIX).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

DEFAULT_LANGUAGES = "fas+ara+eng"
SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
DEFAULT_MAX_PAGES = 20
DEFAULT_MAX_BYTES = 15 * 1024 * 1024


class OcrError(RuntimeError):
    """A safe-to-display OCR failure."""


def _tesseract_command() -> str:
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    command = configured or shutil.which("tesseract")
    if not command:
        raise OcrError(
            "OCR محلی روی این سرور آماده نیست: برنامهٔ Tesseract نصب نیست. "
            "از میزبان cPanel بخواهید Tesseract با زبان‌های fas و ara را نصب کند."
        )
    return command


def _run_tesseract(
    command: str, image: bytes, *, languages: str, timeout_seconds: float
) -> str:
    try:
        result = subprocess.run(
            [command, "stdin", "stdout", "-l", languages, "--psm", "3"],
            input=image,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError("زمان OCR تمام شد؛ فایل کوچک‌تر یا واضح‌تری بفرستید.") from exc
    except OSError as exc:
        raise OcrError("اجرای Tesseract روی این سرور ممکن نیست.") from exc

    if result.returncode:
        stderr = result.stderr.decode("utf-8", errors="replace").lower()
        if "failed loading language" in stderr or "could not initialize" in stderr:
            raise OcrError(
                "دادهٔ زبان OCR موجود نیست. Tesseract باید زبان‌های fas و ara "
                "و در صورت نیاز eng را داشته باشد."
            )
        raise OcrError("Tesseract نتوانست این صفحه را پردازش کند.")
    return result.stdout.decode("utf-8", errors="replace").strip()


def extract_text(
    path: str | Path,
    *,
    languages: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = 240.0,
) -> str:
    """Extract text from a supported image/PDF without any network/API calls.

    Searchable PDF pages use their embedded text layer. Scanned PDF pages are
    rasterized with PyMuPDF and passed to local Tesseract one at a time.
    """
    source = Path(path)
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise OcrError("فرمت پشتیبانی نمی‌شود؛ PDF یا تصویر بفرستید.")
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise OcrError("فایل بارگذاری‌شده پیدا نشد.") from exc
    if size <= 0 or size > max_bytes:
        raise OcrError(f"حجم فایل باید کمتر از {max_bytes // (1024 * 1024)} مگابایت باشد.")

    command = _tesseract_command()
    language_setting = (languages or os.environ.get("OCR_LANGUAGES", DEFAULT_LANGUAGES)).strip()
    if not language_setting or any(ch.isspace() for ch in language_setting):
        raise OcrError("تنظیم زبان OCR نامعتبر است.")

    # Confirm the configured language packs exist before doing expensive work.
    try:
        langs = subprocess.run(
            [command, "--list-langs"], capture_output=True, text=True,
            timeout=10, check=False, env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OcrError("بررسی نصب Tesseract روی سرور ناموفق بود.") from exc
    if langs.returncode:
        raise OcrError("Tesseract روی سرور به‌درستی اجرا نمی‌شود.")
    available = set(langs.stdout.splitlines()[1:])
    missing = [lang for lang in language_setting.split("+") if lang not in available]
    if missing:
        raise OcrError(
            "دادهٔ زبان OCR نصب نیست: " + ", ".join(missing)
            + ". میزبان باید زبان‌های fas و ara را نصب کند."
        )

    if source.suffix.lower() != ".pdf":
        text = _run_tesseract(
            command, source.read_bytes(), languages=language_setting,
            timeout_seconds=timeout_seconds,
        )
        if not text:
            raise OcrError("متنی در تصویر تشخیص داده نشد؛ تصویر واضح‌تری بفرستید.")
        return text

    try:
        import fitz  # PyMuPDF; ships wheels and does not require Poppler.
    except ImportError as exc:
        raise OcrError("وابستگی PDF نصب نیست؛ بستهٔ PyMuPDF را نصب کنید.") from exc

    pieces: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    try:
        with fitz.open(source) as document:
            if document.is_encrypted:
                raise OcrError("PDF رمزدار پشتیبانی نمی‌شود؛ نسخهٔ بدون رمز بفرستید.")
            if not 1 <= document.page_count <= max_pages:
                raise OcrError(f"PDF باید بین ۱ تا {max_pages} صفحه داشته باشد.")
            for page in document:
                embedded = page.get_text("text").strip()
                if embedded:
                    pieces.append(embedded)
                    continue
                # 2x is a practical compromise for shared-hosting CPU and memory.
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = pixmap.tobytes("png")
                page_timeout = deadline - time.monotonic()
                if page_timeout <= 0:
                    raise OcrError("زمان OCR تمام شد؛ PDF کوتاه‌تر یا فایل کوچک‌تری بفرستید.")
                page_text = _run_tesseract(
                    command, image, languages=language_setting,
                    timeout_seconds=page_timeout,
                )
                pieces.append(page_text)
    except OcrError:
        raise
    except Exception as exc:
        raise OcrError("PDF خراب است یا قابل خواندن نیست.") from exc

    text = "\n\n".join(part for part in pieces if part).strip()
    if not text:
        raise OcrError("متنی در فایل تشخیص داده نشد؛ فایل واضح‌تری بفرستید.")
    return text
