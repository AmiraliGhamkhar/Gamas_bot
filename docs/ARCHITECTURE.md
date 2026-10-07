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
Job intake + back-pressure    bounded asyncio.Queue + fixed worker pool
        ↓
Media intake                  media.py (async child process, hard timeout)
        ↓
Python media worker           media_worker.py (PyAV — no ffmpeg binary)
        ↓
STT provider layer            stt.py (Speechmatics / Deepgram / OpenAI-compatible)
        ↓
Normalization                 textnorm.py, units.py
        ↓
Structuring                   structuring.py (chunk → global context → editorial)
        ↓
QA                            qa.py, units.semantic_coverage
        ↓
DOCX / PPTX export            docx_export.py, presentations.py
        ↓
Delivery                      telegram_text.py (render → paginate → send)
```

Cross-cutting concerns: `config.py` (settings), `database.py` (accounting,
plans, payments, provider credentials, audit), `billing.py` (plan catalog),
`provider_credentials.py` (Fernet-encrypted key pool), `provider_health.py`
(cached health checks), `logging_config.py` (structured logs + `job_id`),
`instance_lock.py` (single Telegram session guard).

## Module map

| Module | Responsibility |
| --- | --- |
| `bot.py` | Telegram handlers, job queue, billing/admin UI, delivery orchestration |
| `telegram_text.py` | Markdown → Telegram-safe HTML and UTF-16 pagination |
| `config.py` | The single configuration model; validates every environment value |
| `database.py` | Schema, migrations, accounting ledger, payments, credentials, audit |
| `billing.py` | Canonical plan catalog and formatting helpers |
| `docx_export.py` | RTL Word document: cover, frame, styles, static TOC, fonts |
| `structuring.py` | Transcript/slide → structured notes (chunking, prompts, repair) |
| `editorial.py` | Global-context outline and cross-chunk compilation |
| `qa.py` | Deterministic note-quality gates |
| `units.py` | Number/unit extraction and semantic coverage |
| `stt.py` | Provider abstraction, retries, fallback, credential rotation |
| `media.py` | Async child-process media worker with a hard timeout |
| `media_worker.py` | PyAV probe/extract/merge (in-process, no system binaries) |
| `presentations.py` | PPTX/legacy-PPT reading, zip-bomb limits, slide audio |
| `progress.py` | Flood-safe progress messages |
| `provider_credentials.py` | Encrypted provider-key storage and rotation |
| `provider_health.py` | Cached, secret-free provider health checks |
| `textnorm.py`, `bidi.py` | Persian normalization and mixed-script run splitting |

Dependency direction is acyclic: `bot.py` imports the pipeline modules, the
pipeline modules never import `bot.py`, and `config.py` imports nothing from
the package.

## Concurrency model

* **Handlers** are Telethon coroutines. They never do CPU-heavy or blocking
  work inline: uploads are accepted, recorded, and queued.
* **Job queue**: `asyncio.Queue(maxsize=MAX_PENDING_JOBS)` drained by
  `MAX_CONCURRENT_JOBS` workers. A full queue is rejected with an explicit
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

SQLite in WAL mode with `foreign_keys=ON`, a 5 s busy timeout and a single
connection guarded by an `asyncio.Lock`; write transactions use
`BEGIN IMMEDIATE`. Migrations are plain `.sql` files applied once in filename
order and recorded in `schema_migrations`.

Billing is an append-only ledger, never a mutated balance: `entitlements`
(what a user owns), `usage_reservations` (what a running job holds) and
`usage_ledger` (every reserve/consume/release/grant event with its reason).
`reserve → finalize` charges the measured duration; `reserve → release` returns
it untouched on any failure or cancellation, which is why a failed job cannot
silently consume credit.

## Invariants the code is built around

1. A provider result is never attributed to the wrong provider.
2. A page number in the table of contents is only ever a *measured* page
   number; page numbers are never guessed or interpolated.
3. A failed job releases its reserved credit exactly once.
4. Text never reaches a `w:t` element that Word cannot store (invalid XML
   characters are dropped, soft breaks become spaces).
5. No shell command is built from user input; the media worker is invoked with
   a fixed argument vector and `shell=False`.
6. The Telegram session is guarded by an advisory lock so two processes cannot
   both poll the same bot account.

## Where to change things

* New configuration: add the field to `Settings`, read it in `from_env`, add it
  to `.env.example`. `tests/test_config_consistency.py` fails otherwise.
* New dependency: add it to `pyproject.toml`, run
  `python -m scripts.sync_requirements`. `tests/test_dependency_consistency.py`
  fails otherwise.
* New media operation: implement it in `media_worker.py` and expose it through
  `media.py` — never add a system binary.
