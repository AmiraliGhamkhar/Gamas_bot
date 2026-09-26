# Gamas Bot

> **Persian Telegram Lecture Notes Assistant** — turn voice messages, audio, video, and PowerPoint presentations into structured Persian lecture notes.

Gamas Bot is a Python-based Telegram bot built with [Telethon](https://github.com/LonamiWebs/Telethon) (MTProto) for processing Persian lectures.

It accepts audio, voice messages, videos, and PowerPoint presentations, converts speech to text using **Speechmatics** or **Deepgram**, and optionally generates structured lecture notes using **Gemini 2.5 Flash-Lite**.

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
- Gemini-powered note generation
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
         Gemini 2.5 Flash-Lite
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
- Gemini API key (optional)

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

GEMINI_API_KEY=YOUR_GEMINI_KEY
```

At least one of the two STT API keys is required.

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

| Provider | Role | Console |
|---|---|---|
| Speechmatics | Primary STT engine (default) | <https://portal.speechmatics.com/> |
| Deepgram | Alternative/fallback STT engine | <https://console.deepgram.com/> |
| Gemini | Converts transcripts into structured lecture notes | <https://aistudio.google.com/apikey> |

Gemini is optional. If unavailable, the raw transcript is preserved and delivered instead.

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

## Commands

### User commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | Usage guide |

Users can then send a voice message, audio file, video file, or PowerPoint presentation.

### Admin commands

| Command | Description |
|---|---|
| `/users` | List up to 50 most recent users |
| `/stats` | Usage statistics |
| `/broadcast <message>` | Send a message to all non-banned users |
| `/ban <user_id>` | Ban a user |
| `/unban <user_id>` | Unban a user |

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

Audio is sent to the configured STT provider. If Gemini is enabled, the following may be sent for note generation:

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
│   ├── structuring.py     # Gemini note generation
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
│
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Production Deployment

Gamas Bot maintains a persistent Telethon/MTProto connection, so it must run as a long-lived process. It is not designed for stateless serverless environments or traditional shared hosting.

### Linux (systemd)

A ready-made unit file is provided in `deploy/gamas-bot.service`. It assumes the project lives in `/opt/gamas-bot` with a virtual environment in `/opt/gamas-bot/.venv` and runs as the `bot` user:

```bash
sudo useradd --system --home /opt/gamas-bot bot
sudo cp deploy/gamas-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gamas-bot
sudo journalctl -u gamas-bot -f
```

Adjust `WorkingDirectory`, `EnvironmentFile`, `ExecStart`, and `User`/`Group` in the unit file if your paths differ.

### Windows

Recommended options:

- Windows Task Scheduler
- [NSSM](https://nssm.cc/)
- Dedicated Windows server or VPS

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
