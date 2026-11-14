# Billing and payments

Credit («اعتبار») is the user-facing unit. The ledger itself stores **integer
seconds** and is append-only: never edit history to repair a balance, use the
audited operations instead.

## The unit and standard catalog

**100 000 credits = 3 hours = 10 800 seconds.** This is only the usage-unit
conversion; it does not define plan prices. `seconds_to_credits()` rounds up,
`credits_to_seconds()` rounds down, so a shown balance is never overstated. The
arithmetic uses exact fractions — no float crosses a billing boundary.

| Plan code | Duration | Credit | Original/list price | Final/sale price |
| --- | ---: | ---: | ---: | ---: |
| `free_1h` | 1 h | 33 334 (grant) | — | 0 |
| `paid_3h_30d` | 3 h | 100 000 | 160 000 | 79 000 |
| `paid_6h_30d` | 6 h | 200 000 | 200 000 | 99 000 |
| `paid_10h_30d` | 10 h | 333 334 | 220 000 | 109 000 |
| `paid_20h_30d` | 20 h | 666 667 | 360 000 | 179 000 |
| `paid_26h_30d` | 26 h | 866 667 | 460 000 | 229 000 |
| `paid_30h_30d` | 30 h | 1 000 000 | 600 000 | 299 000 |

All six paid plans have 30-day validity. Original/list and final/sale prices
are explicit per plan; they are not derived from hours or the credit conversion.
The wallet shows both prices, and new payment snapshots and charges use only the
final/sale price. The 50-hour tier is intentionally not configured or sold
until both of its prices are supplied.

Plans are centralized in `billing.PAID_PLAN_SPECS`. Optional `PLAN_*` settings
in `.env` override each duration, original price, sale price and validity;
non-positive overrides fall back to the corresponding catalog default and
`--check` reports them. A full startup sync retires obsolete built-in plans
without touching custom plans, payment history, snapshots or balances; partial
syncs do not deactivate unrelated plans.

## Transcription charges

- The measured **source duration** is held before STT. An unmeasurable file is
  refused rather than estimated.
- The charge is committed once the transcript has been produced and stored, so a
  chat that refuses a message does not cost the user twice or erase a finished
  transcript.
- Pre-delivery failure or cancel releases the hold; startup recovery releases
  interrupted holds.
- A later booklet or results-notification failure leaves a `partial` job when
  the base transcript was retained. The measured transcription charge stands
  even if no PDF booklet was persisted: booklet generation is optional and has
  no separate user charge. Only a persisted PDF artifact counts as a
  booklet-ready result; a DOCX alone does not count as the PDF booklet.
- Failure/restart recovery uses a saved non-empty transcript to distinguish
  `partial` from `failed`; a billing settlement or `delivered_at` timestamp by
  itself is not evidence that transcript content was persisted.
- PowerPoint charges the source narration, not inserted silence. A deck without
  narration is free.

## Learning charges

| Action | Default cost |
| --- | --- |
| Quiz, 5 / 10 / 20 questions | 5 / 10 / 20 minutes (`QUIZ_COST_SECONDS_PER_QUESTION=60`) |
| AI deck, 10 / 20 / 40 cards | 5 / 10 / 20 minutes (`FLASHCARD_AI_COST_SECONDS=300`, base size 10) |
| Manual decks, Word/PDF export, re-download | free |
| Office and document notes/exports | free to users; provider calls cost the operator |

A paid generation debits before the provider call with a durable pending record.
Saving finalizes; failure, cancel, 30-minute preview expiry or startup recovery
refunds once. If fewer items are produced than requested, the shortfall is
refunded pro rata in the same transaction that stores the artifact. At least 3
questions or 5 cards are required for a valid result.

Admission is checked before the debit: one generation per user at a time, one
active quiz, 3 new sessions per rolling hour, 30 stored decks. Each user keeps
one free quiz and one free AI deck; unlimited users spend neither gifts nor
credit.

## Purchases (card to card, manual review)

```dotenv
PAYMENT_CARD_NUMBER=...
PAYMENT_CARD_HOLDER=...
PAYMENT_BANK_NAME=...
```

1. Choosing a plan freezes price, duration and validity on the order.
2. A validated receipt moves `awaiting_payment → pending_review`; no credit yet.
3. An admin verifies the transfer and approves or rejects with a reason.
4. Approval, credit and audit commit together; notifications never repeat it.

One open order per user; cancelling before the receipt is allowed and payment
history stays. Admin procedure: [Admin](ADMIN.md#payments-review).

## Refunds

A refund creates a **new non-expiring lot only if every contributing lot was
non-expiring**; otherwise it inherits the earliest expiry of the contributing
lots, compared as UTC instants. Refunds require the original debit — an
untraceable refund is refused, and a failed refund stays recoverable rather than
being reported as success.

## Referrals

`/start ref_<code>` attributes a first-ever invite once. The referrer earns 20
minutes on attribution and at most one 1-hour reward for the invited user's first
approved payment. Both are ordinary ledger credits; the payment reward and its
first-payment decision commit inside the approval transaction. Receipt upload
alone pays nothing. If the referrer is banned when that first payment is
approved, the payment reward is permanently forfeited; a later purchase or
unban cannot reopen qualification. Existing reward rows are not reversed or
rewritten.

## Legacy v2 credit import

Run the one-shot importer with a separate fresh v3 target:

```bash
python scripts/import_v2.py --from data/gamas_v2.db --into data/gamas.db
```

The source is opened read-only; the target refuses an existing user population,
and the data import commits or rolls back as one transaction. v2 and v3 both
store balances in integer seconds, so eligible balances convert at **1 second to
1 second** (the user-facing credit tariff is not applied during migration).
Each active, unexpired entitlement becomes a v3 credit for `granted_seconds`
and, if any seconds were consumed, an allocated debit for `granted_seconds -
remaining_seconds`; the credit expiry and start timestamp are preserved, so a
future-start entitlement is not available early. Malformed active entitlement
timestamps abort the transaction rather than becoming non-expiring credit. The
importer verifies every non-unlimited user's total and currently available
balance against eligible v2 entitlements, checks payment ownership/linkage,
SQLite foreign keys and database integrity, then commits.

Expired or revoked entitlements are not reactivated or imported into the v3
balance. The original v2 `usage_ledger`, `admin_audit_log`, and audio
submission/transcription rows remain untouched in the source database as the
historical archive; those event/content histories are not copied into v3 by this
credit importer. Imported payment rows retain their original v2 request ID as a cross-reference,
and active paid entitlement credit rows point at the corresponding v3 payment.
The v3 ledger still records migrated consumption as an allocated debit; this is
not a new charge. The importer command is not an in-place repair or merge tool.

Known limitation: v2 payment requests store their actual `amount_toman`, and
linked entitlements preserve seconds actually granted, but orders do not freeze
validity days (or seconds for orders with no entitlement). The importer preserves
the order amount and uses the entitlement grant when present; missing historical
plan terms fall back to the source catalogue's current values. The source v2
database remains the authority for any purchase terms not represented in those
rows.

## Balance cache

`user_balances` is a cache of the FIFO ledger, refreshed inside the same
transaction as every write and on read after the next expiry instant. The ledger
rows and allocations remain the source of truth; never "fix" the cache by hand.

Back up before updates. Migrations run at startup; never run older code against a
newer database.
