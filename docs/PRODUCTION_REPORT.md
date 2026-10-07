# Production hardening — final report (18 points, §54)

Branch `arena/59eaae07-gamas-bot`, based on `main` (`cea2e61`). Every statement
below is either **verified** with a command run in this environment, or marked
**not verified** with the reason. Nothing here claims a rendered page number, a
live provider health result or a bank verification that was not actually
produced. The full-suite count quoted is the last run of this revision:

```
$ .venv/bin/python -m pytest tests/ -q
673 passed, 1 skipped, 1620 subtests passed in 44.96s
$ .venv/bin/ruff check gamas_bot scripts tests passenger_wsgi.py --select E9,F
All checks passed!
```

---

## 1. Exact plans, exactly as specified — **verified**

One canonical catalogue in `gamas_bot/billing.py`, seeded into SQLite, read by
every screen:

| Plan code | Included time | Price | Validity |
|---|---:|---:|---:|
| `free_1h` | 3,600 s (1 hour) | free | lifetime, once per user, `/start` never renews |
| `paid_25h_30d` | 90,000 s | 150,000 Toman | 30 days from approval |
| `paid_50h_30d` | 180,000 s | 250,000 Toman | 30 days from approval |

Paid entitlements are cumulative; consumption takes the entitlement that expires
soonest first. Evidence: `tests/test_billing.py` (17 tests), including the
legacy-code migration, restart idempotence, the configured-tariff seed, and the
check that no handler hard-codes a price or the card number.

## 2. Canonical card-to-card configuration in one place — **verified**

`CANONICAL_PAYMENT_CARD = "5022291332906625"`, holder «امیرعلی غمخوار», bank
«بانک پاسارگاد» live only in `gamas_bot/config.py`, are overridable from the
environment, and are grouped by the single `format_payment_card()` helper
(`5022 2913 3290 6625`). `tests/test_billing.py` asserts both the values and
that `bot.py` contains none of them.

## 3. True Persian RTL DOCX — **verified structurally, visually inspected offline**

`docDefaults`/styles/paragraph marks/runs carry BiDi, Persian runs carry the
complex-script slots (`w:cs` + `w:szCs` + `w:bCs`), Latin runs carry an explicit
`w:rtl w:val="0"`, and mixed lines are ordered logically. `python -m
scripts.validate_docx` reports 0 structural failures for both sample documents,
including `rtl_paragraphs_present`, `ltr_runs_marked`,
`complex_script_faces_set` and `element_ordering_valid`. Five real documents
(Persian-only, Persian+English medical, 22 topics, numeric/unit-heavy,
URLs/e-mail) were rendered with the repository's offline renderer and inspected
by eye.

## 4. Static table of contents with no F9 — **verified as structure; page numbers NOT validated as rendered**

Page 1 is the cover, page 2 is the topic list, page 3+ are the notes. Verified by
`tests/test_docx_layout_requirements.py` (9 tests): the heading paragraph, the
borderless RTL two-column table, unique internal bookmarks shared with the body
headings, one `w:hyperlink w:anchor` per row, Persian-digit page numbers, no
`w:instrText`, no `TOC \o` field and no `updateFields` anywhere; the footer keeps
a real `PAGE` field. `DOCX_TOC_LEVELS` defaults to `1-1`. Content-based heading
matching, so long and duplicate titles are handled.

**Not verified in this environment:** the numbers the offline renderer drew came
from an injected page map (`_rendered_toc_page_numbers` was mocked), and the
offline renderer is not Word or LibreOffice. Real rendered pagination, Word
hyperlink navigation and PDF-destination mapping require LibreOffice + `pypdf`
on the delivery host. When exact mapping is unavailable the DOCX is **not**
delivered with guessed numbers — `DocxPaginationError` fails closed, and
`tests/test_bot_presentation_flow.py` covers that path.

## 5. Integer-second entitlement/usage-ledger architecture — **verified**

`plans`, `entitlements` and `usage_ledger` tables with
reserve → consume/release, all inside SQLite transactions; grants are
idempotent, the balance can never go negative, and concurrent jobs cannot spend
the same seconds. Evidence: `tests/test_billing.py`,
`tests/test_regressions.py`, and the reservation-release tests in the
presentation flow.

## 6. Manual receipt + admin approve/reject — **verified; manual by design**

`tests/test_payment_flow.py` (17 tests + 11 subtests) covers: a payment stays
pending until a human decision; approval notifies the user once and is
idempotent (a second approval grants nothing); rejection stores a sanitized
reason and grants nothing; the receipt path is recorded with its `file_id`; the
user can submit a new request afterwards; a second image while pending is
refused with the request id instead of being queued as media.

**Approval is a manual administrative decision by design.** The bot performs no
bank, card or transfer verification and does not claim to. It displays the
canonical card, stores the private receipt for a human to inspect, and records
who decided and when. Receipts live outside the web root in `RECEIPT_DIR`
(700/600), under server-generated names, and are deleted after
`RECEIPT_RETENTION_DAYS` counted from review.

## 7. Encrypted multi-key credentials with rotation rules — **verified**

Keys are Fernet-encrypted in SQLite, masked everywhere (`••••••••1234`), with 429
→ `Retry-After` cooldown + rotation, 401/403 → quarantine, 400/415/422 → no
rotation, and bounded retries for 5xx/network failures. Evidence:
`tests/test_provider_credentials.py` (14 tests + 3 subtests), including
"secrets never reach the logs" and "all keys exhausted produces a clean,
secret-free error".

## 8. Administrator panels — **verified (rendered text and callbacks asserted)**

| Panel | Entry point | Tests |
|---|---|---|
| 💳 پرداخت‌ها (payments) | `admin:payments`, `/payments` | payment listing/detail/approve/reject |
| 🩺 وضعیت سرویس‌ها (health) | `admin:health`, `admin:health:refresh` | status text, cached badge, per-key test button |
| 🔑 API Keys (credentials) | `admin:credentials` | add/enable/disable/delete/reorder/test, mask only |
| ⏱ اعتبار کاربران (user credits) | `admin:credits`, `admin:credit:add:<id>` | balance split, entitlements, ledger, adjustment |

Every one of them was rendered in a local harness with an admin account and an
empty database, and the exact text and buttons are asserted by tests. Nothing
above required a Telegram connection.

## 9. Telegram UX menus — **verified**

The main menu, admin menu and every submenu carry the section-40 labels; the
full set is asserted through the callbacks in `tests/test_payment_flow.py` and
`tests/test_provider_health.py`. Insufficient-credit messages state available vs
required duration and offer 💳 خرید اشتراک.

## 10. Security audit — **verified where a test exists**

* **IDOR / callback spoofing:** receipts and every admin action are refused for a
  non-admin sender and in group chats (`test_a_non_admin_cannot_open_a_receipt_or_a_payment`,
  `test_group_chat_cannot_reach_the_admin_payment_panel`).
* **Path traversal:** `_safe_receipt_path` refuses `/etc/passwd`, a file outside
  the receipt root and `../` escapes (`test_receipt_paths_outside_private_storage_are_refused`).
* **Secret/receipt leakage:** masking asserted in panels, audit entries and logs;
  no receipt path is exposed on the user-facing screens.
* **Duplicate approval / quota races:** transactional single-transaction approval
  and reservation tests in `test_billing.py` and `test_payment_flow.py`.
* **Prompt/injection and prompt-injection of admin input:** rejection reasons and
  notes pass through `clean_human_text()` (ZWNJ/ZWJ preserved, control
  characters dropped).

## 11. Regression tests from §47 — **verified for the areas below**

The new regression module set is `tests/test_payment_flow.py`,
`tests/test_provider_health.py`, `tests/test_provider_credentials.py`
(extended), `tests/test_billing.py` (extended) and
`tests/test_docx_layout_requirements.py`; the whole suite passes
(673 passed, 1 skipped, 1,620 subtests).

| Regression area | Where it is covered |
|---|---|
| free grant is once-per-user and not renewed by `/start` | `test_billing.py` |
| legacy `free_lifetime_1h` → `free_1h` migration, no double grant | `test_billing.py` |
| paid plans, cumulative balance, earliest expiry consumed first | `test_billing.py` |
| concurrent reservation cannot overspend or create a negative balance | `test_billing.py`, `test_regressions.py` |
| release on cancellation/STT failure/pagination failure | `test_bot_presentation_flow.py`, `test_billing.py` |
| receipt intake, pending state across restart, duplicate approval | `test_payment_flow.py` |
| a lecture file is never swallowed by an open payment | `test_payment_flow.py` |
| rejection grants nothing and sanitizes the reason (ZWNJ kept) | `test_payment_flow.py`, `test_audit.py` |
| receipt privacy: admin-only, path-traversal-proof, group chats refused | `test_payment_flow.py` |
| 429 cooldown + rotation, 401/403 quarantine, 400/422 no rotation | `test_provider_credentials.py` |
| all keys exhausted → clean secret-free error; success clears state | `test_provider_credentials.py` |
| no secret in logs, panels or audit entries | `test_provider_credentials.py`, `test_provider_health.py` |
| health states, cache, cost-safe probes, concurrency, masking | `test_provider_health.py` |
| DOCX page layout contract and static TOC (no F9 / no `TOC` field) | `test_docx_layout_requirements.py` |
| RTL/LTR runs, tables, header/footer, element ordering | `test_docx_polish.py`, `test_docx_export.py`, `scripts/validate_docx.py` |

The brief's 60 items were **not** enumerated one-by-one against a checklist in
this report; the rows above are the areas this revision added or re-verified. No
existing test was removed or weakened (the only assertion change replaced an
over-strict expectation of exactly one cache invalidation with the correct
single production call).

## 12. Real DOCX validation — **partly verified**

`scripts/validate_docx.py` passed with 0 structural failures; the corpus was
rendered with `scripts/render_docx_pages.py` (no blank page, no overflow, no
orphan heading, in any sample). The offline renderer needed one real fix found
during this audit: it read only direct `w:r` children, so every hyperlinked TOC
row rendered blank — after the fix the topic list shows its titles and page
numbers. **Not verified:** real Word/LibreOffice rendering. LibreOffice is not
installed in this environment (`curl` reaches the network but `apt-get` cannot
resolve `libreoffice-writer`).

## 13. cPanel compatibility — **preserved, verified by inspection and preflight**

No Docker, no Redis, no external database, no new daemon, no root requirement.
SQLite (WAL) plus `RECEIPT_DIR` on disk; migrations run automatically at
startup, are transactional and idempotent — verified by applying migration 004
to a pre-004 database, restarting three times, and confirming the same balance
and no duplicated plan, entitlement or ledger row. `scripts/cpanel_preflight.py`
runs; the only FAIL in this sandbox is the intentionally absent `.env`.

## 14. Documentation updated — **verified**

`README.md` (plan table + plan keys, purchase/balance/credit/health/key
management, credential rotation, validation corpus), `.env.example` (seven plan
keys, receipt retention wording), `docs/DEPLOY_CPANEL.md` (state-on-disk table,
panels, retention), `docs/DEPLOY_FA.md` (menus, adjustments, health panel,
rotation) and `docs/AUDIT.md` (revision log with findings 1–11 and what was and
was not validated).

## 15. No unverified claims — **the rule applied to this report**

* No page-number accuracy is claimed: the numbers inspected came from an
  injected page map and the offline renderer, never from a real office render.
* No live provider health result is claimed: every probe test injects its HTTP
  response; no provider was contacted and no probe was executed against a real
  endpoint.
* Payment verification is stated everywhere as manual-administrator-by-design.
* No Telegram login, no paid STT/LLM request and no bank interaction happened
  while producing this revision.

## 16. Health checks are cost-safe and leak nothing — **verified**

Probes only call read-only list endpoints (Speechmatics jobs, Deepgram projects,
Gemini/Anthropic/OpenAI-compatible models). No transcription job is submitted
and no model content is generated; a deployment that cannot be probed safely is
reported as *configured* with a note (`probe_supported=False`). The panel is
manual and cached for 5 minutes, so it is never executed per Telegram event, and
probe results run concurrently to keep the panel responsive. Provider error text
reaches the screen only through `sanitize_detail()`; only the masked tail of a
key is ever shown, and a test asserts no key material appears in the panel text.

## 17. Minimum-change rule — **honoured**

No working subsystem was rewritten. The DOCX author, STT layer, structuring
layer and MediaWorker are untouched apart from the one renderer fix in
`scripts/render_docx_pages.py`. Existing modules gained additive functions
(`clean_human_text`, `add_audit_entry`, `admin_user_credit_overview`,
`credential_for_test`, `reorder`); no existing behaviour was replaced, no
working test was deleted, and the single assertion that was relaxed was
over-strict rather than protective (it demanded exactly one cache invalidation
where the correct design performs one production call, now asserted as such).

## 18. Remaining, explicitly open — **not verified, do not claim otherwise**

| Item | Status | What closes it |
|---|---|---|
| Real rendered page numbers / Word pagination / hyperlink navigation | not verified | install LibreOffice + `pypdf` on the host, run a long document through the pipeline, inspect the delivered PDF/DOCX |
| `soffice`-based visual validation | not verified | same |
| Live provider health check and live rotation under a real 429 | not verified | run the panel once against a real provider account |
| Live Telegram flows (menus, receipt upload, approval notification) | not verified | a manual smoke test on the deployment host |
| Real bank transfer approval | manual by design | an administrator verifies the transfer and approves |
