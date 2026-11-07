# Changelog

Concise history; detailed patches and earlier notes remain in Git history.

## Unreleased — hardening

- WO-SUB-2: record sweep membership and check timestamp in one update.
- WO-SUB-1: cache confirmed membership for five minutes by default, including explicit checks.
- WO-PAY-5: edit the wallet screen in place when opening an existing order.
- WO-PAY-3: surface older open orders and clamp wallet history pages.
- WO-PAY-4: record cancellation actors, expose cancelled orders and use UTC receipt retention.
- WO-CAP-1: lower document upload and cumulative Office expansion defaults.
- WO-SUP-1: atomically limit open support tickets and enforce a creation cooldown.
- WO-SUP-3: confirm user ticket closures before closing or notifying staff.
- WO-SUP-2: retain full support openers while deriving a bounded first-line subject.
- WO-PAY-1: preserve stored receipt bytes and modification time when viewed.
- WO-LRN-7: log and reject malformed three-option questions rather than mis-grade them.
- WO-LRN-15: use bounded document metadata and previews for screens.
- WO-LRN-12 (partial): index quiz admission and answers; retention remains unimplemented.
- WO-LRN-6: refund paid learning shortfalls pro rata in the artifact transaction.
- WO-LRN-8: protect live curation sources from library pruning and refund missing sources cleanly.
- WO-LRN-4: retain source locators when splitting overflowed learning chunks.
- WO-LRN-1: retain undelivered paid previews until their normal expiry and offer resume.
- WO-LRN-2: preserve originating credit expiry and lot IDs on learning refunds and recovery.
- WO-LRN-10: gate flashcard DOCX delivery while keeping paid quiz answers accessible.
- WO-LRN-11: reject invalid flashcard presets rather than silently charging the base cost.
- WO-LRN-3: bound learning calls for small presets on many chunks.
- WO-LRN-5: preserve manual-card state through the finish callback.
- WO-DB-9: check submission links and explicitly roll back duplicate quiz inserts.
- WO-DB-5: batch learning inserts and transcription-retention deletes.
- WO-DB-2 (partial): add performance indexes; expiry predicates retain legacy timezone correctness.
- WO-DB-3: route plan, user, transcription-retention and admin count reads to the read connection.
- WO-DB-10: retry transient SQLite read contention and count read errors.
- WO-DB-8: bound SQLite caches, mapping and journal size for shared hosting.
- WO-DB-4: report discarded stray transactions in logs and metrics.
- WO-DB-1: consume pending membership-gate actions atomically.

## Unreleased — one static booklet prompt and the AvalAI route (2026-11-06)

- Replaced the assembled per-mode note prompt with one static agent contract
  (`gamas_bot/_prompt_block.py`, 8 principles, byte-identical for every request)
  and moved every per-job datum into the user message. The system prompt is now
  the same string for every chunk, source kind, mode and job, so a provider with
  implicit prefix caching bills it once; `PromptTests` asserts the bytes and the
  absence of per-job data instead of the old per-mode phrasing.
- The documented per-request templates are now the real ones: chunk requests are
  `متن پیاده‌سازی‌شدهٔ خام (بخش i از n)` + text + `موضوع همین بخش`, the single-part
  path says `(درس تک‌بخشی)`, and the editorial compile is `متن منبع کامل` + digest
  then `پیش‌نویس جزوه` + draft. Overflow recovery re-renders both halves from the
  raw text instead of cutting a rendered message, and the repair/compile passes
  share one message shape.
- The prompt carries the full booklet skeleton the exporter already renders
  (cover/title, contents from headings, objectives, per-section definitions,
  examples, steps, formulas, tables, callouts and key points, a one-page summary
  and an English-term glossary) plus the no-drop coverage rule.
- Added AvalAI as a note route: `AVALAI_API_KEY`, `AVALAI_BASE_URL`
  (default `https://api.avalai.ir/v1`) and `AVALAI_MODEL` (default
  `deepseek-v4.1-flash`), last in the default chain so an unconfigured key
  changes nothing. It sends the documented `json_schema` envelope and keeps the
  `json_object` → drop degradation ladder.
- `.env.example` now documents the `DEEPSEEK_*` and `AVALAI_*` keys, lists the
  real `NOTE_PROVIDERS` default and no longer advertises the removed Groq and
  Cerebras settings.

## Unreleased — providers, membership and referrals (2026-11-02)

- Removed the Groq and Cerebras note integrations from every registry, request
  profile, config diagnostic and provider test. A legacy `NOTE_PROVIDERS` value
  naming them is skipped with one explicit startup warning; startup continues.
- Added DeepSeek V4.1 Flash as `deepseek` (`deepseek-flash` at
  `https://api.deepseek.com`) with JSON Output only, no undocumented strict
  schema, and bounded retries through the existing failover chain.
- Deferred the channel gate from every interaction to value delivery. Blocked
  files are stored in `pending_gate_actions` (migration 018) and resume after a
  successful check without resubmission; a bounded, resumable sweep re-checks
  active users oldest-first and stops on an inconclusive result.
- Added first-start referral attribution and idempotent 20-minute/1-hour ledger
  rewards (migration 019), an invite screen, and an admin referral report. The
  payment reward commits with approval; receipt upload pays nothing.
- `.env.example` still needs an operator edit because the workspace file guard
  blocks env-family files: set `NOTE_PROVIDERS` without the removed providers and
  document the `DEEPSEEK_*` keys.

## Unreleased — centralized Persian UX copy (2026-11-02)

- Added `gamas_bot/texts.py` as the single source for start/help screens,
  library states, wallet and receipt copy, support tickets, the channel gate,
  progress headlines and shared delivery errors.
- Rewrote the start and help screens as short, sectioned, Persian-first guides;
  button labels, gate wording and progress headlines now follow one glossary.
- Added copy-contract tests and a guard that prevents Persian string literals
  from returning to the migrated handler modules.
- No billing, schema, provider or gate behavior changed.

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
