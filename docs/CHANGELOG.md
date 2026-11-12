# Changelog

Short, operator-relevant history. Detailed patches remain in Git history.

## Unreleased

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

**Migration numbering.** `migrations/v3/` contains two files numbered `016` (`016_office_library_kind.sql` and `016_subscription_enforcement.sql`). This is harmless today: the loader sorts filenames and tracks each by name. New migrations must continue from `029_`.
