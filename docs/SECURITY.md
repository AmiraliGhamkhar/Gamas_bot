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
| Audio/video | The configured STT chain (Speechmatics, Deepgram and/or an OpenAI-compatible transcription API) |
| Transcript text for booklets, AI quizzes and AI flashcards | Approved note-generation routes only: Gemini and AvalAI; OpenRouter is a fallback only when sensitivity policy permits it |
| Scan and photo images | i2ocr.com — only when `OCR_ENABLED=true` |
| Transcripts, notes, billing, tickets | local SQLite (`data/gamas_v3.db`) |
| Generated files | Telegram; the library stores message references |

Which third-party service receives a request depends on the deployment's
private `.env` and process environment. A recoverable provider failure can send
the same audio or text to the next configured route. Model IDs and the code-
default chains are listed in [Providers](PROVIDERS.md); they do not identify a
live server's actual configuration.

Unclassified (`UNKNOWN`) text never goes to OpenRouter. Explicit `INTERNAL`,
`SENSITIVE` and `PHI_OR_PII` labels require per-provider approval; all three
opt-ins default to false. `STANDARD_EDUCATIONAL` is an explicit caller assertion,
not an automatic classification of uploaded content. Gemini/AvalAI eligibility
for `UNKNOWN` is not proof of consent or suitability for patient data. Legacy
provider variables do not activate additional routes.

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

## Reporting

Report vulnerabilities privately to the maintainer. Never publish credentials,
user data or working exploit details in a public issue, and never send `.env`,
sessions, receipts or private transcripts in a support ticket.
