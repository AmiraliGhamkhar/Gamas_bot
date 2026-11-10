# Run on Windows PowerShell

Python **3.11–3.13**, 64-bit. The examples use Python 3.12 and a project
virtualenv; no activation or execution-policy change is needed. Run each command
separately and stop on the first error — PowerShell 5.1 does not fail fast.

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

Install somewhere private, for example `C:\gamas\Gamas_bot` — not inside a
OneDrive/Dropbox folder or any folder shared with other accounts. Create `.env`
only once; never overwrite it during updates.

## 2. Fill `.env`

```dotenv
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
ADMIN_IDS=111111111
SPEECHMATICS_API_KEY=...
```

`ADMIN_IDS` is numeric Telegram user IDs, not usernames. One STT key is enough.
Optional keys, the tariff and every limit: [Configuration](CONFIGURATION.md)
and [Providers](PROVIDERS.md). Restart after **every** `.env` change.

## 3. Check, then run

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe -m gamas_bot
```

`--check` is offline: it verifies packages, configuration, paths and the
effective provider order, but not live keys. Fix every `FAIL`, review every
`WARN`. Then send `/start` and a short voice note: the transcript must arrive
before any booklet. Stop with **Ctrl+C** and wait for shutdown.

## 4. Run in the background

```powershell
.\.venv\Scripts\python.exe scripts\ensure_running.py start
.\.venv\Scripts\python.exe scripts\ensure_running.py status
.\.venv\Scripts\python.exe scripts\ensure_running.py stop
.\.venv\Scripts\python.exe scripts\ensure_running.py restart
```

`status` prints `running (pid=…)`, `stopped` or `misconfigured`; `start` prints
`started`, `running`, `throttled` (a start attempt happened less than 45 s ago),
`failed` or `misconfigured`. The launcher writes the child's log to
`data\logs\bot.log` and startup failures to `data\logs\launcher.err`. Never
start a second copy: the session lock exits with code 3.

## 5. Keep it running after a reboot (Task Scheduler)

```powershell
$py     = "$PWD\.venv\Scripts\python.exe"
$script = "$PWD\scripts\ensure_running.py"
schtasks /Create /SC MINUTE /MO 1 /TN "GamasBot" /TR "`"$py`" `"$script`"" /F
Start-ScheduledTask -TaskName "GamasBot"
Get-ScheduledTask -TaskName "GamasBot"
```

The task re-checks the process once a minute and starts it if it is missing.
Task Scheduler runs it only while the account is logged on unless you edit the
task and store the password ("Run whether user is logged on or not"). Remove it
before maintenance with `Unregister-ScheduledTask -TaskName "GamasBot" -Confirm:$false`,
or disable it with `Disable-ScheduledTask -TaskName "GamasBot"`.

## 6. Logs

```powershell
Get-Content .\data\logs\bot.log -Tail 80 -Wait
Get-Content .\data\logs\launcher.err -Tail 40
```

`LOG_FILE` in `.env` overrides the file; foreground runs log to the console.
Rotation defaults: 5 MiB × 5 files.

## 7. Update

```powershell
.\.venv\Scripts\python.exe scripts\ensure_running.py stop
# back up .env and data\ first (see Operations)
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe scripts\ensure_running.py --force
```

Stop on Git conflicts or local changes; never reset files to force an update.
Migrations run at startup. Rolling back requires matching code **and** the backup
taken before the update.

## Troubleshooting (short)

| Symptom | Fix |
| --- | --- |
| `py` not found | Install Python 3.11+ from python.org, reopen PowerShell |
| Script execution blocked | Call `.\\.venv\\Scripts\\python.exe` directly, as above |
| Missing packages | Re-run pip install with **the same** interpreter |
| `--check` reports `FAIL` | Fill Telegram credentials, numeric `ADMIN_IDS`, one STT key |
| No answer in Telegram | Check logs, network and that only one instance runs |
| PyAV/lxml wheel fails | 64-bit Python 3.11–3.13; avoid 32-bit Python |

More: [Troubleshooting](TROUBLESHOOTING.md) · [Operations](OPERATIONS.md).
