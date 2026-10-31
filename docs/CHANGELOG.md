# Changelog

## Unreleased — Free Office restoration

- Restored the optional **🗂 آفیس رایگان** menu and private, owner-scoped message/text-file drafts for
  `.txt`, `.md`, `.markdown`, `.csv` and `.tsv`; generated files use the existing RTL Word/Excel/
  PowerPoint/PDF builders and library references. `OFFICE_ENABLED` is `true` by default.
- Reconnected uploaded PDF/DOCX/PPTX cards to full source-grounded booklets, separate compact
  revision notes, all-text exports, original download and library navigation. Full and compact notes
  offer both Word and selectable-text PDF. PowerPoint audio remains explicit and separate.
- Added strict source-completeness fallback for planned decks, lossless source limits, formula-like
  XLSX cells stored as text, private draft expiry, bounded Office work and a library migration that
  preserves existing rows. Free Office/document-note operations do not write billing or free-use
  state; deterministic fallbacks remain available with no note provider.
- Updated README and Office/architecture/configuration/cPanel/testing/security documentation and
  added end-to-end Office flow and CSV formula-injection regression coverage.

## Unreleased — workflow re-audit

- Migration 015 records paid quiz/deck operations atomically with their debits. Artifact save
  finalizes them transactionally; cancellation, expiry and startup recover an unsaved charge once.
  Legacy pre-migration paid debits cannot be automatically matched to interrupted previews.
- Telegram callback answers now preserve specific alerts; textless narrated presentations expose an
  explicit audio-processing action; ranged downloads validate every range and complete short writes.
- Admission fingerprints cover queued/running uploads; shutdown joins admission and stops queued
  progress. Partial receipts are removed on cancellation, while a committed receipt remains attached.
- Active quiz sources survive library pruning; transient Telegram forwarding failures no longer
  permanently disable a library item. Old unavailable flags are repaired after a successful retry.
- Document splitting is linear in the extracted text size; identical concurrent uploads share an
  extraction, document parsers are concurrency-limited, extracted text is capped at 5,000,000
  characters, and progress edits cannot occupy a model slot. Learning chunk concurrency now honors
  its full configured 1–8 range. The separate document cache still has no retention/delete policy.

## Unreleased — v2 (hubs, identity, free first try, document ingestion, quality)

- **Admin hubs (R1).** The nine sibling admin buttons collapse into three hubs with new callback
  data — `adm:hub:pay`, `adm:hub:usr`, `adm:hub:sys` — each opening on its most-used view:
  pending receipts, user search, live system status. History, statistics, pending-receipt users,
  the blocked list, user cards and processing details are one tap deeper. Broadcast, support
  management, the audit trail and the leaderboard are untouched, and every old callback
  (`adm:pay`, `adm:ph`, `adm:pd`, `adm:users`, `adm:ovu:`, `adm:ovp:`, `adm:ovpay:`, `adm:ovs`) still
  routes, so a pre-deploy keyboard never dead-ends. Billing transitions, the ledger and the
  `ADMIN_IDS` check are unchanged.
- **Identity (R2).** Migration 013 adds `users.username`, `users.last_name` and `users.last_seen_at`.
  The upsert refreshes them on every interaction and every admin list or card renders
  `@username · شناسه: <id> · <name>` (em dash when Telegram reports no handle). Telegram ids are
  `INTEGER` end to end, so values above 2^53 round-trip exactly; a foreign-key integrity check
  guards user references, and a double `/start` never creates a second row.
- **Free first try (R3).** The first AI flashcard deck and the first quiz are free for every user —
  full quality, same models, claimed by one atomic `UPDATE … WHERE used = 0` before any model call.
  Failures, cancellation, preview expiry and failed delivery restore the matching claim; a unique
  token prevents stale refunds after reset. Migration 014 atomically finalizes claims with artifact
  persistence and recovers unfinished free claims at startup. Admin reset is confirmed and audited,
  and both entry screens carry a «🎁 اولین بار رایگان» badge.
- **Document ingestion (R4).** PDF, `.docx` and PowerPoint text (plus best-effort legacy `.doc`/
  `.ppt` by extension) is extracted into locator-tagged sections by the new
  `gamas_bot/doc_extract.py`, capped at `DOC_MAX_BYTES` (default 50 MB). PowerPoint extraction now
  precedes the optional original presentation/audio pipeline. Encrypted PDFs and
  image-only documents get Persian explanations, a PDF with less than ~10 characters per page is
  refused as scanned instead of storing an empty row, and an identical file is deduped by SHA-256
  and reuses the stored extraction. Documents live in the new `documents` table, appear under
  «📄 اسناد» in the library with a ~300-character preview, and offer «🃏 ساخت فلش‌کارت» /
  «❓ ساخت سؤالات» directly. New dependency: `pypdf` (pure Python, no OCR).
- **Quality and speed (R5).** Prompts now demand atomic one-fact cards, cloze-first phrasing, no
  yes/no items, four-option MCQs with misconception distractors, a short explanation, a difficulty
  tag, and a `source` locator per item; items that cannot be anchored are discarded. Generation
  parses once, chunks the text, runs `LEARNING_CHUNK_CONCURRENCY` calls (default 4, range 1–8) through
  `llm.complete_json` with `asyncio.gather`, retries an invalid chunk once, skips it after a second
  failure, retains valid partial results, dedupes near-duplicates and reports incomplete coverage instead of failing the
  run. A preview/curation screen lets the user delete unwanted items before saving, with live
  progress («در حال تولید… بخش ۳ از ۷») and three size presets: «⚡ سریع» (10/5),
  «⭐ استاندارد» (20/10), «🎯 کامل» (40/20), distributed proportionally across chunks.

- Quiz prompts mix recall, comprehension and application (~40% `medium`, ~60% `hard`) with plausible
  misconception distractors. New generation requires a valid difficulty and an exact locator from
  its input chunk; legacy stored questions without these fields remain readable.

Learning loop add-on — quizzes, leaderboard and flashcards:

- Booklets with extracted text gain an in-bot quiz (5/10/20 presets, legacy 15-question callbacks,
  `QUIZ_COST_SECONDS_PER_QUESTION`
  debited per question before generation) and AI flashcard decks (`FLASHCARD_AI_COST_SECONDS` flat
  charge, `FLASHCARD_AI_DECK_SIZE` pairs), plus manual decks, flip study mode and Word export.
  Every non-durable generation path — model failure, malformed/short output, database error or
  cancellation — refunds the full debit; unlimited users are never charged. A per-user in-flight
  guard and lock reject parallel generations before any second debit, and admission caps
  (`QUIZ_MAX_ACTIVE_HOURS`, `QUIZ_MAX_SESSIONS_PER_HOUR`, `FLASHCARD_MAX_DECKS_PER_USER`) are
  enforced before charging.
- All-time leaderboard (`🏆 برترین‌ها`) ranks users by quiz points (+10 per correct answer),
  hides Telegram identifiers and excludes banned users. Admins can gift a paid plan's credit from
  the board; the gift is audited, cooldown-limited against double taps, refused for admins/banned
  users, and announced by best-effort DM.
- Migrations 011 and 012 add display names, extracted booklet text and the quiz/flashcard tables.
  Foreign keys cascade ended quiz rows when a library item is pruned while flashcard decks survive
  with a nulled item reference; active quiz sources are protected from pruning. Manual flashcard
  conversations are disarmed at every navigation, `/start`, `/cancel` or other command.

One-tap flow, wallet and Telegram-backed library (v4):

- `/start` and every return to the user panel explain that a lecture file is the only action; audio,
  video and PowerPoint uploads now always run the full configured pipeline without an `stt`/`make`
  mode state. Transcript delivery remains before note generation, and absent note providers mean a
  transcript-only job.
- The user menu is now Library, Wallet, Support and Help (plus the separate admin entry for admins).
  The wallet combines balance, plan purchase and recent payments; the library stores Telegram
  message references for each delivered transcript/booklet and normally prunes to `LIBRARY_MAX_ITEMS`
  while preserving active quiz sources.
- Migration 010 adds the library reference table without copying generated files to the server.
  The former Free Office bot UI and queue are retired; its pure export modules remain covered by
  offline tests, and `OFFICE_ENABLED` now defaults to `false`.

Admin statistics dashboard (migration 009):

- Replaced the flat lifetime overview with a period-aware dashboard: users / processing /
  payments / system health, each with `امروز | ۷ روز | ۳۰ روز | کل` windows and a drill-down
  screen, plus an explicit «نیازمند توجه» list (pending receipts, failures by category, credit
  expiring within 7 days, database errors, provider timeouts/rate limits). Metric definitions are
  in [Admin panel](ADMIN.md).
- Submissions now record `started_at`, `error_kind` and `stt_engine`. That makes queue wait,
  processing time, failure categories and STT provider usage measurable from authoritative rows for
  a chosen window; provider usage also survives transcript retention, which previously erased the
  only record of it. Pre-upgrade rows keep NULL and are excluded from those figures rather than
  counted as zero.
- Failures are categorised once (input/download/media/stt/notes/delivery/billing/database/
  cancelled/capacity/banned/internal) at the point they are recorded, so a period report no longer
  has to guess from error text.
- New reporting indexes (submissions by `created_at`/`finished_at`/`kind`, users by `joined_at`,
  payments by `created_at`, credit ledger by `kind, expires_at`). Windows are a sargable
  `created_at >= ?` bound; the first version used `(COALESCE(?, '') = '' OR created_at >= ?)`,
  which is not index-usable, so the same 7-day report measured 50.0 ms instead of 29.5 ms at
  20 000 submissions (see [Performance](PERFORMANCE.md)).
- `python -m scripts.admin_report_bench` reproduces the dashboard timings on synthetic data.
- Fixed a stale changelog line that claimed the shipped STT order leads with Deepgram; the default
  remains Speechmatics primary (`DEFAULT_STT_PROVIDERS`).

Concurrency and capacity-wait fixes:

- Fixed single-provider capacity starvation: with only Speechmatics configured (2 in-flight
  slots against `MAX_ACTIVE_JOBS=3`), the third concurrent job waited just 15 s for a slot and
  then failed outright even though running transcriptions hold slots for minutes. The
  all-providers-full waits are now sized against attempt durations
  (`STT_SLOT_WAIT_TIMEOUT_SECONDS` 15 -> 600, `LLM_SLOT_WAIT_TIMEOUT_SECONDS` 30 -> 300); the
  zero-wait overflow pass is unchanged. `config.check` now emits a startup advisory when
  `MAX_ACTIVE_JOBS` exceeds the configured STT slots or `NOTE_CONCURRENCY` exceeds the
  configured LLM slots.
- A job arriving while every size-eligible STT provider is in a short post-error cooldown now
  waits out the shortest cooldown once (bounded by `STT_SLOT_WAIT_TIMEOUT_SECONDS` and the chain
  deadline, holding no slot) instead of failing instantly; counted as `cooldown_waits` in metrics.
- Free Office builds run on a dedicated bounded lane (`OFFICE_JOB_CONCURRENCY`, default 2), so a
  seconds-long text->document build no longer queues behind multi-hour transcription jobs.
- Capped the shared `asyncio.to_thread` executor at 8 threads: on shared hosting
  `os.cpu_count()` reports host cores and the default pool could spawn up to 32 threads.

- Added `tests/test_acceptance_mission.py`: post-merge acceptance coverage mapping the manual
  payment workflow, the two-way support tickets and the main-menu copy to one executable check
  each, including the exact Persian messages users and administrators see.

Payment, support and Persian UX fixes:

- Bounded and paginated the admin pending-payment screen so large review backlogs remain usable.
- Fixed the admin new/open ticket filter to include freshly created `awaiting_admin` tickets as well
  as reopened `open` tickets.
- Migration 008 enforces forward-only payment transitions and complete review metadata without
  rewriting production rows. Tests cover concurrent approvals through independent SQLite connections.
- Submitted orders remain readable when new card-to-card purchases are disabled; simplified the main
  menu copy and made unavailable-receipt status wording accurate.
- Manual review now requires a stored receipt and an explicit approval confirmation. Payment status,
  credit and audit writes are atomic, and migration 006 enforces one credit ledger row per payment.
- Added user/admin payment histories, order details, complete admin receipt notifications, rejection
  reasons, safe activation retries, and Telegram image-document receipt support.
- Ticket creation and its first message are atomic. Support prompts survive input mistakes, commands
  disarm stale replies, and long histories are delivered completely in Telegram-sized chunks.
- Simplified the main menu into purchase, balance, payment history and support actions with concise
  formal Persian labels and Persian currency grouping.

Documentation and maintenance pass:

- Rewrote every guide for two platforms only: Windows PowerShell (local) and Linux cPanel shared
  hosting. Removed the expired performance plan; its measured facts are now a short
  [Performance](PERFORMANCE.md) page.
- Fixed transcript retention to include `partial` jobs (booklet failed after the raw transcript was
  delivered), so finished text expires on schedule.
- `--check` now names an empty/unknown `STT_PROVIDERS` list instead of the misleading "no provider
  configured" message.
- The cPanel preflight now runs the real configuration validation, so it can no longer report
  `PASS` for a deployment the bot would refuse to start; removed a dead branch and an unnecessary
  network probe.
- Shutdown cancels and awaits background cleanup/prewarm tasks before closing the database.
- Free Office's last-document cache evicts the least recently built document, not the first key.
- Removed dead plan metadata, duplicated single-chunk note code, and stale ignore/config comments.

Performance defaults (already shipped):

- STT order leads with Deepgram Nova-3; Speechmatics stays as fallback.
- Note layer: 25 s first-byte guard, 60 s hedge, 6 concurrent map requests, structured output,
  progressive booklet.
- Audio: `application=audio, compression_level=0` Opus, `AUDIO_FORMAT_POLICY=auto` chooses WAV or
  Opus from encoder CPU plus the job's measured transfer rate.
- Large lectures compile and repair in bounded section groups instead of skipping those passes.

## 3.0.0 — 2026-10-11

- Rebuilt around a bounded job queue, SQLite billing ledger and recoverable reservations.
- Ordered STT and note-provider failover; raw transcript delivered first, then an RTL Word booklet.
- PowerPoint narration support and the free text-to-Office menu.
- cPanel cron/Passenger launch support. Single-process by design; not horizontally scalable without
  a job broker.

For older history, use `git log`.
