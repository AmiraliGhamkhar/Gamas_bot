# Operations

## Starting and stopping

* **systemd**: the full unit is `deploy/gamas-bot.service` (a dedicated
  `bot` user, `WorkingDirectory=/opt/gamas-bot`, `Restart=always` with a 5 s
  `RestartSec`, `UMask=0077` and a `ProtectSystem=strict` sandbox that only
  writes under `data/`). Install it with
  `sudo cp deploy/gamas-bot.service /etc/systemd/system/` and adjust the paths
  if your layout differs; `DEPLOY_FA.md` walks through the same unit step by
  step.
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

### AI provider platform operations

* `🤖 پلتفرم AI` in the admin panel (`admin:ai`) manages the multi-provider
  note generation layer: provider cards, key wizard (key message is deleted
  before storage), route order, usage counters, model catalog, structured
  event log, and a secret-free **dry-run** request preview.
* Free-tier safety is the default: `AI_FREE_ONLY=true` never routes to paid
  providers — blocked attempts are logged as `billing_blocked`/quota events,
  never silently charged. `AI_ALLOW_PAID_FALLBACK=true` appends paid legs
  **after** all free legs only.
* Quota incidents: 429 responses put the credential in cooldown and the
  router fails over to the next leg; the operator can reset a provider's
  daily counters from `پلتفرم AI → مصرف → بازنشانی شمارندهٔ امروز`.
* Model catalogs sync from the providers' `/models` endpoints with a TTL
  (`AI_PROVIDER_SYNC_TTL`, default 24 h), manual trigger available per
  provider. Disappeared models are deactivated, never deleted.
* Every 429/5xx is logged with provider, model, status, attempt, latency,
  retry delay, route position and credential label — actionable without
  secrets. See [docs/AI_PROVIDERS.md](AI_PROVIDERS.md).
* Benchmark: `python -m scripts.benchmark_notes --router` prints the planned
  failover chain with per-leg token/character budgets (no network calls);
  `--live` still scores the configured provider's real output.

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
