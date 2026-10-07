"""Offline page renderer for the generated .docx booklets.

There is no Word, LibreOffice or PDF tool in the deployment/CI container, so a
document could previously only be checked by parsing its XML. This renderer
draws the real layout instead: it reads ``document.xml``/``styles.xml`` plus the
header/footer parts, wraps the text with Pillow (Persian is pre-shaped with
``arabic-reshaper`` + ``python-bidi``) and paints each page at a fixed scale.

It is deliberately a *layout* renderer, not a Word clone: justification is
approximate and only the bundled DejaVu faces are used. That is enough to
inspect what actually matters — cover composition, the page frame, the running
header, the footer and its page numbers, heading hierarchy, tables, callouts,
page breaks, whitespace, orphaned headings, clipped/overflowing text and blank
pages — and it never needs the network.

Usage::

    python -m scripts.render_docx_pages booklet.docx --out pages/
    python -m scripts.render_docx_pages booklet.docx --sheet sheet.png
    python -m scripts.render_docx_pages booklet.docx --json facts.json
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

if __package__ in (None, ""):  # allow "python scripts/render_docx_pages.py"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree
from PIL import Image, ImageDraw, ImageFont

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"w": W, "r": R}


def _q(tag: str) -> str:
    return f"{{{W}}}{tag}"


#: Rendering resolution. 100 dpi keeps an A4 page under 1200 px tall, which is
#: enough to read the layout and small enough to inspect several pages at once.
DPI = 100
#: Twips (1/1440 inch) -> pixels.
TWIP = DPI / 1440.0
#: Fallback page geometry (A4) when a section does not state its own.
FALLBACK_PAGE = {"w": 11906, "h": 16838}
FALLBACK_MARGINS = {"top": 1361, "right": 1247, "bottom": 1247, "left": 1247,
                    "header": 680, "footer": 680}

_FONT_CACHE: dict[tuple[str, bool, int], ImageFont.FreeTypeFont] = {}
#: Font files that ship with the DejaVu package; the renderer still works (with
#: an approximate face) when none of them is installed.
FONT_CANDIDATES = {
    ("sans", False): ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",),
    ("sans", True): ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",),
    ("serif", False): ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",),
    ("serif", True): ("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",),
    ("mono", False): ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",),
    ("mono", True): ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",),
}


def font(family: str, bold: bool, size_pt: float) -> ImageFont.FreeTypeFont:
    size = max(int(round(size_pt * DPI / 72.0)), 6)
    key = (family, bold, size)
    if key not in _FONT_CACHE:
        for candidate in FONT_CANDIDATES.get(key[:2], FONT_CANDIDATES[("sans", False)]):
            if Path(candidate).is_file():
                _FONT_CACHE[key] = ImageFont.truetype(candidate, size)
                break
        else:  # pragma: no cover - depends on the host's font packages
            _FONT_CACHE[key] = ImageFont.load_default(size=size)
    return _FONT_CACHE[key]


# ---------------------------------------------------------------------------
# XML -> blocks
# ---------------------------------------------------------------------------


def _val(element, attribute: str = "val", default=None):
    if element is None:
        return default
    return element.get(f"{{{W}}}{attribute}", default)


def _hex_color(value, default=(0, 0, 0)):
    if not value:
        return default
    value = value.lstrip("#")
    if len(value) != 6:
        return default
    try:
        return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return default


def _spacing(p_pr) -> dict:
    spacing = p_pr.find(_q("spacing")) if p_pr is not None else None
    if spacing is None:
        return {"before": 0, "after": 0, "line": 240, "rule": "auto"}
    return {
        "before": int(spacing.get(f"{{{W}}}before", 0) or 0),
        "after": int(spacing.get(f"{{{W}}}after", 0) or 0),
        "line": int(spacing.get(f"{{{W}}}line", 240) or 240),
        "rule": spacing.get(f"{{{W}}}lineRule", "auto"),
    }


def _paragraph_runs(paragraph) -> tuple[list[dict], bool]:
    """Runs of one paragraph: text plus the effective formatting of each run."""
    runs: list[dict] = []
    page_break = False
    # ``paragraph.iter`` (not a direct-child loop): a run may sit inside
    # ``w:hyperlink`` — every clickable TOC row does — and a renderer that skips
    # those shows an empty topic list, which is exactly the kind of regression
    # this tool exists to catch.
    for child in paragraph.iter(_q("r")):
        r_pr = child.find(_q("rPr"))
        text_parts: list[str] = []
        for node in child:
            if node.tag == _q("t"):
                text_parts.append(node.text or "")
            elif node.tag == _q("tab"):
                text_parts.append("\t")
            elif node.tag == _q("br"):
                if node.get(f"{{{W}}}type") == "page":
                    page_break = True
                else:
                    text_parts.append("\n")
        text = "".join(text_parts)
        if not text:
            continue
        size = None
        if r_pr is not None and r_pr.find(_q("sz")) is not None:
            size = int(r_pr.find(_q("sz")).get(f"{{{W}}}val", 0) or 0) / 2.0
        bold = r_pr is not None and r_pr.find(_q("b")) is not None
        rtl = r_pr is not None and r_pr.find(_q("rtl")) is not None
        color = _hex_color(_val(r_pr.find(_q("color"))) if r_pr is not None else None)
        font_name = ""
        if r_pr is not None and r_pr.find(_q("rFonts")) is not None:
            font_name = r_pr.find(_q("rFonts")).get(f"{{{W}}}ascii", "") or ""
        runs.append(
            {"text": text, "size": size, "bold": bold, "rtl": rtl,
             "color": color, "font": font_name}
        )
    return runs, page_break


def _paragraph(node, styles: dict) -> dict:
    p_pr = node.find(_q("pPr"))
    style_name = ""
    if p_pr is not None and p_pr.find(_q("pStyle")) is not None:
        style_name = p_pr.find(_q("pStyle")).get(f"{{{W}}}val", "") or ""
    effective = styles.get(style_name, {})
    alignment = _val(p_pr.find(_q("jc"))) if p_pr is not None else None
    shade = None
    if p_pr is not None and p_pr.find(_q("shd")) is not None:
        shade = _hex_color(_val(p_pr.find(_q("shd")), "fill", ""), None)
    bordered = p_pr is not None and p_pr.find(_q("pBdr")) is not None
    indent = {"left": 0, "right": 0, "hanging": 0}
    if p_pr is not None and p_pr.find(_q("ind")) is not None:
        ind = p_pr.find(_q("ind"))
        for key in ("left", "right", "hanging"):
            indent[key] = int(ind.get(f"{{{W}}}{key}", 0) or 0)
    runs, page_break = _paragraph_runs(node)
    return {
        "kind": "p",
        "runs": runs,
        "text": "".join(run["text"] for run in runs),
        "style": style_name,
        "style_size": effective.get("size"),
        "style_bold": effective.get("bold", False),
        "style_color": effective.get("color"),
        "style_space_before": effective.get("before", 0),
        "style_space_after": effective.get("after", 0),
        "align": alignment or effective.get("align"),
        "spacing": _spacing(p_pr),
        "shade": shade,
        "bordered": bordered,
        "indent": indent,
        "page_break": page_break,
        "has_sectpr": p_pr is not None and p_pr.find(_q("sectPr")) is not None,
        "sectpr": p_pr.find(_q("sectPr")) if p_pr is not None else None,
        "keep_next": p_pr is not None and p_pr.find(_q("keepNext")) is not None,
    }


def _table(node, styles: dict) -> dict:
    rows: list[list[dict]] = []
    widths: list[int] = []
    grid = node.find(_q("tblGrid"))
    if grid is not None:
        widths = [int(col.get(f"{{{W}}}w", 0) or 0) for col in grid.findall(_q("gridCol"))]
    header_fill = None
    repeat_header = False
    for row_index, row in enumerate(node.findall(_q("tr"))):
        tr_pr = row.find(_q("trPr"))
        if tr_pr is not None and tr_pr.find(_q("tblHeader")) is not None:
            repeat_header = True
        cells = []
        for cell in row.findall(_q("tc")):
            tc_pr = cell.find(_q("tcPr"))
            fill = None
            if tc_pr is not None and tc_pr.find(_q("shd")) is not None:
                fill = _hex_color(_val(tc_pr.find(_q("shd")), "fill", ""), None)
            paragraphs = [_paragraph(p, styles) for p in cell.findall(_q("p"))]
            cells.append({"paragraphs": paragraphs, "fill": fill})
            if row_index == 0 and fill:
                header_fill = fill
        rows.append(cells)
    return {
        "kind": "tbl",
        "rows": rows,
        "widths": widths,
        "repeat_header": repeat_header,
        "header_fill": header_fill,
    }


def _styles_from(styles_xml: bytes) -> dict:
    """Effective paragraph-style facts: size (pt), boldness, colour, spacing."""
    result: dict[str, dict] = {}
    if not styles_xml:
        return result
    root = etree.fromstring(styles_xml)
    for style in root.findall(_q("style")):
        if style.get(f"{{{W}}}type") not in (None, "paragraph"):
            continue
        name = _val(style.find(_q("name"))) or style.get(f"{{{W}}}styleId", "")
        p_pr = style.find(_q("pPr"))
        r_pr = style.find(_q("rPr"))
        entry: dict = {}
        if r_pr is not None and r_pr.find(_q("sz")) is not None:
            entry["size"] = int(r_pr.find(_q("sz")).get(f"{{{W}}}val", 0) or 0) / 2.0
        if r_pr is not None and r_pr.find(_q("b")) is not None:
            entry["bold"] = _val(r_pr.find(_q("b"))) not in ("0", "false")
        if r_pr is not None and r_pr.find(_q("color")) is not None:
            entry["color"] = _hex_color(_val(r_pr.find(_q("color"))))
        if p_pr is not None:
            entry.update(
                {
                    "before": _spacing(p_pr)["before"],
                    "after": _spacing(p_pr)["after"],
                    "align": _val(p_pr.find(_q("jc"))),
                }
            )
        # styleId and the display name are both valid references
        result[name] = entry
        style_id = style.get(f"{{{W}}}styleId")
        if style_id and style_id != name:
            result[style_id] = entry
    return result


def _section_props(sect_pr, relationships: dict, relationship_texts: dict) -> dict:
    page = dict(FALLBACK_PAGE)
    margins = dict(FALLBACK_MARGINS)
    borders = None
    header_text = ""
    footer_text = ""
    if sect_pr is None:
        return {"page": page, "margins": margins, "borders": borders,
                "page_number_start": None,
                "header_footer": {"header": header_text, "footer": footer_text}}
    size = sect_pr.find(_q("pgSz"))
    if size is not None:
        page = {
            "w": int(size.get(f"{{{W}}}w", FALLBACK_PAGE["w"])),
            "h": int(size.get(f"{{{W}}}h", FALLBACK_PAGE["h"])),
        }
    mar = sect_pr.find(_q("pgMar"))
    if mar is not None:
        for key in margins:
            margins[key] = int(mar.get(f"{{{W}}}{key}", margins[key]))
    page_number_start = None
    number_type = sect_pr.find(_q("pgNumType"))
    if number_type is not None and number_type.get(f"{{{W}}}start"):
        try:
            page_number_start = int(number_type.get(f"{{{W}}}start"))
        except ValueError:
            page_number_start = None
    pg_borders = sect_pr.find(_q("pgBorders"))
    if pg_borders is not None:
        borders = {
            "offset": pg_borders.get(f"{{{W}}}offsetFrom", "page"),
            "edges": {},
        }
        for edge in ("top", "left", "bottom", "right"):
            node = pg_borders.find(_q(edge))
            if node is not None:
                borders["edges"][edge] = {
                    "val": _val(node),
                    "color": _hex_color(_val(node, "color")),
                    "size": int(node.get(f"{{{W}}}sz", 8) or 8),
                    "space": int(node.get(f"{{{W}}}space", 24) or 24),
                }
    for kind, target in (("header", "headerReference"), ("footer", "footerReference")):
        ref = sect_pr.find(_q(target))
        if ref is not None:
            rid = ref.get(f"{{{R}}}id")
            part = relationships.get(rid)
            if part:
                text = relationship_texts.get(part, "")
                if kind == "header":
                    header_text = text
                else:
                    footer_text = text
    return {
        "page": page,
        "margins": margins,
        "borders": borders,
        "page_number_start": page_number_start,
        "header_footer": {"header": header_text, "footer": footer_text},
    }


def _header_footer_texts(zf: zipfile.ZipFile, relationships: dict) -> dict[str, str]:
    texts: dict[str, str] = {}
    for target in relationships.values():
        if not (target.startswith("header") or target.startswith("footer")):
            continue
        path = f"word/{target}"
        try:
            root = etree.fromstring(zf.read(path))
        except KeyError:
            continue
        chunks: list[str] = []
        for paragraph in root.iter(_q("p")):
            parts: list[str] = []
            for node in paragraph.iter():
                if node.tag == _q("t") and node.text:
                    parts.append(node.text)
                elif node.tag == _q("instrText") and node.text and "PAGE" in node.text:
                    parts.append("{page}")
            text = "".join(parts).strip()
            if text:
                chunks.append(text)
        texts[target] = " • ".join(chunks)
    return texts


# ---------------------------------------------------------------------------
# Text shaping and wrapping
# ---------------------------------------------------------------------------


def _shape(text: str, rtl: bool) -> str:
    """Shape and reorder one *visual line* for Pillow (no ``raqm`` in DejaVu).

    ``python-bidi`` is given the whole logical line plus its base direction, so
    mixed content (``دوز 500 mg``, ``Gamas Bot — صفحه ۳``, URLs, formulas) is
    reordered exactly as a bidi-aware layout engine would — shaping Latin runs
    separately and concatenating them is what garbles such lines.
    """
    if not text or not _has_rtl(text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(
            arabic_reshaper.reshape(text), base_dir="R" if rtl else "L"
        )
    except Exception:  # pragma: no cover - shaping is best-effort
        return text


def _has_rtl(text: str) -> bool:
    return any("\u0590" <= char <= "\u08ff" for char in text)


def _is_rtl(text: str) -> bool:
    """Paragraph base direction: right-to-left unless Latin dominates."""
    if not _has_rtl(text):
        return False
    rtl_chars = sum(1 for char in text if "\u0590" <= char <= "\u08ff")
    return rtl_chars * 2 >= max(len(text.replace(" ", "")), 1)


def _wrap(text: str, face: ImageFont.FreeTypeFont, width: int, rtl: bool) -> list[str]:
    """Greedy word wrapping measured on the *shaped* line.

    Pillow (without ``raqm``) can only draw the visually reordered string, so
    wrapping must measure that same string; measuring the logical text wraps
    Persian lines differently from how they are painted and overflows the box.
    """
    lines: list[str] = []
    for hard_line in text.split("\n"):
        words = hard_line.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and face.getlength(_shape(candidate, rtl)) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Layout and painting
# ---------------------------------------------------------------------------


class Page:
    def __init__(self, size: tuple[int, int], background=(255, 255, 255)):
        self.image = Image.new("RGB", size, background)
        self.draw = ImageDraw.Draw(self.image)
        self.blocks = 0
        self.last_block = None
        self.content_bottom = 0

    def save(self, path: Path) -> None:
        self.image.save(path)


def _paragraph_style(block: dict) -> dict:
    """Effective font size / bold / colour / spacing of one paragraph."""
    sizes = [run["size"] for run in block["runs"] if run["size"]]
    size = block["style_size"] or (sum(sizes) / len(sizes) if sizes else 10.5)
    bold = bool(block["style_bold"] or any(run["bold"] for run in block["runs"]))
    color = block["style_color"] or (block["runs"][0]["color"] if block["runs"] else (0, 0, 0))
    before = block["spacing"]["before"] or block["style_space_before"]
    after = block["spacing"]["after"] or block["style_space_after"]
    return {"size": size, "bold": bold, "color": color, "before": before, "after": after}


def _layout_paragraph(block: dict, geometry: dict) -> dict:
    """Measure one paragraph (wrapping included) without drawing it.

    Pagination must know the real height before a block is placed; otherwise a
    multi-line paragraph is drawn past the bottom margin and the page geometry
    lies. The measurement is cached on the block so drawing does not repeat it.
    """
    style = _paragraph_style(block)
    left = geometry["left"] + int(block["indent"]["right"] * TWIP)
    right = geometry["right"] - int(block["indent"]["left"] * TWIP)
    width = max(right - left, 40)
    rtl = _is_rtl(block["text"])
    face = font("sans", style["bold"], style["size"])
    lines = _wrap(block["text"], face, width, rtl) if block["text"] else []
    line_height = int(round(style["size"] * DPI / 72.0 * 1.45))
    layout = {
        "style": style,
        "left": left,
        "right": right,
        "width": width,
        "rtl": rtl,
        "face": face,
        "lines": lines,
        "line_height": line_height,
        "height": line_height * len(lines),
        "before": int(round(style["before"] * TWIP)),
        "after": int(round(style["after"] * TWIP)),
    }
    block["_layout"] = layout
    return layout


def _measure_paragraph(block: dict, geometry: dict) -> int:
    layout = _layout_paragraph(block, geometry)
    return layout["before"] + layout["height"] + layout["after"]


def _draw_paragraph(page: Page, block: dict, cursor: int, geometry: dict) -> int:
    if not block["text"] and not block["bordered"] and not block["shade"]:
        # an empty spacer paragraph still consumes its line height
        style = _paragraph_style(block)
        height = style["size"] * DPI / 72.0 * 1.2
        return cursor + int(round(height))

    layout = block.get("_layout") or _layout_paragraph(block, geometry)
    style = layout["style"]
    left, right, width = layout["left"], layout["right"], layout["width"]
    rtl, face, lines = layout["rtl"], layout["face"], layout["lines"]
    line_height, total_height = layout["line_height"], layout["height"]

    cursor += layout["before"]
    if block["shade"]:
        padding = 4
        page.draw.rectangle(
            [left - padding, cursor - padding, right + padding, cursor + total_height + padding],
            fill=block["shade"],
        )
    if block["bordered"] and block["text"]:
        page.draw.rectangle(
            [left - 4, cursor - 2, right + 4, cursor + total_height + 2],
            outline=(200, 200, 200),
        )

    align = block["align"] or ("right" if rtl else "left")
    for index, line in enumerate(lines):
        draw_text = _shape(line, rtl)
        text_width = face.getlength(draw_text)
        if align in ("center",):
            x = left + (width - text_width) / 2
        elif align in ("right", "end", "both"):
            x = right - text_width
        else:
            x = left
        page.draw.text((x, cursor + index * line_height), draw_text, font=face, fill=style["color"])

    page.last_block = "heading" if block["style"].startswith(("Heading", "Title")) else (
        "text" if block["text"] else "empty"
    )
    page.blocks += 1
    cursor += total_height + layout["after"]
    page.content_bottom = cursor
    return cursor


def _first_line_height(block: dict, geometry: dict) -> int:
    """Height of the first line / first row of the block after a heading."""
    if block["kind"] == "p":
        layout = _layout_paragraph(block, geometry)
        return layout["line_height"]
    if not block["rows"]:
        return 0
    first_row = block["rows"][0]
    height = 1
    for cell in first_row:
        for paragraph in cell["paragraphs"]:
            height = max(height, int(_paragraph_style(paragraph)["size"] * DPI / 72.0 * 1.4))
    return height + 10


def _measure_table(block: dict, geometry: dict) -> int:
    """Approximate height of a table: one line per cell paragraph plus padding."""
    rows = block["rows"]
    if not rows:
        return 0
    total = 0
    for row in rows:
        tallest = 1
        for cell in row:
            lines = 0
            for paragraph in cell["paragraphs"]:
                style = _paragraph_style(paragraph)
                size = style["size"]
                lines += max(1, int(len(paragraph["text"]) / 45) + 1) if paragraph["text"] else 1
                paragraph["_line_height"] = int(size * DPI / 72.0 * 1.4)
            cell_height = sum(p.get("_line_height", 14) * max(1, 1) for p in cell["paragraphs"])
            tallest = max(tallest, cell_height + 10)
        total += tallest + 2
    return total + 8


def _draw_table(page: Page, block: dict, cursor: int, geometry: dict) -> int:
    rows = block["rows"]
    if not rows:
        return cursor
    width = geometry["right"] - geometry["left"]
    columns = max(len(row) for row in rows)
    widths = [value for value in block["widths"] if value][:columns]
    if len(widths) < columns:
        widths = [width // columns] * columns
    scale = width / sum(widths)
    column_px = [int(value * scale) for value in widths]
    padding = 5
    row_height = 0
    for row_index, row in enumerate(rows):
        cells_heights = []
        row_x = geometry["right"]
        for column_index, cell in enumerate(row):
            cell_width = column_px[min(column_index, len(column_px) - 1)]
            fill = cell["fill"] or (block["header_fill"] if row_index == 0 else None)
            if fill:
                page.draw.rectangle(
                    [row_x - cell_width, cursor, row_x, cursor + 18],
                    fill=fill,
                )
            y = cursor + padding
            for paragraph in cell["paragraphs"]:
                style = _paragraph_style(paragraph)
                if not paragraph["text"]:
                    y += int(style["size"] * DPI / 72.0)
                    continue
                rtl = _is_rtl(paragraph["text"])
                face = font("sans", style["bold"], style["size"])
                lines = _wrap(paragraph["text"], face, cell_width - 2 * padding, rtl)
                for line in lines:
                    draw_text = _shape(line, rtl)
                    text_width = face.getlength(draw_text)
                    x = row_x - cell_width + padding
                    if style["bold"] or (paragraph["align"] == "center"):
                        x = row_x - cell_width + (cell_width - text_width) / 2
                    else:
                        x = row_x - cell_width + cell_width - padding - text_width
                    page.draw.text((x, y), draw_text, font=face, fill=style["color"])
                    y += int(style["size"] * DPI / 72.0 * 1.4)
            cells_heights.append(y - cursor + padding)
            row_x -= cell_width
        row_height = max(cells_heights or [30])
        row_x = geometry["right"]
        for column_index in range(len(row)):
            cell_width = column_px[min(column_index, len(column_px) - 1)]
            page.draw.rectangle(
                [row_x - cell_width, cursor, row_x, cursor + row_height],
                outline=(190, 190, 190),
            )
            row_x -= cell_width
        cursor += row_height
        if row_index == 0 and len(rows) > 1:
            cursor += 2
    page.blocks += 1
    page.last_block = "table"
    page.content_bottom = cursor + 8
    return cursor + 8


def _draw_page_border(page: Page, page_size: tuple[int, int], borders: dict) -> None:
    if not borders:
        return
    edges = borders["edges"]
    if not edges:
        return
    space = min(edge["space"] for edge in edges.values())
    width = page_size[0] - 2 * int(space * DPI / 72.0)
    height = page_size[1] - 2 * int(space * DPI / 72.0)
    box = [
        int(space * DPI / 72.0),
        int(space * DPI / 72.0),
        int(space * DPI / 72.0) + width,
        int(space * DPI / 72.0) + height,
    ]
    for edge in edges.values():
        color = edge["color"]
        line_width = max(int(round(edge["size"] / 8.0 * DPI / 72.0)), 1)
        if edge["val"] == "double":
            offset = line_width + 1
            page.draw.rectangle(
                [box[0] + offset, box[1] + offset, box[2] - offset, box[3] - offset],
                outline=color,
                width=line_width,
            )
        page.draw.rectangle(box, outline=color, width=line_width)
        break


def _draw_header_footer(
    page: Page, geometry: dict, page_size: tuple[int, int], section: dict, page_number: int
) -> None:
    face = font("sans", False, 9)
    if section.get("header"):
        text = _shape(section["header"], True)
        width = face.getlength(text)
        page.draw.text(
            ((page_size[0] - width) / 2, 18), text, font=face, fill=(120, 120, 120)
        )
    if section.get("footer"):
        text = section["footer"].replace("{page}", str(page_number))
        shaped = _shape(text, _is_rtl(text))
        width = face.getlength(shaped)
        page.draw.text(
            ((page_size[0] - width) / 2, page_size[1] - 30),
            shaped,
            font=face,
            fill=(120, 120, 120),
        )
        page.draw.line(
            [(geometry["left"], page_size[1] - 38), (geometry["right"], page_size[1] - 38)],
            fill=(210, 210, 210),
        )


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------


def _relationships(zf: zipfile.ZipFile) -> dict[str, str]:
    try:
        root = etree.fromstring(zf.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}
    return {
        rel.get("Id"): rel.get("Target")
        for rel in root
        if rel.get("Target") and not rel.get("TargetMode") == "External"
    }


def render(
    path: Path, out_dir: Path | None = None, sheet: Path | None = None
) -> dict:
    with zipfile.ZipFile(path) as zf:
        document_xml = zf.read("word/document.xml")
        try:
            styles_xml = zf.read("word/styles.xml")
        except KeyError:
            styles_xml = b""
        relationships = _relationships(zf)
        relationship_texts = _header_footer_texts(zf, relationships)
    styles = _styles_from(styles_xml)
    root = etree.fromstring(document_xml)
    body = root.find(_q("body"))

    blocks: list[dict] = []
    for child in body:
        if child.tag == _q("p"):
            blocks.append(_paragraph(child, styles))
        elif child.tag == _q("tbl"):
            blocks.append(_table(child, styles))
    body_sectpr = body.find(_q("sectPr"))

    # Split into sections: a paragraph carrying a sectPr *ends* its section.
    sections: list[dict] = []
    current: list[dict] = []
    for block in blocks:
        current.append(block)
        if block["kind"] == "p" and block["has_sectpr"]:
            sections.append(
                {
                    "blocks": current,
                    "props": _section_props(
                        block["sectpr"], relationships, relationship_texts
                    ),
                }
            )
            current = []
    sections.append(
        {
            "blocks": current,
            "props": _section_props(body_sectpr, relationships, relationship_texts),
        }
    )

    pages: list[dict] = []
    warnings: list[str] = []
    page_number = 0  # the number Word prints (restarts per section)
    page_index = 0  # the physical sheet of paper (never restarts)
    for section in sections:
        props = section["props"]
        size = (
            int(props["page"]["w"] * TWIP),
            int(props["page"]["h"] * TWIP),
        )
        margins = props["margins"]
        geometry = {
            "left": int(margins["left"] * TWIP),
            "right": size[0] - int(margins["right"] * TWIP),
            "top": int(margins["top"] * TWIP),
            "bottom": size[1] - int(margins["bottom"] * TWIP),
        }
        cursor = geometry["top"]
        page = Page(size)
        if props.get("page_number_start"):
            page_number = props["page_number_start"] - 1
        page_number += 1
        page_index += 1
        section_meta = section["props"].get("header_footer", {"header": "", "footer": ""})

        for block in section["blocks"]:
            if block["kind"] == "p" and block["page_break"] and page.blocks:
                _draw_header_footer(page, geometry, size, section_meta, page_number)
                _draw_page_border(page, size, props["borders"])
                pages.append(_page_facts(page, page_number, page_index))
                if out_dir:
                    page.save(out_dir / f"page-{page_index:02d}.png")
                page = Page(size)
                page_number += 1
                page_index += 1
                cursor = geometry["top"]
            height = (
                _measure_paragraph(block, geometry)
                if block["kind"] == "p"
                else _measure_table(block, geometry)
            )
            # keepNext: a heading must not be the last thing on a page, so the
            # space it needs is the heading *plus* the first line after it.
            if block["kind"] == "p" and block["keep_next"]:
                # Word's keepNext keeps this paragraph with the *first line* (or
                # first table row) after it. Modelling that here is what makes
                # the warning below trustworthy.
                index = section["blocks"].index(block)
                following = section["blocks"][index + 1 : index + 2]
                if following:
                    height += _first_line_height(following[0], geometry)
            if cursor + height > geometry["bottom"] and page.blocks:
                # "Orphan heading" means: the page ends with a heading *and* not
                # even the first line of what follows would have fitted next to
                # it. A break inside the following paragraph is normal Word
                # behaviour (keepNext keeps the heading with its first line).
                if page.last_block == "heading":
                    first_line = _first_line_height(block, geometry)
                    if cursor + first_line > geometry["bottom"]:
                        warnings.append(
                            f"page {page_number}: heading left alone at the bottom of the page"
                        )
                _draw_header_footer(page, geometry, size, section_meta, page_number)
                _draw_page_border(page, size, props["borders"])
                pages.append(_page_facts(page, page_number, page_index))
                if out_dir:
                    page.save(out_dir / f"page-{page_index:02d}.png")
                page = Page(size)
                page_number += 1
                page_index += 1
                cursor = geometry["top"]
            if block["kind"] == "p":
                cursor = _draw_paragraph(page, block, cursor, geometry)
            else:
                cursor = _draw_table(page, block, cursor, geometry)
            if cursor > geometry["bottom"]:
                warnings.append(
                    f"page {page_number}: content overflows the bottom margin"
                )
                cursor = geometry["bottom"]

        _draw_header_footer(page, geometry, size, section_meta, page_number)
        _draw_page_border(page, size, props["borders"])
        if page.blocks == 0:
            warnings.append(f"page {page_index} (printed {page_number}): blank page")
        pages.append(_page_facts(page, page_number, page_index))
        if out_dir:
            page.save(out_dir / f"page-{page_index:02d}.png")
    facts = {
        "source": str(path),
        "pages": pages,
        "sections": len(sections),
        "page_size_px": [int(FALLBACK_PAGE["w"] * TWIP), int(FALLBACK_PAGE["h"] * TWIP)],
        "warnings": warnings,
        "ok": not warnings,
    }
    if sheet and pages:
        _contact_sheet(path, out_dir, sheet)
    return facts


def _page_facts(page: Page, page_number: int, page_index: int) -> dict:
    gray = page.image.convert("L")
    histogram = gray.histogram()
    dark = sum(histogram[:200])
    return {
        "page": page_number,
        "index": page_index,
        # True only for the first page of a section that restarts at ۱
        "numbering_restarted": page_number == 1 and page_index > 1,
        "blocks": page.blocks,
        "ink_ratio": round(dark / max(sum(histogram), 1), 4),
        "content_bottom": page.content_bottom,
    }


def _contact_sheet(source: Path, out_dir: Path | None, target: Path) -> None:
    files = sorted((out_dir or target.parent).glob("page-*.png"))
    if not files:
        return
    thumbs = [Image.open(file).convert("RGB") for file in files]
    thumb_width = 320
    scaled = [
        image.resize((thumb_width, int(image.height * thumb_width / image.width)))
        for image in thumbs
    ]
    columns = min(4, len(scaled))
    rows = (len(scaled) + columns - 1) // columns
    gap = 10
    cell_h = max(image.height for image in scaled)
    sheet = Image.new(
        "RGB",
        (columns * thumb_width + gap * (columns + 1), rows * cell_h + gap * (rows + 1)),
        (230, 230, 230),
    )
    for index, image in enumerate(scaled):
        row, column = divmod(index, columns)
        sheet.paste(
            image,
            (gap + column * (thumb_width + gap), gap + row * (cell_h + gap)),
        )
    sheet.save(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--out", type=Path, help="directory for page-XX.png images")
    parser.add_argument("--sheet", type=Path, help="write a contact sheet of all pages")
    parser.add_argument("--json", type=Path, help="write the layout report as JSON")
    args = parser.parse_args(argv)

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    facts = render(args.docx, args.out, args.sheet)
    if args.json:
        args.json.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"pages={len(facts['pages'])} sections={facts['sections']} "
        f"warnings={len(facts['warnings'])}"
    )
    for warning in facts["warnings"]:
        print(f"  ! {warning}")
    for page in facts["pages"]:
        print(
            f"  sheet {page['index']:>2} (printed {page['page']:>2}): "
            f"blocks={page['blocks']:>3} ink={page['ink_ratio']:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
