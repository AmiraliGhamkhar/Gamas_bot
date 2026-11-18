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
| Transcript text for booklets, AI quizzes and AI flashcards | Configured approved note-generation routes in task-specific order; OpenRouter remains the last fallback |
| Transcripts, notes, billing, tickets | Local SQLite (`data/gamas_v3.db` by default; configurable with `DB_PATH`) |
| Media-submission booklet PDF, DOCX and review HTML | Validated bytes stored in SQLite and sent to Telegram on request |
| Library | References to delivered Telegram files; document text may also be cached locally |

Which third-party service receives a request depends on the deployment's
private `.env` and process environment. A recoverable provider failure can send
the same audio or text to the next configured route. Model IDs and the code-
default chains are listed in [Providers](PROVIDERS.md); they do not identify a
live server's actual configuration.

There is no document-content classifier that filters names, identifiers, case
narratives or other educational source text from the configured provider chain.
Prompt construction strips configured technical credentials, secret-named
environment values, common API/bearer/Telegram token patterns, and explicitly
labelled password/key/secret assignments; it does not de-identify user content.
A provider failure can send the same sanitized prompt to the next configured
route. Review each provider's current terms and obtain any consent your
jurisdiction requires. No third-party deletion or confidentiality guarantee is
made by this project.

## Controls that are already in place

- Admin authorization fails closed; private callbacks verify ownership.
- Channel membership infrastructure errors fail open (a Telegram outage must not
  lock everyone out); confirmed non-membership always blocks. Billing, bans and
  admin checks still apply.
- Receipts are signature-checked, pixel-bounded and re-encoded before private
  storage; paths are validated before reads and deletions.
- Media runs with fixed arguments, no shell and bounded child processes; document
  parsing uses bounded threads (no OS-level memory sandbox).
- Uploads, source text, drafts, response bodies, timeouts and outputs have
  explicit resource bounds. Educational source is not silently truncated for
  model context: context overflow is semantically split and every part is
  processed/merged, or the operation fails visibly.
- Logs contain metadata and references only, and are redacted against configured
  secrets. LLM prompts also strip technical credential values at the request
  boundary. Never add transcript text or receipts to a log.

## Reporting

Report vulnerabilities privately to the maintainer. Never publish credentials,
user data or working exploit details in a public issue, and never send `.env`,
sessions, receipts or private transcripts in a support ticket.
