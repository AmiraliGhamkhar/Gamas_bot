# Architecture

Gamas Bot turns a lecture upload (audio, video or PowerPoint) into a Persian
right-to-left Word booklet plus the raw transcript. It is a single Python
process: a Telethon client, a bounded in-process job queue and a SQLite
database. There is no broker, no worker fleet and no external queue, because one
deployment's workload does not justify them.

## Pipeline

```
Telegram handlers        bot.py         accept upload, menus, delivery
        ↓
Job queue                jobs.py        bounded asyncio.Queue + fixed workers
        ↓
Media intake             media.py       child process, hard timeout
        ↓
Media worker             media_worker.py  PyAV — no ffmpeg binary
        ↓
Speech-to-text           stt.py         Speechmatics / Deepgram / OpenAI-compatible
        ↓
Transcript engine        transcript.py  cleanup, paragraphing, chunking
        ↓  raw .txt delivered here
Note generation          notes.py + llm.py   map over chunks, optional reduce
        ↓
Word export              docx_export.py RTL booklet
        ↓
Delivery                 telegram_text.py   render → paginate → send
```

## Module map

| Module | Responsibility |
| --- | --- |
| `__main__.py` | CLI entry point: `--check`, run, media-worker check |
| `launcher.py` | Detached-process start used by cron and Passenger |
| `instance_lock.py` | Advisory lock on the session path; one polling instance |
| `config.py` | The single configuration model; validates every environment value |
| `logging_config.py` | Structured logs, rotation, `job_id` correlation |
| `db.py` | Schema, migrations, ledger, payments, plans, audit |
| `billing.py` | Plan catalog and formatting helpers |
| `jobs.py` | Queue bounds, back-pressure, graceful shutdown, startup recovery |
| `bot.py` | Telegram handlers, menus, admin panel, delivery orchestration |
| `media.py` | Runs the media worker as a child process with a hard timeout |
| `media_worker.py` | PyAV probe / extract / merge (in-process, no system binaries) |
| `presentations.py` | PPTX and legacy-PPT reading, zip-bomb limits, slide audio |
| `stt.py` | STT provider adapters, retries, cooldowns, ordered failover |
| `transcript.py` | Fidelity-gated cleanup, paragraphing from word timings, chunking |
| `llm.py` | One LLM client: per-provider request/response, failover, error classes |
| `notes.py` | Prompts, chunk budget, map/reduce, deterministic merge |
| `docx_export.py` | RTL Word document: cover, frame, styles, TOC, fonts |
| `progress.py` | Flood-safe progress messages |
| `telegram_text.py` | Markdown → Telegram-safe HTML, UTF-16 pagination |
| `textnorm.py`, `bidi.py` | Persian normalization and mixed-script run splitting |

Dependencies point one way: `bot.py` imports the pipeline modules, the pipeline
modules never import `bot.py`, and `config.py` imports nothing from the package.

## Concurrency

* **Handlers** are Telethon coroutines. They never block inline: uploads are
  accepted, recorded and queued.
* **Job queue** — `asyncio.Queue` bounded by `MAX_QUEUED_JOBS`, drained by
  `MAX_ACTIVE_JOBS` workers. A full queue is rejected with an explicit
  back-pressure message instead of growing an unbounded backlog.
* **Blocking calls** run through `asyncio.to_thread` (document generation,
  presentation reading). The media worker is a real child process, so a decoder
  crash cannot take the bot down and a timeout can kill it.
* **Shutdown** stops intake, rejects queued work, cancels workers, disconnects
  Telegram, then closes SQLite. A job is either fully recorded or fully failed —
  never left in `processing`.
* **Startup recovery** fails every interrupted submission and releases its
  reservation.

## Data model

SQLite in WAL mode, `foreign_keys=ON`, 5 s busy timeout, one connection guarded
by an `asyncio.Lock`; writes use `BEGIN IMMEDIATE`. Migrations are plain `.sql`
files under `migrations/v3/`, applied once in filename order and recorded in
`schema_migrations`. Eight application tables:

| Table | Meaning |
| --- | --- |
| `users` | Telegram id, join date, banned flag, special (unlimited) flag |
| `submissions` | One upload: kind, filename, status, error, measured duration |
| `transcriptions` | The transcript text and the generated `notes_json` |
| `plans` | Selling plans: hours, price, validity, `is_custom`, active |
| `payments` | Card-to-card requests, receipt path, review outcome |
| `ledger` | Append-only credit events: grants, charges, adjustments |
| `holds` | What a running job currently has reserved |
| `audit` | Admin actions with subject and reason |

Billing is an append-only ledger, never a mutated balance. A job's lifecycle is
`hold → (settle | release)`; see [BILLING.md](BILLING.md).

## Invariants

1. A provider result is never attributed to the wrong provider.
2. A table-of-contents page number is only ever a *measured* one; numbers are
   never guessed, so v3 prints links instead of unverified numbers.
3. A failed job releases its reserved credit exactly once.
4. Text never reaches a `w:t` element Word cannot store (invalid XML characters
   dropped, soft breaks become spaces).
5. No shell command is built from user input; the media worker is invoked with a
   fixed argument vector and `shell=False`.
6. The Telegram session is guarded by an advisory lock, so two processes cannot
   both poll one bot account.
7. The raw transcript is persisted and delivered before note generation starts,
   so a note-provider outage never costs the user their content.
8. A long list is paged, never cut. Callbacks carry an *address*, never a
   payload, and stay inside Telegram's 64-byte `callback_data` limit.

## Where to change things

* **New configuration** — add the field to `Settings`, read it in `from_env`,
  document it in `.env.example`. `tests/test_config_consistency.py` fails
  otherwise.
* **New dependency** — add it to `pyproject.toml`, then run
  `python -m scripts.sync_requirements`. CI checks `requirements.txt` has not
  drifted.
* **New media operation** — implement it in `media_worker.py` and expose it
  through `media.py`. Never add a system binary.
