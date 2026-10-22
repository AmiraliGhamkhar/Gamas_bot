# Configuration

`.env.example` lists every setting with its default. Copy it to `.env`, fill the required values,
leave the rest as shipped unless you have a reason to change them. Keep `.env` private.

## Create `.env`

**Windows PowerShell**

```powershell
Copy-Item .env.example .env
notepad .env
```

**cPanel Terminal**

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

## Required values

```dotenv
TELEGRAM_BOT_TOKEN=...
TELEGRAM_API_ID=12345
TELEGRAM_API_HASH=...
ADMIN_IDS=111111111,222222222
SPEECHMATICS_API_KEY=...       # or DEEPGRAM_API_KEY, or the STT_OPENAI_* trio
```

`ADMIN_IDS` are numeric Telegram user IDs. A process environment variable overrides `.env`;
otherwise the built-in default applies. Relative paths resolve from the project directory, so a
cron job cannot create a second database or session.

## Settings that matter most

| Setting | What to do |
| --- | --- |
| `STT_PROVIDERS` | Ordered failover list; the first provider **with a key** serves every job. Default `deepgram,speechmatics,openai_compatible`. |
| `STT_LANGUAGE` | Persian default `fa`. Deepgram requires `DEEPGRAM_MODEL=nova-3` for Persian. |
| `NOTE_PROVIDERS` | Ordered note routes. Providers without their own key are skipped. See [Note providers](PROVIDERS.md). |
| `MAX_ACTIVE_JOBS`, `MAX_QUEUED_JOBS` | Concurrency and queue. On cPanel start with `1` and a small queue. |
| `MAX_ACTIVE_JOBS_PER_USER` | Per-user admission cap (default `2`); stops several uploads the balance cannot cover. |
| `MAX_FILE_SIZE` | Download ceiling (default 2 GiB). Lower it to fit host disk/upload limits. |
| `TEMP_MIN_FREE_BYTES` | Free space that must remain on `TEMP_DIR` before a download (default 2 GiB). |
| `DOWNLOAD_STALL_SECONDS` | Abort a Telegram download with no progress for this long (default `300`). |
| `MEDIA_WORKER_CONCURRENCY` | Heavy media jobs at once. Use `1` on shared hosting. |
| `AUDIO_FORMAT_POLICY` | `auto` (default) picks WAV or Opus from encoder CPU + measured transfer rate; `wav`/`opus` force one. |
| `STT_TRANSFER_TIMEOUT_SECONDS`, `MAX_STT_RESPONSE_BYTES` | One transfer deadline (default `600`) and response ceiling (default 64 MiB). |
| `NOTE_MAX_OUTPUT_TOKENS`, `NOTE_REQUEST_TIMEOUT_SECONDS` | Output budget (default 16384) and request deadline (default 900). Keep the deadline longer than the time the budget needs on the slowest model. |
| `NOTE_CONCURRENCY` | Parallel map requests (default `6`, capped at chunks and 8). Lower it for low-RPM providers. |
| `NOTE_STRUCTURED_OUTPUT` | `1` asks for schema-constrained JSON; a provider that rejects it is retried once without it. |
| `NOTE_PROGRESSIVE_BOOKLET` | `1` delivers the audited merge as a draft while the editorial compile runs. |
| `NOTE_STRICT_GROUNDING`, `NOTE_QUALITY_GATE` | Evidence checks (`true`/`repair` by default). See [Note quality](NOTE_QUALITY.md). |
| `RAW_FALLBACK_FILE_CHARS`, `RAW_FALLBACK_MAX_FILES` | Part size and count when a long raw `.txt` upload fails. |
| `OFFICE_ENABLED`, `OFFICE_MAX_CHARS`, `OFFICE_MIN_INTERVAL_SECONDS` | Free Office on/off, document limit and per-user throttle. |
| `PRESENTATION_MAX_*` | Deck clip/duration/unpacked/temp limits. |
| `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER`, `PAYMENT_BANK_NAME` | Set all three to enable card-to-card purchases. |
| `RECEIPT_RETENTION_DAYS` | Delete receipt images after this many days (default `90`); the payment row remains. |
| `TRANSCRIPT_RETENTION_DAYS` | Delete finished transcript text after this many days (`0` = keep forever). |
| `DB_PATH`, `TELEGRAM_SESSION`, `TEMP_DIR`, `RECEIPT_DIR`, `LOG_FILE` | Keep all of them outside `public_html`. |

## Validate

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe -m gamas_bot --check
```

```bash
# cPanel Terminal (virtualenv active)
python -m gamas_bot --check
python -m scripts.cpanel_preflight
```

`--check` is offline and safe while the bot is running. The preflight also checks the host: OS,
disk, permissions, lock, Telegram and provider connectivity. It reports `FAIL` for problems that
would stop the bot and `WARN` for advisory ones.
