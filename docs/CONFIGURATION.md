# Configuration

All settings live in `.env` in the project root. Copy `.env.example` **once**,
then edit it; never replace it during an update.

```powershell
Copy-Item .env.example .env ; notepad .env
```

```bash
cp .env.example .env && chmod 600 .env && nano .env
```

Rules that prevent most support cases:

- `.env` is read **once at startup** into one frozen settings object. Restart the
  bot after every change; a running process keeps the old values.
- Process environment overrides the file. Relative paths resolve from the
  project directory.
- Keep `.env`, `data/` and the session file outside every web root
  (`public_html`, `www`, `htdocs`, `httpdocs`).

## Required

| Setting | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | API credentials from [my.telegram.org](https://my.telegram.org) |
| `ADMIN_IDS` | Comma-separated **numeric** Telegram user IDs (not usernames) |
| One STT key | `SPEECHMATICS_API_KEY`, `DEEPGRAM_API_KEY` or `STT_OPENAI_API_KEY` |

`--check` fails while any of these is missing or empty.

## Optional

| Feature | Settings | Default |
| --- | --- | --- |
| Lecture booklets | a [note-provider key](PROVIDERS.md) | none → transcript only |
| Channel membership | `SUBSCRIPTION_REQUIRED`, `SUBSCRIPTION_CHANNEL` | `true`, `@gamas_AI` |
| Manual purchases | `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER`, `PAYMENT_BANK_NAME` | empty → purchases hidden |
| Free Office | `OFFICE_ENABLED` | `true` |
| Scans and photos | `OCR_ENABLED` + the OCR extra | `false` |
| File logs | `LOG_FILE` | console, or `data/logs/bot.log` when the launcher runs it |
| Mini App button | `MINI_APP_URL` | empty → no button |
| Free first hour | `FREE_PLAN_HOURS` | `1` hour |

## Tariff

The confirmed tariff is **100 000 credits = 3 hours = 65 000 Toman** (≈ 21 667
Toman per hour). Never set `65000` to `65`.

```dotenv
PLAN_3_PRICE_TOMAN=65000
PLAN_9_PRICE_TOMAN=195000
PLAN_12_PRICE_TOMAN=260000
PLAN_24_PRICE_TOMAN=520000
PLAN_36_PRICE_TOMAN=780000
PLAN_60_PRICE_TOMAN=1300000
CREDIT_PRICE_PER_100K_TOMAN=65000
```

At startup the bot synchronizes only its built-in active plan catalogue; existing
orders, payment snapshots, custom plans and balances are never rewritten. See
[Billing](BILLING.md).

## Note generation (optional)

`NOTE_PROVIDERS` is the failover order; a provider without its own key is skipped.

```dotenv
NOTE_PROVIDERS=gemini,openrouter
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
```

Shipped defaults are `gemini,nara,openrouter,deepseek,avalai` with
`NOTE_MAX_OUTPUT_TOKENS=16384` and structured output on. A verified deployment
profile (2026-11-07) is documented in [Providers](PROVIDERS.md#tested-profile) —
it is a recommendation, not a guarantee for your account.

## Mini App

```dotenv
MINI_APP_URL=https://studio.example.com     # https, no path, no trailing slash
MINI_APP_AUTH_MAX_AGE_SECONDS=300           # 30..3600
MINI_APP_STATIC_DIR=                        # empty → ./mini_app
```

Empty `MINI_APP_URL` hides every app button. Deployment:
[cPanel](MINIAPP_CPANEL.md) · [Cloudflare](MINIAPP_CLOUDFLARE.md).

## Paths

| Setting | Default |
| --- | --- |
| `DB_PATH` | `data/gamas_v3.db` |
| `TELEGRAM_SESSION` | `data/gamas` (file `data/gamas.session`) |
| `TEMP_DIR` | `data/tmp` |
| `RECEIPT_DIR` | `data/receipts` |
| `LOG_FILE` | unset → console; launcher child uses `data/logs/bot.log` |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `plain` (`json` available) |
| `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` | 5 MiB, 5 files |

## Safe limits (ship defaults first)

| Group | Default |
| --- | --- |
| Jobs | `MAX_ACTIVE_JOBS=3`, `MAX_QUEUED_JOBS=8`, `MAX_ACTIVE_JOBS_PER_USER=2` |
| Transfers | `MAX_CONCURRENT_DOWNLOADS=2`, `MAX_CONCURRENT_UPLOADS=2`, `TELEGRAM_DOWNLOAD_CONNECTIONS=1` |
| Media | `MAX_FILE_SIZE=134217728` (128 MiB), `MAX_MEDIA_DURATION_SECONDS=7200`, `MEDIA_WORKER_CONCURRENCY=1` |
| Documents | `DOC_MAX_BYTES=20971520` (20 MiB), `DOC_UNPACKED_MAX_BYTES=104857600` |
| PowerPoint narration | `PRESENTATION_MAX_CLIPS=50`, `PRESENTATION_MAX_TOTAL_DURATION=7200`, `PRESENTATION_MAX_UNPACKED_BYTES=134217728` |
| Office | `OFFICE_MAX_CHARS=100000`, `OFFICE_MAX_INPUT_BYTES=1000000`, `OFFICE_MAX_SLIDES=30`, `OFFICE_SESSION_TTL_MINUTES=30`, `OFFICE_JOB_CONCURRENCY=2` |
| STT slots | `SPEECHMATICS_MAX_IN_FLIGHT=2`, `DEEPGRAM_MAX_IN_FLIGHT=4`, `STT_OPENAI_MAX_IN_FLIGHT=2`, `STT_PROVIDER_TIMEOUT_SECONDS=1800` |
| Note slots | `NOTE_CONCURRENCY=3`, `LLM_PRIMARY_MAX_IN_FLIGHT=4`, `LLM_FALLBACK_MAX_IN_FLIGHT=4`, `NOTE_REQUEST_TIMEOUT_SECONDS=900` |
| Learning | `LEARNING_GLOBAL_CONCURRENCY=2`, `LEARNING_CHUNK_CONCURRENCY=4`, `LEARNING_MAX_OUTPUT_TOKENS=4096` |
| Retention | `RECEIPT_RETENTION_DAYS=90`; `TRANSCRIPT_RETENTION_DAYS`, `DOCUMENT_RETENTION_DAYS`, `LEARNING_RETENTION_DAYS`, `AUDIT_RETENTION_DAYS`, `HOLDS_RETENTION_DAYS` = `0` (keep) |
| Support | `SUPPORT_MAX_OPEN_TICKETS=3`, `SUPPORT_TICKET_COOLDOWN_SECONDS=600` |

Overflow is refused, never silently truncated. Raising a cap does not raise the
host's RAM; see [host sizing](OPERATIONS.md#host-sizing).

## Validate

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe -m scripts.probe_note_providers --show-config
```

```bash
python -m gamas_bot --check
python -m scripts.cpanel_preflight
python -m scripts.probe_note_providers --show-config
```

Fix every `FAIL`, review every `WARN`. Both commands are offline and
credential-free; they do not prove that a live key, model or network path works.
Provider probes are described in [Providers](PROVIDERS.md).
