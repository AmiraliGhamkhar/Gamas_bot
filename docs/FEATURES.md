# Features and limits

Everything below runs in one process with SQLite; no office suite, media binary
or local OCR engine is involved. Overflow is **refused**, never silently
truncated — split the source instead.

## Audio and video

Send audio, video or a Telegram voice/video note.

- The **raw transcript is always delivered first** as a `.txt` file (fallback:
  up to 8 files of 100 000 characters each). Booklet generation happens after it
  and can fail without losing the transcript.
- Upload cap `MAX_FILE_SIZE=1073741824` (1 GiB), duration
  `MAX_MEDIA_DURATION_SECONDS=7200` (2 hours). Unmeasurable duration is refused
  before any credit is held.
- PyAV carries its own FFmpeg libraries, so no system `ffmpeg`/`ffprobe` is
  needed; media work runs in bounded child processes (`MEDIA_WORKER_CONCURRENCY=1`
  on shared hosting, keep it).
- `AUDIO_FORMAT_POLICY=auto` picks WAV or Opus from size and provider limits;
  change it only after measuring.
- The code-default transcription failover order is Speechmatics (`enhanced`),
  Deepgram (`nova-3`), then an OpenAI-compatible API (`whisper-1`). Keyless
  routes are skipped; see [Providers](PROVIDERS.md) for overrides and limits.
- A 64 MiB free-space reserve (`TEMP_MIN_FREE_BYTES`) is kept for temporary work.

Check the media stack without touching a provider:

```powershell
.\.venv\Scripts\python.exe -m gamas_bot.media_worker check
```

```bash
python -m gamas_bot.media_worker check
```

## Booklets (Word and PDF)

With a note provider configured, a finished transcript also produces a Persian
RTL booklet. Booklets, AI quizzes and AI flashcards use the same note-provider
chain; the code default starts with Gemini `gemini-2.5-flash-lite`, followed by
Nara, OpenRouter, DeepSeek and AvalAI when their credentials are configured.
The operator reports the current deployment model IDs as Nara
`laguna-s-2.1`, OpenRouter `nvidia/nemotron-3.5-super-vl:free`, and Gemini
`gemini-3.5-flash-lite`; this checkout has no live `.env` to verify their active
order. See [Providers](PROVIDERS.md) for defaults and overrides.

- Word (`.docx`) with mixed Persian/English runs, heading styles, tables,
  «خلاصه», key points and a glossary. Fonts are **referenced**, not embedded —
  install `Vazirmatn` from `assets/fonts/` on the PC that opens the file.
- PDF built from the same validated `StructuredNotes` model, with an embedded
  Vazirmatn subset, a topic map and selectable text.
- Nothing is pushed automatically: the final message lists what was produced and
  offers one button per artifact. Re-downloading is free and never re-renders.
- Quality gates (`NOTE_STRICT_GROUNDING`, `NOTE_QUALITY_GATE`,
  `NOTE_COMPLETENESS_FILL`) remove unsupported numbers and reject truncated or
  malformed model output. They cannot prove every sentence is correct — review
  medical, legal or safety material manually.
- `NOTE_MODE` is a legacy no-op: audio booklets are always full booklets; compact
  revision notes are a separate document action.

Editing happens in the [Mini App](MINIAPP_CPANEL.md): every structured save
appends a revision and re-renders both PDF and DOCX, so an export always matches
a saved revision.

## Study documents

Send a text PDF, DOCX or PowerPoint (`.pptx`). Legacy `.ppt`/`.pps`/`.pot` is
converted in-process; `.doc` is not supported — save as `.docx` first.

- The source card offers extracted text, full booklets, compact revision notes,
  exports, the original download and learning actions. Page/section/slide
  locators stay attached.
- Identical uploads reuse the same user's extraction; caches are owner-scoped.
- Limits: `DOC_MAX_BYTES=1073741824` (1 GiB), `DOC_UNPACKED_MAX_BYTES=1073741824`
  expanded archive, 5 000 000 extracted characters, 250 000 characters per
  note/export source.
- PowerPoint audio is **not** processed on upload. The deck's source card has
  **🎧 پردازش صدای ارائه**; only that button starts narration. Slide text,
  speaker notes and linked clips are supported, and words are mapped to slides
  only when the timing actually fits. Unlinked media needs
  `PRESENTATION_INCLUDE_UNREFERENCED_MEDIA=true`.
- Narration limits: `PRESENTATION_MAX_CLIPS=50`,
  `PRESENTATION_MAX_TOTAL_DURATION=7200`, `PRESENTATION_MAX_TEMP_BYTES=268435456`.
  Inserted silence is free; a deck without narration is free but still recorded.

## Free Office / آفیس رایگان

Paste text/messages or upload `.txt`, `.md`, `.markdown`, `.csv`, `.tsv`, then
pick Word, Excel, PowerPoint or PDF.

- `OFFICE_ENABLED=true` by default. No note key is required; deterministic
  fallbacks keep the workflow usable without any AI provider.
- One private in-memory draft per user, `OFFICE_SESSION_TTL_MINUTES=30`, cleared
  on cancel/navigation/shutdown.
- Limits: `OFFICE_MAX_CHARS=100000`, `OFFICE_MAX_INPUT_BYTES=1000000`,
  `OFFICE_MAX_SLIDES=30`, Excel tables up to 2000 rows × 40 columns, output
  files up to 50 MiB, `OFFICE_JOB_CONCURRENCY=2`.
- Excel cells are literal strings: `=1+2` is stored, never evaluated. Text to
  PowerPoint creates a new deck; it is not a visual conversion of a document.

Office and document notes/exports never spend user credit or learning gifts, but
configured AI calls still cost the operator money.

## OCR (opt-in, third party)

**Off by default.** `OCR_ENABLED=false` refuses text-poor scans and photos
instead of saving an empty document. Enabling it uploads page images to
**i2ocr.com** — no local OCR engine runs.

```powershell
.\.venv\Scripts\python.exe -m pip install ".[ocr]"
```

```bash
python -m pip install '.[ocr]'
```

```dotenv
OCR_ENABLED=true
OCR_LANGUAGE=persian          # `fa` and `farsi` are accepted aliases
OCR_MAX_PAGES=20              # 0 = every page
OCR_TIMEOUT_SECONDS=600
```

`curl-cffi` is only needed for this feature and is validated at startup when OCR
is on. Existing PDF text is not OCRed; images embedded inside Word/PowerPoint
are not OCRed either — send them separately. Successful text is cached in
SQLite, and library pruning does not erase that cache. Test live with your own
non-sensitive file:

```bash
python -m scripts.ocr_smoke scan.jpg --language persian
```

## Library, learning and Studio

- **Library** keeps the newest `LIBRARY_MAX_ITEMS` (50) Telegram file references
  per user; files themselves stay in Telegram. Pruning a reference does not
  delete cached extracted text.
- **Learning**: quizzes and flashcards generated from a saved booklet or
  document, previewed before saving. Costs are in [Billing](BILLING.md).
- **Gamas Studio** (Mini App): home summary, all-time/weekly/monthly
  leaderboard, per-submission speakers with owner-chosen names, structured
  booklet editor, exports and revision history.

## Privacy summary

Audio goes to your STT provider; note text goes to your note provider; OCR (if
enabled) sends images to i2ocr. Logs contain references, counts and durations —
never keys, transcript text, receipts or document contents. See
[Security](SECURITY.md).
