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

Wallet → balance + plan purchase + recent payments
```

The raw transcript is saved and delivered before note generation. When notes are not configured the
job ends after transcript delivery; it never offers a mode choice. PowerPoint narration is merged
into one STT request and mapped to slides only where timestamps support it. Library entries contain
only Telegram chat/message references, never a server-side copy of the artifact.

## Code map

| Area | Files |
| --- | --- |
| Startup/configuration | `__main__.py`, `config.py`, `launcher.py`, `instance_lock.py`, `logging_config.py`, `metrics.py` |
| Telegram, queue & stage gates | `bot.py`, `jobs.py`, `capacity.py`, `progress.py`, `telegram_text.py` |
| Media and decks | `media.py`, `media_worker.py`, `presentations.py` |
| Providers and notes | `stt.py`, `llm.py`, `http_util.py`, `transcript.py`, `textnorm.py`, `notes.py`, `notes_evidence.py` |
| Documents | `doc_extract.py` (PDF/Word/PowerPoint → locator-tagged sections → chunks), `docx_export.py`, `pdf_export.py`, `office.py`, `office_ai.py` (pure exports; retired UI), `mdblocks.py`, `bidi.py` |
| Data, billing and reporting | `db.py`, `billing.py`, `metrics.py`, `migrations/v3/` |

## Rules

- Every job receives a unique `job_id` (`job-<submission_id>-<hex>`), isolated per-job state, and explicit lifecycle transitions (`queued` → `downloading` → `extracting` → `transcribing` → `generating_notes` → `uploading` → `completed` / `failed` / `cancelled`).
- Concurrency is decoupled by pipeline stage rather than a single global lock: `MAX_CONCURRENT_DOWNLOADS`, `MEDIA_WORKER_CONCURRENCY`, per-provider STT pools (`SPEECHMATICS_MAX_IN_FLIGHT` / `DEEPGRAM_MAX_IN_FLIGHT`), per-route LLM pools (`LLM_PRIMARY_MAX_IN_FLIGHT` / `LLM_FALLBACK_MAX_IN_FLIGHT`), and `MAX_CONCURRENT_UPLOADS`. No stage slot is held while waiting for another stage.
- Primary saturation on either STT (`speechmatics`) or LLM (`routes[0]`) immediately overflows in `0 ms` to secondary capacity (`deepgram` / `routes[1]`) without waiting.
- SQLite runs in WAL mode with serialized `BEGIN IMMEDIATE` writes on `self.db` and lock-free MVCC reads on `self.read_db` (`PRAGMA query_only=ON`).
- Each user may hold at most `MAX_ACTIVE_JOBS_PER_USER` pending/processing submissions; the check
  and insert share one write transaction.
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
