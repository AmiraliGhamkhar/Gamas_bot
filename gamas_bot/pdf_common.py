"""Shared reportlab pieces for the two PDF deliverables.

The booklet renderer (:mod:`gamas_bot.notes_pdf`) and the Free Office PDF
exporter (:mod:`gamas_bot.pdf_export`) print the same bundled Vazirmatn face
onto the same A4 geometry and both promise byte-identical output for identical
input. Font discovery/registration, Persian shaping and the invariant canvas
therefore live here once, so the two documents cannot drift apart (registering
the same face twice also used to be duplicated bookkeeping in each module).
"""

from __future__ import annotations

from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as _rl_canvas

from .config import PROJECT_DIR

#: Registered face names. They are stable because stored PDF bytes reference
#: them and the visual-QA test asserts the embedded subset by name.
REGULAR_NAME = "GamasVazir"
BOLD_NAME = "GamasVazirBold"
REGULAR_FILE = "Vazirmatn-Regular.ttf"
BOLD_FILE = "Vazirmatn-Bold.ttf"

#: Registered once per process: reportlab rejects a second registration.
_font_names: tuple[str, str] | None = None


def find_font(filename: str) -> Path | None:
    """The bundled font file, discovered from the project or the working dir."""
    for base in (PROJECT_DIR / "assets" / "fonts", Path.cwd() / "assets" / "fonts"):
        path = base / filename
        try:
            if path.is_file():
                return path
        except OSError:  # pragma: no cover - defensive
            continue
    return None


def register_fonts() -> tuple[str, str]:
    """``(regular, bold)`` registered font names, or an ``OfficeError``.

    ``OfficeError`` is imported lazily: :mod:`gamas_bot.office` pulls in the
    OOXML stack, and a missing font must not turn a Media/STT import into an
    Office import.
    """
    global _font_names
    if _font_names is not None:
        return _font_names
    from .office import OfficeError

    regular = find_font(REGULAR_FILE)
    if regular is None:
        raise OfficeError(
            "فونت فارسی روی سرور پیدا نشد؛ assets/fonts/Vazirmatn-Regular.ttf لازم است."
        )
    bold = find_font(BOLD_FILE) or regular
    try:
        if REGULAR_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(REGULAR_NAME, str(regular)))
        if BOLD_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(BOLD_NAME, str(bold)))
    except Exception as exc:  # noqa: BLE001 - reportlab raises several types
        raise OfficeError("فونت فارسی خوانده نشد؛ فایل فونت خراب است.") from exc
    _font_names = (REGULAR_NAME, BOLD_NAME)
    return _font_names


def visual(text: str) -> str:
    """One line in storage order: contextual forms, then visual reordering."""
    if not text:
        return text
    return get_display(arabic_reshaper.reshape(text))


class InvariantCanvas(_rl_canvas.Canvas):
    """A canvas with a fixed ID and timestamps: identical bytes per input."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs["invariant"] = 1
        super().__init__(*args, **kwargs)
