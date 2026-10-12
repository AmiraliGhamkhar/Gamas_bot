# Deploying on cPanel

> **Read this first.** Gamas Bot is a Telegram bot, not a website. It keeps one
> permanent outbound connection to Telegram and serves no pages and no HTTP API.
> cPanel serves web requests, so the bot has to run *next to* the hosting, not
> through it. This guide does that with a once-a-minute cron watchdog (primary)
> and an optional Passenger status URL (secondary).
>
> This procedure is verified against the code and test suite on Linux. It has
> **not** been run on a real cPanel account — step 4 exists to prove your host
> before you rely on it. If your plan fails that checklist, the honest answer is
> a small VPS (see [DEPLOY_FA.md](DEPLOY_FA.md)).

## 1. Can your plan run it?

| Requirement | Why | How to check |
| --- | --- | --- |
| Terminal / SSH | install packages, run the preflight | cPanel → *Terminal* |
| **Cron Jobs** | restarts the bot when the host kills it | cPanel → *Cron Jobs* |
| Python **3.11+** | code requirement | `python3.11 --version` |
| glibc **2.28+** (CloudLinux / AlmaLinux / Rocky 8+) | `av` and `lxml` ship manylinux wheels that need it; on CloudLinux 7 they cannot install | preflight prints it |
| Outbound TCP **443** to Telegram and your provider hosts | MTProto and the provider APIs | preflight tests it |
| Background processes allowed for more than a few minutes | some hosts kill anything over a CPU/time budget (CloudLinux LVE) | watch the log after step 6 |
| ~1–2 GB RAM, several GB disk and inodes | decoding and temp files, up to 2 GB per upload | cPanel → *Resource Usage* |

Also:

* **Code and data must live outside `public_html`.** `.env`, the SQLite database
  and the Telegram session must never be downloadable. The preflight fails when
  the project is inside a web root.
* No `ffmpeg`, `ffprobe`, `soffice`, Redis, Docker, external database or root
  access is needed. Media runs on PyAV inside a worker process.
* Use `MAX_ACTIVE_JOBS=1` and a small `MAX_QUEUED_JOBS` on shared hosting.

## 2. Upload the code

```bash
cd ~
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git gamas_bot
```

Without git, upload and extract a ZIP into `/home/USER/gamas_bot`. **Not** under
`public_html`.

## 3. Create the environment

**Option A — *Setup Python App*** (gives you a virtualenv and the status URL):
create an application with Python 3.11+, root `gamas_bot`, startup file
`passenger_wsgi.py`, entry point `application`, any URL such as `bot-status`.
Then copy the *"Enter the virtual environment"* command it shows and run it.

**Option B — plain venv:**

```bash
cd ~/gamas_bot && python3.11 -m venv .venv && source .venv/bin/activate
```

Then:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Configure and run the preflight

```bash
cd ~/gamas_bot
cp .env.example .env
chmod 600 .env
nano .env
python -m scripts.cpanel_preflight
```

Minimum: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
`ADMIN_IDS`, one STT key, and optionally the note keys. Everything else has a
working default — see [CONFIGURATION.md](CONFIGURATION.md).

Fix every `FAIL`. The preflight checks Python version, OS/glibc, PyAV import,
project location, the single-instance lock, `.env` presence and permissions,
configuration, `ADMIN_IDS`, Telegram connectivity, note-API reachability, the
private receipt directory and the media worker. Typical failures:

| Message | Fix |
| --- | --- |
| `Operating system` WARN | host OS too old for the wheels → ask for CloudLinux/AlmaLinux 8+, or use a VPS |
| `Project location` FAIL | move the project out of `public_html` |
| `Telegram connectivity` FAIL | ask the host to allow outbound 443 |
| `Note API` WARN | outbound firewall or DNS for your note provider |
| `Single-instance lock` FAIL | the data directory is on a filesystem without `flock`; point `DB_PATH`, `TELEGRAM_SESSION` and `TEMP_DIR` at a local disk |
| `Private receipt directory` FAIL | move `RECEIPT_DIR` outside the web root |
| `Media worker` FAIL | `pip install -r requirements.txt` did not finish |

## 5. First run in the foreground

```bash
python -m gamas_bot
```

Send the bot a message on Telegram, then stop it with `Ctrl+C`. Shutdown is
graceful; running jobs are marked interrupted and their credit released.

## 6. Keep it alive with cron

cPanel → *Cron Jobs* → once per minute, with **absolute** paths:

```text
/home/USER/virtualenv/gamas_bot/3.11/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

(Option B: `/home/USER/gamas_bot/.venv/bin/python …`.)

Each run costs a few tens of milliseconds — it imports only the configuration and
the lock helper, not Telethon or PyAV. If the bot holds its lock, nothing
happens. Otherwise it starts one detached process, at most once per 45 seconds,
so a bot that dies at startup (bad token, no network) is retried politely instead
of hammered. It prints `running`, `started`, `throttled` or `misconfigured`
(exit 2 for the last).

```bash
python scripts/ensure_running.py
tail -f ~/gamas_bot/data/logs/bot.log
cat ~/gamas_bot/data/logs/launcher.err   # non-empty only if Python itself failed
```

## 7. Optional: Passenger status URL

If you used *Setup Python App*, its URL returns
`{"service": "gamas-bot", "status": "running"}` (200; 503 with `starting` or
`error` otherwise) and each request also ensures the bot is running — so an
external monitor hitting it every 5 minutes is a second, independent watchdog.
The cPanel *Restart* button restarts only the status app, **not** the bot.

## 8. Day-to-day

| Task | Command |
| --- | --- |
| Restart | `pkill -TERM -u "$USER" -f 'python.* -m gamas_bot$'` — cron restarts it within a minute |
| Stop permanently | remove the cron line, then the `pkill` above |
| Update | `git pull && pip install -r requirements.txt`, then restart |
| Logs | `data/logs/bot.log` (or `LOG_FILE`) |
| Backup | `.env`, `data/gamas_v3.db` (+ `-wal`/`-shm` while running), `data/gamas.session`, `data/receipts/` |

Migrations run at startup, so the launcher, Passenger entry point and cron
watchdog all keep working unchanged. Relative paths resolve from the **project
directory**, so cron and Passenger see the same database and session as your
terminal.

Billing is manual card-to-card: an admin must inspect the receipt and approve or
reject it from the **🧾 پرداخت‌ها** panel. Uploading a receipt grants no credit.

## 9. Troubleshooting

* **Bot starts, then disappears after some minutes.** The host is killing
  long-running processes (CloudLinux LVE). The log just stops with no error.
  Lower `MAX_ACTIVE_JOBS`, ask the host to raise CPU/`nproc` limits, or move to a
  VPS. Jobs running at that moment are marked `failed` and the user must resend.
* **`misconfigured` every minute.** Read `data/logs/launcher.err`; the usual
  causes are a wrong token/API id/hash or a missing `.env`.
* **Every upload fails immediately.** Check `df -h ~` and `df -i ~` (disk and
  inodes) and the `data/tmp` permissions.
* **Two replies to every message.** Two bots share a token from *different
  directories*, so each has its own session and lock. Run one copy per token.
