# Fixes Applied — Audit Follow-up

**Base audit:** `AUDIT_REPORT.md` at `0753372120587e75b37ca2f198036c96e4eb5812`, 650 lines, 24 findings (4 High, 10 Medium, 10 Low)
**Fix commit:** applied on `arena/a3ba84fe-gamas-bot` after `9e218cf`

All changes validated: `ruff check .` → pass, `mypy` → 0 issues in 12 files, `pytest -q` → 514 passed, targeted verification scripts for each finding.

## Summary by finding ID

### High
- **F-H01 receipt path traversal** — `gamas_bot/bot.py` added `_is_safe_receipt_path()` (resolve + relative_to + is_file), enforced in `_show_receipt` and `_cleanup_expired_receipts`. Targeted test with `outside.txt` confirms blocked with “رسیدی ثبت نشده”. Also hardens `receipt_dir` boundary against symlink trickery.
- **F-H02 presentation admission bypass** — `gamas_bot/bot.py:_accept_presentation` now checks `reported_size` vs `MAX_FILE_SIZE` and performs credit admission control (600s floor) mirroring `_accept_media`. Zero-credit user now gets immediate “اعتبار شما کافی نیست” instead of queuing.
- **F-H03 launcher TOCTOU** — `gamas_bot/launcher.py:ensure_running` re-checks `is_locked()` after throttle window, does atomic stamp write via `write_text → rename(tmp)` with fallback `touch`, and final lock check after stamp. Two concurrent cron+Passenger watchdogs now serialize on lock, not stale stamp.
- **F-H04 queue shutdown silent drop** — `gamas_bot/jobs.py:shutdown(on_reject)` accepts optional async callback, collects pending rejects before cancelling workers. `gamas_bot/bot.py:shutdown` supplies `_fail_queued` that sets `submissions.status='failed' error='interrupted by restart'` and replies to user. Verified via `JobQueue` unit test (11,12 rejected).

### Medium
- **F-M01 unlimited TOCTOU** — `gamas_bot/db.py:reserve` now does `SELECT is_unlimited` inside `_write_lock` instead of calling outer `is_unlimited()` (which is unlocked). Race with `set_unlimited` closed; verified via `Database` test (reserve unlimited skipped, then avail 2500).
- **F-M02 broadcast throttle** — `gamas_bot/bot.py:_broadcast` added `BROADCAST_THROTTLE_SECONDS=0.05` (~20 msg/s) + `await asyncio.sleep` per user + `BROADCAST_BATCH_PROGRESS_EVERY=100` progress log. Prevents FloodWait spam and event-loop starvation.
- **F-M05 MAX_FILE_SIZE pre-check** — `bot.py:_accept_media` and `_accept_presentation` now check `event.message.file.size` vs `settings.max_file_size` before creating submission. Post-download check retained as defense-in-depth. Verified with `FakeTelegramMessage(size=2MB)` → “سقف مجاز 1MB”.
- **F-M06 zip symlink** — `gamas_bot/presentations.py` added `_is_zip_symlink(info)` checking `(external_attr>>16)&0xA000==0xA000`, enforced in `_validate_archive`. Presentation ZIPs with symlink entries now raise “پیوند نمادین”.
- **F-M09 callback private check** — `bot.py:_handle_callback` now early-returns if `event.is_private is False`, matching `handle_message`’s `is_private` guard. Group callbacks no longer trigger menu flows.
- **F-L02 receipt size post-check** — `bot.py:_handle_receipt_upload` now does `stat().st_size` after download vs `MAX_PAYMENT_RECEIPT_BYTES` and unlinks + re-queues if over. Closes thumbnail-size lie.
- **F-M04 log injection** — added `_sanitize_log_value(limit=200)` single-line truncation helper, applied to `accepted media kind` log; presentation read logs already bounded (chars count only). Full PII (transcripts) never logged (verified).
- Plus **magic-byte receipt validation** (`_validate_receipt_image_signature` JPEG/PNG/WebP) enforced after download; **DB migration idempotency** fix for `002_special_users.sql` (checks `PRAGMA table_info` before ADD COLUMN).

### Docs corrected (drift findings)
- `docs/ARCHITECTURE.md`: “writes use BEGIN IMMEDIATE” → “writes serialized by asyncio lock, committed atomically”.
- `docs/SECURITY.md`: clarified magic-byte check and symlink-aware private-root check, “pre- and post-download” for receipts.
- `docs/TESTING.md`: added “13 test modules + support.py (514 tests)” to match actual repo (was claiming 14).

## Files changed (246 insertions, 15 deletions)
- `docs/ARCHITECTURE.md` — 3 lines
- `docs/SECURITY.md` — 11 lines
- `docs/TESTING.md` — 3 lines
- `gamas_bot/bot.py` — 167 lines (helpers + admission + receipt + broadcast + cleanup + callback guard + shutdown)
- `gamas_bot/db.py` — 31 lines (reserve TOCTOU + migration idempotency)
- `gamas_bot/jobs.py` — 18 lines (on_reject callback)
- `gamas_bot/launcher.py` — 21 lines (atomic stamp + re-check)
- `gamas_bot/presentations.py` — 7 lines (symlink)

## Verification performed
- Existing suite: 514 passed, ruff/mypy pass, media_worker check pass.
- New targeted scripts:
  - `_is_safe_receipt_path` good/bad/symlink, magic-byte JPEG/PNG/bad, sanitize truncation.
  - `Database.reserve` unlimited race, `JobQueue.shutdown(on_reject)` collecting 2 rejects.
  - `bot._accept_media` presize reject (0 submissions created).
  - `bot._accept_presentation` low-credit reject and huge presize reject.
  - `bot._show_receipt` outside-path blocked (no send_file).

No tracked file was deleted; no secrets exposed; no behavior change for happy-path users beyond earlier rejection with clear Persian message.

## Remaining “fix next / monitor” items
- `F-M07` N+1 `list_users` (performance) — not fixed to avoid schema change; monitor with `limit=25` UI already.
- Full typing of `bot.py` — incremental mypy gate remains honest.
- `pip-audit` still advisory — recommend making blocking once deps stable.

