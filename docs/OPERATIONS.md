# Operations

One instance per token/session. Paths below are defaults; add your own paths to
the backup commands if you changed them.

## Start and stop

**Windows PowerShell**

```powershell
.\.venv\Scripts\python.exe scripts\ensure_running.py start
.\.venv\Scripts\python.exe scripts\ensure_running.py status
.\.venv\Scripts\python.exe scripts\ensure_running.py stop
```

**cPanel**

```bash
python scripts/ensure_running.py start
python scripts/ensure_running.py status
python scripts/ensure_running.py stop
```

Foreground mode (`python -m gamas_bot`) stops with **Ctrl+C**; wait for the
shutdown to finish. Before maintenance, disable the cron job / scheduled task
(and any uptime monitor pointing at `passenger_wsgi.py`), otherwise the watchdog
restarts the bot within a minute. After fixing a startup error,
`ensure_running.py --force` bypasses the 45-second spawn throttle. Never delete
an active lock file and never kill processes by name on shared hosting.

## Logs

```powershell
Get-Content .\data\logs\bot.log -Tail 80 -Wait
Get-Content .\data\logs\launcher.err -Tail 40
```

```bash
tail -n 80 data/logs/bot.log
tail -n 80 data/logs/launcher.err
```

Foreground runs log to the console; the launcher's child always writes
`data/logs/bot.log`. Rotation: `LOG_MAX_BYTES=5242880` × `LOG_BACKUP_COUNT=5`.
Useful lines:

- `GMS-#####` — job reference to quote in support.
- `LLM request sending … requested_output_tokens=<n>` — the budget actually sent.
- `note provider usage … output_tokens=<n>` — what the provider billed.
- `note provider failed; trying the next one` — failover with a safe reason.

To print the effective note-generation providers and model IDs without keys or
network calls, run `python -m scripts.probe_note_providers --show-config` in the
project virtualenv. This reflects the environment visible to that command; for a
managed service, compare its environment overrides with the private `.env`.
Review `STT_PROVIDERS` and its model settings separately for transcription.
Logs never contain keys, prompts or transcript text. Environment changes need a
restart.

## Backup and restore

Stop triggers and the bot, confirm `status` is `stopped`, then copy `.env` and the
**whole** `data/` directory (database, `-wal`/`-shm` sidecars, session, receipts,
logs). Copying only the `.db` file while it is live can lose WAL transactions.

**Windows PowerShell**

```powershell
$Backup = Join-Path $HOME ("Gamas-backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $Backup -Force | Out-Null
Copy-Item .env -Destination $Backup
Copy-Item data -Destination $Backup -Recurse
```

**cPanel**

```bash
umask 077
BACKUP="$HOME/backups/gamas-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
cp -p .env "$BACKUP/"
cp -a data "$BACKUP/"
```

Keep backups encrypted and outside web roots, Cloud-sync folders and Git. Record
`git rev-parse HEAD` and the Python version with each backup.

**Restore:** stop triggers and the bot, move the current files aside, restore
matching code + `.env` + the complete data set, reinstall dependencies, run
`--check`, start, and read the log. Never mix a restored database with stale
`-wal`/`-shm` files, never run older code against a newer schema, and never
connect two copies of one Telegram session.

## Updates

Follow [Windows step 7](WINDOWS.md#7-update) or
[cPanel step 6](CPANEL.md#6-update-safely). Migrations run at startup; there is no
separate migration command. Update code and data together — a downgrade needs
both.

Legacy v2 data import is exceptional: read
`python scripts/import_v2.py --help`, test on copies, never on live data.

## Retention

| Data | Default |
| --- | --- |
| Reviewed receipt images | 90 days (`RECEIPT_RETENTION_DAYS`) |
| Pending-review receipts, payments, ledger | kept |
| Transcript text | kept (`TRANSCRIPT_RETENTION_DAYS=0`) |
| Job temporary files | cleaned after each job, stale work swept at startup |
| Uploaded-document text cache | kept (`DOCUMENT_RETENTION_DAYS=0`) |
| Quizzes, audits, released holds | kept within their retention settings |

Setting `DOCUMENT_RETENTION_DAYS`, `LEARNING_RETENTION_DAYS`,
`AUDIT_RETENTION_DAYS` or `HOLDS_RETENTION_DAYS` to a positive number enables a
daily batched sweep (500 rows per run) of unlinked/terminal rows only. Library
pruning does **not** erase the document text cache — plan a privacy policy for
it deliberately.

The database is also housekept automatically: a passive WAL checkpoint daily and
`PRAGMA optimize` weekly.

## Host sizing

| Resource | Default |
| --- | --- |
| Active / queued / per-user jobs | 3 / 8 / 2 |
| Downloads / media workers / uploads | 2 / 1 / 2 |
| STT slots (Speechmatics / Deepgram / OpenAI-compatible) | 2 / 4 / 2 |
| Note-map concurrency / primary / fallback LLM slots | 3 / 3 / 1 |
| Media upload / duration | 1 GiB / 2 h |
| Temporary reserve | 64 MiB plus working copies |
| Office builds | 2 |

Independent stages overlap network waits, so a single media worker is fine.
`NOTE_MAX_OUTPUT_TOKENS` is a per-request budget, not RAM: 32 768 tokens at a
conservative 20 tokens/s can exceed the 900 s request deadline, which `--check`
warns about. The shipped output budget is 16,384, with a 131,072-token context
and 64,000-character chunk ceiling; editorial compilation is off. Dispatch
never hedges, including with a stale nonzero `NOTE_HEDGE_AFTER_SECONDS`; bounded
transport retries and schema compatibility adjustments still cost requests.
Provider-specific caps and the shared quota ledger also apply (see
[Providers](PROVIDERS.md)).

Recommended 2 GB cPanel profile (`.env`):

```dotenv
MAX_ACTIVE_JOBS=3
MAX_QUEUED_JOBS=6
MEDIA_WORKER_CONCURRENCY=1
MAX_CONCURRENT_DOWNLOADS=2
TELEGRAM_DOWNLOAD_CONNECTIONS=1
NOTE_CONCURRENCY=3
LEARNING_CHUNK_CONCURRENCY=2
LEARNING_GLOBAL_CONCURRENCY=2
NOTE_HEDGE_AFTER_SECONDS=0
DOC_MAX_BYTES=1073741824
```

If the account gets throttled: check CPU/RAM/process/disk quotas first, then
lower jobs, media and note concurrency, then file limits. Cron cannot bypass host
quotas — move to a VPS if the host kills permitted background work. Document
parsing uses bounded threads, not a memory sandbox: pathological files still cost
CPU.

## Monitoring

- `python -m gamas_bot --check` — offline, safe to run while the bot serves.
- `python scripts/ensure_running.py status` — process state and PID.
- `GET /health` on the optional Passenger app — `running`/`starting`/`error`.
- `GET /health` on the Mini App — liveness plus `database: true/false` from a file-exists check only.
- `GET /ready` on the Mini App — read-only SQLite/schema probe; it does not authenticate requests or run migrations. See [cPanel Mini App operations](MINIAPP_CPANEL.md).

An uptime monitor should poll at 5-minute intervals and only pages you, not
restart the bot.

## Benchmarks (isolated checkout only)

```bash
python -m scripts.concurrency_bench --help
python -m scripts.perf_bench --help
python -m scripts.admin_report_bench --help
python -m scripts.document_chunk_bench --help
```

Simulated latencies and 429s are inputs, not live measurements. Record settings,
Python version, workload and host quota with any number you publish; never promise
fixed throughput.
