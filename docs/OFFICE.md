# Free Office (آفیس رایگان) and document notes

Free Office is a user-facing Telegram feature again. The main menu shows **🗂 آفیس رایگان** when
`OFFICE_ENABLED=true` (the default). Every user-facing Office/document-note operation is free from
the user's wallet and does not consume either of the separate free quiz/flashcard allowances.
Configured model providers may still have operator-side API costs; provider use is limited to the
configured note routes and never enables paid transcription.

## Pasted text and text files

1. Open **🗂 آفیس رایگان** before sending text. Paste one or more messages, or upload `.txt`, `.md`,
   `.markdown`, `.csv` or `.tsv` files. Each accepted part is appended to that user's in-memory draft
   in arrival order.
2. Choose Word, Excel, PowerPoint or PDF. The bot delivers a real `.docx`, `.xlsx`, `.pptx` or
   selectable-text `.pdf` and indexes the Telegram file message in the existing personal library.
   Any format can be generated separately from the same draft.
3. Use **➕ افزودن پیام** to continue, **📦 انتخاب خروجی** to build another format, or
   **❌ پایان و پاک‌کردن متن** to discard the draft. `/cancel`, leaving the Office screen, expiry and
   process shutdown also clear the in-memory draft.

CSV/TSV uses Python's quoted-field parser; inconsistent or malformed rows are rejected with a
Persian error instead of being partially parsed. Formula-looking values are written as literal text
in XLSX cells. `openpyxl` does not calculate formulas; the workbook contains values, not a formula
calculation engine.

Uploaded and pasted text is read verbatim, so it can carry C0 control characters (a form feed from a
printer dump, a stray `\x01`). They are normalized once, when the draft is parsed, exactly as the Word
engine has always normalized them: vertical tab and form feed become a space (they separate words),
and characters XML 1.0 cannot store are removed. The visible wording is unchanged, and no format can
refuse a text that another format accepts — before this rule, Excel rejected the whole file
(`openpyxl.IllegalCharacterError`) while Word, PowerPoint and PDF of the same text built normally.

## Uploaded study documents

PDF, `.docx` and `.pptx` uploads retain their original Telegram message and the existing
owner-scoped `documents` extraction. A document card offers:

- a complete source-grounded study booklet, delivered in both Word and PDF;
- distinct compact revision notes, delivered in both Word and PDF;
- Word, Excel, a newly generated PowerPoint, or PDF exports from the complete extracted text;
- the original-file download, the existing quiz/flashcard actions, and return to the document
  library.

Cached page, Word-section and slide locators are carried into note sections and text exports when
available. The original is never overwritten. Audio extraction/transcription for a PowerPoint remains
an independent, explicit **🎧 پردازش صدای ارائه** action; uploading or exporting a document does not
start STT or consume a speech quota.

A generated PowerPoint is a new deck arranged from extracted text, not pixel-perfect conversion of
an arbitrary uploaded presentation. PDF output is a new selectable-text document. Scanned/image-only
PDFs are not OCR-supported: they are refused by the existing extractor with a clear explanation.
Encrypted, malformed and over-limit source files are also refused rather than treated as empty.

## Providers, grounding and fallbacks

- Only the existing configured note-provider routes are reused. With no note provider, Free Office
  still produces plain exports, a deterministic extractive compact revision sheet and a basic
  source-preserving booklet.
- Full and compact notes have separate generation paths. Compact mode has its own prompt/schema and
  bullet-only renderer; it is not the full booklet relabelled. Both paths validate against the
  extracted source and retain available locators. Invalid or unsupported model output is rejected
  in favor of a safe deterministic fallback; dense text that cannot be compressed without losing
  important content may be refused rather than shortened misleadingly.
- PPTX planning must retain every source line. An incomplete plan falls back to complete
  deterministic slides; slide/row/column overflow is rejected rather than silently truncated.
- Office file construction, document reading, note source preparation and temporary-file I/O use the
  existing bounded thread helpers. Office builds have a separate semaphore and per-user creation
  throttle. No system Office/LibreOffice, OCR engine, FFmpeg, Redis, Docker, GPU or new provider is
  required.

## Limits and storage

| Control | Default / behavior |
| --- | --- |
| `OFFICE_ENABLED` | `true`; set `false` to hide the menu and reject stale Office callbacks |
| `OFFICE_MAX_INPUT_BYTES` | `1,000,000` bytes per uploaded text file, checked before and during download |
| `OFFICE_MAX_CHARS` | `100,000` characters per pasted-text draft; a part that exceeds the limit is rejected whole |
| Office document-note/export source | Hard ceiling `250,000` source characters; larger extracted documents remain downloadable, but no note/export path silently truncates them |
| `OFFICE_MAX_SLIDES` | `30` total slides by default (title, agenda and closing included; minimum 4); over-limit PPT output is refused |
| XLSX tables | Up to `2,000` data rows and `40` columns per table; overflow is refused |
| `OFFICE_SESSION_TTL_MINUTES` | `30`; inactive drafts are swept periodically, on navigation/use and during shutdown |
| In-memory drafts | At most `128` users and `200` input parts per draft |
| `OFFICE_JOB_CONCURRENCY` | `2` Office build/note/delivery operations at once |
| `OFFICE_AI_CONCURRENCY` | `3` concurrent slide-planning chunks |
| `OFFICE_MIN_INTERVAL_SECONDS` | `10` seconds per user between Office creations; `0` disables only this throttle |
| Generated file cap | `50 MiB` per output; private temporary files are removed in `finally` |

`DOC_MAX_BYTES` (default 50 MiB) and the existing 5,000,000-character extraction cap apply before
Office document-note/export limits. `TEMP_DIR` must be private and outside `public_html`, as with the
rest of the bot. User documents are not written to public web storage or logged; the library stores
only Telegram message references for generated files. Extracted study text remains in the existing
SQLite document cache and currently follows its existing retention policy.

## Configuration and testing

Set the switch and resource limits in `.env`; see [Configuration](CONFIGURATION.md) and
[cPanel deployment](DEPLOY_CPANEL.md). To run the isolated Office tests:

```bash
python -m pytest -q tests/test_office.py tests/test_office_notes.py tests/test_office_flow.py
```

The flow tests open real DOCX/XLSX/PPTX/PDF outputs, cover CSV formula safety, document-note mode
differences, locators, billing isolation, owner-scoped callbacks and no-provider fallbacks. They use
fake Telegram/provider responses, not a live user account or paid provider.
