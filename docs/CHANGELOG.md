# Changelog

Short, operator-relevant history. Detailed patches remain in Git history.

## Unreleased

**Gamas Studio mobile reliability and startup.** The Telegram SDK is loaded
asynchronously; SDK/initData failures have an explicit retry/explanation state.
Home is the only startup API request; leaderboard and Studio data are loaded on
first use and cached only in page memory. Read requests coalesce while in flight,
use bounded timeouts, and expose persistent manual retry; mutations are not
automatically retried. Telegram theme/viewport and BackButton are feature
detected. The Persian RTL UI has responsive light/dark tokens, touch targets,
safe-area padding and mobile editor controls while preserving all Studio
features.

**Mini App WSGI and SQLite.** Home summary work is now one read-only connection
and one query instead of five connection/read operations. `/ready` is a
read-only SQL/schema probe distinct from the existing `/health` file-exists
check. Static responses now support ETag/Last-Modified validation and correct
HEAD/304 behavior; request IDs and WSGI timing are recorded without request
bodies or signed credentials. No schema migration, index, Passenger directive,
bot process or live database was changed. cPanel documentation now targets the
reported root-domain setup and requires live verification; production and
physical-device results remain outstanding.

**Reporting, booklet delivery and auditability.** Admin completion rates now
separate fully completed work from partial transcript delivery; PDF booklet
success is counted only from persisted PDF bytes. PDF button send attempts are
tracked in bounded SQLite counters so user retries remain distinct from logical
submissions. Invalid renderer output is not persisted as a final artifact.
Referral payment rewards are limited to the referred user's first approved
payment, with a banned-referrer decision recorded transactionally, and the
leaderboard aggregates reward rows before joining invite counts. The v2 importer
keeps source payment IDs and links eligible paid credit lots to imported payment
history while validating balances and foreign keys.

**AvalAI hardening.** The OpenAI-compatible note route now captures AvalAI's
`avalai-request-id` for every successful normal or streaming completion and
logs it with token usage, so exact User API cost reconciliation has a
correlation id. Error messages prefer the same id and redact secrets. The LLM
transport has a short connect timeout, refuses redirects that could forward a
bearer key, and caps provider `Retry-After` delays at 60 seconds. `--check`
rejects accidentally configured AvalAI User API or Google-native API bases, and
the standalone AvalAI smoke script no longer prints URL credentials, queries or
fragments.

**Large uploads.** Default video/audio and PowerPoint/document upload ceilings are now 1 GiB; presentation extraction permits up to 1 GiB unpacked with a 2 GiB workspace budget. Operators must provision adequate temporary disk and verify Telegram/host transfer limits.

## 3.0 — Gamas Studio, reference-grade booklets, confirmed tariff

**Mini App (Gamas Studio).** Persian-first RTL single page with Home,
Leaderboard (all-time / weekly / monthly), Submissions, structured Booklet
Editor, Preview, Exports and Revision History — no build step. Deploy:
[cPanel](MINIAPP_CPANEL.md) or [Cloudflare](MINIAPP_CLOUDFLARE.md).

**Structured editing.** The editor works on the canonical `StructuredNotes`
model: sections, subsections and blocks (paragraphs, lists, definitions,
examples, key points, callouts, process steps, tables, formulas, glossary) with
add/delete/reorder and cell-level table editing. Validation is the model's own
(`from_payload`); unknown blocks are rejected. Every save re-renders both the
Persian RTL PDF and the editable DOCX and stores the bytes against the new
revision — exports serve stored bytes and never re-render.

**Speakers.** Per-submission speaker list with stable engine labels and
owner-chosen display names and roles (migration 028, additive). Renaming never
touches audio, timestamps or segments and never infers identity.

**Booklets.** Native `StructuredNotes` PDF renderer: generated Persian cover and
topic map, hierarchy, educational blocks, RTL tables, glossary, running
furniture and an explicit auto-generation disclaimer. Additive migration 027
persists owner-scoped PDF/DOCX bytes; the completion screen sends nothing
automatically and each output button returns the exact stored file.

**Tariff.** Confirmed: 100 000 credits = 3 hours = 10 800 seconds = 65 000 Toman.
All six built-in plans use proportional 30-day prices; historical snapshots and
custom plans are untouched.

**Quality.** PDF visual QA (`tests/test_pdf_visual_qa.py`) checks geometry,
fonts, ink coverage and text collisions, and found three real glyph/layout
defects — a missing step-connector arrow, callout icons missing from Vazirmatn,
and a cover badge drawn over the topic text — all fixed.

## 2.x — providers, hardening, billing recovery

- Note-provider failover with per-provider keys/models, bounded retries,
  structured-output compatibility ladder and a tested Gemini → OpenRouter
  profile; NaraRouter documented as generation-unverified (HTTP 502).
- Cache-stable static note prompt for provider prefix caching.
- Durable payment/learning recovery: pending charges, one-time refunds,
  forward-only payment states, receipt fingerprinting, append-only lot ledger
  with a materialized balance cache.
- Broadcast runs with resume checkpoints and permanent-failure tracking;
  channel-membership gate with cached confirmations and a bounded sweep.
- Bounded everything: jobs, queues, uploads, document expansion, Office drafts,
  provider bodies and outputs.

## 1.x — the bot

Transcription, RTL Word booklets, PDF export, study documents, Free Office,
library, wallet, support tickets and referrals on Windows PowerShell and cPanel.

**Migration numbering.** `migrations/v3/` contains two files numbered `016` (`016_office_library_kind.sql` and `016_subscription_enforcement.sql`). This is harmless today: the loader sorts filenames and tracks each by name. New migrations must continue from `032_`.
