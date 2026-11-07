# Operations

Run one instance per token/session. Paths below assume the default configuration;
include custom paths in backups and log commands.

## Start and stop

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe -m gamas_bot
```

Stop with **Ctrl+C** and wait for shutdown. Keep the foreground console open.

**cPanel Terminal** (virtualenv active):

```bash
python scripts/ensure_running.py status
python scripts/ensure_running.py stop
python scripts/ensure_running.py restart
```

Pause cron and Passenger/monitor triggers before maintenance, or they can restart
the bot. Stop must report `stopped` before copying data. After fixing a startup
error, `python scripts/ensure_running.py --force` bypasses the spawn throttle.
Never delete an active lock or use broad process-kill commands.

## Logs

Foreground uses stdout unless `LOG_FILE` is set. The background launcher defaults
to `data/logs/bot.log`; startup diagnostics go to `data/logs/launcher.err`.
`LOG_MAX_BYTES` defaults to 5 MiB, `LOG_BACKUP_COUNT` to 5 rotating backups.

```powershell
Get-Content .\data\logs\bot.log -Tail 80 -Wait
```

```bash
tail -n 80 data/logs/bot.log
tail -n 80 data/logs/launcher.err
```

Use the `GMS-#####` job reference. Share only redacted metadata, never private content.

## Backups and restore

Disable start triggers, stop the bot and verify shutdown first. Store backups
encrypted, outside web roots and Git. Never share a session file.

Back up `.env` and the entire private `data/` directory: database, any SQLite
sidecars, Telegram session and receipts. Include configured paths outside `data/`.
Copying a live SQLite file alone can lose WAL transactions. Live backups need
SQLite's backup API plus coordinated session/receipt handling.

**Windows PowerShell** (bot stopped):

```powershell
$Backup = Join-Path $HOME ("Gamas-backups/" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $Backup -Force | Out-Null
Copy-Item .env -Destination $Backup
Copy-Item data -Destination $Backup -Recurse
```

**cPanel Terminal** (bot stopped):

```bash
umask 077
BACKUP="$HOME/backups/gamas-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
cp -p .env "$BACKUP/"
cp -a data "$BACKUP/"
```

Verify the copy succeeded; restrict Windows backup access to the operator account.
Record `git rev-parse HEAD` and the Python version alongside the backup.

To restore: stop triggers and the bot, preserve current files separately, restore
matching code, `.env` and the **complete** data set, reinstall dependencies, run
`--check`, start and inspect logs. Never mix a restored DB with stale WAL/SHM files
or run older code against a newer schema. Test recovery on an isolated copy;
never connect two copies of the same Telegram session.

## Updates and retention

Use [Windows update steps](DEPLOY_WINDOWS.md#3-update) or
[cPanel update steps](DEPLOY_CPANEL.md#5-update-safely). Migrations run at startup.

- Reviewed receipt images expire after 90 days by default; pending-review receipts,
  payment rows and ledger history remain.
- `TRANSCRIPT_RETENTION_DAYS=0` keeps transcript text forever. A positive value
  removes old finished-job text in bounded batches, not active work.
- Job temporary files are cleaned after work; startup sweeps stale work directories.
- Uploaded-document text has no automatic retention policy. Library pruning does
  not delete this cache. Monitor disk use and plan a privacy policy.

### Optional database retention and maintenance

`DOCUMENT_RETENTION_DAYS`, `LEARNING_RETENTION_DAYS`, `AUDIT_RETENTION_DAYS`,
and `HOLDS_RETENTION_DAYS` default to 0 (off). The daily sweep removes old
unlinked document caches, terminal quiz sessions, audit records, and released
holds respectively, in batches of at most 500. Live library-linked documents,
active quizzes, outstanding holds, and leaderboard points remain intact. A
passive WAL checkpoint runs daily and SQLite optimize weekly.

The learning retention default is 0 rather than 90: WO-DB-7 explicitly requires
all retention knobs to default off, preserving deployed data.

### Broadcasts

Broadcasts require an administrator preview and confirmation and run one at a
time. They exclude banned users and recipients with three permanent Telegram
delivery failures; a successful send resets the failure counter. A stopped or
interrupted run can be resumed by its administrator after re-entering the same
text (verified by SHA-256). Cursors are checkpointed per batch, so an abrupt
process crash can cause delivery to be repeated within the last uncheckpointed
batch; Telegram has no transactional exactly-once send. The text itself is not
stored in the database.
