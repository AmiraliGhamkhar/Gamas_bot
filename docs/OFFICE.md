# Free Office and study documents

## Free Office / آفیس رایگان

1. Open **🗂 آفیس رایگان**.
2. Paste text/messages or upload `.txt`, `.md`, `.markdown`, `.csv` or `.tsv`.
3. Select Word, Excel, PowerPoint or PDF and verify the delivered file opens.

`OFFICE_ENABLED=true` is the default. Text collection starts only inside this
workflow. One private in-memory draft per user expires after 30 minutes by default
and clears on cancel/navigation/shutdown. No note key is needed; deterministic
source-based fallbacks remain available.

## Study documents

Send a text PDF, DOCX or PowerPoint. The source card offers extracted text,
full booklets, distinct compact revision notes, exports, original download and
learning actions. Page/section/slide locators stay attached. Identical uploads
reuse the same user's extraction, never another user's cache.

Legacy `.ppt` converts in-process. Save `.doc` as `.docx` before sending.
Scans/photos need [opt-in OCR](OCR.md). PowerPoint upload alone does not run STT;
select its explicit audio/narration action.

## Limits

| Input or output | Default / fixed limit |
| --- | --- |
| Study document | `DOC_MAX_BYTES=52428800` (50 MiB) |
| Extracted text | 5000000 characters |
| Document note/export source | 250000 characters |
| Office text file / draft | 1000000 bytes / 100000 characters |
| Generated slides | 30, including structural slides |
| Excel table | 2000 data rows × 40 columns |
| Output file | 50 MiB |
| Draft capacity | 128 users; 200 parts each |

Overflow is refused, not truncated; split large sources. Excel stores literal
strings, including `=1+2`, without calculating formulas. Generated PowerPoint is a
new text-based deck, not a pixel-perfect conversion. PDF text is selectable;
check Persian layout manually.

## Credit and storage

Office and document-note/export actions never spend wallet credit or learning gifts.
AI calls can cost the operator money. **Separate quiz/flashcard actions can spend
gifts/credit**; narration follows [transcription billing](BILLING.md).

The library stores Telegram message references. Originals are not overwritten.
Extracted text stays in SQLite with no automatic cache-retention policy; pruning
a library reference does not erase it. See [Security](SECURITY.md).
