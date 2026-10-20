# Free Office menu

The **📄 آفیس رایگان** menu converts a message or supported text file into `.docx`, `.xlsx`, `.pptx` or `.pdf`. It does not consume user credit.

## Input and behavior

- Send text or `.txt`, `.md`, `.markdown`, `.csv`, `.tsv` or `.log` content.
- `.docx` and `.pdf` uploads are not source documents and are not read.
- Headings, bullets and clear tab/pipe-separated rows become document structure. Other lines remain paragraphs.
- The deterministic path preserves the user's wording apart from basic Persian/whitespace normalization. A configured note model may arrange text for slides/tables; output is checked against the source and falls back to deterministic formatting on failure.
- An unfinished request is kept in memory until `OFFICE_SESSION_TTL_MINUTES` expires. It is not stored as a database transcript; the submission status is recorded for admin statistics.

Limits are controlled by `OFFICE_MAX_INPUT_BYTES`, `OFFICE_MAX_CHARS`, `OFFICE_MAX_SLIDES` and `OFFICE_SESSION_TTL_MINUTES`. See `.env.example`.
