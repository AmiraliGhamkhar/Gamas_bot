# Configuration

## One source of truth

`.env.example` is the complete list of environment variables; `gamas_bot/config.py`
is the only place they are read — one frozen `Settings` dataclass built by
`from_env()`. Nothing else touches `os.environ` at runtime, and no secret is ever
stored in the database.

`tests/test_config_consistency.py` locks the contract:

* every variable `config.py` reads appears in `.env.example`;
* every variable in `.env.example` is read by the code;
* each value shown in `.env.example` is what the code produces when that variable
  is set to it, so a documented default cannot drift.

## Precedence

1. Real environment variables (systemd `Environment=`, Passenger, the shell);
2. `.env` — project root first, then the working directory;
3. the built-in default in `config.py`.

`load_dotenv(..., override=False)` means an exported variable always wins over
`.env`, which is what an operator expects from a service unit.

Relative paths (`DB_PATH`, `TELEGRAM_SESSION`, `TEMP_DIR`, `RECEIPT_DIR`,
`LOG_FILE`) are anchored to the **project directory**, never to the process
working directory. Passenger, `su -c` and cron all start elsewhere, and a
relative path there would silently create a second database and session.

## Variables by area

Exact names, defaults and Persian explanations live in `.env.example`; this is
the map of what exists.

| Area | Variables |
| --- | --- |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`, `ADMIN_IDS` |
| Storage & logs | `DB_PATH`, `TEMP_DIR`, `LOG_FILE`, `LOG_LEVEL`, `LOG_FORMAT`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` |
| Queue & timeouts | `MAX_FILE_SIZE`, `MAX_ACTIVE_JOBS`, `MAX_QUEUED_JOBS`, `MEDIA_TIMEOUT`, `CONVERT_TIMEOUT`, `STT_JOB_TIMEOUT`, `STT_POLL_INTERVAL` |
| Speech-to-text | `STT_PROVIDERS`, `STT_LANGUAGE`, `SPEECHMATICS_API_KEY`, `SPEECHMATICS_BASE_URL`, `SPEECHMATICS_MODEL`, `DEEPGRAM_API_KEY`, `DEEPGRAM_BASE_URL`, `DEEPGRAM_MODEL`, `STT_OPENAI_BASE_URL`, `STT_OPENAI_API_KEY`, `STT_OPENAI_MODEL`, `STT_OPENAI_MAX_UPLOAD_BYTES` — see [STT_PROVIDERS.md](STT_PROVIDERS.md) |
| Note generation | `NOTE_PROVIDERS`, `NARA_API_KEY`, `NARA_BASE_URL`, `NARA_MODEL`, `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`, `NOTE_PROVIDER`, `NOTE_API_KEY`, `NOTE_BASE_URL`, `NOTE_MODEL`, `GEMINI_API_KEY`, `NOTE_MODE`, `NOTE_CONTEXT_TOKENS`, `NOTE_MAX_OUTPUT_TOKENS`, `NOTE_JSON_MODE`, `NOTE_CHUNK_CHARS`, `NOTE_CONCURRENCY`, `NOTE_COMPILE`, `NOTE_COMPILE_MAX_CHARS`, `NOTE_REASONING_EFFORT` — see [PROVIDERS.md](PROVIDERS.md) |
| Raw text | `RAW_TEXT_PROFILE` (`plain` \| `timestamped`) |
| Free Office | `OFFICE_ENABLED`, `OFFICE_MAX_INPUT_BYTES`, `OFFICE_MAX_CHARS`, `OFFICE_MAX_SLIDES`, `OFFICE_SESSION_TTL_MINUTES` — see [OFFICE.md](OFFICE.md) |
| Presentations | `PRESENTATION_MAX_CLIPS`, `PRESENTATION_MAX_TOTAL_DURATION`, `PRESENTATION_MAX_UNPACKED_BYTES`, `PRESENTATION_MIN_CLIP_SECONDS`, `PRESENTATION_SILENCE_SECONDS`, `PRESENTATION_WAV_LIMIT_BYTES`, `PRESENTATION_INCLUDE_SLIDE_TEXT`, `PRESENTATION_INCLUDE_VIDEO_AUDIO` |
| Billing | `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER`, `PAYMENT_BANK_NAME`, `RECEIPT_DIR`, `RECEIPT_RETENTION_DAYS`, `MAX_PAYMENT_RECEIPT_BYTES`, `FREE_PLAN_HOURS`, `PLAN_*_HOURS`, `PLAN_*_PRICE_TOMAN`, `PLAN_*_VALIDITY_DAYS` |

## Notes that matter

* **`NOTE_PROVIDERS` is the failover order** (default `nara,openrouter`) and the
  only routing policy. A provider is attempted only when it has its **own**
  credential, so a missing key is never treated as a configured provider and one
  key is never sent to two providers.
* **`NOTE_PROVIDER` is the older single slot** and still works. It is classified
  by host, so a deployment whose `NOTE_BASE_URL` is `https://router.bynara.id/v1`
  is the Nara leg of the chain rather than a duplicate generic gateway.
* **`GEMINI_API_KEY` is a legacy alias**: when `NOTE_PROVIDER` is unset, it is the
  only key present and no dedicated provider key exists, `gemini` is selected
  automatically.
* **`NOTE_PROVIDER=anthropic` has no default model** — set `NOTE_MODEL`.
* **`NOTE_CONTEXT_TOKENS`** sizes one request: the output budget, system prompt
  and contextual material are subtracted and the rest is split into chunks. Set
  it to the smallest window among the configured models. The defaults are a
  matched pair (the pinned OpenRouter endpoint serves 262,144 tokens and
  Agnes's reference window is larger still, with Nara's gateway cap
  unpublished; see [PROVIDERS.md](PROVIDERS.md)), so the default assumption
  is 131,072 — half the known served window. Changing
  `NARA_MODEL`/`OPENROUTER_MODEL` without revisiting this value sizes requests
  for a window the new model may not have.
* **`NOTE_COMPILE_MAX_CHARS`** caps the merged booklet sent to the editorial
  compile pass; `0` (default) derives the cap from `NOTE_CONTEXT_TOKENS` with
  an absolute ceiling in the code.
* **`NOTE_REASONING_EFFORT`** optionally sets `reasoning_effort` on the
  Nara/OpenRouter note routes (`none`…`max`); unset sends nothing and keeps
  each provider's own default.
* **No credential vault.** Keys come from the environment only. There is no
  encryption key, no key wizard and no key column in the database.

## What `--check` reports

`python -m gamas_bot --check` is offline and side-effect free.

**`FAIL`** — the process exits `2` at startup:

* Telegram settings missing (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`,
  `TELEGRAM_API_HASH`);
* no STT provider configured among those listed in `STT_PROVIDERS`;
* an unknown `NOTE_PROVIDER`;
* a note provider that has a credential but no model name (for example
  `NOTE_PROVIDER=anthropic` without `NOTE_MODEL`);
* `ADMIN_IDS` empty.

**`WARN`** — advisory; the bot still runs:

* note generation not configured ("raw transcripts only");
* a Persian deployment whose `DEEPGRAM_MODEL` cannot transcribe `fa`;
* a `NOTE_CONTEXT_TOKENS` / `NOTE_MAX_OUTPUT_TOKENS` pair that leaves no room for
  the transcript in one request.

Advisory problems are printed by `--check` and written to the log at startup, and
never prevent a job from running.

## Secrets

Provider keys live in the process environment or an owner-only `.env`. They are
never written to the database, never logged, and never included in a user-facing
message. Provider error text is redacted against the key before it reaches the
job's error column.
