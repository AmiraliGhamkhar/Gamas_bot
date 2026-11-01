# Configuration

Copy [`.env.example`](../.env.example) to `.env` once, then edit it. Never replace
`.env` during updates. Process environment overrides the file; relative paths
resolve from the project directory. Restart after changes.

## Required values

| Setting | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | API credentials from my.telegram.org |
| `ADMIN_IDS` | Comma-separated numeric Telegram user IDs, not usernames |
| STT key | At least one of `SPEECHMATICS_API_KEY`, `DEEPGRAM_API_KEY`, `STT_OPENAI_API_KEY` |

Use real values, not placeholders. Keep secrets out of Git, logs and support messages.

## Optional features

| Feature | Configuration |
| --- | --- |
| Lecture booklets | Add a [note-provider key](PROVIDERS.md); audio otherwise delivers transcript only |
| Channel membership | Default `SUBSCRIPTION_REQUIRED=true`, channel `@gamas_AI`; make the bot a channel admin. [Guide](TELEGRAM_SUBSCRIPTION.md) |
| Manual purchases | Set `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER`, `PAYMENT_BANK_NAME` |
| Free Office | `OFFICE_ENABLED=true`; deterministic fallback works without note keys |
| Scans/photos | Default `OCR_ENABLED=false`; [install the OCR extra](OCR.md) before enabling |
| File logs | Set `LOG_FILE=data/logs/bot.log`; foreground otherwise uses stdout |

## Safe starting limits

Keep shipped defaults unless measurements justify changes.

| Settings | Defaults |
| --- | --- |
| `MAX_ACTIVE_JOBS`, `MAX_QUEUED_JOBS`, `MAX_ACTIVE_JOBS_PER_USER` | 3 / 8 / 2 |
| `MAX_CONCURRENT_DOWNLOADS`, `MEDIA_WORKER_CONCURRENCY`, `MAX_CONCURRENT_UPLOADS` | 2 / 1 / 2 |
| `MAX_FILE_SIZE`, `MAX_MEDIA_DURATION_SECONDS` | 134217728 bytes (128 MiB) / 7200 seconds |
| `DOC_MAX_BYTES`, `TEMP_MIN_FREE_BYTES` | 52428800 bytes (50 MiB) / 67108864 bytes (64 MiB) |
| `SPEECHMATICS_MAX_IN_FLIGHT`, `DEEPGRAM_MAX_IN_FLIGHT`, `STT_OPENAI_MAX_IN_FLIGHT` | 2 / 4 / 2 |
| `STT_PROVIDER_TIMEOUT_SECONDS`, `STT_SLOT_WAIT_TIMEOUT_SECONDS` | 1800 / 600 seconds |
| `LLM_PRIMARY_MAX_IN_FLIGHT`, `LLM_FALLBACK_MAX_IN_FLIGHT` | 4 / 4 slots |
| `LLM_SLOT_WAIT_TIMEOUT_SECONDS` | 300 seconds |
| `NOTE_CONCURRENCY` | 3 requests |
| `NOTE_HEDGE_AFTER_SECONDS` | 60 seconds (0 disables hedging) |
| `NOTE_STRICT_GROUNDING`, `NOTE_QUALITY_GATE`, `NOTE_COMPLETENESS_FILL` | true / repair / true |
| `OFFICE_MAX_CHARS`, `OFFICE_MAX_INPUT_BYTES`, `OFFICE_MAX_SLIDES` | 100000 / 1000000 / 30 |
| `OFFICE_SESSION_TTL_MINUTES` | 30 minutes |
| `OFFICE_JOB_CONCURRENCY`, `OFFICE_MIN_INTERVAL_SECONDS` | 2 builds / 10 seconds |
| `LIBRARY_MAX_ITEMS` | 50 references/user; active-quiz sources may be retained longer |
| `RECEIPT_RETENTION_DAYS`, `TRANSCRIPT_RETENTION_DAYS` | 90 / 0 (keep transcript text forever) |

The template lists additional model, timeout, tariff and presentation settings.
`NOTE_MODE` is a legacy no-op; audio booklets use full mode. Compact document
notes are separate. See [Billing](BILLING.md) for learning prices.

## Private paths

Defaults: `DB_PATH=data/gamas_v3.db`, `TELEGRAM_SESSION=data/gamas`,
`TEMP_DIR=data/tmp`, `RECEIPT_DIR=data/receipts`. The session file is normally
`data/gamas.session`. Keep all paths outside web roots.

## Validate

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
```

**cPanel Terminal** (virtualenv active):

```bash
python -m gamas_bot --check
python -m scripts.cpanel_preflight
```

Fix `FAIL`; review `WARN`. The offline check does not prove live keys, model access
or reachability. Preflight also probes the host/network and creates temporary checks.
