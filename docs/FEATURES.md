# Features and limits

Everything below runs in one process with SQLite; no office suite or media
binary is involved. Overflow is **refused**, never silently truncated — split
the source instead.

## Audio and video

Send audio, video or a Telegram voice/video note.

- The raw transcript is persisted before booklet generation and offered from the
  final results screen; it is not pushed as an automatic file. A booklet failure
  does not erase the transcript. If the results notification itself fails, the
  stored work remains available and the submission is marked `partial`.
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
RTL booklet. Booklets and AI quizzes use the same note-provider
allowlist: AvalAI, Gemini and OpenRouter. The default chain is
`avalai,gemini,openrouter`; task-specific priorities apply before dispatch,
and OpenRouter is always a fallback. The deterministic merge keeps source order,
provenance, and distinct explanations and values. Optional edit-only synthesis
(`NOTE_COMPILE`) is off by default. It can reorganize chapters, shorten explicit
continuation headings, and add extractive summaries, comparisons, and study
links without rewriting the factual body. See
[Providers](PROVIDERS.md) for models, quotas and safe probes.

- Word (`.docx`) with mixed Persian/English runs, heading styles, tables,
  «خلاصه», key points and a glossary. Fonts are **referenced**, not embedded —
  install `Vazirmatn` from `assets/fonts/` on the PC that opens the file.
- PDF built from the same validated `StructuredNotes` model, with an embedded
  Vazirmatn subset, a topic map and selectable text.
- Interactive HTML review sheet (`🌐 دریافت جزوه مرور تعاملی HTML`): a single
  self-contained `.html` file with the Vazirmatn fonts embedded, so it works
  offline. It has a 30-minute study plan split into 5–12 sections (the last one
  a one-minute self-test), collapsible questions and progress tracking. The
  bot builds it from exact excerpts of a verified booklet, without contacting
  a model. The template, fonts and Content-Security-Policy stay unchanged.
  Recovered, unverified, or too-thin booklets do not get a review sheet.
  Downloading it is free and never re-renders the file.
- Nothing is pushed automatically: the final message lists what was produced and
  offers one button per artifact. When a PDF or Word booklet is available,
  «🤝 دعوت دوستان» opens the invite screen without replacing the downloads.
  Re-downloading is free and never re-renders.
- Quality gates (`NOTE_STRICT_GROUNDING`, `NOTE_QUALITY_GATE`,
  `NOTE_COMPLETENESS_FILL`) repair and re-audit claims, remove unsupported local
  spans (whole high-risk medical clauses when needed), and use a deterministic
  source-only outline when the model result cannot be made safe. Structural
  validation runs before rendering. Safe omissions are delivered as `partial`
  with a notice; unsafe or unusable structure is never rendered. These checks
  cannot prove semantic correctness — review medical, legal or safety material
  manually.
- `NOTE_MODE` is a legacy no-op: audio booklets are always full booklets; compact
  revision notes are a separate document action.

## Study documents

Send a text PDF, DOCX or PowerPoint (`.pptx`). Legacy `.ppt`/`.pps`/`.pot` is
converted in-process; `.doc` is not supported — save as `.docx` first. A scanned
PDF (no text layer) or a photo of a page is refused with an explicit message;
no page image is ever sent to a third-party recognition service.

- The source card offers extracted text, full booklets, compact revision notes,
  exports, the original download and learning actions. Page/section/slide
  locators stay attached. On fresh ingestion, mixed PDFs report pages with
  no text layer. If a page raises an extraction error, the bot does not save
  a partial extraction as a complete document.
- Booklet generation shows the same pipeline stages as lecture notes:
  «اعتبارسنجی جزوه با متن سند» (evidence gate) and «ویرایش نهایی جزوه»
  (editorial compile) appear on the progress bar, and an accepted booklet
  whose coverage is imperfect is delivered with an explicit completion note
  instead of a «verified» claim. Compact revision notes report per-chunk
  progress and the same final validation stage.
- Identical uploads reuse the same user's extraction; caches are owner-scoped.
- The code-default ceilings are `DOC_MAX_BYTES=1073741824` (1 GiB) and
  `DOC_UNPACKED_MAX_BYTES=1073741824`, with a 5 000 000-character extraction
  safety limit and a separate 250 000-character limit for non-note exports.
  Booklets split complete extracted text into request-sized pieces. If the
  full booklet would need more than 200 initial map requests, it uses the
  complete source-only fallback when enabled, rather than charging the
  provider for a pathological request count. When several short topics share
  one request, the request names every topic it owns and asks for one
  section each, so packing never hides the rest of the lesson. On shared cPanel,
  deployment must pass `python -m scripts.cpanel_preflight`: the 1 GiB default
  with two download
  slots needs 4.125 GiB of *free* temporary space. The documented 1 GiB cPanel
  profile uses 128 MiB document/input ceilings instead; see
  [cPanel deployment](CPANEL.md#2-validate).
- PowerPoint audio is **not** processed on upload. The deck's source card has
  **🎧 پردازش صدای ارائه**; only that button starts narration. Slide text,
  speaker notes and linked clips are supported, and words are mapped to slides
  only when the timing actually fits. Unlinked media needs
  `PRESENTATION_INCLUDE_UNREFERENCED_MEDIA=true`.
- Narration limits: `PRESENTATION_MAX_CLIPS=50`,
  `PRESENTATION_MAX_TOTAL_DURATION=7200`, `PRESENTATION_MAX_TEMP_BYTES=268435456`.
  Inserted silence is free; a deck without narration is free but still recorded.

## Free Office / آفیس رایگان

The separate Office button is hidden from the menu. Upload `.txt`, `.md`,
`.markdown`, `.csv`, or `.tsv` to open a draft and pick Word, Excel, PowerPoint
or PDF; supported document cards also offer exports. Existing Office callbacks
remain available for older messages. Pasted study text outside an Office draft
enters the main booklet workflow instead of the multi-message Office draft.

- `OFFICE_ENABLED=true` by default. No note key is required; deterministic
  fallbacks keep the workflow usable without any AI provider.
- One private in-memory draft per user, `OFFICE_SESSION_TTL_MINUTES=30`, cleared
  on cancel/navigation/shutdown.
- Limits: `OFFICE_MAX_CHARS=100000`, `OFFICE_MAX_INPUT_BYTES=1000000`,
  Excel tables up to 2000 rows × 40 columns, output files up to 50 MiB,
  `OFFICE_JOB_CONCURRENCY=2`. Decks have **no slide cap** by default
  (`OFFICE_MAX_SLIDES=0`); a positive value restores a hard cap.
- PowerPoint decks are visually categorized: each section's lines are
  classified (grid / statistics / list / prose / mixed) and rendered with a
  category ribbon, badge and colour-coded agenda dots; grid sections become
  real RTL tables and over-full sections split into «(ادامه)» slides.
- Excel cells are literal strings: `=1+2` is stored, never evaluated. Text to
  PowerPoint creates a new deck; it is not a visual conversion of a document.
- Every build shows a multi-stage progress bar (analysis → layout → graphic
  design per slide → delivery).

Office and document notes/exports never spend user credit or learning gifts, but
configured AI calls still cost the operator money.

## Library and learning

- **Library** keeps up to `LIBRARY_MAX_ITEMS` (50) delivered-file references per
  user. Telegram receives files when requested; media-submission booklet PDF,
  DOCX and review HTML bytes are also persisted in SQLite. Pruning a library
  reference does not delete cached extracted text.
- **Learning**: multiple-choice quizzes generated from a saved booklet or
  document and delivered as one offline interactive HTML file, whose questions
  also feed the Mini App's Review tab. Costs are in [Billing](BILLING.md).
  Flashcards were removed; the free first try applies to a quiz only.

## Privacy summary

Audio goes to your STT provider; note text goes to your note provider. Logs
contain references, counts and durations — never keys, transcript text, receipts
or document contents. See [Security](SECURITY.md).
