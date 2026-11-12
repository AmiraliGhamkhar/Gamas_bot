# Development

One Telethon client, bounded asyncio work, SQLite in WAL mode, PyAV for media.
No broker, web framework, external database or system Office/media binary.

## Flows

```text
audio/video → admission + hold → queue → media → STT → transcript delivered
            → optional grounded notes → RTL Word + PDF → Telegram library
PDF/DOCX/PPTX → owner-scoped extraction → SQLite text cache → notes/exports
text/CSV/MD → private Office draft → bounded build → Word/Excel/PPT/PDF
wallet → order + receipt → admin review → transactional credit
Studio edit → validated StructuredNotes → revision → PDF + DOCX bytes
```

## Code map (`gamas_bot/`)

| Area | Modules |
| --- | --- |
| Startup/config | `__main__`, `config`, `launcher`, `instance_lock`, `logging_config` |
| Telegram workflows | `bot`, `bot_ui`, `bot_jobs`, `bot_documents`, `bot_learning`, `bot_wallet`, `bot_support`, `bot_admin` |
| Contracts/routing | `protocols`, `bot_deps`, `routing`, `conversation_state`, `intake` |
| Queue/transfers | `jobs`, `capacity`, `progress`, `telegram_transfer`, `telegram_text` |
| Media/providers | `media`, `media_worker`, `presentations`, `stt`, `llm`, `llm_profiles`, `http_util` |
| Notes/documents | `transcript`, `notes`, `notes_evidence`, `doc_extract`, `ocr`, `office*`, `docx_export`, `notes_pdf`, `pdf_export`, `pdf_common` |
| Mini App | `mini_app` (WSGI + `initData` auth), `mini_app_store` (per-request SQLite), `mini_app/` bundle |
| Storage/billing | `db` facade, `db_*` facets, `billing`, `subscription`, `learning`, `referrals` |

## Invariants to preserve

- **One instance per token/session.** Keep locking and exit codes (`2`
  misconfigured, `3` already running).
- **Bounded everything.** Admission caps, queues, uploads, extracted text,
  provider bodies, timeouts and outputs. Overflow is refused, never truncated.
  Release stage slots in `finally`, never hold one slot while waiting on another.
- **Never block the event loop.** File/image/document work goes through
  `fileio.run_file_io` or bounded threads; media runs in bounded child processes
  with a fixed argv, no shell.
- **Serialized writes.** `BEGIN IMMEDIATE` for writes; reporting uses the
  read-only connection and must never mutate state.
- **Orderly shutdown.** Stop intake, reject queued work, join active work, then
  close SQLite. Recovery runs only after the instance lock is held.
- **Secrets only from env/`.env`.** Redact errors; logs carry metadata, never keys,
  prompts, transcripts or receipts.
- **Billing is append-only.** Credit is integer seconds with lot-aware
  allocations; refunds create new lots; receipts never grant credit.
- **Persian text stays in logical Unicode order.** Preserve numbers, units and
  ZWNJ; display normalization is idempotent and never applied to stored content.
- **Documents come from validated models.** `StructuredNotes.from_payload`
  rejects empty/invalid structure before booklet rendering. Final PDF bytes are
  parsed in strict mode and must contain at least one page; DOCX must be a valid
  ZIP package with its required document parts. Only validated bytes are
  persisted/advertised. Format failures are recorded as partial output and do
  not erase the retained transcript or a successful alternate format.

## Dependencies

`pyproject.toml` is the single source of truth; `requirements.txt` is generated:

```bash
python -m scripts.sync_requirements
```

Runtime dependencies stay bounded ranges with a justification in
`pyproject.toml`. `curl-cffi` is the opt-in `ocr` extra; dev tools live in `dev`.
No dependency may require a system binary on shared hosting.

## Migrations

- Add a numbered file to `migrations/v3/`; never edit an applied migration.
- Migrations run at startup and are additive where possible.
- Test three paths: a fresh database, a populated upgrade, and a replay.
  `tests/test_migration_schema.py` covers the upgrade cases; CI asserts a fresh
  database applies every file and leaves exactly `APPLICATION_TABLES`.
- `migrations/v2/` is read-only legacy material for `scripts/import_v2.py`.
- There are no downgrade scripts: roll back code **and** data together.

## Conventions

- Typed Python, domain helpers, existing exception patterns. Never reformat
  unrelated code and do not run a repository-wide formatter.
- Code, comments, logs and docs in English; user messages and LLM prompts in
  Persian.
- UTC timestamps; monotonic clocks for elapsed durations.
- New settings go through `config.py`, `.env.example` and
  [Configuration](CONFIGURATION.md); `--check` stays offline and side-effect free.
- Behaviour changes need regression, failure or race tests. Update the affected
  docs and [CHANGELOG](CHANGELOG.md).

## Verification

See [Testing](TESTING.md) for the full gate on both shells. Report what actually
ran; a partial run is not a pass.

## Additional developer CLIs

- `python -m scripts.credit_price_preview` prints the tariff and plan catalogue.
- `python -m scripts.smoke_avalai` checks AvalAI metadata; generation is opt-in and uses provider quota.
- `python -m scripts.evaluate_diarization` evaluates diarization and transcription from a reference and hypothesis.
