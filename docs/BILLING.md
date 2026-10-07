# Billing

## Model

Accounting is an append-only ledger in integer **seconds**, never a mutated
balance and never floating-point hours.

| Table | Meaning |
| --- | --- |
| `plans` | the selling plan (hours, price in Toman, validity days, enabled flag) |
| `entitlements` | what a user owns: granted seconds, remaining seconds, expiry, source |
| `usage_reservations` | what a running job currently holds |
| `usage_ledger` | every grant/reserve/consume/release event, with a reason |

The lifecycle of one job is `reserve → (finalize | release)`:

1. the *measured* media duration is reserved before transcription begins
   (`math.ceil`, minimum 1 s) — a file whose duration cannot be determined is
   refused rather than billed blindly;
2. on success the reservation is finalized for exactly that duration;
3. on any failure, cancellation or shutdown the reservation is released in
   full, with a reason, exactly once.

Recovery is part of this: at startup every `pending`/`processing` submission is
marked failed and its reservation is released, so a crash cannot leave credit
held forever.

## Plans

The canonical tariff lives in `config.py` (`CANONICAL_*` constants) and is
mirrored by `billing.PAID_PLAN_SPECS`; `tests/test_billing.py` pins the two
together and `tests/test_config_consistency.py` pins the `Settings` defaults.
Default plans: a 1-hour free grant, then 5/10/20/25/50-hour paid plans.
Every value is overridable per deployment (`FREE_PLAN_HOURS`, `PLAN_5_*`, …).

`sync_plan_catalog()` runs at startup and only refreshes plans the operator has
not touched: as soon as an admin edits, disables or deletes a plan in the
`🧾 طرح‌های فروش` panel, that plan stops being rewritten from the environment.

## Payments

Card-to-card, verified by a human admin:

1. the user asks for a plan, which creates a `payment_requests` row
   (one open request per user is enforced by a unique index);
2. the user sends a receipt image in private chat — content-signature checked,
   size bounded, stored `0600` in `RECEIPT_DIR`;
3. admins are notified and approve or reject with a required reason;
4. approval grants the entitlement idempotently (a unique index ties one
   entitlement to one payment), and the user is notified.

Receipts are deleted by a retention job after `RECEIPT_RETENTION_DAYS` and the
database records that the file is gone; the row itself is kept for accounting.

## Special (unlimited) users

`⭐ کاربران ویژه` are never billed. Their jobs are still recorded — so the audit
trail shows the usage — but no reservation is created. Admins cannot add other
admins to this list, and every change is written to the admin audit log.

## What is not implemented

There is no automated payment gateway, no refund flow and no proration. Adding
one would need a real provider integration and is deliberately out of scope.
