# Architecture

One Python process: a Telethon client, a bounded asyncio job queue and a SQLite database. No broker,
separate media server or external database is required. Windows is for local use; cPanel uses a
cron watchdog.

## Flows

```text
Audio/video/deck → bounded queue → PyAV media worker → STT
                 → raw transcript delivered → optional notes
                 → evidence checks → RTL Word booklet
                 → transcript/booklet Telegram message references saved to the user's library

PDF/DOCX/PPTX → owner-scoped extraction → locator-tagged SQLite document cache
               → full booklet / distinct compact notes / text exports
               → original download + generated Word/Excel/PowerPoint/selectable PDF in library

Pasted text or .txt/.md/.markdown/.csv/.tsv → private in-memory Office draft
               → bounded Office semaphore → DOCX/XLSX/PPTX/selectable PDF
               → Telegram file references in the same per-user library

Wallet → balance + plan purchase + recent payments
```

The raw transcript is saved and delivered before lecture-note generation. When note providers are
not configured, audio jobs still deliver transcripts; Free Office remains usable with deterministic
fallbacks. PowerPoint narration is merged into one STT request only after the user explicitly selects
the audio action on a source document. Uploading a presentation or exporting a deck never starts STT.
Generated files remain in Telegram and the existing library indexes their chat/message references;
extracted booklet and uploaded-document text remain in SQLite for learning/document actions. Free
Office drafts are in-memory only, user-scoped, bounded to 128 concurrent drafts and swept after their
configured TTL. Office operations never enter billing, the ledger, subscriptions or quiz/flashcard
free-use counters.

## Code map

| Area | Files |
| --- | --- |
| Startup/configuration | `__main__.py`, `config.py`, `launcher.py`, `instance_lock.py`, `logging_config.py`, `metrics.py` |
| Telegram, queue & stage gates | `bot.py`, `jobs.py`, `capacity.py`, `progress.py`, `telegram_text.py` |
| Media and decks | `media.py`, `media_worker.py`, `presentations.py` |
| Providers and notes | `stt.py`, `llm.py`, `http_util.py`, `transcript.py`, `textnorm.py`, `notes.py`, `notes_evidence.py` |
| Documents and Free Office | `doc_extract.py` (PDF/Word/PowerPoint → locator-tagged sections → chunks), `docx_export.py`, `pdf_export.py`, `office.py` (bounded text/CSV parsing and builders), `office_ai.py` (optional source-checked deck/table planning), `office_notes.py` (full and compact note modes), `mdblocks.py`, `bidi.py` |
| Data, billing and reporting | `db.py`, `billing.py`, `metrics.py`, `migrations/v3/` |

## Rules

- Every job receives a unique `job_id` (`job-<submission_id>-<hex>`), isolated per-job state, and explicit lifecycle transitions (`queued` → `downloading` → `extracting` → `transcribing` → `generating_notes` → `uploading` → `completed` / `failed` / `cancelled`).
- Concurrency is decoupled by pipeline stage rather than a single global lock: `MAX_CONCURRENT_DOWNLOADS`, `MEDIA_WORKER_CONCURRENCY`, per-provider STT pools (`SPEECHMATICS_MAX_IN_FLIGHT` / `DEEPGRAM_MAX_IN_FLIGHT`), per-route LLM pools (`LLM_PRIMARY_MAX_IN_FLIGHT` / `LLM_FALLBACK_MAX_IN_FLIGHT`), and `MAX_CONCURRENT_UPLOADS`. No stage slot is held while waiting for another stage.
- Primary saturation on either STT (`speechmatics`) or LLM (`routes[0]`) immediately overflows in `0 ms` to secondary capacity (`deepgram` / `routes[1]`) without waiting.
- SQLite runs in WAL mode with serialized `BEGIN IMMEDIATE` writes on `self.db` and lock-free MVCC reads on `self.read_db` (`PRAGMA query_only=ON`).
- Each user may hold at most `MAX_ACTIVE_JOBS_PER_USER` pending/processing submissions; the check
  and insert share one write transaction. Shutdown joins in-flight upload/document/receipt admission
  before closing SQLite, and fails jobs that never reached a worker.
- Document parsing has its own semaphore sized by `MEDIA_WORKER_CONCURRENCY` and reuses a same-user,
  same-hash extraction even for simultaneous uploads. Extracted text is capped at 5,000,000
  characters; parsing still runs in a worker thread rather than an OS-isolated process.
- Free Office is independently gated by `OFFICE_ENABLED`; one user has at most one in-memory draft,
  100,000 pasted characters by default, 1 MB per text-file upload, 200 parts and the configured
  TTL. Its build/note/delivery work has its own bounded semaphore, per-user throttle and 50 MiB
  output cap. Document notes/exports accept at most 250,000 source characters and refuse overflow
  rather than truncating; compact notes use a dedicated compact-only prompt/schema and shared source
  evidence checks. Office callbacks are private and document callbacks always query by owner id.
- File parsing/rendering uses `run_file_io` and temporary files under the private `TEMP_DIR`; Office
  tasks are canceled and joined before SQLite/HTTP shutdown, and temporary artifacts are removed in
  `finally`. No Office suite or new external service is required.
- The media worker runs with a timeout and no shell. `av` supplies the media libraries.
- SQLite migrations run at startup; billing writes are transactional.
- A lock on the Telegram session prevents duplicate polling. Keep the session and database on a
  local writable filesystem.
- Provider output is untrusted and checked before delivery.
- The admin dashboard reads only authoritative rows: each submission records `started_at` (the
  worker pickup), `error_kind` (one bounded failure category) and `stt_engine` (written beside the
  transcript, so provider usage survives transcript retention). Windows filter `created_at` with a
  sargable `>= ?` bound so the reporting indexes are used; see [Admin panel](ADMIN.md).
- Reporting never mutates state and runs on the read-only connection; a dashboard tap cannot take
  the write lock away from a job.
