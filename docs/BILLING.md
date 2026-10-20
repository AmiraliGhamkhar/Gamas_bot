# Billing

## Credit and jobs

Credit is an append-only ledger measured in integer seconds. A job reserves its measured source-audio duration before STT starts.

- Credit is charged after the complete raw transcript is delivered. A successful booklet completes as `done`; a later booklet failure is `partial` and the transcription remains billed.
- Failures or cancellations before complete transcript delivery release the hold. A truncated text fallback is not billable. A crash before confirmed delivery favors the user.
- PowerPoint billing uses source narration duration; inserted silence is not charged.
- Credits are tracked by lot and expire independently. New purchases are not reduced by old debits. Active holds keep their original allocations.
- Startup recovery closes interrupted work and releases any active hold.

## Plans and payments

The default catalog includes a free grant and paid hour bundles; prices and validity are configurable in `.env.example`. Admin changes to a plan are preserved. Payments are card-to-card and manually reviewed:

1. A purchase stores a snapshot of its price, duration and validity.
2. A receipt upload does **not** grant credit. Receipt images are size-checked, validated and kept in the private receipt directory.
3. An admin approves or rejects the request. Approval uses the saved snapshot and is idempotent; rejection requires a reason.
4. Users may resume a pending purchase or cancel it before submitting a receipt. A submitted receipt must be reviewed by an admin.

Set `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER` and `PAYMENT_BANK_NAME` to enable purchases. Receipt files are removed after `RECEIPT_RETENTION_DAYS`; payment records remain for accounting.

## Admin and user status

- Unlimited users are not charged. Enabling unlimited status releases active holds; turning it off affects new work only. Admins cannot change their own or another admin's status.
- Banned users cannot start new work. Queued jobs are checked before processing; already-processing work is not forcibly killed.
- Credit, ban and unlimited changes are audited and cooldown-limited.

## Database upgrades

Stop the bot and back up the database before deploying an upgrade. Migrations run automatically at startup. The v2 importer is a one-time operation; check its help before use:

```powershell
python scripts/import_v2.py --help
```

Do not run an older bot against a database after its schema has been upgraded. Card transfers are verified by a human; this bot has no payment gateway and cannot reverse a charge after transcript delivery.
