# Architecture

One Python process: a Telethon client, a bounded asyncio job queue and a SQLite database. No broker,
separate media server or external database is required. Windows is for local use; cPanel uses a
cron watchdog.

## Flows

```text
Audio/video/deck → bounded queue → PyAV media worker → STT
                 → raw transcript delivered → optional notes
                 → evidence checks → RTL Word booklet

Text → Free Office parser → DOCX / XLSX / PPTX / PDF
```

The raw transcript is saved and delivered before note generation. PowerPoint narration is merged
into one STT request and mapped to slides only where timestamps support it. Free Office reads text,
not uploaded `.docx`/`.pdf` files.

## Code map

| Area | Files |
| --- | --- |
| Startup/configuration | `__main__.py`, `config.py`, `launcher.py`, `instance_lock.py`, `logging_config.py`, `metrics.py` |
| Telegram, queue & stage gates | `bot.py`, `jobs.py`, `capacity.py`, `progress.py`, `telegram_text.py` |
| Media and decks | `media.py`, `media_worker.py`, `presentations.py` |
| Providers and notes | `stt.py`, `llm.py`, `http_util.py`, `transcript.py`, `textnorm.py`, `notes.py`, `notes_evidence.py` |
| Documents | `docx_export.py`, `pdf_export.py`, `office.py`, `office_ai.py`, `mdblocks.py`, `bidi.py` |
| Data and billing | `db.py`, `billing.py`, `migrations/v3/` |

## Rules

- Every job receives a unique `job_id` (`job-<submission_id>-<hex>`), isolated per-job state, and explicit lifecycle transitions (`queued` → `downloading` → `extracting` → `transcribing` → `generating_notes` → `uploading` → `completed` / `failed` / `cancelled`).
- Free Office document builds run on a dedicated small lane (`OFFICE_JOB_CONCURRENCY`, default 2) so they never queue behind multi-hour transcription jobs; both lanes share the same stage gates and provider pools below.
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
