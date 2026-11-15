# Gamas Bot security and reliability audit — Second pass

> **Historical audit:** This document records an earlier checkout and its
> reported results, not the current branch or a fresh verification. Generated
> `AUDIT/results/` logs are ignored and are not present in the repository;
> cited line numbers and removed files may no longer match current code.
> Re-run relevant checks before relying on these findings.

**Audit date:** 2026-11-15 UTC

**Branch:** `arena/357a91cc-gamas-bot` (Arena-managed; no branch switch)

**Scope:** checkout source and tests only. No `.env`, Telegram session, cron entry, production data, or live Telegram/STT/LLM/OCR endpoint was accessed.

## Evidence and claim policy

Every conclusion below is marked **[VERIFIED]**, **[INFERRED]**, or **[UNVERIFIED]**. A `[VERIFIED]` item has both a current source/test citation and a named command log under `AUDIT/results/`; a scanner match alone is not a vulnerability. Logs containing a non-zero tool exit are evidence of the reported condition, never a pass.

## Executive result

- **[VERIFIED] F-002 is closed at runtime:** aggregate, process-local temporary-disk reservations make concurrent Telegram downloads account for every active two-working-copy budget, and the lease is released in a `finally` block. See `gamas_bot/telegram_transfer.py:82-134`, `gamas_bot/bot_jobs.py:808-837`, `tests/test_download_reservations.py:1-134`, and `AUDIT/results/b2-reservation-regression-after.log`.
- **[VERIFIED] F-002 stream bounds are closed:** a declared Telegram size is an independent stream cap, generic and declared-size overflows delete the partial target, and a final declared-size mismatch is rejected. See `gamas_bot/telegram_transfer.py:295-408`, `tests/test_download_reservations.py:122-134`, and `AUDIT/results/b3-streaming-limit-regression-after.log`.
- **[VERIFIED] Three randomized warning-as-error Python 3.11 runs passed before the final audit-only characterization test was added: each reports `1894 passed, 510 subtests passed`. See `AUDIT/results/a2-pytest-random-seed-104729.log`, `AUDIT/results/a2-pytest-random-seed-130363.log`, and `AUDIT/results/a2-pytest-random-seed-15485863.log`.
- **[VERIFIED] Current offline dependency audit reported zero advisories.** `requirements.txt` was audited by `pip-audit`; see `AUDIT/results/c4-pip-audit-deduplicated.log` and `AUDIT/results/c4-pip-audit.json`.
- **[VERIFIED] The clean current-source secret scan has five candidates, all manually disposed as non-secrets:** the referral character alphabet at `gamas_bot/db_referrals.py:25` and four explicitly synthetic benchmark configuration values at `scripts/concurrency_bench.py:260-270`. See `AUDIT/results/c3-detect-secrets-clean-source-summary.log`.
- **[VERIFIED] The clean source/docs/config history scan found zero candidates across the three reachable commits while excluding tests, `AUDIT`, `.env`, session, cron, and data paths.** See `AUDIT/results/c3-git-history-detect-secrets-clean-summary.log`.
- **[UNVERIFIED] Native execution on Python 3.12/3.13, a live cPanel quota profile, and provider behavior under live credentials were not available in this sandbox.** CI declares 3.11–3.13 at `.github/workflows/tests.yml:12-29`, while package metadata permits `>=3.11` at `pyproject.toml:18-28`.

## Final-gate state

| Gate | Result | Evidence |
|---|---|---|
| Randomized `pytest`, warnings as errors, seeds 104729 / 130363 / 15485863 | **[VERIFIED]** 3 × `1894 passed, 510 subtests passed` before the audit characterization test | `a2-pytest-random-seed-*.log` |
| Final post-commit `pytest -W error -q -p no:randomly` | **[VERIFIED]** 1,904 passed, 510 subtests passed | `AUDIT/results/final-pytest.log` |
| Final post-commit `unittest discover -q` | **[VERIFIED]** 1,870 tests passed | `AUDIT/results/final-unittest.log` |
| Final post-commit Ruff / mypy / compileall | **[VERIFIED]** all passed; mypy checked 74 source files | `final-ruff.log`, `final-mypy.log`, `final-compileall.log` |
| Final post-commit package/worker checks | **[VERIFIED]** `pip check`, requirements synchronization, and media-worker self-check passed | `final-pip-check.log`, `final-requirements-sync.log`, `final-media-worker.log` |
| Final post-commit dependency and citation checks | **[VERIFIED]** zero dependency advisories; 144 citations, zero invalid references | `final-pip-audit.log`, `final-citation-check.log` |
| Pytest vs unittest collection reconciliation | **[VERIFIED]** 1,894 pytest nodes, 1,860 unittest methods, zero missing unittest methods before F-002/audit-only tests were added | `AUDIT/results/a3-runner-diff.log` |
| F-002 reservation and stream regression tests | **[VERIFIED]** reservation 5 passed; selected stream/intake tests 12 passed | `b2-reservation-regression-after.log`, `b3-streaming-limit-regression-after.log` |
| Offline Bandit | **[VERIFIED]** completed with 269 heuristic findings; dispositions below | `c1-bandit.json`, `c1-bandit-production-triage-candidates.log` |
| Offline local-rule Semgrep | **[VERIFIED]** completed with four warnings in developer scripts; dispositions below | `c2-semgrep-offline-summary.log` |
| Ruff security/style selection | **[VERIFIED]** completed with 392 mixed source/test diagnostics; not a clean security claim | `c2-ruff-security.txt` |
| Clean source/history secret scans | **[VERIFIED]** no undisposed source/history credential candidates | `c3-detect-secrets-clean-source-summary.log`, `c3-git-history-detect-secrets-clean-summary.log` |
| Document/media characterization | **[VERIFIED]** 169 passed, 12 subtests passed | `d3-document-media-characterization.log` |
| LLM/render characterization | **[VERIFIED]** 441 passed, 122 subtests passed | `d4-llm-render-characterization.log` |
| STT/capacity/job characterization | **[VERIFIED]** 114 passed | `d5-stt-capacity-job-characterization.log` |
| Config/launcher characterization | **[VERIFIED]** 96 passed, 127 subtests passed | `d6-config-launcher-characterization.log` |
| Database/learning characterization | **[VERIFIED]** 249 passed, 2 subtests passed | `d7-db-learning-characterization.log` |

## Finding F-002 — concurrent download admission and stream-size enforcement

**Severity:** High (availability/resource exhaustion)

**Status:** **[VERIFIED] remediated in commits `198c073` and `251855f`**

**Trust boundary:** Telegram media bytes → temporary filesystem

### Evidence

- **[VERIFIED]** The reservation ledger keys claims by filesystem device, combines existing reservations with a new request under a thread lock, and exposes idempotent release. See `gamas_bot/telegram_transfer.py:58-134` and `AUDIT/results/b2-reservation-regression-after.log`.
- **[VERIFIED]** Download admission reserves `reported_bytes * 2` plus the configured free-space floor inside the bounded file-I/O path, then releases the lease in `finally`. See `gamas_bot/bot_jobs.py:808-837` and `AUDIT/results/b2-reservation-regression-after.log`.
- **[VERIFIED]** The transfer helper treats the known declared size as a separate maximum, checks cumulative progress and final size, and removes the target for every `BaseException` path. See `gamas_bot/telegram_transfer.py:215-320` and `AUDIT/results/b3-streaming-limit-regression-after.log`.
- **[VERIFIED]** The focused regressions exercise aggregate rejection, idempotent release, cancellation/failure cleanup, declared-size overflow, generic overflow, and target cleanup. See `tests/test_download_reservations.py:1-134`, `AUDIT/results/b2-reservation-regression-after.log`, and `AUDIT/results/b3-streaming-limit-regression-after.log`.

### Residual risk

- **[INFERRED]** Reservations are process-local by design; separate bot processes sharing a filesystem are protected operationally by the single-instance lock, not by an inter-process disk ledger. The lock uses an OS-held exclusive lock at `gamas_bot/instance_lock.py:37-115`; the claim has not been tested against a live shared-host process supervisor in this audit.

## Static-analysis dispositions

### Bandit

- **[VERIFIED]** Bandit found 72 production heuristics: 40 B608 SQL-construction alerts and 32 low-severity assertions, exception-handling, pseudo-random, subprocess, and XML-escaping alerts. See `AUDIT/results/c1-bandit-production-triage-candidates.log`.
- **[VERIFIED]** The 40 B608 alerts are not a reproduced SQL injection: identifier interpolation is constrained to fixed maps/constants or generated `?` placeholder lists, while externally supplied values remain bound parameters. Representative controls are `gamas_bot/db_reporting.py:45-84`, `gamas_bot/db_support.py:27-58`, `gamas_bot/db_support.py:180-198`, `gamas_bot/db_users.py:75-100`, and `gamas_bot/db_users.py:440-500`; see `AUDIT/results/c1-bandit-production-triage-candidates.log` and `AUDIT/results/d7-db-learning-characterization.log`.
- **[VERIFIED]** B311 concerns do not generate security tokens: it is retry jitter at `gamas_bot/db_core.py:99-105`, multipart boundary variation at `gamas_bot/ocr.py:373-381`, and backoff jitter at `gamas_bot/ocr.py:581-589`. See `AUDIT/results/c1-bandit-production-triage-candidates.log`.
- **[VERIFIED]** The B404/B603 subprocess pair is a fixed vector `[sys.executable, "-m", "gamas_bot"]`, has no shell, redirects standard input, and starts a new session. See `gamas_bot/launcher.py:253-279` and `AUDIT/results/c2-targeted-grep.log`.
- **[VERIFIED]** B406 flags imports of an XML escape function, not XML parsing; the output path escapes rendered text before ReportLab markup. See `gamas_bot/notes_pdf.py:120-150`, `gamas_bot/pdf_export.py:168-172`, and `AUDIT/results/c1-bandit-production-triage-candidates.log`.
- **[INFERRED]** B101/B110/B112 remain code-quality/reliability review items rather than confirmed security vulnerabilities. Their listed sites either assert internal postconditions or use best-effort cleanup/logging; representative cleanup is `gamas_bot/media.py:130-157`. See `AUDIT/results/c1-bandit-production-triage-candidates.log`.

### Semgrep and targeted API inventory

- **[VERIFIED]** Offline Semgrep reported four interpolated-SQL warnings only in developer scripts: `scripts/admin_report_bench.py:154,212` and `scripts/import_v2.py:83,112`. The first iterates a literal table tuple; the second obtains migration table/column names from a restrictive migration regex before a local SQLite call. See `scripts/admin_report_bench.py:145-215`, `scripts/import_v2.py:70-120`, and `AUDIT/results/c2-semgrep-offline-summary.log`.
- **[VERIFIED]** The targeted inventory matched no `eval`/`exec`, pickle, unsafe YAML, `mktemp`, archive extraction, `shell=True`, or direct `requests`/`httpx` usage in the scan scope. It did inventory the controlled subprocess sites and SQL construction. See `AUDIT/results/c2-targeted-grep.log`.
- **[UNVERIFIED]** This was an offline local-rule Semgrep configuration, not the upstream hosted/default ruleset; it must not be interpreted as equivalent to a full remote Semgrep policy scan. See `AUDIT/results/c2-semgrep-status.log`.

## Manual trust-boundary review

- **[VERIFIED] Telegram callbacks require a private sender, reject banned users, and reject `adm:`/`payv:` callbacks before dispatch for non-administrators.** See `gamas_bot/bot.py:1523-1563` and `AUDIT/results/d6-config-launcher-characterization.log`.
- **[VERIFIED] Document callbacks parse exact shapes/types and load document metadata through a user-scoped lookup before showing or exporting it.** See `gamas_bot/bot_documents.py:1544-1599`, `gamas_bot/bot_documents.py:1848-1910`, `tests/test_document_ingestion.py:489-518`, and `AUDIT/results/d3-document-media-characterization.log`.
- **[VERIFIED] Admin hub parameters are allowlisted/validated after the central admin gate.** See `gamas_bot/bot_admin.py:1748-1845`, `tests/test_admin_hubs.py:123-144`, and `AUDIT/results/d6-config-launcher-characterization.log`.
- **[VERIFIED] Billing reserves only an owned submission in one transaction, checks balance/ban state, records allocations, and payment review atomically updates payment, credit/referral, and audit data.** See `gamas_bot/db_billing.py:259-374`, `gamas_bot/db_payments.py:313-403`, `gamas_bot/db_referrals.py:64-199`, and `AUDIT/results/d7-db-learning-characterization.log`.
- **[VERIFIED] Subscription denial is fail-closed only for a confirmed denial; an unavailable membership check presents an unavailable state rather than falsely calling a user a non-member.** See `gamas_bot/subscription.py:398-457` and `AUDIT/results/d7-db-learning-characterization.log`.
- **[VERIFIED] Document/PPTX intake has image-pixel, OOXML expansion/text, ZIP member/path, XML parser, and subprocess timeout/kill controls.** See `gamas_bot/doc_extract.py:80-128`, `gamas_bot/doc_extract.py:290-315`, `gamas_bot/presentations.py:240-435`, `gamas_bot/media.py:102-180`, and `AUDIT/results/d3-document-media-characterization.log`.
- **[INFERRED]** The absence of a compression-ratio cap is not recorded as a confirmed finding: extraction has explicit absolute expansion limits, ZIP entry/name checks, and bounded member reads. The deliberately stricter ratio-only characterization failed as expected (`AUDIT/results/d3-zip-compression-ratio-before.log`) but did not establish that a legitimate low-output-ratio file violates the current security contract. A human policy decision is required before adding a new rejection threshold.
- **[VERIFIED] STT response bodies, transfer deadlines, capacity waits, job queues, and child-process lifetimes are explicitly bounded.** See `gamas_bot/stt.py:269-358`, `gamas_bot/capacity.py:75-178`, `gamas_bot/jobs.py:165-330`, and `AUDIT/results/d5-stt-capacity-job-characterization.log`.
- **[VERIFIED] User-derived review HTML is escaped and delivered with a restrictive hashed-script CSP; DOCX/PDF text is made XML-safe before rendering.** See `gamas_bot/review_sheet_html.py:163-185`, `gamas_bot/review_sheet_html.py:322-351`, `gamas_bot/docx_export.py:261-302`, `gamas_bot/notes_pdf.py:120-150`, and `AUDIT/results/d4-llm-render-characterization.log`.

## Characterization and benchmark evidence

- **[VERIFIED]** New direct characterization tests keep prompt source text out of the static system prompt, prove the composed bot includes the admin/document/billing mixins, exercise launcher early-exit classification, and check document callback IDs. See `tests/test_risky_module_characterization.py:1-58` and `AUDIT/results/e-risky-module-characterization.log`.
- **[VERIFIED]** The offline one-minute synthetic PyAV matrix completed; the shipped Opus 32k VoIP configuration encoded at 80× real time and projected 12.7 MB/hour on this sandbox. See `AUDIT/results/f1-pyav-benchmark.log`.
- **[VERIFIED]** The offline DOCX benchmark built a 30-section booklet in 0.283 s (36k-char source) and 0.251 s (108k-char source) on this sandbox. See `AUDIT/results/f2-docx-benchmark.log`.
- **[VERIFIED]** The realistic Persian short, medical-long, and mixed RTL/LTR DOCX/PDF fixtures built and passed structural/raster QA: respectively 3/5/3 PDF pages with PDF build times of 0.249/0.274/0.238 s. See `AUDIT/results/f3-pdf-docx-realistic-render-benchmark.log` and `tests/test_booklet_rendering.py:1-60`.
- **[INFERRED]** These offline timings are local ratios, not capacity commitments for cPanel or provider calls.

## Open human items

1. **[UNVERIFIED]** Run the full Python 3.12 and 3.13 matrix (and decide whether supported metadata should cap or test Python 3.14) in CI; this sandbox only has Python 3.11.2. See `pyproject.toml:18-28`, `.github/workflows/tests.yml:12-29`, and `AUDIT/results/a2-environment.log`.
2. **[INFERRED]** Decide whether a compression-ratio limit is desired in addition to the existing absolute OOXML expansion and ZIP-member limits; doing so can reject highly compressible legitimate Office documents. See `gamas_bot/doc_extract.py:290-315`, `gamas_bot/presentations.py:401-425`, and `AUDIT/results/d3-zip-compression-ratio-before.log`.
3. **[UNVERIFIED]** Validate live provider policies, cPanel disk/process/RAM/IO quotas, and the configured subscription channel with production-authorized operators only; those endpoints/data were intentionally not accessed here.
