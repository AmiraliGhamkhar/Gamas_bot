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

## 2. Confirm the production tariff

The confirmed paid tariff is **100,000 credits = 3 hours = 65,000 Toman**.
It is not 65 Toman. For a new deployment, the defaults in `.env.example` already
match; on an existing deployment set these exact values in `.env`:

```dotenv
PLAN_3_PRICE_TOMAN=65000
PLAN_9_PRICE_TOMAN=195000
PLAN_12_PRICE_TOMAN=260000
PLAN_24_PRICE_TOMAN=520000
PLAN_36_PRICE_TOMAN=780000
PLAN_60_PRICE_TOMAN=1300000
```

Do not edit historical payments, custom plans, balances, credit lots or refunds.
At the next startup GAMAS safely synchronizes only its built-in active catalogue;
existing orders retain their frozen price snapshots.

## 3. Check and start

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe -m gamas_bot
```

Fix every `FAIL`; review each `WARN`. The check is offline and does not verify keys
with Telegram/providers. Send `/start` and short audio; confirm the transcript
arrives before any booklet. Stop with **Ctrl+C** and wait for shutdown.

Note-provider keys and models are optional and configured in `.env`; the shipped
defaults are not a deployment recommendation. See
[Configuration](CONFIGURATION.md#note-provider-configuration) and
[Note-generation providers](PROVIDERS.md) for the tested two-route profile. To
inspect the effective order and non-secret settings offline, run
`.\.venv\Scripts\python.exe -m scripts.probe_note_providers --show-config`.
Settings are read only at startup, so stop and restart the bot after any `.env`
change; a real generation probe is opt-in (`--generate`) and consumes quota.

Keep the console open and prevent sleep while testing. Do not run this copy while
the same token/session is active on cPanel.

## 4. Update

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
