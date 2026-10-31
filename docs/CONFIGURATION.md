# Configuration

`.env.example` lists every setting with its default. Copy it to `.env`, fill the required values,
leave the rest as shipped unless you have a reason to change them. Keep `.env` private.

## Create `.env`

**Windows PowerShell**

```powershell
Copy-Item .env.example .env
notepad .env
```

**cPanel Terminal**

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

## Required values

```dotenv
TELEGRAM_BOT_TOKEN=...
TELEGRAM_API_ID=12345
TELEGRAM_API_HASH=...
ADMIN_IDS=111111111,222222222
SPEECHMATICS_API_KEY=...       # or DEEPGRAM_API_KEY, or the STT_OPENAI_* trio
```

`ADMIN_IDS` are numeric Telegram user IDs. A process environment variable overrides `.env`;
otherwise the built-in default applies. Relative paths resolve from the project directory, so a
cron job cannot create a second database or session.

## Settings that matter most

| Setting | What to do |
| --- | --- |
| `STT_PROVIDERS` | Ordered failover list; default `speechmatics,deepgram,openai_compatible` (Speechmatics primary, immediate non-blocking overflow/failover to Deepgram). |
| `STT_FALLBACK_ENABLED` | Enable automatic STT overflow/failover across configured providers (default `true`). |
| `SPEECHMATICS_MAX_IN_FLIGHT`, `DEEPGRAM_MAX_IN_FLIGHT`, `STT_OPENAI_MAX_IN_FLIGHT` | Local concurrent slot caps per STT provider (`2` / `4` / `2`). When primary slots are full, new jobs overflow immediately to Deepgram without waiting. |
| `STT_PROVIDER_TIMEOUT_SECONDS`, `STT_SLOT_WAIT_TIMEOUT_SECONDS` | Per-provider wall-clock attempt ceiling (`1800` s) and max wait (`600` s) only when **all** configured STT providers are simultaneously saturated (or briefly cooling down). The wait is bounded, and overflow to a provider with a free slot still happens with zero wait. |
| `STT_LANGUAGE` | Persian default `fa`. Deepgram requires `DEEPGRAM_MODEL=nova-3` for Persian. |
| `NOTE_PROVIDERS` | Ordered note routes. Providers without their own key are skipped. See [Note providers](PROVIDERS.md). |
| `LLM_PRIMARY_MAX_IN_FLIGHT`, `LLM_FALLBACK_MAX_IN_FLIGHT`, `LLM_SLOT_WAIT_TIMEOUT_SECONDS` | Local concurrent slot caps for primary (`4`) and secondary (`4`) note routes plus all-full wait bound (`300` s); primary saturation overflows immediately in `0 ms` to secondary routes. Startup (`--check`) warns when `MAX_ACTIVE_JOBS` exceeds the configured STT slots or `NOTE_CONCURRENCY` exceeds the configured LLM slots. |
| `MAX_ACTIVE_JOBS`, `MAX_QUEUED_JOBS` | Active stage-gated pipeline workers (default `3`) and pending queue ceiling (default `8`). |
| `MAX_CONCURRENT_DOWNLOADS`, `MAX_CONCURRENT_UPLOADS` | Independent stage semaphores for Telegram downloads (`2`) and uploads (`2`). |
| `DOWNLOAD_TIMEOUT_SECONDS`, `UPLOAD_TIMEOUT_SECONDS` | Stage deadlines for Telegram download (`900` s) and upload (`600` s). |
| `DEDUP_WINDOW_SECONDS` | Duplicate Telegram update & duplicate active file suppression window (`120` s). |
| `SUBSCRIPTION_CHANNEL`, `SUBSCRIPTION_REQUIRED`, `SUBSCRIPTION_CACHE_SECONDS`, `SUBSCRIPTION_TIMEOUT_SECONDS`, `TELEGRAM_API_BASE_URL` | Mandatory channel subscription gate. Every user must be a member of `SUBSCRIPTION_CHANNEL` (default `@gamas_AI`) before protected features; the bot must be an administrator of that channel. `SUBSCRIPTION_CACHE_SECONDS=0` re-checks on every interaction. See [Mandatory channel subscription](TELEGRAM_SUBSCRIPTION.md). |
| `MAX_ACTIVE_JOBS_PER_USER` | Per-user admission cap (default `2`); stops several uploads the balance cannot cover. |
| `MAX_FILE_SIZE` | Download ceiling (default 128 MiB) so one upload plus working copies fits modest shared hosting. Raise only after checking Telegram and disk limits. |
| `TEMP_MIN_FREE_BYTES` | Free space that must remain on `TEMP_DIR` before a download (default 64 MiB); the downloader also reserves room for copies. |
| `DOWNLOAD_STALL_SECONDS` | Abort a Telegram download with no progress for this long (default `300`). |
| `MEDIA_WORKER_CONCURRENCY` | Heavy media jobs at once. Use `1` on shared hosting. |
| `AUDIO_FORMAT_POLICY` | `auto` (default) picks WAV or Opus from encoder CPU + measured transfer rate; `wav`/`opus` force one. |
| `STT_TRANSFER_TIMEOUT_SECONDS`, `MAX_STT_RESPONSE_BYTES` | One transfer deadline (default `600`) and response ceiling (default 64 MiB); file uploads stream from disk. |
| `NOTE_MAX_OUTPUT_TOKENS`, `NOTE_REQUEST_TIMEOUT_SECONDS` | Output budget (default 16384) and request deadline (default 900). LLM response bodies are capped at 16 MiB and streamed incrementally. |
| `NOTE_CONCURRENCY` | Parallel note-map requests (default `3`, capped at chunks and 8). Lower it for low-RPM providers or shared hosting. |
| `NOTE_HEDGE_AFTER_SECONDS` | Start a second configured route only after this delay (default `60` seconds); this can spend extra provider quota. |
| `NOTE_STRUCTURED_OUTPUT` | `1` asks for schema-constrained JSON; a provider that rejects it is retried once without it. |
| `NOTE_PROGRESSIVE_BOOKLET` | `1` delivers the audited merge as a draft while the editorial compile runs. |
| `NOTE_STRICT_GROUNDING`, `NOTE_QUALITY_GATE` | Evidence checks (`true`/`repair` by default). See [Note quality](NOTE_QUALITY.md). |
| `NOTE_COMPLETENESS_FILL` | `1` (default) re-scans the source after the draft and fills coverage gaps with bounded, source-grounded requests (max 6). `0` disables the extra pass. See [Note quality](NOTE_QUALITY.md). |
| `NOTE_MODE` | Deprecated / no-op. Only FULL booklets (complete, class-replacement) are produced; `standard` and `summary` were removed and any value is safely ignored. |
| `RAW_FALLBACK_FILE_CHARS`, `RAW_FALLBACK_MAX_FILES` | Part size and count when a long raw `.txt` upload fails. |
| `LIBRARY_MAX_ITEMS` | Newest library references retained per user (default `50`); a source of an active quiz is also retained until a later prune. Files remain in Telegram; extracted document text is a separate cache. |
| `QUIZ_COST_SECONDS_PER_QUESTION` | Credit seconds charged per generated quiz question (default `60`). The presets ask for 5/10/20 questions, so a quiz costs count × this value; debited before generation and refunded on non-durable failure. Bounded to 1–86,400. The first quiz per user is free. |
| `QUIZ_MAX_ACTIVE_HOURS` | An active quiz untouched for longer (default `6` h) is lazily expired on the next quiz start, freeing the one-active-quiz admission slot. |
| `QUIZ_MAX_SESSIONS_PER_HOUR` | Per-user cap on new quiz sessions in any rolling hour (default `3`); checked before the debit. |
| `FLASHCARD_AI_COST_SECONDS` | Base credit charge for one AI flashcard deck (default `300`), scaled by the requested size (10/20/40 cards), debited before generation and fully refunded on failure. The first deck per user is free. Manual decks and Word export are free. |
| `FLASHCARD_AI_DECK_SIZE` | Question/answer pairs requested per AI deck (default `10`, allowed 5–50). Output shorter than five valid cards fails validation and refunds the charge. |
| `LEARNING_CHUNK_CONCURRENCY` | Parallel model calls while generating a quiz/deck (default `4`, allowed 1–8). If there are more chunks than requested items, some chunks receive zero items and the preview reports incomplete coverage. |
| `FLASHCARD_MAX_DECKS_PER_USER` | Stored decks per user, AI and manual counted together (default `30`). New generation is refused at the cap. |
| `DOC_MAX_BYTES` | Largest accepted study document (default `52_428_800` = 50 MB), checked before and during download. Extracted text is separately limited to 5,000,000 characters; larger texts must be split. PDF, `.docx` and PowerPoint are supported; `.doc`/`.ppt` are best-effort by extension. Scans and photos need `OCR_ENABLED` (see the OCR row). |
| `OFFICE_ENABLED` | Free Office user menu and document-note/export actions (`true` by default); `false` hides the menu and rejects stale callbacks. |
| `OFFICE_MAX_INPUT_BYTES`, `OFFICE_MAX_CHARS` | Per-upload text-file cap (default 1,000,000 bytes) and per-draft pasted-text cap (100,000 characters); oversized inputs are rejected whole. |
| `OFFICE_MAX_SLIDES`, XLSX table limits | Maximum total generated slides (30 by default, minimum 4, including title/agenda/closing); XLSX limits are 2,000 data rows and 40 columns per table. Overflow is an error, never silent truncation. |
| `OFFICE_SESSION_TTL_MINUTES` | In-memory text-draft lifetime (30 minutes by default); drafts are swept periodically, on navigation/use and at shutdown. |
| `OFFICE_JOB_CONCURRENCY`, `OFFICE_AI_CONCURRENCY` | Bounded concurrent Office build/note/delivery operations (2) and PPT planning chunks (3). |
| `OFFICE_MIN_INTERVAL_SECONDS` | Per-user delay between Office creations (10 seconds; `0` disables the throttle). This is resource protection, not a charge. |
| `OCR_ENABLED`, `OCR_LANGUAGE`, `OCR_MAX_PAGES`, `OCR_TIMEOUT_SECONDS` | Opt-in OCR for scanned PDFs and photos (`false` by default). When enabled, text-poor page images are uploaded to i2ocr.com and read there, so documents leave the host; requires `pip install 'gamas-bot[ocr]'`. Language defaults to `persian` (`fa`/`farsi` are accepted), at most 20 pages per document are sent, and one document gets 600 seconds. See [OCR](OCR.md). |
| Document Office note/export source ceiling | Fixed at 250,000 characters. Longer extracted documents remain downloadable but note/export actions refuse rather than truncate. |
| `MAX_MEDIA_DURATION_SECONDS` | Maximum accepted audio/video duration (default 7,200 seconds / 2 hours), checked from Telegram metadata and media probes. |
| `PRESENTATION_MAX_*` | Deck limits (defaults: 50 clips, 2 hours total, 128 MiB unpacked, 256 MiB temporary). |
| `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER`, `PAYMENT_BANK_NAME` | Set all three to enable card-to-card purchases. |
| `RECEIPT_RETENTION_DAYS` | Delete receipt images after this many days (default `90`); the payment row remains. |
| `TRANSCRIPT_RETENTION_DAYS` | Delete finished transcript text after this many days (`0` = keep forever). |
| `DB_PATH`, `TELEGRAM_SESSION`, `TEMP_DIR`, `RECEIPT_DIR`, `LOG_FILE` | Keep all of them outside `public_html`. |

Free Office uses the existing configured note routes only. Without note-provider credentials it still
works with deterministic source-preserving exports and notes. Provider requests can have operator-side
API costs, but no Office or document-note action debits user wallet credit or the existing quiz/
flashcard free-use allowances. `TEMP_DIR` must be private and outside `public_html`; drafts themselves
are in memory and expire after `OFFICE_SESSION_TTL_MINUTES`.

## Validate

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe -m gamas_bot --check
```

```bash
# cPanel Terminal (virtualenv active)
python -m gamas_bot --check
python -m scripts.cpanel_preflight
```

`--check` is offline and safe while the bot is running. The preflight also checks the host: OS,
disk, permissions, lock, Telegram and provider connectivity. It reports `FAIL` for problems that
would stop the bot and `WARN` for advisory ones.
