# Deploy on cPanel

This is a background Telegram client, not a website. Your host must allow Git,
Terminal/SSH, Python **3.11–3.13**, Cron Jobs, background processes and outbound
TCP 443 to Telegram/providers. Confirm those requirements with your host first.

## 1. Install privately

Run in **cPanel Terminal**, outside `public_html` and every other web root:

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

Create `.env` only once. Fill [required settings](CONFIGURATION.md). Use your host's
Python command if named differently. With **Setup Python App**, activate the
virtualenv cPanel shows instead of creating another one.

## 2. Validate

```bash
python -m scripts.cpanel_preflight
python -m gamas_bot --check
python -m gamas_bot
```

Fix every `FAIL`; review every `WARN`. Preflight writes temporary permission/lock
probes and tests network connections. `--check` is offline and does not migrate data.
Send `/start` and short audio, then **Ctrl+C** and wait for shutdown before enabling cron.

Start with shipped limits: 3 active jobs, 8 queued, 1 media worker, 128 MiB media
uploads. These are ceilings, not RAM guarantees. See [Performance](PERFORMANCE.md).

## 3. Add the watchdog

In **cPanel → Cron Jobs**, choose once per minute. Replace `USER` and paths with
your account values. Put only this in the **Command** field:

```bash
/home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

The equivalent full crontab entry is:

```text
* * * * * /home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

Setup Python App users must substitute its interpreter path. The watchdog prevents
second instances and throttles failed starts. Inspect status and logs:

```bash
python scripts/ensure_running.py status
tail -n 80 data/logs/bot.log
tail -n 80 data/logs/launcher.err
```

## 4. Optional Passenger

Use the private project as application root, `passenger_wsgi.py` as startup file,
and `application` as entry point. `GET /` or `/health` starts/checks the process and
returns `running`, `starting` or `error` JSON. This is not an end-to-end health test.
Keep cron enabled; Passenger needs requests to trigger the launcher.

## 5. Update safely

1. Pause cron **and Passenger/uptime-monitor triggers**.
2. Stop and confirm shutdown; do not proceed after a stop failure.
3. [Back up configuration and data](OPERATIONS.md#backups-and-restore).
4. Update and validate, stopping on any error:

```bash
cd "$HOME/gamas_bot"
. .venv/bin/activate
python scripts/ensure_running.py stop
python scripts/ensure_running.py status
# Continue only after status reports stopped and the backup is complete:
git pull --ff-only && python -m pip install -r requirements.txt && python -m gamas_bot --check
```

Only after the update/check succeeds, start with
`python scripts/ensure_running.py --force`. Check status/logs, then restore triggers.
Migrations run automatically on startup; no `manage.py` is used. Never run older
code against a migrated database.

If PyAV/lxml wheels fail on old Linux, request CloudLinux/AlmaLinux 8+ and supported
Python. Cron cannot bypass CPU/RAM/process limits; change hosting if needed.
