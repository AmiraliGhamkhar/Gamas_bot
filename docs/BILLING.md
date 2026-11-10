# Billing and payments

Credit («اعتبار») is the user-facing unit. The ledger itself stores **integer
seconds** and is append-only: never edit history to repair a balance, use the
audited operations instead.

## The unit

**100 000 credits = 3 hours = 10 800 seconds = 65 000 Toman** (≈ 21 667 T/hour).
`seconds_to_credits()` rounds up, `credits_to_seconds()` rounds down, so a shown
balance is never overstated. The arithmetic uses exact fractions — no float
crosses a billing boundary.

| Plan code | Duration | Credit | Toman |
| --- | --- | --- | --- |
| `free_1h` | 1 h | 33 334 (grant) | 0 |
| `paid_3h_30d` | 3 h | 100 000 | 65 000 |
| `paid_9h_30d` | 9 h | 300 000 | 195 000 |
| `paid_12h_30d` | 12 h | 400 000 | 260 000 |
| `paid_24h_30d` | 24 h | 800 000 | 520 000 |
| `paid_36h_30d` | 36 h | 1 200 000 | 780 000 |
| `paid_60h_30d` | 60 h | 2 000 000 | 1 300 000 |

Every paid plan is a whole number of 3-hour packages with 30-day validity, so the
hourly price is identical everywhere. Plans come from `PLAN_*` values in `.env`
(see [Configuration](CONFIGURATION.md#tariff)); a non-positive override falls
back to the default and `--check` reports it. Startup synchronizes only the
built-in active catalogue — orders, snapshots, custom plans and balances are
never rewritten.

## Transcription charges

- The measured **source duration** is held before STT. An unmeasurable file is
  refused rather than estimated.
- The charge is committed once the transcript has been produced and stored, so a
  chat that refuses a message does not cost the user twice or erase a finished
  transcript.
- Pre-delivery failure or cancel releases the hold; startup recovery releases
  interrupted holds.
- A later booklet failure leaves a `partial` job: the transcription charge stands,
  the booklet is simply not billed (it never is).
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
minutes on attribution and 1 hour when the invited user's first payment is
approved. Both are ordinary ledger credits; the payment reward commits inside the
approval transaction, so a replayed approval cannot pay twice. Bans are recorded
but not paid. Receipt upload alone pays nothing.

## Balance cache

`user_balances` is a cache of the FIFO ledger, refreshed inside the same
transaction as every write and on read after the next expiry instant. The ledger
rows and allocations remain the source of truth; never "fix" the cache by hand.

Back up before updates. Migrations run at startup; never run older code against a
newer database.
