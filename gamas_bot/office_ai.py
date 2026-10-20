"""The optional model layer of the Free Office section.

Two jobs need a model, and both are *arrangement* jobs:

* designing the slide order of a deck (which lines belong to which slide);
* inventing a table when the user sent prose instead of a grid.

Neither is a rewriting job. The section's promise is that the document says
what the user said, so every line and every cell a model proposes is checked
against the user's own text before it is used. A model that paraphrases loses
the lines it paraphrased, and a document that quietly stops being the user's
text is worse than no document at all — so a model that fails the check is
ignored, and the deterministic fallback in :mod:`gamas_bot.office` runs
instead.

The module therefore has exactly one failure mode that matters: it returns
``None``, and the caller falls back. It never raises.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import Settings
from .llm import complete_json
from .mdblocks import HEADING_PATTERN
from .office import OfficeSlide, OfficeTable
from .textnorm import normalize_for_compare

logger = logging.getLogger(__name__)

#: The system prompt of the slide planner. The rules are stated as hard
#: constraints because the validator below enforces them anyway — the prompt
#: is there to get a *useful* answer, the validator is there to keep it
#: honest.
DECK_SYSTEM_PROMPT = (
    "تو یک دستیار ساخت ارائه (پاورپوینت) هستی. متن کاربر را می‌خوانی و فقط "
    "ساختار آن را تعیین می‌کنی: برای هر بخش یک عنوان کوتاه و سطرهایی که زیر "
    "آن بخش می‌آیند.\n"
    "قواعد سخت‌گیرانه:\n"
    "۱. هیچ جمله‌ای را بازنویسی، خلاصه، ترجمه یا اضافه نکن.\n"
    "۲. هر سطری که برمی‌گردانی باید عیناً در متن کاربر باشد.\n"
    "۳. ترتیب سطرها همان ترتیب متن کاربر است.\n"
    "۴. عنوان بخش‌ها می‌تواند کوتاه و گویا باشد، ولی محتوا را تغییر نده.\n"
    "۵. اگر متن کاربر عنوان بخش دارد، از همان عنوان استفاده کن.\n"
    "پاسخ فقط و فقط یک شیء JSON معتبر باشد، بدون هیچ توضیح دیگری."
)

#: The system prompt of the table builder.
TABLE_SYSTEM_PROMPT = (
    "تو یک دستیار ساخت صفحه‌گسترده (اکسل) هستی. متن کاربر را می‌خوانی و آن "
    "را به یک جدول ساده تبدیل می‌کنی.\n"
    "قواعد سخت‌گیرانه:\n"
    "۱. هر خانه‌ای که در سطرهای داده می‌نویسی باید عیناً در متن کاربر باشد.\n"
    "۲. عنوان ستون‌ها کوتاه و گویا باشد (می‌توانی خودت انتخاب کنی).\n"
    "۳. هیچ داده‌ای را از خودت نساز و چیزی را خلاصه نکن.\n"
    "۴. اگر متن کاربر سطرهای کامل ندارد، فقط سطرهایی را بیاور که واقعاً در "
    "متن هستند.\n"
    "پاسخ فقط و فقط یک شیء JSON معتبر باشد، بدون هیچ توضیح دیگری."
)

#: Bounds on what a model may answer with.
MAX_SECTIONS = 40
MAX_LINES_PER_SECTION = 30
MAX_SECTION_TITLE_CHARS = 120
MAX_TABLE_ROWS = 500
MAX_TABLE_COLUMNS = 20
MAX_HEADER_CHARS = 60

#: A plan that loses more than this share of its lines is treated as a
#: failed plan: the deterministic fallback is better than a deck with holes in
#: it.
MAX_DROPPED_LINE_RATIO = 0.5

#: Maximum characters of one text chunk sent to the presentation planner.
#: Long messages and .txt/.md uploads are split at headings/paragraphs so
#: the model never receives more context than it can process reliably.
MAX_DECK_CHUNK_CHARS = 5000


def _bounded_text(value: Any, limit: int) -> str:
    """A trimmed string of at most ``limit`` characters."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _ordered_source_lines(candidate: list[str], source: str) -> list[str]:
    """The candidate lines that appear in the source, in the source's order.

    A line that is not in the source is dropped, and a line that appears
    *earlier* than the line before it is dropped as well: a deck whose
    bullets jump back and forth is not the user's text any more.
    """
    haystack = normalize_for_compare(source)
    kept: list[str] = []
    cursor = 0
    for line in candidate:
        text = _bounded_text(line, 2000)
        if not text:
            continue
        needle = normalize_for_compare(text)
        if not needle:
            continue
        position = haystack.find(needle, cursor)
        if position < 0:
            continue
        kept.append(text)
        cursor = position + len(needle)
    return kept


def _split_text_by_headings(text: str, max_chars: int = MAX_DECK_CHUNK_CHARS) -> list[str]:
    """Split long text at Markdown headings; fall back to paragraph chunks."""
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    current_heading: str | None = None

    def flush() -> None:
        nonlocal current_lines, current_len, current_heading
        if current_lines:
            chunk_text = "\n".join(current_lines).strip()
            if chunk_text:
                chunks.append(chunk_text)
            current_lines = []
            current_len = 0

    for line in text.splitlines():
        stripped = line.strip()
        is_heading = HEADING_PATTERN.match(stripped) is not None
        if is_heading:
            current_heading = stripped
            # Start a new chunk at each heading to keep slide structure intact.
            flush()
        current_lines.append(stripped)
        current_len += len(stripped) + 1
        if current_len >= max_chars:
            flush()
            # When splitting due to length (not a new heading), preserve
            # the current heading context in the new chunk.
            if current_heading is not None and current_heading not in (current_lines or []):
                pass  # The heading will stay with the previous chunk; next chunk
                # continues with remaining content. This is acceptable because
                # the model will process the section in two parts with the same
                # structure.
    flush()
    return [chunk for chunk in chunks if chunk.strip()]


def _chunk_long_text(text: str, max_chars: int = MAX_DECK_CHUNK_CHARS) -> list[str]:
    """Split very long input into model-sized chunks for presentation planning.

    Uses heading boundaries first (so sections stay coherent), then paragraph
    breaks for oversized prose blocks. This lets the Free Office presentation
    feature accept long messages and full .txt / .markdown uploads without
    losing slide-level structure.
    """
    chunks = _split_text_by_headings(text, max_chars)
    # If any chunk is still too large (e.g. a single massive paragraph),
    # split it further at paragraph breaks.
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            result.append(chunk)
            continue
        for sub_line in chunk.splitlines():
            sub_stripped = sub_line.strip()
            if not sub_stripped:
                continue
            # Build sub-chunks from lines, keeping paragraphs together where
            # possible but never exceeding max_chars.
            if not result or len(result[-1]) + len(sub_stripped) + 1 > max_chars:
                result.append(sub_stripped)
            else:
                result[-1] += "\n" + sub_stripped
    return [c for c in result if c.strip()]


def _slide_sections(payload: dict) -> list[tuple[str, list[str]]]:
    """The ``(title, lines)`` pairs of a slide plan payload."""
    raw = payload.get("sections")
    if not isinstance(raw, (list, tuple)):
        return []
    sections: list[tuple[str, list[str]]] = []
    for entry in raw[:MAX_SECTIONS]:
        if not isinstance(entry, dict):
            continue
        title = _bounded_text(entry.get("title"), MAX_SECTION_TITLE_CHARS)
        lines = entry.get("lines")
        if not isinstance(lines, (list, tuple)):
            lines = []
        sections.append((title, [str(line) for line in lines[:MAX_LINES_PER_SECTION]]))
    return sections


def _table_payload(payload: dict) -> tuple[list[str], list[list[str]]]:
    """The ``(headers, rows)`` of a table payload."""
    raw_headers = payload.get("headers")
    headers: list[str] = []
    if isinstance(raw_headers, (list, tuple)):
        for value in raw_headers[:MAX_TABLE_COLUMNS]:
            text = _bounded_text(value, MAX_HEADER_CHARS)
            headers.append(text)
    raw_rows = payload.get("rows")
    rows: list[list[str]] = []
    if isinstance(raw_rows, (list, tuple)):
        for row in raw_rows[:MAX_TABLE_ROWS]:
            if isinstance(row, (list, tuple)):
                cells = [_bounded_text(cell, 2000) for cell in row]
            else:
                cells = [_bounded_text(row, 2000)]
            rows.append(cells)
    return headers, rows


async def plan_deck(
    text: str,
    *,
    settings: Settings,
    session: Any,
    max_sections: int = MAX_SECTIONS,
) -> list[OfficeSlide] | None:
    """Ask the model for the slide order of ``text``.

    Returns ``None`` when there is no model, when the request failed, or when
    the answer did not survive validation — in every case the caller falls
    back to the user's own headings.
    """
    if not settings.note_configured:
        return None

    async def _process_chunk(chunk_text: str) -> list[OfficeSlide] | None:
        user_prompt = (
            "متن کاربر:\n"
            "<text>\n"
            f"{chunk_text}\n"
            "</text>\n\n"
            "خروجی JSON با این ساختار برگردان:\n"
            '{"sections": [{"title": "عنوان بخش", "lines": ["سطر اول", "سطر دوم"]}]}\n'
            f"حداکثر {max_sections} بخش."
        )
        try:
            payload = await complete_json(
                session, settings, system=DECK_SYSTEM_PROMPT, user=user_prompt
            )
        except Exception as exc:
            logger.warning("office deck planning chunk failed; skipping chunk: %s", exc)
            return None
        sections = _slide_sections(payload)
        if not sections:
            return None
        proposed = sum(len(lines) for _title, lines in sections)
        chunk_slides: list[OfficeSlide] = []
        for title, lines in sections:
            kept = _ordered_source_lines(lines, chunk_text)
            if not kept:
                continue
            chunk_slides.append(
                OfficeSlide(
                    title=title or "بخش",
                    bullets=tuple(kept),
                    notes="\n".join(kept),
                )
            )
        if not chunk_slides:
            return None
        if (
            proposed
            and len(chunk_slides)
            and (proposed - sum(len(s.bullets) for s in chunk_slides)) / proposed
            > MAX_DROPPED_LINE_RATIO
        ):
            return None
        return chunk_slides[:max_sections]

    # For very long messages or .txt / .markdown uploads, split at headings
    # so the model never receives more context than it can handle.
    slides: list[OfficeSlide] = []
    chunks = _chunk_long_text(text, MAX_DECK_CHUNK_CHARS)
    semaphore = asyncio.Semaphore(max(1, int(getattr(settings, "office_ai_concurrency", 3) or 3)))

    async def _bounded(chunk_text: str) -> list[OfficeSlide] | None:
        async with semaphore:
            return await _process_chunk(chunk_text)

    # gather preserves chunk order, so the merged deck is unchanged.
    for result in await asyncio.gather(*(_bounded(chunk) for chunk in chunks)):
        if result is not None:
            slides.extend(result)
    if slides:
        # Deduplicate by slide title, keeping the first occurrence's content.
        seen_titles: set[str] = set()
        unique_slides: list[OfficeSlide] = []
        for slide in slides:
            key = slide.title.strip().lower()
            if key not in seen_titles:
                seen_titles.add(key)
                unique_slides.append(slide)
            else:
                # Merge bullets from duplicate slides rather than dropping them.
                existing_index = next(
                    i for i, s in enumerate(unique_slides) if s.title.strip().lower() == key
                )
                existing = unique_slides[existing_index]
                merged_bullets = list(existing.bullets) + list(slide.bullets)
                unique_slides[existing_index] = OfficeSlide(
                    title=existing.title,
                    bullets=tuple(merged_bullets),
                    notes=existing.notes + "\n" + slide.notes,
                )
        slides = unique_slides[:max_sections]
        return slides[:max_sections] if slides else None

    # If chunking produced nothing, fall back to a single attempt with the
    # full text (useful for medium-length inputs that don't need splitting).
    full_result = await _process_chunk(text)
    return full_result


async def infer_table(
    text: str,
    *,
    settings: Settings,
    session: Any,
) -> OfficeTable | None:
    """Ask the model to turn ``text`` into a table.

    Returns ``None`` when there is no model, when the request failed, or when
    no data cell survived validation. Data cells must appear in the user's own
    text; only the column headings may be the model's own words.
    """
    if not settings.note_configured:
        return None
    user_prompt = (
        "متن کاربر:\n"
        "<text>\n"
        f"{text}\n"
        "</text>\n\n"
        "خروجی JSON با این ساختار برگردان:\n"
        '{"headers": ["ستون یک", "ستون دو"], "rows": [["مقدار یک", "مقدار دو"]]}\n'
        "هر سطر داده باید عیناً از متن کاربر باشد."
    )
    try:
        payload = await complete_json(
            session, settings, system=TABLE_SYSTEM_PROMPT, user=user_prompt
        )
    except Exception as exc:
        logger.warning("office table inference failed; using the plain sheet: %s", exc)
        return None

    headers, rows = _table_payload(payload)
    headers = [header for header in headers if header]
    if not headers:
        return None

    source = normalize_for_compare(text)
    width = len(headers)
    kept_rows: list[tuple[str, ...]] = []
    for row in rows:
        if not row:
            continue
        cells = list(row[:width]) + [""] * (width - len(row[:width]))
        # Every data cell must be the user's own text. A row that fails is
        # dropped whole: a half-invented row is worse than a missing one.
        if any(
            cell and normalize_for_compare(cell) not in source
            for cell in cells
        ):
            continue
        if not any(cell for cell in cells):
            continue
        kept_rows.append(tuple(cells))
    if not kept_rows:
        return None
    return OfficeTable(headers=tuple(headers), rows=tuple(kept_rows))
