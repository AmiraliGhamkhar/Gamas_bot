# Gamas Bot

A Persian study assistant for Telegram. Send a lecture recording (audio or
video) or a PowerPoint deck and the bot returns a polished, right-to-left Word
booklet plus the raw transcript.

* **Persian first.** RTL paragraphs and styles, mixed Persian/English runs,
  Persian digits, real heading styles, a cover page, a page frame and a visible
  table of contents on page 2.
* **No system media binaries.** Media handling uses PyAV and python-pptx from
  the Python dependencies — there is no `ffmpeg`, `ffprobe` or `soffice`
  requirement for transcription.
* **Provider-neutral.** Speech-to-text (Speechmatics / Deepgram / any
  OpenAI-compatible endpoint) and note generation (Gemini / Anthropic / any
  OpenAI-compatible endpoint) are configuration, not code.
* **Prepaid billing with a real ledger.** Credit is reserved before a job runs
  and released in full on every failure or cancellation path.

| Documentation | |
| --- | --- |
| [docs/README.md](docs/README.md) | index of every page |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | pipeline, modules, concurrency, data model |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | the configuration model |
| [docs/DOCX.md](docs/DOCX.md) | the Word booklet and its RTL contracts |
| [docs/DEPLOY_CPANEL.md](docs/DEPLOY_CPANEL.md) · [docs/DEPLOY_FA.md](docs/DEPLOY_FA.md) | deployment (English / فارسی) |
| [docs/TESTING.md](docs/TESTING.md) | gates and benchmarks |
| [docs/SECURITY.md](docs/SECURITY.md) | threat model and controls |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | notable changes |

## How it works

```
Telegram → bounded job queue → media worker (PyAV) → speech-to-text
        → normalization → structuring (chunk + global context) → QA
        → RTL Word document + raw text → delivery
```

A completed job delivers two files: the Word booklet and a `.txt` file
containing the transcript exactly as extracted. If the note model is unavailable,
the transcript is still delivered — content is never lost to a provider outage.

## Supported formats

* **Audio** — MP3, M4A, WAV, OGG/OPUS, FLAC, WMA, AMR, AAC and Telegram voice
  notes; other common containers are probed and handled when they carry audio.
* **Video** — MP4, MKV, MOV, AVI, WEBM, WMV, MPEG/TS and Telegram round video
  notes. The audio track is extracted; the video stream is never re-encoded.
* **Presentations** — PPTX, PPTM, PPSX, PPSM, POTX, POTM, and legacy PPT/PPS/POT
  (converted in-process). ODP/OTP are not supported; export them to PPTX.

## Requirements

* Python **3.11 – 3.13**
* A Telegram bot token and API credentials ([my.telegram.org](https://my.telegram.org))
* At least one speech-to-text provider, unless you configure encrypted keys
  from the admin panel instead
* Optional, for exact TOC page numbers only: LibreOffice (`soffice`) and `pypdf`
  — see [`docs/DOCX.md`](docs/DOCX.md). Without them the booklet is still
  delivered with a link-only table of contents.

## Installation

Dependencies have one canonical definition in `pyproject.toml`;
`requirements.txt` is generated from it and is what a cPanel/Passenger
deployment installs.

```bash
git clone https://github.com/AmiraliGhamkhar/Gamas_bot
cd Gamas_bot
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env            # then edit .env
python -m scripts.cpanel_preflight   # readiness checklist
python -m gamas_bot --check          # configuration/dependency self-check
python -m gamas_bot                  # run
```

For development:

```bash
python -m pip install -r requirements.txt ruff pytest
ruff check .
python -m pytest -q
```

### Minimum configuration

Everything is documented in `.env.example`; the values you cannot start without:

```dotenv
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789

# At least one STT engine (or add encrypted keys from the admin panel):
SPEECHMATICS_API_KEY=...
# DEEPGRAM_API_KEY=...
# STT_OPENAI_BASE_URL=https://api.openai.com/v1
# STT_OPENAI_API_KEY=...

# Note generation:
NOTE_API_PROVIDER=gemini
GEMINI_API_KEY=...

# Admin Telegram numeric ids, comma separated:
ADMIN_IDS=123456789

# Required to store provider keys from the admin panel:
# PROVIDER_CREDENTIALS_ENCRYPTION_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
```

Relative paths are anchored to the project directory, so a cron job or
`su -c` start cannot accidentally create a second database or session.

## Processing limits

Defaults, all configurable:

| Limit | Default |
| --- | --- |
| Upload size | 2 GB |
| Active jobs / queued jobs | 3 / 8 (further uploads are rejected with an explicit message) |
| One media operation | 3600 s |
| Legacy `.ppt` conversion | 600 s |
| STT job | 21600 s |
| Slide clips per deck | 300 (21600 s total) |
| Unpacked deck size | 4 GB (with entry-count and path checks) |
| Receipt image | 5 MB, retained 90 days |

## Testing

```bash
python -m compileall -q gamas_bot scripts tests passenger_wsgi.py
ruff check .
python -m pytest -q
python -m scripts.benchmark_notes         # deterministic, offline
python -m scripts.validate_docx --render
```

See [`docs/TESTING.md`](docs/TESTING.md). CI runs the suite on Python 3.11,
3.12 and 3.13 with both `pytest` and `unittest`.

## Deployment

* **cPanel / Passenger** — [`docs/DEPLOY_CPANEL.md`](docs/DEPLOY_CPANEL.md) and
  [`docs/DEPLOY_FA.md`](docs/DEPLOY_FA.md).
* **systemd** — install `deploy/gamas-bot.service` (hardened unit:
  `Restart=always`, `UMask=0077`, `ProtectSystem=strict`);
  [`docs/OPERATIONS.md`](docs/OPERATIONS.md) explains the unit and the
  start/stop/health workflow.
* An advisory lock on the Telegram session path guarantees a single polling
  instance even if a watchdog starts a second copy.

## Known limitations

* Speech-to-text is billed by provider duration and is never chunked: a file
  larger than every configured engine's direct-upload limit is rejected rather
  than split.
* Exact TOC page numbers need an office layout engine. Without one the numbers
  are omitted, never estimated.
* Legacy `.ppt` fidelity is bounded by what the converter library supports;
  unsupported content is reported rather than silently dropped.
* A single process serves one bot token. Horizontal scaling would need a real
  job broker, which this workload does not justify.
* Payments are card-to-card and confirmed manually; there is no gateway
  integration.

## Privacy

Audio is sent to the configured speech-to-text provider and the transcript to
the configured note provider. Temporary files are removed after processing.
The database keeps the transcript and the generated notes so a job can be
audited; payment receipts are stored privately (outside the web root, `0600`)
and deleted after the retention window. Logs never contain transcripts, keys or
receipt contents. See [`docs/SECURITY.md`](docs/SECURITY.md).

## License

Proprietary — all rights reserved.

## Author

Amirali Ghamkhar
