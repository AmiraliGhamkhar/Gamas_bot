"""The Gamas document design system: one source of visual truth.

Every colour, size, spacing step and page measurement used by the booklet
renderers lives here. Renderers (:mod:`gamas_bot.docx_export` and
:mod:`gamas_bot.notes_pdf`) never invent a value: they look it up by semantic
name, which is what keeps the Word file and the PDF the same publication.

The palette is the Gamas identity: deep navy structure, teal and cyan accents,
warm cream paper, ink-black text. Red is reserved for safety-critical material.
Cream ``paper`` is the editorial paper tone from the Gamas website design
record; the navy/teal family is the booklet's own scientific language.

Colour values are ``#RRGGBB`` strings. Word needs ``RRGGBB`` for shading and
ReportLab needs a ``Color``; each renderer converts, nothing else stores colour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEX_PATTERN = re.compile(r"^#[0-9A-F]{6}$")


@dataclass(frozen=True, slots=True)
class Palette:
    """Semantic colour tokens. No renderer may hard-code a hex value."""

    # --- structure: deep navy family -------------------------------------
    navy_950: str
    navy_900: str
    navy_800: str
    navy_700: str
    # --- accent: teal / cyan ---------------------------------------------
    teal_700: str
    teal_600: str
    teal_500: str
    teal_400: str
    cyan_500: str
    cyan_300: str
    # --- paper ------------------------------------------------------------
    cream_50: str
    cream_100: str
    paper: str
    # --- text -------------------------------------------------------------
    ink: str
    body: str
    muted: str
    on_dark: str
    on_accent: str
    # --- lines and neutral fills -----------------------------------------
    line: str
    border: str
    zebra: str
    # --- semantics --------------------------------------------------------
    success: str
    warning: str
    danger: str
    info: str
    # --- component tints (fill + its deeper frame tone) -------------------
    definition_fill: str
    definition_border: str
    example_fill: str
    example_border: str
    note_fill: str
    note_border: str
    warn_fill: str
    warn_border: str
    tip_fill: str
    tip_border: str
    summary_fill: str
    summary_border: str
    key_fill: str
    key_border: str
    formula_fill: str
    formula_border: str
    quote_fill: str
    quote_border: str
    highlight: str
    # --- cover ------------------------------------------------------------
    cover_panel: str
    cover_panel_soft: str
    cover_band: str


@dataclass(frozen=True, slots=True)
class TypeScale:
    """Point sizes and leading. Persian body text stays at a readable 11 pt."""

    body: float
    body_leading: float          # Word line-spacing multiple
    body_pdf_leading: float      # points, for ReportLab
    h1: float
    h2: float
    h3: float
    banner: float
    caption: float
    micro: float
    table: float
    table_head: float
    toc: float
    cover_brand: float
    cover_label: float
    cover_title: float
    cover_meta: float
    cover_quote: float
    stat: float
    eyebrow: float


@dataclass(frozen=True, slots=True)
class Spacing:
    """Vertical rhythm, in points (Word) and convertible to PDF points."""

    hair: float
    xs: float
    sm: float
    md: float
    lg: float
    xl: float
    xxl: float
    section: float
    banner_pad: float
    card_pad: float
    table_cell_x: float


@dataclass(frozen=True, slots=True)
class Geometry:
    """Page and component measurements.

    The PDF margins are pinned by the visual-QA contract: A4, 2.0 cm side and
    bottom margins, 2.2 cm top margin, a 20 pt decorative frame inset. The Word
    document keeps its own (slightly larger) margins for print binding.
    """

    page_width_cm: float
    page_height_cm: float
    docx_margin_top_cm: float
    docx_margin_bottom_cm: float
    docx_margin_side_cm: float
    pdf_margin_lr_cm: float
    pdf_margin_top_cm: float
    pdf_margin_bottom_cm: float
    pdf_frame_inset_pt: float
    header_band_pt: float
    footer_band_pt: float
    content_width_cm: float
    cover_art_width_cm: float
    cover_art_ratio: float
    banner_min_height_cm: float
    rule_pt: float
    hairline_pt: float
    border_pt: float
    border_space_pt: int
    radius_sm_pt: float
    radius_md_pt: float
    emblem_cm: float
    table_width_cm: float


PALETTE = Palette(
    navy_950="#04212E",
    navy_900="#062C3D",
    navy_800="#073B4C",
    navy_700="#0B4658",
    teal_700="#087F8C",
    teal_600="#0C9AA6",
    teal_500="#12A8B5",
    teal_400="#3FC0CB",
    cyan_500="#25C6D6",
    cyan_300="#67DCE5",
    cream_50="#FFF9ED",
    cream_100="#F8F2E6",
    paper="#F2EFE7",
    ink="#102B36",
    body="#1B3942",
    muted="#60747C",
    on_dark="#FFF9ED",
    on_accent="#FFFFFF",
    line="#C7D8DD",
    border="#A9C3CB",
    zebra="#F5FAFA",
    success="#1F8A70",
    warning="#B4762A",
    danger="#A73822",
    info="#0C7C8A",
    definition_fill="#EAF2F4",
    definition_border="#9CBEC7",
    example_fill="#EDF6EF",
    example_border="#A9CDB6",
    note_fill="#E7F4F5",
    note_border="#9FCFD6",
    warn_fill="#FBEDE9",
    warn_border="#DDA79B",
    tip_fill="#FBF1DF",
    tip_border="#D9BC86",
    summary_fill="#E9F3F4",
    summary_border="#A7CBD0",
    key_fill="#EAF4F4",
    key_border="#9BC8CD",
    formula_fill="#F1F5F6",
    formula_border="#C2D2D8",
    quote_fill="#FAF6EC",
    quote_border="#DCCFB1",
    highlight="#FFF1BF",
    cover_panel="#062C3D",
    cover_panel_soft="#0B4658",
    cover_band="#F8F2E6",
)

TYPESCALE = TypeScale(
    body=11.0,
    body_leading=1.55,
    body_pdf_leading=17.0,
    h1=18.0,
    h2=13.5,
    h3=11.8,
    banner=15.5,
    caption=8.5,
    micro=7.8,
    table=10.2,
    table_head=10.2,
    toc=10.5,
    cover_brand=34.0,
    cover_label=10.5,
    cover_title=24.0,
    cover_meta=9.5,
    cover_quote=12.0,
    stat=20.0,
    eyebrow=8.2,
)

SPACING = Spacing(
    hair=1.0,
    xs=2.0,
    sm=4.0,
    md=8.0,
    lg=12.0,
    xl=18.0,
    xxl=26.0,
    section=10.0,
    banner_pad=7.0,
    card_pad=9.0,
    table_cell_x=6.0,
)

GEOMETRY = Geometry(
    page_width_cm=21.0,
    page_height_cm=29.7,
    docx_margin_top_cm=2.4,
    docx_margin_bottom_cm=2.2,
    docx_margin_side_cm=2.2,
    pdf_margin_lr_cm=2.0,
    pdf_margin_top_cm=2.2,
    pdf_margin_bottom_cm=2.0,
    pdf_frame_inset_pt=20.0,
    header_band_pt=42.0,
    footer_band_pt=34.0,
    content_width_cm=16.6,
    cover_art_width_cm=16.0,
    cover_art_ratio=2.19,
    banner_min_height_cm=0.95,
    rule_pt=1.0,
    hairline_pt=0.5,
    border_pt=0.9,
    border_space_pt=24,
    radius_sm_pt=3.0,
    radius_md_pt=7.0,
    emblem_cm=2.4,
    table_width_cm=16.6,
)


@dataclass(frozen=True, slots=True)
class GamasTheme:
    """The complete design system handed to a renderer."""

    name: str
    palette: Palette
    type: TypeScale
    space: Spacing
    geometry: Geometry

    def ooxml(self, value: str) -> str:
        """``#RRGGBB`` → ``RRGGBB`` for ``w:fill``/``w:color``."""
        return value.lstrip("#").upper()

    def validate(self) -> None:
        """Raise ``ValueError`` if a token is not a normalised hex colour."""
        for field_name in self.palette.__slots__:
            value = getattr(self.palette, field_name)
            if not _HEX_PATTERN.match(value):
                raise ValueError(f"palette token {field_name} is not #RRGGBB: {value!r}")


#: The one theme instance. Import this instead of defining colours locally.
THEME = GamasTheme(
    name="Gamas Scientific",
    palette=PALETTE,
    type=TYPESCALE,
    space=SPACING,
    geometry=GEOMETRY,
)
THEME.validate()

__all__ = [
    "GEOMETRY",
    "PALETTE",
    "SPACING",
    "TYPESCALE",
    "GamasTheme",
    "Geometry",
    "Palette",
    "Spacing",
    "THEME",
    "TypeScale",
]
