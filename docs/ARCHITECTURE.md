# Architecture

Gamas Bot turns a lecture upload (audio, video or PowerPoint) into a Persian,
right-to-left Word booklet plus the raw transcript. It is a single Python
process: a Telethon client, a bounded in-process job queue, and a SQLite
database. There is no broker, no worker fleet and no external queue, because
the workload of one deployment does not justify them.

## Pipeline

```
Telegram Interface            gamas_bot/bot.py (handlers, menus, delivery)
        ↓
Job intake + back-pressure    jobs.py (bounded asyncio.Queue + fixed worker pool)
        ↓
Media intake                  media.py (async child process, hard timeout)
        ↓
Python media worker           media_worker.py (PyAV — no ffmpeg binary)
        ↓
STT provider layer            stt.py (Speechmatics / Deepgram / OpenAI-compatible)
        ↓
Raw text engine               transcript.py (paragraphing, fidelity gate)
        ↓
Progressive delivery          raw .txt + chat preview → the user, immediately
        ↓
Notes (map-reduce)            llm.py + notes.py (one map per chunk, one reduce)
        ↓
DOCX / PPTX export            docx_export.py, presentations.py
        ↓
Delivery                      telegram_text.py (render → paginate → send)
```

Cross-cutting concerns: `config.py` (settings), `db.py` (users, submissions,
plans, payments, ledger, holds, audit), `billing.py` (plan catalog),
`logging_config.py` (structured logs + `job_id`), `instance_lock.py` (single
Telegram session guard). Provider credentials are read from the environment
only — nothing secret is stored in the database.

## Module map

| Module | Responsibility |
| --- | --- |
| `__main__.py`, `launcher.py` | Entrypoint, self-check, single-instance launch |
| `config.py` | The single configuration model; validates every environment value |
| `bot.py` | Telegram handlers, menus, small admin panel, delivery orchestration |
| `jobs.py` | Bounded queue, worker pool, clean shutdown and recovery |
| `db.py` | Eight tables, WAL, migrations, ledger, holds, payments, audit |
| `billing.py` | Canonical plan catalog and formatting helpers |
| `stt.py` | Three STT providers, ordered failover, in-memory cooldowns, word timings |
| `transcript.py` | The raw text engine: paragraphing, typography cleanup, fidelity gate |
| `llm.py` | One thin LLM client (OpenAI-compatible, Gemini, Anthropic) |
| `notes.py` | Map-reduce note generation with deterministic merge fallback |
| `docx_export.py` | RTL Word document: cover, styles, link-only TOC, page numbers |
| `media.py` | Async child-process media worker with a hard timeout |
| `media_worker.py` | PyAV probe/extract/merge (in-process, no system binaries) |
| `presentations.py` | PPTX/legacy-PPT reading, zip-bomb limits, slide audio |
| `progress.py` | Flood-safe progress messages |
| `telegram_text.py` | Markdown → Telegram-safe HTML and UTF-16 pagination |
| `textnorm.py`, `bidi.py` | Persian normalization and mixed-script run splitting |

Dependency direction is acyclic: `bot.py` imports the pipeline modules, the
pipeline modules never import `bot.py`, and `config.py` imports nothing from
the package.

## Concurrency model

* **Handlers** are Telethon coroutines. They never do CPU-heavy or blocking
  work inline: uploads are accepted, recorded, and queued.
* **Job queue**: `asyncio.Queue(maxsize=MAX_QUEUED_JOBS)` drained by
  `MAX_ACTIVE_JOBS` workers. A full queue is rejected with an explicit
  back-pressure message instead of growing an unbounded backlog.
* **Job semaphore**: a second bound on *active* jobs, so a job started outside
  the worker pool still cannot exceed the configured concurrency.
* **Blocking library calls** run through `asyncio.to_thread` (document
  generation, presentation reading). The media worker is a real child process,
  so a decoder crash cannot take the bot down and a timeout can kill it.
* **Shutdown** stops intake first, then rejects whatever is still queued, then
  cancels workers — a job is either fully recorded or fully failed, never
  silently dropped with a `processing` row left behind.
* **Startup** recovers interrupted work: every `pending`/`processing`
  submission is failed and its usage reservation released.

## Data model

SQLite in WAL mode with `foreign_keys=ON`, a busy timeout and a single
connection guarded by an `asyncio.Lock`; write transactions use
`BEGIN IMMEDIATE`. Migrations are plain `.sql` files in `migrations/v3/`,
applied once in filename order and recorded in `schema_migrations`. The eight
tables are `users`, `submissions`, `transcriptions`, `plans`, `payments`,
`ledger`, `holds` and `audit`; the v2 database is not migrated in place —
`scripts/import_v2.py` copies users, credit and the ledger across when needed.

Billing is append-only, never a mutated balance: `ledger` rows of kind `credit`
(seconds granted, optionally expiring) and `debit` (seconds consumed by a
finished job), plus `holds` rows (seconds reserved by a running job).
`available = credits(not expired) − debits − holds(not released)`. A job
reserves before it runs, settles for the measured duration on success and
releases everything on any failure or cancellation — which is why a failed job
cannot silently consume credit.

## Invariants the code is built around

1. A provider result is never attributed to the wrong provider.
2. The table of contents is link-only; page numbers exist only as the native
   Word `PAGE` field in the footer. TOC page numbers are never guessed — there
   is no layout engine in v3 to measure them.
3. A failed job releases its reserved credit exactly once.
4. Text never reaches a `w:t` element that Word cannot store (invalid XML
   characters are dropped, soft breaks become spaces).
5. No shell command is built from user input; the media worker is invoked with
   a fixed argument vector and `shell=False`.
6. The Telegram session is guarded by an advisory lock so two processes cannot
   both poll the same bot account.
7. The raw text can be *reformatted*, never *lost*: every `transcript.py`
   transform runs inside a fidelity gate that compares content fingerprints
   (digit groups, Latin tokens, ZWNJ, non-space characters) and rejects any
   transform that changes them.
8. The raw transcript is delivered before note generation starts. An LLM
   outage degrades the job to raw-only; it can never withhold the transcript
   or silently consume credit.
9. Provider credentials come from the environment only. No secret is ever
   written to the database or a log line.

## Where to change things

* New configuration: add the field to `Settings`, read it in `from_env`, add it
  to `.env.example`. `tests/test_config_consistency.py` fails otherwise.
* New dependency: add it to `pyproject.toml`, run
  `python -m scripts.sync_requirements`. `tests/test_dependency_consistency.py`
  fails otherwise.
* New media operation: implement it in `media_worker.py` and expose it through
  `media.py` — never add a system binary.
