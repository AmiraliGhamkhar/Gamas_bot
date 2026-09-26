Gamas Bot

«Persian Telegram Lecture Notes Assistant — turn voice messages, audio, video, and PowerPoint presentations into structured Persian lecture notes.»

""Python" (https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)" (https://www.python.org/)
""Telegram" (https://img.shields.io/badge/Telegram-MTProto-26A5E4?logo=telegram&logoColor=white)" (https://telegram.org/)
""SQLite" (https://img.shields.io/badge/Database-SQLite-003B57?logo=sqlite&logoColor=white)" (https://www.sqlite.org/)
""FFmpeg" (https://img.shields.io/badge/Media-FFmpeg-007808?logo=ffmpeg&logoColor=white)" (https://ffmpeg.org/)

Gamas Bot is a Python-based Telegram bot built with Telethon and MTProto for processing Persian lectures.

It accepts audio, voice messages, videos, and PowerPoint presentations, converts speech to text using Speechmatics or Deepgram, and optionally generates structured lecture notes using Gemini 2.5 Flash-Lite.

---

Features

- Persian speech-to-text ("fa")
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
- Windows-friendly PowerShell setup

Supported audio

MP3
M4A
WAV
OGG
FLAC
WMA
AMR
and other FFmpeg-supported formats

Supported video

MP4
MKV
MOV
AVI
Telegram Video Notes

Supported presentations

PPTX
PPTM
PPSX
PPT
PPS
ODP

---

How It Works

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

---

Requirements

- Windows 10/11
- Python 3.11+
- FFmpeg
- FFprobe
- LibreOffice (only required for legacy PowerPoint formats)
- Telegram Bot Token
- Telegram API ID + API Hash
- At least one STT API key
- Gemini API key (optional)

---

Installation — Windows

1. Install FFmpeg

Using WinGet:

winget install Gyan.FFmpeg

Verify:

ffmpeg -version
ffprobe -version

2. Install LibreOffice

Only required for:

PPT
PPS
ODP

Install:

winget install TheDocumentFoundation.LibreOffice

Verify:

soffice --version

---

3. Clone the Repository

git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git
cd Gamas_bot

---

4. Create a Virtual Environment

py -3.11 -m venv .venv

Activate it:

.\.venv\Scripts\Activate.ps1

If PowerShell blocks activation:

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

Then:

.\.venv\Scripts\Activate.ps1

---

5. Install Dependencies

python -m pip install --upgrade pip
pip install -r requirements.txt

---

6. Configure Environment Variables

Create ".env":

Copy-Item .env.example .env

Open it:

notepad .env

Minimal configuration:

TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
TELEGRAM_API_ID=YOUR_API_ID
TELEGRAM_API_HASH=YOUR_API_HASH

ADMIN_IDS=123456789

SPEECHMATICS_API_KEY=YOUR_SPEECHMATICS_KEY
DEEPGRAM_API_KEY=YOUR_DEEPGRAM_KEY

GEMINI_API_KEY=YOUR_GEMINI_KEY

At least one of the STT API keys is required.

---

Telegram Credentials

Create your bot using @BotFather.

You need:

TELEGRAM_BOT_TOKEN=...

Telethon also requires:

TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...

Get these from:

https://my.telegram.org

---

API Providers

Speechmatics

Primary STT engine.

https://portal.speechmatics.com/

Deepgram

Optional alternative/fallback STT engine.

https://console.deepgram.com/

Gemini

Used to convert transcripts into structured lecture notes.

https://aistudio.google.com/apikey

Gemini is optional. If unavailable, the raw transcript is preserved.

---

STT Configuration

Example:

STT_PRIMARY=speechmatics
STT_LANGUAGE=fa

STT_FALLBACK_ENABLED=true
STT_MIN_CONFIDENCE=0.65

SPEECHMATICS_BASE_URL=https://eu1.asr.api.speechmatics.com/v2
DEEPGRAM_MODEL=nova-3

Primary engine

Use Speechmatics:

STT_PRIMARY=speechmatics

Or Deepgram:

STT_PRIMARY=deepgram

Fallback

Enable:

STT_FALLBACK_ENABLED=true

Disable:

STT_FALLBACK_ENABLED=false

When enabled, the secondary engine can be used if the primary request fails or reports confidence below:

STT_MIN_CONFIDENCE=0.65

«Confidence scores from different providers are not necessarily calibrated against each other. Tune this threshold using your own validation dataset.»

---

PowerPoint Processing

PowerPoint files receive additional processing.

                    PowerPoint
                        │
             ┌──────────┼──────────┐
             ▼          ▼          ▼
          Slides      Audio      Video
           Text       Clips       Audio
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

The processor:

1. Reads the actual presentation slide order.
2. Extracts slide text.
3. Extracts speaker notes.
4. Finds referenced audio.
5. Optionally extracts audio from embedded videos.
6. Filters very short audio clips.
7. Converts audio to mono 16 kHz.
8. Adds short silence between clips.
9. Sends the combined audio to STT.
10. Combines transcript + slide content.
11. Generates structured notes.

PowerPoint configuration

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

Disable PowerPoint support:

PPTX_ENABLED=false

Presentations without audio can still produce notes from slide text and speaker notes.

---

Processing Limits

Default application file limit:

MAX_FILE_SIZE_BYTES=2000000000

This is an application-level limit and does not guarantee that an STT provider accepts the file.

For very long recordings, provider-specific request limits still apply.

The application does not automatically split arbitrary long audio files into smaller STT requests.

Control concurrent processing with:

MAX_CONCURRENT_JOBS=2

Increase this only after testing CPU, memory, disk, network, and API limits.

---

Run

Activate the virtual environment:

.\.venv\Scripts\Activate.ps1

Start the bot:

python -m gamas_bot

The application automatically creates the required database and data directories on first startup.

---

Database

Gamas Bot uses SQLite with:

- WAL mode
- Foreign keys
- Busy timeout
- Versioned migrations

Migration files:

migrations/
├── 001_initial.sql
└── 002_presentations.sql

Configure paths with:

DATABASE_PATH=...
TELEGRAM_SESSION_PATH=...
TEMP_DIR=...

Temporary media is removed after processing.

Transcripts and generated notes remain in SQLite until explicitly deleted.

---

User Commands

/start
/help

Users can then send:

Voice message
Audio file
Video file
PowerPoint presentation

---

Admin Commands

/users
/stats
/broadcast <message>
/ban <user_id>
/unban <user_id>

Administrators are defined with:

ADMIN_IDS=123456789,987654321

Only configured administrator IDs can execute administrative commands.

---

Input Handling

Input| Processing
Telegram voice| Speech-to-text
Audio file| Speech-to-text
Video| Extract audio → STT
Video Note| Extract audio → STT
PPTX/PPTM/PPSX| Slides + audio + notes
PPT/PPS/ODP| LibreOffice → PPTX
PDF/Image/ZIP| Rejected with instructions
Plain text| Ignored

Files without an audio stream are detected before consuming STT resources.

GIF files are not treated as lecture videos.

---

Testing

Run all tests:

python -m unittest discover -s tests -v

STT Benchmark

Prepare samples:

samples/
├── short.wav
├── short.wav.txt
├── lecture-long.mp3
└── lecture-long.mp3.txt

Run:

python -m scripts.benchmark_stt `
    samples\short.wav `
    samples\lecture-long.mp3 `
    --output results.csv

The benchmark can compare:

- Response time
- Provider confidence
- Normalized Persian WER

Persian normalization includes:

ي → ی
ك → ک

The benchmark does not store full transcripts in the CSV report.

---

Security

PowerPoint packages are validated before extraction.

Checks include:

- Path traversal protection
- Absolute-path rejection
- Unpacked-size limits
- Media reference validation

Never commit secrets or runtime data.

The following should remain local:

.env
*.session
*.db
temporary files
API keys

---

Privacy

Audio is sent to the configured STT provider.

If Gemini is enabled, the following may be sent for note generation:

- Transcript
- Slide text
- Speaker notes

Temporary presentation and media files are deleted after processing.

The local database may contain:

- User information
- Job status
- Raw transcripts
- Generated notes
- Presentation metadata
- Presentation clip metadata

Before production deployment, review:

- Provider data-retention policies
- User consent requirements
- Applicable privacy regulations
- Data residency
- Your application's retention/deletion policy

---

STT Evaluation

Speechmatics and Deepgram both support Persian, but real-world accuracy depends on:

- Recording quality
- Microphone
- Background noise
- Speaker
- Accent
- Technical vocabulary
- Medical/engineering terminology

Do not assume that one provider is universally better for Persian academic lectures.

For production evaluation, use the same recordings and manually verify domain-specific terminology.

---

Project Structure

Gamas_bot/
│
├── gamas_bot/
│   ├── __main__.py
│   └── ...
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
│
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md

---

Windows Production

Gamas Bot maintains a persistent Telethon/MTProto connection, so it should run as a persistent process.

Recommended Windows options:

- Windows Task Scheduler
- NSSM
- Dedicated Windows server
- VPS with Windows

It is not designed for stateless serverless environments or traditional shared hosting.

---

Useful PowerShell Commands

Check Python

python --version

Check FFmpeg

ffmpeg -version
ffprobe -version

Check LibreOffice

soffice --version

Update dependencies

pip install -r requirements.txt --upgrade

Stop the bot

Ctrl + C

---

Quick Start

git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git
cd Gamas_bot

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

Copy-Item .env.example .env
notepad .env

python -m gamas_bot

Send a voice message, audio file, video, or PowerPoint presentation to your Telegram bot.

---

License

Add your project license here.

Example:

MIT License

---

Author

Amirali Ghamkhar

GitHub:
https://github.com/AmiraliGhamkhar
