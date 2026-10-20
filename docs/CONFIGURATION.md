# Configuration for Windows and cPanel

`.env.example` is the full settings list. Copy it to `.env`, fill the required values and leave optional settings commented unless you need them. Keep `.env` private; never commit or send it to support.

## Create `.env`

**Windows PowerShell** (from the repository folder):

```powershell
Copy-Item .env.example .env
notepad .env
```

**cPanel Terminal** (from the project folder, outside `public_html`):

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

## Required values

```dotenv
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
ADMIN_IDS=123456789
SPEECHMATICS_API_KEY=your_stt_key
```

Use a real numeric Telegram user ID for each admin, separated by commas. Configure one STT key whose provider appears in `STT_PROVIDERS`. Add note-provider keys (for example `NARA_API_KEY` or `OPENROUTER_API_KEY`) to get structured booklets; without them, the bot still returns raw transcripts.

## Useful settings

| Setting | What to do |
| --- | --- |
| `STT_PROVIDERS`, `STT_LANGUAGE` | Choose failover order and language; Persian defaults to `fa`. See [STT providers](STT_PROVIDERS.md). |
| `NOTE_PROVIDERS` | Set note-provider order. Each provider needs its own key. See [Note providers](PROVIDERS.md). |
| `MAX_ACTIVE_JOBS`, `MAX_QUEUED_JOBS` | On cPanel, start with `1` active job and a small queue. |
| `MAX_FILE_SIZE` | Lower it to fit the host's upload and temporary-disk limits. |
| `PRESENTATION_MAX_TEMP_BYTES` | Keep it below the available scratch-disk space. |
| `DB_PATH`, `TELEGRAM_SESSION`, `TEMP_DIR`, `RECEIPT_DIR`, `LOG_FILE` | Relative paths resolve from the project directory, not the current shell directory. Keep data outside `public_html`. |
| `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER`, `PAYMENT_BANK_NAME` | Set these to enable card-to-card purchases. Admins review receipts manually. |
| `NOTE_MAX_OUTPUT_TOKENS` | Measure real `output_tokens` p95 from the Phase 0 usage logs (with `ttfb_ms`/`elapsed_ms`) over at least one week, then set cap = p95 + 15% margin (example: 16384 → 8192). Never cut below observed p95 — booklet truncation risk. |
| `NOTE_JSON_MODE` | Enable `true` only when the configured OpenRouter model documents `json_object`; the code already never sends it to Nara. |
| `NOTE_REASONING_EFFORT` | If used, start at `low` (never `minimal` first) and A/B against the quality-gate report before keeping it; only Nara and OpenRouter accept it. |
| `NOTE_CONCURRENCY` | Keep `3` on cPanel; a VPS may use `6-8`. |
| `NOTE_REQUEST_TIMEOUT_SECONDS` | Re-derive after any token-cap change: it must remain greater than the time to generate `NOTE_MAX_OUTPUT_TOKENS` on the slowest configured model. |

## Precedence and checks

A real process environment variable overrides `.env`; otherwise the built-in default is used. Run this after editing configuration:

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe -m gamas_bot --check
```

On cPanel, activate the virtual environment first, then run `python -m gamas_bot --check` and `python -m scripts.cpanel_preflight`. The first check is offline; the preflight tests the host's OS, disk, permissions and outbound network. See [.env.example](../.env.example) for every supported setting and default.
