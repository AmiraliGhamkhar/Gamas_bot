# Gamas Bot

> **Persian Telegram Lecture Notes Assistant** — turn voice messages, audio, video, and PowerPoint presentations into structured Persian lecture notes.

Gamas Bot is a Python-based Telegram bot built with [Telethon](https://github.com/LonamiWebs/Telethon) (MTProto) for processing Persian lectures.

It accepts audio, voice messages, videos, and PowerPoint presentations, converts speech to text using **Speechmatics** or **Deepgram**, and optionally generates structured lecture notes using **Gemini, Anthropic, or any OpenAI-compatible API** (OpenAI, OpenRouter, Groq, Together, DeepSeek, Ollama, vLLM, and similar services).

---

## Features

- Persian speech-to-text (`fa`)
- Telegram voice messages and audio files
- Video-to-text processing
- PowerPoint lecture processing
- Slide audio extraction and merging
- Slide text and speaker-note extraction
- Slide-by-slide lecture notes
- Speechmatics + Deepgram STT
- Configurable STT fallback
- Provider-neutral note generation (Gemini, Anthropic, OpenAI-compatible APIs)
- Inline glass-button menus for users and administrators
- Per-job editable progress bars
- Background processing for long jobs
- SQLite + WAL
- Automatic temporary-file cleanup
- Long-message splitting for Telegram
- Admin commands and user management
- Windows-friendly PowerShell setup and a Linux systemd unit

### Supported formats

| Type | Formats |
|---|---|
| Audio | MP3, M4A, WAV, OGG, FLAC, WMA, AMR and other FFmpeg-supported formats |
| Video | MP4, MKV, MOV, AVI, Telegram video notes |
| Presentations (native) | PPTX, PPTM, PPSX, PPSM, POTX, POTM |
| Presentations (legacy, via LibreOffice) | PPT, PPS, POT, ODP, OTP |

---

## How It Works

```text
                Telegram
                   │
                   ▼
            Telethon / MTProto
                   │
      ┌────────────┼────────────┐
      │            │            │
    Audio        Video     PowerPoint
      │            │            │
      │         FFmpeg     Slide Parser
      │            │            │
      └────────────┼────────────┘
                   ▼
            Audio Preparation
                   │
                   ▼
             Speech-to-Text
             ┌─────┴─────┐
             │           │
        Speechmatics   Deepgram
             │           │
             └─────┬─────┘
                   ▼
             Raw Transcript
                   │
                   ▼
       Configured Note API
  Gemini / Anthropic / OpenAI-compatible
                   │
                   ▼
            Structured Notes
                   │
                   ▼
                Telegram
```

---

## Requirements

- Windows 10/11 or Linux
- Python 3.11+
- FFmpeg + FFprobe
- LibreOffice (only required for legacy presentation formats)
- Telegram bot token
- Telegram API ID + API hash
- At least one STT API key (Speechmatics or Deepgram)
- A note-generation API (Gemini, Anthropic, or OpenAI-compatible; optional)

---

## Installation — Windows

### 1. Install FFmpeg

Using WinGet:

```powershell
winget install Gyan.FFmpeg
```

Verify:

```powershell
ffmpeg -version
ffprobe -version
```

### 2. Install LibreOffice

Only required for legacy formats (PPT, PPS, POT, ODP, OTP):

```powershell
winget install TheDocumentFoundation.LibreOffice
```

Verify:

```powershell
soffice --version
```

### 3. Clone the repository

```powershell
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git
cd Gamas_bot
```

### 4. Create a virtual environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 5. Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 6. Configure environment variables

```powershell
Copy-Item .env.example .env
notepad .env
```

Minimal configuration:

```dotenv
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
TELEGRAM_API_ID=YOUR_API_ID
TELEGRAM_API_HASH=YOUR_API_HASH

ADMIN_IDS=123456789

SPEECHMATICS_API_KEY=YOUR_SPEECHMATICS_KEY
DEEPGRAM_API_KEY=YOUR_DEEPGRAM_KEY

NOTE_API_PROVIDER=gemini
GEMINI_API_KEY=YOUR_GEMINI_KEY
```

At least one of the two STT API keys is required. The note API is optional; if it is disabled or unavailable, the bot returns the raw transcript/slide material instead.

---

## Installation — Linux

```bash
sudo apt install ffmpeg libreoffice-impress   # or your distribution's equivalents

git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git
cd Gamas_bot

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env

python -m gamas_bot
```

---

## Telegram Credentials

Create your bot using [@BotFather](https://t.me/BotFather):

```dotenv
TELEGRAM_BOT_TOKEN=...
```

Telethon also requires an API ID and hash from <https://my.telegram.org>:

```dotenv
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
```

---

## API Providers

### Speech-to-text

| Provider | Role | Console |
|---|---|---|
| Speechmatics | Primary STT engine (default) | <https://portal.speechmatics.com/> |
| Deepgram | Alternative/fallback STT engine | <https://console.deepgram.com/> |

### Note generation (provider-neutral)

Choose one provider with `NOTE_API_PROVIDER`. Note generation is optional: if the provider is disabled, has no valid key, or temporarily fails, the raw transcript/slide material is still preserved and delivered.

#### Gemini

```dotenv
NOTE_API_PROVIDER=gemini
NOTE_API_KEY=...                 # or use the legacy GEMINI_API_KEY
NOTE_API_MODEL=gemini-2.5-flash-lite
# NOTE_API_BASE_URL=             # leave empty for Google's default endpoint
```

#### OpenAI-compatible APIs

This mode works with any service implementing `POST /chat/completions`, including OpenAI, OpenRouter, Groq, Together, DeepSeek, local Ollama/vLLM gateways, and compatible private APIs:

```dotenv
NOTE_API_PROVIDER=openai_compatible
NOTE_API_KEY=...
NOTE_API_BASE_URL=https://api.openai.com/v1
NOTE_API_MODEL=gpt-4o-mini
```

OpenRouter example:

```dotenv
NOTE_API_PROVIDER=openai_compatible
NOTE_API_KEY=...
NOTE_API_BASE_URL=https://openrouter.ai/api/v1
NOTE_API_MODEL=openai/gpt-4o-mini
NOTE_API_EXTRA_HEADERS_JSON='{"HTTP-Referer":"https://example.com","X-Title":"Gamas Bot"}'
```

A local endpoint can omit the key:

```dotenv
NOTE_API_PROVIDER=openai_compatible
NOTE_API_KEY=
NOTE_API_BASE_URL=http://127.0.0.1:11434/v1
NOTE_API_MODEL=qwen2.5:7b
```

#### Anthropic

```dotenv
NOTE_API_PROVIDER=anthropic
NOTE_API_KEY=...
NOTE_API_MODEL=claude-3-5-haiku-latest
# NOTE_API_BASE_URL=             # leave empty for Anthropic's default endpoint
```

Common controls:

```dotenv
NOTE_API_TIMEOUT_SECONDS=240
NOTE_API_RETRIES=2
NOTE_API_MAX_OUTPUT_TOKENS=8192
```

Transient `429` and `5xx` responses and network failures are retried with bounded backoff. Prompts and transcript contents are not written to application logs.

---

## STT Configuration

Example:

```dotenv
STT_PRIMARY=speechmatics
STT_LANGUAGE=fa

STT_FALLBACK_ENABLED=true
STT_MIN_CONFIDENCE=0.65

SPEECHMATICS_BASE_URL=https://eu1.asr.api.speechmatics.com/v2
DEEPGRAM_MODEL=nova-3
```

### Primary engine

```dotenv
STT_PRIMARY=speechmatics
```

or

```dotenv
STT_PRIMARY=deepgram
```

### Fallback

```dotenv
STT_FALLBACK_ENABLED=true
```

When enabled, the secondary engine is used if the primary request fails or reports confidence below:

```dotenv
STT_MIN_CONFIDENCE=0.65
```

> Confidence scores from different providers are not necessarily calibrated against each other. Tune this threshold using your own validation dataset.

---

## PowerPoint Processing

PowerPoint files receive additional processing:

```text
                PowerPoint
                    │
         ┌──────────┼──────────┐
         ▼          ▼          ▼
      Slides      Audio      Video
       Text       Clips      Audio
         │          │          │
         │          └────┬─────┘
         │               ▼
         │             FFmpeg
         │               │
         └───────────────┤
                         ▼
                   Speech-to-Text
                         │
                         ▼
                Slide-by-slide notes
```

The processor:

1. Reads the actual presentation slide order.
2. Extracts slide text.
3. Extracts speaker notes.
4. Finds referenced audio.
5. Optionally extracts audio from embedded videos.
6. Filters very short audio clips.
7. Converts audio to mono 16 kHz.
8. Adds a short silence between clips.
9. Sends the combined audio to STT.
10. Combines transcript + slide content.
11. Generates structured notes.

### PowerPoint configuration

```dotenv
PPTX_ENABLED=true
PPTX_INCLUDE_SLIDE_TEXT=true
PPTX_INCLUDE_VIDEO_AUDIO=true
PPTX_LEGACY_ENABLED=true

PPTX_MIN_CLIP_SECONDS=1.0
PPTX_SILENCE_SECONDS=0.5

PPTX_MAX_CLIPS=300
PPTX_MAX_TOTAL_DURATION_SECONDS=21600
PPTX_MAX_UNPACKED_BYTES=4000000000
PPTX_WAV_LIMIT_BYTES=700000000

FFMPEG_BIN=ffmpeg
FFPROBE_BIN=ffprobe
SOFFICE_BIN=soffice

FFMPEG_TIMEOUT_SECONDS=3600
SOFFICE_TIMEOUT_SECONDS=600
```

Disable PowerPoint support entirely:

```dotenv
PPTX_ENABLED=false
```

Presentations without audio can still produce notes from slide text and speaker notes.

---

## Processing Limits

Default application file limit:

```dotenv
MAX_FILE_SIZE_BYTES=2000000000
```

This is an application-level limit and does not guarantee that an STT provider accepts the file. For very long recordings, provider-specific request limits still apply. The application does not automatically split arbitrary long audio files into smaller STT requests.

Control concurrent processing with (default `3`):

```dotenv
MAX_CONCURRENT_JOBS=3
```

Increase this only after testing CPU, memory, disk, network, and API limits.

---

## Run

Activate the virtual environment, then:

```bash
python -m gamas_bot
```

The application automatically creates the required database and data directories on first startup.

---

## Database

Gamas Bot uses SQLite with:

- WAL mode
- Foreign keys
- Busy timeout
- Versioned migrations

Migration files:

```text
migrations/
├── 001_initial.sql
└── 002_presentations.sql
```

Configure paths with:

```dotenv
DATABASE_PATH=data/bot.sqlite3
TELEGRAM_SESSION_PATH=data/telegram_bot
TEMP_DIR=data/tmp
```

Temporary media is removed after processing. Transcripts and generated notes remain in SQLite until explicitly deleted.

---

## Telegram interaction

Users do not need to memorize commands. `/start` opens an inline button menu with:

- **ساخت جزوه** — explains how to attach a file
- **راهنما** — usage steps
- **قالب‌ها** — supported file types
- **حریم خصوصی** — what is sent to external providers

Each accepted upload gets one status message that is edited through the queue, download, media preparation, STT, note-generation, save, and delivery stages. A text progress bar and percentage remain visible throughout the job.

Administrators get an additional **پنل مدیریت** button. Statistics, user listing, broadcast, ban, and unban are all available through buttons; actions requiring text or a user ID prompt for the next message and provide a cancel button.

The old `/help`, `/users`, `/stats`, `/broadcast`, `/ban`, and `/unban` commands remain available for backward compatibility and automation, but they are no longer required for normal use.

Administrators are defined with:

```dotenv
ADMIN_IDS=123456789,987654321
```

Only configured administrator IDs can execute administrative commands.

---

## Input Handling

| Input | Processing |
|---|---|
| Telegram voice | Speech-to-text |
| Audio file | Speech-to-text |
| Video | Extract audio → STT |
| Video note | Extract audio → STT |
| PPTX / PPTM / PPSX / PPSM / POTX / POTM | Slides + audio + notes |
| PPT / PPS / POT / ODP / OTP | LibreOffice → PPTX → same pipeline |
| PDF / image / ZIP | Rejected with instructions |
| Plain text | Ignored |

Files without an audio stream are detected before consuming STT resources. GIF files are not treated as lecture videos.

---

## Testing

Run all tests:

```bash
python -m unittest discover -s tests -v
```

### STT benchmark

Prepare samples (an optional `<sample>.txt` next to each file holds the reference transcript):

```text
samples/
├── short.wav
├── short.wav.txt
├── lecture-long.mp3
└── lecture-long.mp3.txt
```

Run:

```powershell
python -m scripts.benchmark_stt `
    samples\short.wav `
    samples\lecture-long.mp3 `
    --output results.csv
```

The benchmark compares:

- Response time
- Provider confidence
- Normalized Persian WER

Persian normalization includes `ي → ی` and `ك → ک` (plus diacritics and punctuation removal). The benchmark does not store full transcripts in the CSV report.

---

## Security

PowerPoint packages are validated before extraction. Checks include:

- Path traversal protection
- Absolute-path rejection
- Entry-count and unpacked-size limits
- Media reference validation

Never commit secrets or runtime data. The following should remain local:

- `.env`
- `*.session`
- `*.db` / `*.sqlite3`
- Temporary files
- API keys

---

## Privacy

Audio is sent to the configured STT provider. If a note-generation API is enabled, the following may be sent to that configured provider:

- Transcript
- Slide text
- Speaker notes

Temporary presentation and media files are deleted after processing. The local database may contain:

- User information
- Job status
- Raw transcripts
- Generated notes
- Presentation and clip metadata

Before production deployment, review:

- Provider data-retention policies
- User consent requirements
- Applicable privacy regulations
- Data residency
- Your application's retention/deletion policy

---

## STT Evaluation

Speechmatics and Deepgram both support Persian, but real-world accuracy depends on:

- Recording quality and microphone
- Background noise
- Speaker and accent
- Technical vocabulary (medical/engineering terminology)

Do not assume that one provider is universally better for Persian academic lectures. For production evaluation, use the same recordings on both engines and manually verify domain-specific terminology.

---

## Project Structure

```text
Gamas_bot/
│
├── gamas_bot/
│   ├── __init__.py
│   ├── __main__.py        # entry point: python -m gamas_bot
│   ├── bot.py             # Telethon handlers, job orchestration
│   ├── config.py          # environment-driven settings
│   ├── database.py        # SQLite (aiosqlite) + migrations
│   ├── media.py           # ffmpeg / ffprobe / LibreOffice helpers
│   ├── presentations.py   # PowerPoint parsing and audio extraction
│   ├── progress.py        # editable per-job Telegram progress bars
│   ├── logging_config.py  # text/JSON logging and file rotation
│   ├── structuring.py     # provider-neutral note generation
│   └── stt.py             # Speechmatics / Deepgram clients
│
├── migrations/
│   ├── 001_initial.sql
│   └── 002_presentations.sql
│
├── scripts/
│   └── benchmark_stt.py
│
├── tests/
│
├── deploy/
│   └── gamas-bot.service  # systemd unit for Linux
├── docs/
│   └── DEPLOY_FA.md        # راهنمای فارسی استقرار و عیب‌یابی
│
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Production Deployment

> راهنمای کامل فارسی نصب، FFmpeg، LibreOffice، systemd و عیب‌یابی: [`docs/DEPLOY_FA.md`](docs/DEPLOY_FA.md)

Gamas Bot maintains a persistent Telethon/MTProto connection and does not expose an HTTP port. It must run as a long-lived worker. A VPS, dedicated server, container worker, or PaaS **background worker** is suitable; stateless functions (Vercel/Netlify/Lambda), sleeping free tiers, and traditional shared hosting are not.

For the default 2 GB upload limit, plan disk space for the original file, extracted media, converted presentation, and SQLite database. Start with at least 2 vCPU, 4 GB RAM, and 10–20 GB free disk, set `MAX_CONCURRENT_JOBS=1`, observe usage, and only then increase concurrency.

### Ubuntu/Debian: complete systemd setup

#### 1. Install OS packages

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip git ca-certificates \
  ffmpeg libreoffice-core libreoffice-impress \
  fonts-dejavu-core fonts-noto-core
```

- `ffmpeg` also provides `ffprobe` and should include the `libopus` encoder on Ubuntu/Debian.
- `libreoffice-impress` is only needed for legacy `ppt`, `pps`, `pot`, `odp`, and `otp` files. Native `pptx` parsing does not require LibreOffice.
- Font packages prevent missing-character/font-substitution problems during headless LibreOffice conversion. Install any fonts used by your presentations as well.

Verify the exact executables and codecs:

```bash
command -v ffmpeg ffprobe soffice
ffmpeg -hide_banner -version
ffprobe -hide_banner -version
ffmpeg -hide_banner -encoders | grep -E 'libopus|pcm_s16le'
soffice --headless --version
```

If a host installs binaries outside `PATH`, put absolute paths in `.env`:

```dotenv
FFMPEG_BIN=/usr/bin/ffmpeg
FFPROBE_BIN=/usr/bin/ffprobe
SOFFICE_BIN=/usr/bin/soffice
```

If LibreOffice cannot be installed, use `PPTX_LEGACY_ENABLED=false`. Native PPTX files and normal audio/video continue to work. If FFmpeg cannot be installed, video extraction, unusual audio normalization, and multi-clip presentation merging will not work.

#### 2. Create a locked-down service account and install the app

```bash
sudo useradd --system --create-home --home-dir /opt/gamas-bot \
  --shell /usr/sbin/nologin bot

sudo git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git /opt/gamas-bot
sudo chown -R bot:bot /opt/gamas-bot

sudo -u bot python3 -m venv /opt/gamas-bot/.venv
sudo -u bot /opt/gamas-bot/.venv/bin/pip install --upgrade pip
sudo -u bot /opt/gamas-bot/.venv/bin/pip install -r /opt/gamas-bot/requirements.txt
sudo -u bot mkdir -p /opt/gamas-bot/data/tmp /opt/gamas-bot/data/.cache

sudo -u bot cp /opt/gamas-bot/.env.example /opt/gamas-bot/.env
sudo chmod 600 /opt/gamas-bot/.env
sudo nano /opt/gamas-bot/.env
```

Test once as the service user before enabling systemd:

```bash
cd /opt/gamas-bot
sudo -u bot env HOME=/opt/gamas-bot/data \
  XDG_CACHE_HOME=/opt/gamas-bot/data/.cache \
  /opt/gamas-bot/.venv/bin/python -m gamas_bot
```

After the bot reports that it is online, stop this foreground test with `Ctrl+C`.

#### 3. Install and start the systemd service

The provided unit assumes `/opt/gamas-bot`, stores LibreOffice/fontconfig cache under writable `data/`, writes application output to journald, and restricts filesystem access:

```bash
sudo cp /opt/gamas-bot/deploy/gamas-bot.service /etc/systemd/system/gamas-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now gamas-bot
sudo systemctl status gamas-bot --no-pager -l
sudo journalctl -u gamas-bot -f
```

Useful operations:

```bash
sudo systemctl restart gamas-bot
sudo systemctl stop gamas-bot
sudo journalctl -u gamas-bot --since today --no-pager
sudo journalctl -u gamas-bot -p warning..alert --since '1 hour ago'
```

When updating:

```bash
sudo systemctl stop gamas-bot
sudo -u bot git -C /opt/gamas-bot pull --ff-only
sudo -u bot /opt/gamas-bot/.venv/bin/pip install -r /opt/gamas-bot/requirements.txt --upgrade
sudo systemctl start gamas-bot
```

Back up `data/bot.sqlite3` **together with its `-wal` and `-shm` files while the service is stopped**, plus `.env`. The Telegram session can be recreated but backing up `data/telegram_bot.session` avoids a new login/session handshake.

### FFmpeg details and troubleshooting

FFmpeg is used to inspect streams, extract the first audio track from video, normalize unsupported codecs, and concatenate presentation narration. The bot executes argument arrays without a shell and enforces `FFMPEG_TIMEOUT_SECONDS`.

Common checks:

```bash
# Does the input actually contain audio?
ffprobe -v error -show_streams -show_format -of json /path/to/input.mp4

# Can the service user write temporary files and run FFmpeg?
sudo -u bot touch /opt/gamas-bot/data/tmp/write-test
sudo -u bot /usr/bin/ffmpeg -hide_banner -version

# Check disk and inode exhaustion
df -h /opt/gamas-bot/data
df -i /opt/gamas-bot/data
```

`Unknown encoder 'libopus'` means the host has a restricted FFmpeg build. Install the distribution's full `ffmpeg` package or set a build that includes libopus. `Permission denied` generally means the executable path or `data/tmp` ownership is wrong. Increase `FFMPEG_TIMEOUT_SECONDS` only after checking CPU load, disk throughput, and corrupt input files.

### LibreOffice details and troubleshooting

LibreOffice is always invoked headlessly with a unique temporary user profile, so parallel conversions do not contend for one profile lock. The systemd unit still assigns a writable `HOME` and `XDG_CACHE_HOME` for fontconfig and LibreOffice caches.

Test conversion with a real legacy file:

```bash
sudo -u bot env HOME=/opt/gamas-bot/data \
  XDG_CACHE_HOME=/opt/gamas-bot/data/.cache \
  timeout 60 /usr/bin/soffice --headless --convert-to pptx \
  --outdir /opt/gamas-bot/data/tmp /path/to/sample.ppt
```

If `soffice --version` works interactively but fails under systemd, check the service user's write permissions, `HOME`, `XDG_CACHE_HOME`, fonts, and `journalctl`. Hanging conversions are killed after `SOFFICE_TIMEOUT_SECONDS`; do not disable the timeout. Minimal containers may also need `libreoffice-core`, `libreoffice-impress`, fontconfig, and at least one font package—not only the `soffice` launcher.

### PaaS and containers

Configure the process as a worker command:

```text
python -m gamas_bot
```

The image/build layer must install FFmpeg and LibreOffice; Python packages alone are insufficient. Mount a persistent volume at `/app/data` (or update `DATABASE_PATH`, `TELEGRAM_SESSION_PATH`, and `TEMP_DIR`). Do not run more than one replica against the same Telegram session or SQLite file. Local model URLs such as `127.0.0.1:11434` only work if that model server runs in the same container/VM; otherwise use its private network hostname.

### Windows hosting

Use a dedicated Windows server/VPS with one of:

- Windows Task Scheduler (run whether the user is logged on or not)
- [NSSM](https://nssm.cc/) as a Windows service wrapper
- Another process supervisor that restarts failed workers

Install FFmpeg and LibreOffice with WinGet, use their full `.exe` paths in `.env` if services have a different `PATH`, grant the service account modify permission on `data`, and redirect stdout/stderr or set `LOG_FILE=data/logs/bot.log`.

### Logging and error handling

Default production logs go to stdout/journald and include provider attempts, durations, external-tool exit codes, job IDs, migrations, startup dependency checks, retry events, and tracebacks. Transcript/prompt contents and API keys are not intentionally logged. Users receive a stable reference such as `GMS-000123` on job failure; search it together with the submission ID in server logs.

```dotenv
LOG_LEVEL=INFO                 # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FORMAT=text                # text or one-JSON-object-per-line
LOG_FILE=                      # empty for journald; or data/logs/bot.log
LOG_MAX_BYTES=10000000
LOG_BACKUP_COUNT=5
```

When `LOG_FILE` is set, Python performs size-based rotation internally. Do not configure a second `logrotate` rule for the same file. For central collection (Loki, ELK, Cloud Logging), prefer `LOG_FORMAT=json` and stdout.

Troubleshooting sequence:

1. `systemctl status gamas-bot -l`
2. `journalctl -u gamas-bot -n 200 --no-pager`
3. Verify `.env` ownership/mode and all three executable paths.
4. Run the exact foreground test as the `bot` user.
5. Check disk/RAM and API `401`, `403`, `429`, or `5xx` entries.
6. Use the user's `GMS-...` reference to identify the failed submission.

A `status=203/EXEC` systemd error means `ExecStart` is wrong or the virtual environment is missing. A SQLite `readonly database` error means the `bot` user cannot write `data/` (or `ReadWritePaths` does not match your customized path). Repeated API `429` errors require lower concurrency/rate, a larger provider quota, or a longer retry window—not a process restart.

---

## Useful Commands

```bash
python --version       # check Python
ffmpeg -version        # check FFmpeg
ffprobe -version
soffice --version      # check LibreOffice

pip install -r requirements.txt --upgrade   # update dependencies
```

Stop the bot with `Ctrl + C` (or `systemctl stop gamas-bot` under systemd).

---

## Quick Start

```powershell
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git
cd Gamas_bot

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

Copy-Item .env.example .env
notepad .env

python -m gamas_bot
```

Send a voice message, audio file, video, or PowerPoint presentation to your Telegram bot.

---

## License

Add your project license here (for example, the MIT License).

---

## Author

**Amirali Ghamkhar**
GitHub: <https://github.com/AmiraliGhamkhar>
