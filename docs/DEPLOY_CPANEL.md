# Deploy on cPanel shared hosting

Gamas Bot is a long-running Telegram client, not a website. cPanel must allow outbound connections and a cron job to keep the process alive. Shared hosts may terminate background work; if the checks below fail, use a VPS instead.

## 1. Check the hosting plan

You need cPanel Terminal or SSH, Python 3.11+, Cron Jobs, outbound TCP 443 to Telegram and your API providers, writable disk space, and permission to run a background Python process. The project and all private data must be **outside `public_html`**.

## 2. Upload and install

In cPanel Terminal, clone the repository (or upload and extract a source ZIP outside the web root):

```bash
cd "$HOME"
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git gamas_bot
cd "$HOME/gamas_bot"
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Use the Python 3.11+ command provided by your host if `python3.11` is named differently. If you use cPanel **Setup Python App**, install the requirements into the virtual environment shown by cPanel. Passenger is optional; the cron watchdog below is still required.

## 3. Configure and test

```bash
cd "$HOME/gamas_bot"
cp .env.example .env
chmod 600 .env
nano .env
python -m scripts.cpanel_preflight
python -m gamas_bot --check
```

Set the Telegram token, API ID/hash, numeric `ADMIN_IDS`, and at least one STT provider key. Note-provider keys are optional. The preflight checks packages, file permissions, disk, the lock, Telegram and configured API connectivity. Fix every **FAIL**; a `WARN` needs review. Start once in the foreground, send a test message, then stop with **Ctrl+C**:

```bash
python -m gamas_bot
```

For shared hosting, start with `MAX_ACTIVE_JOBS=1` and a small `MAX_QUEUED_JOBS`. Set upload and temporary-storage limits to fit your plan. See [Configuration](CONFIGURATION.md).

## 4. Add the cron watchdog

In **cPanel → Cron Jobs**, run this once per minute. Replace `USER` and paths with the real absolute paths:

```text
* * * * * /home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

For a cPanel Python App virtualenv, use its Python executable instead, for example `/home/USER/virtualenv/gamas_bot/3.11/bin/python`. The watchdog starts the bot only when it is not running and throttles repeated failed starts. Logs are in `data/logs/bot.log` and `data/logs/launcher.err` unless `LOG_FILE` is set.

## 5. Updates and backups

Before an update, stop the bot, back up its data, then update the source and dependencies:

```bash
cd "$HOME/gamas_bot"
git pull
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m gamas_bot --check
```

Restore the cron job or let the existing job start the bot after it is stopped. Back up `.env`, `data/gamas_v3.db`, the Telegram session (`data/gamas.session`) and `data/receipts/`. Stop the bot before copying the SQLite database, or use a SQLite-aware backup. Keep backups private and encrypted.

## If it fails

- **Project location / receipt directory FAIL:** move the project and receipt data outside every web root.
- **Telegram connectivity FAIL:** ask the host to allow outbound TCP 443.
- **Old OS or PyAV install failure:** ask for a supported Linux image/Python wheel or use a VPS.
- **Process disappears without a Python error:** the shared-host CPU, memory or process policy is killing it. Lower concurrency and upload limits, or move to a VPS.
- **Bot stops responding after a second copy starts:** run only one bot process per token/session.

cPanel hosting layouts vary; the preflight is the final check for your account. Do not expose `.env`, `data/`, logs or backups through the web root.
