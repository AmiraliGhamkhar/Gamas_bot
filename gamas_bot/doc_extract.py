"""Document ingestion: PDF / DOCX / PPTX → text sections → model-sized chunks.

The learning loop can be grounded in a *booklet* the bot generated itself, but
users also arrive with their own material. This module turns an uploaded
document into the same shape the generators already understand — a plain text
stream plus the locator of every part of it — so a generated card can always
say which page, slide or section it came from.

Design rules:

* **Pure-ish and blocking.** Every ``extract_*`` function is synchronous and
  CPU/disk bound; callers run them through ``fileio.run_file_io`` so the
  Telethon event loop never stalls.
* **No OCR.** A scanned PDF (a text-less image container) is refused with an
  explicit Persian message instead of producing an empty deck. OCR stays out of
  scope by decision, not by accident.
* **Reuse, never duplicate.** PowerPoint text comes from
  :mod:`gamas_bot.presentations` (the same reader the lecture pipeline uses), so
  a deck parses identically in both flows. Chunk sizes come from
  :mod:`gamas_bot.learning`, so a chunk can never exceed the model budget.
* **Source anchoring.** A section carries a human-readable ``locator``
  («صفحهٔ ۳», «اسلاید ۷», «بخش «تنفس سلولی»») and a chunk carries the ordered
  list of the locators it spans. The generators copy that locator onto every
  item they emit.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .learning import MAX_MODEL_INPUT_CHARS
from .progress import to_persian_digits

logger = logging.getLogger(__name__)

#: MIME types the ingestion flow accepts. The PowerPoint MIME list also covers
#: the slideshow/template variants Telegram clients report for a plain .pptx.
PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PPTX_MIME_ALIASES = frozenset(
    {
        PPTX_MIME,
        "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
        "application/vnd.openxmlformats-officedocument.presentationml.template",
    }
)

#: Extension fallback: some clients send ``application/octet-stream``.
DOC_EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    # Legacy formats are accepted on a best-effort basis: .ppt goes through the
    # existing ppt2pptx conversion, .doc is refused with a friendly message
    # because no verified pure-Python reader exists for the binary format.
    ".ppt": "pptx",
    ".pps": "pptx",
    ".pot": "pptx",
    ".doc": "docx",
}

SOURCE_TYPE_LABELS_FA = {"pdf": "PDF", "docx": "Word", "pptx": "PowerPoint"}

#: A page holding fewer than this many characters is treated as image-only when
#: the *whole* document is text-poor (see :func:`looks_scanned`).
SCANNED_CHARS_PER_PAGE = 10
SCANNED_MIN_CHARS = 100

#: Preview length shown in the confirmation message after extraction.
PREVIEW_CHARS = 300


class DocExtractError(RuntimeError):
    """Raised for a document the bot cannot turn into usable text."""


class ScannedPdfError(DocExtractError):
    """Raised when a PDF has no text layer (OCR is out of scope)."""


@dataclass(frozen=True, slots=True)
class DocSection:
    """One addressable slice of a document: a page, a slide or a heading."""

    locator: str
    heading: str
    text: str

    def render(self) -> str:
        heading = self.heading.strip()
        body = self.text.strip()
        if heading and body and heading not in body:
            return f"{heading}\n{body}"
        return body or heading


@dataclass(frozen=True, slots=True)
class DocChunk:
    """A piece of a document that fits one model call."""

    index: int
    locator: str
    sources: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class ExtractedDoc:
    """The whole extraction result stored for a document."""

    source_type: str
    page_count: int
    sections: tuple[DocSection, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        return "\n\n".join(
            section.render() for section in self.sections if section.render().strip()
        )

    @property
    def char_count(self) -> int:
        return sum(len(section.text.strip()) for section in self.sections)


def normalize_mime(mime_type: str | None) -> str:
    return (mime_type or "").split(";", 1)[0].strip().lower()


def classify_document(filename: str | None, mime_type: str | None) -> str | None:
    """Return ``"pdf" | "docx" | "pptx"`` for an accepted upload, else ``None``.

    MIME wins because it is what Telegram actually reports; the extension is a
    fallback for clients that send ``application/octet-stream``.
    """
    mime = normalize_mime(mime_type)
    if mime == PDF_MIME:
        return "pdf"
    if mime == DOCX_MIME:
        return "docx"
    if mime in PPTX_MIME_ALIASES:
        return "pptx"
    if not filename:
        return None
    return DOC_EXTENSIONS.get(Path(filename).suffix.lower())


def document_title(filename: str | None, source_type: str) -> str:
    """A clean, bounded title for the library row and the document card."""
    raw = Path(filename).name if filename else ""
    title = " ".join(str(raw).split())[:120]
    if title:
        return title
    return f"سند {SOURCE_TYPE_LABELS_FA.get(source_type, source_type)}"


def sha256_file(path: Path) -> str:
    """Hash a file in bounded slices (never loads the whole upload into RAM)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def looks_scanned(sections: Iterable[DocSection], page_count: int) -> bool:
    """True when the text layer is too thin to be real content.

    The rule is a floor of 100 characters *and* a rate of ten characters per
    page: a slide deck with one short title per slide is legitimate, while a
    30-page scan yields a handful of stamps and nothing else.
    """
    total = sum(len(section.text.strip()) for section in sections)
    threshold = max(SCANNED_MIN_CHARS, int(page_count) * SCANNED_CHARS_PER_PAGE)
    return total < threshold


def _pdf_sections(path: Path) -> tuple[tuple[DocSection, ...], int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise DocExtractError("خواندن PDF در این نسخه فعال نیست.") from exc
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise DocExtractError("این فایل PDF قابل‌خواندن نیست یا آسیب دیده است.") from exc
    if getattr(reader, "is_encrypted", False):
        # An empty password covers the common "owner password only" case; a real
        # user password cannot be guessed and is reported as such.
        try:
            unlocked: int = int(reader.decrypt("") or 0)
        except Exception:
            unlocked = 0
        if not unlocked:
            raise DocExtractError(
                "این PDF با رمز محافظت شده است؛ نسخهٔ بدون رمز را بفرستید."
            )
    sections: list[DocSection] = []
    try:
        pages = list(reader.pages)
    except Exception as exc:
        raise DocExtractError("خواندن صفحه‌های PDF ناموفق بود.") from exc
    for number, page in enumerate(pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            logger.warning("pdf page %s could not be parsed", number, exc_info=True)
            text = ""
        cleaned = _clean_block(text)
        if cleaned:
            sections.append(
                DocSection(f"صفحهٔ {to_persian_digits(number)}", "", cleaned)
            )
    return tuple(sections), len(pages)


def _docx_sections(path: Path) -> tuple[tuple[DocSection, ...], int]:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise DocExtractError("خواندن Word در این نسخه فعال نیست.") from exc
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise DocExtractError(
            "این فایل Word قابل‌خواندن نیست. اگر پسوند doc است، آن را با "
            "Word به docx ذخیره و دوباره بفرستید."
        ) from exc

    sections: list[DocSection] = []
    heading = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        block = _clean_block("\n".join(buffer))
        if block:
            number = len(sections) + 1
            locator = (
                f"بخش «{heading[:60]}»" if heading else f"بخش {to_persian_digits(number)}"
            )
            sections.append(DocSection(locator, heading, block))
        buffer = []

    for paragraph in getattr(document, "paragraphs", []):
        text = _clean_block(getattr(paragraph, "text", "") or "")
        if not text:
            continue
        if _is_heading(paragraph):
            flush()
            heading = text
            continue
        buffer.append(text)
    flush()

    if not sections:
        raise DocExtractError(
            "این سند متنی ندارد (احتمالاً فقط تصویر است)؛ OCR پشتیبانی نمی‌شود."
        )
    # Word has no reliable page count without rendering; heading/paragraph
    # blocks are the navigable unit, so that is what we report.
    return tuple(sections), len(sections)


def _is_heading(paragraph) -> bool:
    """Detect a heading paragraph across English and Persian Word styles."""
    try:
        name = str(paragraph.style.name or "").strip().lower()
    except Exception:  # pragma: no cover - defensive against odd documents
        return False
    if not name:
        return False
    return name.startswith(("heading", "title", "subtitle", "عنوان", "سرعنوان"))


def sections_from_slides(
    slides: Iterable[object], *, max_chars: int = MAX_MODEL_INPUT_CHARS
) -> tuple[DocSection, ...]:
    """Turn :class:`presentations.SlideText` objects into document sections.

    Shared by the PPTX ingestion path and the lecture pipeline, which caches a
    deck's slide text as a document after it builds the booklet.
    """
    sections: list[DocSection] = []
    for slide in slides:
        body_lines = [
            str(line).strip()
            for line in getattr(slide, "body", ()) or ()
            if str(line).strip()
        ]
        notes = _clean_block(getattr(slide, "notes", "") or "")
        if notes:
            body_lines.append(f"یادداشت گوینده: {notes}")
        body = _clean_block("\n".join(body_lines))
        title = _clean_block(getattr(slide, "title", "") or "")
        if not body and not title:
            continue  # an empty slide is skipped, not an error
        sections.append(
            DocSection(
                f"اسلاید {to_persian_digits(getattr(slide, 'number', len(sections) + 1))}",
                title,
                body[:max_chars],
            )
        )
    return tuple(sections)


def _pptx_sections(path: Path, *, max_chars: int) -> tuple[tuple[DocSection, ...], int]:
    # The PowerPoint reader is shared with the lecture pipeline: one deck must
    # parse the same way whether it arrived as a lecture or as a document.
    import zipfile

    from . import presentations

    if not zipfile.is_zipfile(path):
        raise DocExtractError(
            "این فایل یک ارائهٔ معتبر PowerPoint نیست یا آسیب دیده است."
        )
    try:
        presentations._preflight_zip_directory(path)
        slides = presentations._extract_slide_text(path)
    except Exception as exc:
        raise DocExtractError("خواندن متن اسلایدهای این ارائه ناموفق بود.") from exc
    sections = list(sections_from_slides(slides, max_chars=max_chars))
    if not sections:
        raise DocExtractError(
            "این ارائه متن قابل‌استفاده‌ای ندارد؛ OCR پشتیبانی نمی‌شود."
        )
    return tuple(sections), len(slides)


def extract_document(
    path: Path, source_type: str, *, max_chars: int = MAX_MODEL_INPUT_CHARS
) -> ExtractedDoc:
    """Extract a PDF/DOCX/PPTX into locator-tagged sections (blocking)."""
    source_type = (source_type or "").strip().lower()
    if source_type == "pdf":
        sections, page_count = _pdf_sections(path)
        if looks_scanned(sections, page_count):
            raise ScannedPdfError(
                "این PDF لایهٔ متنی ندارد (ظاهراً اسکن‌شده است)؛ OCR پشتیبانی "
                "نمی‌شود. لطفاً نسخهٔ متنی یا Word فایل را بفرستید."
            )
    elif source_type == "docx":
        sections, page_count = _docx_sections(path)
    elif source_type == "pptx":
        sections, page_count = _pptx_sections(path, max_chars=max_chars)
    else:  # pragma: no cover - guarded by classify_document
        raise DocExtractError("این نوع سند پشتیبانی نمی‌شود.")
    # Hard-trim every section so a pathological single page cannot exceed the
    # provider budget; chunking below cuts on paragraph boundaries anyway.
    bounded = tuple(
        DocSection(section.locator, section.heading, section.text[:max_chars])
        for section in sections
    )
    return ExtractedDoc(source_type, page_count, bounded)


def _clean_block(text: str) -> str:
    """Collapse whitespace while keeping paragraph breaks meaningful."""
    lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
    kept = [line for line in lines if line]
    return "\n".join(kept).strip()


def _split_paragraphs(text: str, limit: int) -> list[str]:
    """Split on paragraph (then line, then word) boundaries under ``limit``."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return [piece for piece in pieces if piece]


def chunk_sections(
    sections: Iterable[DocSection], *, max_chars: int = MAX_MODEL_INPUT_CHARS
) -> list[DocChunk]:
    """Pack sections into model-sized chunks, preserving their locators.

    A section larger than the budget is split on paragraph boundaries and the
    resulting pieces share the same locator ("صفحهٔ ۴ (۱/۲)"), so an item built
    from any piece still cites the right page.
    """
    budget = max(500, int(max_chars))
    chunks: list[DocChunk] = []
    buffer: list[str] = []
    sources: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, sources, size
        text = "\n\n".join(part for part in buffer if part).strip()
        if text:
            chunks.append(
                DocChunk(
                    index=len(chunks),
                    locator=_chunk_locator(tuple(sources)),
                    sources=tuple(dict.fromkeys(sources)),
                    text=text,
                )
            )
        buffer = []
        sources = []
        size = 0

    for section in sections:
        rendered = section.render().strip()
        if not rendered:
            continue
        pieces = _split_paragraphs(rendered, budget)
        for piece_index, piece in enumerate(pieces, start=1):
            locator = section.locator
            if len(pieces) > 1:
                locator = f"{locator} ({to_persian_digits(piece_index)}/{to_persian_digits(len(pieces))})"
            piece_len = len(piece)
            entry = f"{locator}\n{piece}" if section.heading.strip() else piece
            if size and size + piece_len + 2 > budget:
                flush()
            if piece_len > budget:
                # A single paragraph longer than the budget is hard-trimmed; the
                # locator still points at the right place.
                entry = f"{locator}\n{piece[:budget]}"
                piece_len = budget
            buffer.append(entry)
            sources.append(locator)
            size += piece_len + 2
    flush()
    return chunks


def chunk_text(
    text: str, *, max_chars: int = MAX_MODEL_INPUT_CHARS, label: str = "بخش"
) -> list[DocChunk]:
    """Chunk a flat text (a booklet without page numbers) into labelled parts."""
    paragraphs = _split_paragraphs(str(text or ""), max(500, int(max_chars)))
    sections = [
        DocSection(f"{label} {to_persian_digits(index)}", "", piece)
        for index, piece in enumerate(paragraphs, start=1)
    ]
    return chunk_sections(sections, max_chars=max_chars)


def _chunk_locator(sources: tuple[str, ...]) -> str:
    unique = list(dict.fromkeys(sources))
    if not unique:
        return "سند"
    if len(unique) == 1:
        return unique[0]
    return f"{unique[0]} تا {unique[-1]}"


def preview_text(text: str, limit: int = PREVIEW_CHARS) -> str:
    """The first ``limit`` characters on a word boundary, for the reply."""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    window = cleaned[:limit]
    cut = window.rfind(" ")
    return (window[:cut] if cut > limit // 2 else window).rstrip() + "…"


def chunks_to_json(chunks: Iterable[DocChunk]) -> str:
    import json

    return json.dumps(
        [
            {
                "index": chunk.index,
                "locator": chunk.locator,
                "sources": list(chunk.sources),
                "text": chunk.text,
            }
            for chunk in chunks
        ],
        ensure_ascii=False,
    )


def chunks_from_json(payload: str | None, *, max_chars: int = MAX_MODEL_INPUT_CHARS) -> list[DocChunk]:
    """Rebuild cached chunks from the ``documents.chunks_json`` column.

    A missing or unparsable cache is repaired by re-chunking the stored text, so
    generation never fails because of a bad cache row.
    """
    import json

    try:
        raw = json.loads(payload or "[]")
    except (TypeError, ValueError):
        raw = []
    chunks: list[DocChunk] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            sources = entry.get("sources")
            chunks.append(
                DocChunk(
                    index=len(chunks),
                    locator=str(entry.get("locator") or "سند")[:200],
                    sources=tuple(
                        str(item)[:200] for item in sources
                    )
                    if isinstance(sources, list)
                    else (),
                    text=text[:max_chars],
                )
            )
    return chunks
