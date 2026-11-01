# Maintainer guide

Gamas Bot is a Persian Telegram study assistant. Read the relevant
[guide](docs/README.md) and implementation before editing. Code is the source of
truth; report documentation mismatches. Keep changes small and practical.

## Platform and scope

- Support Python 3.11–3.13, Windows PowerShell and Linux cPanel shared hosting.
- Keep Telethon, bounded asyncio work, SQLite WAL and PyAV. No system binaries,
  local OCR engines, brokers, external databases, web frameworks or payment gateways.
- Do not modify production `.env`, `data/`, sessions, receipts or backups.
  `migrations/v2/` is read-only legacy material.
- Code/comments/logs/docs use English; user messages and LLM prompts use Persian.
  Store UTC timestamps; use monotonic clocks for elapsed durations.

## Runtime and security invariants

- One instance per token/session; preserve locking and exit codes.
- Jobs have isolated IDs/state, explicit lifecycle transitions and bounded failure
  categories. Admission caps and submission inserts are atomic; queue overflow refuses
  work with a clear Persian explanation.
- Preserve independent stage/provider caps. Never hold one stage slot while waiting
  on another. Available fallback capacity receives overflow immediately; all-full
  waits are bounded. Release leases in `finally` and keep release idempotent.
- Never block the event loop with file/image/document work. Use `fileio.run_file_io`
  or bounded threads; media uses bounded child processes, fixed argv, no shell and
  no network media protocols. Validate ZIP names, entries, symlinks and expanded sizes.
- Bound uploads, source text, drafts, response bodies, timeouts and outputs. Refuse
  overflow; never silently truncate. See [limits](docs/CONFIGURATION.md).
- Serialize `BEGIN IMMEDIATE` writes. Reports use the query-only read connection,
  never mutate state or take the write lock. Keep foreign keys enabled and IDs exact int64.
- Shutdown: stop intake, reject queued jobs, join active delivery/document/receipt/
  Office/learning work, then close SQLite. Recovery follows the instance lock:
  fail interrupted work, release holds, restore gifts/refund pending learning once,
  clean stale temporary work. Clean temporary artifacts in `finally`.
- Keep private paths outside web roots, owner-only on cPanel. Validate receipt paths,
  image signatures/pixel bounds and re-encode before storage.
- Secrets come only from env/`.env`. Redact errors; logs contain references/metadata,
  never transcript/document/receipt contents. No user input reaches a shell.
- Admin authorization fails closed; private callbacks check owner ID. Audit and
  cooldown-limit changes, protecting self/peer admins. Only channel-membership
  infrastructure errors fail open; confirmed non-membership blocks.
- Respect Telegram UTF-16 message/button limits. Render tables before pagination.
  Keep text helpers deterministic and idempotent.

## Billing invariants

- Credit is an append-only integer-second, lot-aware ledger. Never rewrite history;
  preserve earliest-expiry allocations and held lots. Refunds create non-expiring lots.
- Reserve measured source audio before STT; refuse unmeasurable duration. Charge
  only after complete transcript delivery. Pre-delivery failure releases holds;
  later booklet failure is partial and leaves the transcription charge intact.
- PowerPoint inserted silence is free; narration-free decks remain free with delivery recorded.
- Paid learning debit, allocations and pending charge commit atomically. Saving
  finalizes with the artifact; failure/cancel/preview expiry/recovery refunds once.
  Failed refunds remain recoverable; never report success before commit.
- Preserve one independent free quiz and AI deck per user, atomically claimed with
  owned tokens. Older jobs cannot undo newer claims/admin resets. Unlimited uses
  neither gifts nor credit. Check admission before debit; one generation per user.
- Preserve minimum valid counts (3 questions/5 cards), quiz/deck admission caps and
  disclosed coverage/count shortfalls. Keep paid/free previews recoverable.
- Receipt upload never grants credit. Preserve forward-only states, frozen plan
  snapshots, receipt/reviewer/reason checks and unique payment-linked credit.
  Approval/status/audit commit together; notifications never repeat credit.
- Office/document-note/export actions never debit wallet or learning gifts.
  Separate narration/learning keeps its billing; operator provider costs may apply.
- Unlimited releases holds; bans reject new/queued work, not running jobs.
  Leaderboard gifts use paid plans only. Pending-review receipts never auto-expire.

## Source, Persian text and documents

- Deliver the complete raw transcript before notes. Failed map chunks must not
  produce partial booklets. Validate untrusted provider output; preserve bounded
  grounding/completeness/repair/compile checks, source order and facts.
- Preserve source numbers/units and Persian ZWNJ. Display normalization is
  idempotent; lossy comparison normalization is not for displayed/stored content.
- Store logical Unicode order, never reversed BiDi. Keep mixed RTL/LTR Word runs,
  selectable PDF text, source-only locators and grounded learning questions.
- Use shared `mdblocks` parsing; normalize invalid controls consistently across
  Office formats. Excel values are literal strings, never executable formulas.
- Extraction dedup/cache and callbacks stay owner-scoped. Preserve page/section/
  slide locators and originals in Telegram. Narration starts only explicitly.
- Office drafts remain bounded, private and TTL-limited. Text-to-PowerPoint creates
  a new deck, not a visual conversion; incomplete AI plans fall back safely.
- OCR stays opt-in and remote; disabled means no page uploads. Do not promise
  document-cache deletion through library pruning or third-party retention guarantees.

## Implementation and verification

- Use typed Python, domain helpers and existing exception patterns. Never reformat
  unrelated code or run a repository-wide formatter.
- Declare bounded dependencies in `pyproject.toml`; generate `requirements.txt`
  with `python -m scripts.sync_requirements`, never hand-edit it. Justify additions.
- New settings flow through config and are documented in `.env.example` and
  `docs/CONFIGURATION.md`. `--check` stays offline and side-effect-free.
- Add numbered migrations in `migrations/v3/`; never edit applied SQL. Test fresh
  databases, populated upgrades, foreign keys and replay safety.
- Add regression/failure/race tests for behavior changes. Use no live keys or
  production data. See [Testing](docs/TESTING.md) for both shell variants.
- Full gate: Ruff, mypy, pytest, unittest discovery, requirement sync, compileall
  and media-worker check. Report actual results/limits; partial runs are not full passes.
- Update affected docs and `docs/CHANGELOG.md`. Report changes, verification and
  unresolved risks. Do not commit generated artifacts, secrets or private fixtures.
