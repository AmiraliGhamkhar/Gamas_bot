# Word document generation

`gamas_bot/docx_export.py` produces the deliverable. It is the most
detail-sensitive module in the project because "looks right in Persian" is a
much narrower target than "is a valid `.docx`".

## Layout

Cover page (optional) → static table of contents (optional, long documents
only) → learning objectives → summary → sections → key points → review
questions → glossary. The raw transcript ships alongside as a `.txt` file; it is
never replaced by the Word document.

## Persian / RTL requirements

These are contracts, not styling preferences, and each has tests:

* **Paragraph direction** — paragraphs whose text is RTL-dominant get
  `w:bidi`, and their *paragraph mark* gets its own `w:rtl` so an empty line,
  the caret and a shaded spacer behave RTL too. Latin-only lines are left to
  Word's default direction.
* **Mixed scripts** — `gamas_bot/bidi.py` splits text into direction runs;
  Persian runs get the complex-script face (`w:cs`) with `w:rtl`, embedded
  English keeps `w:ascii`/`w:hAnsi` and `w:rtl=0`. Logical order is never
  reversed in the file.
* **Defaults and styles** — `w:docDefaults` and every style definition are
  configured RTL, because Word derives the direction of style-generated
  content and of anything the reader types from there, not from the runs.
* **Real heading styles** — `Heading 1/2/3` carry the outline level, so the
  Navigation Pane, bookmarks and the TOC all work. `TOC 1/2/3`, `Title`,
  `Subtitle`, `Quote`, `Table text`, `Definition`, `Example`, `Note` and
  `Warning` are defined explicitly.
* **Digits** — page numbers, section numbers and list markers are rendered in
  Persian digits; numeric *values* in the content are never converted.
* **Fonts** — referenced by name, never embedded. A named profile sets the four
  roles (see `FONT_PROFILES` in `config.py`); `word/fontTable.xml` advertises
  the fallback through `w:altName` so a reader without the Persian face
  substitutes gracefully.

## The page frame, header and footer

The frame is a real `w:pgBorders` inside the section properties
(`offsetFrom="page"`), not a bordered paragraph. The running header carries the
document title; the footer carries a real `PAGE` field, so it stays correct if
the document is re-paginated.

## The static table of contents

The TOC is a two-column table of **ordinary text**: each entry is an internal
hyperlink to the bookmark of a real `Heading 1/2/3` paragraph. It is not a
`TOC` field, so a reader never sees "update field" placeholders and the numbers
are stable.

Page numbers come from an actual layout pass:

1. the document is saved with fixed-width placeholders;
2. LibreOffice converts it to PDF headlessly and `pypdf` resolves every TOC
   hyperlink to the physical page its target landed on;
3. those numbers are written into the table and the document is saved again;
4. the document is rendered *again* to confirm the written numbers did not move
   anything, and only then is it returned.

`DOCX_TOC_PAGE_NUMBERS` selects the policy when the renderer is unavailable:

| Value | Renderer present | Renderer absent |
| --- | --- | --- |
| `auto` (default) | exact verified numbers | link-only topic list, empty number column |
| `required` | exact verified numbers | document is not generated; explicit error |
| `off` | never invoked | link-only topic list |

Ambiguity, an unstable map, or a link that points at the cover/TOC page is
always a hard failure — a page number that was not measured is never printed.

The render loop is bounded: a stable document costs exactly two renders, and a
document that never stabilises fails after three instead of looping.

## Reproducibility and performance

* Identical inputs produce byte-identical output. python-docx stamps package
  members with the wall clock on save; the final post-processing pass
  normalises the timestamps so artifacts are comparable by hash.
* Bookmark identifiers are per-document. They used to come from a
  process-global counter, which made the name of a heading in one user's
  booklet reveal that user's position in the server's job sequence.
* The font-fallback rewrite touches the whole package, so it runs once on the
  final payload instead of after every intermediate save.
* python-docx resolves a paragraph style by scanning every style in the
  document on every assignment. The resolved id is memoized per document, which
  removes about a quarter of the document build's CPU. Output is unchanged
  byte-for-byte.

## Fonts and images

A logo is used only from a local path (`DOCX_LOGO_PATH`, or
`assets/gamas_logo.*`); generation never downloads an image and never fails when
the file is missing (it falls back to the typographic mark).
