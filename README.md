# Gamas Bot

A Persian study assistant for Telegram. Send a lecture recording (audio or video)
or a PowerPoint deck; the bot returns the raw transcript first, then a
right-to-left Word booklet.

* **Persian first** — RTL paragraphs and styles, mixed Persian/English runs,
  Persian digits, real heading styles, cover page, page frame, native
  page-number fields.
* **No system media binaries** — media runs on PyAV and python-pptx. There is no
  `ffmpeg`, `ffprobe` or `soffice` requirement.
* **Providers are configuration** — speech-to-text (Speechmatics / Deepgram /
  any OpenAI-compatible endpoint) and note generation (NaraRouter / OpenRouter /
  Gemini / Anthropic / any OpenAI-compatible endpoint). Keys live in the
  environment only, never in the database.
* **Prepaid billing with a real ledger** — credit is reserved before a job runs
  and released in full on every failure path.

## Pipeline

```
Telegram → bounded job queue → media worker (PyAV) → speech-to-text
        → transcript engine (cleanup + paragraphing)
        → raw .txt delivered → note model (map + optional reduce)
        → RTL Word document → delivery
```

The raw transcript is persisted and delivered **before** note generation starts,
so a note-provider outage never costs the user their content.

In chat everything starts from one button, **📎 ساخت جزوه**. Admins step into
their own panel (**🛠 پنل مدیریت**) for statistics, users, pending payments and
broadcasts.

## Supported formats

| Kind | Formats |
| --- | --- |
| Audio | MP3, M4A, WAV, OGG/OPUS, FLAC, WMA, AMR, AAC, Telegram voice notes |
| Video | MP4, MKV, MOV, AVI, WEBM, WMV, MPEG/TS, Telegram round videos (audio track extracted, never re-encoded) |
| Decks | PPTX, PPTM, PPSX, PPSM, POTX, POTM, legacy PPT/PPS/POT (converted in-process). ODP/OTP are not supported |

## Install

```bash
git clone https://github.com/AmiraliGhamkhar/Gamas_bot
cd Gamas_bot
python -m venv .venv && . .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env          # then edit .env
python -m scripts.cpanel_preflight   # host readiness checklist
python -m gamas_bot --check          # offline config self-check
python -m gamas_bot                  # run
```

Requires Python **3.11–3.13**, a Telegram bot token and API credentials
([my.telegram.org](https://my.telegram.org)), and at least one STT key.

### Minimum configuration

`.env.example` documents all 81 variables. The ones you cannot start without:

```dotenv
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789
ADMIN_IDS=123456789            # numeric ids, comma separated

# At least one STT engine:
SPEECHMATICS_API_KEY=...
# DEEPGRAM_API_KEY=...

# Optional — without it the bot delivers raw transcripts only.
# The order IS the failover policy:
NARA_API_KEY=...
OPENROUTER_API_KEY=...
```

Relative paths are anchored to the project directory, so cron or `su -c` cannot
silently create a second database or session.

### Migrating from v2

```bash
python scripts/import_v2.py --from data/gamas_v2.db --into data/gamas.db
```

Users, plans, pending payments and live balances are replayed into the v3
append-only ledger. The script refuses to run twice and never writes to the
source database.

## Limits

All configurable; defaults shown.

| Limit | Variable | Default |
| --- | --- | --- |
| Upload size | `MAX_FILE_SIZE` | 2 GB |
| Active / queued jobs | `MAX_ACTIVE_JOBS` / `MAX_QUEUED_JOBS` | 3 / 8 |
| One media operation | `MEDIA_TIMEOUT` | 3600 s |
| Legacy `.ppt` conversion | `CONVERT_TIMEOUT` | 600 s |
| One STT job | `STT_JOB_TIMEOUT` | 21600 s |
| Clips per deck | `PRESENTATION_MAX_CLIPS` | 300 |
| Unpacked deck size | `PRESENTATION_MAX_UNPACKED_BYTES` | 4 GB |
| Note request window | `NOTE_CONTEXT_TOKENS` | 131072 tokens |
| Note chunk ceiling | `NOTE_CHUNK_CHARS` | 64000 chars |
| Note answer budget | `NOTE_MAX_OUTPUT_TOKENS` | 16384 tokens |
| Receipt image | `MAX_PAYMENT_RECEIPT_BYTES` | 5 MB, kept 90 days |

Beyond the queue bounds an upload is rejected with an explicit message rather
than queued in memory.

## Test

```bash
ruff check .
python -m mypy
python -m pytest -q
```

No test needs an API key or makes a billable call. See
[docs/TESTING.md](docs/TESTING.md).

## Deploy

* **cPanel / Passenger** — [docs/DEPLOY_CPANEL.md](docs/DEPLOY_CPANEL.md),
  Persian walkthrough in [docs/DEPLOY_FA.md](docs/DEPLOY_FA.md).
* **systemd** — `deploy/gamas-bot.service` (`Restart=always`, `UMask=0077`,
  `ProtectSystem=strict`).

An advisory lock on the Telegram session path guarantees a single polling
instance even if a watchdog starts a second copy.

## Known limitations

* STT is billed by provider duration and is never chunked: a file larger than
  every configured engine's upload ceiling is rejected, not split.
* The table of contents links to headings but prints no page numbers — exact
  numbers need an office layout engine, and they are omitted rather than
  guessed.
* Legacy `.ppt` fidelity is bounded by the converter library; unsupported
  content is reported, not silently dropped.
* One process serves one bot token. Horizontal scaling would need a real job
  broker.
* Payments are card-to-card and confirmed by a human. No gateway integration.

## Privacy

Audio goes to the configured STT provider, the transcript to the configured note
provider. Temporary files are removed after processing. The database keeps the
transcript and generated notes so a job can be audited. Receipts are stored
outside the web root at `0600` and deleted after the retention window. Logs never
contain transcripts, keys or receipt contents.

## Documentation

[docs/README.md](docs/README.md) indexes every page.

## License

Proprietary — all rights reserved.

## Author

Amirali Ghamkhar
