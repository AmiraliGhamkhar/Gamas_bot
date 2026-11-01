# Operations

## Start and stop

**Windows PowerShell** — run in a console, stop with **Ctrl+C**:

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe -m gamas_bot
```

**cPanel** — the once-per-minute cron watchdog in [Deploy on cPanel](DEPLOY_CPANEL.md) keeps the
bot running. Start it manually once to test:

```bash
. .venv/bin/activate
python -m gamas_bot --check
python -m gamas_bot
```

Stop, inspect, or restart a background instance via `scripts/ensure_running.py` (never with a broad
`pkill` pattern):

```bash
python scripts/ensure_running.py status     # running (pid=...), stopped, or misconfigured
python scripts/ensure_running.py stop       # graceful SIGTERM using the locked PID
python scripts/ensure_running.py restart    # stop + immediate verified start
python scripts/ensure_running.py --force    # bypass the 45s spawn throttle after fixing .env
```

Pause the cron job while updating or stopping, otherwise it restarts the bot within a minute.

Only one process may use a Telegram session. The instance lock enforces this; a second copy exits
with code `3` instead of competing, while configuration/startup errors exit with code `2`.

## Logs and health

```bash
python -m gamas_bot --check                 # settings + runtime packages + media worker
python -m gamas_bot.media_worker check      # PyAV + ppt2pptx
python -m scripts.cpanel_preflight          # full host check (cPanel)
```

Logs go to **stdout** unless `LOG_FILE` is set. When `LOG_FILE` is configured (or defaulted by the
background launcher to `data/logs/bot.log`), logs rotate automatically at `LOG_MAX_BYTES` (default
5 MiB) across `LOG_BACKUP_COUNT` private (`0600`) files with configured tokens/API keys redacted.
Pre-spawn validation failures and early child crashes are recorded in `data/logs/launcher.err`
(bounded at 1 MiB, `0600`) as well as `stderr`. Use the `GMS-#####` job reference to find log
lines. Never attach transcripts, receipt images or API keys to support requests.

## Update

1. Pause the cron job, stop the bot, and back up (see below).
2. Update source and dependencies: `git pull`, then `python -m pip install -r requirements.txt`.
3. Run `python -m gamas_bot --check`.
4. Re-enable the cron job or start the bot. Migrations run at startup.

Do not run an older version against a database that has already been migrated.

## Backups

Back up these files after stopping the bot:

- `.env`
- `data/gamas_v3.db` (use a SQLite-aware backup if the bot is running, so WAL data is included)
- `data/gamas.session`
- `data/receipts/`

Keep backups encrypted, outside `public_html` and out of Git.

## Retention and disk

- `RECEIPT_RETENTION_DAYS` (default `90`) deletes old receipt images and clears the payment
  reference, writing `receipt_expired` to the audit log.
- `TRANSCRIPT_RETENTION_DAYS` (default `0` = forever) deletes stored text of **finished** jobs
  (`done`, `partial`, `failed`, `rejected`) in bounded daily batches and logs
  `transcript_expired`. Pending/processing rows are never touched.
- Startup removes stale temporary work directories. Temporary job folders are deleted when the
  job ends.

Shared hosting: keep `MEDIA_WORKER_CONCURRENCY=1` (1 child PyAV process max), `MAX_ACTIVE_JOBS=2..3`,
a bounded queue (`MAX_QUEUED_JOBS=8`), and conservative file/temp limits so network-bound STT/LLM
stages run concurrently across users while RAM stays well under 1 GB. If the host kills the
process or runs out of RAM/disk, move to a VPS; cron cannot bypass account limits.
