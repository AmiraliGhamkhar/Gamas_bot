"""Operator smoke test for the opt-in OCR service (never run by the bot).

The bot's OCR path talks to i2ocr.com, a third-party website; the offline suite
can only prove the protocol against a mock (``tests/test_ocr.py``). Before
letting users rely on ``OCR_ENABLED=true``, run this against one real file of
your own — a scanned page or a photo — on the deployment host::

    python -m scripts.ocr_smoke scan.jpg
    python -m scripts.ocr_smoke lesson.pdf --pages 1,2 --language persian

It prints, stage by stage, what the service answered, and the recognised text
length (not the text itself: this is a diagnostic, not an OCR viewer). Exit
code is 1 when any stage fails, and the error names the stage that stopped —
``challenge``, ``upload``, ``pdf2png`` or ``ocr_backend`` — which is exactly
what a future protocol change needs. Nothing is written to the database.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from gamas_bot.ocr import (
    DEFAULT_LANGUAGE,
    I2OCRClient,
    OcrError,
    curl_cffi_available,
    normalize_ocr_language,
)


def _parse_pages(raw: str) -> list[int]:
    pages: list[int] = []
    for part in str(raw or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            number = int(part)
        except ValueError:
            raise SystemExit(f"--pages: {part!r} is not a page number") from None
        if number < 1:
            raise SystemExit(f"--pages: page numbers start at 1 (got {number})")
        pages.append(number)
    return pages or [1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the i2ocr OCR path.")
    parser.add_argument("file", type=Path, help="a scanned page image or a PDF")
    parser.add_argument(
        "--language",
        default=DEFAULT_LANGUAGE,
        help=f"i2ocr language slug (default: {DEFAULT_LANGUAGE}; 'fa' is accepted)",
    )
    parser.add_argument(
        "--pages",
        default="1",
        help="PDF pages to recognise, comma separated (default: 1)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="seconds between requests (default: 2.0, be polite to a free service)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=300.0,
        help="wall-clock budget for the run in seconds (default: 300)",
    )
    options = parser.parse_args(argv)

    if not options.file.is_file():
        print(f"file not found: {options.file}", file=sys.stderr)
        return 1
    try:
        language = normalize_ocr_language(options.language)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not curl_cffi_available():
        print(
            "curl-cffi is missing: pip install 'gamas-bot[ocr]' (see docs/OCR.md)",
            file=sys.stderr,
        )
        return 1

    pages = _parse_pages(options.pages)
    started = time.monotonic()
    print(f"file     : {options.file} ({options.file.stat().st_size} bytes)")
    print(f"language : {language}")
    print(f"pages    : {', '.join(str(page) for page in pages)}")
    client = I2OCRClient(min_interval=options.interval)
    try:
        recognized = client.extract_pages(
            options.file, pages, language=language, budget_seconds=options.budget
        )
    except OcrError as exc:
        print(f"FAILED   : {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    elapsed = time.monotonic() - started
    if not recognized:
        print("FAILED   : the service returned no page at all", file=sys.stderr)
        return 1
    for page in recognized:
        preview = " ".join(page.text.split())[:60]
        print(f"page {page.number:>3}  : {len(page.text):>7} chars  {preview}")
    empty = [page.number for page in recognized if not page.text.strip()]
    print(f"elapsed  : {elapsed:.1f}s for {len(recognized)} page(s)")
    if empty:
        print(f"note     : no text recognised on page(s) {empty} (blank or unclear scan)")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator tool
    raise SystemExit(main())
