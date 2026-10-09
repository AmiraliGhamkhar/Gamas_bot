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

**Dependency security.** CI runs `pip-audit` against `requirements.txt` as an
advisory gate, and every runtime dependency is bounded by a range in
`pyproject.toml`. Where an advisory exists, the lower bound is the first
release that contains *every* published fix, so a deployment cannot resolve a
known-vulnerable version: `cryptography` is floored at `50.0.0` for exactly
that reason (7 advisories below it, fixed in 48.0.1/49.0.0/50.0.0).

## AI provider, billing, and student-note boundaries

Provider calls use the encrypted credential manager. The credential secret is
redacted from object representations, audit details, provider error summaries,
and structured events. A provider error can contain an echoed token or a backend
stack trace; only a bounded error category/status reaches logs. Prompts,
transcripts, and raw completions are not persisted in the provider usage ledger.

`AI_FREE_ONLY=true` and `AI_ALLOW_PAID_FALLBACK=false` are the defaults.
Provider and model free status are not sufficient by themselves: generation also
requires a per-key billing attestation. Migrated credentials are marked
`unknown` and blocked until an administrator confirms either (a) billing is
disabled or a hard `$0` cap prevents overage, or (b) billable use is explicitly
authorized for a paid route. Attestations are timestamped and attributed to an
admin; clearing an attestation blocks the key. Read-only catalog/health requests
are not generation authorization.

Environment-backed legacy `NOTE_API_*` credentials are preserved, but are not
auto-attested. When a deployment needs generation from its existing env key, an
administrator may add the same secret to the Fernet-encrypted vault and attest
the stored key. The legacy provider/base/model settings remain unchanged. No
actual key is included in migrations, tests, logs, docs, or the final report.

The canonical note parser and QA/repair path treat provider metadata as
untrusted. Backend names, request IDs, HTTP diagnostics, tool traces, echoed
credentials, and stack fragments are scrubbed before a student-facing note is
saved or rendered. The provider output cannot replace the canonical structured
note schema, billing decision, QA, merge, DOCX, or PPTX pipeline. DOCX output
continues to declare RTL layout for Persian text and preserves Latin runs.

Provider terms, model eligibility, account quotas, and overage controls can
change. Gamas cannot verify an operator's live provider billing page; a `free`
attestation is an administrator assertion, and should be reviewed whenever the
provider plan or account payment settings change. Review the current official
links in [AI_PROVIDERS.md](AI_PROVIDERS.md).

## Reporting

Report a vulnerability privately to the maintainer listed in `README.md`. Do not
open a public issue containing a working exploit or any credential.
