# Security

## Threat model in one paragraph

The bot accepts files and text from anonymous Telegram users and turns them
into documents that are stored and delivered. The interesting risks are
malicious uploads (zip bombs, malformed media, path traversal), credential and
transcript leakage through logs, payment manipulation, and any path where user
input could reach a shell or the filesystem outside its sandbox.

## What is enforced

**No shell from user input.** Every subprocess uses a fixed argument vector with
`shell=False` (media worker, launcher, DOCX renderer). File names, captions and
provider metadata are never interpolated into a command line.

**Private storage.** `RECEIPT_DIR` must be outside the web document root, is
created `0700` and verified at startup; SQLite and its `-wal`/`-shm` sidecars
are `0600`. Receipt files are `0600` and are only ever re-sent to admins — the
path is checked against the private root before any read or delete, and cleanup
never deletes outside it.

**Upload validation.** Receipts must be JPEG/PNG/WebP *by content signature*
after download, not only by declared MIME type, and are bounded by
`MAX_PAYMENT_RECEIPT_BYTES`. Media and decks are bounded by `MAX_FILE_SIZE_BYTES`
before and after download.

**Office packages.** Decks are validated before any parsing: entry count,
total unpacked size, and a path check that rejects absolute paths and `..`
components. No macro is ever executed; the macro-enabled content types are
opened only to read slide structure.

**Credentials.** Provider keys stored in the database are Fernet ciphertext
under `PROVIDER_CREDENTIALS_ENCRYPTION_KEY`, which is environment-only. The
admin panels show a masked tail and never the value. A key message in Telegram
is deleted before being stored, and if deletion fails the key is *not* stored.

**Authorization.** Every admin action requires the sender to be in `ADMIN_IDS`;
admin panels refuse to run in group chats; a banned user is refused at both the
message and callback layer; and the bot refuses to grant credit to, ban or
modify another admin.

**Billing integrity.** Credit is reserved before work and finalized only after
delivery; every failure and cancellation path releases the reservation exactly
once. Entitlement grants are idempotent per payment, and one open payment
request per user is enforced by a unique index.

**Log hygiene.** Structured logs carry a job identifier, provider attempt
counts, timings and failure classes. Transcripts, API keys, ciphertext and
receipt contents are never logged.

**Single instance.** An advisory lock on the Telegram session path prevents two
processes from polling the same bot account.

## Reporting

Report a vulnerability privately to the maintainer listed in `README.md`. Do not
open a public issue containing a working exploit or any credential.
