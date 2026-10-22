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

Purchases are card-to-card and manually reviewed:

1. Creating a purchase stores a snapshot of its price, duration and validity.
2. Uploading a receipt does **not** grant credit. The image is validated and stored privately.
3. An admin approves or rejects. Approval credits the saved snapshot and is idempotent; rejection
   requires a reason.
4. A user may cancel an unsubmitted purchase. Only one request per user may be open.

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
bot against a migrated database. The v2 importer is a one-time, all-or-nothing operation:

```powershell
.\.venv\Scripts\python.exe scripts\import_v2.py --help
```

```bash
python scripts/import_v2.py --help
```

There is no payment gateway: a human verifies each transfer, and a charge cannot be reversed after
the transcript has been delivered.
