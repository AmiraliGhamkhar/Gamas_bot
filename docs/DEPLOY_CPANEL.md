# Deploy on cPanel shared hosting

Gamas Bot is a long-running Telegram client, not a website. The account needs Terminal/SSH,
Python 3.11+, Cron Jobs, outbound TCP 443 and permission to run a background Python process.
Everything private must stay **outside `public_html`**.

## 1. Install (cPanel Terminal)

```bash
cd "$HOME"
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git gamas_bot
cd "$HOME/gamas_bot"
python3.11 -m venv .venv          # use the host's 3.11+ command if named differently
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If you use **Setup Python App**, install the requirements into the virtualenv cPanel shows you.
Passenger is optional; the cron watchdog below is still required.

## 2. Configure

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, numeric `ADMIN_IDS`, and
one STT key. For shared hosting start with:

```dotenv
MAX_ACTIVE_JOBS=1
MAX_QUEUED_JOBS=4
MAX_ACTIVE_JOBS_PER_USER=1
MEDIA_WORKER_CONCURRENCY=1
```

Lower `MAX_FILE_SIZE`, `TEMP_MIN_FREE_BYTES` and the presentation limits to fit the plan.

## 3. Test before going live

```bash
python -m scripts.cpanel_preflight
python -m gamas_bot --check
python -m gamas_bot
```

Fix every `FAIL`; review every `WARN`. Send the bot a test message and a short audio file, then stop
the foreground run with **Ctrl+C**. `--check` is offline; the preflight also tests disk,
permissions, the lock and outbound network.

## 4. Add the cron watchdog

In **cPanel → Cron Jobs**, add this once per minute (replace `USER`):

```text
* * * * * /home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

With **Setup Python App**, use its interpreter instead, for example
`/home/USER/virtualenv/gamas_bot/3.11/bin/python`. The watchdog starts the bot only when it is not
running and throttles repeated failed starts. Logs: `data/logs/bot.log` and
`data/logs/launcher.err`.

Optionally point an uptime monitor at `passenger_wsgi.py` if you enabled a Python app; it returns
`running`, `starting` or `error` as JSON and starts the bot when needed.

## 5. Update

```bash
cd "$HOME/gamas_bot"
# Pause the cron job in cPanel so it does not restart the bot mid-update,
# then stop the bot only if the PID recorded in the lock is still alive:
PID="$(cat data/gamas.lock 2>/dev/null)"
ps -p "$PID" >/dev/null 2>&1 && kill "$PID"
. .venv/bin/activate
git pull
python -m pip install -r requirements.txt
python -m gamas_bot --check
```

Re-enable the cron job; it starts the bot within a minute.

Database migrations run at startup. Never run an older version against a migrated database. The
cron job restarts the bot within a minute.

## 6. Back up

Back up `.env`, `data/gamas_v3.db`, `data/gamas.session` and `data/receipts/`. Stop the bot first,
or use a SQLite-aware backup so WAL data is included. Store backups encrypted and outside
`public_html` and Git.

## If it fails

| Symptom | Fix |
| --- | --- |
| `Project location` or receipt directory `FAIL` | Move the project/receipts outside every web root |
| `Telegram connectivity FAIL` | Ask the host to allow outbound TCP 443 |
| PyAV install fails / old OS | Request CloudLinux 8+/AlmaLinux 8+ and a supported Python wheel, or use a VPS |
| Process disappears without a Python error | The host killed it: lower concurrency and limits, or move to a VPS |
| Bot stops after a second copy starts | Run one process per token/session |
| `Permission denied` on data files | `chmod 700 data`, `chmod 600 .env`; check the account owns the files |

The preflight is the final check for your account layout.
