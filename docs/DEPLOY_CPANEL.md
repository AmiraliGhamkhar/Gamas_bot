# Deploying Gamas Bot on a cPanel host

> **Read this first.** Gamas Bot is a Telegram bot, not a website. It keeps one
> permanent outbound connection to Telegram and has no pages, no HTTP API and no
> frontend. cPanel is built to serve web requests, so the bot has to be started
> *next to* the web hosting, not through it. This guide does that with a
> once-a-minute cron watchdog (primary) and an optional Passenger status URL
> (secondary). It works only on cPanel plans that satisfy the checklist below —
> if your plan does not, the honest answer is a small VPS (see
> [`DEPLOY_FA.md`](DEPLOY_FA.md)).
>
> This procedure was verified against the code and the test-suite on Linux. It
> has **not** been run on a real cPanel account; step 5 (`cpanel_preflight.py`)
> is there to prove your host before you rely on it.

## 1. Can your plan run it? (checklist)

| Requirement | Why | How to check |
|---|---|---|
| Terminal/SSH access | install packages, run the preflight | cPanel → *Terminal* |
| **Cron Jobs** | restarts the bot when the host kills it | cPanel → *Cron Jobs* |
| Python **3.11+** (*Setup Python App* or CloudLinux alt-python) | code base requirement | `python3.11 --version` |
| glibc **2.28+** (CloudLinux/AlmaLinux/Rocky **8+**) | `av` (PyAV) and `lxml` ship manylinux wheels that need it; on CloudLinux 7 they cannot be installed | preflight prints it |
| Outbound TCP **443** to Telegram and your STT / note-API hosts | MTProto and the provider APIs | preflight tests it |
| Background processes allowed for more than a few minutes | the bot is a long-running process; some hosts kill anything over a CPU/time budget (CloudLinux LVE, "entry/nproc" limits) | watch `data/logs/bot.log` after step 8 |
| ~1–2 GB RAM and several GB of free disk / inodes | audio/video decoding and temp files (up to 2 GB per upload) | cPanel → *Resource Usage* |

Also know:

* **Application code and data must live outside `public_html`.** `.env`,
  `data/bot.sqlite3` and the Telegram session must never be downloadable.
  The preflight fails when the project is inside a web root.
* Telegram is filtered in some countries/data-centres. If the preflight cannot
  reach it, set `TELEGRAM_PROXY` (SOCKS5/HTTP). **Only Telegram traffic uses the
  proxy** — the Speechmatics/Deepgram/note-API calls go out directly, so those
  hosts must be reachable from the server.
* Use `MAX_CONCURRENT_JOBS=1` on shared hosting, and keep the pending queue
  small (`MAX_PENDING_JOBS=2`): at most `MAX_CONCURRENT_JOBS` jobs run and
  at most `MAX_PENDING_JOBS` wait; anything beyond that is rejected with a
  "bot is busy" message instead of piling up in memory.

## 2. Upload the code

Over SSH (recommended):

```bash
cd ~
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git gamas_bot
```

Without git, upload and extract a ZIP into `/home/USER/gamas_bot` (File Manager).
Do **not** put it under `public_html`.

## 3. Create the Python environment

**Option A — *Setup Python App* (gives you a virtualenv and the optional status URL):**

1. cPanel → *Setup Python App* → *Create Application*.
2. Python version **3.11 or newer**; *Application root* `gamas_bot`;
   *Application URL* e.g. `bot-status` (any sub-path/domain);
   *Startup file* `passenger_wsgi.py`; *Entry point* `application`.
3. Create, then copy the *"Enter to the virtual environment"* command shown at the
   top of the page and run it in *Terminal*. It looks like
   `source /home/USER/virtualenv/gamas_bot/3.11/bin/activate && cd /home/USER/gamas_bot`.

**Option B — plain venv (no status URL):**

```bash
cd ~/gamas_bot && python3.11 -m venv .venv && source .venv/bin/activate
```

Then, in the activated environment:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

(Do not use cPanel's *Run Pip Install* button with a big requirements file if it
times out; the terminal has no such limit.)

## 4. Configure

```bash
cd ~/gamas_bot
cp .env.example .env
chmod 600 .env
nano .env
```

Minimum: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ADMIN_IDS`,
one STT key, and (optionally) the note-API key. Recommended for shared hosting:

```dotenv
MAX_CONCURRENT_JOBS=1
MAX_PENDING_JOBS=2
PROGRESS_ANIMATION_ENABLED=false   # fewer Telegram edits, less CPU
# TELEGRAM_PROXY=socks5://user:pass@proxy.example.com:1080
```

Relative paths (`data/...`) and `.env` are resolved from the **project
directory**, not from the shell's current directory, so cron and Passenger see the
same database and session as your terminal.

## 5. Run the preflight

```bash
python scripts/cpanel_preflight.py
```

Fix every `FAIL`. Typical messages:

| Message | Meaning / fix |
|---|---|
| `glibc < 2.28` | host OS too old for the wheels → ask for CloudLinux/AlmaLinux 8+ or use a VPS |
| `inside the web root` | move the project out of `public_html` |
| `cannot open outbound TCP 443 to Telegram` | ask the host to allow it, or set `TELEGRAM_PROXY` |
| `Speechmatics/Deepgram/Note API … unreachable` | outbound firewall/DNS; the app does not proxy these |
| `file locking is not enforced` | data directory is on a filesystem without `flock`; point `TELEGRAM_SESSION_PATH`, `DATABASE_PATH`, `TEMP_DIR` to a local disk |
| `Media worker` fails | `pip install -r requirements.txt` did not finish |

## 6. First run in the foreground

```bash
python -m gamas_bot
```

You should see `Persian study assistant is online username=@…`. Send the bot a
message on Telegram, then stop it with `Ctrl+C` (shutdown is graceful; running jobs
are marked as interrupted).

## 7. Keep it alive with cron

cPanel → *Cron Jobs* → *Once Per Minute* (`* * * * *`). Command — use **absolute
paths** of the Python from step 3:

```text
/home/USER/virtualenv/gamas_bot/3.11/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

(Option B: `/home/USER/gamas_bot/.venv/bin/python …`.)

What it does every minute, in a few tens of milliseconds: if the bot holds its
lock nothing happens; otherwise it starts one detached bot process, at most once
per 45 seconds, so a bot that crashes at start-up (wrong token, no network) is
retried politely instead of hammered. Only one bot can ever run — a second
process exits immediately with code 3 ("Not starting").

Check it:

```bash
python scripts/ensure_running.py     # prints: running | started | throttled | misconfigured
tail -f ~/gamas_bot/data/logs/bot.log
cat ~/gamas_bot/data/logs/launcher.err   # only non-empty if Python itself failed to start
```

## 8. Optional: status URL + second watchdog (Passenger)

If you used *Setup Python App*, opening its URL returns

```json
{"service": "gamas-bot", "status": "running"}
```

(HTTP 200; 503 with `starting` or `error` otherwise; nothing else is exposed).
Each request also makes sure the bot is running, so an external monitor such as
UptimeRobot hitting that URL every 5 minutes is a second, independent watchdog.
The cPanel *Restart* button restarts only this status app, **not** the bot.

## 9. Operations

| Task | Command |
|---|---|
| Restart the bot (graceful; cron starts it again within a minute) | `pkill -TERM -u "$USER" -f 'python.* -m gamas_bot$'` |
| Stop it permanently | remove the cron line, then the `pkill` above |
| Update | `cd ~/gamas_bot && git pull && pip install -r requirements.txt`, then restart |
| Logs | `data/logs/bot.log` (rotated, 10 MB × 5). Set `LOG_FILE` in `.env` to choose another path |
| Backup | `data/bot.sqlite3` (plus `-wal`/`-shm` while running) and `data/telegram_bot.session`. Stop the bot or use `sqlite3 data/bot.sqlite3 ".backup backup.sqlite3"` |
| Disk cleanup | `data/tmp` is emptied automatically at start-up and after each job |

The database is created and migrated automatically on first start.

## 10. Troubleshooting

* **Bot starts, then disappears after some minutes.** The host is killing
  long-running processes (CloudLinux LVE limits). `data/logs/bot.log` will simply
  stop without an error. Lower `MAX_CONCURRENT_JOBS`, disable
  `PROGRESS_ANIMATION_ENABLED`, ask the host to raise CPU/`nproc` limits — or move
  to a VPS. Cron will keep restarting it, but jobs running at that moment are
  lost (they are marked `failed` and the user must resend), and jobs still
  waiting in the queue are rejected with an explanation at shutdown.
* **`status: starting` forever.** Read `data/logs/launcher.err` and
  `data/logs/bot.log`; the usual causes are a wrong token/API id/hash or blocked
  Telegram access.
* **Every upload fails immediately.** Check `df -h ~` and `df -i ~` (disk and
  inodes) and the `data/tmp` permissions.
* **Two replies to every message.** Two bots share a token from *different
  directories* (each has its own session/lock). Run only one copy per token.
