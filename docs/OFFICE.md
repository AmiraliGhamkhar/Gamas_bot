# The Free Office section

`📄 آفیس رایگان` turns the user's own text into a real Office file: Word,
Excel, PowerPoint or PDF. It is a second front door to the bot — the lecture
pipeline answers *«I have a recording, make me a booklet»*, this one answers
*«I have text, make me a document»*.

The section is **free**: it never reserves credit, never charges, and never
refunds, because there is nothing to charge. The only thing it records is a
`submissions` row of kind `office`, so the admin panel can see how many
documents were made.

## The flow

```
📄 آفیس رایگان  →  choose a format  →  send the text (or a .txt/.md/.markdown/.csv)
        ↑                                     ↓
        └──── «➕ افزودن متن»  ←  the document is delivered
```

The content and the format may arrive **in any order**. A user who pastes a
table first is asked for the format afterwards; a user who picks a format
first is asked for the text. Both end at the same place.

* a **text message** is the content;
* an uploaded **`.txt` / `.markdown` / `.md` / `.csv` / `.tsv` / `.log`** file is
  the content (read as UTF-8, UTF-8-BOM, cp1256 or Latin-1, whichever decodes);
  very long messages and full text uploads are split at headings / paragraphs
  so the presentation planner always receives a processable chunk;
* after the document is delivered, «➕ افزودن متن» reopens the request so the
  next message is added to the same document and a new one is built.

A session that is left idle for `OFFICE_SESSION_TTL_MINUTES` is dropped, so
the content of a request is not kept for hours.

## What the text becomes

`gamas_bot/mdblocks.py` reads the text once, for all four formats:

| The user wrote | It becomes |
| --- | --- |
| `# عنوان` … `###### عنوان` | a heading (Word: `Heading 1/2/3`; PowerPoint: a slide title) |
| `- مورد` / `* مورد` / `• مورد` | a bullet |
| a grid of tab-, pipe- or comma-separated rows | a table (Word: a real table; Excel: a sheet) |
| anything else | a paragraph — **one line per paragraph**, so the line breaks the user typed survive |

A comma-separated row needs **at least three columns** to count as a table.
Persian prose is full of commas, and a two-column guess would turn a sentence
into a table; a tab or a pipe is unambiguous and is accepted at any width.

The title of the document is the first heading, or the first line when it is
short enough to be one, or the name of the uploaded file.

## The four files

**Word** is rendered by `gamas_bot/docx_export.py` — the same engine as the
lecture booklet, so the document has the brand cover, the topic list, the page
frame, the running header and the footer page field, all right-to-left. A
pasted grid becomes a real Word table with the booklet's own table dress.

**Excel** (`gamas_bot/office.py`) is a right-to-left sheet (`w:rtl` on the
sheet view), a navy header row that repeats on every printed page, frozen
headings, zebra rows and sized columns. A cell that is *only* an ASCII number
becomes a real number, so a column can be summed; Persian digits stay text,
because turning «۲۰» into the number 20 would change what the sheet says.

**PowerPoint** is 16:9, with a title slide, an agenda, one slide per section
and a closing slide. Every paragraph is right-to-left (`a:pPr/@rtl`) and
right-aligned, and Persian runs carry a complex-script face. The full text of
each section is kept in the notes pane. A text that has no headings of its
own is cut into slides of six lines; its first line titles the deck instead
of repeating itself as the first bullet.

**PDF** (`gamas_bot/pdf_export.py`) is rendered by `reportlab` from the same
parsed blocks, so it reuses the booklet's cover, the optional clickable topic
list, the page frame, the running header and the footer page number. Persian
is shaped with `arabic-reshaper` and reordered for storage with `python-bidi`,
set in the bundled Vazirmatn face (SIL OFL, `assets/fonts/`); wrapping happens
on the logical text and only the finished lines are shaped. The canvas is
`invariant`, so identical text always produces identical bytes. Like the other
formats it never calls a model: the words that reach the PDF are the user's
words. The only textual changes are typographic — Arabic letter forms are
folded to Persian, repeated whitespace collapses, and a horizontal rule line is
not carried into the document. Nothing else is added, removed or reordered.

## The model layer is optional

Arranging prose into slides, and inventing a table where there is none, is
the only thing a model is asked for — and it is only asked when a note
provider is configured. `gamas_bot/office_ai.py` splits very long messages
and `.txt` / `.markdown` uploads at headings and paragraphs, then sends each
chunk separately to the model, merges the results, and validates the output
against the user's text:

* a slide bullet that does not appear in the source is dropped, and a bullet
  that appears out of order is dropped too;
* a table row with a cell that is not in the source is dropped;
* a plan that lost more than half its lines is refused entirely.

Whatever survives is an **arrangement** of the user's own words, never a
rewrite. When there is no provider, when the request fails, or when the answer
fails the check, the deterministic fallback runs: the user's own headings
become the slides, and the user's lines become a one-column sheet. The
section therefore works on a deployment with no model at all.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OFFICE_ENABLED` | `true` | the whole section; when off, the menu entry and the flow are gone |
| `OFFICE_MAX_INPUT_BYTES` | `1000000` | the largest uploaded text file (bytes) |
| `OFFICE_MAX_CHARS` | `20000` | the largest text of one document |
| `OFFICE_MAX_SLIDES` | `30` | the most slides one deck may have |
| `OFFICE_SESSION_TTL_MINUTES` | `30` | how long an idle request is kept |

## What it is not

* The section does not read **uploads of documents**: a `.docx` or a `.pdf` is
  not a source. Only text and text files are.
* The section does not **rewrite** the user's text. It normalises letter forms
  and whitespace and does not carry `---` rule lines; if a cap truncates a deck
  the event is logged. Nothing is reworded or summarised.
* A cell that is only an ASCII number becomes a real number so it can be summed,
  **unless** it is zero-padded (`007`, `09123456789`) or longer than 15 digits —
  coercion there would change what the cell says, so it stays text.
* The section does not **cost credit**, so it is not a way around the balance:
  it is a separate, free feature, and the lecture pipeline is unchanged.
