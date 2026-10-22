# Word output

The bot writes `.docx` booklets with right-to-left Persian paragraphs, mixed Persian/English text,
real Word heading styles and tables. The raw transcript is delivered separately.

## What to expect

- A cover page, section headings, an optional clickable topic list, a header and a page-number
  field.
- The topic list links to headings but has **no page numbers**: the server does not run a Word
  layout engine.
- Fonts are referenced by name, not embedded. Install the bundled Vazirmatn font
  (`assets/fonts/`) on the viewing Windows computer; otherwise Word substitutes a font.
- Microsoft Word is the reference viewer. Other Office-compatible viewers may lay out
  mixed-direction text differently.

Code: `gamas_bot/docx_export.py` (RTL helpers in `gamas_bot/bidi.py`). Tests:
`tests/test_docx_export.py` and `tests/test_pdf_export.py`. Font license:
`assets/fonts/LICENSE.txt`.
