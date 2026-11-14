"""Typography roles and the type scale.

The Persian document face is Vazirmatn (bundled, SIL OFL). Latin technical
terms and formulas use a Latin face, because a Persian face's Latin glyphs are
not always metrically reliable at small sizes; Word resolves that through the
``latin`` font role, ReportLab through a separate registered face.

Sizes live in :mod:`gamas_bot.document.theme`; this module only expresses how a
role maps onto a renderer.
"""

from __future__ import annotations

from dataclasses import dataclass

from .theme import THEME

#: Word's default Latin face (declared in ``DEFAULT_FONT_CONFIG`` too). The
#: booklet never downloads fonts; Tahoma ships with Windows and is the
#: configured fallback for Vazirmatn.
DEFAULT_LATIN_FACE = "Tahoma"

#: Word line-spacing multiple for body prose (Persian needs air between lines).
BODY_LINE_SPACING = THEME.type.body_leading


@dataclass(frozen=True, slots=True)
class FontRoles:
    """The three faces one document resolves to."""

    body: str
    heading: str
    latin: str
    fallback: str

    @property
    def complex_script(self) -> str:
        return self.body


def size(role: str) -> float:
    """Point size for a named typographic role."""
    return float(getattr(THEME.type, role))


def pdf_leading(role: str = "body") -> float:
    """Leading in points for a named role.

    Body prose uses the explicit PDF leading from the scale; every other role
    gets a conventional 1.35 × size, which keeps Persian ascenders/descenders
    clear of the line above without the airiness of the body leading.
    """
    if role == "body":
        return THEME.type.body_pdf_leading
    return round(size(role) * 1.35, 1)


__all__ = [
    "BODY_LINE_SPACING",
    "DEFAULT_LATIN_FACE",
    "FontRoles",
    "pdf_leading",
    "size",
]
