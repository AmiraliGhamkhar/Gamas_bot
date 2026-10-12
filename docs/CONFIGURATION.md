# Configuration

## One source of truth

`.env.example` is the canonical, complete list of environment variables, and
`gamas_bot/config.py` is the only place they are read: one frozen `Settings`
dataclass built by `Settings.from_env()`. Nothing else touches `os.environ` at
runtime, and no secret is ever stored in the database.

`tests/test_config_consistency.py` enforces the contract:

* every variable `config.py` reads appears in `.env.example`;
* every variable documented in `.env.example` is read by the code;
* each value shown in `.env.example` is what the code produces when that
  variable is set to it (so a documented default cannot drift from the code —
  this is what caught the stale `DEEPGRAM_MODEL`, log path and database name);
* the STT pages do not advertise settings from the deleted v2 provider
  platform.

`tests/test_config.py` covers parsing, precedence and `check()`.

## Precedence

1. Real environment variables (systemd `Environment=`, Passenger, the shell);
2. `.env` — the project root first, then the working directory;
3. the built-in default in `config.py`.

`load_dotenv(..., override=False)` means an exported variable always wins over
`.env`, which is what an operator expects from a service unit.

Relative paths (`DB_PATH`, `TELEGRAM_SESSION`, `TEMP_DIR`, `RECEIPT_DIR`,
`LOG_FILE`) are anchored to the project directory, never to the process working
directory: Passenger, `su -c` and cron all start somewhere else, and a relative
path there would silently create a second database and session.

## Variables by area

Exact names, defaults and the Persian explanations live in `.env.example`; this
table is the map of what exists.

| Area | Variables |
| --- | --- |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ADMIN_IDS`, `TELEGRAM_SESSION` |
| Storage | `DB_PATH`, `TEMP_DIR`, `MAX_FILE_SIZE` |
| Logging | `LOG_FILE`, `LOG_LEVEL`, `LOG_FORMAT`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` |
| Job queue | `MAX_ACTIVE_JOBS`, `MAX_QUEUED_JOBS`, `MEDIA_TIMEOUT`, `CONVERT_TIMEOUT` |
| Speech-to-text | `STT_PROVIDERS`, `STT_LANGUAGE`, `SPEECHMATICS_API_KEY`, `SPEECHMATICS_BASE_URL`, `SPEECHMATICS_MODEL`, `DEEPGRAM_API_KEY`, `DEEPGRAM_BASE_URL`, `DEEPGRAM_MODEL`, `STT_OPENAI_BASE_URL`, `STT_OPENAI_API_KEY`, `STT_OPENAI_MODEL`, `STT_OPENAI_MAX_UPLOAD_BYTES`, `STT_JOB_TIMEOUT`, `STT_POLL_INTERVAL` — see [STT_PROVIDERS.md](STT_PROVIDERS.md) |
| Note generation | `NOTE_PROVIDER`, `NOTE_API_KEY`, `GEMINI_API_KEY` (legacy alias), `NOTE_BASE_URL`, `NOTE_MODEL`, `NOTE_MODE`, `NOTE_MAX_OUTPUT_TOKENS`, `NOTE_JSON_MODE`, `NOTE_CHUNK_CHARS`, `NOTE_CONCURRENCY`, `NOTE_COMPILE` |
| Raw text | `RAW_TEXT_PROFILE` (`plain` \| `timestamped`) |
| Presentations | `PRESENTATION_MAX_CLIPS`, `PRESENTATION_MAX_TOTAL_DURATION`, `PRESENTATION_MAX_UNPACKED_BYTES`, `PRESENTATION_MIN_CLIP_SECONDS`, `PRESENTATION_SILENCE_SECONDS`, `PRESENTATION_WAV_LIMIT_BYTES`, `PRESENTATION_INCLUDE_SLIDE_TEXT`, `PRESENTATION_INCLUDE_VIDEO_AUDIO` |
| Billing | `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER`, `PAYMENT_BANK_NAME`, `RECEIPT_DIR`, `RECEIPT_RETENTION_DAYS`, `MAX_PAYMENT_RECEIPT_BYTES`, `FREE_PLAN_HOURS`, `PLAN_*_HOURS`, `PLAN_*_PRICE_TOMAN`, `PLAN_*_VALIDITY_DAYS` |

Notes:

* `GEMINI_API_KEY` is only a legacy alias: when `NOTE_PROVIDER` is unset and it
  is the only key present, `NOTE_PROVIDER=gemini` is selected automatically.
* `NOTE_PROVIDER=anthropic` has no built-in default model: set `NOTE_MODEL`.
* Speech-to-text secrets are read from the environment only. There is no
  credential vault, admin key wizard or encryption key in v3.

## What `--check` validates

`python -m gamas_bot --check` is offline and side-effect free. It reports:

* `FAIL` — Telegram settings missing, **no STT provider configured among the
  providers listed in `STT_PROVIDERS`**, an unknown `NOTE_PROVIDER`, an empty
  `ADMIN_IDS`; the process exits `2` at startup for these;
* `WARN` — advisory problems that reduce functionality but keep the bot
  running: note generation not configured (“raw transcripts only”) and a
  Persian deployment whose `DEEPGRAM_MODEL` cannot transcribe `fa`.

Advisory problems are printed by `--check`, written to the log at startup, and
never prevent a job from running.

## Secrets

Provider keys live in the process environment / owner-only `.env` file. They are
never written to the database, never logged, and never included in a user-facing
message; provider error text is redacted against the key before it is stored in
the job's error column.
