# Troubleshooting

Always use the interpreter that runs the bot, from the repository root.

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
```

```bash
python -m gamas_bot --check
python -m scripts.cpanel_preflight
python scripts/ensure_running.py status
tail -n 80 data/logs/launcher.err
```

## Startup and host

| Symptom | Fix |
| --- | --- |
| `py`/`python` not found | Install Python 3.11–3.13, reopen the shell; on cPanel use the host's interpreter path |
| PowerShell blocks activation | Call `.\\.venv\\Scripts\\python.exe` directly — no policy change needed |
| Missing packages | Reinstall `requirements.txt` with the **same** interpreter |
| `pip install` fails on `av`/`lxml` | 64-bit Python 3.11–3.13; old cPanel Linux needs CloudLinux/AlmaLinux 8+ |
| `--check` prints `FAIL` | Fill Telegram credentials, numeric `ADMIN_IDS`, one STT key |
| `misconfigured` from the launcher | The same failures as `--check`; read `data/logs/launcher.err` |
| `throttled` | A failed start happened < 45 s ago; fix the cause, then `--force` |
| Exit code `3` / duplicate instance | Another copy holds the session lock; stop it, never delete the lock |
| Process vanishes without traceback | Host CPU/RAM/process limit; lower jobs/media/note concurrency or change hosting |
| Cron does nothing | Absolute interpreter and script paths; check `launcher.err` |
| Bot restarts during maintenance | Cron / scheduled task / uptime monitor still enabled — pause them |
| Permission or path failure | Private paths outside web roots; `chmod 600 .env`, `chmod 700 data` |

## Telegram and delivery

| Symptom | Fix |
| --- | --- |
| No response to `/start` | Wrong token, no network, competing instance, or membership gate — check the log |
| Membership prompt loops | Bot must be a channel **admin**; verify a non-admin account; `SUBSCRIPTION_CHANNEL` must resolve |
| Members blocked wrongly | Infrastructure errors fail open and alert admins — check those alerts and the bot's admin role |
| Button opens a blank app | `MINI_APP_URL` not HTTPS, has a path, or the app is down ([cPanel](MINIAPP_CPANEL.md) / [Cloudflare](MINIAPP_CLOUDFLARE.md)) |
| Upload refused | Over `MAX_FILE_SIZE`/`MAX_MEDIA_DURATION_SECONDS`; unmeasurable audio is refused before charging |
| Transcript arrives, no booklet | No note provider key, or provider/quality failure — the transcription stays charged by design |

## Gamas Studio Mini App

For root-domain cPanel/Passenger checks and copy-paste tests, see
[Mini App operations](MINIAPP_CPANEL.md). Start by distinguishing the static
shell, Telegram SDK, authenticated API, and database readiness:

| Symptom | Diagnostic action |
| --- | --- |
| Slow/blank launch | On the same phone/network measure `/`, `/static/app.css`, `/static/app.js`, `/health`, and `/ready`; inspect console and Network status/bytes. Compare Wi-Fi with mobile data. |
| Telegram SDK unavailable | Visit `https://telegram.org/js/telegram-web-app.js` independently on the affected network, retry in the app, and compare other networks. SDK reachability is not proven by a desktop result. |
| SDK loads but says open from Telegram | Use the BotFather Menu Button or a Web App button. A normal browser has no signed `initData` and must not be authenticated. |
| Auth/API error | Reopen to refresh the signed payload; check 401 vs network/5xx and note the displayed request ID. Never copy or log `initData`. |
| API slow or times out | Use `?diagnostics=1`, compare browser timing with WSGI request ID/duration logs, and inspect `/ready`. A healthy `/health` alone only confirms the DB file exists. |
| JavaScript static reset | Repeat full GETs and compare exact response sizes/status on the same route. HEAD does not test the JS body; Range responses of 200 are full responses, not range support. |
| Passenger 500/502/503 | Confirm Setup Python App startup file/callable, Python 3.12 environment, cPanel error log, DB path and read permission. Restart only the existing Mini App via its cPanel control; do not start/stop the bot. |
| Studio empty or save says migration gap | Confirm same DB as the bot and read-only migration markers 027/028. Do not run SQL manually or replace the live database. |
| Export fails | Check owner-scoped API response and available persisted artifact; do not blindly retry a save/restore. Use the response request ID in logs. |

Diagnostics display only SDK capabilities and API metadata. Logs must never
include the full signed payload, secrets or private user content.

## Providers

| Symptom | Fix |
| --- | --- |
| `401`/`403`/`402`/`429` | Key, entitlement, balance or rate limit; configure a fallback instead of raising limits blindly |
| `finish_reason=length` / empty booklet | Output budget exhausted; the answer is rejected, not delivered. Check `NOTE_MAX_OUTPUT_TOKENS`, restart |
| Old process sends 16 384 tokens although `.env` says 32 768 | Settings load at startup only; compare the log's `requested_output_tokens` with `--show-config`, then restart |
| Metadata probe passes, generation fails | Different checks — only `--generate` proves generation, and it costs quota |
| Gemini HTTP 400 `INVALID_ARGUMENT` with structured output | Set `NOTE_STRUCTURED_OUTPUT=0` (tested profile), restart, re-probe; one bounded schema-dropping retry exists |
| NaraRouter HTTP 502 | Observed for `laguna-s-2.1`; remove `nara` from `NOTE_PROVIDERS` until a real generation probe passes |
| Provider order looks wrong after editing `.env` | `--show-config` shows the effective chain; blank `NOTE_API_KEY`/`NOTE_BASE_URL` avoid an extra legacy route |
| Persian Deepgram error | Use `STT_LANGUAGE=fa` with `DEEPGRAM_MODEL=nova-3` |
| Long provider timeouts on a slow free tier | Lower `NOTE_MAX_OUTPUT_TOKENS`, set `NOTE_HEDGE_AFTER_SECONDS=0`, raise `NOTE_REQUEST_TIMEOUT_SECONDS` with evidence |

## Documents, Office and OCR

| Symptom | Fix |
| --- | --- |
| `.doc` refused | Save as `.docx`; legacy `.ppt`/`.pps`/`.pot` is converted automatically |
| Large source refused | Split it; limits refuse overflow instead of truncating |
| Scan or photo refused | OCR is off by default; read [OCR](FEATURES.md#ocr-opt-in-third-party) before enabling uploads |
| Word layout or font looks wrong | Install Vazirmatn from `assets/fonts/`; check in Microsoft Word |
| Excel cell shows a formula | Values are literal strings by design; nothing is calculated |
| Narration never starts | It needs the explicit **🎧 پردازش صدای ارائه** button, not the upload |

## Billing and accounts

| Symptom | Fix |
| --- | --- |
| Receipt uploaded, no credit | A human admin must approve the order; see the wallet history |
| Balance differs from the sum of history | Ledger is authoritative; the cache refreshes on read after the next expiry — report it, do not edit rows |
| Paid generation failed, no refund | Check the pending charge; refunds are automatic once, recoverable at startup |
| Quiz/flashcard refused | Admission caps: one active quiz, 3 sessions/hour, 30 decks — or insufficient credit |

## What to include in a support request

Failing command, Python version, redacted error, `GMS-#####` job reference and
the UTC timestamp. Never send `.env`, sessions, receipts, keys or transcript text.

Logs and backups: [Operations](OPERATIONS.md) · settings:
[Configuration](CONFIGURATION.md) · providers: [Providers](PROVIDERS.md).
