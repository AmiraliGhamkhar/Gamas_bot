# Security and privacy

## Operator checklist

- Keep the project, `.env`, database, Telegram session, logs, temporary files and receipts **outside `public_html`**. Run the cPanel preflight before going live.
- Protect `.env` and backups. Do not commit secrets or send them in logs/support messages.
- Use one bot process per token/session. The instance lock prevents duplicate polling on supported local filesystems.
- Limit uploads, queue size and temporary storage to what the hosting plan can handle.

## What the bot enforces

- User input is never passed to a shell command. Media runs in a bounded child process.
- Downloads have progress and final-size checks. Receipts must be valid JPEG/PNG/WebP images, are decoded under a pixel limit and re-encoded before storage. Receipt paths and private directories are checked before reads/deletes.
- PowerPoint ZIP files have entry, central-directory, name and expanded-size limits. Members are streamed under limits and integrity-checked; unsafe paths, symlinks and unsupported compression are rejected.
- Admin actions require an admin ID and private chat. Invalid callbacks fail closed. Admin write commands and broadcasts are cooldown/rate-limited.
- API keys come from the process environment or `.env`; they are not stored in SQLite or logged. Provider error text is bounded and redacted.
- Logs contain job references and operational metadata, not transcripts, receipt contents or keys.
- Receipt and temporary directories use private permissions on supported filesystems. The cPanel preflight checks that the receipt directory is private and writable.

## Data sent to providers

Audio/video narration is sent to the configured STT provider. The transcript may be sent to the configured note provider. Review each provider's retention and privacy terms before enabling it. The raw transcript is delivered before note generation; after a complete raw transcript is delivered, its transcription charge remains even if booklet generation later fails.

The database stores submissions, transcripts, notes, billing records and audit events. Free Office request text is kept in an expiring in-memory session; an administrative submission record remains. Back up or delete data according to your retention needs.

## Report a vulnerability

Contact the repository maintainer privately. Do not publish an exploit, user data or credentials in a public issue.
