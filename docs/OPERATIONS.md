# Operations

## Start and stop

**Windows PowerShell:** run the bot in a console and stop it with `Ctrl+C`.

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe -m gamas_bot
```

**cPanel:** use the once-per-minute Cron Jobs watchdog in [Deploy on cPanel](DEPLOY_CPANEL.md). It starts one process per Telegram session. Do not run another copy with the same token/session.

## Health and logs

```powershell
python -m gamas_bot --check
python -m gamas_bot.media_worker check
```

On the cPanel server, also run `python -m scripts.cpanel_preflight`. The bot logs to `LOG_FILE` when configured; otherwise it writes to standard output. The cPanel launcher stores logs in `data/logs/bot.log` and startup errors in `data/logs/launcher.err`.

Use the job reference (`GMS-#####`) to find related log lines. Logs do not need transcripts, receipt images or API keys; do not attach those to support requests.

## Update safely

1. Stop the bot and back up `.env`, the database, Telegram session and receipts.
2. Update the source and install dependencies: `python -m pip install -r requirements.txt`.
3. Run `python -m gamas_bot --check`.
4. Start the bot again. Database migrations run at startup; do not run an older version against a database after it has been migrated.

For cPanel, make sure the cron job does not start the bot during the update. See [Billing](BILLING.md) before migrating old databases.

## Backups and capacity

Keep backups encrypted and outside the web root and Git. Stop the bot before copying the SQLite database, or use a SQLite-aware backup so WAL data is included. Shared hosting should use `MAX_ACTIVE_JOBS=1`, a short queue and upload/temp limits that fit the account. If the host kills the process or lacks disk/RAM, move to a VPS; cron cannot bypass provider limits.
