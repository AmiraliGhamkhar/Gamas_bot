"""Telegram message rendering and pagination.

Telegram is a hard boundary with three rules that the rest of the bot should
not have to remember:

* one message carries at most 4096 **UTF-16 code units** of *parsed* text
  (HTML tags do not count, an astral character counts twice);
* a message whose markup nests the same tag (``<b><b>x</b></b>``) is rejected
  as a whole, taking the content with it;
* a table split across two messages loses its header, so a table has to be
  rendered before it is paginated.

This module owns exactly that: it converts the small Markdown subset the note
model emits into Telegram-safe HTML and splits the *rendered* result on line
boundaries that never cut inside a tag. ``gamas_bot.bot`` re-exports these
names for backward compatibility.
"""

from __future__ import annotations

import html
import re

MESSAGE_CHUNK_SIZE = 3800
# Telegram refuses messages whose *parsed* text is longer than 4096 UTF-16
# code units. HTML tags do not count, but the renderer below can still grow the
# visible text (table headers are repeated on every row), so the rendered page
# is what has to be measured before sending.
TELEGRAM_TEXT_LIMIT = 4096
MIN_CHUNK_SIZE = 400
TAG_PATTERN = re.compile(r"<[^>]+>")
# A configuration error (unsupported language/model for the selected engine)
# is actionable for the operator, so it is reported verbatim. Transient provider
# failures are not: the tracking reference is what the user needs for those.
def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = MESSAGE_CHUNK_SIZE) -> list[str]:
    if limit <= 0:
        raise ValueError("Message limit must be positive")
    text = text.strip()
    pages: list[str] = []
    while _utf16_length(text) > limit:
        used = 0
        safe_end = 0
        for index, character in enumerate(text):
            width = 2 if ord(character) > 0xFFFF else 1
            if used + width > limit:
                break
            used += width
            safe_end = index + 1
        cut = text.rfind("\n\n", 0, safe_end)
        if cut < safe_end // 2:
            cut = text.rfind("\n", 0, safe_end)
        if cut < safe_end // 2:
            cut = text.rfind(" ", 0, safe_end)
        if cut < safe_end // 2:
            cut = safe_end
        # A zero-width cut would loop forever on the same text.
        cut = max(cut, 1)
        pages.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pages.append(text)
    return pages or [""]


def _plain_length(rendered: str) -> int:
    """Length Telegram counts for an HTML message: tags off, entities decoded."""
    return _utf16_length(html.unescape(TAG_PATTERN.sub("", rendered)))


def _split_rendered(rendered: str, budget: int) -> list[str]:
    """Split already rendered HTML on line boundaries, never inside a tag.

    ``markdown_to_telegram_html`` renders every source line independently, so a
    tag never spans a newline and cutting there keeps the markup valid.
    """
    pages: list[str] = []
    current: list[str] = []
    used = 0
    for line in rendered.split("\n"):
        length = _plain_length(line)
        if length > budget:
            if current:
                pages.append("\n".join(current))
                current, used = [], 0
            # One oversized line: drop its markup so it can be cut anywhere.
            plain = html.unescape(TAG_PATTERN.sub("", line))
            pages.extend(html.escape(page, quote=False) for page in split_message(plain, budget))
            continue
        if current and used + length + 1 > budget:
            pages.append("\n".join(current))
            current, used = [line], length
            continue
        current.append(line)
        used += length + 1
    if current:
        pages.append("\n".join(current))
    return [page for page in pages if page.strip()]


def render_pages(text: str, reserve: int = 64) -> list[str]:
    """Render Markdown once, then paginate the HTML for Telegram.

    Rendering before splitting matters: a table cut in half would otherwise
    lose its header and the next page's first data row would be mistaken for
    one. Paginating the rendered text also measures what Telegram counts,
    because the renderer repeats table headers and can grow the text well past
    the length of its Markdown source.
    """
    budget = max(MIN_CHUNK_SIZE, TELEGRAM_TEXT_LIMIT - reserve)
    return _split_rendered(markdown_to_telegram_html(text), budget) or [""]


def _emphasis_is_nested(value: str) -> bool:
    """True when every <b>/<i> tag closes in the reverse order it opened."""
    stack: list[str] = []
    for tag in re.findall(r"</?[bi]>", value):
        if not tag.startswith("</"):
            stack.append(tag[1])
        elif not stack or stack.pop() != tag[2]:
            return False
    return not stack


def markdown_to_telegram_html(text: str) -> str:
    """Convert the small Markdown subset used by the LLM to Telegram-safe HTML."""
    # NUL is not valid Telegram text and must not impersonate our code placeholders.
    lines = text.replace("\x00", "").splitlines()
    result: list[str] = []
    table_header: list[str] | None = None
    table_rows = 0
    in_code = False

    def inline(value: str) -> str:
        value = html.escape(value, quote=False)
        # Stash inline code first so its contents are not treated as Markdown.
        stashed: list[str] = []

        def stash(match: re.Match) -> str:
            stashed.append(match.group(1))
            return f"\x00{len(stashed) - 1}\x00"

        value = re.sub(r"`([^`]+)`", stash, value)
        plain = value
        value = re.sub(r"\*\*(?!\s)(.+?)(?<!\s)\*\*", r"<b>\1</b>", value)
        # Emphasis markers must hug their text and sit outside a word, so
        # "2 * 3 * 4" and identifiers such as @a_b_c survive untouched.
        value = re.sub(r"(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", value)
        value = re.sub(r"(?<![\w_])_(?!\s)([^_]+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", value)
        if not _emphasis_is_nested(value):
            # Crossed markers such as "**a *b** c*" would yield <b><i></b></i>,
            # which Telegram rejects as a whole ("can't parse entities").
            # Showing the markers literally beats losing the message.
            value = plain
        return re.sub(
            r"\x00(\d+)\x00",
            lambda match: f"<code>{stashed[int(match.group(1))]}</code>",
            value,
        )

    def strong(value: str) -> str:
        """Wrap already-rendered inline HTML in <b> without nesting a bold tag.

        Telegram rejects the *whole* message when the same formatting tag
        appears inside itself ("<b><b>x</b></b>"), so a heading or table header
        that is itself bold ("# **title**") must not gain a second <b>.
        """
        if "<b>" in value or "</b>" in value:
            return value
        return f"<b>{value}</b>"

    def strong_label(value: str, suffix: str = ":") -> str:
        """``strong(value)`` keeping ``suffix`` inside the bold run when possible."""
        if "<b>" in value or "</b>" in value:
            return value + suffix
        return f"<b>{value}{suffix}</b>"

    def flush_table() -> None:
        """Emit a table header that never received a data row."""
        nonlocal table_header, table_rows
        if table_header is not None and not table_rows:
            cells = [inline(cell) for cell in table_header if cell.strip()]
            if cells:
                result.append(" · ".join(strong(cell) for cell in cells))
        table_header = None
        table_rows = 0

    for line in lines:
        stripped = line.strip()
        in_table = not in_code and stripped.startswith("|") and "|" in stripped[1:]
        if not in_table:
            flush_table()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            # Code lines are escaped verbatim; Markdown does not apply inside fences.
            result.append(html.escape(line, quote=False))
            continue
        if in_table:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            # Markdown only requires one dash per separator cell (|--|-:|).
            if cells and all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            if table_header is None:
                table_header = cells
                table_rows = 0
                continue
            # A row may carry more cells than the header (malformed tables are
            # common in generated Markdown). Extra cells keep their value so no
            # content is ever dropped, and an empty header label is omitted
            # instead of rendering a stray colon.
            pairs = []
            for index, cell in enumerate(cells):
                if not cell:
                    continue
                label = table_header[index].strip() if index < len(table_header) else ""
                pairs.append(
                    f"{strong_label(inline(label))} {inline(cell)}" if label else inline(cell)
                )
            result.append(" · ".join(pairs) if pairs else inline(" | ".join(cells)))
            table_rows += 1
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if heading:
            result.append(strong(inline(heading.group(1).strip())))
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            item = re.sub(r"^\s*[-*+]\s+", "", line)
            result.append("• " + inline(item))
            continue
        if re.match(r"^\s*\d+[.)]\s+", line):
            result.append(inline(line.strip()))
            continue
        result.append(inline(line))
    flush_table()
    return "\n".join(result).strip()


