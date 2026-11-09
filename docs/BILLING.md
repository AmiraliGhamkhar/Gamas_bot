# Billing and payments

Credit is an append-only ledger of **integer seconds**. Never edit ledger history
to repair balances; use audited credit operations.

## The credit unit («اعتبار»)

Students buy in whole **credit** packages; the ledger keeps seconds. The exchange
rate is fixed in `gamas_bot/billing.py` and is the single definition used by every
screen, plan and receipt:

| | Value |
| --- | --- |
| Package | **100 000 اعتبار** |
| Package duration | **3 ساعت** = 10 800 seconds |
| Package price | **60 000 تومان** → 20 000 T/hour |
| Credits per hour | 33 334 (rounded up) |
| Credits per second | ≈ 9.26 |

`seconds_to_credits()` rounds **up** and `credits_to_seconds()` rounds **down**, so
a balance shown to the user is never overstated.

| Plan code | Duration | Credit | Toman |
| --- | --- | --- | --- |
| `free_1h` | 1 h | 33 334 (grant) | 0 |
| `paid_3h_30d` | 3 h | 100 000 | 60 000 |
| `paid_9h_30d` | 9 h | 300 000 | 180 000 |
| `paid_12h_30d` | 12 h | 400 000 | 240 000 |
| `paid_24h_30d` | 24 h | 800 000 | 480 000 |
| `paid_36h_30d` | 36 h | 1 200 000 | 720 000 |
| `paid_60h_30d` | 60 h | 2 000 000 | 1 200 000 |

Every paid plan is a whole number of packages with 30-day validity, so the price
per hour is identical everywhere. Plans are configured by `PLAN_*` in `.env`
(`config.PLAN_ENV_FIELDS`); a non-positive override falls back to the default and
`config.check()` reports it.

## Transcription

- Reserve measured source-audio duration before STT; refuse unmeasurable duration.
  An intake estimate is not permission to charge an unmeasured file.
- Charge once the transcript has been **produced and stored**. Delivery is
  button-driven (see below), so a chat that refuses a message is no longer a
  reason to discard a finished transcription — nor a reason to charge twice. A
  later booklet failure leaves a partial job and the transcription charge stands.
- Pre-delivery failure/cancel releases the hold. Incomplete delivery is not billable;
  startup recovery releases interrupted holds.
- PowerPoint charges source narration, not inserted silence. No narration means free.
- Lots expire independently; holds preserve allocations. Refunds create new
  lots that inherit the earliest expiry of the original contributing lots;
  they never delete old debits.

## Delivery and billing are separate

A finished job ends with **one** message — what was produced, how many speakers
were found — plus a button per artifact. Nothing is pushed unasked, and every
fetch afterwards is free:

| Artifact | Where the bytes come from | Cost |
| --- | --- | --- |
| 📖 Booklet (`.docx`) | forwarded from the user's library row | none |
| 📝 Raw transcript | rebuilt from the stored transcription row | none |
| ⭐ Key points | read from the stored `notes_json` | none |

Because fetching is a pure function of persisted data, the same request may be
pressed any number of times and the tenth answer is byte-identical to the first.
Nothing about a fetch can create, move or refund a ledger entry.

## Quizzes and AI flashcards

One free quiz and one independent free AI deck per user. Failed, cancelled,
undeliverable or expired previews restore the matching gift. These are separate
from free-plan credit. Unlimited users spend neither credit nor gifts.

| Paid action | Default charge |
| --- | --- |
| Quiz: 5 / 10 / 20 questions | 5 / 10 / 20 minutes (`QUIZ_COST_SECONDS_PER_QUESTION=60`) |
| AI deck: 10 / 20 / 40 cards | 5 / 10 / 20 minutes (`FLASHCARD_AI_COST_SECONDS=300`, base size 10) |
| Manual decks and Word export | Free |
| Office and document notes/exports | Free to users; operator API costs may apply |

Paid learning debits happen before generation with durable pending charges.
Saving finalizes; failure/cancel/30-minute preview expiry/startup recovery refunds
unfinished charges at most once. A failed preview edit is retried as a new message;
if delivery still fails, the preview remains pending until its normal expiry and
can be resumed through /start. Failed refund writes remain recoverable; never
claim success before commit. Valid results need at least 3 questions or 5 cards.
Count/coverage shortfalls are reported; review the preview before saving. Paid
previews saved with fewer items than requested receive an integer-second
pro-rata refund, rounded up, in the same transaction as the artifact.

Admission is checked before debit: one generation per user, 1 active quiz, 3 new
sessions per rolling hour and 30 stored decks by default. Inactive quizzes lazily
expire after 6 hours. Historical debits predating durable recovery cannot reliably
be reconstructed as unfinished operations.

## Manual purchases

Set `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER`, `PAYMENT_BANK_NAME`. Configure
plans with `FREE_PLAN_HOURS` and `PLAN_*` in `.env`. In v4, plan editing is
environment-only; there is no in-bot plan editor. Existing `is_custom` data
protection is retained for legacy records.

1. Selecting a plan freezes the order's price, duration and validity.
2. A validated receipt moves `awaiting_payment` to `pending_review`; it adds no credit.
3. An admin verifies the transfer, then approves or rejects with a reason.
4. Approval, credit and audit commit together. Failure rolls back; notification
   failures never repeat activation. Final decisions are immutable.

Administrators view stored receipt bytes as-is; viewing does not re-encode the
image or refresh its retention timestamp. A SHA-256 fingerprint of the normalized
image flags reuse by a non-cancelled order in the administrator alert; it does
not block upload or grant credit.

Users may cancel before receipt upload; one open order per user. Payment history
remains. Reviewed receipt images expire after 90 days by default; pending-review
receipts are not automatically deleted.

## Referral rewards

A first-ever `/start ref_<code>` attributes the invite once. The referrer earns
20 minutes when the invite is attributed and 1 hour when the invited user's first
payment is approved. Both are ordinary non-expiring ledger credits; the reward
table's unique key makes replayed starts and approvals idempotent, and the payment
reward commits inside the approval transaction. Receipt upload alone pays nothing.
Banned referrers are recorded but not paid. The invite screen and the admin
referral report show attributed users and reward seconds; no wallet operation or
learning gift is involved.

## Admin and upgrades

Grants, unlimited status, bans and gift resets are audited/cooldown-limited.
Protected actions cannot target self/peer admins. Unlimited releases holds; bans
block new/queued work without killing running jobs. Leaderboard gifts use paid-plan
credit only. See [Admin](ADMIN.md).

Back up before updates. Migrations run at startup; never downgrade against the
migrated database. Legacy v2 import is exceptional: inspect
`scripts/import_v2.py --help` and test on copies, never live data.

Refund credit inherits the earliest non-null expiry of the original debit's
contributing lots (including startup recovery), compared as UTC instants even for
legacy offset timestamps; invalid source expiries cause the refund to fail closed.
It is non-expiring only if all
contributing lots were non-expiring. Refund notes record contributing lot IDs.
Generic refunds require the original debit ID; an untraceable refund is rejected.

### Materialized balance cache

`user_balances` is a cache of FIFO ledger truth. SQLite triggers mark affected
users dirty, and the writer refreshes those rows before committing the same
transaction, including payment/referral rewards and hold recovery. Startup
backfills in batches and repairs a sample; offline import dirty rows are
recomputed before serving traffic. The cache records the next credit start or
expiry and is refreshed on read after that instant, or after a clock rollback.
Ledger rows and allocation records remain authoritative.
