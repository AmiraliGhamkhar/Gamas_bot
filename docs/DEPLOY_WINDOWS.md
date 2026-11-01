# Run on Windows PowerShell

Use Python **3.11–3.13** and Git. These examples use Python 3.12. Run commands
separately and stop on errors; Windows PowerShell 5.1 does not fail-fast by default.
No virtualenv activation or execution-policy change is needed.

## 1. Install

```powershell
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git
Set-Location Gamas_bot
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Create `.env` only on first installation; do not overwrite existing configuration.
Fill [required values](CONFIGURATION.md). Keep the project private, outside shared,
web-served and cloud-sync folders.

## 2. Check and start

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe -m gamas_bot
```

Fix every `FAIL`; review each `WARN`. The check is offline and does not verify keys
with Telegram/providers. Send `/start` and short audio; confirm the transcript
arrives before any booklet. Stop with **Ctrl+C** and wait for shutdown.

Keep the console open and prevent sleep while testing. Do not run this copy while
the same token/session is active on cPanel.

## 3. Update

Stop with Ctrl+C and [back up](OPERATIONS.md#backups-and-restore) first. Then:

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe -m gamas_bot
```

Stop on Git conflicts/local changes; never reset files to force an update.
Migrations run at startup. A rollback requires matching code and backup data.

## Logs

Foreground logs appear in the console. For rotating files, set
`LOG_FILE=data/logs/bot.log` in `.env` and restart.

```powershell
Get-Content .\data\logs\bot.log -Tail 80 -Wait
```

[Common fixes](TROUBLESHOOTING.md) · [Development tests](TESTING.md)
