# Deploy on cPanel

The bot is a long-running Telegram client, not a website. The host must allow
**Terminal/SSH, Python 3.11–3.13, Cron Jobs, background processes** and outbound
TCP 443 to Telegram and the AI providers. Confirm that with your host before
buying a plan; a "Python support" badge alone is not enough.

## 1. Install outside every web root

```bash
cd "$HOME"
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git gamas_bot
cd "$HOME/gamas_bot"
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
nano .env
```

If `python3.11` does not exist, use the interpreter that *cPanel → Setup Python
App* shows (`/home/USER/virtualenv/gamas_bot/3.11/bin/python`) or the one from
`ls /opt/alt/python*/bin/python3*`. With Setup Python App, activate that
virtualenv instead of creating another one.

Keep the project in `$HOME`, never in `public_html`, `www`, `htdocs` or
`httpdocs`. Create `.env` once; protect it with `chmod 600 .env` and keep
`data/` at `chmod 700`.

Minimum `.env` values: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`,
`TELEGRAM_API_HASH`, numeric `ADMIN_IDS`, one STT key. Full list:
[Configuration](CONFIGURATION.md). Restart after every `.env` change.

## 2. Validate

```bash
python -m scripts.cpanel_preflight
python -m gamas_bot --check
python -m gamas_bot
```

`cpanel_preflight` checks Python version, packages, permissions, free disk,
web-root placement, the instance lock and Telegram reachability, and prints
`PASS`/`WARN`/`FAIL`. `--check` is offline and never migrates data. Fix every
`FAIL`. Then send `/start` and a short voice note, stop with **Ctrl+C**, and wait
for `stopped` before enabling cron.

Start with the shipped limits (3 active jobs, 8 queued, 1 media worker, 1 GiB
uploads). They are ceilings, not a RAM guarantee — see [host sizing](OPERATIONS.md#host-sizing).

## 3. Cron watchdog (once per minute)

*cPanel → Cron Jobs → Add New Cron Job*, common settings **Once Per Minute**.
Put only this in the **Command** field, replacing `USER`:

```bash
/home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

Equivalent crontab line:

```cron
* * * * * /home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

The watchdog refuses a second instance and throttles failed restarts (45 s). Use
the absolute interpreter path: cron has no activated virtualenv and no `PATH`
from your shell.

## 4. Status, stop and logs

```bash
python scripts/ensure_running.py status     # running (pid=…) | stopped | misconfigured
python scripts/ensure_running.py stop
python scripts/ensure_running.py restart
tail -n 80 data/logs/bot.log
tail -n 80 data/logs/launcher.err
```

Before maintenance **disable the cron job** (cPanel checkbox or remove the line),
otherwise it restarts the bot within a minute. After fixing a startup error use
`--force` to bypass the spawn throttle. Never delete `data/gamas.lock` while a
process is alive and never use `pkill python` on shared hosting.

## 5. Optional: bot-launcher health endpoint

This optional root-level `passenger_wsgi.py` exposes the bot watchdog status.
You do not need Passenger to run the bot.
If you want an uptime monitor for the bot-launcher endpoint, create a separate
Python app whose startup file is `passenger_wsgi.py` and entry point
`application`. `GET /` and `GET /health` answer
`{"service":"gamas-bot","status":"running"}` (or `starting`/`error`); they never
expose configuration or user data. Keep cron enabled — the endpoint only starts
or reports the process.

## 6. Update safely

```bash
cd "$HOME/gamas_bot"
# 1. disable the cron job in cPanel
python scripts/ensure_running.py stop
python scripts/ensure_running.py status          # must print: stopped
# 2. back up .env and data/ (see Operations)
. .venv/bin/activate
git pull --ff-only
python -m pip install -r requirements.txt
python -m gamas_bot --check
python scripts/ensure_running.py --force
```

Re-enable cron only after `status` reports `running` and the log is clean.
Migrations run at startup; there is no migration command. Never run older code
against a migrated database — roll back code **and** data together.

## 7. Permissions and disk

```bash
chmod 600 .env
chmod 700 data
du -sh data            # database, session, receipts, logs
df -h "$HOME"
```

A 2 GB cPanel account is enough for a small deployment, but plan for the SQLite
database plus receipts and temporary media. If the host kills background
processes or `pip install` cannot build `av`/`lxml` wheels, ask for
CloudLinux/AlmaLinux 8+ with Python 3.11+, or move to a VPS; cron cannot bypass
CPU/RAM/process quotas.

## Troubleshooting (short)

| Symptom | Fix |
| --- | --- |
| Cron does nothing | Absolute paths; check `data/logs/launcher.err` |
| `misconfigured` | `--check` shows the failing variable |
| `start` prints `throttled` | A start attempt happened < 45 s ago; fix the cause, then `--force` |
| Exit code 3 | Another instance holds the lock — stop it, do not delete the lock |
| Bot dies silently | Host process/RAM limit; lower `MAX_ACTIVE_JOBS`, media and note concurrency |
| `pip install` fails on `av`/`lxml` | Wrong Python or old glibc; use the host's supported interpreter |

More: [Troubleshooting](TROUBLESHOOTING.md) · [Operations](OPERATIONS.md).
