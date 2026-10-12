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

**AI provider accounting is metadata-only.** `ai_usage_records`,
`ai_usage_daily`, `ai_quota_snapshots` and `ai_events` store provider, model,
status, latency, token/unit estimates, quota headers from a strict allowlist and
an error class — never a prompt, a transcript, a model answer, a response body
or a key. Estimated Cloudflare Neuron spend is a number derived from token
counts, not provider content. The Telegram logs panel exposes the same fields
and no others.

**Credentials.** Provider keys stored in the database are Fernet ciphertext
under `PROVIDER_CREDENTIALS_ENCRYPTION_KEY`, which is environment-only. The
admin panels show a masked tail and never the value. A key message in Telegram
is deleted before being stored, and if deletion fails the key is *not* stored.
This covers every STT provider key added through the STT wizard (spec §36) as
well as note providers. The STT test tool and dry-run viewer never render a key,
an authorization header, raw audio or transcript text, and sample-test audio is
never copied to a temporary file.

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
receipt contents are never logged. STT lifecycle events
(`stt_provider_events`, `stt_usage_records`) carry the same metadata-only
fields; event field names are allow-listed and values are screened for secret
markers before writing (spec §44/§54). The test suite asserts that panel output
and logs never contain a configured key, a bearer token or a raw provider
response (spec §73).

**Single instance.** An advisory lock on the Telegram session path prevents two
processes from polling the same bot account.

**Dependency security.** CI runs `pip-audit` against `requirements.txt` as an
advisory gate, and every runtime dependency is bounded by a range in
`pyproject.toml`. Where an advisory exists, the lower bound is the first
release that contains *every* published fix, so a deployment cannot resolve a
known-vulnerable version: `cryptography` is floored at `50.0.0` for exactly
that reason (7 advisories below it, fixed in 48.0.1/49.0.0/50.0.0).

## Note-provider, billing, and student-note boundaries

Provider calls read their credentials from the process environment (`.env` or
the service unit) and from nowhere else: there is no credential vault, no key
wizard and no encrypted store in v3, and no key is written to the database,
logged, or included in a user-facing message. A provider's error body can echo a
token or a stack fragment, so only a bounded, key-redacted detail string reaches
the log or the job's error column.

A note provider is **eligible** only when the operator listed it in
`NOTE_PROVIDERS` (or selected it as the v2-era `NOTE_PROVIDER` slot) *and* gave
it its own credential. A missing key is never treated as a configured provider,
one credential is never sent to two providers, and there is no implicit paid
fallback: pointing a slot at a paid endpoint is an explicit configuration
decision. Defaults are the identifiers the providers publish as free
(`agnes-2.5-flash` on Nara's free plan, `openrouter/free` on OpenRouter); Gamas
does not relabel a model as free, and model names and plan lists can change —
the provider's own documentation is the source of truth.

The note pipeline treats provider output as untrusted. Backend names, request
IDs, HTTP diagnostics, echoed credentials and stack fragments are scrubbed before
a student-facing note is saved or rendered, while medical, scientific and
technical terminology (including provider-like names that are legitimate course
content) is preserved. Provider output cannot replace the canonical structured
note schema, the deterministic merge, the DOCX pipeline or the billing decision.
DOCX output declares RTL layout for Persian text and preserves Latin runs.

A note failure never costs the user the transcript: the raw text is persisted and
delivered before note generation starts, the compile step falls back to the
deterministic merge, and a delivery failure after settlement is reported as a
delivery problem rather than a refund.

Identifiers, plan lists and free-tier claims can change at any time; the
operator's provider account page is authoritative and cannot be verified by this
project.

## Reporting

Report a vulnerability privately to the maintainer listed in `README.md`. Do not
open a public issue containing a working exploit or any credential.
