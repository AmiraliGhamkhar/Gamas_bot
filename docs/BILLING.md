# Billing

## Model

Accounting is an append-only ledger in integer **seconds**. There is no mutated
balance column and no floating-point hours.

| Table | Meaning |
| --- | --- |
| `plans` | Selling plans: hours, price in Toman, validity days, `is_custom`, active |
| `ledger` | Every credit event — grant, charge, adjustment — with a note and expiry |
| `holds` | What a running job currently has reserved, and how it ended |
| `hold_allocations` | The specific credit lots reserved by each hold |
| `debit_allocations` | The specific credit lots consumed by each debit |
| `payments` | Card-to-card requests, receipt path, review outcome |

One job's lifecycle is `reserve → (settle | release)`:

1. the **measured** media duration is reserved before transcription begins
   (`math.ceil`, minimum 1 s) — a file whose duration cannot be determined is
   refused rather than billed blindly;
2. after **complete raw-transcript delivery**, `settle_hold` charges that duration,
   never more than was reserved, and records `submissions.delivered_at` in the
   same transaction;
3. before that milestone, failure, cancellation or shutdown releases the hold
   exactly once. Releasing an expired reservation does not revive expired credit;
4. after that milestone, a booklet failure, cancellation or restart records
   `partial`, retains the transcription charge, and still counts the delivered
   transcription in usage statistics. A successful booklet ends in `done`.

Incomplete/truncated raw-text fallback delivery is not billable. The full
transcript remains persisted for support, but the reservation is released.
Telegram delivery and SQLite cannot be one atomic transaction: a crash after
Telegram accepts a transcript but before settlement may leave the delivery
unbilled; recovery favors the user and never bills an unconfirmed delivery.

Credits are separate **lots**, allocated earliest-expiry-first (permanent grants
last). Available balance is the sum of unexpired lots' unused seconds, less
active allocations against those lots. Historical debits never reduce a later
purchase. A hold retains its original lots even if they expire while work is
running; settlement consumes those lots, not a new purchase.

All financial/status writes use a serialized `BEGIN IMMEDIATE` transaction with
rollback on exceptions and cancellation. Settlement checks the hold and user
flag inside that transaction and has a unique submission/debit reference.
Concurrent settle/release attempts therefore have one winner.

For a PowerPoint deck, the measured **source narration duration** is reserved and
settled. Synthetic silence inserted between clips is not billable. The separate
`PRESENTATION_MAX_TOTAL_DURATION` safety cap is applied to the merged STT artifact,
so it includes those gaps; this prevents many short clips from exceeding the STT
limit while avoiding charges for padding. See [PRESENTATIONS.md](PRESENTATIONS.md)
for clip-span and alignment behavior.

`recover_interrupted()` runs at startup: interrupted `pending`/`processing`
submissions become `failed` before raw delivery or `partial` after delivery.
Any still-active hold is released, so a crash cannot leave credit held forever.

## Plans

The catalog lives in `billing.PAID_PLAN_SPECS`; `plan_catalog()` renders it with
per-deployment overrides. Defaults: a 1-hour free grant, then 5 / 10 / 20 / 25 /
50-hour paid plans at 50,000 / 75,000 / 130,000 / 150,000 / 250,000 Toman, 30
days validity. Every value is overridable (`FREE_PLAN_HOURS`, `PLAN_5_HOURS`,
`PLAN_5_PRICE_TOMAN`, `PLAN_5_VALIDITY_DAYS`, …).

`sync_plans()` runs at startup and only refreshes plans the operator has not
touched: as soon as an admin edits, disables or deletes a plan in the
**🧾 طرح‌های فروش** panel, `is_custom` is set and the environment stops
overwriting it.

## Payments

Card-to-card, verified by a human admin:

1. the user asks for a plan → a `payments` row with the **purchase terms frozen
   on it** (`purchased_seconds`, `purchased_price`, `purchased_validity_days`).
   One open request per user is enforced by a partial unique index; tapping the
   same plan again returns the existing request, and a different plan is refused
   until the open one is reviewed or cancelled before receipt submission;
2. the user sends a receipt image in private chat — checked by content signature
   at upload and again before an admin views it, size-bounded, stored `0600` in
   `RECEIPT_DIR`;
3. admins are notified and approve or reject. Approval credits the snapshot; a
   payment whose snapshot is missing is **never** marked approved. Rejection
   asks the admin for a reason, which is stored on the row and in the audit
   entry written in the same transaction;
4. approval credits the ledger idempotently (one entitlement per payment), and
   the user is notified with the amount actually credited.

Because the snapshot is the source of truth, deactivating, deleting or
re-pricing a plan between purchase and review cannot change — or zero — the
credit, and the admin revenue figure is the sum of what users were quoted, not
the current catalogue price.

The purchase screen always renders the persisted snapshot, including when a
request is reopened after a catalog change. The account panel offers **resume**
for pending requests even if their plan is no longer active. It offers **cancel
purchase** only before a receipt is submitted; cancellation is owner-checked,
persisted as `cancelled`, and frees the one-open-request slot. Requests with a
receipt require admin review, not user cancellation. Receipt attachment and
cancellation are serialized, so both cannot win. A reviewed/cancelled request
cannot accept a late upload.

`/cancel` or navigating away only cancels the in-memory upload prompt, not the
purchase; the Persian UI states this explicitly. Reopen the account panel to
resume or cancel a purchase, including after a restart. Already-submitted
receipts are identified as awaiting review; users are told not to pay again.

Uploading a receipt grants nothing. Receipts are deleted after
`RECEIPT_RETENTION_DAYS`; the sweep runs once at startup and then daily, so a bot
that restarts at least once a day still enforces retention. The payment row is
kept for accounting.

## Special (unlimited) users

A special user is never billed. Admins manage the flag from chat:

```
/unlimited <user_id> on <reason>   # reason required and stored
/unlimited <user_id> off
```

* No reservation is taken, so no balance is checked and nothing is debited. The
  submission row still records `duration_seconds`, so the audit trail shows usage.
* Enabling unlimited status immediately releases active holds in the same
  transaction as the flag and audit entry. Turning it off again does not
  retroactively charge those jobs; new reservations use normal credit rules.
* Balance screens show «نامحدود»; the admin user list marks them ⭐.
* Admins cannot be made special, and nobody can change their own flag. Admins are
  billed like everyone else.
* Every change is written to `audit` (`unlimited_on` / `unlimited_off`) with the
  admin id. Turning it off clears the reason and provenance.

## Banned users

Bans reject new messages/callbacks and queued jobs at worker entry, before any
pipeline/download work. Reservation also rechecks the ban under the write lock,
including for unlimited users. Jobs already processing are not forcibly killed;
if they already reserved credit, normal delivery/settlement rules apply. A job
that has not yet reserved is denied at reservation. The ban flag and audit entry
are committed or rolled back together. Admin accounts remain protected by the
command handlers.

## Upgrade and legacy accounting

Migration `004_credit_allocations.sql` adds lot allocation tables, a unique
submission/debit reference, and the raw-delivery timestamp. Startup performs a
one-time, transactional backfill (`billing_migrations.credit_lots_v1`) before
accepting jobs. It is safe to retry after failure and does not rewrite ledger
amounts. The v2 importer now preserves the exact entitlement-to-debit allocation
available in its source data.

Legacy v3 did not store consumption allocation. Backfill reconstructs it using
earliest expiry at the reservation time when a settled hold can be identified,
or otherwise at debit time. Credits created later are never used to fund an
older debit. Active legacy holds also receive allocations. An unfunded legacy
event is logged with its ID and unallocated seconds; it remains in the audit
ledger, but is not carried as debt against future purchases. Legacy settled
submissions receive a delivery milestone and historically failed, settled jobs
become `partial`.

**Deployment:** stop the bot, back up the database (including any live WAL via a
SQLite-aware backup), deploy, then restart. Inspect startup migration warnings
and reconcile any unfunded legacy events with the original records. Exact
historical lot choices and incomplete legacy payment approvals cannot be
recovered from missing data; this upgrade does not invent entitlements, refund
historical duplicate charges, or silently modify payment amounts. Do not run an
old binary against a database that has begun using the allocation model.

## Tests

`tests/test_db_billing.py`, `tests/test_workflow_regressions.py`,
`tests/test_bot.py`, and `tests/test_import_v2.py` cover free grants, allocations,
expiry/renewal, active holds across expiry, concurrent and cross-connection
reservations, exactly-once settlement, release races, unlimited transitions,
transaction failure/cancellation rollback, migration retry/restart, imported
balances, purchase snapshots, cancellation ownership/receipt races, queued bans,
and partial-success usage accounting.

## Not implemented

No payment gateway, post-delivery automatic refunds, or proration. Card-to-card
payments still require human verification; database approval is not proof of a
bank transfer.
