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
| Startup/configuration | `__main__.py`, `config.py`, `launcher.py`, `instance_lock.py`, `logging_config.py` |
| Telegram and queue | `bot.py`, `jobs.py`, `progress.py`, `telegram_text.py` |
| Media and decks | `media.py`, `media_worker.py`, `presentations.py` |
| Providers and notes | `stt.py`, `llm.py`, `http_util.py`, `transcript.py`, `textnorm.py`, `notes.py`, `notes_evidence.py` |
| Documents | `docx_export.py`, `pdf_export.py`, `office.py`, `office_ai.py`, `mdblocks.py`, `bidi.py` |
| Data and billing | `db.py`, `billing.py`, `migrations/v3/` |

## Rules

- The queue is bounded by `MAX_ACTIVE_JOBS` and `MAX_QUEUED_JOBS`; dispatch is round-robin across
  users, and a user's own jobs keep their order.
- Each user may hold at most `MAX_ACTIVE_JOBS_PER_USER` pending/processing submissions; the check
  and insert share one write transaction.
- The media worker runs with a timeout and no shell. `av` supplies the media libraries.
- SQLite migrations run at startup; billing writes are transactional.
- A lock on the Telegram session prevents duplicate polling. Keep the session and database on a
  local writable filesystem.
- Provider output is untrusted and checked before delivery.
