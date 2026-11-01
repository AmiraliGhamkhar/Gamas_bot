# Troubleshooting

Use the interpreter that runs the bot. Start with the offline check.

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
```

**cPanel Terminal** (virtualenv active):

```bash
python -m gamas_bot --check
python -m scripts.cpanel_preflight
python scripts/ensure_running.py status
```

| Problem | Action |
| --- | --- |
| Python/`py` not found | Install Python 3.11–3.13 and reopen PowerShell; on cPanel use the host's interpreter path. |
| PowerShell blocks activation | Call `.\.venv\Scripts\python.exe` directly; no execution-policy change needed. |
| Missing packages | Install `requirements.txt` with the same interpreter used to run the bot. |
| PyAV/lxml wheel fails | Use supported Python/OS; old cPanel Linux may need CloudLinux/AlmaLinux 8+. |
| Configuration `FAIL` | Fill Telegram credentials, numeric `ADMIN_IDS` and at least one STT key. |
| No response | Check token, network, logs and status; stop any competing instance. |
| Duplicate instance / exit 3 | Stop the other copy; do not remove its active lock. |
| Process disappears without traceback | Check hosting limits; lower concurrency/file limits or change hosting. |
| Cron fails | Use absolute interpreter/script paths and once-per-minute scheduling; read `data/logs/launcher.err`. |
| Bot restarts during maintenance | Pause cron and Passenger/uptime-monitor triggers. |
| Permission/location failure | Keep private paths outside web roots; on cPanel use `chmod 600 .env` and `chmod 700 data`. |
| Transcript but no booklet | Check note-provider keys and quality/provider errors. Delivered transcription remains charged. |
| Provider 401/403/402/429 | Check key, entitlement, balance and quota; configure fallback rather than blindly raising limits. |
| Persian Deepgram error | Use `STT_LANGUAGE=fa` with `DEEPGRAM_MODEL=nova-3`. |
| Scan/photo refused | OCR is off by default. Read [OCR](OCR.md) before enabling remote uploads. |
| Word layout/font issue | Install bundled Vazirmatn on the viewing PC; check in Microsoft Word. |
| Receipt but no credit | A human admin must approve the order. Check Wallet history. |
| Membership behaves unexpectedly | Check channel reference, bot admin role and settings. Infrastructure errors deliberately fail open. |
| Large document/export refused | Split the source; limits refuse overflow instead of truncating. |

For help, collect the failing command, Python version, redacted error, job reference
and timestamp. Never send `.env`, sessions, receipts or private text.
[Logs/backups](OPERATIONS.md) · [Configuration](CONFIGURATION.md)
