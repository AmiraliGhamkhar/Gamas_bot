# Security and privacy

## Operator checklist

- Keep the project, `.env`, database, Telegram session, logs, temporary files and receipts
  **outside `public_html`**. Run `python -m scripts.cpanel_preflight` on cPanel.
- `chmod 600 .env`; `chmod 700 data/`. Never commit or share secrets, sessions or backups.
- Run one bot process per token/session. The instance lock enforces it.
- Limit uploads, queue size and temporary storage to what the plan can handle.
- Back up encrypted and store backups outside the web root.

## What the bot enforces

- No user input reaches a shell. Media runs in a bounded child process with no shell and no
  network protocols.
- Downloads are size-checked before and after transfer and abort when they stop making progress.
- Receipts must be valid JPEG/PNG/WebP images, decoded under a pixel limit and re-encoded before
  storage. Paths are validated before reads/deletes.
- PowerPoint ZIPs are checked for entry count, names, expanded size, symlinks and compression;
  members are streamed under limits.
- Admin actions require an admin ID and a private chat; callbacks fail closed; admin writes and
  broadcasts are cooldown-limited.
- API keys come from the environment or `.env`, never SQLite, and are redacted from error text.
- Logs contain job references and metadata, not transcripts, receipt content or keys.
- The database, session, receipt directory and logs are created owner-only where the filesystem
  supports it.

## Data sent to providers

Audio is sent to the configured STT provider. Lecture transcripts, document notes and optional
Office AI planning are sent only to the existing configured note-provider routes. Office never starts
STT/transcription. Review each provider's retention and privacy terms before enabling it. The raw
transcript is delivered before lecture note generation; once delivered, its transcription charge
stands even if the booklet later fails. Free Office/document-note operations themselves have no user
wallet or free-use charge; configured provider use may still incur operator-side costs.

The database stores submissions, transcripts, notes, billing, audit records, extracted booklet and
uploaded-document text, and the Telegram chat/message references used by each user's library;
generated files themselves stay in Telegram. `LIBRARY_MAX_ITEMS` bounds normal retained references
per user, except sources protected by active quizzes. It does **not** bound the separate document
text cache: uploaded-document extracts currently have no automatic retention or self-service delete
policy, even if their library mirror is pruned. Plan disk and privacy procedures accordingly.
Free Office drafts are held only in the bot process's memory, keyed by the private Telegram user id,
limited to 128 concurrent users/200 parts each, and swept after `OFFICE_SESSION_TTL_MINUTES` (default
30 minutes), on navigation and at shutdown. Temporary source/output files live under the private
`TEMP_DIR` and are removed in `finally`; Office code does not log document text or contents. The
existing extracted-document text is cached in SQLite and currently has no automatic retention or
self-service deletion policy; transcript retention is separate and configurable in `.env` (see
[Operations](OPERATIONS.md)).

## Report a vulnerability

Contact the repository maintainer privately. Do not publish exploits, user data or credentials in
a public issue.
