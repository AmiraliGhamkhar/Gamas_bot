# Gamas Bot

A Persian study assistant for Telegram. Send a lecture recording (audio or
video) or a PowerPoint deck and the bot returns the raw transcript first,
then a polished, right-to-left Word booklet.

* **Persian first.** RTL paragraphs and styles, mixed Persian/English runs,
  Persian digits, real heading styles, a cover page, a page frame, native
  page-number fields and a link-only table of contents.
* **No system media binaries.** Media handling uses PyAV and python-pptx from
  the Python dependencies — there is no `ffmpeg`, `ffprobe` or `soffice`
  requirement.
* **Provider-neutral.** Speech-to-text (Speechmatics / Deepgram / any
  OpenAI-compatible endpoint) and note generation (Gemini / Anthropic / any
  OpenAI-compatible endpoint) are configuration, not code. Keys live in the
  environment only.
* **Prepaid billing with a real ledger.** Credit is reserved before a job runs
  and released in full on every failure or cancellation path.

| Documentation | |
| --- | --- |
| [docs/OVERHAUL_PLAN.md](docs/OVERHAUL_PLAN.md) | the v3 design contract (module map, invariants) |
| [docs/README.md](docs/README.md) | index of every page (v3 pages first, v2-era pages marked) |

## How it works

```
Telegram → bounded job queue → media worker (PyAV) → speech-to-text
        → transcript engine (fidelity-gated cleanup + paragraphing)
        → raw .txt delivered  →  note model (map + optional reduce)
        → RTL Word document → delivery
```

The transcript engine's primary output is the document handed to the note
model — cleaned, deterministically paragraphed from word timings, and checked
against the raw engine text so nothing is silently lost. That same document is
the `.txt` deliverable. If the note model is unavailable, the raw transcript is
still delivered — content is never lost to a provider outage.

In the chat everything starts from one button, **📎 ساخت جزوه**; the account
screen shows balance, purchase plans and payment history. Administrators get
an overview with statistics, user listings, pending card-to-card payments with
receipts, grants and broadcast — one small panel, no provider plumbing.

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
* At least one speech-to-text provider key
* Optional: a note-generation provider (without one, the bot delivers raw
  transcripts only and says so in `--check`)

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

# At least one STT engine:
SPEECHMATICS_API_KEY=...
# DEEPGRAM_API_KEY=...
# STT_OPENAI_BASE_URL=https://api.openai.com/v1
# STT_OPENAI_API_KEY=...

# Note generation (optional; without it only raw transcripts are delivered):
NOTE_PROVIDER=gemini
NOTE_API_KEY=...

# Admin Telegram numeric ids, comma separated:
ADMIN_IDS=123456789
```

Relative paths are anchored to the project directory, so a cron job or
`su -c` start cannot accidentally create a second database or session.

### Migrating from v2

Existing deployments import users, plans, pending payments and live credit
balances from the old database in one shot:

```bash
python scripts/import_v2.py --from data/gamas_v2.db --into data/gamas.db
```

The v2 `entitlements` state is replayed into the v3 append-only ledger, so the
derived balance equals the old remaining balance and expiry dates survive.
The script refuses to run twice and never writes to the source database.

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
```

The suite covers the transcript engine (fidelity, paragraphing, chunking),
the STT failover/cooldown machinery, the note merge invariants, the ledger
(reserve → consume/release, exactly-once settlement), the job queue bounds,
the DOCX RTL contracts and the v2 import.

## Deployment

* **cPanel / Passenger** — [`docs/DEPLOY_CPANEL.md`](docs/DEPLOY_CPANEL.md) and
  [`docs/DEPLOY_FA.md`](docs/DEPLOY_FA.md).
* **systemd** — install `deploy/gamas-bot.service` (hardened unit:
  `Restart=always`, `UMask=0077`, `ProtectSystem=strict`).
* An advisory lock on the Telegram session path guarantees a single polling
  instance even if a watchdog starts a second copy.

## Known limitations

* Speech-to-text is billed by provider duration and is never chunked: a file
  larger than every configured engine's direct-upload limit is rejected rather
  than split.
* The table of contents links to headings but does not print page numbers —
  exact numbers need an office layout engine, and numbers are omitted rather
  than estimated.
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
receipt contents.

## License

Proprietary — all rights reserved.

## Author

Amirali Ghamkhar
