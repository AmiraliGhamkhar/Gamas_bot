"""Build the static Gamas booklet artwork.

Run once, commit the PNGs, never at booklet time:

    python -m scripts.build_booklet_assets

The artwork is drawn with primitives (bezier-sampled curves, arcs, polygons) at
high resolution and downsampled, which gives clean vector-like edges without an
SVG rasterizer or a new dependency. Every file is written to the path and size
declared in ``assets/booklet/manifest.json``, so the manifest is the contract:
re-running the script verifies it.

Design rules enforced here:

* no text of any kind — all typography is rendered by the document renderers,
  because rasterized Persian text is exactly what distorts;
* the Gamas motif: cream paper, deep navy frame, teal/cyan organic curves, an
  ECG waveform, an open book, and one topic-specific scientific object;
* deterministic output: fixed geometry, no randomness, no timestamps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import math

from PIL import Image, ImageDraw

from gamas_bot.document.theme import THEME

P = THEME.palette
ROOT = Path(__file__).resolve().parent.parent / "assets" / "booklet"

#: Covers are drawn at 2x, icons at 4x, then downsampled (anti-aliasing).
COVER_SUPERSAMPLE = 2
ICON_SUPERSAMPLE = 4

Color = tuple[int, int, int, int]


def rgba(hex_value: str, alpha: int = 255) -> Color:
    value = hex_value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha)


NAVY = rgba(P.navy_900)
NAVY_DEEP = rgba(P.navy_950)
NAVY_SOFT = rgba(P.navy_800)
TEAL = rgba(P.teal_600)
TEAL_DEEP = rgba(P.teal_700)
TEAL_LIGHT = rgba(P.teal_400)
CYAN = rgba(P.cyan_500)
CREAM = rgba(P.cream_50)
CREAM_100 = rgba(P.cream_100)
PAPER = rgba(P.paper)
MUTED = rgba(P.muted)
WHITE = rgba("#FFFFFF")


def bezier(points: list[tuple[float, float]], steps: int = 48) -> list[tuple[float, float]]:
    """Sample a chain of cubic bezier segments (first point + 3n control points)."""
    if len(points) < 4:
        return points
    out: list[tuple[float, float]] = []
    for index in range(0, len(points) - 3, 3):
        p0, p1, p2, p3 = points[index : index + 4]
        for step in range(steps + 1):
            t = step / steps
            u = 1 - t
            out.append(
                (
                    u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],
                )
            )
    return out


def stroke(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], color: Color,
           width: int, closed: bool = False) -> None:
    if len(points) < 2:
        return
    draw.line(points + ([points[0]] if closed else []), fill=color, width=width, joint="curve")
    radius = max(1, width // 2)
    for x, y in points:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)


def dot_grid(draw: ImageDraw.ImageDraw, x: float, y: float, columns: int, rows: int,
             gap: float, radius: float, color: Color) -> None:
    for row in range(rows):
        for column in range(columns):
            cx = x + column * gap
            cy = y + row * gap
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)


def ecg_points(left: float, right: float, baseline: float, amplitude: float) -> list[tuple[float, float]]:
    """One repeated ECG complex, tiled across ``left..right``."""
    pattern = (
        (0.00, 0.00), (0.10, 0.00), (0.14, 0.06), (0.17, -0.04), (0.19, 0.38),
        (0.21, -0.62), (0.23, 0.10), (0.26, 0.20), (0.30, 0.00), (0.42, 0.00),
        (0.46, -0.10), (0.50, 0.00),
    )
    span = right - left
    period = span / 3.2
    points: list[tuple[float, float]] = []
    x = left
    while x < right:
        for offset, height in pattern:
            px = x + offset * period
            if px > right:
                break
            points.append((px, baseline - height * amplitude))
        x += period
    return points


def rounded_frame(draw: ImageDraw.ImageDraw, width: int, height: int, inset: float,
                  radius: float, color: Color, line: int) -> None:
    draw.rounded_rectangle(
        (inset, inset, width - inset, height - inset),
        radius=radius, outline=color, width=line,
    )


# ---------------------------------------------------------------------------
# Topic emblems (unit-space glyphs, reusable at any size)
# ---------------------------------------------------------------------------


def _glyph_book(draw, cx, cy, size, ink, accent, line):
    half = size * 0.5
    spine = size * 0.05
    top = cy - size * 0.30
    bottom = cy + size * 0.34
    for direction in (-1, 1):
        page = [
            (cx + direction * spine, top + size * 0.04),
            (cx + direction * half * 0.92, top),
            (cx + direction * half * 0.92, bottom - size * 0.06),
            (cx + direction * spine, bottom),
        ]
        draw.polygon(page, fill=CREAM, outline=ink, width=line)
        for step in range(3):
            y = top + size * (0.18 + step * 0.10)
            draw.line(
                [(cx + direction * size * 0.12, y), (cx + direction * half * 0.78, y - size * 0.03)],
                fill=accent, width=max(2, line // 2),
            )
    draw.line([(cx, top + size * 0.04), (cx, bottom)], fill=ink, width=line)


def _glyph_cross(draw, cx, cy, size, ink, accent, line):
    arm = size * 0.17
    length = size * 0.46
    draw.rounded_rectangle(
        (cx - arm, cy - length, cx + arm, cy + length), radius=arm * 0.6, fill=ink
    )
    draw.rounded_rectangle(
        (cx - length, cy - arm, cx + length, cy + arm), radius=arm * 0.6, fill=ink
    )
    draw.ellipse((cx - arm * 0.55, cy - arm * 0.55, cx + arm * 0.55, cy + arm * 0.55), fill=accent)


def _glyph_capsule(draw, cx, cy, size, ink, accent, line):
    """A capsule at 45 degrees, drawn on a square plate so rotation never clips."""
    plate_size = int(size * 0.96)
    plate = Image.new("RGBA", (plate_size, plate_size), (0, 0, 0, 0))
    plate_draw = ImageDraw.Draw(plate)
    length = plate_size * 0.84
    width = plate_size * 0.34
    left = (plate_size - length) / 2
    top = (plate_size - width) / 2
    box = (left, top, left + length, top + width)
    plate_draw.rounded_rectangle(box, radius=int(width / 2), fill=CREAM,
                                 outline=ink, width=max(3, int(line * 1.2)))
    split = left + length / 2
    plate_draw.rounded_rectangle((split, top + 2, left + length - 2, top + width - 2),
                                 radius=int(width / 2), fill=rgba(P.teal_400, 150))
    plate_draw.line([(split, top + 2), (split, top + width - 2)], fill=ink,
                    width=max(2, int(line)))
    rotated = plate.rotate(42, resample=Image.BICUBIC, expand=False)
    draw._image.paste(rotated, (int(cx - plate_size / 2), int(cy - plate_size / 2)), rotated)  # type: ignore[attr-defined]


def _glyph_bone(draw, cx, cy, size, ink, accent, line):
    shaft_half = size * 0.24
    lobe = size * 0.15
    draw.rounded_rectangle(
        (cx - shaft_half, cy - size * 0.075, cx + shaft_half, cy + size * 0.075),
        radius=size * 0.06, fill=ink,
    )
    for direction in (-1, 1):
        x = cx + direction * shaft_half
        for vertical in (-1, 1):
            y = cy + vertical * size * 0.12
            draw.ellipse((x - lobe, y - lobe, x + lobe, y + lobe), fill=ink)
        draw.ellipse(
            (x - lobe * 0.42, cy - lobe * 0.42, x + lobe * 0.42, cy + lobe * 0.42), fill=accent
        )


def _glyph_heart(draw, cx, cy, size, ink, accent, line):
    upper = cy - size * 0.06
    points = bezier([
        (cx, cy + size * 0.42),
        (cx - size * 0.62, cy - size * 0.02),
        (cx - size * 0.34, upper - size * 0.34),
        (cx, upper),
        (cx + size * 0.34, upper - size * 0.34),
        (cx + size * 0.62, cy - size * 0.02),
        (cx, cy + size * 0.42),
    ])
    draw.polygon(points, fill=rgba(P.warn_fill), outline=ink, width=line)
    inner = bezier([
        (cx, cy + size * 0.26),
        (cx - size * 0.40, cy + size * 0.02),
        (cx - size * 0.20, cy - size * 0.16),
        (cx, cy - size * 0.02),
        (cx + size * 0.20, cy - size * 0.16),
        (cx + size * 0.40, cy + size * 0.02),
        (cx, cy + size * 0.26),
    ])
    stroke(draw, inner, accent, max(2, line // 2))


def _glyph_chart(draw, cx, cy, size, ink, accent, line):
    base = cy + size * 0.34
    height = size * 0.46
    width = size * 0.14
    for index, factor in enumerate((0.45, 0.72, 1.0)):
        x = cx - size * 0.28 + index * size * 0.22
        top = base - height * factor
        draw.rounded_rectangle((x, top, x + width, base), radius=size * 0.03,
                               fill=(NAVY_SOFT if index < 2 else TEAL), outline=ink, width=max(2, line // 2))
    stroke(draw, [(cx - size * 0.34, cy + size * 0.08), (cx - size * 0.08, cy - size * 0.10),
                  (cx + size * 0.10, cy + size * 0.02), (cx + size * 0.34, cy - size * 0.30)],
           accent, max(2, line))


def _glyph_flask(draw, cx, cy, size, ink, accent, line):
    neck = size * 0.09
    top = cy - size * 0.40
    shoulder = cy - size * 0.04
    body_left = cx - size * 0.30
    body_right = cx + size * 0.30
    bottom = cy + size * 0.38
    outline = [
        (cx - neck, top), (cx + neck, top), (cx + neck, shoulder),
        (cx + size * 0.26, bottom - size * 0.04),
        (cx + size * 0.16, bottom), (cx - size * 0.16, bottom),
        (cx - size * 0.26, bottom - size * 0.04), (cx - neck, shoulder),
    ]
    draw.polygon(outline, fill=CREAM, outline=ink, width=line)
    fill_level = cy + size * 0.12
    draw.polygon(
        [(body_left + size * 0.03, min(fill_level + size * 0.08, bottom - size * 0.05)),
         (body_right - size * 0.03, min(fill_level + size * 0.08, bottom - size * 0.05)),
         (body_right - size * 0.03, bottom - size * 0.06),
         (cx + size * 0.10, bottom - size * 0.01),
         (cx - size * 0.10, bottom - size * 0.01),
         (body_left + size * 0.03, bottom - size * 0.06)],
        fill=TEAL_LIGHT,
    )
    draw.line([(cx - neck, top + size * 0.02), (cx + neck, top + size * 0.02)], fill=ink, width=line)
    for step in range(2):
        draw.ellipse((cx - size * 0.06 + step * size * 0.07, cy + size * 0.22,
                      cx - size * 0.01 + step * size * 0.07, cy + size * 0.27), fill=CREAM)


def _glyph_network(draw, cx, cy, size, ink, accent, line):
    nodes = [
        (cx, cy - size * 0.30),
        (cx - size * 0.32, cy + size * 0.04),
        (cx + size * 0.32, cy + size * 0.04),
        (cx, cy + size * 0.32),
    ]
    for index, node in enumerate(nodes):
        for other in nodes[index + 1 :]:
            draw.line([node, other], fill=rgba(P.teal_400, 170), width=max(2, line - 1))
    for index, (x, y) in enumerate(nodes):
        radius = size * (0.11 if index == 0 else 0.085)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     fill=(NAVY if index == 0 else CREAM), outline=ink, width=max(2, line))
        if index == 0:
            draw.ellipse((x - radius * 0.34, y - radius * 0.34, x + radius * 0.34, y + radius * 0.34),
                         fill=CYAN)


GLYPHS: dict[str, Callable] = {
    "book": _glyph_book,
    "cross": _glyph_cross,
    "capsule": _glyph_capsule,
    "bone": _glyph_bone,
    "heart": _glyph_heart,
    "chart": _glyph_chart,
    "flask": _glyph_flask,
    "network": _glyph_network,
}


# ---------------------------------------------------------------------------
# Cover artwork
# ---------------------------------------------------------------------------


def _leaf(draw: ImageDraw.ImageDraw, cx: float, cy: float, *, length: float, width: float,
          angle: float, fill: Color, outline: Color, edge: int) -> None:
    """A teardrop leaf polygon, rotated about its base."""
    points: list[tuple[float, float]] = []
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    for step in range(13):
        t = step / 12
        local_x = t * length
        local_y = -width * math.sin(math.pi * t) * 0.5
        points.append((cx + local_x * cos_a - local_y * sin_a,
                       cy + local_x * sin_a + local_y * cos_a))
    for step in range(13):
        t = 1 - step / 12
        local_x = t * length
        local_y = width * math.sin(math.pi * t) * 0.5
        points.append((cx + local_x * cos_a - local_y * sin_a,
                       cy + local_x * sin_a + local_y * cos_a))
    draw.polygon(points, fill=fill, outline=outline, width=edge)


def draw_cover(width: int, height: int, glyph: str) -> Image.Image:
    image = Image.new("RGBA", (width, height), CREAM)
    draw = ImageDraw.Draw(image)
    draw._image = image  # type: ignore[attr-defined]  # used by the capsule glyph

    # Paper: a warm cream field with a faint band, so the art sits on paper.
    draw.rectangle((0, 0, width, height), fill=CREAM)
    draw.rectangle((0, height * 0.62, width, height), fill=rgba(P.cream_100, 150))

    # Organic teal curves sweeping from the lower left, layered.
    curve = bezier([
        (-width * 0.05, height * 0.94),
        (width * 0.30, height * 0.58),
        (width * 0.52, height * 1.05),
        (width * 1.05, height * 0.52),
    ])
    band = curve + [(width * 1.05, height * 1.02), (-width * 0.05, height * 1.02)]
    draw.polygon(band, fill=rgba(P.teal_400, 46))
    stroke(draw, curve, rgba(P.teal_500, 210), max(4, height // 150))

    inner_curve = bezier([
        (-width * 0.05, height * 0.82),
        (width * 0.34, height * 0.46),
        (width * 0.56, height * 0.92),
        (width * 1.05, height * 0.40),
    ])
    stroke(draw, inner_curve, rgba(P.cyan_500, 160), max(3, height // 240))

    # Graduation-cap silhouette, top right: academic, drawn flat.
    cap_x, cap_y, cap_size = width * 0.845, height * 0.175, height * 0.115
    draw.polygon(
        [(cap_x, cap_y - cap_size * 0.55), (cap_x + cap_size, cap_y),
         (cap_x, cap_y + cap_size * 0.55), (cap_x - cap_size, cap_y)],
        fill=NAVY, outline=NAVY_DEEP,
    )
    draw.polygon(
        [(cap_x - cap_size * 0.55, cap_y + cap_size * 0.10),
         (cap_x + cap_size * 0.55, cap_y + cap_size * 0.10),
         (cap_x + cap_size * 0.42, cap_y + cap_size * 0.72),
         (cap_x - cap_size * 0.42, cap_y + cap_size * 0.72)],
        fill=NAVY_SOFT, outline=NAVY,
    )
    draw.line([(cap_x + cap_size * 0.5, cap_y + cap_size * 0.2),
               (cap_x + cap_size * 0.5, cap_y + cap_size * 0.95)], fill=CYAN,
              width=max(3, height // 220))
    draw.ellipse(
        (cap_x + cap_size * 0.42, cap_y + cap_size * 0.9,
         cap_x + cap_size * 0.58, cap_y + cap_size * 1.06),
        fill=CYAN,
    )

    # Brain / knowledge motif: a cloud-silhouette brain in thin line art.
    brain_x, brain_y, brain_r = width * 0.155, height * 0.245, height * 0.105
    lobes = (
        (-0.42, -0.10), (-0.24, -0.44), (0.02, -0.56), (0.30, -0.36),
        (0.44, -0.02), (0.28, 0.30), (-0.02, 0.44), (-0.30, 0.28),
    )
    for scale, colour in ((0.55, rgba(P.teal_600, 215)), (0.46, CREAM)):
        for dx, dy in lobes:
            cx = brain_x + dx * brain_r
            cy = brain_y + dy * brain_r
            radius = brain_r * scale
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=colour)
    # Gyri: short folds that stay well inside the silhouette.
    for dx, dy, flip in ((-0.18, -0.22, 1), (0.20, -0.10, -1), (-0.02, 0.20, 1)):
        cx = brain_x + dx * brain_r
        cy = brain_y + dy * brain_r
        fold = brain_r * 0.19
        draw.arc((cx - fold, cy - fold, cx + fold, cy + fold),
                 start=200 if flip > 0 else 20, end=340 if flip > 0 else 160,
                 fill=rgba(P.teal_600, 200), width=max(3, height // 300))
    draw.line([(brain_x, brain_y - brain_r * 0.34), (brain_x, brain_y + brain_r * 0.34)],
              fill=rgba(P.teal_600, 190), width=max(2, height // 320))

    # The ECG waveform: the Gamas motif, drawn as a real path.
    baseline = height * 0.40
    stroke(draw, ecg_points(width * 0.06, width * 0.94, baseline, height * 0.20),
           rgba(P.navy_900, 230), max(4, height // 130))
    stroke(draw, ecg_points(width * 0.06, width * 0.94, baseline + height * 0.045, height * 0.09),
           rgba(P.cyan_500, 120), max(2, height // 280))

    # Open book: the anchor object, centre-left, with layered pages.
    book_cx, book_cy = width * 0.30, height * 0.635
    book_w, book_h = width * 0.36, height * 0.34
    draw.ellipse(
        (book_cx - book_w * 0.62, book_cy - book_h * 0.58,
         book_cx + book_w * 0.62, book_cy + book_h * 0.66),
        fill=rgba(P.teal_400, 34),
    )
    glyph_size = min(book_w, book_h) * 0.92
    _glyph_book(draw, book_cx, book_cy, glyph_size, NAVY, TEAL_LIGHT, max(4, height // 150))

    # Floating medical/scientific objects: a molecule and a pen.
    molecule = (width * 0.56, height * 0.27)
    atom = height * 0.026
    links = [
        ((molecule[0] + atom * 0.9, molecule[1] + atom * 0.5), (molecule[0] + atom * 3.0, molecule[1] + atom * 1.6)),
        ((molecule[0] + atom * 0.9, molecule[1] + atom * 0.5), (molecule[0] + atom * 2.6, molecule[1] - atom * 1.4)),
    ]
    for start, end in links:
        draw.line([start, end], fill=rgba(P.navy_800, 190), width=max(3, height // 260))
    for cx, cy, radius, fill in (
        (molecule[0], molecule[1], atom, TEAL),
        (molecule[0] + atom * 3.0, molecule[1] + atom * 1.6, atom * 0.8, CREAM),
        (molecule[0] + atom * 2.6, molecule[1] - atom * 1.4, atom * 0.8, CREAM),
    ):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     fill=fill, outline=NAVY, width=max(2, height // 300))

    # Document motif: a small study card, lower right.
    card_w, card_h = width * 0.085, height * 0.20
    card_x, card_y = width * 0.855, height * 0.64
    draw.rounded_rectangle(
        (card_x, card_y, card_x + card_w, card_y + card_h),
        radius=height * 0.014, fill=CREAM, outline=NAVY, width=max(3, height // 200),
    )
    draw.polygon(
        [(card_x + card_w * 0.68, card_y), (card_x + card_w, card_y + card_h * 0.14),
         (card_x + card_w * 0.68, card_y + card_h * 0.14)],
        fill=rgba(P.cream_100), outline=NAVY,
    )
    for step in range(3):
        line_y = card_y + card_h * (0.34 + step * 0.16)
        draw.line([(card_x + card_w * 0.14, line_y), (card_x + card_w * 0.84, line_y)],
                  fill=rgba(P.teal_600, 190), width=max(2, height // 340))
    draw.line([(card_x + card_w * 0.14, card_y + card_h * 0.82),
               (card_x + card_w * 0.52, card_y + card_h * 0.82)],
              fill=CYAN, width=max(3, height // 280))

    # Topic object on a cream medallion, right of centre.
    medallion = (width * 0.735, height * 0.575, height * 0.155)
    draw.ellipse(
        (medallion[0] - medallion[2], medallion[1] - medallion[2],
         medallion[0] + medallion[2], medallion[1] + medallion[2]),
        fill=rgba(P.cream_50, 240), outline=NAVY, width=max(4, height // 150),
    )
    draw.ellipse(
        (medallion[0] - medallion[2] * 0.86, medallion[1] - medallion[2] * 0.86,
         medallion[0] + medallion[2] * 0.86, medallion[1] + medallion[2] * 0.86),
        outline=rgba(P.cyan_500, 180), width=max(2, height // 320),
    )
    GLYPHS[glyph](draw, medallion[0], medallion[1], medallion[2] * 1.25, NAVY, TEAL, max(4, height // 160))

    # Botanical sprig: organic secondary decoration, lower left.
    sprig = bezier([
        (width * 0.045, height * 0.90),
        (width * 0.055, height * 0.80),
        (width * 0.095, height * 0.76),
        (width * 0.085, height * 0.64),
    ])
    stroke(draw, sprig, rgba(P.teal_700, 190), max(3, height // 260))
    for index, (px, py) in enumerate(sprig[8::14][:4]):
        _leaf(
            draw, px + width * 0.006, py,
            length=height * 0.085, width=height * 0.032,
            angle=-0.9 if index % 2 == 0 else 0.5,
            fill=rgba(P.teal_400, 120), outline=rgba(P.teal_700, 190),
            edge=max(2, height // 360),
        )

    # Dots: restrained geometric counterpoint, top left.
    dot_grid(draw, width * 0.045, height * 0.075, 5, 3, height * 0.05,
             max(2, height * 0.006), rgba(P.navy_800, 90))

    # Depth: a soft shadow under the book.
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.ellipse(
        (book_cx - book_w * 0.52, book_cy + book_h * 0.30,
         book_cx + book_w * 0.52, book_cy + book_h * 0.56),
        fill=rgba(P.navy_950, 40),
    )
    image.alpha_composite(shadow)

    # Structure: the navy frame and its cyan inner hairline.
    rounded_frame(draw, width, height, inset=height * 0.028, radius=height * 0.06,
                  color=NAVY, line=max(5, height // 120))
    rounded_frame(draw, width, height, inset=height * 0.052, radius=height * 0.045,
                  color=rgba(P.cyan_500, 120), line=max(2, height // 400))
    return image


def draw_icon(width: int, height: int, glyph: str) -> Image.Image:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw._image = image  # type: ignore[attr-defined]
    size = min(width, height) * 0.74
    GLYPHS[glyph](draw, width / 2, height / 2, size, NAVY,
                  TEAL_LIGHT, max(6, int(size * 0.045)))
    return image


def draw_ecg_divider(width: int, height: int) -> Image.Image:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    points = ecg_points(width * 0.02, width * 0.98, height * 0.55, height * 0.36)
    stroke(draw, points, rgba(P.teal_500, 210), max(3, height // 26))
    for x in (width * 0.02, width * 0.98):
        draw.ellipse((x - height * 0.09, height * 0.55 - height * 0.09,
                      x + height * 0.09, height * 0.55 + height * 0.09), fill=TEAL_DEEP)
    return image


def draw_monogram(width: int, height: int) -> Image.Image:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = width * 0.03
    draw.rounded_rectangle((pad, pad, width - pad, height - pad), radius=width * 0.22,
                           fill=NAVY, outline=NAVY_DEEP, width=int(width * 0.012))
    inner = width * 0.115
    draw.rounded_rectangle((inner, inner, width - inner, height - inner),
                           radius=width * 0.15, outline=rgba(P.cyan_500, 210),
                           width=max(3, int(width * 0.012)))
    size = width * 0.52
    _glyph_book(draw, width * 0.50, height * 0.47, size, rgba(P.cream_50), CYAN,
                max(4, int(width * 0.020)))
    # A small ECG line rising through the mark.
    points = ecg_points(width * 0.22, width * 0.78, height * 0.74, height * 0.09)
    stroke(draw, points, CYAN, max(3, int(width * 0.014)))
    return image


def save(image: Image.Image, relative: str, size: tuple[int, int]) -> None:
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    image.resize(size, Image.LANCZOS).save(path, "PNG", optimize=True)


def main() -> int:
    from gamas_bot.document.assets import manifest, validate_manifest

    payload = manifest()
    assets = payload.get("assets") or {}
    glyph_for_cover = {
        "hero-general": "book",
        "hero-medical": "cross",
        "hero-pharma": "capsule",
        "hero-anatomy": "bone",
        "hero-nursing": "heart",
        "hero-epidemiology": "chart",
        "hero-chemistry": "flask",
        "hero-data": "network",
    }
    written = 0
    for key, entry in assets.items():
        relative = str(entry["path"])
        width, height = int(entry["width"]), int(entry["height"])
        stem = Path(relative).stem
        if key == "brand/monogram":
            image = draw_monogram(width * 2, height * 2)
        elif key == "decorative/ecg-line":
            image = draw_ecg_divider(width * 2, height * 2)
        elif key.startswith("cover/"):
            image = draw_cover(width * COVER_SUPERSAMPLE, height * COVER_SUPERSAMPLE,
                               glyph_for_cover.get(stem, "book"))
        elif key.startswith("icons/"):
            glyph = stem if stem in GLYPHS else "book"
            image = draw_icon(width * ICON_SUPERSAMPLE, height * ICON_SUPERSAMPLE, glyph)
        else:
            continue
        save(image, relative, (width, height))
        written += 1
        print(f"wrote {relative} ({width}x{height})")

    problems = validate_manifest()
    if problems:
        for problem in problems:
            print("MANIFEST:", problem)
        return 1
    print(f"{written} assets written; manifest valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
