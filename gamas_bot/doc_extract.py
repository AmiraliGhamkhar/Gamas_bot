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

#: Preview length shown in the confirmation message after extraction.
PREVIEW_CHARS = 300

#: Office files are ZIP containers. Bound expansion before python-docx/pptx
#: opens them, independently of the 50 MB compressed-upload limit.
OFFICE_UNPACKED_MAX_BYTES = 200 * 1024 * 1024
#: Bound stored text and generated chunk metadata for highly compressed files.
MAX_EXTRACTED_DOCUMENT_CHARS = 5_000_000


class DocExtractError(RuntimeError):
    """Raised for a document the bot cannot turn into usable text."""


class ScannedPdfError(DocExtractError):
    """Raised when a PDF has no text layer (OCR is out of scope)."""


class EmptyPresentationTextError(DocExtractError):
    """A valid presentation has no slide text, but may still contain audio."""


def _check_extracted_text_size(characters: int) -> None:
    if characters > MAX_EXTRACTED_DOCUMENT_CHARS:
        raise DocExtractError(
            "متن استخراج‌شده از سقف ربات بیشتر است؛ سند را به چند بخش تقسیم و جداگانه ارسال کنید."
        )


@dataclass(frozen=True, slots=True)
class DocSection:
    """One addressable slice of a document: a page, a slide or a heading."""

    locator: str
    heading: str
    text: str
    heading_level: int | None = None

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
        rendered = (section.render() for section in self.sections)
        return "\n\n".join(text for text in rendered if text.strip())

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

    The threshold is roughly 100 characters per ten pages. A genuinely short
    one-page document is therefore not incorrectly classified as a scan.
    """
    total = sum(len(section.text.strip()) for section in sections)
    threshold = max(1, int(page_count)) * SCANNED_CHARS_PER_PAGE
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
    total_chars = 0
    for number, page in enumerate(pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            logger.warning("pdf page %s could not be parsed", number, exc_info=True)
            text = ""
        cleaned = _clean_block(text)
        total_chars += len(cleaned)
        _check_extracted_text_size(total_chars)
        if cleaned:
            sections.append(
                DocSection(f"صفحهٔ {to_persian_digits(number)}", "", cleaned)
            )
    return tuple(sections), len(pages)


def _check_office_package(path: Path) -> None:
    """Reject malformed or excessively expanded OOXML before parsing it."""
    import zipfile

    from . import presentations

    try:
        if not zipfile.is_zipfile(path):
            raise DocExtractError(
                "فایل Office معتبر نیست؛ اگر قالب قدیمی است آن را به docx یا pptx تبدیل کنید."
            )
        presentations._preflight_zip_directory(path)
        with zipfile.ZipFile(path) as archive:
            total = 0
            for info in archive.infolist():
                if info.flag_bits & 1 or info.file_size < 0:
                    raise DocExtractError("فایل Office رمزدار یا آسیب‌دیده است.")
                total += info.file_size
                if total > OFFICE_UNPACKED_MAX_BYTES:
                    raise DocExtractError("حجم بازشدهٔ فایل Office بیش از حد مجاز است.")
    except DocExtractError:
        raise
    except Exception as exc:
        raise DocExtractError("ساختار فایل Office قابل‌خواندن نیست.") from exc


def _docx_sections(path: Path) -> tuple[tuple[DocSection, ...], int]:
    _check_office_package(path)
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
    heading_level: int | None = None
    buffer: list[str] = []
    total_chars = 0

    def count_text(text: str) -> None:
        nonlocal total_chars
        total_chars += len(text)
        _check_extracted_text_size(total_chars)

    def flush() -> None:
        nonlocal buffer
        block = _clean_block("\n".join(buffer))
        if block or heading:
            number = len(sections) + 1
            locator = (
                f"بخش «{heading[:60]}»" if heading else f"بخش {to_persian_digits(number)}"
            )
            sections.append(DocSection(locator, heading, block, heading_level))
        buffer = []

    # document.paragraphs alone omits tables, which can hold an entire handout.
    # Iterate body blocks in order so table content retains its heading locator.
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for block in document.element.body.iterchildren():
        if isinstance(block, CT_Tbl):
            table = Table(block, document)
            for row in table.rows:
                seen_cells: set[object] = set()
                cells: list[str] = []
                for cell in row.cells:
                    if cell._tc in seen_cells:  # merged cells appear repeatedly
                        continue
                    seen_cells.add(cell._tc)
                    text = _clean_block(cell.text)
                    if text:
                        cells.append(text)
                if cells:
                    row_text = " | ".join(cells)
                    count_text(row_text)
                    buffer.append(row_text)
            continue
        if not isinstance(block, CT_P):
            continue
        paragraph = Paragraph(block, document)
        text = _clean_block(paragraph.text or "")
        if not text:
            continue
        count_text(text)
        if _is_heading(paragraph):
            flush()
            heading = text
            heading_level = _heading_level(paragraph)
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


def _heading_level(paragraph) -> int | None:
    """Preserve the Word heading level where the style supplies one."""
    import re

    name = str(getattr(getattr(paragraph, "style", None), "name", "") or "")
    match = re.search(r"([1-9])\s*$", name)
    if match:
        return int(match.group(1))
    return 1 if _is_heading(paragraph) else None


def sections_from_slides(
    slides: Iterable[object], *, max_chars: int = MAX_MODEL_INPUT_CHARS
) -> tuple[DocSection, ...]:
    """Turn :class:`presentations.SlideText` objects into document sections.

    Shared by the PPTX ingestion path and the lecture pipeline, which caches a
    deck's slide text as a document after it builds the booklet.
    """
    sections: list[DocSection] = []
    total_chars = 0
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
        section = DocSection(
            f"اسلاید {to_persian_digits(getattr(slide, 'number', len(sections) + 1))}",
            title,
            body,
        )
        total_chars += len(section.render()) + (2 if sections else 0)
        _check_extracted_text_size(total_chars)
        sections.append(section)
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
        _check_office_package(path)
        slides = presentations._extract_slide_text(path)
    except DocExtractError:
        raise
    except Exception as exc:
        raise DocExtractError("خواندن متن اسلایدهای این ارائه ناموفق بود.") from exc
    sections = list(sections_from_slides(slides, max_chars=max_chars))
    if not sections:
        raise EmptyPresentationTextError(
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
    # Account for heading rendering and separators as well as raw section text;
    # otherwise many small sections could exceed the stored-text limit.
    total_chars = 0
    nonempty = 0
    for section in sections:
        rendered = section.render()
        if rendered.strip():
            total_chars += len(rendered) + (2 if nonempty else 0)
            nonempty += 1
            _check_extracted_text_size(total_chars)
    # The chunker bounds model inputs; oversized documents are refused rather
    # than silently losing the end of a long page or slide.
    return ExtractedDoc(source_type, page_count, sections)


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
    start = 0
    length = len(text)
    while length - start > limit:
        window = text[start:start + limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        pieces.append(window[:cut].strip())
        start += cut
        # Avoid repeatedly copying the entire remaining document. Only the
        # bounded window and whitespace at its boundary need to be inspected.
        while start < length and text[start].isspace():
            start += 1
    if start < length:
        pieces.append(text[start:])
    return [piece for piece in pieces if piece]


def chunk_sections(
    sections: Iterable[DocSection], *, max_chars: int = MAX_MODEL_INPUT_CHARS
) -> list[DocChunk]:
    """Pack sections into model-sized chunks, preserving their locators.

    A section larger than the budget is split on paragraph boundaries and the
    resulting pieces share the same locator ("صفحهٔ ۴ (۱/۲)"), so an item built
    from any piece still cites the right page.
    """
    budget = int(max_chars)
    if budget < 64:
        raise ValueError("chunk budget must be at least 64 characters")
    chunks: list[DocChunk] = []
    buffer: list[str] = []
    sources: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, sources, size
        text = "\n\n".join(buffer)
        if text:
            chunks.append(DocChunk(
                index=len(chunks),
                locator=_chunk_locator(tuple(sources)),
                sources=tuple(dict.fromkeys(sources)),
                text=text,
            ))
        buffer, sources, size = [], [], 0

    for section in sections:
        rendered = section.render().strip()
        if not rendered:
            continue
        # Each section is explicitly labelled *inside* the text the model sees.
        # Reserve room for its locator, part numbering and separator; otherwise
        # long headings could make an ostensibly bounded chunk exceed the limit.
        base_locator = " ".join(section.locator.split())[:min(100, budget // 3)] or "سند"
        overhead = len(base_locator) + 32
        pieces = _split_paragraphs(rendered, budget - overhead)
        for piece_index, piece in enumerate(pieces, start=1):
            locator = base_locator
            if len(pieces) > 1:
                locator = f"{locator} ({to_persian_digits(piece_index)}/{to_persian_digits(len(pieces))})"
            entry = f"[{locator}]\n{piece}"
            if len(entry) > budget:
                raise ValueError("section locator exceeds the chunk budget")
            separator = 2 if buffer else 0
            if size + separator + len(entry) > budget:
                flush()
                separator = 0
            buffer.append(entry)
            sources.append(locator)
            size += separator + len(entry)
    flush()
    return chunks


def chunk_text(
    text: str, *, max_chars: int = MAX_MODEL_INPUT_CHARS, label: str = "بخش"
) -> list[DocChunk]:
    """Chunk a flat text (a booklet without page numbers) into labelled parts."""
    content = str(text or "").strip()
    if not content:
        return []
    return chunk_sections(
        [DocSection(f"{label} {to_persian_digits(1)}", "", content)],
        max_chars=max_chars,
    )


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
            locator = str(entry.get("locator") or "سند")[:120]
            source_list = tuple(str(item)[:120] for item in sources) if isinstance(sources, list) else ()
            if len(text) > max_chars:
                # Old caches may be oversized. Split them; never silently lose
                # their tail or send an over-context request to a provider.
                repaired = chunk_sections([DocSection(locator, "", text)], max_chars=max_chars)
                for part in repaired:
                    chunks.append(DocChunk(len(chunks), part.locator, part.sources, part.text))
            else:
                chunks.append(DocChunk(len(chunks), locator, source_list or (locator,), text))
    return chunks
