# Workflow re-audit — current working tree

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

