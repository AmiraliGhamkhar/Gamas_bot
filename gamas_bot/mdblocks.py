"""Text → blocks: the one parser every document producer is read by.

The bot has two very different sources of document text: the transcript
engine (Markdown-ish, machine written) and the Free Office section (whatever
a user typed, pasted or uploaded). Both end up in the same Word engine, so
both are read by *this* parser — it decides what a line is, and nothing else
does.

The rules are deliberately small and deterministic:

* ``# text`` … ``###### text`` — a heading. The raw Markdown level is kept, so
  each consumer can apply its own convention (the lecture pipeline starts at
  ``##``; the Office section starts at ``#``).
* ``- text`` / ``* text`` / ``+ text`` / ``• text`` — a bullet.
* a run of two or more *grid rows* — a table whose first row is the header.
* anything else — a paragraph, **one line per paragraph**, so the line breaks
  a user typed survive into the document instead of being reflowed.

A grid row is a line that contains a tab, a Markdown pipe-table row, or a
comma-separated line with **at least three** columns. The three-column rule
is not an accident: Persian prose is full of commas, and a two-column guess
would turn a sentence into a table. A tab or a pipe is an unambiguous signal,
so those are accepted at any width.

Nothing here raises: an unparsable line is a paragraph. A document that
degrades to plain paragraphs is still the user's text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: ``#`` … ``######`` with the text after it.
HEADING_PATTERN = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$")
#: ``-`` / ``*`` / ``+`` / ``•`` with the text after it.
BULLET_PATTERN = re.compile(r"^\s*[-*+•]\s+(.*\S)\s*$")
#: A Markdown pipe-table row: it starts with a pipe and has at least two.
PIPE_ROW_PATTERN = re.compile(r"^\s*\|.*$")
#: The ``|---|---|`` separator row of a Markdown table.
PIPE_SEPARATOR_PATTERN = re.compile(r"^\s*\|(?:\s*:?-{1,}:?\s*\|)+\s*$")
#: A horizontal rule: skipped, never rendered as a paragraph of dashes.
RULE_PATTERN = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
#: A comma-separated row needs at least this many columns to count as a table.
MIN_CSV_COLUMNS = 3
#: The deepest heading level any consumer can render.
MAX_HEADING_LEVEL = 3


@dataclass(frozen=True, slots=True)
class TextBlock:
    """One structural element of a document.

    ``kind`` is one of ``heading``, ``paragraph``, ``bullet`` or ``table``.
    A table carries its cells in ``rows`` (the first row is the header); every
    other kind carries its text in ``text``.
    """

    kind: str
    text: str = ""
    level: int = 1
    rows: tuple[tuple[str, ...], ...] = ()

    @property
    def is_table(self) -> bool:
        return self.kind == "table"


def heading_level(raw_level: int, *, offset: int = 0) -> int:
    """Map a raw Markdown heading level to a renderable one.

    ``offset=-1`` keeps the lecture pipeline's convention (its top level is
    ``##``, so ``##`` is the first heading style); ``offset=0`` is plain
    Markdown, where ``#`` is the top level. The result is always clamped to
    ``1..MAX_HEADING_LEVEL`` because the Word engine has three heading styles.
    """
    return min(max(int(raw_level) + int(offset), 1), MAX_HEADING_LEVEL)


def _split_pipe_row(line: str) -> tuple[str, ...] | None:
    """Split a Markdown pipe row into its cells, or ``None`` if it is not one."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    body = stripped.strip("|")
    if "|" not in body:
        return None
    return tuple(cell.strip() for cell in body.split("|"))


def _split_cells(line: str) -> tuple[str, ...] | None:
    """Split one line into table cells, or ``None`` when it is not a grid row.

    A tab wins over a comma: a TSV file whose cells contain commas is still a
    TSV file, and splitting it on commas would shred every cell.
    """
    if "\t" in line:
        return tuple(cell.strip() for cell in line.split("\t"))
    pipe = _split_pipe_row(line)
    if pipe is not None:
        return pipe
    if line.count(",") >= MIN_CSV_COLUMNS - 1:
        return tuple(cell.strip() for cell in line.split(","))
    return None


def _separator_kind(line: str) -> str | None:
    """The separator a grid line uses: ``tab``, ``pipe`` or ``comma``."""
    if "\t" in line:
        return "tab"
    if _split_pipe_row(line) is not None:
        return "pipe"
    if line.count(",") >= MIN_CSV_COLUMNS - 1:
        return "comma"
    return None


def _fits_run(line: str, kind: str, columns: int) -> bool:
    """True when ``line`` may join the grid run of ``kind``/``columns``."""
    if kind == "tab":
        return "\t" in line
    if kind == "pipe":
        return _split_pipe_row(line) is not None
    if kind == "comma":
        return line.count(",") == columns - 1
    return False


def _collect_grid(lines: list[str], index: int) -> tuple[list[tuple[str, ...]], int]:
    """Collect the grid run starting at ``lines[index]``.

    Returns the rows (a Markdown separator row is dropped) and the index of
    the first line *after* the run. A run of one line is not a table: the
    caller keeps that line as a paragraph.
    """
    first = lines[index]
    kind = _separator_kind(first)
    if kind is None:
        return [], index
    columns = len(_split_cells(first) or ())
    rows: list[tuple[str, ...]] = []
    cursor = index
    while cursor < len(lines):
        line = lines[cursor].rstrip()
        if not line.strip():
            break
        if kind == "pipe" and PIPE_SEPARATOR_PATTERN.match(line):
            cursor += 1
            continue
        if not _fits_run(line, kind, columns):
            break
        cells = _split_cells(line)
        if cells is None:
            break
        rows.append(cells)
        cursor += 1
    return rows, cursor


def _normalize_rows(rows: list[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    """Pad every row to the width of the widest one.

    A short row is padded with empty cells and a long row is trimmed, so a
    table is always rectangular — and no cell is ever lost in the middle of
    it, which is what a spreadsheet needs to stay readable.
    """
    width = max((len(row) for row in rows), default=0)
    if width <= 0:
        return ()
    return tuple(
        tuple(list(row[:width]) + [""] * (width - len(row[:width])))
        for row in rows
    )


def parse_blocks(text: str) -> list[TextBlock]:
    """Parse a document's text into its blocks.

    The order of the text is preserved, and every line ends up in exactly one
    block: nothing the user wrote is dropped, and nothing is invented.
    """
    blocks: list[TextBlock] = []
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index].rstrip()
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if RULE_PATTERN.match(stripped):
            index += 1
            continue
        heading = HEADING_PATTERN.match(line)
        if heading:
            blocks.append(
                TextBlock(
                    "heading",
                    heading.group(2).strip(),
                    level=min(len(heading.group(1)), MAX_HEADING_LEVEL),
                )
            )
            index += 1
            continue
        bullet = BULLET_PATTERN.match(line)
        if bullet:
            blocks.append(TextBlock("bullet", bullet.group(1).strip()))
            index += 1
            continue
        if _separator_kind(stripped) is not None:
            rows, cursor = _collect_grid(lines, index)
            if len(rows) >= 2:
                blocks.append(TextBlock("table", rows=_normalize_rows(rows)))
                index = cursor
                continue
        blocks.append(TextBlock("paragraph", stripped))
        index += 1
    return blocks


def blocks_to_text(blocks: list[TextBlock]) -> str:
    """Render blocks back to the text they came from.

    Used by the Office section to hand a *parsed* document to a renderer that
    speaks plain text. The round trip is lossless for every kind but the
    table, which becomes a Markdown pipe table.
    """
    lines: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            lines.append(f"{'#' * max(1, block.level)} {block.text}")
        elif block.kind == "bullet":
            lines.append(f"- {block.text}")
        elif block.kind == "table":
            for row in block.rows:
                lines.append("| " + " | ".join(row) + " |")
            if block.rows:
                lines.append("|" + "|".join("---" for _ in block.rows[0]) + "|")
        else:
            lines.append(block.text)
    return "\n".join(lines)
