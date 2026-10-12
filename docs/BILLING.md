# Billing

## Model

Accounting is an append-only ledger in integer **seconds**. There is no mutated
balance column and no floating-point hours.

| Table | Meaning |
| --- | --- |
| `plans` | Selling plans: hours, price in Toman, validity days, `is_custom`, active |
| `ledger` | Every credit event — grant, charge, adjustment — with a note and expiry |
| `holds` | What a running job currently has reserved, and how it ended |
| `payments` | Card-to-card requests, receipt path, review outcome |

One job's lifecycle is `reserve → (settle | release)`:

1. the **measured** media duration is reserved before transcription begins
   (`math.ceil`, minimum 1 s) — a file whose duration cannot be determined is
   refused rather than billed blindly;
2. on success `settle_hold` charges exactly that duration, never more than was
   reserved;
3. on any failure, cancellation or shutdown `release_hold` returns it in full,
   with a reason, exactly once.

`recover_interrupted()` runs at startup: every `pending`/`processing` submission
is marked failed and its hold released, so a crash cannot leave credit held
forever.

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

1. the user asks for a plan → a `payments` row (one open request per user,
   enforced by a unique index);
2. the user sends a receipt image in private chat — checked by content signature,
   size-bounded, stored `0600` in `RECEIPT_DIR`;
3. admins are notified and approve or reject with a required reason;
4. approval credits the ledger idempotently (one entitlement per payment), and
   the user is notified.

Uploading a receipt grants nothing. Receipts are deleted after
`RECEIPT_RETENTION_DAYS`; the row is kept for accounting.

## Special (unlimited) users

A special user is never billed. Admins manage the flag from chat:

```
/unlimited <user_id> on <reason>   # reason required and stored
/unlimited <user_id> off
```

* No reservation is taken, so no balance is checked and nothing is debited. The
  submission row still records `duration_seconds`, so the audit trail shows usage.
* A job already running when the user became special is released, not charged.
* Balance screens show «نامحدود»; the admin user list marks them ⭐.
* Admins cannot be made special, and nobody can change their own flag. Admins are
  billed like everyone else.
* Every change is written to `audit` (`unlimited_on` / `unlimited_off`) with the
  admin id. Turning it off clears the reason and provenance.

## Tests

`tests/test_db_billing.py` covers the free grant being once-only, reserve →
settle charging the measured duration, reserve → release returning everything,
settle never exceeding the reservation, insufficient balance, expired credit,
concurrent reservations, startup recovery, the whole special-user matrix, payment
approval crediting exactly once, and catalog sync protecting custom plans.

## Not implemented

No payment gateway, no refunds, no proration. Adding one needs a real provider
integration and is deliberately out of scope.
