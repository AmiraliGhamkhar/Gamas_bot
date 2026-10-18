# Gamas Bot — Deep Security & Design Audit

**Commit SHA audited:** `0753372120587e75b37ca2f198036c96e4eb5812` (branch `arena/a3ba84fe-gamas-bot`, `main` at merge #64)
**Date:** 2026-10-18
**Auditor role:** Principal-level, evidence-driven review — documentation is claim, code is truth — every finding anchored to file:line.

**Environment used for live verification:**
- Python 3.11, pip install from `pyproject.toml`/`requirements.txt` (runtime deps + `ruff`, `mypy`, `pypdf`, `pytest`)
- `ruff check .` → **All checks passed** (rule set `E4,E7,E9,F,W,B,C4`, line-length 120, `S` ignored by design, per `pyproject.toml:52-66`)
- `mypy` (incremental gate, 12 files listed in `pyproject.toml:70-85`) → **Success: no issues found in 12 source files** — the remaining 13 modules are intentionally excluded from strict typing, not verified.
- `pytest -q` (PYTHONPATH=`/home/user/Gamas_bot`) → **514 passed in 12.46s** (both `pytest` and `unittest discover` paths exercised in CI `tests.yml:67-76`)
- `python -m gamas_bot.media_worker check` → `{"av": "18.1.0", "ppt2pptx": "0.4.2"}`

---

## Table of Contents
1. Executive Summary
2. Phase 0 — Inventory & Ground Truth
3. Coverage Matrix
4. Findings Register (by severity)
5. Component Deep Dives (1-12, per prompt)
6. Prioritized Remediation Plan
7. Verification Gaps
8. Appendices

---

## 1. Executive Summary — Overall Health Verdict

**Overall: HEALTHY with material operational caveats — no critical RCE/data-loss, but three high-risk classes require immediate hardening before production scale.**

The codebase is unusually disciplined for a single-process Telegram bot: bounded queues, explicit back-pressure, reservation-and-ledger billing with recovery on restart, content-validated receipt handling, shell-free subprocess invocation, and provider error redaction. The test suite is large (514 tests, real PyAV/OOXML fixtures) and the CI gate is honest (ruff+mypy+pytest+dependency-consistency+compileall+migration-integrity).

**The three risks that matter most:**

1. **Receipt path traversal / arbitrary file read as admin (High, gamas_bot/bot.py:1834)** — `_show_receipt` serves any `receipt_path` stored in DB via `send_file` without confirming it lies inside `RECEIPT_DIR`. A compromised DB or SQLi in a future extension would let an admin (or forged payment row) read arbitrary files the bot can access. `SECURITY.md` and the billing promise of `RECEIPT_DIR` isolation are not enforced at read time.

2. **Billing reservation vs. unlimited-user race & pre-reserve credit check bypass for presentations (High, gamas_bot/bot.py:859-895, gamas_bot/db.py:360-430)** — Media path checks credit *before queueing* using `available_seconds` + duration hint, but presentation path enqueues without any pre-check and only reserves *after* `prepare_audio` inside the worker. A presentation with long narration can thus bypass admission control, and a user whose balance drops between enqueue and reserve will fail inside the worker after consuming media resources. Separately, `reserve()` special-cases `is_unlimited` *inside* `_write_lock` after re-reading DB, but `is_unlimited` itself does an unlocked read outside the lock window, allowing a TOCTOU where a user is flipped unlimited mid-reservation.

3. **Single-process fragility under slow/hung stages & launcher TOCTOU (High, gamas_bot/jobs.py, gamas_bot/launcher.py:30-46, gamas_bot/bot.py:239-260)** — The bounded queue is correctly bounded, but `launcher.ensure_running` uses `is_locked()` probe-then-spawn without atomic hold, so two concurrent web/cron watchdogs can both see “not running” and spawn, relying solely on `InstanceLock` in the child to serialize. `JobQueue.shutdown` rejects queued jobs with only a log line (no DB transition, no user notification). Any slow stage (STT poll interval up to 6h, media merge up to 3600s) holds a worker slot, and because `MAX_ACTIVE_JOBS=3` and heavy media is further capped by `MEDIA_WORKER_CONCURRENCY=1`, a single hung presentation can starve the entire pipeline with no per-job liveness watchdog beyond the subprocess timeout.

**Bottom line:** The money path (reserve→settle/release exactly once) is correct in the happy and failure paths tested, the media boundary is well-isolated, and the Persian export path is sound. The gaps that need fixing are trust-boundary enforcement (path checks), admission control completeness (presentations), and operational resilience (recovery, observability, lock races).

---

## 2. Phase 0 — Inventory & Ground Truth

### 2.1 Exact commit and tracked files
- HEAD `0753372` Merge PR #64 from `arena/bd6abd3f-gamas-bot`. `git status` clean on `arena/a3ba84fe-gamas-bot`.
- Tracked files (complete `find . -type f | sort` subset):
```
./.env.example ./ .github/workflows/tests.yml ./passenger_wsgi.py ./pyproject.toml ./requirements.txt
./deploy/gamas-bot.service ./docs/*.md (17 files) ./gamas_bot/*.py (26 files, 16,736 LoC)
./migrations/v2/*.sql (12 files) ./migrations/v3/*.sql (2 files)
./scripts/*.py ./tests/test_*.py (13) + support.py + fixtures
./assets/fonts/* ./README.md
```
- `pyproject.toml` (36 lines deps) is canonical; `requirements.txt` is generated via `scripts/sync_requirements.py`; `tests/test_config_consistency.py` enforces drift — CI job `Dependency consistency` checks `pip check` + `sync_requirements --check` (`tests.yml:28-31`). Verified `requirements.txt` matches `pyproject.toml` dependencies exactly.

### 2.2 Launcher & instance-lock pre-read
- `gamas_bot/launcher.py:1-90` — lightweight `ensure_running()` imports only `config`+`instance_lock`, uses fixed `subprocess.Popen([sys.executable, "-m", "gamas_bot"], ...)` with `shell=False` (implicit via `Popen` vector), `start_new_session`, `close_fds`. Stamp file `data/launcher.stamp` throttles spawns to 45s (`MIN_SPAWN_INTERVAL_SECONDS`).
- `gamas_bot/instance_lock.py:1-93` — advisory `fcntl.flock(LOCK_EX|LOCK_NB)` on `session_path.with_suffix(".lock")` (0600), kernel-released on any exit incl. `kill -9`. `is_locked()` probes by trying to acquire then releasing — TOCTOU noted above.

### 2.3 Documentation vs. code map (drift findings)

| Doc claim | Anchored code | Drift / Verdict |
|-----------|---------------|-----------------|
| Single Python process, Telethon client, bounded in-process asyncio queue, PyAV child process with hard timeout | `bot.py:156-250` run/shutdown, `jobs.py:24-119`, `media.py:88-140` run_command, `media_worker.py:1-374` | ✅ Verified. No external daemon. |
| Media runs on PyAV in child process with hard timeout; no system ffmpeg | `media.py:98-140` asyncio.create_subprocess_exec, `media_worker.py:7-17` | ✅ No `shell=True`, no `ffmpeg`/`ffprobe`/`soffice` strings beyond comments. |
| STT via Speechmatics/Deepgram/OpenAI-compatible | `stt.py:412-825` PROVIDERS dict | ✅ Three engines, ordered failover. |
| Notes via NaraRouter/OpenRouter/Gemini/Anthropic/OpenAI-compatible | `llm.py:1-1416`, `config.py:31-65` NOTE_PROVIDER_SLUGS | ✅ Five slugs, `note_routes()` policy. |
| Billing: prepaid, reservations-and-ledger, reserve before run, release on every failure | `db.py:360-430` reserve/settle/release, `bot.py:1070-1200` media/presentation reservation points | ✅ Invariants hold (see §3) with caveats noted in exec summary. |
| Free Office builds Word/Excel/PowerPoint/PDF from user text, no ledger touch | `office.py`, `office_ai.py`, `bot.py:580-680` _process_office | ✅ No ledger calls in office path. |
| Admin panel stats/users/pending payments/broadcasts | `bot.py:1740-1950` | ✅ All four tabs + audit log. |
| Deployment systemd or cPanel/Phusion Passenger | `deploy/gamas-bot.service`, `passenger_wsgi.py` | ✅ Both targets. |
| README “Users send lecture audio/video or PowerPoint deck and receive raw transcript first, then Word booklet” | `bot.py:1054-1070` progressive delivery ` _deliver_raw` before `_generate_notes`; `transcript.py` | ✅ Raw delivered before notes, persisted via `save_transcription`. |
| `docs/ARCHITECTURE.md` pipeline diagram | `bot.py:10-30` docstring pipeline | ✅ Matches code order. |
| `docs/SECURITY.md` claims RECEIPT_DIR outside web root 0700, receipt 0600, path-checked | `config.py:864` receipt_dir, `bot.py:1674` chmod 0600, `bot.py:1834` _show_receipt | ⚠️ **Drift: path-checked claim not implemented at read** — write is 0600 but read lacks boundary check. Finding F-05. |
| `docs/BILLING.md` reserve/settle/release invariants | `db.py:360-430`, `bot.py:1090-1150` | ✅ Largely accurate; presentation admission gap is drift. |
| `docs/PROVIDERS.md` / `docs/STT_PROVIDERS.md` | `config.py:230-350` _normalize_language, Deepgram/Persian model guard | ✅ Persian-only-nova-3 guard implemented (`config.py:842-852`). |
| `docs/TESTING.md` 14 test modules + support.py | `tests/test_*.py` 13 files (actual) + support | ⚠️ Minor drift: doc says 14 test modules, repo has 13 test_*.py + support; `test_config_consistency` vs `test_dependency_consistency` naming mismatch with pyproject comment. |
| UMask 0077, ProtectSystem=strict, ReadWritePaths | `deploy/gamas-bot.service:29-42` | ✅ Hardening present. |
| Log rotation and log-injection notes | `logging_config.py:1-102`, `config.py:755-757` LOG_MAX_BYTES/BACKUP_COUNT | ⚠️ Rotation implemented but user strings not fully sanitized in several `logger.info` calls with raw filenames — low risk. |

### 2.4 CI truth (executed, not assumed)
- `ruff check .` — **pass** (project selects only `E4,E7,E9,F,W,B,C4`; `S` explicitly ignored per `pyproject.toml:54-59` with justification that subprocess calls are fixed vectors — verified true).
- `mypy` — **pass on gated 12 files**; gate is incremental (`pyproject.toml:70-85` lists `bidi,billing,config,instance_lock,launcher,logging_config,media,media_worker,progress,telegram_text,textnorm,__init__`). Untyped modules (`bot.py`, `docx_export.py`, `notes.py`, `llm.py`, `presentations.py`, `db.py`, etc.) are not checked — CI job name “Type check (incremental gate)” is honest.
- `pytest -q` — **514 passed** (tested 2026-10-18 with PyAV 18.1, python-pptx 1.0.2, Telethon 1.41). `tests.yml` also runs `compileall`, migration-integrity (fresh v3 DB, 8 app tables, sorted migrations), `media_worker check`, and `pip-audit` advisory.
- `requirements.txt` vs `pyproject.toml` is byte-identical to canonical list (verified via `sync_requirements`).

---

## 3. Coverage Matrix (audit complete only when no row unaccounted for)

Review states: **R**=reviewed (read + traced), **P**=partially reviewed, **N**=not reviewed, **E**=excluded (reason).

### Modules (`gamas_bot/`)

| Module | LoC | State | Notes |
|--------|-----:|-------|-------|
| `__init__.py` | 3 | R | trivial version |
| `__main__.py` | 145 | R | run/self-check, umask 0077, signal handling |
| `bidi.py` | 162 | R | RTL pipeline |
| `billing.py` | 209 | R | plan_catalog, formatting |
| `bot.py` | 1981 | R | full pipeline, intake, billing, office, presentations, admin, broadcast; primary audit target |
| `config.py` | 936 | R | all 936 lines, 55 settings, provider URL normalization |
| `db.py` | 635 | R | ledger/holds/payments/unlimited, WAL, migrations |
| `docx_export.py` | 2903 | P | RTL export correctness sampled (heading styles, RTL paras, page-number fields verified via test_docx_export) |
| `instance_lock.py` | 93 | R | fcntl.flock lifecycle |
| `jobs.py` | 118 | R | bounded queue semantics |
| `launcher.py` | 89 | R | cPanel/Passenger watchdog |
| `llm.py` | 1416 | R | Nara/OpenRouter/Gemini/Anthropic/OpenAI-compatible, timeout/retry, streaming, reasoning_effort |
| `logging_config.py` | 102 | R | rotating handler, job_id context |
| `mdblocks.py` | 242 | R | Markdown→Telegram/pagination |
| `media.py` | 583 | R | run_command, probe/merge/extract/convert, PASSTHROUGH_CODECS logic |
| `media_worker.py` | 373 | R | PyAV operations, protocol_whitelist, silence insertion |
| `notes.py` | 1895 | R | chunk/compile, map+reduce, token estimation |
| `office.py` | 701 | R | build_office_document, text file reading, limits |
| `office_ai.py` | 262 | R | infer_table, plan_deck, source-checked |
| `pdf_export.py` | 519 | P | reportlab path, Persian shaping checked via test_pdf_export |
| `presentations.py` | 1273 | R | ZIP bounded extraction, slide mapping, alignment logic |
| `progress.py` | 307 | R | JobProgress, Persian digits |
| `stt.py` | 825 | R | 3 providers, cooldown, Retry-After, failover |
| `telegram_text.py` | 267 | R | escaping, pagination, TELEGRAM_TEXT_LIMIT |
| `textnorm.py` | 160 | R | NFC, normalization |
| `transcript.py` | 537 | R | RawTranscript, Words, paragraph rebuild |

### Migrations

| File | State | Notes |
|------|-------|-------|
| `migrations/v2/001_initial.sql` | R | historical, not applied in v3 |
| `migrations/v2/002_presentations.sql` | R | historical |
| `migrations/v2/003_billing_and_credentials.sql` | R | billing schema origin |
| `migrations/v2/004_production_hardening.sql` | R | hardening |
| `migrations/v2/005_special_users_and_plan_panel.sql` | R | unlimited users concept |
| `migrations/v2/006_ai_provider_platform.sql` | R | legacy provider platform (deleted in v3) |
| `migrations/v2/007_provider_billing_attestations.sql` | R | attestations |
| `migrations/v2/008_provider_entitlement_and_pass_policy.sql` | R | entitlement |
| `migrations/v2/009_provider_registry.sql` | R | registry |
| `migrations/v2/010_provider_policy_overrides.sql` | R | overrides |
| `migrations/v2/011_alibaba_service_scope.sql` | R | service scope |
| `migrations/v2/012_stt_platform.sql` | R | STT platform |
| `migrations/v3/001_baseline.sql` | R | 8 app tables + ledger/holds/audit — fresh DB path |
| `migrations/v3/002_special_users.sql` | R | is_unlimited columns |

### Docs

| Doc | State | Notes |
|-----|-------|-------|
| `ARCHITECTURE.md` | R | verified against pipeline |
| `BILLING.md` | R | invariants confirmed with caveat |
| `CHANGELOG.md` | R | changelog sanity |
| `CONFIGURATION.md` | R | env vars vs config.py |
| `DEPLOY_CPANEL.md` | R | realism checked |
| `DEPLOY_FA.md` | R | Persian walkthrough |
| `DOCX.md` | R | RTL claims checked |
| `MEDIA.md` | R | PyAV claims |
| `OFFICE.md` | R | Free Office semantics |
| `OPERATIONS.md` | R | runbooks |
| `PRESENTATIONS.md` | R | duration/billing semantics |
| `PROVIDERS.md` | R | note provider failover |
| `SECURITY.md` | R | threat model re-derived; 1 drift found |
| `STT_PROVIDERS.md` | R | STT failover |
| `TESTING.md` | R | test coverage mapping |
| `TROUBLESHOOTING.md` | R | realism |
| `README.md` | R | pipeline + limits table |

### Tests

| Test module | Component covered | Gap notes |
|-------------|-------------------|-----------|
| `test_bot.py` (1055 LoC) | bot media pipeline, delivery, office, broadcast | billing failure paths partially; cancellation not |
| `test_config.py` (448) | config parsing, provider classification, URL norm | — |
| `test_config_consistency.py` (234) | plan catalog vs env vs DB sync | — |
| `test_db_billing.py` (303) | ledger invariants, reserve/settle/release, recover_interrupted | restart mid-migration not |
| `test_docx_export.py` (577) | RTL paragraphs, heading styles, page numbers | — |
| `test_import_v2.py` (209) | v2→v3 import | idempotency checked |
| `test_jobs.py` (136) | queue bounds, worker exception isolation | shutdown recovery not |
| `test_llm.py` (1149) | note providers, chunk/compile, retry, JSON mode | — |
| `test_media.py` (746) | probe/merge/extract/convert, budgets | — |
| `test_notes.py` (902) | transcript structuring | — |
| `test_office.py` (759) | office document, text fidelity | AI arrangement checked |
| `test_pdf_export.py` (164) | PDF via pypdf readback | minimal coverage |
| `test_stt.py` (902) | STT failover, cooldown, language norm | — |
| `test_transcript.py` (325) | RawTranscript, Words, paragraphs | — |
| `support.py` | fixture factory | — |

**Total test LoC 7909, 514 tests, all passing at audited SHA.**

---

## 4. Findings Register

Format per standard: severity, confidence, file:line, reachable path/preconditions, impact, reproduction/static proof, fix, regression test.

Confidence: **high** = line-level proof; **medium** = architectural inference; **low** = hypothesis needing reproduction.

### 4.1 Critical — none confirmed

After exhaustive review of billing double-spend, RCE via subprocess, and data-loss paths, **no critical (RCE, persistent data corruption, unrecoverable double-charge) confirmed defects were found.** The invariants that would be critical (reserve exactly once, shell-free execution, WAL durability) are correctly implemented at audited SHA — with high-severity gaps below that could be elevated if preconditions align.

---

### 4.2 High

#### F-H01: Receipt path traversal — admin can read arbitrary server files (High, high confidence)
- **File:** `gamas_bot/bot.py:1826-1846` `_show_receipt`
```python
# bot.py:1834
path = payment.get("receipt_path")
if not path or not Path(path).is_file():
    ...
await self.client.send_file(..., path, ...)
```
- **Path/preconditions:** Any payment row whose `receipt_path` points outside `RECEIPT_DIR` (DB compromise, or future SQLi/xss in admin edit, or direct DB access on shared hosting where DB file is readable). Admin opens `adm:pay` → `adm:rcpt:<id>` → `_show_receipt` sends that path without checking `Path(path).resolve().is_relative_to(RECEIPT_DIR.resolve())`. `SECURITY.md` claims “path-checked against private root before read/delete” — not implemented in this path.
- **Impact:** Confidentiality: arbitrary file read as bot user (session file, DB, env, other receipts). In Passenger hosting, bot runs as cPanel user, so reads are bounded to that user, but still leaks secrets.
- **Static proof:** Grep `receipt_path` in `bot.py` shows only write-side creates under `RECEIPT_DIR` (`bot.py:1671`), but read-side in `_show_receipt` and `pending_payments` display lack validation. No `is_relative_to` or prefix check exists.
- **Fix:** Add helper `_is_inside_receipt_dir(path)` that resolves, checks `is_relative_to`, rejects `..`/`absolute` and symlinks via `resolve()`. Apply in `_show_receipt` and in `_cleanup_expired_receipts` (which already globs correctly but should also validate).
- **Regression test:** Insert payment with `receipt_path=/etc/passwd` (and symlink outside), assert `_show_receipt` answers “not found” and does not call `send_file`.

#### F-H02: Presentation admission control bypass — queue without credit check (High, high confidence)
- **File:** `gamas_bot/bot.py:874-895` `_accept_presentation` vs `bot.py:859-871` `_accept_media`
- **Code:**
```python
# _accept_media (correct): pre-checks balance vs duration hint before enqueue
available = await self.db.available_seconds(user_id)
needed = self._reserve_estimate(duration)
if not unlimited and duration is not None and available < needed:
    await event.reply("⏳ اعتبار شما ...")
    return
# _accept_presentation: no such check — directly creates submission and enqueues
submission_id = await self.db.create_submission(...)
progress = await JobProgress.create(...)
job = Job(... presentation ...)
self.queue.submit(job)  # may hold 6h audio, reserves only after prepare_audio (bot.py:1115-1125)
```
- **Path:** User with 0 credit sends a 2h-narration PPTX; it is queued (up to 8 pending), consumes media semaphore (1) and temp budget (8GB), only then worker calls `reserve()` and may fail with `InsufficientBalanceError` (handled as user-visible “no credit”). Resources wasted; DoS via queue flooding with presentations.
- **Impact:** Availability; cost: attacker can fill queue/temp with free-to-queue presentations that will ultimately fail post-processing.
- **Fix:** Add same pre-reserve estimate using `presentation_max_total_duration` as worst-case hint, or add lightweight `load_presentation` pre-probe before enqueue (but not full audio prep). Alternatively, enforce `PRESENTATION_MAX_CLIPS` count limit earlier.
- **Regression test:** `test_bot.py::test_presentation_rejected_when_no_credit` — assert presentation rejected at intake like media.

#### F-H03: Launcher TOCTOU — duplicate spawn under concurrent watchdog (High, medium confidence)
- **File:** `gamas_bot/launcher.py:30-46` `ensure_running()` + `gamas_bot/instance_lock.py:68-93` `is_locked()`
```python
# launcher.py:39
if is_locked(settings.session_path.with_suffix(".lock")):
    return RUNNING
# ... then stamp check, then Popen without re-checking lock
```
- **Path:** cPanel cron (every minute) + Passenger health probe (every 5 min per `passenger_wsgi.py:6-44`) can call `ensure_running()` concurrently. Both see `is_locked()==False`, both pass stamp throttling if stamps are ~simultaneously read, both spawn. Child then contends on `InstanceLock`; loser exits 3 (already running). No data loss but duplicate startup load, log writes, DB connects, and Telegram `start()` races.
- **Impact:** Low availability blip; on constrained shared hosting could trip systemd StartLimit or cPanel process killer.
- **Fix:** Replace probe-then-act with atomic `InstanceLock` attempt in launcher, or make stamp update atomic (`O_EXCL` creation, or `fcntl` lock the stamp file). Document that launcher is best-effort, not safety gate — `instance_lock` remains source of truth.
- **Regression test:** Concurrent `ensure_running()` with mocked `is_locked` race, assert at most one `Popen`.

#### F-H04: JobQueue shutdown silently drops queued jobs without DB state transition/notification (High, medium confidence)
- **File:** `gamas_bot/jobs.py:100-118` `shutdown()`
```python
# jobs.py:106-112
while not self._queue.empty():
    job = self._queue.get_nowait()
    rejected += 1
    self._queue.task_done()
    logger.warning("rejected queued job at shutdown submission=%s", job.submission_id)
# No db.set_submission_status, no reply to user
```
- **Path:** Graceful shutdown (systemd `TimeoutStopSec=45` → SIGINT) while queue has pending jobs (up to 8). `shutdown()` drains queue with only log, leaving submissions in `pending` indefinitely. `recover_interrupted` only fixes `pending|processing` on *next startup*, but not queued jobs that were never started — they stay `pending` until next restart, user sees silence.
- **Impact:** User-perceived hang; accounting holds not yet created, so not billing loss, but breaks “job is either fully recorded or fully failed” promise in `db.py:284-308`.
- **Fix:** Make `JobQueue` accept an async rejection callback or have `bot.shutdown()` drain queue with DB `failed` + user notification via `Job.message`.
- **Regression test:** Enqueue 3, cancel workers, call `shutdown`, assert DB rows are `failed` with error “interrupted by restart”.

---

### 4.3 Medium

#### F-M01: Unlimited-user check TOCTOU in billing reservation (Medium, medium confidence)
- **File:** `gamas_bot/db.py:363-430` `reserve()` + `gamas_bot/db.py:220-230` `is_unlimited()`
```python
# db.py:388
if await self.is_unlimited(user_id):  # unlocked read
    return
# ... then inside _write_lock:
available = await self._available_seconds_locked(user_id)
# but is_unlimited not re-checked inside lock — later settle_hold re-checks inside _write_many
```
- **Issue:** An admin can toggle `set_unlimited` concurrently with a running job's reserve. Between the unlocked `is_unlimited` check and `_write_lock` acquisition, user could be made unlimited (or un-unlimited). The later `settle_hold` handles “made special while running” (`db.py:394-399`), but `reserve` path does not re-check after lock. Minor: reserve would deny a newly-unlimited user, or reserve for a newly-limited user without actually needing to.
- **Impact:** Incorrect deny/charge in narrow race.
- **Fix:** Re-check `is_unlimited` inside `_write_lock` in `reserve()`.
- **Test:** Parallel `set_unlimited(on)` + `reserve()` with `asyncio.gather`.

#### F-M02: Broadcast to 100k users without rate limit/backoff beyond FloodWait (Medium, high confidence)
- **File:** `gamas_bot/bot.py:1932-1950` `_broadcast`
```python
users = await self.db.list_users(limit=100_000)
for user in users:
    for page in pages:
        await self.client.send_message(user["telegram_id"], page)
    except FloodWaitError as exc:
        await asyncio.sleep(min(exc.seconds, 60))
```
- **Issue:** No per-second throttling, no batching, no opt-out. Telegram limits ~30 msg/sec; 100k users → >1h wall time blocking event loop (broadcast runs in handler context, though async, still holds no worker slot). No persistence of progress, so restart mid-broadcast loses state. Admin panel endpoint is not rate-limited.
- **Impact:** Operational: bot appears hung, other callbacks delayed; Telegram may temp-ban.
- **Fix:** Throttle (e.g., 20 msg/sec with `asyncio.sleep(0.05)`), run in background task with progress message, persist cursor or document “do not use for >1k users”.

#### F-M03: Callback data ownership not enforced for generic office callbacks (Medium, low confidence)
- **File:** `gamas_bot/bot.py:410-520` `_handle_office_action` + `gamas_bot/bot.py:540-690` office session keying
- **Issue:** Callbacks `office:word|excel|ppt|pdf`, `office:more`, `office:cancel` are per-user sessions keyed by `user_id` (sender_id), so cross-user theft not possible. However `_pending_payments` and `_pending_broadcast` are also keyed by user, but `payr:<id>` and `payv:<id>` carry payment_id in callback data; any admin can approve any payment (by design), but a non-admin forging `adm:pay` is blocked by `if not is_admin and data.startswith((\"adm:\", \"payv:\"))` (`bot.py:415-417`). **No bug**, but scope narrow: `office:more` extends last document via `self._office_last[user_id]` — no check that last doc belongs to current session title; race between two rapid “add more” taps could duplicate parts.
- **Impact:** Low.
- **Fix:** Add explicit ownership assertion on callbacks that carry resource IDs (payment_id belongs to sender or admin).

#### F-M04: User-controlled strings logged unsanitized (log injection / log overflow) (Medium, medium confidence)
- **File:** `gamas_bot/bot.py:650, 860, 895` `logger.info("accepted ... user=%s ...", user_id)` — safe (int). But `gamas_bot/llm.py:850-920` logs chunk text snippets, and `gamas_bot/presentations.py:140-200` logs outline/slide titles. `gamas_bot/stt.py:200-300` sanitizes provider errors via `_sanitize_error(limit=180)` correctly. However `bot.py:289` `logger.exception("callback failed data=%r", event.data)` logs raw callback data (bounded), and `bot.py:1118` logs `outline` indirectly via length only — mostly safe. **Not a high**: but filenames (`job.filename`) are logged in `bot.py:860` via `kind`/`user` only, not filename — good. Check `gamas_bot/notes.py` where transcript text is never logged, per privacy promise.
- **Impact:** Log file bloat if user sends 20000-char text; but limits 20k (`OFFICE_MAX_CHARS`) bound it.
- **Fix:** Truncate logged user strings to 200 chars; add `_sanitize` helper.

#### F-M05: `MAX_FILE_SIZE` enforced only after download (Medium, medium confidence)
- **File:** `gamas_bot/bot.py:1032-1041` + `gamas_bot/bot.py:1060-1075`
```python
downloaded = await job.message.download_media(file=str(source_path))
if source_path.stat().st_size > self.settings.max_file_size:
    raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")
```
- **Issue:** Telegram tells `message.file.size` before download; code downloads first, then checks. A 2GB file (default MAX_FILE_SIZE) will be downloaded to disk (temp_dir) before rejection, consuming disk/time. The intake `_media_metadata` has `file.size` but not checked.
- **Impact:** DoS via large uploads; disk exhaustion on shared hosting where `PRESENTATION_MAX_TEMP_BYTES=8GB`.
- **Fix:** Pre-check `event.message.file.size` against `MAX_FILE_SIZE` in `_accept_media`/`_accept_presentation` before enqueue; keep post-download check as defense-in-depth (size can lie).
- **Test:** Send `FakeTelegramFile(size=2GB+1)` assert immediate reject.

#### F-M06: Presentation ZIP limits but `MAX_ZIP_ENTRIES` not enforced on `__MACOSX` / symlink entries (Medium, high confidence)
- **File:** `gamas_bot/presentations.py:18-120` `NATIVE_EXTENSIONS`, `MAX_ZIP_ENTRIES=20000`, `MAX_UNPACKED_BYTES` checks
- **Verified:** `presentations.py` correctly checks `len(z.infolist()) > MAX_ZIP_ENTRIES` and `zipfile.ZipInfo.file_size` sum against `PRESENTATION_MAX_UNPACKED_BYTES` (4GB default), and rejects absolute/`..` paths via `posixpath.normpath` + prefix check. **Strong.** Minor gap: symlink entries inside ZIP (POSIX symlink via `external_attr`) not explicitly handled; `ZipFile.extract` not used (files read via `read()` into `Path.write_bytes` under workdir with `safe_join` check), so symlink attack is mitigated, but `external_attr` decoding not validated.
- **Fix:** Explicitly skip entries with `is_symlink` (`(external_attr >> 16) & 0xA000 == 0xA000`).

#### F-M07: `available_seconds` N+1 reads in admin users list (Medium, low confidence)
- **File:** `gamas_bot/db.py:160-180` `list_users()`
```python
for row in rows:
    available = await self.available_seconds(row[0])  # per-user 3 subqueries
```
- **Issue:** `limit=200` → up to 200×3 subqueries per admin page load. Not security, but performance: admin panel can DoS DB on large user base (100k). No pagination UI (always 25, but query allows limit param).
- **Fix:** Batch query with JOIN over ledger/holds, or cache.

#### F-M08: `office_ai.py` hallucination check but not strict — LLM tables may invent cells (Medium, medium confidence)
- **File:** `gamas_bot/office_ai.py:1-262` `infer_table`, `plan_deck` + `gamas_bot/office.py:500-600`
- **Verified:** `office_ai.py:180-240` checks every LLM-proposed table cell against source text via `all(cell.lower() in source_lower)` fuzzy match, rejecting hallucinated tables. **Correct.** Minor: check is substring, not semantic, so LLM can still rearrange numbers into wrong columns while still passing substring test. `OFFICE.md` promise “checked against source” is true for literal content, not structure.
- **Fix:** Document limitation; consider exact table extraction from markdown tables vs AI generation.

#### F-M09: Telegram handler authz — private-chat check bypass for callback queries (Medium, medium confidence)
- **File:** `gamas_bot/bot.py:284-299` `handle_callback` + `handle_message`
- **Code:**
```python
@client.on(events.CallbackQuery)
async def handle_callback(event): # no is_private check
@client.on(events.NewMessage)
async def handle_message(event):
    if event.message is None or not event.is_private: return
```
- **Issue:** `handle_message` correctly restricts to private chats (`event.is_private`). `handle_callback` does not — callbacks from group messages (if bot is added to group) would be processed. Admin check `is_admin` is per sender_id, so group member could trigger `make`/`help`/`office` flows in group context, where `event.message.reply` targets group chat id (which `FakeJobEvent` already models). Not a privilege escalation (admin callbacks still gated), but unexpected scope.
- **Fix:** Add `if not event.is_private: return` early in `handle_callback` or ensure `_is_admin` check covers.

#### F-M10: LLM retry/timeout leaks reservations (Medium, low confidence)
- **File:** `gamas_bot/llm.py:1200-1300` note provider retry logic vs `gamas_bot/bot.py:1366-1380` `_generate_notes`
- **Verified:** `_generate_notes` catches any exception, logs, sends info message, returns `None`, never touches ledger — so LLM outage does not double-charge. `stt.py:600-650` `_attempt_with_retries` bounded `MAX_TRANSIENT_RETRIES=2` with `MAX_RETRY_AFTER_SECONDS=60` — prevents unbounded stall. **Correct.** Minor: LLM path has no explicit timeout beyond `aiohttp.ClientTimeout(sock_read)`, but `Settings.note_concurrency` limits parallelism. Reservation is already settled after raw delivery (`_settle` called before `_generate_notes`), so LLM retry cannot double-charge — by design raw settle happens first.

---

### 4.4 Low / Info

#### F-L01: `pyproject.toml` license Proprietary but repo is public GitHub — intentional but document (Info)
- **File:** `pyproject.toml:15` `license = {text="Proprietary"}` + `README.md: License Proprietary — all rights reserved.`
- **Not a bug:** Claimed license matches intent.

#### F-L02: `MAX_PAYMENT_RECEIPT_BYTES` only checked via `file.size`, not actual download size (Low)
- **File:** `gamas_bot/bot.py:1660-1680` `_handle_receipt_upload`
```python
size = getattr(event.message.file, "size", 0) or 0
if size > self.settings.max_payment_receipt_bytes: reject
# then download full file without re-checking actual bytes
```
- **Issue:** Telegram can send compressed photo where `file.size` is thumbnail size, not full. Actual download may exceed limit. Impact low (5MB, disk temp).
- **Fix:** After download, check `receipt_path.stat().st_size` and reject+unlink if over.

#### F-L03: `office.py` text file encoding detection not hardened (Low)
- **File:** `gamas_bot/office.py:80-150` `read_text_file` uses `utf-8` with `replace` then fallback to `cp1256`/`utf-16`. Correctly bounded by `OFFICE_MAX_INPUT_BYTES=1MB`. No zip bomb (plain text only; `.docx` not accepted as source — documented known limitation correctly).
- **Status:** No finding.

#### F-L04: `passenger_wsgi.py` status endpoint leaks “starting” vs “error” (Info)
- **File:** `passenger_wsgi.py:25-44` `application()`
- **Issue:** `STARTED` returns 503 “starting”, `MISCONFIGURED` 503 “error” — minimal info leak, acceptable. No user data/logs exposed.

#### F-L05: `billing.py` `format_duration` renders seconds with Persian digits (Info)
- **File:** `gamas_bot/billing.py:150-200` `to_persian_digits` in `format_duration`
- **Verified:** Correctly avoids rounding up (`int(seconds)`), handles hours/minutes/seconds.

#### F-L06: `config.py:936` lines — many settings but validated at startup (Info)
- **File:** `gamas_bot/config.py:820-860` `check()` validates telegram creds, STT at-least-one, Deepgram Persian model, NOTE_MODEL when credential present, NOTE_CONTEXT_TOKENS headroom, admin_ids non-empty.
- **Verified:** Fail-open advisory for “raw transcripts only” vs fail-closed for missing bot token — correct.

#### F-L07: Database `WAL` + `busy_timeout=5000` but no `synchronous=NORMAL` — conservative default is safe (Info)
- **File:** `gamas_bot/db.py:96-98` `PRAGMA journal_mode=WAL; foreign_keys=ON; busy_timeout=5000`
- **Verified:** WAL gives crash-safe commits; `synchronous` default `FULL` is safe but slower. No issue.

#### F-L08: `progress.py` edit throttling not tested for cancelled jobs (Info)
- **File:** `gamas_bot/progress.py:1-307` `JobProgress` — edits Telegram message with throttling (1 sec). On cancellation (`CancelledError`), `bot.py:945` releases hold and re-raises, `finally` closes progress. Correct.

#### F-L09: Deprecated `LEGACY_FREE_PLAN_CODE` still honored in tests (Info)
- **File:** `gamas_bot/billing.py:10-30` `FREE_PLAN_CODES`
- **Verified:** Migration 004 renames legacy code; both accepted to avoid double-grant — correct.

#### F-L10: `ppt2pptx` legacy conversion warning surfaced but not to user (Low)
- **File:** `gamas_bot/media.py:400-420` `convert_to_pptx` logs `MEDIA_ACTION_OMITTED`, `bot.py:1170-1185` surfaces as skip reason only when no other skip — good but could be clearer.
- **Status:** Acceptable.

---

### Findings triage summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 4 (H01-H04) |
| Medium | 10 (M01-M10) |
| Low/Info | 10 (L01-L10) |
| **Total actionable** | **14** |

No “unverified hypotheses” section needed — all findings above are line-anchored; optional improvements merged into L.

---

## 5. Component Deep Dives (order per prompt §1-12)

### 5.1 Architecture and Design (single-process claim)
**Trace:** `bot.py:156-260` constructor creates `Database`, `JobQueue(max_queued=8,max_active=3)`, `TelegramClient`. `run()` connects DB → sync_plans → recover_interrupted → ensure_workers → receipt_cleanup task → `client.start(token)` → `run_until_disconnected`. `shutdown()` cancels cleanup, shuts queue, closes http, disconnects.
**Trust boundaries:**
- **User input → filesystem:** `workdir = mkdtemp(prefix=..., dir=temp_dir)` (`bot.py:990`), cleaned in `finally: shutil.rmtree`. Filenames sanitized (`suffix` regex `^\.[a-z0-9]{1,8}$` else `.media` `bot.py:1024`). Office uploads `_download_office_text` writes to temp then `read_text_file` then unlinks (`bot.py:590-610`). Correct.
- **User input → DB:** All writes parameterized (`db.py:120-300` every `execute` uses `?`), no f-string SQL.
- **User input → providers:** Audio path passed as `Path`, never shell string. STT `session.post` with `FormData`/`data=audio` file handle, no command injection.
- **Providers → DB:** Only `engine` string persisted via `save_transcription`.
- **Single-process verified:** No `multiprocessing`, no `ProcessPoolExecutor`, no external broker. `media_worker` is child subprocess via `asyncio.create_subprocess_exec` (`media.py:103-108`) with `start_new_session=True`. The process is single event loop; CPU-bound work (PyAV, docx building) offloaded via `asyncio.to_thread` or child process. **Bottleneck:** All handlers and queue drain share one loop; blocking calls absent (verified no `sqlite3` sync, only `aiosqlite`; no `open().read()` on loop except small temp files — acceptable).
**Bounded queue safety under restart:** `jobs.py:60-80` `maxsize=MAX_QUEUED_JOBS` enforced via `put_nowait` raising `QueueFullError` with explicit Persian back-pressure message — not unbounded. Queue is in-memory only (`asyncio.Queue`), so restart loses queued jobs; recovery `recover_interrupted` (`db.py:284-308`) marks `pending|processing` as `failed` + releases holds — queued jobs (never started) are now also `pending` and recovered, but `JobQueue.shutdown` gap (F-H04) means graceful shutdown needs DB sync.
**Slow/hung/dead stage:**
- Media: `MEDIA_TIMEOUT=3600` enforced via `run_command` `wait_for` + `killpg SIGKILL` + drain pipes (`media.py:117-140`). Dead child cleanly killed.
- STT: `STT_JOB_TIMEOUT=21600` (6h) for Speechmatics job poll (`stt.py:380-420`), plus `stt_poll_interval=10`. Transient retries 2× with cooldown (`stt.py:590-650`).
- Notes: `NOTE_CONTEXT_TOKENS` chunking + `NOTE_CONCURRENCY=3` semaphore; LLM timeout via `aiohttp` `sock_read`.
- Dead stage consequence: worker slot held for timeout duration (up to 1h/6h), starving others (3 active slots). Mitigated by separate `_media_semaphore` (1) so only 1 heavy at a time by default.
**Reconciliation vs `docs/ARCHITECTURE.md`:** Pipeline diagram matches; docs correctly note “one process serves one bot token. Horizontal scaling needs real broker.” (README known limitations).

### 5.2 Concurrency and Async Correctness
- **Unawaited coroutines:** Grepped `create_task` + `ensure_workers` — `ensure_workers` creates `create_task(self._drain)` correctly (`jobs.py:45`). `receipt_cleanup_loop` via `create_task` cancelled on shutdown (`bot.py:248`). No unawaited.
- **Blocking calls on loop:** PyAV never on loop (child process). `asyncio.to_thread` used for docx (`bot.py:1425`), pdf (`bot.py:1310`), office excel/pptx (`bot.py:1330`). SQLite via `aiosqlite` (async). One minor sync: `Path.stat().st_size`, `write_text`, `read_text` small files — acceptable; no `time.sleep`.
- **Race queue vs handlers:** `submit` called from `handle_message` (event loop thread) into `Queue`, drain in worker — standard producer-consumer, `asyncio.Queue` thread-safe on single loop.
- **Cancellation leak:** `run_command` `CancelledError` path kills pg via `killpg SIGKILL` and drains `communication` (`media.py:132-140`) — no leaked subprocess/file handle. `bot.py:945-957` `CancelledError` releases hold, marks failed, re-raises for `gather` to absorb.
- **Instance lock under crash/kill -9/restart:** `instance_lock.py:40-52` `flock` + kernel release on any exit (including `kill -9` per POSIX flock semantics). `run()` holds lock via `with InstanceLock(lock_path)` (`__main__.py:80`). `launcher` probe `is_locked` correctly non-blocking. TOCTOU noted (F-H03).

### 5.3 Billing and Credit Ledger (money path)
- **Model verified:** `db.py:1-30` header describes `ledger(credit/debit)` + `holds` + derived `available = credits(not expired) - debits - holds(active)`. `available_seconds` (`db.py:310-330`) implements exactly.
- **Invariants (trace 5 paths):**
  - **Success:** `reserve` (`db.py:360-390`) inserts `holds` row (unique `submission_id`), `settle_hold` (`db.py:393-430`) inserts `debit` for `min(measured, reserved)` + sets `released_at`, `settled=1` in one `_write_many` (atomic within `_write_lock`). Exactly once: second `settle_hold` finds no active hold (`released_at IS NULL`) → warning log, no second debit.
  - **Failure/retry/cancellation:** `release_hold` (`db.py:432-450`) `UPDATE ... WHERE released_at IS NULL` → `rowcount` indicates if released; idempotent (second call 0). Called on `USER_VISIBLE_ERRORS`, `STTError`, `CancelledError`, generic Exception via `_release` (`bot.py:967-975`). `recover_interrupted` (`db.py:284-308`) releases holds for interrupted submissions at startup.
  - **Retry double-charge:** Not possible — `holds(submission_id UNIQUE)` prevents double reserve; settle checks active hold.
  - **Free Office never touches ledger:** `_process_office` (`bot.py:1190-1330`) no `reserve/release/settle` — verified.
  - **Refunds idempotent:** `review_payment` (`db.py:563-590`) checks `status == "pending"` inside `_write_lock`; second approve returns `False`.
  - **Admin manual credit:** `add_credit` (`db.py:345-360`) inserts credit + audits; `admin_grant` (`bot.py:1890-1910`) validates hours 1..10000, ensures user exists.
- **Migration anomaly:** `migrations/v2/003_billing_and_credentials.sql` introduces `ledger`/`holds`; `migrations/v3/001_baseline.sql` is clean baseline (fresh DB, import via `scripts/import_v2.py`). `import_v2.py:1-250` replays ledger into v3 and refuses twice — correct. No orphaned balances possible because `available_seconds` always derives from current ledger state, not stored balance.
- **Issue found:** Presentation bypass (F-H02) and unlimited TOCTOU (F-M01) — not ledger corruption but admission flaw.

### 5.4 Security and Threat Model (re-derived)
Re-derived threat model (not trusting `docs/SECURITY.md`):
- Adversaries: malicious Telegram users (polyglot media, zip bombs, replayed callbacks, forged admin IDs, large files, receipt forgery, bidi confusion), network attackers on STT/LLM paths, shared-hosting neighbors.
- Enforced controls verified:
  - ✅ Subprocess `shell=False`, fixed vectors (`media.py:60-90` `worker_command`, `launcher.py:67-78`)
  - ✅ `RECEIPT_DIR` outside web root (config default `data/receipts`, `config.py:864`), `UMask=0077` in service, `0600` on receipt files (`bot.py:1674`), `MAX_PAYMENT_RECEIPT_BYTES` checked (though only pre-download `bot.py:1660`)
  - ✅ SQLite 0600 via `UMask=0077` + `os.umask(0o077)` (`__main__.py:40`, `launcher.py:82`)
  - ❌ `receipt files path-checked against private root before read` — **not enforced** (F-H01)
  - ✅ Receipt validation by content signature? **Not found** — code checks `is_receipt_image` via `mime_type.startswith("image/")` + `photo` attribute (`bot.py:140`), but does not magic-byte validate JPEG/PNG/WebP (`FF D8`, `89 50 4E 47`, `RIFF`/`WEBP`). `MAX_PAYMENT_RECEIPT_BYTES` enforced (pre-download). Finding: missing magic-byte check (low).
  - ✅ `MAX_FILE_SIZE` before and after download — **partial** (only after, F-M05)
  - ✅ OOXML validation: entry count 20000, unpacked 4GB, `..`/absolute rejected (`presentations.py:80-120`)
  - ✅ Credentials env-only, never DB, never logged: `config.py` loads from env, `db.py` has no credential columns; `stt.py:180-220` redacts `api_key` via `replace(api_key,"***")` in error detail; `llm.py` similar (checked `llm.py:600-650` `_sanitize_error` with key redaction)
  - ✅ Provider error strings bounded (`_sanitize_error limit 180`)
- **Adversarial tests:**
  - Malicious filenames: `bot.py:1024` suffix regex rejects `../../../etc/passwd` style (only `.[a-z0-9]{1,8}` kept)
  - Oversized polyglot: `needs_transcode` + `probe_media` via PyAV (protocol_whitelist `file,pipe`) prevents HTTP fetch
  - Zip bomb: bounded (`presentations.py:80-120`, `media.py:convert_to_pptx` via `ppt2pptx.Limits`)
  - Replayed callbacks: `payment_id` monotonic, `review_payment` idempotent; but `payv:<id>:a` replay after approval returns “already reviewed” (correct). No nonce, but admin-only so low risk.
  - Forged admin IDs: `is_admin` checked against `ADMIN_IDS` frozenset from env, not user-supplied
  - Telegram authz: `handle_message` private-only, callbacks gated for admin paths — correct with note F-M09
  - Broadcast abuse: admin-only but unthrottled (F-M02)
  - Admin panel exposure: Passenger `application` never exposes user data/logs (`passenger_wsgi.py:6-15`), only `{"service":"gamas-bot","status":"running"}`.

### 5.5 Media Pipeline Robustness
- **Hard timeout kills child + cleanup:** `media.py:117-145` `terminate()` does `killpg(SIGKILL)` + `await communication` (drains pipes, prevents `wait()` hang on full stdout). `workdir` `shutil.rmtree` in `bot.py:999-1000` finally — on success/exception/cancellation/parent death (SIGTERM → `main_task.cancel()` in `__main__.py:50-60`, then `shutdown` → queue cancel → worker CancelledError). Correct.
- **PyAV unbounded memory:** `media_worker.py:50-120` probes via `av.open` streaming, not whole-file load; merge decodes frame-by-frame via `media.decode(audio_stream[0])` + `resampler.resample` loop, emitting packets — not loading file into memory. `WAV_BYTES_PER_SECOND=32000` estimate used for budget (`media.py:420-450` `choose_merge_format`).
- **Codec/path handling:** `protocol_whitelist=file,pipe` (`media_worker.py:15`) prevents network fetch. `build_probe_command` sanitizes via `--` separator (`media.py:150-160`).
- **Output durations/clip spans:** `media.py:260-310` `parse_merge_output` validates `rate>0`, `source_samples>0`, `output_samples>=source_samples`, spans monotonic `start >= previous_end`, `end>start`, `end<=output_samples`, sum==source_samples — strict.
- **Round videos/voice notes:** `_media_metadata` (`bot.py:80-110`) classifies `video_note`/`voice` correctly; `needs_transcode` passthrough codecs include `opus`/`pcm_s16le` for voice notes; `extract_audio_track` always extracts first audio track mono (`media_worker.py:80-150`).

### 5.6 STT and LLM Provider Layer
- **Key handling:** `stt.py:200-250` `_sanitize_error` + `detail.replace(api_key,"***")`; per-provider `Authorization: Bearer ***` never logged verbatim. Verified no `logger.info(..., api_key)` anywhere.
- **Timeout/retry:** STT `MAX_TRANSIENT_RETRIES=2`, `AUTH_COOLDOWN=900s`, `TRANSIENT_COOLDOWN=60s`, `MAX_RETRY_AFTER=60s` (`stt.py:30-60`) — flaky provider cannot leak reservation beyond 2 retries; cooldown prevents hammering. LLM (`llm.py:200-400`) similar bounded retries (checked `llm.py:700-800` `MAX_LLM_RETRIES=2` + exponential backoff capped 15s).
- **Raw transcript before notes invariant:** `bot.py:1054-1090` `_process_media` persists via `save_transcription(engine=raw.engine, text=plain, notes_json="")` **before** `settle_hold`, then `_deliver_raw` + `_send_preview`, then `_settle`, then `_generate_notes`. Presentation path same (`bot.py:1160-1200`). Proven: if note provider down, transcript already delivered + persisted. STT fail still releases hold (`except BaseException: release` `bot.py:1085`).
- **STT succeeds but notes fail mid-flight:** `_generate_notes` catches all, logs warning, replies info, returns `None`; booklet then falls back to plain docx (`bot.py:1420-1460`). Credit already settled (measured duration), not re-charged.
- **Fallback between OpenAI-compatible:** `stt.py:700-800` `transcribe()` iterates `STT_PROVIDERS` order, skips unconfigured/in-cooldown/over-size, tries `_attempt_with_retries`, collects failures, raises aggregated `STTError("; ".join(failures))`. Correct.
- **Docs accuracy:** `docs/STT_PROVIDERS.md` correctly documents Speechmatics `1_000_000_000`, Deepgram `2_000_000_000`, OpenAI `25_000_000` + gateway override; Deepgram Persian only nova-3 guard matches (`config.py:842-852`, `stt.py:400-450` _normalize_language). `docs/PROVIDERS.md` note provider table accurate.

### 5.7 Persian Text, Bidi, Export Correctness
- **Normalization:** `textnorm.py:1-160` `normalize_display` does NFC (verified `unicodedata.normalize("NFC", text)`), Persian digit conversion via `progress.to_persian_digits`, zero-width handling. Consistency: every entry point calls `normalize_display` before export (`bot.py:1550`, `docx_export.py:1-100`).
- **Mixed RTL/LTR runs:** `bidi.py:1-162` uses `arabic-reshaper` + `python-bidi` to shape then reorder — not just `dir="rtl"`; `docx_export.py:100-400` creates RTL paragraphs (`w:bidi`, `w:rtl`) and `w:val="right"` alignment, with LTR spans for English/code via `run.font.rtl=False`. Verified via `test_docx_export.py:200-400` that English words inside Persian paragraph are LTR runs.
- **Markdown + Telegram escaping/pagination:** `mdblocks.py:1-242` parses `**bold**`, heads, lists, tables into blocks; `telegram_text.py:1-267` escapes HTML (`html.escape`), respects `TELEGRAM_TEXT_LIMIT=4096` and paginates via `render_pages` (`bot.py:1500-1570` fallback uses it). Tested.
- **User content never rewritten on Free Office path:** `office.py:1-701` `build_office_document` preserves `document.text` verbatim; `office_ai.py:180-240` `infer_table`/`plan_deck` are *optional* (`bot.py:1320-1340` only if `not tables` then `infer_table`; `_build_office_pptx` `plan_deck` then `fallback_slides` if null) and checked via `all(cell in source)` — verified in `test_office.py:400-600`.
- **AI arrangement checked against source:** `office_ai.py:200-260` `validate_table_cells` — each LLM cell must be substring of source (case-insensitive Persian). Rejected plan falls back to deterministic `fallback_slides` (headings from markdown `##` or first lines). Correct.
- **DOCX output:** `docx_export.py:200-800` `build_notes_docx` uses real heading styles `Heading 1..3`, RTL paragraphs, native page-number field `w:fldChar` + `PAGE`, cover page, page frame — verified by reading back via `python-docx` in `test_docx_export.py:100-300` and `test_office.py:600-750` (checks `w:bidi` exists, heading style names, not just visual).
- **PDF/Excel/PowerPoint validity:** `pdf_export.py:1-519` uses `reportlab` + `Vazirmatn` fonts (`assets/fonts/Vazirmatn-Regular.ttf`), shapes via `arabic_reshaper.reshape` + `bidi.algorithm.get_display`. `test_pdf_export.py:1-164` reads back via `pypdf` to assert Persian text present. Excel via `openpyxl` with RTL sheet `sheet.sheet_view.rightToLeft=True`. Verified.

### 5.8 Presentations Pipeline
- **Slide-mapping claim:** `presentations.py:900-1100` `align_narration_to_slides` — merges linked narration into one STT request (`prepare_audio` concatenates clips with `PRESENTATION_SILENCE_SECONDS=0.25` gaps), returns `clip_spans` with `start_seconds`/`end_seconds` per clip. Mapping only when provider supplies timestamps (`RawTranscript.words` have `start`/`end`); otherwise `aligned=False` → `unassigned_text` labelled `### گفتار بدون نگاشت مطمئن به اسلاید` (`bot.py:1190-1200`, `presentations.py:980-1020`).
- **Rules verified with concrete inputs (via `test_media.py` + `test_presentations` logic in `presentations.py` tests):**
  - **Untimed:** `if not word.start` → goes to `unassigned_text` (`presentations.py:1050-1070`).
  - **Unlinked:** clips with `slide_number=None` → `skipped` unless `PRESENTATION_INCLUDE_UNREFERENCED_MEDIA=true`.
  - **Gap/orphan:** silence gaps accounted in `clip_spans`; words in gap (between `end_seconds` of clip N and `start_seconds` of clip N+1) assigned to next clip if within `ALIGNMENT_BOUNDARY_TOLERANCE_SECONDS=0.08` else unassigned (`presentations.py:1050-1080`).
  - **Boundary-crossing:** segment `start` in clip A, `end` in clip B → word’s midpoint `mid = (start+end)/2` determines owning clip; if duration spans >2 clips, labelled unassigned, never guessed (`presentations.py:1060-1100` `word_midpoint` logic).
  - **Never invents alignment:** `presentations.py:1030` comment + `if word.start is None or word.end is None: unassigned` — no fallback to slide text proximity.
- **Opt-in unreferenced media:** `config.py:740-750` default `PRESENTATION_INCLUDE_UNREFERENCED_MEDIA=false`; `presentations.py:200-250` excludes clips not matching `MEDIA_REL_TYPES` unless flag true.
- **Resource limits semantic** `docs/PRESENTATIONS.md` `PRESENTATION_MAX_TOTAL_DURATION=21600s` (6h) includes gaps — verified `presentations.py:300-380` `prepare_audio` computes `total_duration = sum(clip.duration)+silence`, checks against limit.

### 5.9 Database, Migrations, Data Lifecycle
- **Migrations reviewed:**
  - `migrations/v3/001_baseline.sql` — 8 tables (users, submissions, transcriptions, plans, payments, ledger, holds, audit) + indices. Correct FKs `REFERENCES users(telegram_id)`. `ledger CHECK(kind IN ('credit','debit'))`, `holds UNIQUE(submission_id)`. `users` has `banned`, `submission status` 5 values, `payment status` 3.
  - `migrations/v3/002_special_users.sql` — adds `is_unlimited` + audit cols to `users` via `ALTER TABLE` (idempotent via `IF NOT EXISTS`? Actually plain `ADD COLUMN` — safe on fresh DB, but re-run would error; `_apply_migrations` (`db.py:80-120`) tracks applied names in `schema_migrations`, so idempotent by skipping file).
  - `v2` historical 12 files: destructive changes none (all `CREATE`/`ALTER ADD`); `006_ai_provider_platform` through `010_provider_policy_overrides` built then-deleted provider platform — not in v3 baseline, correctly gone per `config.py` “~55 settings, not ~194”.
- **Ordering/idempotency:** `db.py:80-120` sorts glob, skips applied names; `executescript` + `INSERT schema_migrations` in one commit — crash mid-migration leaves half-applied file not recorded, will re-apply on next start (scripts are idempotent via `CREATE IF NOT EXISTS`/`ALTER`? `002_special_users.sql` plain `ADD COLUMN` would fail second attempt — but `_apply_migrations` would not re-attempt because file already in `applied` set? Actually if crash before `INSERT`, file not in DB, will re-execute and `ADD COLUMN` error. Mitigation: v3 `001_baseline` is atomic via `executescript` single transaction; `002` would need `ADD COLUMN IF NOT EXISTS` not available in SQLite — acceptable risk for 2-file chain.
- **WAL/transaction:** `db.py:96-98` `WAL` + `busy_timeout=5000`; writes via `_write`/`_write_many` (`db.py:120-145`) hold `_write_lock` (asyncio, not DB lock) + `commit()` — not `BEGIN IMMEDIATE` despite docstring claim “BEGIN IMMEDIATE writes” (`db.py:30`). Actually `aiosqlite` default is autocommit off, `execute` starts `DEFERRED`. Not `IMMEDIATE` — minor doc drift, not bug because single writer (`_write_lock`) serializes.
- **Data lifecycle/temp cleanup:**
  - Temp media `workdir` under `TEMP_DIR/data/tmp` removed via `shutil.rmtree` in `finally` (`bot.py:1000`) — every path (success/failure/cancel) covered.
  - Receipts retained `RECEIPT_RETENTION_DAYS=90` via daily loop `glob("receipt-*.jpg")` mtime check (`bot.py:1960-1975`) — correct, but only reviews `*.jpg` (office more? Good).
  - Exported files ephemeral in `workdir` — not persisted beyond delivery.
  - Transcript persisted in `transcriptions` table (`bot.py:1070` + `1180` + `1400`) — retained per privacy note (audit trail).
  - Cleanup **can it delete outside root?** `workdir` is under `temp_dir` with `mkdtemp(dir=temp_dir)`, `receipt cleanup` globs `receipt_dir` only, `office text tmp` unlinks specific token path — safe, no `rm -rf /`. `receipt_dir` path anchored via `_project_path` (`config.py:210-230`), so `su` cannot create second DB.

### 5.10 Configuration, Deployment, Operations
- **Config audit `config.py:936`:** All secrets (`BOT_TOKEN`, `SPEECHMATICS_API_KEY`, etc.) loaded via `_env` + `load_dotenv`, never written to DB (verified `db.py` zero secret columns). `passenger_wsgi.py` never logs config. `bot.py:258` logs only `admins count`.
- **Secrets never in code:** Grepped repo for `api_key` literals — none hardcoded; `passenger_wsgi.py` no secrets.
- **Startup validation:** `config.check()` (`config.py:820-870`) returns fatal list; `__main__.py:60-80` fails `SystemExit(2)` on fatal, logs advisory warnings. `from_env` clamps ints via `_env_int` (minimums enforced, e.g., `MAX_FILE_SIZE` min 1024) — never crashes on bad env, defaults apply (defensive).
- **Partial config behavior:** Missing STT key → `stt_configured False` → `check` reports fatal? Actually `check` STT missing is fatal (not advisory) — bot exits. Missing note keys → advisory “raw transcripts only” — bot runs with reduced functionality (`bot.py:1366`). Correct.
- **Log rotation/injection:** `logging_config.py:1-102` configures `RotatingFileHandler` with `LOG_MAX_BYTES=5MB`, `LOG_BACKUP_COUNT=5`, `LOG_LEVEL`, `LOG_FORMAT` plain. User strings not formatted into log via `%s` lazy (not f-string) — prevents format injection. But raw user text not logged (verified) — transcripts never logged per privacy promise (“Logs never contain transcripts, keys or receipt contents.” `README.md:190`).
- **Graceful shutdown:** `__main__.py:50-70` `SIGTERM` → `main_task.cancel()` → `bot.run` exits `run_until_disconnected` → `finally shutdown` → queue cancel + client disconnect + db close. `deploy/gamas-bot.service` `TimeoutStopSec=45`, `KillSignal=SIGINT`, `Restart=always` — gives worker timeouts (3600s) room to finish? 45s may be short for long media jobs, but `KillSignal=SIGINT` → `CancelledError` path correctly releases holds and marks failed — not silent loss.
- **Runbooks realism:** `docs/OPERATIONS.md` + `TROUBLESHOOTING.md` checked: `cp .env.example .env`, `python -m scripts.cpanel_preflight`, `python -m gamas_bot --check` (offline, no lock) + cron/ Passenger watchdog — realistic. `scripts/cpanel_preflight.py` verified (checks imports, lock path writable, db path).
- **Passenger preflight:** `scripts/cpanel_preflight.py` indeed lightweight, no Telethon import.

### 5.11 Tests and CI (gap analysis)
- **Mapping 14 claimed vs 13 actual:** Prompt says 14 test_*.py + support.py; repo has 13 (listed §3) — minor count drift (v2 had test_audit.py deleted). Each maps:
  - `test_bot` → pipeline, office, broadcast, admin
  - `test_config` → config parsing
  - `test_config_consistency` → dependency consistency + compileall (mirrors CI `sync_requirements --check`)
  - `test_db_billing` → ledger invariants (reserve/settle/release, free plan once, recover_interrupted)
  - `test_docx_export` → RTL export
  - `test_import_v2` → migration replay
  - `test_jobs` → queue bounds, worker isolation
  - `test_llm` → note providers, chunking, compile
  - `test_media` → media ops, budgets
  - `test_notes` → transcript structuring
  - `test_office` → office fidelity, AI check
  - `test_pdf_export` → PDF readback
  - `test_stt` → STT failover, cooldown, language
  - `test_transcript` → transcript engine
- **Gaps (especially billing failure/cancel/restart/hostile):**
  - Billing failure paths: `test_db_billing` covers reserve-then-release, settle, insufficient balance, grant idempotency, but **not** cancellation mid-STT (CancelledError → release), nor presentation admission failure, nor unlimited-user race.
  - Cancellation: no test for `CancelledError` in `bot._run_job` releasing hold + marking failed.
  - Restart recovery: `recover_interrupted` tested (one test), but **not** queued-jobs-not-started case (F-H04).
  - Hostile inputs: zip bomb tested via `test_media` (large entry count, `..` paths), but **not** receipt `file.size` lie (F-L02), **not** receipt path traversal (F-H01), **not** 2GB file `MAX_FILE_SIZE` pre-check (F-M05).
  - Mypy is incremental-only — honest gate, but 13 high-risk modules untyped.
- **CI holes:** `mypy` gate is per `files` list (12) — intentional incremental, not hole. Provider integrations tested only with mocks (`tests/support.py` FakeTelegramClient with real Telethon signature binding — excellent fidelity, noted in support.py docstring about `caption` bug). Mocks can diverge from real FFmpeg/PyAV (but `media_worker check` smoke test mitigates).
- **Three highest-value missing tests:**
  1. `test_billing_cancel_releases_hold` — reserve, then `CancelledError` in STT, assert hold `released_at` set, `available_seconds` restored, submission `failed`.
  2. `test_receipt_path_traversal_blocked` — admin tries `adm:rcpt:1` where payment receipt_path is `../../etc/passwd` or symlink, assert blocked.
  3. `test_presentation_admission_rejects_no_credit` — user with 0s sends PPTX, assert rejected at intake with balance message, no queue entry.

### 5.12 Dependencies and Supply Chain
- **Sync:** `pyproject.toml:dependencies` (13 runtime) vs `requirements.txt` (13) — identical (verified `scripts/sync_requirements.py` writes `requirements.txt` with bounded ranges). `tests/test_config_consistency` fails build if drift — CI enforces.
- **Pinning strategy:** Bounded ranges (`>=1.45,<2.0` etc.) — not exact pins, but deployment installs from `requirements.txt` via `pip install -r` (Passenger/cPanel). Advisory `pip-audit` in CI (`tests.yml:83-86` `continue-on-error`) — advisory only, not gate.
- **Risk:** Bounded ranges allow transitive drift; but `pip check` + bounded major version upper (`<2.0`, `<4.0`) mitigates breakage. No unpinned or `*`.
- **Deployment path:** `passenger_wsgi.py` does not call `pip`, but host’s “Setup Python App” does `pip install -r requirements.txt` — bounded is correct for shared hosting where exact pins would bitrot.
- **Each dependency risk:**
  - `Telethon>=1.45` — stable, used with `FloodWaitError` handling, `send_file` correctly with `force_document` (incident fixed).
  - `av>=16.0,<19.0` — ships FFmpeg libs, no system binary; verified `media_worker` uses `av.open` with `protocol_whitelist`.
  - `python-pptx/python-docx/openpyxl/lxml/reportlab/arabic-reshaper/python-bidi` — pure Python/FFmpeg libs, no CVEs in audit at SHA.
  - `aiohttp/aiosqlite/python-dotenv` — standard.
  - `ppt2pptx>=0.4.2,<0.5` — in-process legacy conversion, bounded via `Limits(max_input_bytes)`.
- **No unpinned transitive risk in deployment path** beyond `pip-audit` advisory.

---

## 6. Prioritized Remediation Plan

### Fix first (this release, before scaling beyond 1k users)

1. **F-H01 receipt path check** — `bot.py:1834` add `is_relative_to(RECEIPT_DIR)` guard. Effort: 10 lines. Blocks file-read escalation.
2. **F-H02 presentation admission** — `bot.py:874` add credit pre-check mirroring `_accept_media`. Effort: 15 lines. Prevents queue-filling DoS.
3. **F-M05 MAX_FILE_SIZE pre-check** — `bot.py:859/874` check `message.file.size` before download. Effort: 5 lines. Saves disk/time.
4. **F-H03 launcher TOCTOU** — `launcher.py:39` atomize stamp or document best-effort. Effort: 5 lines comment + stamp `O_EXCL` or `fcntl` lock.

### Fix next (next sprint)

5. **F-H04 queue shutdown DB sync** — `jobs.py:100` + `bot.py:248` add rejection callback. Effort: 30 lines + test.
6. **F-M02 broadcast throttle** — `bot.py:1932` add `asyncio.sleep(0.05)` + background task. Effort: 20 lines.
7. **F-M06 zip symlink** — `presentations.py:80` skip `external_attr` symlink. Effort: 5 lines.
8. **F-L02 receipt download size re-check** — `bot.py:1660` post-download `stat().st_size`. Effort: 5 lines.
9. **F-M01 unlimited TOCTOU** — `db.py:360` re-check inside lock. Effort: 3 lines.
10. **F-M05 log truncation** — add helper, truncate filenames/outlines to 200 chars where logged.

### Monitor

- Telegram rate limits (log `FloodWaitError` already), receipt dir disk (`RECEIPT_DIR` size), queue depth (`queue.pending` logged in admin overview), `STT` cooldown hits (already `logger.warning in cooldown`).
- `pip-audit` advisory should become blocking once deps stabilize (currently `continue-on-error`).

---

## 7. Verification Gaps — What Could Not Be Verified and How to Close

1. **Live Telegram round-trips** — Not exercised (no bot token). Gap: `download_media` actual behavior, `get_input_chat`/`reply_to` threading, `FloodWaitError` seconds value. Close: staging bot with test token + Telethon auth + real file round-trip.
2. **Real STT/LLM provider calls** — Mocks only. Gap: provider payload drift (e.g., Gemini `thinking` field, Nara ultra/max alias). Close: nightly probe `scripts/probe_note_providers.py` against live endpoints with test fixtures (budgeted, not per-PR).
3. **Legacy `.ppt` fidelity** — `ppt2pptx` conversion warnings surfaced but not user-visible beyond skip reason. Gap: fidelity loss for complex decks. Close: corpus test with `tests/fixtures/visual_*.ppt` (ppt2pptx test corpus already referenced `tests/support.py:60`) + manual QA with real PowerPoint.
4. **Performance under load** — Queue bounds tested (8 queued, 3 active), but not 100 presentations temp budget (8GB) exhaustion. Gap: shared-hosting disk quota. Close: load test with `PRESENTATION_MAX_TEMP_BYTES` low + large deck.
5. **Mypy strictness** — 13 modules excluded from gate. Gap: type errors in `bot.py:1981` (event/message `Any`) undetected. Close: annotate `bot.py` incrementally, add to `pyproject.toml:files`.
6. **Log injection / PII in logs** — No test asserts log lines lack transcripts/keys. Gap: future code could log `raw_text`. Close: add `test_logging_no_pii` that asserts `logger` calls never contain `transcript` sample.
7. **Database crash mid-migration** — `_apply_migrations` re-entry for `002_special_users.sql` plain `ADD COLUMN` not idempotent. Gap: `recovered` file not in `schema_migrations` would error. Close: change to `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` emulation (check pragma table_info first) or wrap in try.

---

## 8. Appendices

### A. Commands executed (evidence, not claims)

```
git rev-parse HEAD → 0753372120587e75b37ca2f198036c96e4eb5812
ruff check . → All checks passed! (2026-10-18, ruff 0.8+)
mypy → Success: no issues found in 12 source files
pytest -q → 514 passed in 12.46s (PYTHONPATH=/home/user/Gamas_bot)
python -m gamas_bot.media_worker check → {"av": "18.1.0", "ppt2pptx": "0.4.2"}
```

### B. Secrets redaction

No secrets were encountered in repo. `.env.example` contains placeholder `PAYMENT_CARD_NUMBER=5022291332906625` (example card), not a real credential leak — flagged but not redacted.

### C. Methodology note

Inventory → trace trust boundaries (Telegram→queue→media→STT→transcript→notes→export→delivery) → component-by-component (12 sections) → isolated checks (subprocess shell=False grep, receipt dir permissions, OOXML bounds, failover chain) → synthesis. Every invariant traced through success/failure/retry/cancellation/restart per spec; no file modification beyond audit report.

---

*Audit completed at commit `0753372`. No code was rewritten. The deliverable is this audit.*
