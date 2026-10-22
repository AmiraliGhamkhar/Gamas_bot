# Billing

Credit is an append-only ledger in **integer seconds**. A job reserves its measured source-audio
duration before transcription starts.

## Charges

- The charge is applied after the **complete raw transcript is delivered**. A later booklet failure
  makes the job `partial` and keeps the transcription charge.
- Failures or cancellations before transcript delivery release the hold. A truncated text fallback
  is not billable. A crash before confirmed delivery favors the user.
- PowerPoint bills the measured source narration. Inserted silence is not charged. A deck with no
  narration reserves nothing (free), but delivery is still recorded.
- Unknown source duration is reserved at a conservative floor at intake; a file whose duration
  cannot be measured is refused rather than charged blindly.
- A user may hold at most `MAX_ACTIVE_JOBS_PER_USER` pending/processing submissions; the cap is
  enforced in the same transaction that creates the row.
- Credit lots expire independently. New purchases are never reduced by older debits. Active holds
  keep their original lot allocations.
- Startup recovery fails interrupted work and releases its hold.

## Plans and payments

The default catalog is a lifetime free grant plus paid hour bundles, configurable with the
`FREE_PLAN_HOURS` and `PLAN_*` variables. A non-positive tariff value is reported by
`--check` and the built-in default is used instead.

Purchases are card-to-card and manually reviewed. A purchase request moves through these states:

`awaiting_payment` (created, no receipt yet) → `pending_review` (receipt uploaded) → `approved` or
`rejected` (admin decision). `cancelled` is the owner's choice before a receipt is sent. Only
`approved` grants credit. Legacy `pending` rows are mapped to one of the new states by migration 005.

Steps:

1. Creating a purchase stores a snapshot of its price, duration and validity.
2. Uploading a Telegram photo or JPEG/PNG/WebP image document (immediately after choosing a plan, via
   «📤 ارسال رسید», or directly after a bot restart when an unpaid order exists) moves the order to
   `pending_review`; it does **not** grant credit. The image is validated, re-encoded and stored privately.
3. The admin opens «🔎 جزئیات سفارش» (from «💳 پرداخت‌های در انتظار» or «📋 تاریخچه پرداخت‌ها»),
   checks the receipt, and explicitly confirms approval or selects a preset / custom rejection reason.
   An order without a receipt cannot be approved or rejected.
4. Approval credits the saved snapshot in the same transaction as the status and audit updates.
   Migrations `006_payment_entitlement_links.sql` and `007_payment_state_guards.sql` link the credit
   to the payment and enforce valid statuses, terminal-state immutability, and one credit row per
   payment across retries and concurrent reviewers.
5. If activation fails, the entire transaction rolls back to `pending_review`; the admin can safely
   fix the database issue and retry. Notification failures never undo or repeat activation.
6. A user may cancel an unsubmitted purchase. Only one request per user may be open. Users have a
   paginated payment history, and admins have separate pending-review and full-history screens.

Set `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER` and `PAYMENT_BANK_NAME` to enable purchases.
Receipt images are removed after `RECEIPT_RETENTION_DAYS` (default 90); the payment row remains and
the deletion is audited (`receipt_expired`).

## Admin and user status

- Unlimited users are never charged; enabling the flag releases active holds. Admins cannot change
  their own status or another admin's.
- Banned users cannot start new work. Queued jobs are rejected before processing; running work is
  not killed.
- Credit, ban and unlimited changes are audited and cooldown-limited.

## Database upgrades

Back up the database before upgrading. Migrations run automatically at startup; do not run an older
bot against a migrated database. Migration 006 adds only a nullable ledger column and indexes; it
does not rewrite existing payments, credits, balances or expiry dates. The v2 importer is a
one-time, all-or-nothing operation:

```powershell
.\.venv\Scripts\python.exe scripts\import_v2.py --help
```

```bash
python scripts/import_v2.py --help
```

There is no payment gateway: a human verifies each transfer, and a charge cannot be reversed after
the transcript has been delivered.
