# Gamas Bot audit report

**Audit date:** 2026-11-15 UTC
**Scope:** Telethon ingress, concurrent media/document pipelines, SQLite, cPanel resource constraints, STT/LLM fallback, booklet generation, and dependency security.
**Evidence policy:** Each assessment is marked **VERIFIED**, **INFERRED**, or **UNVERIFIED**. Named `AUDIT/results/` files contain the local command output; no provider service, production data, `.env`, session, or cron job was intentionally accessed.

## Executive result

- **VERIFIED — remediated:** Pillow 12.3.0 was reachable from untrusted image-ingress paths and had 42 `pip-audit` advisories. Commit `3bff030` raises the declared floor to Pillow 13.0 and adds a regression test.
- **VERIFIED — remediated to fail closed:** A 1 GiB cPanel filesystem cannot admit the shipped 1 GiB media/document maximum under the existing two-working-copy rule. Commit `12aa5ea` makes cPanel preflight fail rather than permit that impossible deployment profile, without changing generic upload defaults.
- **VERIFIED — documentation clarified:** The 2 GiB unset presentation temporary default remains unchanged; `.env.example` now explicitly identifies its 128/256 MiB presentation values as cPanel overrides, not code defaults.
- **VERIFIED:** The final suite passed 1,894 tests and 510 subtests; unittest discovery passed 1,860 tests; the dependency audit reported no known vulnerabilities.
- **UNVERIFIED:** No live cPanel deployment was available, so its chosen interpreter and kernel-enforced RAM/process/disk/IO quotas could not be measured.

## Deployment runtime selection

- **VERIFIED:** The repository deployment material selects **Python 3.11**: `README.md:29-39` and `docs/CPANEL.md:14-26` create/select a 3.11 virtual environment. `pyproject.toml:23` requires Python `>=3.11`, and `.github/workflows/tests.yml:18` tests 3.11–3.13.
- **UNVERIFIED:** The exact interpreter selected in a live cPanel Setup Python App is outside this checkout. The audit sandbox used Python 3.11.2.

## Final gates

| Assessment | Command | Result | Evidence |
| --- | --- | --- | --- |
| **VERIFIED** | `.venv/bin/python -m pytest -p no:randomly -q` | 1,894 passed, 510 subtests passed in 332.58 s | `AUDIT/results/pytest-after-preflight.log` |
| **VERIFIED** | `.venv/bin/python -m unittest discover -s tests -q` | 1,860 tests passed in 297.079 s | `AUDIT/results/unittest-after-preflight.log` |
| **VERIFIED** | `.venv/bin/python -m ruff check gamas_bot scripts tests` | All checks passed | `AUDIT/results/ruff-after-preflight.log` |
| **VERIFIED** | `.venv/bin/python -m mypy` and targeted preflight mypy | 74 production files and `scripts/cpanel_preflight.py` passed | `AUDIT/results/mypy-after-preflight-full.log`, `mypy-cpanel-preflight.log` |
| **VERIFIED** | compileall, `pip check`, requirements-sync, media-worker self-check | All passed | `AUDIT/results/compileall-after-preflight.log`, `pip-check-after-preflight.log`, `requirements-sync-after-preflight.log`, `media-worker-check-after-preflight.log` |
| **VERIFIED** | `.venv/bin/python -m pip_audit -r requirements.txt --format json` | No known vulnerabilities | `AUDIT/results/pip-audit-after-preflight.json` |
| **VERIFIED** | `.venv/bin/python -m scripts.concurrency_bench cpanel` | Six scripted jobs completed in 8.5834 s; 121.86 MB peak RSS | `AUDIT/results/concurrency-after-preflight.log` |

- **VERIFIED:** `ruff format --check` reports pre-existing formatting drift in 151 files (`AUDIT/results/ruff-format-after-preflight.log`). The project explicitly does not enforce formatter output in CI (`pyproject.toml` Ruff comment); no formatter suppression, threshold relaxation, or unrelated mass reformat was made.
- **UNVERIFIED:** The scripted benchmark models provider latency/429 events and does not measure actual cPanel 10 MB/s IO, 1024 IOPS, process accounting, or provider accounts.

## F-001 — Pillow image-parser dependency floor

**Severity:** HIGH security
**Status:** **VERIFIED — REMEDIATED**

### Evidence

- **VERIFIED:** Potentially untrusted image processing reaches Pillow at `gamas_bot/doc_extract.py:540-544`, `gamas_bot/document/qa.py:285-293`, and `gamas_bot/intake.py:269-285`.
- **VERIFIED:** Before the fix, `pyproject.toml:45` and `requirements.txt:13` allowed Pillow 12.3.0. `AUDIT/results/pip-audit.json` records 42 Pillow advisories in the resolved baseline.
- **VERIFIED:** The saved baseline audit identifies Pillow 13.0.0 as the available fix for those advisories.

### Remediation and verification

- **VERIFIED:** Commit `3bff030 security: require patched Pillow release` changes the package metadata and generated requirements entry to `Pillow>=13.0,<14.0`.
- **VERIFIED:** `tests/test_dependency_security.py:13-20` rejects a future regression below the protected range.
- **VERIFIED:** The post-fix environment resolved Pillow 13.0.0, `pip check` passed, and `AUDIT/results/pip-audit-after-preflight.json` contains no vulnerable dependency.

### Residual risk

- **INFERRED:** A future Pillow advisory remains possible because the application uses bounded ranges rather than a resolver lockfile; recurring dependency audit is still required.

## F-002 — 1 GiB disk profile versus 1 GiB upload defaults

**Severity:** HIGH availability/resource coherence
**Status:** **VERIFIED — REMEDIATED TO PREFLIGHT FAILURE**

### Reproduction

- **VERIFIED:** `gamas_bot/config.py:210-216` defines a 1 GiB `DEFAULT_MAX_FILE_SIZE_BYTES` and a 64 MiB temporary reserve. `gamas_bot/config.py:1289-1296` applies that ceiling to `MAX_FILE_SIZE`, `DOC_MAX_BYTES`, and `DOC_UNPACKED_MAX_BYTES` when unset.
- **VERIFIED:** `gamas_bot/bot_jobs.py:807-828` passes `reported_bytes * 2` plus `TEMP_MIN_FREE_BYTES` to the download admission guard before a Telegram transfer.
- **VERIFIED:** `gamas_bot/telegram_transfer.py:72-87` raises `DiskSpaceError` when the temporary filesystem has less space than that requirement, failing closed before download.
- **VERIFIED:** The original reproduction at `AUDIT/results/cpanel-disk-incoherence-repro.log` invoked that production guard with 1,073,741,824 bytes free. A default maximum input required 2,214,592,512 bytes for one admission and returned `result=REFUSED (DiskSpaceError)`.
- **VERIFIED:** The default has two concurrent download slots (`gamas_bot/config.py:220`, `gamas_bot/config.py:1536-1542`). A capacity profile therefore must account for two independent two-working-copy admissions, not only one transfer.

### Remediation

- **VERIFIED:** `scripts/cpanel_preflight.py:173-216` now calculates the maximum of the media/document input ceilings, two working copies, the configured temporary reserve, and every `MAX_CONCURRENT_DOWNLOADS` slot. It returns a **FAIL** result if actual `temp_dir` free space is below the aggregate requirement.
- **VERIFIED:** `scripts/cpanel_preflight.py:343-358` includes this check in the normal preflight run, so `main()` returns a non-zero status whenever the new result is a failure.
- **VERIFIED:** `tests/test_cpanel_preflight.py:18-59` reproduces a one-GiB-free/default-sized two-download failure, verifies the larger document ceiling is considered, and verifies an exact capacity boundary. `tests/test_cpanel_preflight.py:61-86` proves `run_all()` includes the failure.
- **VERIFIED:** `AUDIT/results/cpanel-preflight-capacity-after.log` runs the effective default settings against a synthetic 1 GiB free-space result and records `level=FAIL` and a 4.12 GiB aggregate requirement.
- **VERIFIED:** Commit `12aa5ea fix: reject impossible cpanel download capacity` contains this preflight guard, regression tests, and the deployment-material update.

### Operator effect

- **VERIFIED:** Generic 1 GiB upload ceilings were intentionally retained, per the selected remediation option; a cPanel deployment with insufficient actual free space is now blocked at preflight rather than silently advertised as viable.
- **VERIFIED:** `docs/CPANEL.md:44-73`, `docs/OPERATIONS.md:164-215`, and `.env.example:41-49` document the 4.125 GiB default two-download requirement and provide a 128 MiB cPanel override profile. That override requires 640 MiB free temporary space for its two slots before database, log, receipt, or virtual-environment usage.
- **INFERRED:** A 1 GiB nominal account may still lack 640 MiB at runtime after application/runtime data is present. The actual `Temp download capacity` result is therefore the deployment gate, not the nominal account quota.

## F-003 — presentation capacity documentation drift

**Severity:** MEDIUM operability/documentation
**Status:** **VERIFIED — REMEDIATED IN DEPLOYMENT MATERIAL**

- **VERIFIED:** `gamas_bot/config.py:216` retains `DEFAULT_PRESENTATION_MAX_TEMP_BYTES = 2 * 1024**3`.
- **VERIFIED:** Before the documentation clarification, `.env.example:273-280` displayed 128 MiB unpacked/256 MiB temporary presentation values without identifying them as explicit cPanel overrides; `docs/FEATURES.md:86-100` also displayed the 256 MiB profile.
- **VERIFIED:** `.env.example:274-284` now says those values are conservative cPanel overrides rather than unset code defaults; `docs/FEATURES.md:86-92` points shared-host deployments to the capacity preflight and cPanel profile.
- **INFERRED:** This removes the operator-facing ambiguity while preserving generic defaults and avoids an unapproved user-visible change to default presentation capability.

## Focused concurrency, persistence, and security review

- **VERIFIED:** SQLite enables WAL/NORMAL, foreign keys, bounded cache/journal settings, a query-only read connection, and serialized `BEGIN IMMEDIATE` writes at `gamas_bot/db_core.py:249-364`.
- **VERIFIED:** `CapacityPool` provides immediate priority reservation/overflow and bounded waits at `gamas_bot/capacity.py:97-176`; STT uses it at `gamas_bot/stt.py:1291-1545` and LLM routing uses it at `gamas_bot/llm.py:2320-2533`.
- **VERIFIED:** The post-fix scripted benchmark recorded two Speechmatics overflow skips and two Deepgram attempts/successes, plus two AvalAI rate-limit failovers to Gemini, without queue-wait counters (`AUDIT/results/concurrency-after-preflight.log`). This verifies the scripted fallback contract, not production provider quota behavior.
- **VERIFIED:** Telegram transfer paths use declared-size checks, optional byte ranges, stall detection, positional writes, cleanup, and download semaphores at `gamas_bot/telegram_transfer.py:98-320` and `gamas_bot/bot_jobs.py:807-832`.
- **VERIFIED:** Media worker subprocesses use argument vectors, no shell, timeout/cancellation cleanup, and POSIX process-group termination at `gamas_bot/media.py:102-186`.
- **VERIFIED:** Reviewed Bandit B608 sites in `db_billing.py`, `db_library.py`, `db_payments.py`, `db_reporting.py`, `db_support.py`, and `db_users.py` interpolate only local/whitelisted schema identifiers or generated placeholder lists; user/data values use SQLite parameters. No SQL-injection reproduction was obtained.
- **UNVERIFIED:** Static review and offline simulations cannot prove live provider response formats, quota exhaustion behavior, or cPanel kernel quota enforcement.

## Audit-process limitations

- **VERIFIED:** Work remained on `arena/c4b817d1-gamas-bot`. The supplied workspace was not a newly created `audit/2026-11-15` branch or separately prepared fresh-clone audit worktree.
- **VERIFIED:** No migration, schema, production data, authentication control, admin flow, live cron entry, `.env`, or session was changed.
- **UNVERIFIED:** Native execution on Python 3.12–3.14 did not occur because only Python 3.11.2 was installed in the audit sandbox.
- **UNVERIFIED:** Semgrep did not fetch its remote rule pack due TLS EOF; its empty output was not treated as a successful scan.
