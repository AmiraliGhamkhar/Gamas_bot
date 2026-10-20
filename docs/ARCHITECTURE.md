# Architecture

Gamas Bot is one Python process with a Telethon client, bounded asyncio job queue and SQLite database. It needs no broker or separate media server. cPanel uses a cron watchdog; Windows PowerShell is suitable for local setup and testing.

## Main flows

```text
Audio/video/deck → bounded queue → PyAV media worker → STT
                 → transcript cleanup → raw text delivered
                 → optional note provider → evidence checks → RTL Word file

Text → Free Office parser → DOCX/XLSX/PPTX/PDF
```

The raw transcript is saved and delivered before note generation. PowerPoint narration is combined into one STT request and mapped to slides only when timestamps support the mapping. Free Office accepts text, not uploaded DOCX/PDF source documents.

## Code map

| Area | Files |
| --- | --- |
| Startup/configuration | `__main__.py`, `config.py`, `launcher.py`, `instance_lock.py` |
| Telegram and queue | `bot.py`, `jobs.py`, `progress.py` |
| Media and decks | `media.py`, `media_worker.py`, `presentations.py` |
| Providers and notes | `stt.py`, `llm.py`, `transcript.py`, `notes.py`, `notes_evidence.py` |
| Documents | `docx_export.py`, `pdf_export.py`, `office.py`, `office_ai.py`, `mdblocks.py` |
| Data and billing | `db.py`, `billing.py`, `migrations/v3/` |

## Operating rules

- The in-memory queue is bounded by `MAX_ACTIVE_JOBS` and `MAX_QUEUED_JOBS`; excess work is rejected.
- The media worker runs with a timeout and no shell. `av` supplies media libraries; system FFmpeg is not required.
- SQLite migrations run at startup. Billing writes are transactional; a job hold is settled or released according to the raw-transcript delivery milestone.
- A lock on the Telegram session prevents duplicate polling. Keep the session and database on a local writable filesystem.
- Provider output is untrusted and is checked before a note is delivered. See [Billing](BILLING.md), [Security](SECURITY.md) and [Note quality](NOTE_QUALITY.md).
