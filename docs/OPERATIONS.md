# Operations

## Starting and stopping

* **systemd** — `deploy/gamas-bot.service` runs under a dedicated user with
  `UMask=0077`, a `ProtectSystem=strict` sandbox and `Restart=always`. Install
  with `sudo cp deploy/gamas-bot.service /etc/systemd/system/` and adjust the
  paths; [DEPLOY_FA.md](DEPLOY_FA.md) walks through it.
* **cPanel / Passenger** — `passenger_wsgi.py`, `gamas_bot/launcher.py` and
  `scripts/ensure_running.py` keep one bot process alive behind a once-a-minute
  cron watchdog. See [DEPLOY_CPANEL.md](DEPLOY_CPANEL.md).

The instance lock prevents a second Telegram polling client: a duplicate process
exits immediately.

Shutdown order is deliberate — stop intake, reject queued work, cancel workers,
disconnect Telegram, close SQLite. A submission is recorded as either failed or
processed; it is never left in `processing` after a restart.

## Health checks

All three are offline unless stated:

```bash
python -m gamas_bot --check              # configuration; exits 2 on a FAIL
python -m gamas_bot.media_worker check   # av and ppt2pptx import
python -m scripts.cpanel_preflight       # host readiness + outbound TCP probes
```

There is no provider-health panel and no background probing: a provider is
exercised only by a real job.

## Note providers

* **Order is policy.** `NOTE_PROVIDERS` (default `nara,openrouter`) is the order
  providers are attempted. Nothing is reordered at runtime and nothing is stored
  in the database, so a restart cannot change the priority.
* **Credentials are environment-only.** `NARA_API_KEY`, `OPENROUTER_API_KEY`,
  `GEMINI_API_KEY` and the older `NOTE_API_KEY` live in `.env` or the service
  unit. There is no vault, wizard or encrypted store.
* **Missing keys are visible, not silent.** `--check` lists the eligible
  providers and warns when note generation is not configured — the bot then
  delivers raw transcripts only. The job log names every provider skipped for
  missing credentials and every provider left behind after a failure.
* **Failures are bounded.** Transient failures get a small retry budget per
  provider, then the next provider runs. An authentication failure or a rejected
  request moves on immediately. An oversized input is split, never re-sent
  unchanged; an answer cut off by the output budget is not retried at all. Every
  note request has its own 300 s timeout.

See [PROVIDERS.md](PROVIDERS.md).

## Logs

`LOG_FORMAT=json` emits structured operational metadata. `LOG_FILE`
(`data/gamas.log` by default) enables rotation with `LOG_MAX_BYTES` (5 MB) and
`LOG_BACKUP_COUNT` (5); without it, output goes to stdout/stderr for systemd or
the Passenger log.

Every job carries its submission reference (`GMS-#####`) as `job_id`, so one
upload can be followed end to end. Never attach raw request or response bodies,
API keys, transcripts or student documents to a provider issue report — quote the
`request_id` the provider returned instead.

## Backups

Back these up together, or not at all:

* `DB_PATH` (`data/gamas_v3.db`) and its `-wal`/`-shm` sidecars — or a consistent
  snapshot via `sqlite3 data/gamas_v3.db ".backup backup.db"`;
* the Telegram session file (`TELEGRAM_SESSION`, `data/gamas.session`);
* `.env`;
* `RECEIPT_DIR` if receipts must outlive the retention window.

Keep backups encrypted and access-controlled. Never put them in a Git repository
or an ordinary log archive.

## Capacity

Concurrency is bounded by `MAX_ACTIVE_JOBS` running jobs and `MAX_QUEUED_JOBS`
waiting ones. Beyond that, uploads are rejected explicitly instead of building an
unbounded queue. On shared hosting use `MAX_ACTIVE_JOBS=1` and a small
`MAX_QUEUED_JOBS`. Practical throughput is usually limited by STT and provider
latency; document generation is short and runs off the event loop.

## Updates

```bash
git pull
pip install -r requirements.txt
python -m gamas_bot --check     # migrations run at startup
```

Restart the bot afterwards. Migrations under `migrations/v3/` are applied
automatically on startup and recorded in `schema_migrations`; a migration is
never applied twice.
