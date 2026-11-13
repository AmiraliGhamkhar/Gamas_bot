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
| Legacy provider settings warning | Removed-provider keys/slots are ignored from both environment and `.env`; configure only Gemini, AvalAI or OpenRouter. Never paste keys into diagnostics |
| Provider order looks wrong after editing `.env` | `--show-config` shows eligible configuration; task policy may reorder primaries, OpenRouter stays last, and sensitivity may exclude it. Clear the legacy `NOTE_*` slot if it is no longer wanted |
| Persian Deepgram error | Use `STT_LANGUAGE=fa` with `DEEPGRAM_MODEL=nova-3` |
| Booklet refused with `reason=unsupported_claims` | A section sentence shares no vocabulary with the lecture (claim-level prose check). Read `note acceptance rejected … unsupported_claims=<n>`; the transcript is kept and the submission is `partial`. Regenerate, or set `NOTE_QUALITY_GATE=warn` to observe only |
| `error_kind=notes_incomplete` but the booklet exists | The bounded repair could not restore every source number/term; the booklet is delivered and the job is `partial` on purpose. Check `note delivered incomplete reason=missing_numbers|missing_terms` |
| Notes never generated for a lecture naming a patient | `auto` classified it `SENSITIVE` (honorific name) so no provider is eligible. Declare `NOTE_DATA_SENSITIVITY=sensitive` together with that provider's approval flag only after review |
| Booklet refused: `error_kind=notes_quality`, user told «تأیید نشد» | The final evidence gate found numbers or terms the source does not contain. The transcript was kept and the submission is `partial`. Read `note acceptance rejected … fabricated=<n>` and the provider's `detail=` lines; never disable `NOTE_STRICT_GROUNDING` to raise the success count |
| Booklet succeeds but a number is missing | Recall is a repair signal, not an acceptance rule: a diagnosed omission is delivered, an unsupported claim is not. Check `note repair left unresolved …` in the log |
| A configured provider was never called | Search `LLM route policy`; `openrouter:blocked_by_unknown` means the source was not classified as shareable, and `blocked_by_sensitivity` means its opt-in flag is false |
| Provider error has no useful reason | Each failure line ends with `detail=`: HTTP status, error code and request id, reduced to printable ASCII. Provider wording that is not ASCII is dropped on purpose, because it may be echoed lecture text. The user-facing text intentionally keeps class names only |
| Job appears stuck in `validating_notes` / `compiling_notes` | Those are real stages, not queue stalls: the note pipeline reports them while it audits evidence and runs the editorial compile. Repair is bounded to two attempts, then the gate decides. `compiling_notes` is only reported when the compile will actually run, so `NOTE_COMPILE=false` never shows it |
| Speechmatics 403 | Two different causes now read differently: «دسترسی حساب … مجاز نیست» means subscription/permission, «اعتبارنامه را نپذیرفت» means the key itself. Both put the provider on cooldown and fail over to the next `STT_PROVIDERS` entry |
| Long provider timeouts on a slow free tier | Keep the conservative output budget; adjust `NOTE_REQUEST_TIMEOUT_SECONDS` only with evidence. Dispatch never hedges, even with a stale nonzero hedge setting |

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
