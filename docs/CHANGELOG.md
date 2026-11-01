# Changelog

Concise history; detailed patches and earlier notes remain in Git history.

## Unreleased — documentation refresh (2026-11-01)

- Short, practical guides for cPanel Terminal and Windows PowerShell; dedicated
  Windows setup page and consolidated update/backup/restore instructions.
- Removed revision-specific `AUDIT_REPORT.md`, `PRODUCTION_AUDIT.md` and duplicate
  `DEPLOY_FA.md`; current feature/deployment guides replace them.
- Removed invalid migration steps; migrations run at startup.
- Clarified cron command fields, Passenger maintenance triggers, provider costs,
  OCR privacy, offline checks and document-cache retention limits.
- Updated documentation test coverage for both setup guides and local links.
- No runtime, schema, dependency or billing behavior changed.

## Existing unreleased work — summary

- Payment/support/admin reads use query-only storage; subscription responses are
  bounded and alerts name the configured channel.
- Broadcast flood waits retry the same recipient; the console entry point works.
- Optional remote OCR supports scans/photos with bounded requests; off by default.
- Learning is source-anchored with bounded top-ups, preview curation, independent
  gifts and durable paid recovery.
- Office/document workflows support notes and exports, literal Excel values,
  bounded drafts and no user wallet/free-use charge.
- Admin hubs, identity lookup, progress displays and short library titles improve
  operation. Reporting and pipeline concurrency stay bounded.
- Payment transitions, receipt checks, ownership and shutdown/recovery were hardened.

## 3.0.0 — 2026-10-11

SQLite v3 credit ledger, manual receipt review and shared-hosting deployment.
Use current guides for supported behavior, not historical migration walkthroughs.
