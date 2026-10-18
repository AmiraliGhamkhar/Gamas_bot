# Security

## Threat model

The bot accepts files and text from anonymous Telegram users and turns them into
documents that are stored and delivered. The interesting risks are malicious
uploads (zip bombs, malformed media, path traversal), credential and transcript
leakage through logs, payment manipulation, and any path where user input could
reach a shell or the filesystem outside its sandbox.

## What is enforced

**No shell from user input.** Every subprocess uses a fixed argument vector with
`shell=False` (media worker, launcher). File names, captions and provider
metadata are never interpolated into a command line.

**Private storage.** `RECEIPT_DIR` must be outside the web document root, is
created `0700` and verified at startup. SQLite and its `-wal`/`-shm` sidecars are
`0600`. Receipt files are `0600` and only ever re-sent to admins; the path is
resolved and checked to be strictly inside the private root before any read or
delete (symlink-aware), and cleanup never deletes outside it.

**Upload validation.** Receipts must be JPEG/PNG/WebP **by content signature**
after download (magic-byte check for `FF D8 FF`/PNG/WebP), not only by
declared MIME type, and are bounded by `MAX_PAYMENT_RECEIPT_BYTES` (pre- and
post-download). Media and decks are bounded by `MAX_FILE_SIZE` before and
after download, with symlink entries in ZIPs rejected.

**Office packages.** Decks are validated before parsing: entry count, total
unpacked size (`PRESENTATION_MAX_UNPACKED_BYTES`), and a path check that rejects
absolute paths and `..` components. No macro is executed; macro-enabled content
types are opened only to read slide structure.

**Credentials are environment-only.** Provider keys live in the process
environment or an owner-only `.env`. There is no credential vault, no encryption
key and no key column in the database — nothing to exfiltrate from a database
dump. A key is never logged and never appears in a user-facing message. A
provider's error body can echo a token or a stack fragment, so only a bounded,
key-redacted detail string reaches the log or the submission's error column.

**Authorization.** Every admin action requires the sender to be in `ADMIN_IDS`;
admin panels refuse to run in group chats; a banned user is refused at both the
message and callback layer; and the bot refuses to grant credit to, ban or modify
another admin.

**Billing integrity.** Credit is reserved before work and settled only after
delivery; every failure and cancellation path releases the reservation exactly
once, and startup recovery releases anything a crash left held. Payment approval
credits idempotently, and one open payment request per user is enforced by a
unique index.

**Log hygiene.** Structured logs carry a job identifier, provider attempt counts,
timings and failure classes. Transcripts, API keys and receipt contents are never
logged.

**Single instance.** An advisory lock on the Telegram session path prevents two
processes from polling the same bot account.

**Dependencies.** Every runtime dependency is bounded by a range in
`pyproject.toml`, and CI runs `pip-audit` against `requirements.txt` as an
advisory gate.

## Provider boundaries

A note provider is **eligible** only when the operator listed it in
`NOTE_PROVIDERS` (or selected it as `NOTE_PROVIDER`) *and* gave it its own
credential. A missing key is never treated as a configured provider, one
credential is never sent to two providers, and there is no implicit paid
fallback: pointing a slot at a paid endpoint is an explicit configuration
decision.

Provider output is untrusted. Backend names, request ids, HTTP diagnostics,
echoed credentials and stack fragments are scrubbed before a student-facing note
is saved or rendered, while medical, scientific and technical terminology —
including provider-like names that are legitimate course content — is preserved.
Provider output cannot replace the canonical note schema, the deterministic
merge, the DOCX pipeline or the billing decision.

Model identifiers, plan lists and free-tier claims change at any time; the
operator's provider account page is authoritative and cannot be verified by this
project.

A note failure never costs the user the transcript: the raw text is persisted and
delivered before note generation starts, the compile step falls back to the
deterministic merge, and a delivery failure after settlement is reported as a
delivery problem rather than a refund.

## Reporting

Report a vulnerability privately to the maintainer listed in `README.md`. Do not
open a public issue containing a working exploit or any credential.
