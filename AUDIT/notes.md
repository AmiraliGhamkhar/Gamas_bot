# Audit working notes

## Phase 0 — Recon (completed 2026-11-15 UTC)

**Status (5 lines):**
1. [VERIFIED] Repository instructions were read first: `AGENTS.md`, `README.md`, `docs/DEVELOPMENT.md`, `docs/TESTING.md`, `docs/CPANEL.md`, and CI material.
2. [VERIFIED] Work remained on `arena/c4b817d1-gamas-bot`; no separately prepared `audit/<date>` branch/worktree was available.
3. [VERIFIED] Packaging is pip/setuptools using `pyproject.toml` plus generated `requirements.txt`; no resolver lockfile or `runtime.txt` exists.
4. [VERIFIED] Repository cPanel material selects Python 3.11; the live cPanel interpreter is [UNVERIFIED].
5. [VERIFIED] Architecture, trust boundaries, entry paths, and review hotspots are recorded in `AUDIT/architecture.md`.

### Evidence

- [VERIFIED] `git branch --show-current` returned `arena/c4b817d1-gamas-bot`.
- [VERIFIED] `pyproject.toml:23` declares `requires-python = ">=3.11"`; `.github/workflows/tests.yml:18` has a 3.11–3.13 matrix.
- [VERIFIED] `README.md:29-39` and `docs/CPANEL.md:14-26` choose/create a Python 3.11 environment.
- [VERIFIED] `gamas_bot/bot.py:544-555` registers Telethon callback and message ingress.

## Phase 1 — Baseline (completed 2026-11-15 UTC)

**Status (5 lines):**
1. [VERIFIED] An isolated Python 3.11.2 `.venv` was created without root/system-package installation.
2. [VERIFIED] The initial resolver selected Pillow 12.3.0; `AUDIT/results/pip-audit.json` recorded 42 Pillow advisories.
3. [VERIFIED] The original source/test baseline and randomized/dev-warning checks passed before remediation; source-recon results remain in `AUDIT/results/`.
4. [UNVERIFIED] Native 3.12–3.14 and live cPanel execution were unavailable in this sandbox.
5. [UNVERIFIED] Semgrep failed to fetch its remote rule pack with TLS EOF; its empty output is not treated as a pass.

## Phase 2 — Security, resource, concurrency and cPanel review (completed 2026-11-15 UTC)

**Status (5 lines):**
1. [VERIFIED] F-001 was fixed in commit `3bff030`: `Pillow>=13.0,<14.0` and a regression test prevent the audited vulnerable range from returning.
2. [VERIFIED] F-002 was reproduced against the production disk-admission function, then changed in commit `12aa5ea` so cPanel preflight fails an impossible aggregate download profile.
3. [VERIFIED] F-003 documentation ambiguity was corrected: the presentation byte values in `.env.example` are explicitly cPanel overrides, not unset defaults.
4. [VERIFIED] Final pytest passed **1,894 tests / 510 subtests**; unittest discovery passed **1,860 tests**; Ruff, mypy, compileall, pip check, requirements synchronization, media-worker self-check, and pip-audit passed.
5. [VERIFIED] The offline cPanel benchmark completed six scripted pipelines in **8.5834 s** with **121.86 MB** peak RSS and exercised both scripted Speechmatics→Deepgram and AvalAI→Gemini fallback.

### F-001 — Pillow parser dependency floor (remediated)

- [VERIFIED] Image-processing ingress uses Pillow at `gamas_bot/doc_extract.py:540-544`, `gamas_bot/document/qa.py:285-293`, and `gamas_bot/intake.py:269-285`.
- [VERIFIED] `AUDIT/results/pip-audit.json` records the pre-fix Pillow 12.3.0 advisories; `AUDIT/results/pip-audit-after-preflight.json` reports no known vulnerabilities after the fix.
- [VERIFIED] Commit `3bff030 security: require patched Pillow release` changes `pyproject.toml:45` and `requirements.txt:13`; `tests/test_dependency_security.py:13-20` locks the expected range.

### F-002 — cPanel download-capacity coherence (remediated to fail closed)

- [VERIFIED] The default 1 GiB input plus a 64 MiB reserve appears at `gamas_bot/config.py:210-216`; media downloads reserve `reported_bytes * 2` at `gamas_bot/bot_jobs.py:807-828`, and `gamas_bot/telegram_transfer.py:72-87` rejects shortfall.
- [VERIFIED] The production-admission reproduction in `AUDIT/results/cpanel-disk-incoherence-repro.log` showed one 1 GiB input requires 2,214,592,512 bytes on a 1 GiB filesystem and produced `DiskSpaceError`.
- [VERIFIED] `scripts/cpanel_preflight.py:173-216` now calculates the aggregate capacity of the larger media/document ceiling, reserve, two working copies, and all configured download slots; `run_all()` executes it at `scripts/cpanel_preflight.py:343-358`.
- [VERIFIED] `AUDIT/results/cpanel-preflight-capacity-after.log` confirms default effective settings return `level=FAIL` at one GiB free because two slots need 4.12 GiB.
- [VERIFIED] `tests/test_cpanel_preflight.py:18-86` covers failure, max-document selection, exact capacity, and `run_all()` integration. Commit `12aa5ea` contains the fix and cPanel materials.
- [INFERRED] Preflight cannot reserve disk against unrelated future processes; the existing runtime admission guard remains necessary and provides a second fail-closed layer.

### F-003 — presentation capacity documentation drift (remediated in documentation)

- [VERIFIED] `gamas_bot/config.py:216` retains a 2 GiB unset presentation temporary default.
- [VERIFIED] `.env.example:274-284` now labels the 128/256 MiB presentation values as conservative cPanel overrides; `docs/FEATURES.md:86-92`, `docs/CPANEL.md:44-73`, and `docs/OPERATIONS.md:164-215` document the actual-free-space preflight gate and a 128 MiB shared-host profile.

### Commands and final evidence

```bash
.venv/bin/python -m pytest -p no:randomly -q
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m ruff check gamas_bot scripts tests
.venv/bin/python -m mypy
.venv/bin/python -m mypy scripts/cpanel_preflight.py
.venv/bin/python -m compileall -q gamas_bot scripts tests passenger_wsgi.py
.venv/bin/python -m pip check
.venv/bin/python -m scripts.sync_requirements --check
.venv/bin/python -m gamas_bot.media_worker check
.venv/bin/python -m pip_audit -r requirements.txt --format json
.venv/bin/python -m scripts.concurrency_bench cpanel
```

- [VERIFIED] The final outputs are retained under `AUDIT/results/`, including `pytest-after-preflight.log`, `unittest-after-preflight.log`, `pip-audit-after-preflight.json`, and `concurrency-after-preflight.log`.
- [VERIFIED] `ruff format --check` reports pre-existing formatting drift in 151 files; no formatter suppression, test deletion, skipped test, xfail, or threshold relaxation was introduced.
- [UNVERIFIED] Offline source/tests cannot prove real provider quota behavior, provider response formats, or the cPanel host’s process/RAM/IO enforcement.
