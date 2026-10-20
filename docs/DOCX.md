# Word output

The bot generates `.docx` files with right-to-left Persian paragraphs, mixed Persian/English text, real Word heading styles and tables. The raw transcript is delivered separately as text.

## What to expect

- The booklet uses a cover, section headings, optional clickable topic list, header and page-number field.
- The topic list links to headings but has **no page numbers**; the server does not run a Word layout engine.
- The document refers to fonts by name rather than embedding them. Install the bundled Vazirmatn font on a Windows computer for the intended Persian appearance; the viewer may otherwise substitute a font.
- Microsoft Word is the reference viewer for RTL layout. Other Office-compatible viewers may render mixed-direction text differently.

The exporter is `gamas_bot/docx_export.py`; RTL helpers are in `gamas_bot/bidi.py`. Regression tests are in `tests/test_docx_export.py` and `tests/test_pdf_export.py`. The font license is `assets/fonts/LICENSE.txt`.
