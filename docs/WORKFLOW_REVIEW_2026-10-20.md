# Payment, user-status, and usage workflow review

Reviewed: 2026-10-20, checkout based on `91e406e114b53f18fe1a28954cf55d07695cf1f9`.
**Follow-up:** all seven findings have now been addressed in this branch. See
[BILLING.md](BILLING.md) for the implemented policies, migration procedure, and
legacy-data limitations. The findings below preserve the original review.

Scope: `gamas_bot/db.py`, `gamas_bot/bot.py`, billing catalog, v3 schema, and relevant tests. This is a review; production code was not changed.

## Validation

Installed dependencies in an ignored local `.venv`, then ran:

```sh
.venv/bin/python -m pytest tests/test_db_billing.py tests/test_bot.py tests/test_jobs.py -q
```

Result: **89 passed**. Additional isolated real-SQLite probes reproduced findings 1–4 below. These probes used temporary databases, not production data. No live Telegram, bank verification, or provider calls were made. Findings 5–7 are from control-flow inspection.

## Workflow map

- **Payments:** select paid plan → create/reuse pending request with frozen terms → arm receipt upload → store validated image → notify admins → admin approves or rejects with a reason. Successful approval writes credit, status, and audit entry; expiry starts at approval. Upload alone grants no credit.
- **User status:** banned users are blocked at message and callback entry. Unlimited users bypass reservations; settlement checks whether an existing reservation should instead be released. Admin command handlers protect admin accounts from bans and unlimited status. Paid access is a derived credit balance, not a separate subscription-status flag.
- **Usage:** accept/queue → prepare media → measure duration → reserve whole seconds → transcribe → persist/deliver raw transcript → settle → generate/deliver booklet. Failures before settlement release holds. Office jobs are free; presentation billing uses source narration rather than padding. Startup marks interrupted jobs failed and releases active holds.

## Findings, ordered by priority

### 1. HIGH — Expired purchases leave their historical debits against new purchases

**Location:** `gamas_bot/db.py:444–459, 525–540` (`available_seconds`, `_available_seconds_locked`).

Balance excludes expired credits but subtracts *all lifetime debits*. There is no allocation linking consumption to a particular credit grant. Thus usage already paid for by an expired package is deducted again from future packages (and can also consume surviving lifetime credit).

**Reproduced:** grant 3,600 seconds valid for one day, use 1,800, advance two days, buy another 3,600 seconds. Available balance is **1,800**, not **3,600**.

**Recommendation:** allocate reservations and debits to credit lots, preferably earliest-expiring first. Expire only each lot's unused remainder. Define behavior for expiry during an active hold and migration of existing historical consumption. Do not simply filter debits by date: overlapping packages and permanent credit make that incorrect.

### 2. HIGH — Settlement is not concurrency-safe or exactly-once

**Location:** `gamas_bot/db.py:543–588` (`settle_hold`).

The active-hold lookup and unlimited-status check occur outside the write lock. Two calls can both observe an active hold and each insert a debit. A concurrent release or unlimited-status change can also occur between the checks and the debit. The later hold update is unconditional.

**Reproduced:** start with 1,000 seconds, reserve 100, run two `settle_hold(submission, 100)` calls with `asyncio.gather`. Balance becomes **800**, not **900**.

**Recommendation:** perform hold/status lookup, conditional transition, debit, and commit under one transactional lock; add a unique debit-to-submission constraint. Test simultaneous settle/settle, settle/release, and settle/unlimited transitions. The current normal worker calls settlement once, but the database invariant itself is unsafe.

### 3. HIGH — Failed approval can later commit as approved without entitlement

**Location:** `gamas_bot/db.py:702–768` (`review_payment`); related helpers `_write_many` and user-status audit writes.

Approval updates payment status before inserting the entitlement and audit. There is no rollback on exception or cancellation. Because the connection is shared, a later unrelated write can commit the partially completed transaction.

**Reproduced with fault injection:** a temporary SQLite trigger aborts the ledger insert during approval. `review_payment` raises `IntegrityError`; a subsequent `ensure_user` commits the pending status update. The payment is now **approved with zero credit**. Retrying approval returns false because it is no longer pending.

**Recommendation:** use explicit transaction handling with rollback on exception/cancellation and shared write-lock discipline. Put status changes and their audit records in the same transaction too (`set_banned` and `set_unlimited` currently commit the flag before auditing). Add failure-injection tests after every approval write.

### 4. HIGH — Reopened purchase screen uses live terms, not frozen terms

**Location:** `gamas_bot/bot.py:1874–1914` (`_start_payment`), `gamas_bot/db.py:638–674` (`create_payment`).

The screen is constructed from the current plan before `create_payment` returns an existing pending request. Approval correctly uses the old snapshot, but the user can be instructed to pay the new price for a different amount of credit.

**Reproduced:** create a pending 5-hour/50,000-Toman request, change the catalog to 10 hours/75,000 Toman, select it again. The same payment ID is returned; the screen's plan data is now 10 hours/75,000, while its approval snapshot remains 5 hours/50,000.

**Recommendation:** create/retrieve the request first, then render exclusively from its persisted purchase snapshot. Allow pending requests to be resumed independently of whether their catalog plan remains active.

### 5. MEDIUM — Cancel does not cancel a pending payment

**Location:** `gamas_bot/bot.py:1009–1013, 1902–1907, 1916–1930`; `gamas_bot/db.py:657–666`.

`/cancel` and navigation clear only the in-memory upload prompt. The payment stays pending indefinitely and prevents a different plan purchase. The error message tells users to complete or cancel the request, but there is no persisted payment cancellation operation. Admin rejection is the available escape route.

**Recommendation:** add an ownership-checked cancellation transition for requests not yet submitted for review, with explicit handling for already-uploaded receipts. Separate “cancel upload prompt” from “cancel purchase” in the UI; define expiry/resumption behavior for abandoned requests.

### 6. MEDIUM — Ban enforcement does not extend to already queued jobs

**Location:** `gamas_bot/bot.py:820–824, 960–965, 1161–1177, 2296–2322`; `gamas_bot/db.py:499–523`.

Message/callback entry gates work, but neither worker startup nor reservation checks the ban flag, and `/ban` does not remove queued work. A user banned after acceptance can still have queued work processed and charged/delivered.

**Recommendation:** decide whether bans only prohibit new requests or also cancel queued/running work. If the latter, recheck before starting expensive work, cancel queued jobs, and release their holds. Current behavior should otherwise be explicitly documented.

### 7. MEDIUM — Failure status and billing policy diverge after raw delivery

**Location:** `gamas_bot/bot.py:1307–1318, 1207–1217`; `gamas_bot/db.py:414–440`; `docs/BILLING.md`.

Settlement happens after raw transcript delivery, before booklet completion and before terminal `done` status. A cancellation, unexpected later failure, or restart in this interval leaves the debit intact while the submission becomes `failed`. `release_hold` cannot undo an already-settled debit. This conflicts with the documentation's promise that any failed/cancelled job returns all credit. Admin usage statistics count only `done` submissions, so billed transcription can also be absent from the reported usage total.

**Recommendation:** choose an explicit policy. If raw delivery is the billable milestone, persist a distinct delivered/billed state and report transcription usage independently of final booklet status; update the failure/refund documentation. If only full completion is billable, move settlement to the completion transaction or add compensating credits. Test restart/cancellation immediately after settlement.

## Existing safeguards worth retaining

- Once-only free grant under the write lock.
- Serialized reservations and insufficient-balance denial within one Database instance.
- Payment snapshots, one-open-request constraint, and repeat approval returning no change on the normal success path.
- Reason-required rejection and positive entitlement snapshot validation.
- Receipt ownership checks, signature/size validation, restricted files, and admin callback gating.
- Measured-duration billing, charge capped at reservation, source-only presentation billing, and no hold for unlimited users.
- Idempotent release and startup recovery of active holds.

## Suggested order

1. Repair credit-lot/expiry accounting and make financial transactions rollback-safe.
2. Make settlement exactly-once and serialize status transitions.
3. Render purchases from snapshots and provide a real cancel/resume flow.
4. Clarify ban and partial-success billing policy; add transition/fault-injection tests.
