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
* **Right alignment** — direction and alignment are separate properties, so the
  document declares both: `w:docDefaults`, every paragraph style and every
  written paragraph carry `w:jc w:val="right"`. A paragraph with `w:bidi` and no
  `w:jc` aligns to its logical *start* edge, which a reader resolves from its own
  defaults rather than from the file. Only deliberately centred blocks (brand
  mark, cover title, footer, formula lines, topic-list cells) override it.
* **Mixed scripts** — `gamas_bot/bidi.py` splits text into direction runs.
  Persian runs get the complex-script face (`w:cs`) with `w:rtl`; embedded
  English keeps `w:ascii`/`w:hAnsi` and `w:rtl=0`. Logical order is never
  reversed in the file.
* **Defaults and styles** — `w:docDefaults` and every style definition are
  configured RTL, because Word derives the direction of style-generated content
  from there, not from the runs.
* **Real heading styles** — `Heading 1/2/3` carry the outline level, so the
  Navigation Pane, bookmarks and the topic list all work. `Title`, `Subtitle`,
  `Quote`, `Table text`, `Definition`, `Example`, `Note` and `Warning` are
  defined explicitly.
* **Digits** — page numbers, section numbers and list markers are Persian digits;
  numeric *values* in the content are never converted.
* **Fonts** — referenced by name, never embedded. `DEFAULT_FONT_CONFIG` sets the
  four roles (body, heading, latin, fallback) and `word/fontTable.xml` advertises
  the fallback through `w:altName`, so a reader without the Persian face
  substitutes gracefully.

## Page frame, header, footer

The frame is a real `w:pgBorders` inside the section properties
(`offsetFrom="page"`), not a bordered paragraph. The running header carries the
document title; the footer carries a native `PAGE` field, so it stays correct if
the document is re-paginated.

## The topic list

The topic list is a two-column table of **ordinary text**: each entry is an
internal hyperlink to the bookmark of a real `Heading 1/2/3` paragraph.

It is **link-only — it prints no page numbers.** Exact numbers would need a
layout pass through an office renderer, which v3 deliberately does not require.
A number that was not measured is never printed, so the column is omitted rather
than guessed.

It is built only for documents that need one: at least `TOC_MIN_SECTIONS` (4)
navigable headings or `TOC_MIN_BODY_CHARS` (2400) of body text. `TOC_LEVELS`
defaults to `"1-1"`, because the document's own block labels («تعریفها»,
«مثالها») are real Heading 2s that repeat in every section and would otherwise
dominate the list. If nothing qualifies, the page is dropped — an empty
«فهرست مطالب» over an empty table is never emitted, and a missing topic list is
never a reason to lose the booklet.

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
