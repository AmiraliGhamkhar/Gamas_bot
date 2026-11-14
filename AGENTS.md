# Maintainer guide

Gamas Bot is a Persian Telegram study assistant for Windows (PowerShell) and
shared Linux hosting (cPanel). Read the relevant [guide](docs/README.md) and the
code before editing. **Code is the source of truth**; fix docs that disagree.
Keep changes small and practical.

## Platform

- Python 3.11–3.13, PowerShell on Windows, cPanel shared hosting, one process per
  bot token.
- Telethon, bounded asyncio work, SQLite WAL, PyAV. No system binaries, brokers,
  external databases, web frameworks or payment gateways.
- Do not modify production `.env`, `data/`, sessions, receipts or backups.
  `migrations/v2/` is read-only legacy material.
- Code, comments, logs and docs in English; user messages and LLM prompts in
  Persian. Store UTC; use monotonic clocks for elapsed time.

## Runtime

- Preserve single-instance locking, exit codes (`2` misconfigured, `3` already
  running) and orderly shutdown (stop intake → reject queued → join active →
  close SQLite).
- Jobs have isolated state and explicit lifecycle. Admission and submission
  inserts are atomic; overflow is refused with a clear Persian explanation.
- Keep stage/provider caps independent; release slots in `finally` and keep the
  release idempotent. Never hold one slot while waiting for another.
- Never block the event loop: `fileio.run_file_io` or bounded threads for files,
  bounded child processes with fixed argv and no shell for media.
- Bound uploads, source text, drafts, bodies, timeouts and outputs. Refuse
  overflow; never silently truncate.
- Serialize `BEGIN IMMEDIATE` writes; reporting uses the query-only connection.
  Foreign keys on, IDs exact int64.
- Startup recovery follows the instance lock: fail interrupted work, release
  holds, restore gifts, refund pending learning once, clean stale temp files.

## Security

- Secrets only from env/`.env`; redact errors. Logs carry references, ids, counts
  and durations — never keys, prompts, transcript/document text or receipts.
- Private paths stay outside web roots; `600`/`700` on cPanel; validated receipt
  paths and image signatures before storage.
- Admin authorization fails closed; private callbacks check ownership; audited,
  cooldown-limited changes never target self or peer admins.
- Only channel-membership **infrastructure** errors fail open. Confirmed
  non-membership blocks.

## Billing (append-only)

- Credit is an integer-second, lot-aware ledger. Never rewrite history; preserve
  earliest-expiry allocations and held lots; refunds create non-expiring lots only
  when all contributing lots were non-expiring.
- Reserve measured audio before STT; charge only after the transcript is stored
  and delivered; pre-delivery failure releases the hold.
- Learning debits commit with a durable pending charge; save finalizes; failure,
  cancel, preview expiry and recovery refund at most once.
- Office, document notes and exports never spend credit or gifts, though they may
  cost the operator provider money.
- Receipts never grant credit; approval, credit and audit commit together.

## Documents and Persian text

- Deliver the full raw transcript before notes. Failed chunks never produce a
  partial booklet; truncated completions are refused.
- Keep grounding/completeness/repair gates and source order. Preserve source
  numbers, units and ZWNJ; display normalization must stay idempotent.
- Store logical Unicode order — never reversed BiDi. PDF and DOCX render from the
  same validated `StructuredNotes` model.
- Excel values are literal strings, never executable formulas.
- OCR stays opt-in and remote; disabled means no page uploads.

## Verification

- Typed Python, domain helpers, existing exception patterns. Never reformat
  unrelated code.
- Declare dependencies in `pyproject.toml`; regenerate `requirements.txt` with
  `python -m scripts.sync_requirements`. Justify additions.
- New settings flow through `config.py`, `.env.example` and
  `docs/CONFIGURATION.md`; `--check` stays offline and side-effect free.
- Add numbered migrations in `migrations/v3/`; never edit applied SQL. Test
  fresh, populated and replay paths.
- Add regression/failure/race tests. Full gate in [Testing](docs/TESTING.md):
  Ruff, mypy, pytest, unittest discovery, requirement sync, compileall and the
  media-worker check. Report what actually ran; partial runs are not passes.
- Keep the doc set short: update the affected guide, this file only if a rule
  changed, and `docs/CHANGELOG.md` for behaviour. Keep
  `tests/test_config_consistency.py` green — it checks local links and that the
  docs do not advertise removed settings.
- Do not commit generated artifacts, secrets or private fixtures.
