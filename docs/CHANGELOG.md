# Changelog

## Unreleased

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
