# Billing and payments

Credit is an append-only ledger of **integer seconds**. Never edit ledger history
to repair balances; use audited credit operations.

## Transcription

- Reserve measured source-audio duration before STT; refuse unmeasurable duration.
  An intake estimate is not permission to charge an unmeasured file.
- Charge only after the **complete transcript is delivered**. A later booklet
  failure leaves a partial job and the transcription charge stands.
- Pre-delivery failure/cancel releases the hold. Incomplete delivery is not billable;
  startup recovery releases interrupted holds.
- PowerPoint charges source narration, not inserted silence. No narration means free.
- Lots expire independently; holds preserve allocations. Refunds create new
  lots that inherit the earliest expiry of the original contributing lots;
  they never delete old debits.

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
contributing lots (including startup recovery); it is non-expiring only if all
contributing lots were non-expiring. Refund notes record contributing lot IDs.
Generic refunds require the original debit ID; an untraceable refund is rejected.
