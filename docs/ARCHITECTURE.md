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
Evidence gate            notes_evidence.py   signals, table cells, tip bases
        ↓  optional bounded repair + number scrub
        ↓
Word export              docx_export.py RTL booklet
        ↓
Delivery                 telegram_text.py   render → paginate → send
```

A presentation stays one deck-level STT request. Relationship-linked media are
ordered by slide and playback use; the media worker returns source/output
durations and sample-accurate clip spans. Timestamped transcript segments are
attached only when their full interval belongs to one slide-linked span. Plain,
untimed, gap, orphan and boundary-crossing text remains explicitly unmapped.
Presentation note chunks prefer whole slide blocks. See
[PRESENTATIONS.md](PRESENTATIONS.md) for defaults and limits.

The Free Office section is a second, much shorter path off the same queue:

```
Free Office handlers     bot.py         text / .txt upload → a request
        ↓
Text parser              mdblocks.py    headings, bullets, grids, paragraphs
        ↓
Document builders        office.py      .xlsx (openpyxl) and .pptx (python-pptx)
        ↓
Word export              docx_export.py the same booklet engine
        ↓
Arrangement (optional)   office_ai.py   one model request, checked against the source
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
| `presentations.py` | PPTX and legacy-PPT reading, relationship-linked slide audio, per-slide XML fallback, chart values, duration limits and timestamp-to-slide mapping |
| `stt.py` | STT provider adapters, retries, cooldowns, ordered failover |
| `transcript.py` | Fidelity-gated cleanup, paragraphing from word timings, chunking |
| `llm.py` | One LLM client: per-provider request/response, failover, error classes |
| `notes.py` | Prompts, chunk budget, map/reduce, deterministic merge, evidence gate and one-pass repair |
| `notes_evidence.py` | Deterministic signals (numbers with units, terms, URLs), notes audit, table-cell and tip-basis validation, sentence coverage. No network, no model, no dependency |
| `docx_export.py` | RTL Word document: cover, frame, styles, TOC, fonts |
| `mdblocks.py` | The one text parser: headings, bullets, grids, paragraphs |
| `office.py` | The Free Office section: document model, `.xlsx` and `.pptx` builders |
| `office_ai.py` | The optional model layer: slide order and table, checked against the source |
| `progress.py` | Flood-safe progress messages |
| `telegram_text.py` | Markdown → Telegram-safe HTML, UTF-16 pagination |
| `textnorm.py`, `bidi.py` | Persian normalization and mixed-script run splitting |

Dependencies point one way: `bot.py` imports the pipeline modules, the pipeline
modules never import `bot.py`, and `config.py` imports nothing from the package.

### Note generation and evidence

The note model restructures; it does not author. `notes.py` sends one request per
chunk (bounded concurrency), merges the answers deterministically, and then
`notes_evidence.py` checks the result against the transcript: every number+unit,
Latin term and URL must survive (recall), nothing may appear that the source does
not contain (precision), every table cell must trace to the source, and every
practical tip must carry a verbatim `basis`. A deficit triggers at most one
bounded repair request that carries only the missing facts and their source
windows; a number the lecture never contained is removed with the sentence that
states it. `NOTE_STRICT_GROUNDING=false` restores prompt-only behaviour and
`NOTE_QUALITY_GATE=warn` audits without acting. See
[NOTE_QUALITY.md](NOTE_QUALITY.md).

## Concurrency

* **Handlers** are Telethon coroutines. They never block inline: uploads are
  accepted, recorded and queued.
* **Job queue** — `asyncio.Queue` bounded by `MAX_QUEUED_JOBS`, drained by
  `MAX_ACTIVE_JOBS` workers. A full queue is rejected with an explicit
  back-pressure message instead of growing an unbounded backlog.
* **Blocking calls** run through `asyncio.to_thread` (document generation,
  presentation reading). The media worker is a real child process, so a decoder
  crash cannot take the bot down and a timeout can kill it. `MEDIA_WORKER_CONCURRENCY`
  separately bounds heavy media stages (default 1); presentation package and
  scratch-file sizes are capped by the `PRESENTATION_MAX_*` settings.
* **Shutdown** stops intake, rejects queued work, cancels workers, disconnects
  Telegram, then closes SQLite. A job is either fully recorded or fully failed —
  never left in `processing`.
* **Startup recovery** fails every interrupted submission and releases its
  reservation.

## Data model

SQLite in WAL mode, `foreign_keys=ON`, 5 s busy timeout, one connection guarded
by an `asyncio.Lock`; **every** write — including `release_hold`, free grants and
payment review — takes that lock and commits atomically (single-writer, no `BEGIN
IMMEDIATE` needed). Migrations are plain `.sql` files under `migrations/v3/`,
applied once in filename order and recorded in `schema_migrations`; they run
before any handler or worker starts, and `ADD COLUMN` statements whose column
already exists are skipped so a crash mid-migration is safe to retry. Eight
application tables:

| Table | Meaning |
| --- | --- |
| `users` | Telegram id, join date, banned flag, special (unlimited) flag |
| `submissions` | One upload: kind (`audio` \| `video` \| `presentation` \| `office`), filename, status, error, measured duration |
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
3. Failures before complete raw delivery release reserved credit exactly once.
   Failures after raw delivery are `partial`: transcription remains billed and
   counted in usage, independently of booklet completion.
4. Text never reaches a `w:t` element Word cannot store (invalid XML characters
   dropped, soft breaks become spaces).
5. No shell command is built from user input; the media worker is invoked with a
   fixed argument vector and `shell=False`.
6. The Telegram session is guarded by an advisory lock, so two processes cannot
   both poll one bot account.
7. The raw transcript is persisted and delivered before note generation starts,
   so a note-provider outage never costs the user their content.
8. A long list is paged. The transcript is normally delivered as a `.txt`; when
   that upload fails the text fallback pages it in chat up to a bounded 40
   pages, and says so if the bound is ever reached. Callbacks carry an
   *address*, never a payload, and stay inside Telegram's 64-byte
   `callback_data` limit.
9. The Free Office section does not change the content the user wrote. It
   normalises typography (Arabic letter forms to Persian, repeated whitespace)
   and does not carry horizontal-rule lines, and a model may *arrange* the text;
   every line it proposes is checked against the source, and a line that is not
   there is dropped. A heading with no body still becomes a slide. The section
   works with no model at all.

## Where to change things

* **New configuration** — add the field to `Settings`, read it in `from_env`,
  document it in `.env.example`. `tests/test_config_consistency.py` fails
  otherwise.
* **New dependency** — add it to `pyproject.toml`, then run
  `python -m scripts.sync_requirements`. CI checks `requirements.txt` has not
  drifted.
* **New media operation** — implement it in `media_worker.py` and expose it
  through `media.py`. Never add a system binary.
