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

Audio is sent to the configured STT provider. The transcript may be sent to the configured note
provider. Review each provider's retention and privacy terms before enabling it. The raw transcript
is delivered before note generation; once delivered, its transcription charge stands even if the
booklet later fails.

The database stores submissions, transcripts, notes, billing and audit records. Free Office request
text is held in an in-memory session that expires; an administrative submission row remains.
Retention is configurable in `.env` (see [Operations](OPERATIONS.md)).

## Report a vulnerability

Contact the repository maintainer privately. Do not publish exploits, user data or credentials in
a public issue.
