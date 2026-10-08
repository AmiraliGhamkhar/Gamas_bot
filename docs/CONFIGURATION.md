# Configuration

## One source of truth

`.env.example` is the canonical, complete list of environment variables, and
`gamas_bot/config.py` is the only place they are read. There is exactly one
configuration model: the frozen `Settings` dataclass, built by
`Settings.from_env()`, which validates every value at startup and raises a
Persian, actionable error instead of failing later inside a job.

`tests/test_config_consistency.py` enforces this:

* every variable `config.py` reads must appear in `.env.example` (including the
  legacy aliases documented as commented examples);
* every variable assigned in `.env.example` must be read by some code path;
* `Settings` defaults must agree with the canonical constants in `config.py`
  and `billing.py`, so a plan price cannot be defined twice with two values;
* documentation may not reference a module or a document that does not exist.

## Precedence

1. Real environment variables (systemd `Environment=`, Passenger, the shell);
2. `.env` — the working directory first, then the project root;
3. the built-in default.

`load_dotenv(..., override=False)` means an exported variable always wins over
`.env`, which is what an operator expects from a service unit.

Relative paths (`DATABASE_PATH`, `TELEGRAM_SESSION_PATH`, `TEMP_DIR`,
`RECEIPT_DIR`, `LOG_FILE`) are anchored to the project root, never to the
process working directory: Passenger, `su -c` and cron all start in a different
directory, and a relative path there would silently create a second, empty
database and Telegram session.

Aliases (`DOCX_BODY_FONT`, `DOCX_HEADING_FONT`, `DOCX_LATIN_FONT`,
`DOCX_FALLBACK_FONT`, `SPEECHMATICS_MODEL`, `FFMPEG_TIMEOUT_SECONDS`,
`SOFFICE_TIMEOUT_SECONDS`) are still read for deployments that predate the
canonical spellings. The canonical name always wins when both are set.

## Categories

| Area | Variables (canonical spelling) |
| --- | --- |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ADMIN_IDS`, `TELEGRAM_PROXY`, `TELEGRAM_SESSION_PATH` |
| Storage | `DATABASE_PATH`, `TEMP_DIR`, `MAX_FILE_SIZE_BYTES` |
| Job queue | `MAX_CONCURRENT_JOBS`, `MAX_PENDING_JOBS`, `PROGRESS_ANIMATION_ENABLED` |
| STT | `STT_PRIMARY`, `STT_LANGUAGE`, `STT_FALLBACK_ENABLED`, `STT_MIN_CONFIDENCE`, `STT_MAX_ATTEMPTS`, `STT_RETRY_*`, `STT_JOB_TIMEOUT_SECONDS`, `STT_POLL_INTERVAL_SECONDS`, `SPEECHMATICS_*`, `DEEPGRAM_*`, `STT_OPENAI_*` |
| Notes | `NOTE_API_PROVIDER`, `NOTE_API_KEY`, `NOTE_API_BASE_URL`, `NOTE_API_MODEL`, `NOTE_API_TIMEOUT_SECONDS`, `NOTE_API_RETRIES`, `NOTE_API_MAX_OUTPUT_TOKENS`, `NOTE_API_JSON_MODE`, `NOTE_API_EXTRA_HEADERS_JSON`, `NOTE_MODE`, `NOTE_REPAIR_ENABLED`, `NOTE_GLOBAL_CONTEXT_ENABLED` |
| AI provider platform | `AI_FREE_ONLY` (default `true`), `AI_ALLOW_PAID_FALLBACK` (default `false`), `AI_ROUTING_ENABLED` (default `true`), `AI_PROVIDER_SYNC_TTL`, `AI_DEFAULT_NOTE_ROUTE`, `AI_MAX_PROVIDER_FAILOVERS`, `AI_MAX_GENERATION_RETRIES` (`-1` = auto), `AI_QUOTA_SAFETY_MARGIN`, `CLOUDFLARE_ACCOUNT_ID` — full reference in [docs/AI_PROVIDERS.md](AI_PROVIDERS.md) |
| Word document | `DOCX_FONT_PROFILE`, `DOCX_FONT_*`, `DOCX_COVER_ENABLED`, `DOCX_TOC_*`, `DOCX_TOC_PAGE_NUMBERS`, `DOCX_PAGE_BORDER_*`, `DOCX_SHOW_FOOTER_BRAND`, `DOCX_LOGO_PATH`, `DOCX_PAGINATION_*` |
| PowerPoint | `PPTX_ENABLED`, `PPTX_LEGACY_ENABLED`, `PPTX_INCLUDE_*`, `PPTX_MIN_CLIP_SECONDS`, `PPTX_SILENCE_SECONDS`, `PPTX_MAX_*`, `MEDIA_TIMEOUT_SECONDS`, `PPT_CONVERT_TIMEOUT_SECONDS` |
| Billing | `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER`, `PAYMENT_BANK_NAME`, `FREE_PLAN_HOURS`, `PLAN_5_*`, `PLAN_10_*`, `PLAN_20_*`, `PLAN_25_*`, `PLAN_50_*`, `RECEIPT_DIR`, `RECEIPT_RETENTION_DAYS`, `MAX_PAYMENT_RECEIPT_BYTES`, `PROVIDER_CREDENTIALS_ENCRYPTION_KEY` |
| Observability | `LOG_LEVEL`, `LOG_FORMAT`, `LOG_FILE`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` |

Exact names, defaults and Persian explanations live in `.env.example`; the
values above are the ones this page mentions by name so that a reader can find
them quickly.

## The static table of contents

`DOCX_TOC_PAGE_NUMBERS` decides how the table of contents gets its numbers:

* `auto` (default) — when LibreOffice (`soffice`) and `pypdf` are available the
  exact, renderer-verified page numbers are printed; when they are not, the
  document is still delivered with a topic list whose entries are internal
  hyperlinks and whose page-number column is empty.
* `required` — the strict behaviour: a document whose exact mapping cannot be
  produced is not generated at all.
* `off` — the renderer is never invoked; the TOC is always the link-only list.

No mode ever invents a page number. `scripts/cpanel_preflight.py` reports the
missing renderer as a warning under `auto` and a failure under `required`.

## Secrets

Provider keys and the Fernet master key belong in the service environment or in
the `🩺 وضعیت سرویس‌ها` / `🔑 API Keys` panel (which stores ciphertext), never in
`.env` committed to a repository. `PROVIDER_CREDENTIALS_ENCRYPTION_KEY` is
environment-only: losing it makes stored credentials unrecoverable, so back it
up with the database.
