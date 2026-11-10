# Security and privacy

## Checklist

- Keep `.env`, the database, session, receipts, logs and backups **outside** every
  web root (`public_html`, `www`, `htdocs`, `httpdocs`, `public_ftp`).
- cPanel: `chmod 600 .env`, `chmod 700 data`. Windows: restrict the project folder
  to the operator account and keep it out of shared/cloud-synced folders.
- Never commit keys or user files. Treat the Telegram session like a password;
  rotate keys that were exposed.
- Run one process per token/session and never bypass an active lock.
- Back up before updates and use audited credit operations, never ledger edits.

## What goes where

| Data | Where |
| --- | --- |
| Audio/video | your STT provider (Speechmatics / Deepgram / OpenAI-compatible) |
| Transcript text for notes | your note provider (Gemini / OpenRouter / …) |
| Scan and photo images | i2ocr.com — only when `OCR_ENABLED=true` |
| Transcripts, notes, billing, tickets | local SQLite (`data/gamas_v3.db`) |
| Generated files | Telegram; the library stores message references |
| Mini App requests | your host; signed `initData` is verified on every call |

Review each provider's current terms and get the consent your jurisdiction
requires before processing sensitive material. No third-party deletion or
confidentiality guarantee is made by this project.

## Controls that are already in place

- Admin authorization fails closed; private callbacks verify ownership.
- Channel membership infrastructure errors fail open (a Telegram outage must not
  lock everyone out); confirmed non-membership always blocks. Billing, bans and
  admin checks still apply.
- Receipts are signature-checked, pixel-bounded and re-encoded before private
  storage; paths are validated before reads and deletions.
- Media runs with fixed arguments, no shell and bounded child processes; document
  parsing uses bounded threads (no OS-level memory sandbox).
- Uploads, source text, drafts, response bodies, timeouts and outputs are all
  bounded; overflow is refused, never truncated.
- Logs contain metadata and references only, and are redacted against configured
  secrets. Never add transcript text or receipts to a log.
- The Mini App verifies Telegram's `initData` HMAC (constant-time), enforces
  `auth_date` freshness and scopes every query to the signed user id.

## Mini App specifics

- Serve it over HTTPS only (Telegram requirement) and keep the origin bound to
  loopback behind Cloudflare, or terminate TLS at the web server on cPanel.
- The bundle is served by the app itself with CSP, `nosniff` and `no-referrer`.
- Request bodies are capped at 1 MB and rendered exports are served from stored
  bytes, never re-rendered on demand.

## Reporting

Report vulnerabilities privately to the maintainer. Never publish credentials,
user data or working exploit details in a public issue, and never send `.env`,
sessions, receipts or private transcripts in a support ticket.
