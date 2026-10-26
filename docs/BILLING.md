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

## Learning loop (quizzes and AI flashcards)

Learning features bill the same integer-second ledger, per user, from booklets with extracted
text. Legacy items whose text was never stored cannot start a generation and are never charged.

- **Quiz.** The start screen offers 5, 10 or 15 questions. The price is
  `count × QUIZ_COST_SECONDS_PER_QUESTION` (default 60 s), i.e. 5, 10 or 15 credit minutes at the
  shipped tariff. The debit happens **before** the model call, with ledger note
  `quiz-pending:{item_id}`.
- **AI flashcard deck.** One flat charge of `FLASHCARD_AI_COST_SECONDS` (default 300 s = 5 minutes)
  per generated deck, regardless of its size (`FLASHCARD_AI_DECK_SIZE`, default 10 pairs).
  Manual decks and the Word export are free.
- **Full refund on every non-durable path.** If the model fails, returns malformed or too-short
  output (fewer than five valid questions/cards), the database write fails, or the task is
  cancelled (including shutdown), the full debit is refunded in one transaction
  (`refund: …` note) and the placeholder message is replaced with a failure notice. Refund errors
  are logged but never crash the handler. Unlimited users are never debited, so no refund is
  attempted for them.
- **One generation at a time per user.** A synchronous in-flight guard plus a per-user async lock
  reject a second tap while a generation is charging or awaiting the model, before any second
  debit. The guard is always released, even when cancellation strikes while waiting on the lock.
- **Admission limits, checked before the debit.** At most one active quiz per user (an untouched
  quiz lazily expires after `QUIZ_MAX_ACTIVE_HOURS`, default 6 h) and at most
  `QUIZ_MAX_SESSIONS_PER_HOUR` (default 3) new sessions in any rolling hour. Flashcard decks
  (AI and manual counted together) are capped at `FLASHCARD_MAX_DECKS_PER_USER` (default 30).
  A rejected admission costs nothing.
- Charges use the same lot-aware, earliest-expiry-first allocations as transcription, so refunds
  restore credit without disturbing other lots.

## Leaderboard and admin credit gift

Each correct quiz answer adds 10 all-time points to the user row. The board ranks users by total
points, hides Telegram identifiers, excludes banned users, and shows the caller's own rank.

From the board, an admin can gift the credit of a **paid plan only** (the lifetime free grant is
not giftable) to a ranked, non-admin, non-banned user: the exact plan seconds
(`round(hours × 3600)`) are credited, audited as `grant_credit`, and announced to the recipient by
best-effort DM. Gifts are cooldown-limited through the shared admin-action cooldown (30 s), so a
double-tapped confirmation cannot credit twice, and admins cannot gift to themselves or other
admins.

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
   Migrations `006_payment_entitlement_links.sql`, `007_payment_state_guards.sql` and
   `008_payment_transition_integrity.sql` link the credit to the payment, enforce valid forward-only
   transitions with reviewer metadata, keep terminal states immutable, and allow one credit row per
   payment across retries and concurrent reviewers.
5. If activation fails, the entire transaction rolls back to `pending_review`; the admin can safely
   fix the database issue and retry. Notification failures never undo or repeat activation.
6. A user may cancel an unsubmitted purchase. Only one request per user may be open. Users have a
   paginated payment history; admins have paginated pending-review and full-history screens, so a
   large backlog remains reachable within Telegram's message and keyboard limits.

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
bot against a migrated database. Migration 006 adds only a nullable ledger column and indexes.
Migration 008 replaces one status-update trigger and rewrites no rows. Migrations 011 and 012 add
the learning-loop columns and tables (quiz sessions/questions/answers, flashcard decks/cards);
their foreign keys cascade quiz data with a pruned library item while flashcard decks are retained
with a nulled item reference. All of them are additive: existing payments, credits, balances,
expiry dates, users and library rows are preserved, and legacy users default to an empty display
name and null extracted text. The v2 importer is a one-time, all-or-nothing operation:

```powershell
.\.venv\Scripts\python.exe scripts\import_v2.py --help
```

```bash
python scripts/import_v2.py --help
```

There is no payment gateway: a human verifies each transfer, and a charge cannot be reversed after
the transcript has been delivered.
