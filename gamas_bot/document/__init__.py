"""The Gamas document engine.

The pipeline this package supports:

    LLM → validated ``StructuredNotes`` → document engine → DOCX + PDF
        → automated QA → artifact validation → Telegram delivery

Division of responsibility, and the reason this package exists:

* :mod:`~gamas_bot.document.theme` owns every visual token (colour, type size,
  spacing, page geometry, component dimensions). No renderer hard-codes one.
* :mod:`~gamas_bot.document.components` owns the semantic vocabulary: what a
  warning, definition, high-yield card or table *is*, in visual terms.
* :mod:`~gamas_bot.document.assets` owns the artwork: semantic keys, a manifest,
  deterministic topic selection. A booklet never generates an image.
* :mod:`~gamas_bot.document.rtl` owns Persian shaping and logical-order
  wrapping for the PDF; Word keeps logical order and resolves BiDi itself.
* :mod:`~gamas_bot.document.layout` and :mod:`~gamas_bot.document.qa` own
  page geometry and post-render inspection.

The content model (``gamas_bot.notes``) stays the single semantic source: the
LLM never controls colours, fonts, layout or OOXML.
"""

from __future__ import annotations

from .theme import THEME, GamasTheme
from .theme import Geometry, Palette, Spacing, TypeScale

__all__ = [
    "THEME",
    "GamasTheme",
    "Geometry",
    "Palette",
    "Spacing",
    "TypeScale",
]
