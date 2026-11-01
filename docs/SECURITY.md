# Security and privacy

## Operator checklist

- Keep `.env`, DB, session, logs, receipts, backups and temporary files outside
  `public_html` and every web root, on a private local filesystem.
- On cPanel use owner-only permissions: files `600`, private directories `700`.
  On Windows restrict folder access to the operator account.
- Never commit keys or user files. Rotate exposed keys; treat sessions as credentials.
- Use one process per token/session; never bypass an active lock.
- Back up before updates. Use audited credit operations, not ledger-history edits.

## Data handling

Audio goes to configured STT providers; text to configured note/learning providers.
OCR is off by default; enabling it sends scans/photos to **i2ocr.com**. Review
provider policies and obtain appropriate consent before sensitive processing.
Do not promise third-party confidentiality or deletion guarantees.

SQLite stores text, learning, billing, tickets and audit data. Generated files stay
in Telegram; the library stores message references. Library pruning does **not**
erase the document-text cache, which has no automatic retention or self-service
deletion policy. See [retention](OPERATIONS.md#updates-and-retention).

Office drafts stay in memory, expire after TTL and clear on navigation/shutdown.
Temporary files are private and cleaned after work. Office is free to users,
but configured AI calls may cost the operator.

## Boundaries

- Admin authorization fails closed; private callbacks check ownership.
- Only channel-membership infrastructure errors deliberately fail open. Confirmed
  non-membership blocks access; billing, bans and admin checks still apply.
- Receipts are validated, pixel-limited and re-encoded before private storage.
  Paths are validated before reads/deletion.
- Media uses fixed arguments, no shell and bounded child processes. Document parsing
  uses bounded threads, not an OS memory/CPU sandbox.
- Logs contain metadata/references, never keys, transcript/document text or receipts.
  Review redaction before sharing diagnostics.

Report vulnerabilities privately to the maintainer. Do not publish credentials,
user data or exploit details in public issues.
