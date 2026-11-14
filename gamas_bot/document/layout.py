"""Page geometry shared by both renderers.

ReportLab works in points, Word in centimetres and points. This module converts
once, from the single :data:`gamas_bot.document.theme.GEOMETRY` set, so the PDF
frame and the Word frame are the same frame.
"""

from __future__ import annotations

from dataclasses import dataclass

from .theme import THEME

#: PostScript point per centimetre.
CM = 28.346456692913385


def cm_to_pt(value_cm: float) -> float:
    return value_cm * CM


@dataclass(frozen=True, slots=True)
class PdfFrame:
    """The A4 page and its text frame, in points."""

    page_width: float
    page_height: float
    margin_lr: float
    margin_top: float
    margin_bottom: float
    width: float
    height: float
    frame_inset: float
    header_baseline: float
    footer_baseline: float

    @property
    def left(self) -> float:
        return self.margin_lr

    @property
    def bottom(self) -> float:
        return self.margin_bottom

    @property
    def top_from_bottom(self) -> float:
        return self.margin_bottom + self.height


def pdf_frame() -> PdfFrame:
    geometry = THEME.geometry
    width = cm_to_pt(geometry.page_width_cm)
    height = cm_to_pt(geometry.page_height_cm)
    margin_lr = cm_to_pt(geometry.pdf_margin_lr_cm)
    margin_top = cm_to_pt(geometry.pdf_margin_top_cm)
    margin_bottom = cm_to_pt(geometry.pdf_margin_bottom_cm)
    return PdfFrame(
        page_width=width,
        page_height=height,
        margin_lr=margin_lr,
        margin_top=margin_top,
        margin_bottom=margin_bottom,
        width=width - 2 * margin_lr,
        height=height - margin_top - margin_bottom,
        frame_inset=geometry.pdf_frame_inset_pt,
        # Header sits inside the frame inset, above the text frame; the footer
        # sits below it. Both stay clear of the content frame by design.
        header_baseline=height - 1.15 * CM,
        footer_baseline=1.1 * CM,
    )


__all__ = ["CM", "PdfFrame", "cm_to_pt", "pdf_frame"]
