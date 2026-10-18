# Word document generation

`gamas_bot/docx_export.py` produces the deliverable. It is the most
detail-sensitive module here, because "looks right in Persian" is a much narrower
target than "is a valid `.docx`".

## Layout

Cover page → topic list (long documents only) → learning objectives → summary →
sections → key points → review questions → glossary. The raw transcript ships
alongside as a `.txt`; it is never replaced by the Word document.

## RTL contracts

These are contracts, not styling preferences. `tests/test_docx_export.py` covers
them.

* **Paragraph direction** — RTL-dominant paragraphs get `w:bidi`, and their
  *paragraph mark* gets its own `w:rtl`, so an empty line, the caret and a shaded
  spacer behave RTL too. Latin-only lines keep Word's default.
* **RTL start alignment** — direction and alignment are separate properties.
  Right/start-aligned RTL paragraphs deliberately have **no** `w:jc`: Word
  resolves `w:jc="right"` logically in bidi context and can place headings at
  the physical left margin. No RTL style or document default carries a hidden
  right-alignment `w:jc`; centred blocks use `w:jc="center"`, and prose uses
  `w:jc="both"` for justification. This is the Word-desktop-sensitive path;
  LibreOffice previews are not a substitute for checking Microsoft Word.
* **Mixed scripts** — `gamas_bot/bidi.py` splits text into direction runs.
  Persian runs get the complex-script face (`w:cs`) with `w:rtl`; embedded
  English keeps `w:ascii`/`w:hAnsi` and `w:rtl=0`. Logical order is never
  reversed in the file.
* **Defaults and styles** — RTL paragraph styles and document defaults carry
  `w:bidi`; `Normal`, heading, `Header` and `Footer` styles are explicitly set.
  Every output section also has `w:bidi`, and `settings.xml`'s existing
  `w:themeFontLang` is set to `fa-IR` (including its `w:bidi` locale).
* **Real heading styles** — `Heading 1/2/3` carry the outline level, so the
  Navigation Pane, bookmarks and the topic list all work. `Title`, `Subtitle`,
  `Quote`, `Table text`, `Definition`, `Example`, `Note` and `Warning` are
  defined explicitly.
* **Digits** — generated section numbers and list markers use Persian digits;
  numeric *values* in content are never converted. Footer numbers are a native
  `PAGE` field whose run uses the Persian locale.
* **Fonts and language** — fonts are referenced by name, never embedded.
  `DEFAULT_FONT_CONFIG` defaults the Persian body/heading faces to bundled
  Vazirmatn and Latin/fallback to Tahoma; the DOCX references fonts by name
  rather than embedding them. Persian runs carry `w:rtl`,
  `w:lang w:val="fa-IR" w:bidi="fa-IR"`, `w:szCs`, and the complex-script font
  slot. Latin runs are explicitly LTR and tagged `en-US`.
  `word/fontTable.xml` advertises the fallback through `w:altName`.

## Page frame, header, footer

When a cover is enabled, its frame is a real `w:pgBorders` inside the cover
section properties (`offsetFrom="page"`), not a bordered paragraph.
`add_section()` copies section properties, so the following content section
explicitly removes the cloned frame. Without a cover, the first/body section
retains the configured frame. The cover has no header/footer references; content
has a running title header and a native `PAGE` footer field. Numbering restarts
at 1 in the content section.

## The topic list

The topic list is a one-column static list of **ordinary text**: each entry is an
internal hyperlink to the bookmark of a real `Heading 1/2/3` paragraph.

It is **link-only — it prints no page numbers.** Exact page values require a
layout pass through an office renderer. This service deliberately has no
LibreOffice runtime dependency, so it does not guess or emit placeholder page
numbers. Entries are internal hyperlinks to real bookmarks and remain usable
in mobile viewers. A two-pass renderer-backed TOC is a separate feature, not a
claim made by this Python-only generator.

It is built only for documents that need one: at least `TOC_MIN_SECTIONS` (4)
navigable headings or `TOC_MIN_BODY_CHARS` (2400) of body text. `TOC_LEVELS`
defaults to `"1-1"`, because the document's own block labels («تعریفها»,
«مثالها») are real Heading 2s that repeat in every section and would otherwise
dominate the list. If nothing qualifies, the page is dropped — an empty
«فهرست مطالب» over an empty table is never emitted, and a missing topic list is
never a reason to lose the booklet.

## Typography and tables

Body paragraphs use justified alignment and 1.5 line spacing with 10 pt after
paragraphs; headings are real outline styles with `keep_with_next`. Callout
paragraphs are kept together and table rows use `cantSplit`, with repeating
header rows. Tables use python-docx's `table_direction = RTL` API (minimum
python-docx version 1.2) so logical column one appears on the visual right.

## Reproducibility and performance

* Identical inputs produce byte-identical output. python-docx stamps package
  members with the wall clock on save; a final post-processing pass normalizes
  the timestamps so artifacts are comparable by hash.
* Bookmark identifiers are per-document. They used to come from a process-global
  counter, which made a heading name in one user's booklet reveal that user's
  position in the server's job sequence.
* The font-fallback rewrite touches the whole package, so it runs once on the
  final payload instead of after every intermediate save.
* Style resolution is memoized per document, and every run's `w:rPr` is written
  directly for fresh runs instead of going through python-docx property setters
  (each of which performs several schema-order tree scans). Output is unchanged
  byte-for-byte.

## Logo

A logo is used only from a local path in `LOGO_CANDIDATES`
(`assets/gamas_logo.png` or `.jpg`). Generation never downloads an image and
never fails when the file is missing — it falls back to the typographic mark.
