# Final report — production billing, payments, provider keys and RTL DOCX (§54)

Branch `arena/1f5ae9d9-gamas-bot`, based on `main` (`faeb8cd`, "Production
hardening: plans, receipts, provider health, TOC validation (#27)"). This report
covers the full-repository audit of that revision and the changes made on top
of it. Every claim below is either backed by a command run in this environment
or marked **not performed**. Payment verification is **manual admin approval by
design**; no automatic gateway exists and none is claimed.

Verification commands (this session, this sandbox):

```
$ .venv/bin/python -m pytest tests/ -q
678 passed, 1 skipped, 1622 subtests passed
$ .venv/bin/python -m unittest discover -s tests
OK (skipped=1)
$ .venv/bin/ruff check gamas_bot scripts tests passenger_wsgi.py --select E9,F
All checks passed!
$ .venv/bin/python -m compileall -q gamas_bot scripts tests passenger_wsgi.py   # ok
$ .venv/bin/python -m pip check                                              # no broken requirements
$ .venv/bin/python -m scripts.validate_docx --out … --render                 # PASSED, offline render
$ .venv/bin/python /tmp/docx_validation/run_validation.py                     # PASSED, 0 failures
```

---

## 1. Files changed (this session)

| File | Change |
|---|---|
| `requirements.txt` | Declared `arabic-reshaper` and `python-bidi` — the offline page renderer (`scripts/render_docx_pages.py`, also used by `scripts.validate_docx --render`) imports both to shape Persian and reorder mixed lines; they were missing, so Persian pages rendered as disconnected letters. |
| `tests/test_billing.py` | +4 regression tests: paid entitlement expires exactly `starts_at + 30 days` (§47 #26); one approval creates exactly one entitlement even when clicked twice (#37); a cancelled job releases the full reservation exactly once (#33); a duplicate reservation is impossible — same-duration retry is idempotent, conflicting duration refused, balance charged once (#34). |
| `tests/test_media_intake.py` | +1 pipeline test: insufficient balance rejects the job **before** STT/LLM — neither `transcribe` nor `structure_transcript` is awaited, the reservation is recorded `insufficient` with 0 reserved seconds, the user gets the available/required message with a tracking code, and nothing is charged (§47 #29 at the bot level, §18). |
| `docs/FINAL_REPORT.md` | This report. |

No production code needed changes: the audit (§1 of the task) found the
billing/payment/credential/DOCX implementation from `faeb8cd` complete against
§0–§53 except the items above. Everything else below describes the audited
system as it stands.

## 2. Migrations added

None this session. Existing, audited and idempotent:

- `migrations/001_initial.sql` — users, submissions, transcriptions, broadcasts.
- `migrations/002_presentations.sql` — presentation columns + `presentation_clips`.
- `migrations/003_billing_and_credentials.sql` — `plans` (seeded `free_lifetime_1h`,
  `paid_25h_30d`, `paid_50h_30d`), `payment_requests`, `entitlements`
  (unique partial index: one `free_lifetime` per user), `usage_reservations`,
  `usage_ledger`, `admin_audit_log`, `provider_credentials` (ciphertext only),
  plus the one-time migration granting existing users their single free hour.
- `migrations/004_production_hardening.sql` — renames the legacy free-plan code
  to `free_1h` (guarded, never duplicates), adds `receipt_file_id`/`admin_note`,
  and the indexes the payment/credential/health panels query.

The runner (`Database._apply_migrations`) applies files in name order inside a
transaction, records them in `schema_migrations`, tolerates interrupted
`ALTER TABLE` replays, and is covered by
`test_migrations_can_be_replayed_on_an_existing_schema` and
`test_restarting_the_database_reapplies_nothing_twice`.

## 3. DOCX root cause and exact fix (RTL)

Root cause class: paragraph alignment alone cannot make a document RTL — Word
derives direction from `w:docDefaults`, style `w:pPr`, paragraph `w:bidi`,
paragraph-mark `w:rPr/w:rtl`, and run `w:rPr/w:rtl` + complex-script slots.
The fix (already in `faeb8cd`, audited line-by-line this session) lives in
`gamas_bot/docx_export.py` and `gamas_bot/bidi.py`:

- `w:docDefaults` run/paragraph defaults carry the Persian language and RTL
  base direction (`_configure_document_defaults`).
- Styles `Normal`, `Title`, `Subtitle`, `Heading 1–3`, table/callout/quote/TOC
  styles are marked RTL (`_configure_styles`, `_configure_toc_entry_styles`).
- Persian-dominant paragraphs get `w:bidi` + right alignment + `w:rtl` on the
  paragraph mark (`_enable_bidi`, `_set_paragraph_mark_direction`).
- Runs are split by `bidi.split_direction_runs`: RTL segments get `w:rtl`,
  `w:cs`, `w:szCs` and the Persian face; Latin/technical segments are explicitly
  LTR (`<w:rtl w:val="0"/>`) with `w:ascii`/`w:hAnsi` and the Latin face.
  Logical order is never reversed manually.
- Tables get `w:bidiVisual` (RTL visual column order), RTL cell paragraphs,
  repeating header rows (`w:tblHeader`), and schema-ordered `w:tblPr` children
  (`scripts/validate_docx.py` re-checks `pPr`/`rPr`/`tblPr`/`trPr` ordering).
- Header and footer are RTL too; the footer keeps a real `PAGE` field
  (`w:fldChar` + `w:instrText PAGE`), which is the only field left in the file.
- Section marks set `w:rtlGutter`; fonts are per-role (Persian body/heading,
  Latin, fallback advertised via `w:altName`), never embedded.

## 4. Static TOC implementation

The Word TOC field (`w:fldChar`/`w:instrText TOC`/F9 placeholder/
`w:updateFields`) is gone. `build_notes_docx` now:

1. writes a skeleton TOC table (`_add_static_toc_skeleton`) with fixed-width
   placeholder page numbers, followed by an explicit page break;
2. fills it from the real bookmarked headings (`_fill_static_toc`), honouring
   `DOCX_TOC_LEVELS` (default `1-1`) and `DOCX_TOC_MIN_SECTIONS`;
3. renders the document and maps every entry to a real page
   (`_rendered_toc_page_numbers`), fills the numbers
   (`_set_static_toc_page_numbers`), and re-renders until the numbers are stable
   (`_finish_static_toc`, up to 3 iterations + final verification render).

No `w:fldChar`, no `TOC` instruction, no `w:updateFields`, and no user-visible
F9 instruction remains — asserted by tests and re-verified on real generated
files this session.

## 5. Page-number calculation method

Production: LibreOffice (`soffice --headless --convert-to pdf`) + `pypdf`.
Every TOC hyperlink's PDF link destination is resolved to its physical page;
if the renderer is missing, the PDF is unusable, the link count differs from
the entry count, or a target lands before page 3, generation **fails
explicitly** (`DocxPaginationError`) — page numbers are never guessed. This
failure path was exercised live this session: with no `soffice` in the sandbox,
`build_notes_docx` on a TOC-worthy document raised
`DocxPaginationError("…LibreOffice (soffice) در سرور پیدا نشد.")`.

Sandbox validation: LibreOffice cannot be installed here (no root, no package
mirror). For §48 the measurement pass was substituted with an oracle built on
the repository's own offline renderer (`scripts/render_docx_pages.py`), which
performs a real page layout of the .docx. Each TOC number written into a
validated document therefore comes from a real rendered layout, and was then
**cross-checked against an independent render of the final document**: every
number in the TOC equals the physical page where that heading actually starts.
This is a validation-harness substitution only; production code still requires
LibreOffice and still fails explicitly without it.

## 6. Bookmark/hyperlink implementation

- Every body heading gets a unique bookmark `GamasHeading{n}`
  (`_add_bookmark`) — valid OOXML name, unique by counter, so duplicate topic
  titles still get unique anchors.
- Each TOC row's title cell is a `w:hyperlink w:anchor="GamasHeading{n}"` with
  `w:history="1"` and a tooltip; runs inside keep their bidi segmentation.
- Verified on real files: bookmarks unique and name-valid, every anchor
  resolves to a `w:bookmarkStart`, hyperlink count equals TOC row count.

## 7. Billing schema

Integer seconds everywhere; no float hours in accounting. `plans`
(id/code/name/included_seconds/validity_days/price_toman/enabled/sort_order),
`entitlements` (granted/remaining seconds, `starts_at`, `expires_at`, status
`active|expired|revoked|exhausted`, `source` `free_lifetime|payment|admin_credit`,
`payment_id`, `granted_by_admin_id`), `usage_reservations`, `usage_ledger`
(event types `reserve|consume|release|denied|grant|adjustment`), plus the
unique partial index `idx_entitlement_free_once_per_user`. The canonical
catalogue lives in `gamas_bot/billing.py::plan_catalog`, is upserted by
`Database.sync_plan_catalog` at startup, and is overridable only through the
seven `FREE_PLAN_HOURS`/`PLAN_*` environment variables resolved in
`gamas_bot/config.py`.

## 8. Reservation/usage logic

`Database.reserve_usage` runs in a `BEGIN IMMEDIATE` transaction: it verifies
submission ownership, expires stale entitlements, sums available seconds,
refuses with an audited `denied` ledger row when the balance is short, and
otherwise allocates from the earliest-expiring active entitlement first
(free/lifetime entitlements last). `finalize_usage` consumes the actual media
seconds and refunds the unused remainder per entitlement; `release_usage`
returns the full reservation. All three are idempotent and guarded by
`WHERE status='reserved'`, so retries, restarts
(`_recover_interrupted_submissions` releases reservations of jobs that cannot
survive a restart) and double-clicks cannot double-charge or lose credit.
Cross-process safety is tested with two `Database` connections racing on one
balance (`test_two_database_connections_cannot_reserve_the_same_credit_twice`).

## 9. Payment workflow

`💳 خرید اشتراک` → plan picker → instructions showing plan, hours, validity,
price, and the canonical card (`5022 2913 3290 6625` / امیرعلی غمخوار /
بانک پاسارگاد — normalized to digits in config, grouped by the single
`format_payment_card` helper) → "رسید پرداخت را به‌صورت تصویر ارسال کنید".
An image (JPEG/PNG/WebP, magic-byte checked, ≤ `MAX_PAYMENT_RECEIPT_BYTES`,
stored under `RECEIPT_DIR` — outside the web root, dir `700`/file `600`,
random hex name, never the user's filename) creates a `pending`
`payment_requests` row. **No credit is granted at submission time.** Receipts
never enter the STT/PPT pipeline (image-with-open-payment is intercepted first;
tests `test_receipt_image_with_an_open_payment_is_never_a_media_job` etc.).
Old receipts are deleted only after the configurable retention measured from
admin review.

## 10. Admin workflow

Admin-only, private-chat-only panels: `💳 پرداخت‌ها` (pending list → detail with
the private receipt → `✅ تایید` / `❌ رد`), `⏱ اعتبار کاربران` (per-user
free/paid/total, entitlements with expiry, usage history, manual `seconds |
reason` adjustment with audit), `🩺 وضعیت سرویس‌ها` (manual, 5-minute cache,
cheap read-only probes; states configured/healthy/degraded/rate_limited/
authentication_failed/unavailable/disabled/not_configured; masked credentials,
sanitized errors, cooldown display), `🔑 API Keys` (add → metadata → secret
message deleted before storage; enable/disable/delete/reorder/test), `📜 گزارش
مدیر` (audit log), plus stats/users/broadcast/ban/unban. Every payment decision,
credential change and adjustment writes an `admin_audit_log` row without
secrets or receipt contents.

## 11. Credential architecture

`gamas_bot/provider_credentials.py::ProviderCredentialManager` pools
per-(service, provider) keys from `provider_credentials` (Fernet ciphertext at
rest; the master key is env-only via `PROVIDER_CREDENTIALS_ENCRYPTION_KEY` and
never touches SQLite), ordered by `priority, id`, skipping quarantined and
cooling-down keys, with the legacy environment key as a trailing fallback.
`gamas_bot/provider_health.py` runs the manual probes and feeds the same
rotation state. Both STT (`gamas_bot/stt.py`) and notes
(`gamas_bot/structuring.py`) obtain credentials through the manager via
`current_provider_credentials()` / `use_provider_credentials()` and report
results through `record_result`.

## 12. 429 rotation logic

On HTTP 429: the credential is marked `cooldown` with `cooldown_until` from
`Retry-After` (bounded 1 s … 7 days, default 30 s), excluded from selection
until it expires, and the next healthy credential is tried — the same key is
not hammered. On 401/403 the credential is `quarantined` (excluded until an
admin re-enables it). On 400/415/422 the request is treated as invalid and the
credential is **not** rotated. 5xx/network errors get bounded exponential
retries (`STT_MAX_ATTEMPTS`, `STT_RETRY_*`; note API `NOTE_API_RETRIES`), then
rotation/provider fallback. Success clears cooldown/quarantine. All-credentials
exhausted returns a clean, secret-free error. Tests:
`test_429_cools_down_the_first_key_and_rotates_to_the_next`,
`test_401_quarantines_the_first_key_and_rotates_to_the_next`,
`test_bad_request_does_not_rotate_credentials`,
`test_retry_after_header_is_honoured_and_bounded`,
`test_retries_are_bounded_by_the_configuration`,
`test_provider_secrets_never_reach_the_logs`, and more in
`tests/test_provider_credentials.py`, `tests/test_provider_health.py`,
`tests/test_provider_robustness.py`, `tests/test_provider_contracts.py`.

## 13. Security changes (audit result)

No new holes found; the existing controls were verified by test and inspection:
admin callbacks require `is_admin` and private chat; `billing:*` callbacks are
private-only and ownership-checked in SQL (`cancel_payment_intent`,
`submit_payment_receipt` both scope `WHERE user_id=?`); payment actions are
whitelisted; receipt paths are re-resolved and confined to the private root
(`_safe_receipt_path`); receipt bytes are never logged; API keys are
Fernet-encrypted, masked (`••••…last4`) in every UI/audit/log path, and
rejected from labels/URLs/notes that contain key fragments; settings `repr`
hides secrets; SQL is parameterized throughout; the audit log truncates and
never stores secrets. New this session: the insufficient-balance path is
covered end-to-end at the pipeline level (no STT call, no charge).

## 14. cPanel impact

None. No new daemon, no Redis, no Docker, no external DB, no root requirement.
SQLite + WAL with owner-only modes, receipts under `RECEIPT_DIR` outside the
web root, migrations applied automatically at startup, the env-supplied
encryption master key, and the existing `passenger_wsgi.py`/launcher/systemd
paths are unchanged. `scripts/cpanel_preflight.py` and
`tests/test_cpanel_deploy.py` pass. Restart persistence is tested
(`test_payment_and_entitlement_state_survive_a_restart`).

## 15. Tests executed

- `python -m pytest tests/ -q` → **678 passed, 1 skipped, 1623 subtests**
  (673 before this session; +5 new tests: 4 billing + 1 pipeline).
- `python -m unittest discover -s tests` → **OK** (CI entry point).
- `ruff check … --select E9,F`, `compileall`, `pip check` → **clean**.

## 16. Test results

All green. The §47 list is covered as follows (test names in
`tests/test_billing.py`, `tests/test_payment_flow.py`,
`tests/test_provider_credentials.py`, `tests/test_provider_health.py`,
`tests/test_docx_export.py`, `tests/test_docx_layout_requirements.py`,
`tests/test_docx_polish.py`, `tests/test_media_intake.py`,
`tests/test_provider_robustness.py`, `tests/test_provider_contracts.py`):
DOCX #1–#20 ✓ (RTL doc/styles/paragraphs/runs/tables/glossary/header/footer,
static TOC, real titles+numbers, no F9, no `w:fldChar`, TOC page 2, body page
3, unique bookmarks, resolving hyperlinks, duplicate titles, reopen, schema
order). Billing #21–#38 ✓ (incl. this session's #26/#33/#34/#37 and the
pre-existing #21–#25, #27–#32, #35, #36, #38). Payments #39–#47 ✓.
Provider credentials #48–#60 ✓ (multi-key, encryption, masking, no log leaks,
429 cooldown + Retry-After, rotation, 401/403 quarantine, 400/422 no-rotate,
bounded 5xx/network, recovery on success, exhausted-pool error, admin-only
health panel).

## 17. Real DOCX validation results

Performed this session with the repository's own tooling
(`scripts/validate_docx.py`, `scripts/render_docx_pages.py`):

- `python -m scripts.validate_docx --out … --render` → **PASSED**, offline
  render, "no layout warnings (no blank page, no overflow, no orphan heading)".
  (This command previously degraded Persian because `arabic-reshaper`/
  `python-bidi` were undeclared; fixed in `requirements.txt`.)
- Four real sample documents generated and validated end-to-end:
  - `persian_only_4topics` — 4 pages, TOC 6 rows, numbers [3,3,3,3,3,4]
  - `mixed_medical_10topics` — Persian+English medical (HbA1c, eGFR,
    Metformin 500 mg, 120/80 mmHg, mg/dL, %, URLs, emails, Cyrillic/CJK
    stress text), 5 pages, TOC 14 rows
  - `long_22topics` — 22 topics, long titles, duplicated titles, 7 pages,
    TOC 24 rows, numbers [3…7]
  - `numerical_tables` — numeric tables, ranges, formulas, URLs, dates,
    3 pages, TOC 5 rows
- For every document: structural validation ok; XML checks ok (no TOC field, no
  F9 text, no `updateFields`, footer `PAGE` field intact, bookmarks unique/
  valid, hyperlinks resolve and match TOC rows, docDefaults/Normal/headings/
  tables/header/footer RTL, Latin runs explicitly LTR, complex-script fonts);
  real render with **zero layout warnings**; **page 1 = cover only, page 2 =
  static TOC, page 3 = first body heading**; and every TOC page number equals
  the physical page where that heading starts in an independent render of the
  final file.
- Pages 1–3 of the mixed-medical and long documents were **visually inspected**
  (rendered PNGs): RTL cover with brand/quote, RTL TOC with page numbers in
  the left column, mixed-script titles and paragraphs in correct logical
  order, RTL tables with the first logical column on the right, centered LTR
  formulas, header/footer with the page field, page frame — all correct.
- Explicit-failure path verified live: without a renderer, a TOC document is
  **not** produced (raises `DocxPaginationError`).

Honesty note: these page numbers come from the repository's offline renderer
used as the measurement oracle, because LibreOffice is not installable in this
sandbox. In production the numbers come from LibreOffice itself, and without it
the document is not delivered at all. Word's own pagination may differ from
any renderer by a line; the numbers are correct for a real rendered layout, not
guaranteed pixel-identical to Microsoft Word.

## 18. Remaining limitations

1. **LibreOffice absent in this sandbox** — production TOC pagination was
   verified only through its explicit-failure path plus the offline-renderer
   oracle; a deployment run with real `soffice` has not been executed here.
2. **No live provider health check was executed** — no real API keys exist in
   this environment; health/rotation behaviour is verified with scripted HTTP
   fakes only.
3. **Payment verification is manual by design** — card-to-card receipt review
   by an administrator; there is no automatic gateway and none is claimed.
4. The offline renderer is a layout approximation (DejaVu faces, approximate
   justification, no tab leaders drawn); it is used for validation, not as a
   Word clone. Missing-glyph boxes for CJK in the sandbox renders are a font
   limitation of the renderer, not a document defect.
5. The 1 skipped test is a pre-existing POSIX/Windows-conditional skip
   (unchanged from `main`).
