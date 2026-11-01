# Word output

Booklets use RTL Persian paragraphs, mixed Persian/English runs, heading styles
and tables. The raw audio transcript is delivered separately.

- The topic list links to headings without page numbers: the server has no Word layout engine.
- Fonts are referenced, not embedded. Install Vazirmatn from `assets/fonts/` on
  the Windows computer used to view files.
- Microsoft Word is the reference viewer; others may lay out mixed-direction text
  differently. Check tables, numbers and formulas before distributing output.
- Keep text in logical Unicode order; never reverse text to fix display.

Rendering: `gamas_bot/docx_export.py` and `gamas_bot/bidi.py`.
[Font license](../assets/fonts/LICENSE.txt) · [Quality limits](NOTE_QUALITY.md)
