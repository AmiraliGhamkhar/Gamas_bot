# Operations

## Starting and stopping

* **systemd**: a unit is provided in `deploy/gamas-bot.service`. Use
  `Restart=on-failure`, a dedicated user, and the project directory as
  `WorkingDirectory`; see `DEPLOY_CPANEL.md` and `README.md` for the full unit.
* **cPanel / Passenger**: `passenger_wsgi.py` starts the bot in-process and
  `gamas_bot/launcher.py` (plus `scripts/ensure_running.py`) keeps exactly one
  instance alive; `gamas_bot/instance_lock.py` is the advisory lock that makes a
  second polling client impossible.

Shutdown order is deliberate: stop intake, reject whatever is queued, then
cancel the workers, then disconnect Telegram, then close the database. A job is
either fully recorded as failed or fully processed — never dropped while its row
still says `processing`.

## Health and diagnostics

```bash
python -m scripts.cpanel_preflight          # deployment readiness checklist
python -m gamas_bot --check                 # built-in self-check
python -m scripts.validate_docx --render    # DOCX structure + offline pages
```

`🩺 وضعیت سرویس‌ها` in the admin panel shows provider status (manual, cached,
secret-free). The `📜 گزارش مدیر` panel shows the admin audit log and the usage
ledger.

## Logs

`LOG_FORMAT=json` emits one structured object per line with a job identifier.
Useful fields: `job_id`, provider attempt counts, stage durations, failure
class. Transcripts, keys and receipt contents are never logged, by design.

`LOG_FILE` enables rotation (`LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`). When it is
unset, logs go to stdout/stderr, which is what systemd expects.

## Backups

Back up together, or not at all:

* the database (`DATABASE_PATH` plus its `-wal`/`-shm` while the bot is running,
  or a `.backup` snapshot);
* the Telegram session file (`TELEGRAM_SESSION_PATH.session`);
* `PROVIDER_CREDENTIALS_ENCRYPTION_KEY` — stored credentials are unrecoverable
  without it;
* `RECEIPT_DIR` if payment receipts must be retained beyond the retention
  window.

## Capacity

Concurrency is bounded by `MAX_CONCURRENT_JOBS` active jobs and
`MAX_PENDING_JOBS` waiting ones; beyond that the bot rejects uploads with an
explicit message instead of queueing unbounded work. Each active job is
dominated by provider latency (STT + note generation), so the practical limit is
provider throughput, not CPU. Document generation is CPU-bound but short and
runs off the event loop in a thread.
