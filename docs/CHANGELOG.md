# Changelog

## Unreleased

- Hardened callback authorization, file-download limits, receipt validation, ZIP extraction, admin cooldowns, broadcasts and cPanel launcher locking.
- Added regression tests for the security fixes.
- Rewrote setup and operations guides for Windows PowerShell and cPanel shared hosting; removed stale audits and completed/unimplemented design plans.

## 3.0.0 — 2026-10-11

- Rebuilt the bot around a bounded job queue, SQLite billing ledger and recoverable reservations.
- Added ordered speech-to-text and note-provider failover, raw-transcript-first delivery and RTL Word booklets.
- Added PowerPoint narration support and the free text-to-Office menu.
- Added cPanel cron/Passenger launch support. The repository is a single-process app; it is not horizontally scalable without a job broker.

For older implementation history, use Git history. Old v2 provider-platform notes are not current configuration instructions.
