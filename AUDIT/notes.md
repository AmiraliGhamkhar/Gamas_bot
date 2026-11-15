# Audit working notes — Second pass

## Scope and safety record

- **[VERIFIED]** The repository instructions were read before work: `AGENTS.md`, `README.md`, `docs/DEVELOPMENT.md`, `docs/TESTING.md`, and `docs/CPANEL.md`; source/test tooling only was used.
- **[VERIFIED]** No `.env`, Telegram session, cron entry, production data, or live Telegram/STT/LLM/OCR endpoint was read or called. Command evidence is stored in `AUDIT/results/`.
- **[VERIFIED]** Work remains on the Arena-managed `arena/357a91cc-gamas-bot` branch; no branch was created, switched, or renamed.

## Phase A — evidence integrity, baseline, and interpreter coverage

**Status (5 lines):**
1. **[VERIFIED]** Three seeded warning-as-error Python 3.11 pytest runs passed with `1894 passed, 510 subtests passed`; see `AUDIT/results/a2-pytest-random-seed-104729.log`, `a2-pytest-random-seed-130363.log`, and `a2-pytest-random-seed-15485863.log`.
2. **[VERIFIED]** Pytest/unittest collection reconciliation found 1,894 pytest nodes and 1,860 unittest methods with zero methods absent from pytest; see `AUDIT/results/a3-runner-diff.log`.
3. **[VERIFIED]** Metadata allows Python `>=3.11` at `pyproject.toml:18-28`; CI declares 3.11–3.13 at `.github/workflows/tests.yml:12-29`.
4. **[UNVERIFIED]** The sandbox had only Python 3.11.2, so native 3.12/3.13 (and metadata-permitted 3.14) execution did not occur; see `AUDIT/results/a2-environment.log`.
5. **[VERIFIED]** Citation/document integrity will be rechecked after all audit-document edits; this phase does not treat an earlier citation check as final evidence.

## Phase B — F-002 runtime closure

**Status (5 lines):**
1. **[VERIFIED]** Commit `198c073` adds filesystem-keyed, locked download reservations at `gamas_bot/telegram_transfer.py:82-134` and consumes them around transfer at `gamas_bot/bot_jobs.py:808-837`.
2. **[VERIFIED]** Commit `251855f` adds declared-size and final-size enforcement plus partial-file cleanup at `gamas_bot/telegram_transfer.py:295-408`.
3. **[VERIFIED]** Aggregate reservation, cancellation/failure release, and idempotent lease release regressions passed; see `tests/test_download_reservations.py:1-134` and `AUDIT/results/b2-reservation-regression-after.log`.
4. **[VERIFIED]** Declared-overflow and generic-overflow cleanup regressions passed; see `tests/test_download_reservations.py:122-134` and `AUDIT/results/b3-streaming-limit-regression-after.log`.
5. **[INFERRED]** The reservation ledger is intentionally process-local; the OS instance lock at `gamas_bot/instance_lock.py:37-115` is the operational protection against two local bot processes.

## Phase C — static, dependency, and secret scanning

**Status (5 lines):**
1. **[VERIFIED]** Bandit completed with 269 total findings / 72 production heuristics; the full output and production index are `AUDIT/results/c1-bandit.json` and `c1-bandit-production-triage-candidates.log`.
2. **[VERIFIED]** Offline Semgrep completed four developer-script SQL warnings and no matches for its other local dangerous-API rules; see `AUDIT/results/c2-semgrep-offline-summary.log`.
3. **[VERIFIED]** `pip-audit -r requirements.txt` reported zero known advisories; see `AUDIT/results/c4-pip-audit-deduplicated.log`.
4. **[VERIFIED]** Clean source scan candidates were manually disposed as a referral alphabet and synthetic benchmark values; reachable source/docs/config history had zero candidates; see `c3-detect-secrets-clean-source-summary.log` and `c3-git-history-detect-secrets-clean-summary.log`.
5. **[UNVERIFIED]** Offline local Semgrep is not a substitute for a maintained remote/default Semgrep policy; see `AUDIT/results/c2-semgrep-status.log`.

### Scanner dispositions

- **[VERIFIED]** B608 matches compose only fixed identifiers, schema-derived columns from the local database, or generated placeholders; values are parameterized. Representative locations: `gamas_bot/db_reporting.py:45-84`, `gamas_bot/db_support.py:51-58`, `gamas_bot/db_support.py:180-198`, and `gamas_bot/db_users.py:75-100`. Evidence: `AUDIT/results/c1-bandit-production-triage-candidates.log`.
- **[VERIFIED]** The scan’s B311 locations use randomness for retry/multipart values rather than secrets: `gamas_bot/db_core.py:99-105`, `gamas_bot/ocr.py:373-381`, and `gamas_bot/ocr.py:581-589`. Evidence: `AUDIT/results/c1-bandit-production-triage-candidates.log`.
- **[VERIFIED]** The current source secret scan’s high-entropy alphabet is `gamas_bot/db_referrals.py:19-25`, and the keyword matches are synthetic benchmark settings at `scripts/concurrency_bench.py:260-270`. Evidence: `AUDIT/results/c3-detect-secrets-clean-source-summary.log`.
- **[VERIFIED]** Test-only scan candidates are isolated to test fixtures/mocks, including an explicitly synthetic redaction token/key at `tests/test_logging_config.py:107-145`; see `AUDIT/results/c3-detect-secrets-tests-summary.log`.

## Phase D — module and trust-boundary characterization

**Status (5 lines):**
1. **[VERIFIED]** Document/media, archive, transfer, and foreign-document ownership coverage passed 169 tests plus 12 subtests; see `AUDIT/results/d3-document-media-characterization.log`.
2. **[VERIFIED]** LLM, evidence, transcript, DOCX/PDF, and review-sheet coverage passed 441 tests plus 122 subtests; see `AUDIT/results/d4-llm-render-characterization.log`.
3. **[VERIFIED]** STT/failover/capacity/job coverage passed 114 tests; config/launcher coverage passed 96 tests plus 127 subtests; see `d5-stt-capacity-job-characterization.log` and `d6-config-launcher-characterization.log`.
4. **[VERIFIED]** Billing/payment/referral/support/learning coverage passed 249 tests plus 2 subtests; see `AUDIT/results/d7-db-learning-characterization.log`.
5. **[INFERRED]** A standalone compression-ratio cap remains a policy trade-off rather than a confirmed defect because absolute archive limits already apply; the stricter proposed-only test failed as expected in `AUDIT/results/d3-zip-compression-ratio-before.log`.

### Added direct characterization suite

- **[VERIFIED]** `tests/test_risky_module_characterization.py:27-58` proves the static prompt contract, composition mixins, launcher immediate-exit classification, and document callback identifier construction. It passed 5 tests with Ruff clean; see `AUDIT/results/e-risky-module-characterization.log` and `e-risky-module-ruff.log`.
- **[UNVERIFIED]** Mypy of that test resolves imported test support and therefore currently surfaces six pre-existing `tests/support.py` typing errors rather than a new error in the characterization test; see `AUDIT/results/e-risky-module-mypy.log`.

## Phase E — realistic offline benchmark

**Status (5 lines):**
1. **[VERIFIED]** The synthetic one-minute PyAV format matrix ran on Python 3.11.2 / PyAV 18.1.0; see `AUDIT/results/f1-pyav-benchmark.log`.
2. **[VERIFIED]** The 30-section DOCX benchmark completed for 36k and 108k source-character cases; see `AUDIT/results/f2-docx-benchmark.log`.
3. **[VERIFIED]** Short Persian, long medical, and mixed RTL/LTR DOCX/PDF fixtures passed DOCX structural and PDF raster QA; see `AUDIT/results/f3-pdf-docx-realistic-render-benchmark.log` and `tests/test_booklet_rendering.py:1-60`.
4. **[INFERRED]** Benchmark timings are local measurements and do not assert cPanel capacity or provider latency.
5. **[UNVERIFIED]** Live provider performance and cPanel kernel-quota behavior were intentionally out of scope.

## Phase F — finalization

**Status (5 lines):**
1. **[VERIFIED]** The three audit documents have been rewritten for the Second pass and the architecture matrix covers every named trust boundary; see `AUDIT/REPORT.md:1` and `AUDIT/architecture.md:1-53`.
2. **[VERIFIED]** The post-commit full gates passed: pytest 1,904 plus 510 subtests, unittest 1,870, Ruff, mypy, compileall, package/worker checks, dependency audit, and citation validation; see `AUDIT/results/final-pytest.log`, `final-unittest.log`, `final-ruff.log`, `final-mypy.log`, `final-pip-audit.log`, and `final-citation-check.log`.
3. **[VERIFIED]** No new source behavior change is proposed from the scans; the only open ratio-limit idea would change what user Office uploads are accepted.
4. **[UNVERIFIED]** A full remote Semgrep policy scan and native Python 3.12/3.13 execution remain unavailable in this sandbox.
5. **[UNVERIFIED]** Live cPanel/provider verification requires an authorized environment and is an open human item.
