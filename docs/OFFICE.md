# Free Office menu

**📄 آفیس رایگان** converts a message or a text file into `.docx`, `.xlsx`, `.pptx` or `.pdf`.
It never consumes credit.

## Input

- Plain text, or a `.txt`, `.md`, `.markdown`, `.csv`, `.tsv` or `.log` file.
- Uploaded `.docx` and `.pdf` files are **not** read as source documents.
- Headings, bullets and clear tab/pipe-separated rows become document structure; other lines stay
  paragraphs.

## Behavior

- The deterministic path preserves the user's wording apart from basic Persian/whitespace
  normalization.
- A configured note model may arrange prose into slides or a table. Every cell is checked against
  the source; a rejected plan falls back to deterministic formatting.
- An unfinished request is held in memory until `OFFICE_SESSION_TTL_MINUTES` expires. It is not
  stored as a transcript; a submission row is recorded for admin statistics.
- «➕ افزودن متن» extends the last document until the same TTL passes.
- One user cannot build two documents closer than `OFFICE_MIN_INTERVAL_SECONDS` (default 10; `0`
  disables the throttle) because the spreadsheet/deck builders may call the note model.

## Limits

`OFFICE_MAX_INPUT_BYTES` bounds the upload; `OFFICE_MAX_CHARS` bounds the document;
`OFFICE_MAX_SLIDES` bounds a deck. `OFFICE_ENABLED=false` hides the whole section. Defaults are in
`.env.example`.
