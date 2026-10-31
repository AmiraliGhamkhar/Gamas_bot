# Reliability, security and production-readiness audit — current working tree

Date: 2026-10-31. Base revision reviewed: `b526c4e5d0e05b85091de12b20704472afbd31f7`
(`main`, merge of PR #98), branch `arena/2703e486-gamas-bot`, working tree clean at start.
Everything below refers to this checkout, its offline test suite and controlled local
reproductions — **not** to a deployed instance. Prior reports follow this one as history;
their revisions and line references do not describe this tree.

## 1. Executive summary

The repository is in materially better shape than its size suggests. Intake, queueing, media
processing, provider failover, billing, learning, documents, Free Office and the admin surfaces
are all covered by a large offline suite (**1,238 passing tests before any change**) and by
pre-flight gates (`ruff`, `mypy`, `compileall`, requirements synchronisation, `pip check`, the
media-worker check) that were already green on arrival. Legacy audit findings
(`docs/PRODUCTION_AUDIT.md`, previous `AUDIT_REPORT.md` sections) are resolved in this tree; no
P0 defect was found and no user data, price, plan, callback identifier or environment default
was changed.

Three genuine defects were confirmed, reproduced and fixed, each with a regression test that
fails against the previous code:

| ID | Severity | One-line description |
| --- | --- | --- |
| F-1 | P1 | The shared membership check is not cancellation-safe: a cancelled owner strands concurrent waiters forever, and a cancelled waiter cancels the check for everyone. |
| F-2 | P3 | `0.0` used as a "never" sentinel against `time.monotonic()` suppressed the first (and most important) subscription admin alert on a freshly booted host. |
| F-3 | P3 | The same sentinel bug refused a brand-new user's first Free Office export after boot, and `OfficeSession.updated_at` defaulted to "expired". |

The audit also verified a set of high-risk invariants that turned out to be **correct**, and
those negative results are reported in §6 so they are not re-investigated blindly. No
performance regression was introduced, and no optimisation was claimed: this round changed no
hot path.

## 2. Repository architecture (as verified)

One process: a Telethon client, a bounded round-robin job queue, provider pools and SQLite
(WAL, serialized `BEGIN IMMEDIATE` writes on `self.db`, `query_only` reads on `self.read_db`).

```text
Telegram update (message | callback)
  → private-chat + ban checks
  → subscription gate (fresh getChatMember, admins exempt, fail open on infrastructure errors)
  → per-kind router: media intake | document intake | Free Office draft | learning | wallet/payments
      media:      bounded queue → download (bounded, stalled-transfer aware) → PyAV worker
                  → STT failover chain (Speechmatics → Deepgram → OpenAI-compatible)
                  → raw transcript delivered → note pipeline (chunk → map → evidence → booklet)
                  → Telegram delivery → library reference row (no local artifact copy)
      documents:  SHA-256 → owner-scoped extraction cache → locator-tagged chunks
                  → full booklet / compact notes / text exports / original download
      office:     in-memory, user-scoped, TTL-bounded draft → bounded semaphore
                  → DOCX/XLSX/PPTX/selectable PDF → Telegram + library reference
      billing:    reserve (hold) → settle on raw delivery | release on failure;
                  manual card-to-card payments → admin review → single-transaction grant
```

`bot.py` is ~9.2k lines and holds the callback/message routing, screens and orchestration; the
domain work lives in focused modules (`notes.py`, `docx_export.py`, `llm.py`, `stt.py`,
`doc_extract.py`, `office*.py`, `presentations.py`, `media.py`, `db.py`). The coupling is real
but the seams are already drawn along the boundaries the prompt proposes; no extraction was
performed because no verified defect required it (see §10).

## 3. Existing functionality verified

Read and/or exercised in this tree: audio/video/voice/video-note intake and its stage gates;
STT provider order, saturation overflow, cooldowns, upload-size eligibility and chain
deadline; raw-transcript-first delivery with booklet generation afterwards; note routing,
structured-output validation, evidence/gate pipeline and deterministic fallbacks; PDF/DOCX/PPTX
extraction with page/section/slide locators, SHA-256 dedupe, concurrent-duplicate sharing and
the 5,000,000-character cap; scanned-PDF refusal (no OCR claim anywhere); Free Office drafts,
`.txt/.md/.markdown/.csv/.tsv` intake, RTL DOCX/XLSX/PPTX/selectable-PDF output, literal Excel
cells (formula-looking text stored as `s`, verified by test), library indexing and temp
cleanup; full booklets vs compact revision notes as genuinely distinct prompt/schema/validator/
renderer pairs; AI and manual flashcards, quizzes, presets, previews, free first use and paid
recovery; SQLite ledger, holds, allocations, refunds, plan catalogue, receipt validation,
admin review and audit rows; channel subscription enforcement on both message and callback
routes; admin hubs, broadcasts, reports, provider metrics, support tickets and ownership.

## 4. Findings

Severity: **P0** security/data-loss/billing-integrity, **P1** serious functional or
reliability defect, **P2** performance/maintainability/operational, **P3** minor.

### F-1 — Shared membership check is not cancellation-safe (P1)

* **File / function:** `gamas_bot/subscription.py` → `SubscriptionService.check_membership`
  (owner path) and its waiter path (`self._inflight`).
* **Mechanism:** concurrent checks for one user shared a bare `asyncio.Future`.
  (a) The owner resolved the future only on the success path; if the owning task was
  cancelled, the `finally` block removed the map entry without ever resolving the future, so
  every waiter continued to `await` a future nothing could complete — forever, because the
  gate has no timeout around that await.
  (b) Awaiting a future makes it part of the waiting task's cancellation state: cancelling a
  waiter cancels the shared future, so the owner's `set_result` hit `InvalidStateError`
  (swallowed) and *other* waiters' awaits raised `CancelledError` — an unrelated interaction
  died because a sibling was cancelled.
* **Reproduction:** `check_membership` twice for the same user with a resolver that blocks on
  an `asyncio.Event`; cancel the owner → the waiter never completes (`asyncio.wait_for(…, 1.0)`
  times out). Three concurrent checks, cancel the middle one → the shared future reports
  `cancelled()` and the third task raises `CancelledError` even though it was never cancelled.
  Both were observed directly in this checkout.
* **Impact:** a user with two in-flight interactions (e.g. a button tap plus an upload, or two
  quick taps — Telethon dispatches each update in its own task) can end up with a handler that
  never returns, never replies and leaks until restart; in the mirrored case a healthy
  interaction is silently cancelled. Requires concurrency *and* a cancellation (shutdown,
  connection reset), so it is not reachable in a single-interaction flow. No data loss and no
  billing effect: the gate fails open, never to a denial.
* **Correction:** waiters now await the shared request under `asyncio.shield`; the owner
  resolves the future with an inconclusive `MembershipResult(STATE_UNKNOWN, None, "cancelled")`
  before re-raising `CancelledError`, so waiters get a determinate fail-open answer instead of
  hanging.
* **Regression tests:** `tests/test_subscription.py::InflightSharingTests` (three tests:
  cancelled owner releases waiters, cancelled waiter does not cancel the shared check, waiters
  share exactly one resolver call). All three fail against the previous code.

### F-2 — First subscription admin alert swallowed on a freshly booted host (P3)

* **File / function:** `gamas_bot/subscription.py` → `_alert_admins`, state in
  `SubscriptionService.__init__`.
* **Mechanism:** `self._last_admin_alert = 0.0` meant "never alerted" and was compared with
  `time.monotonic()`, whose epoch is the host uptime (not wall-clock). While uptime is below
  `ADMIN_ALERT_INTERVAL_SECONDS` (300 s), `now - 0.0 < 300` is true, so the first alert is
  dropped. That first alert is the one `startup_self_check` raises when the bot is **not an
  administrator of the channel** — the condition that silently disables the mandatory gate
  (it fails open by policy).
* **Reproduction:** deterministic with a monotonic clock at 140 s (observed in this sandbox) or
  with `time.monotonic()` patched to `5.0`: two `require_access` calls with a resolver raising
  `SubscriptionApiError(403, …)` produced **0** admin notifications. This was a *pre-existing
  failing test* in the repository baseline
  (`tests/test_subscription.py::GateTests::test_unknown_alerts_admins_throttled`), i.e. the
  defect was already encoded as an expectation and never verified on a short-uptime host.
* **Impact:** operator visibility only — up to five minutes without an alert after boot;
  access decisions and user experience are unaffected. On long-uptime shared hosting the bug
  is masked, which is why it survived review.
* **Correction:** `_last_admin_alert: float | None = None`, compared only when set (the same
  idiom `_check_admin_cooldown` already uses). Throttling after the first alert is unchanged.
* **Regression tests:** `test_first_admin_alert_is_delivered_on_a_freshly_booted_host` and
  `test_admin_alerts_stay_throttled_within_the_interval` (clock injected as a mock module in
  `gamas_bot.subscription` only, so the event loop keeps its real clock). Plus the pre-existing
  `test_unknown_alerts_admins_throttled`, which now passes.

### F-3 — `0.0` sentinels in the Free Office thresholds (P3)

* **Files / functions:** `gamas_bot/bot.py` → `_office_run_operation` (per-user throttle);
  `gamas_bot/office.py` → `OfficeSession.updated_at`.
* **Mechanism:** (a) `self._office_last_use.get(user_id, 0.0)` treated "no previous export" as
  monotonic `0.0`; with `OFFICE_MIN_INTERVAL_SECONDS > 0` (default 10) and uptime below that
  interval, a user's **first** export was refused with "wait N seconds". (b)
  `OfficeSession.updated_at` defaulted to `0.0` while `_office_sweep_sessions` compares it with
  the same monotonic clock, so any draft constructed without an explicit stamp would look
  expired on the next sweep once uptime exceeded the TTL — a latent trap rather than a live
  bug, because the single construction site always sets the field.
* **Reproduction:** harness in `tests/test_office_flow.py` with `office_min_interval_seconds=10`
  and a bot-local clock at `5.0`: the first `office:fmt:word` tap answered with the throttle
  alert and produced no file. Against the fix the first export builds, and an immediate second
  export is still throttled.
* **Impact:** a ≤10 second window after boot where new users are told to wait for a resource
  they never used; user-visible but harmless (no charge, no data). The expiry default has no
  live impact today.
* **Correction:** `last = self._office_last_use.get(user_id)` + `last is not None` guard;
  `OfficeSession.updated_at: float = field(default_factory=time.monotonic)`.
* **Regression tests:** `tests/test_office_flow.py::test_first_export_is_not_throttled_when_the_host_just_booted`,
  `tests/test_office.py::OfficeSessionTests` (2 tests).

### 4.1 Confirmed-clean areas (checked, no defect found)

Reported so they are not re-litigated without new evidence:

* **Owner scoping.** `document_get`, `document_by_hash`, `library_item`, `library_item_detail`,
  `library_by_*`, support tickets and payments all bind `user_id` in SQL; `doc:*`, `lib:*`,
  `office:*` routes resolve through those helpers, so a forged callback id for a foreign row
  returns "not found".
* **Payments.** `review_payment` re-reads status and receipt inside one transaction, requires
  `pending_review`/`pending` + a receipt, refuses to approve a missing snapshot
  (`PaymentSnapshotError`), and the unique `ledger.payment_id` index backs it up; `payv:` is
  admin-gated before the gate and the approval requires a second explicit `:a:yes` tap.
  Receipts are re-encoded, `0600`, path-contained (symlink/hard-link rejecting) and rejected
  if the order belongs to another user.
* **Learning.** Free-use claims are conditional single-row updates with a claim token; a reset
  cannot be consumed by an older job; `learning_charges` finalisation shares the artifact's
  write transaction and startup recovery refunds once.
* **Excel/CSV.** Quoted CSV fields are parsed with `csv.reader(strict=True)`; formula-looking
  cells are stored with `data_type="s"`; zero-padded codes stay strings.
* **Telegram HTML.** A 20,000-case fuzz of `markdown_to_telegram_html`/`render_pages` over the
  adversarial grammar (crossed emphasis, fences, tables, NUL, astral emoji) produced no
  unbalanced tag, no page over 4096 UTF-16 units and no page whose markup Telegram would
  reject.
* **Secret handling.** `getChatMember` carries the token in the URL path; connection failures
  were probed and aiohttp's error strings contain host:port only, and `_check_uncached` logs
  provider detail (`api-…`, `bot-not-admin`) rather than raw exception text on the normal path.
  No token, receipt, DB path or transcript content is written to logs by the changed code.
* **Migrations.** The suite's migration gate (fresh DB, exact table set, upgrade with populated
  data, interrupted-replay of the `library_items` rebuild) passes untouched; **no migration was
  added or modified** in this round, so `016_*` ordering and the next free sequence (`017_…`)
  remain as documented.
* **Queue/shutdown.** `JobQueue` releases per-user rotation, in-flight map, log context and stage
  metrics in `finally`; admission/receipt/document intake is joined before SQLite closes and
  cancellation paths shield-and-join their transaction (the idiom the subscription code was
  missing).

## 5. Changes implemented

| File | Why it changed |
| --- | --- |
| `gamas_bot/subscription.py` | F-1 (shield + release waiters on cancellation), F-2 (`None` sentinel), docstrings for both guarantees. |
| `gamas_bot/bot.py` | F-3a (per-user Office throttle sentinel + comment). |
| `gamas_bot/office.py` | F-3b (`OfficeSession.updated_at` defaults to the current monotonic time). |
| `tests/test_subscription.py` | 5 regression tests (in-flight cancellation safety ×3, first alert, throttled interval). |
| `tests/test_office_flow.py` | 1 end-to-end regression test for the first-export throttle. |
| `tests/test_office.py` | 1 unit test for the draft timestamp default. |
| `docs/TELEGRAM_SUBSCRIPTION.md`, `docs/CHANGELOG.md` | Document the cancellation semantics and record the fixes. |

No behaviour change to prices, plans, card details, callback identifiers, environment
defaults, database schema, migrations, provider routing or user-facing copy beyond the fixes
above.

## 6. Database and migration changes

**None.** No table, column, index or migration file was added, reordered or edited. The
in-flight map and both throttles are in-process state; `OfficeSession` is an in-memory draft.
Data-preservation guarantees are therefore unchanged, and this upgrade is schema-compatible
with the previous revision in both directions for the changed code (the bot may still not be
rolled back past an *earlier* incompatible migration — unchanged rule).

## 7. Security review

* No P0/P1 authorization or privacy defect found. Callback authorization was re-verified at the
  server-side boundary (private-chat check → ban check → admin prefix check → subscription gate
  → owner-scoped query), not from callback data alone.
* The subscription gate keeps its documented policy: administrators exempt, confirmed
  `left`/`kicked` denied on both routes, infrastructure/permission failures fail **open** with
  an operator alert, unknown ≠ non-member, database subscription fields observational only. The
  change makes fail-open *more* complete (a cancelled verification now yields "unknown" instead
  of a hang or an accidental cancellation). **Switching fail-open to fail-closed remains a
  product decision that has not been made here** (§12).
* File-safety controls re-checked: 50 MB upload cap, OOXML ZIP expansion cap (200 MB) and
  encrypted-entry rejection before python-docx/pptx open a package, encrypted-PDF detection,
  owner-scoped temp directories under `TEMP_DIR`, `finally` cleanup, receipt path containment.
* Remaining accepted risk (unchanged): parsing runs in a worker *thread*, not an OS-isolated
  process, so a pathological single file can consume CPU/memory before the character cap is
  reached.

## 8. Performance review

No performance change is claimed. The three fixes touch a per-request boolean/RPC-sharing path
and two throttles; `asyncio.shield` adds one future wrapper around an already network-bound
call (immeasurable against a `getChatMember` round-trip). No concurrency variable, timeout,
pool size, buffer or chunk size was altered.

Measured, for the record: full `pytest` run 71.4 s before (1,238 passed + 1 pre-existing
failure) and 70.3 s after (1,246 passed) on a 2-core sandbox, Python 3.11.2 — the difference is
the seven added tests and run-to-run noise, not a regression. Baseline coverage snapshot (for
future work, not changed here): total 83%; weak spots `media_worker.py` 16% (exercised via the
subprocess boundary), `office_notes.py` 53%, `telegram_text.py` 66%, `subscription.py` 73%
(the HTTP transport and `startup_self_check` have no offline coverage), `__main__.py` 0%.

## 9. Testing results

All commands were executed in this checkout (Python 3.11.2, venv with `requirements.txt` plus
`pytest`, `ruff`, `mypy`, `coverage`). Nothing below is a live-environment claim.

| Command | Executed | Result |
| --- | --- | --- |
| `python -m pytest -q` (baseline) | yes | **1 failed, 1,238 passed, 62 subtests** in 71.4 s — `test_unknown_alerts_admins_throttled` (→ F-2) |
| `python -m pytest -q` (final) | yes | **1,246 passed, 62 subtests**, 0 failed |
| `python -m unittest discover -s tests -q` (final) | yes | **Ran 1,246 tests — OK** |
| `ruff check .` | yes | All checks passed (baseline and final) |
| `python -m mypy` | yes | Success, no issues in 32 source files (baseline and final) |
| `python -m compileall -q gamas_bot scripts tests passenger_wsgi.py` | yes | OK |
| `python -m scripts.sync_requirements --check` | yes | `requirements.txt is up to date` |
| `python -m pip check` | yes | No broken requirements |
| `python -m gamas_bot.media_worker check` | yes | `{"av": "18.1.0", "ppt2pptx": "0.4.2"}` |
| `coverage run -m pytest` | yes | TOTAL 83% (snapshot above) |
| New tests vs old code | yes | 5 subscription + 1 office-flow + 1 office-unit tests fail on the pre-fix code, pass after |

Not executed (no environment): live Telegram Bot API/MTProto, real STT/LLM providers, real
receipts, cPanel/Passenger, cron watchdog, production-sized databases. The fuzz, token-leak and
cancellation probes were local and synthetic.

## 10. Remaining risks and decisions requiring authorization

1. **Document text retention (product decision, unchanged).** Extracted PDF/DOCX/PPTX text is
   stored in SQLite with no per-user count, expiry or self-service deletion; library pruning and
   transcript retention do not remove it. Needs an approved lifecycle policy (source file,
   extracted text, chunks, generated outputs, learning references, user deletion) before any
   destructive migration. No documents were deleted in this audit.
2. **Parser resource isolation (infrastructure decision).** A separate, CPU/memory/time-limited
   worker process for document parsing is feasible only if the cPanel host permits it; not
   introduced, because it could make the supported deployment impossible.
3. **Subscription fail-open policy.** Unchanged and documented; an explicit product decision is
   required to make confirmed-infrastructure-failure deny access.
4. **Single-process assumptions.** Session lock, stage semaphores, provider pools/cooldowns,
   Office drafts, curation previews, dedupe and admission fingerprints are process-local. Two
   instances are not supported (the instance lock prevents it); no horizontal scaling is
   claimed.
5. **Exactly-once across Telegram + SQLite** is still not achievable; the code intentionally
   favours the user on a crash between remote delivery and local settlement.
6. **Historical paid learning debits** written before migration 015 cannot be classified as
   interrupted vs completed; operators must reconcile manually.
7. **`bot.py` size.** ~9.2k lines with routing, screens and orchestration interleaved. No
   extraction was performed because no defect required it and the prompt forbids wholesale
   rewrites; the seams (callback routing, document orchestration, Office orchestration,
   payments, support, reporting) are viable future work.

## 11. Deployment instructions (unchanged model)

This round adds **no migration**, so the procedure is the standard one in
`docs/DEPLOY_CPANEL.md`:

1. `cp .env .env.bak` (keep it `0600`), outside `public_html`.
2. Stop the bot (kill the worker; the launcher's instance lock prevents a second copy).
3. Back up the SQLite database **while stopped**, including `-wal` and `-shm` sidecars if
   present (`sqlite3 data/gamas_v3.db ".backup ..."` is only valid while running).
4. `git fetch && git checkout <this revision>`; `. .venv/bin/activate`;
   `python -m pip install -r requirements.txt` (unchanged, so usually a no-op).
5. `python -m scripts.cpanel_preflight` then `python -m gamas_bot --check` — both are offline
   and side-effect free, and safe while an instance is running.
6. Start the bot; the cron watchdog entry stays as-is. Startup re-applies no migration and is
   idempotent.
7. Confirm in the log: `subscription self-check` (if enabled), queue workers started, no
   `SQLite reference integrity` warning.
8. **Rollback:** the changed code is compatible with the existing schema in both directions, so
   reverting the checkout and restarting is sufficient; no database restore is needed *for this
   change*.

## 12. Final engineering assessment

**Ready with documented operational limitations.**

Evidence: the baseline suite was green except for one genuine, now-fixed defect; the three
defects found are fixed with tests that provably fail before and pass after; static gates
(`ruff`, `mypy`, `compileall`, dependency sync, `pip check`, media worker) are clean; no schema,
billing, price, callback or configuration contract was touched; and the highest-risk invariants
(owner scoping, payment approval, free-use claims, queue shutdown, HTML rendering limits,
migration replay) were re-verified rather than assumed.

The qualifiers are real: verification here is **offline only** (no live Telegram, providers,
receipts or cPanel), the per-user document-retention policy is still open and needs a product
decision before implementation, document parsing is thread-isolated rather than
process-isolated, and the deployment remains single-instance by design.

---

# Historical reports

The sections below are retained as history. Their revisions, line references and conclusions
describe earlier working trees, not this checkout.

## 2026-10-27 — workflow re-audit (base `cb511f0`)

Date: 2026-10-27. Base revision: `cb511f01ac5145bbd89b7ab01c129b682af67c17`;
branch: `arena/62447c17-gamas-bot`. The findings and verification below refer to the
working-tree changes in this session, not a deployed instance. The previous report is retained
below as historical context; its revision, line references and conclusions do not describe this
working tree.

## Corrected findings

- **Paid learning recovery:** unsaved paid generations could remain charged after a crash, and
  cancellation around artifact commit could race compensation. Migration 015 persists one pending
  operation with its debit and finalizes it with the saved quiz/deck; cancellation, expiry and
  startup refund an unfinished charge exactly once. Failures to refund are shown honestly.
- **Telegram callbacks:** a blank answer could consume Telethon's one callback acknowledgment before
  a later alert. Answers now wait for a specific response where appropriate, with an early blank
  acknowledgment only for long-running generation.
- **Presentation and transfers:** a valid textless narrated presentation now offers explicit audio
  processing; ranged downloads verify each range and handle short positional writes, falling back to
  the single-stream path on failure.
- **Queue, shutdown and receipts:** queued duplicate uploads now retain their fingerprint claim;
  cancellation during admission closes the pending row, and shutdown joins admission/intake and
  terminates queued progress. A cancelled receipt upload is removed unless the database committed
  its reference; a failed user confirmation does not suppress the admin review notification.
- **Library and quizzes:** active quiz sources are protected from pruning, which otherwise cascaded
  an unfinished session; temporary forwarding errors no longer permanently mark files unavailable.
  Existing stale unavailable flags are cleared after a successful retry.
- **Performance and resource usage:** chunk splitting avoids repeated suffix copies. Simultaneous
  identical document uploads share extraction, separate document parsing concurrency is bounded,
  output is capped at 5,000,000 extracted characters, and stalled progress edits no longer hold model
  slots. The full configured learning concurrency range is now honored. Synthetic timings and
  their limitations are in `docs/PERFORMANCE.md`.

## Remaining operational limitations

- Uploaded-document text is stored in SQLite without an automatic per-user count, expiry, or
  self-service deletion policy. Library reference pruning and transcript retention do not remove
  that cache. A retention/deletion policy needs a product decision; no existing documents were
  deleted during this audit.
- Paid learning debits written **before** migration 015 cannot be identified reliably as interrupted
  versus completed; operators must reconcile any historical disputes using available records.
- PDF/Office parsing still runs in a thread, not an independently time- or memory-limited process;
  a pathological individual file can consume significant resources before the text cap is enforced.
- Exactly-once atomicity across Telegram delivery and SQLite cannot be guaranteed. A crash after
  remote delivery but before the local settlement commits intentionally favors the user.
- The automated tests, synthetic benchmarks and media-worker check are offline. They do not verify
  live Telegram, STT or LLM providers, real receipts, cPanel limits, or production traffic.

## Verification

- `python -m pytest -q`: **1,184 passed, 62 subtests passed**.
- `python -m unittest discover -s tests -q -b`: **1,184 passed**.
- `ruff check .`, `python -m mypy`, `pip check`, requirements synchronization, and the local media
  worker check passed on Python 3.11. The cPanel-profile benchmark used simulated providers.

See `docs/TESTING.md` and `docs/PERFORMANCE.md` for commands and reproducible local measurements.
Back up the database before applying migrations; do not run an older bot against a migrated database.

---

## Historical report (previous revision)

# Gamas Bot — Workflow & Logic Re-audit

Date: 2026-10-27
Audited revision: `b6a0f63d744d267861e6cb0094716c977f8e47c3` (branch `arena/4cd656ed-gamas-bot`, worktree clean)
Audited tree hash: `7de3f02333ee2ddee5fa0334e2f59febd055fa6c`

## 1. Executive summary

I re-traced every user-facing workflow end to end (media intake → queue → download → probe →
audio prep → reserve → transcribe → progressive raw delivery → settle → notes/booklet; the
presentation pipeline; the billing/payment/receipt/review path; the quiz/flashcard learning loop;
support tickets; admin panel; startup/self-check/launcher/instance-lock) and cross-checked the
code against its own docstrings, `.env.example`, `docs/*`, `CHANGELOG.md`, the migrations and the
test suite.

**No correctness-blocking defect was found.** The documented workflows are internally consistent,
and the previously reported issues are confirmed fixed in this revision. What remains are a small
number of low-severity observations (§3) and two maintenance notes (§4).

## 2. Verification actually executed

| Check | Command / source | Result |
| --- | --- | --- |
| Full test suite | `python -m pytest -q` | **991 passed, 38 subtests, 0 failed, 0 skipped** |
| Static analysis | `ruff check .` | All checks passed |
| Type analysis | `mypy` | Success, no issues in 32 source files |
| Integrity vs upstream | `gh api repos/AmiraliGhamkhar/Gamas_bot/commits/main` → `b6a0f63…`; `gh pr view 91` → mergeCommit `b6a0f63…` | Worktree == genuine upstream tip |
| Dependency consistency | `python -m scripts.sync_requirements --check` | `requirements.txt is up to date` |
| Coverage | `pytest --cov=gamas_bot --cov-report=term-missing` | TOTAL 83%; `bot.py` 78%, `notes.py` 87%, `docx_export.py` 87%, `__main__.py` 0%, `media_worker.py` 16% |

The audited revision is byte-identical to the public upstream merge commit for PR #91, so no
change was injected for this exercise; findings below are genuine properties of the shipped code.

### Prior findings — confirmed resolved

All five items in `docs/PRODUCTION_AUDIT.md` (starting revision `9f4cc72`) are present in this tree:

1. `_show_admin_payments` pagination + `adm:pay:<page>` navigation — present (`bot.py:2605`, `bot.py:5135`).
2. Migration `008_payment_transition_integrity.sql` — present (`migrations/v3/`).
3. Submitted-order visibility no longer gated on `PAYMENT_CARD_NUMBER` — confirmed.
4. `SUPPORT_FILTERS["open"] = "status IN ('open','awaiting_admin')"` matches the «📂 جدید» label and
   the "جدید" counter — confirmed (`db.py:2331`, `bot.py:5911`).
5. Shortened copy — confirmed.

## 3. Findings (low severity)

### F-1 — `_run_job` banned-user branch: stale progress message and malformed call (bot.py:3057–3064)

```python
if await self.db.is_banned(job.user_id):
    job.transition_to("cancelled")
    await self._release(job, "user banned before processing")
    await self.db.set_submission_status(
    job.submission_id, "rejected", error="user banned", error_kind="banned"
)
    await self._reply(job, "⛔ درخواست صف‌شده به دلیل مسدودی حساب لغو شد.")
    return
```

* The continuation lines of `set_submission_status(...)` are indented one level too shallow.
  Python parses it (the call is parenthesised), so it is **not** a crash, but it reads as if the
  status update were nested inside the `if`.
* Every other failure branch calls `progress.fail(...)`; this one does not, so the user's progress
  message is left at the last "in queue" frame while a reply is sent underneath. Suggested: indent
  the call and add `await progress.fail(str(job.submission_id))`.

### F-2 — Presentation admission floor is charged only for decks with audio (bot.py:2958–2968, 3555)

`_accept_presentation` requires `available >= _reserve_estimate(None) == 600 s` for non-unlimited
users, but `_process_presentation` only reserves when `prepared is not None` and settles
`settled_duration = prepared.source_duration if prepared is not None else 0`. A **slide-only** deck
(no audio) therefore passes a 600-second admission gate and is then settled at **0** seconds.

* Not a billing loss (nothing is charged), but the admission check promises a minimum cost that is
  never taken, and a user can queue a free deck while holding only the floor credit. Worth an
  explicit comment or a decision to settle slide-only decks at a token amount.
* The converse is intended and safe: a deck *with* audio re-reserves the measured duration and fails
  the job (`InsufficientBalanceError`) rather than over-charging.

### F-3 — `_show_admin_payment_detail` lumps "receipt removed" and "never submitted" together (bot.py:5224–5231)

```python
if payment["receipt_path"]:
    receipt_state = "ثبت شده"
elif payment["status"] in {"awaiting_payment", "pending", "cancelled"}:
    receipt_state = "ثبت نشده"
else:
    receipt_state = "در دسترس نیست"
```

For an `approved`/`rejected` order whose receipt file was deleted by retention
(`forget_receipts` nulls `receipt_path`), the admin sees «در دسترس نیست» — identical to a never
submitted receipt. The prior audit deliberately distinguished these for the *user* copy; the admin
detail screen still conflates them. Low impact (administrative only), but it makes a retention
deletion look like a missing receipt.

### F-4 — Cosmetic rendering

* `GamasBot._bytes_text(0)` returns `"0.0 KB"` (`bot.py:4640`) — a zero byte count renders as a
  fractional kilobyte. Suggest `if size == 0: return "۰ بایت"` or similar.
* `progress_bar(100, head=DEFAULT_HEAD)` returns `"█"*width` (`progress.py:71`), dropping the 🚀 head
  that every sub-100 frame carries. Intentional for the "complete" bar, but it means the final frame
  is the only one without the emoji.

## 4. Maintenance notes (not defects)

### M-1 — The Free Office pipeline is unreachable dead code

`build_office_pdf`, `build_office_pptx`, `build_office_xlsx`, `plan_deck` and `infer_table` have
**no callers** in `gamas_bot/` (only a reference in a `config.py` comment). The v4 changelog retires
the Free Office UI and queue ("its pure export modules remain covered by offline tests, and
`OFFICE_ENABLED` now defaults to `false`"). So `office.py`, `office_ai.py`, `pdf_export.py` and the
office branches of `docx_export.py` (~1.8k statements, `pdf_export.py` 91% "covered") exercise
file-writing and `asyncio.to_thread` paths that production can never reach. Not a bug, but it is a
large, write-capable surface with zero live callers; a future cleanup should either delete it or add
an explicit runtime switch test.

### M-2 — `media_worker.py` is 16% covered

The worker that performs probing, conversion and the sample-accurate merge is exercised almost
entirely through the `media.py` subprocess boundary, not directly. Its pure helpers (parse/accounting
logic) are the risky part; the current coverage means a regression inside the worker would only
surface as an integration failure. Recommend adding direct unit tests for the worker's parsers.

## 5. Workflows verified consistent (no mismatch found)

* **Reserve/settle discipline** — `_reserve_estimate` (`bot.py:3043`) returns `ceil(duration)` or a
  600 s floor; `_settle` (`bot.py:3380`) charges `ceil(duration)` or 0. Reserve happens before
  transcription and settle only after the raw transcript is delivered, so a failed delivery refunds.
* **Media pipeline** — intake pre-size/duration gates, post-download size check, disk headroom,
  download stall/timeout, `probe_media` duration check, audio size check, re-probe fallback, and the
  `_deliver_raw` file→paginated-text fallback with its exact `raw_parts` contract (verified
  programmatically: parts rejoin to the original text byte-for-byte).
* **Presentation pipeline** — legacy `.ppt` conversion, `MEDIA_ACTION_OMITTED` skip notice, slide-only
  deck rejection when there is neither slide text nor audio, progressive delivery before settle.
* **Billing** — snapshot freezing, exactly-once `debit_allocations`, lot-aware `spend_seconds`,
  non-expiring `refund_seconds`, receipt-path containment (`_is_safe_receipt_path` rejects symlinks and
  hard links), receipt re-encoding, admin-only `adm:`/`payv:` gating.
* **Learning loop** — `parse_quiz_payload` shuffle + `correct_index` remap, `MIN_VALID_QUESTIONS`
  fail-closed, full refund on every post-debit failure, `quiz_record_answer` exactly-once via the
  unique `quiz_answers(question_id)` index, per-user in-flight marker cleared in `finally`, gift
  cooldown awaited, leaderboard tie/rank math.
* **Routing** — every `Button.inline` payload produced in `bot.py` is handled in `_handle_callback`;
  no orphan button.
* **Plan catalogue** — `.env.example` variables and `config.PLAN_ENV_FIELDS` match exactly (16/16);
  plan codes (`free_1h`, `paid_5h_30d`, …) match the DB seed, the buy buttons and the tests.
* **TOC parity** — `pdf_export.build_office_pdf` and `docx_export` both gate on
  `headings >= 2 and toc_is_worth_it(body_chars, sections, design)`.
* **Cancel/shutdown** — `except asyncio.CancelledError` releases the hold, marks the submission
  `cancelled`, re-raises; `shutdown` cancels background sweeps before closing the DB and fails
  queued jobs with `error_kind="cancelled"`.

## 6. Recommendation

* Fix **F-1** (indentation + missing `progress.fail`) — trivial, user-visible.
* Decide **F-2** explicitly (charge a token amount for slide-only decks or document the free path).
* Fold **F-3** into the already-distinguished receipt wording.
* Treat **M-1/M-2** as backlog items.

No code was modified by the audit itself: the first request was a re-check, not a fix.

## 7. Fixes applied (2026-10-27)

Follow-up request: "Apply all changes and fix all the bugs." Applied to
`gamas_bot/bot.py`; verified with the full suite (991 → 994 tests), `ruff check .`
and `mypy gamas_bot` (all green).

| ID | Change | Why |
|----|--------|-----|
| **F-1** | `_run_job` banned branch: re-indented the `set_submission_status` continuation lines to match their call, and added `await progress.fail(str(job.submission_id))` | The continuation lines were valid Python but were not part of the call's argument list; the rejected job left its queue progress message (and the bar) editing next to the "cancelled" reply. Regression test: `tests/test_workflow_regressions.py::WorkflowBotTests::test_queued_banned_job_closes_its_progress_message` (fails without the fix, passes with it). |
| **F-2** | `_accept_presentation` intake wording: `لازم (حداقل برآورد)` → `حداقل اعتبار برای برقراری در صف` | The 600 s floor is a queue-admission requirement, not a guaranteed charge: a slide-only deck settles at 0. The old wording implied a minimum charge. The free-deck behaviour described in `docs/BILLING.md` is unchanged (and, checked again, a prepared deck always has `source_duration > 0`, so the hold and the settlement always agree). |
| **F-3** | `_show_admin_payment_detail` receipt state: a reviewed order (`approved`/`rejected`/`pending_review`) without a receipt file now shows `پاسخ‌شده؛ رسید موجود نیست` instead of the same "در دسترس نیست" as a never-submitted receipt | Retention pruning (`forget_receipts`) made a reviewed-and-approved order indistinguishable from one that never submitted a receipt. The user-facing copy already separated these; the admin detail now does too. |
| **F-4** | `_bytes_text(0)` (and any negative input, clamped) now returns `۰ بایت` instead of `0.0 KB` | A zero byte count is a byte count, not a fractional kilobyte. Unit test added: `tests/test_bot.py::ByteTextTests`. |
| **F-4b** | *not changed — intentional.* `progress_bar(100)` returns a solid 12-cell bar with no head | The 100 % bar is the completion state (`complete()` appends ` ۱۰۰٪  ✅`), so dropping the leading-edge emoji there is deliberate, not an off-by-one: the bar stays at its fixed width and signals completion. Rendering a rocket on a full bar would read as "still running". |

**Not fixed (backlog, as recorded):**

* **M-1** — the retired Office pipeline (`office.py`, `office_ai.py`, `pdf_export.py`,
  office branches of `docx_export.py`) still has no callers. Removing it is a
  deletion of live source, out of scope for this fix request; `OFFICE_ENABLED=false`
  means it is dead but harmless.
* **M-2** — `media_worker.py` (16 % coverage) is exercised by
  `tests/test_media.py`; adding real-PyAV integration tests is a separate effort.
