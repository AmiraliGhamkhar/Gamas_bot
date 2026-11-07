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
| Gemini HTTP 400 `INVALID_ARGUMENT` with structured output | The 2026-11-07 test succeeded only with `NOTE_STRUCTURED_OUTPUT=0`; the exact rejected field was not isolated. Set that value and `NOTE_GEMINI_THINKING_BUDGET=-1`, restart, then re-run the generation probe. The client also has one bounded schema-dropping retry for this error class. [Providers](PROVIDERS.md) |
| `finish_reason=length` / `MAX_TOKENS`, empty or cut-off booklet | The answer hit the output budget and is deliberately rejected, not delivered as a partial note. Check `requested_output_tokens` in the log, confirm `NOTE_MAX_OUTPUT_TOKENS`, and restart after any change before assuming truncation. |
| Old process sends 16,384 tokens although `.env` says 32,768 | Settings load only at startup; a stale process or a `.env` that was never reloaded is a plausible cause, not a proven one. Compare the log's `requested_output_tokens` with `--show-config`, then stop/start with `scripts/ensure_running.py`. |
| NaraRouter HTTP 502 on generation (including with structured output and reasoning off) | Observed for `laguna-s-2.1` on 2026-11-07: three attempts plus a second test failed. Entitlement/metadata PASS is not a generation guarantee. Remove `nara` from `NOTE_PROVIDERS` until a real generation probe succeeds; keep the `NARA_*` settings for later. Include the safe `request_id` in a support report. |
| Metadata probe passes but generation fails | These are different checks. Only `--generate` verifies generation; see [Providers](PROVIDERS.md#how-to-test-a-provider-independently). |
| Provider order/keys look wrong after editing `.env` | Run `python -m gamas_bot --check` or `python -m scripts.probe_note_providers --show-config` (both credential-free). Leave `NOTE_API_KEY`/`NOTE_BASE_URL` blank to avoid an extra legacy `openai_compatible` leg, and restart the bot. |
| Persian Deepgram error | Use `STT_LANGUAGE=fa` with `DEEPGRAM_MODEL=nova-3`. |
| Scan/photo refused | OCR is off by default. Read [OCR](OCR.md) before enabling remote uploads. |
| Word layout/font issue | Install bundled Vazirmatn on the viewing PC; check in Microsoft Word. |
| Receipt but no credit | A human admin must approve the order. Check Wallet history. |
| Membership behaves unexpectedly | Check channel reference, bot admin role and settings. Infrastructure errors deliberately fail open. |
| Large document/export refused | Split the source; limits refuse overflow instead of truncating. |

For help, collect the failing command, Python version, redacted error, job reference
and timestamp. Never send `.env`, sessions, receipts or private text.
[Logs/backups](OPERATIONS.md) · [Configuration](CONFIGURATION.md)
