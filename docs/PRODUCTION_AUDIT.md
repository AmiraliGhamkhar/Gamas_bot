# Production payment and support audit

Date: 2026-10-22  
Starting revision: `9f4cc72106b9492a44e06b86b28d27e63fe0f20f`  
Working branch: `arena/3985065e-gamas-bot`

## Scope and baseline

The checkout was inspected before editing: branch/worktree, Python dependencies, Telegram handlers,
SQLite schema/migrations, tariff configuration, tests, cron/Passenger startup and cPanel guides. No
production `.env`, database, Telegram session or receipt files were present or modified.

The starting revision already contained most of the requested manual-payment and support-ticket
implementation. Its baseline suite passed before changes: **857 tests and 38 subtests**. Accordingly,
this was a targeted hardening task, not a rewrite. No duplicate-credit or lost-ticket defect was
claimed without evidence.

Architecture retained: Python 3.11+, Telethon, asyncio, aiosqlite/SQLite, the existing job queue,
ledger, admin configuration and logs. No dependency, gateway, broker, external database or dashboard
was added.

## Existing flow traced

### Billing and manual payment

- `buy` renders active plans from `plans`; `plan:<code>` calls `Database.create_payment`.
- Order creation snapshots `purchased_seconds`, `purchased_price` and `purchased_validity_days`.
  Hours are converted to integer seconds with `hours * 3600`.
- `payr`, direct image upload and the post-restart fallback store a validated/re-encoded receipt.
  `set_payment_receipt` checks ownership and moves only an unpaid order to `pending_review`.
- Admin callbacks beginning with `adm:` or `payv:` are gated by `Settings.admin_ids`, parsed from
  numeric `ADMIN_IDS`. Callback IDs are looked up in SQLite and are not treated as payment data.
- `review_payment` performs the status, reviewer audit and payment-linked credit insert in one
  `BEGIN IMMEDIATE` transaction. A unique partial ledger index prevents a second credit per payment.
- Approval requires a receipt and a positive frozen entitlement/price. Rejection requires a reason.
  Terminal orders cannot be reviewed again. Activation errors roll back to `pending_review`.
- Telegram notifications happen after commit and are best-effort; delivery failure does not roll back
  or repeat activation. User/admin histories are persistent.
- Existing balances remain ledger-derived. Free credit is granted once; credit lots accumulate and
  expire independently. No tariff, balance, hold, consumption or expiration rule was changed.

### Support

- `support` / `sup:*` implements category selection, ticket creation, user history, replies, close and
  reopen. `adm:sup` / `adm:s*` implements admin filters, paginated histories, replies, close/reopen.
- `support_tickets`, `support_messages` and `support_ticket_seq` persist UTC ISO-8601 timestamps and
  ordered messages. Public IDs have the form `T-000001`.
- `Database.SUPPORT_TRANSITIONS` centrally defines `open`, `awaiting_admin`, `awaiting_user` and
  `closed` transitions. Ownership is checked for user reads/writes/actions; admin routing uses the
  ticket selected through a verified admin callback.
- Storage precedes notification. Creation/user replies notify every configured admin; admin replies
  notify the owner. Delivery errors are logged by ticket/user ID without message contents.

## Findings in the starting code and resolutions

| Finding | Root cause | Resolution |
| --- | --- | --- |
| A large pending-payment backlog could make the admin screen exceed Telegram text/keyboard limits. | `_show_admin_payments` loaded and rendered every reviewable order; the overview also loaded all rows just to count them. | Added bounded database pagination/counting and `adm:pay:<page>` navigation. Invalid and non-admin callbacks remain rejected. |
| SQLite did not fully enforce the documented forward-only payment state machine. | Migration 007 allowed backward transitions and direct SQL decisions without complete reviewer/reason metadata. | Added migration 008, replacing only the UPDATE trigger. Receipt-backed review, positive snapshots, reviewer metadata, rejection reasons, cancellation rules and terminal immutability are enforced. Legacy `pending` review remains supported. |
| A submitted order could become hidden when new purchases were disabled. | `_show_payment` checked `PAYMENT_CARD_NUMBER` before displaying an already-submitted receipt status. | Submitted status no longer depends on the current card setting. Unsubmitted orders remain cancellable and new orders still require configured payment instructions. |
| New tickets did not appear in the admin «📂 جدید» filter. | Creation stores the first user message and therefore enters `awaiting_admin`, while that filter queried only `open`. | The new/open filter now includes both `awaiting_admin` and `open`; the separate waiting filter remains available. |
| Some copy was unnecessarily long or misleading. | Repeated first-step explanations and “no receipt” wording for a reviewed receipt removed by retention. | Shortened main/plan/payment copy, added actionable next steps, and distinguished an unavailable reviewed receipt from one never submitted. |

Existing protections that were retained rather than duplicated include frozen plan snapshots,
exactly-once entitlement links, atomic activation rollback, receipt image validation, private receipt
storage, admin authorization, ticket ownership, central ticket transitions and notification-failure
isolation.

## Files changed

Runtime and migration:

- `gamas_bot/bot.py`
- `gamas_bot/db.py`
- `migrations/v3/008_payment_transition_integrity.sql` — new; no row rewrites or new columns

Tests:

- `tests/test_payment_states.py`
- `tests/test_payment_workflow.py`
- `tests/test_support.py`
- `tests/test_workflow_regressions.py`

Documentation:

- `docs/BILLING.md`
- `docs/CHANGELOG.md`
- `docs/DEPLOY_CPANEL.md`
- `docs/DEPLOY_FA.md`
- `docs/SUPPORT.md`
- `docs/PRODUCTION_AUDIT.md`

No change was needed in `billing.py`, `config.py`, dependencies, environment variables, cPanel startup
commands or existing plan definitions.

## Migration and rollback safety

Migration 008 is backward-compatible with existing v3 data: it drops/recreates one trigger and does
not update payment, ledger, user or ticket rows. Existing approved legacy rows are not forced to gain
new metadata. Fresh and upgraded temporary databases were exercised by the tests.

Back up the stopped SQLite database and sidecars before deployment. Do not run older code against the
migrated database unless the matching pre-upgrade backup is restored. See `DEPLOY_CPANEL.md` for exact
backup, restart and rollback commands.

## Verification actually executed

Final results on Python 3.11.2:

- `python -m pytest -q`: **862 passed, 38 subtests passed; 0 failed; 0 skipped**.
- `python -m unittest discover -s tests -q`: **862 tests, OK**.
- `ruff check .`: passed.
- `mypy`: passed for the 12 files in the project's incremental gate.
- `compileall` for `gamas_bot`, `scripts`, `tests` and `passenger_wsgi.py`: passed.
- `pip check` and `scripts.sync_requirements --check`: passed; dependencies unchanged.
- Fresh migration smoke check: **8 migrations and 14 application tables** verified.
- `python -m gamas_bot.media_worker check`: passed (`av 18.1.0`, `ppt2pptx 0.4.2`).
- `python -m gamas_bot --check` with synthetic non-production settings: passed, including imports,
  configuration and media worker. The expected advisory noted that note generation was not configured.

## Post-merge independent verification

Date: 2026-10-22
Merge commit verified: `81ca4ab` (PR #85), working branch `arena/736cab45-gamas-bot`

After the merge, the complete workflow was re-traced against `gamas_bot/bot.py` and
`gamas_bot/db.py` and re-verified on Python 3.11.2 with a fresh virtualenv:

- `python -m pytest -q`: **871 passed, 38 subtests passed; 0 failed; 0 skipped**
  (the pre-existing 862 tests plus 9 new acceptance tests, see below).
- `python -m unittest discover -s tests -q`: **871 tests, OK**.
- `ruff check .`, `mypy` (12-module incremental gate), `python -m compileall -q gamas_bot scripts
  tests passenger_wsgi.py`, `scripts.sync_requirements --check`: all passed.
- Fresh-database migration smoke check: all 8 v3 migrations apply and the 14 expected application
  tables exist, including `support_tickets`, `support_messages` and `support_ticket_seq`.
- `python -m gamas_bot.media_worker check`: passed.
- `python -m gamas_bot --check` with synthetic settings (including a card-to-card setup): passed.

New acceptance coverage in `tests/test_acceptance_mission.py`, one executable check per
acceptance item against the real handlers and the exact user-visible Persian copy:

- Plan screen lists the canonical 25-hour/150,000-toman and 50-hour/250,000-toman plans; the order
  screen shows the configured card instructions.
- An order cannot be approved before a receipt exists (an early `payv:*:a:yes` tap changes
  nothing); receipt upload moves it to `pending_review` with the exact awaiting-review copy and
  the administrator receives the order reference, user ID, plan, amount and the receipt file.
- Approval requires the confirmation tap, credits exactly once (repeated taps and the unique
  ledger index verified), and the user receives the exact approval copy only after the commit.
- Rejection delivers the reason, grants nothing, and a later approval attempt is refused;
  cancellation and preset rejection reasons leave a zero balance with the history copy intact.
- Catalogue invariants: 25 h → 90,000 s / 150,000 t / 30 d, 50 h → 180,000 s / 250,000 t / 30 d,
  free 1 h granted exactly once.
- Tickets are created in all four categories with unique `T-000000` IDs and the exact creation
  copy; the admin answer marks `awaiting_user` with the exact reply-notification copy; the user
  answer marks `awaiting_admin` and alerts the administrators.
- Close and reopen preserve history; a new ticket is a separate conversation.
- Forged verdicts, non-admin `adm:`/`payv:` callbacks and cross-user ticket access are refused.
- A simulated Telegram outage loses nothing: stored tickets/messages and the approved credit stay
  intact and repeated approval still credits once.
- This v4 UI replaces the earlier mission-button menu with one direct lecture-upload action and
  four user buttons: «📚 کتابخانه من», «🛒 خرید اشتراک», «🎧 پشتیبانی» and «ℹ️ راهنما». Admins retain
  their separate payment, history and support panel; the library forwards only the requesting user's
  Telegram-backed artifacts.

No runtime code changes were required by this verification; production data, tariffs, expiry
rules and existing entitlements are untouched.

New coverage includes independent-connection concurrent approval, illegal backward/unaudited SQL
transitions, large admin backlog pagination and forged page callbacks, multiple-admin support
notifications/reply routing, and submitted-order visibility with new purchases disabled. Existing
coverage exercises voice/media, STT/notes, document exports, credit accounting, admin commands,
migration preservation and cPanel launcher behavior with fakes.

Not executed: live Telegram delivery, real provider calls, a real cPanel restart, or acceptance tests
against production data. Those require the deployment environment and credentials; no such operation
was attempted.

## Remaining limitations and risks

- Telegram notification delivery remains best-effort, as required; there is no separate retry outbox.
  Persistent order/ticket history is authoritative after a delivery outage.
- Support messages are text-only; attachments are not stored.
- Migration 008 intentionally rejects out-of-band SQL updates that bypass the documented transitions.
  Operational tooling must use the service methods rather than manually setting payment statuses.
- The local suite uses one Python 3.11 runtime. CI remains configured for 3.11, 3.12 and 3.13.
- Real provider quality/latency and host-specific process limits cannot be proven by fake-based tests.

## Manual acceptance checklist

1. Create a Standard order; verify 25 hours, 30 days and 150,000 Toman. Submit a photo and confirm no
   credit is granted before review.
2. Open the receipt as an authorized admin. The first approval tap must only request confirmation;
   final approval adds exactly 90,000 seconds and records reviewer/time.
3. Tap stale approval again and confirm balance and payment-linked credit do not change. Reject a
   separate order and verify its reason is shown and no credit is added.
4. Verify user/admin histories after restart. With more than eight pending orders, use both admin
   pages and open an order from each.
5. Create a ticket in each category, exchange user/admin replies, close and reopen it, and confirm the
   complete earlier history and correct status remain. Repeat with a second authorized admin.
6. Simulate/block Telegram delivery where possible and confirm stored payment/ticket state survives.
7. Send a short audio file without pressing a mode button; verify transcript → booklet delivery and
   both library references. Open the wallet and check its balance/plans/history, then run an existing
   admin user-management command to catch regressions.
