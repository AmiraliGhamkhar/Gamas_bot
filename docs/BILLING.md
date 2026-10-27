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

- **Quiz.** The three presets request 5, 10 or 20 questions, priced at `count ×
  QUIZ_COST_SECONDS_PER_QUESTION` (default 60 s), i.e. 5, 10 or 20 credit minutes. The debit happens
  before model calls, with ledger note `quiz-pending:{item_id}` (or `quiz-pending:doc:{document_id}`).
  The old 15-question callback remains valid for existing keyboards.
- **AI flashcard deck.** The 10, 20 and 40-card presets scale `FLASHCARD_AI_COST_SECONDS` (default
  300 s) relative to `FLASHCARD_AI_DECK_SIZE` (default 10), i.e. 5, 10 and 20 credit minutes with
  defaults. Manual decks and Word export are free; the legacy default-size button is retained.
- **First try and curation.** Each user has an independent free AI deck and free quiz, using the same
  models, presets and validation as paid generations. A conditional SQLite update atomically claims
  the appropriate gift with a unique token before the LLM. The preview is delivered before save;
  the artifact and claim finalization are committed together. Cancel, an undeliverable or expired
  preview (30 minutes), provider failure, insufficient valid content or cancellation restore the
  matching gift; a reset or newer claim cannot be undone by an older job's refund. After the gift,
  existing credit/paywall behavior applies, including the separate free-plan credit grant if present.
  Unlimited users are never charged and do not consume a gift. A confirmed admin reset restores
  both and writes `free_use_reset` to the audit log.
- **Recovery and refunds.** Startup, after the singleton lock, restores unfinished free claims and
  refunds unfinished paid claims. A paid debit, its lot allocations and a `learning_charges` pending
  record are committed together; saving the deck or quiz finalizes that record in the artifact
  transaction. Cancel, failure, preview expiry and startup recovery refund a pending record at most
  once. Graceful shutdown also joins active learning work before closing SQLite. If a refund write
  fails, the pending record remains recoverable on restart, the error is logged, and the user is
  directed to support rather than told the refund succeeded. A valid result requires at least five
  cards or three questions; partial coverage is reported so a user may cancel rather than save.
  Historical paid debits from before migration 015 have no durable operation identity and cannot be
  reliably or automatically distinguished from completed generations.
- **One generation at a time per user.** A synchronous in-flight guard plus a per-user async lock
  reject a second tap while a generation is charging or awaiting the model, before any second
  debit. The guard is always released, even when cancellation strikes while waiting on the lock.
- **Admission limits, checked before the debit.** At most one active quiz per user (an untouched
  quiz lazily expires after `QUIZ_MAX_ACTIVE_HOURS`, default 6 h) and at most
  `QUIZ_MAX_SESSIONS_PER_HOUR` (default 3) new sessions in any rolling hour. Flashcard decks
  (AI and manual counted together) are capped at `FLASHCARD_MAX_DECKS_PER_USER` (default 30).
  A rejected admission costs nothing.
- Charges use the same lot-aware, earliest-expiry-first allocations as transcription. Refunds are
  issued as a new non-expiring credit lot; the original debit allocations remain in the ledger.

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
with a nulled item reference. Pruning preserves sources of active quizzes (temporarily allowing the
library to exceed its configured reference cap); ended/expired quizzes may cascade on a later prune.
Migration 013 adds identity, document caches and free-use flags; 014 adds owned recovery tokens,
quiz difficulty, and rebuilds `library_items` to declare its user foreign key while preserving ids,
content and child links. Legacy library rows missing an owner get an exact-id placeholder user.
Migration 015 adds `learning_charges` for paid generation recovery without rewriting existing ledger
or payment rows; old debits cannot be reconstructed automatically. The v2 importer is a one-time,
all-or-nothing operation:

```powershell
.\.venv\Scripts\python.exe scripts\import_v2.py --help
```

```bash
python scripts/import_v2.py --help
```

There is no payment gateway: a human verifies each transfer, and a charge cannot be reversed after
the transcript has been delivered.
