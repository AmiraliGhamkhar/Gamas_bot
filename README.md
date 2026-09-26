Gamas Bot — Persian Telegram Lecture Notes Assistant

A Persian Telegram bot built with Telethon (MTProto) that converts voice messages, audio files, videos, and PowerPoint presentations into structured, readable lecture notes.

The bot uses Speechmatics as the primary STT engine, Deepgram as an optional fallback, and Gemini 2.5 Flash-Lite to generate structured notes.

Features

- Persian speech-to-text from Telegram voice messages and audio files.
- Supports "MP3", "M4A", "WAV", "OGG", "FLAC", and other common formats.
- Supports video files such as "MP4", "MKV", "MOV", and "AVI".
- Extracts audio from videos using "ffmpeg".
- Automatically converts unsupported audio formats/codecs to mono "16 kHz" audio.
- PowerPoint support:
  - "PPTX", "PPTM", "PPSX"
  - Legacy "PPT", "PPS", "ODP"
  - Extracts slide audio and optional video audio.
  - Preserves slide order.
  - Uses slide text and speaker notes.
  - Generates slide-by-slide notes.
- Speech-to-text engines:
  - Speechmatics
  - Deepgram Nova-3
- Configurable primary/fallback STT engine.
- Gemini-based note generation with raw transcript fallback.
- Background processing for long-running jobs.
- SQLite database with WAL mode.
- User, job, transcript, notes, presentation, and broadcast tracking.
- Temporary audio/video files are deleted after processing.
- Long results are automatically split across multiple Telegram messages.
- Admin commands:
  - "/users"
  - "/stats"
  - "/broadcast <message>"
  - "/ban <id>"
  - "/unban <id>"

---

Architecture

Telegram
   │
   ▼
Telethon / MTProto
   │
   ├── Audio / Voice ───────────────┐
   ├── Video ──► FFmpeg ────────────┤
   └── PowerPoint ─► Extract Media ─┤
                                    ▼
                              Audio Preparation
                                    │
                                    ▼
                           Speech-to-Text Engine
                         ┌──────────┴──────────┐
                         │                     │
                    Speechmatics          Deepgram
                         │                     │
                         └──────────┬──────────┘
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
                               Telegram Output

---

Requirements

Software

- Windows 10/11
- Python 3.11+
- FFmpeg + FFprobe
- LibreOffice (only required for legacy "PPT", "PPS", and "ODP" files)

Install FFmpeg and LibreOffice using PowerShell.

Example with WinGet:

winget install Gyan.FFmpeg
winget install TheDocumentFoundation.LibreOffice

Verify:

ffmpeg -version
ffprobe -version
soffice --version

If the executables are not available in "PATH", set their full paths in ".env".

---

Telegram API Credentials

Create a bot with @BotFather and obtain:

TELEGRAM_BOT_TOKEN

Then create Telegram API credentials at:

https://my.telegram.org

You need:

TELEGRAM_API_ID
TELEGRAM_API_HASH

Telethon requires "api_id" and "api_hash" even when authenticating with a bot token.

---

API Keys

At least one STT provider is required.

Speechmatics

https://portal.speechmatics.com/

Deepgram

https://console.deepgram.com/

Gemini

https://aistudio.google.com/apikey

Gemini is optional. If it is unavailable, the raw transcript is still preserved and returned.

---

Installation — Windows PowerShell

Clone the repository:

git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git
cd Gamas_bot

Create a virtual environment:

py -3.11 -m venv .venv

Activate it:

.\.venv\Scripts\Activate.ps1

If PowerShell blocks script execution:

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

Then activate again:

.\.venv\Scripts\Activate.ps1

Install dependencies:

python -m pip install --upgrade pip
pip install -r requirements.txt

Create the environment file:

Copy-Item .env.example .env

Edit ".env":

notepad .env

---

Minimal ".env"

TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=YOUR_API_HASH
ADMIN_IDS=123456789

SPEECHMATICS_API_KEY=YOUR_SPEECHMATICS_KEY
DEEPGRAM_API_KEY=YOUR_DEEPGRAM_KEY
GEMINI_API_KEY=YOUR_GEMINI_KEY

Only one STT key is required.

---

STT Configuration

STT_PRIMARY=speechmatics
STT_LANGUAGE=fa
STT_FALLBACK_ENABLED=true
STT_MIN_CONFIDENCE=0.65

SPEECHMATICS_BASE_URL=https://eu1.asr.api.speechmatics.com/v2
DEEPGRAM_MODEL=nova-3

Behavior

If the primary engine fails, the fallback engine can be used.

A fallback can also be triggered when the primary engine reports confidence below:

STT_MIN_CONFIDENCE=0.65

Set:

STT_PRIMARY=deepgram

to make Deepgram the primary engine.

Disable fallback:

STT_FALLBACK_ENABLED=false

"STT_LANGUAGE" controls speech recognition language only.

Example:

STT_LANGUAGE=fa

---

PowerPoint Processing

PowerPoint files are processed in this order:

Presentation
   │
   ├── Slide text
   ├── Speaker notes
   ├── Slide audio
   └── Video audio
          │
          ▼
      FFmpeg
          │
          ▼
   Combined audio
          │
          ▼
        STT
          │
          ▼
   Slide-by-slide notes

The bot preserves the actual presentation slide order.

Supported formats:

PPTX
PPTM
PPSX
PPT
PPS
ODP

Legacy formats are converted to "PPTX" using LibreOffice.

Useful settings:

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

Disable PowerPoint processing completely:

PPTX_ENABLED=false

Presentations without audio are still processed using slide text and speaker notes.

---

File Size and Processing Limits

Default application limit:

MAX_FILE_SIZE_BYTES=2000000000

This is the bot/application limit and does not guarantee that every STT provider will accept the file.

For example, Speechmatics Batch and Deepgram have different API limits and request behavior. Very large or very long recordings should be tested with the actual provider account before production use.

Long jobs are processed in the background and do not block the Telegram event loop.

Maximum concurrent jobs:

MAX_CONCURRENT_JOBS=2

Increase carefully according to CPU, RAM, disk, network bandwidth, and API limits.

---

Run the Bot

Activate the environment:

.\.venv\Scripts\Activate.ps1

Start:

python -m gamas_bot

The first run creates the required data directories and SQLite tables automatically.

---

Database

The bot uses SQLite with:

- WAL mode
- Foreign keys
- Busy timeout
- Versioned migrations

Migration files are stored in:

migrations/

Current migrations include:

001_initial.sql
002_presentations.sql

The database path can be changed with:

DATABASE_PATH=...

Other paths:

TELEGRAM_SESSION_PATH=...
TEMP_DIR=...

Temporary media is deleted after processing.

Transcripts and generated notes remain in the local SQLite database until manually deleted.

---

Testing

Run the test suite:

python -m unittest discover -s tests -v

For STT benchmarking:

python -m scripts.benchmark_stt samples\short.wav samples\lecture-long.mp3 --output results.csv

For each sample, place the reference transcript next to the audio file:

lecture.mp3
lecture.mp3.txt

The benchmark can compare Speechmatics and Deepgram using:

- Response time
- Reported confidence
- Normalized Persian WER

Persian normalization includes common character variants such as:

ي → ی
ك → ک

The benchmark does not store the full transcription in the CSV report.

---

Telegram Commands

Users

/start
/help

Send one of the following:

Voice message
Audio file
Video file
PowerPoint presentation

Admin

/users
/stats
/broadcast <message>
/ban <user_id>
/unban <user_id>

Only IDs listed in:

ADMIN_IDS=123,456,789

can execute administrative commands.

---

Input Handling

Input| Behavior
Voice message| Direct STT processing
MP3/M4A/WAV/OGG/FLAC| STT processing
MP4/MKV/MOV/AVI| Audio extracted with FFmpeg
Video note| Audio extracted and transcribed
PPTX/PPTM/PPSX| Slides + audio + notes
PPT/PPS/ODP| Converted to PPTX first
PDF/Image/ZIP/etc.| Rejected with usage instructions
Plain text message| Ignored to avoid interfering with normal chat

Files without an audio stream are detected before STT usage.

GIF files are not treated as lecture videos.

---

Security

The bot performs basic safety checks when unpacking PowerPoint files, including:

- Path traversal protection
- Absolute path rejection
- Unpacked-size limits
- Media reference validation

Sensitive files should never be committed to Git.

Do not commit:

.env
*.session
*.db
temporary files
API keys

The repository ".gitignore" should exclude them.

---

Privacy

User audio is sent to the configured STT provider.

If Gemini is enabled, the following may be sent to Gemini for note generation:

- Raw transcript
- Slide text
- Speaker notes

Presentation files and extracted temporary media are deleted after processing.

The local SQLite database stores:

- User information
- Job status
- Raw transcripts
- Generated notes
- Presentation metadata
- Presentation clip information

Review provider retention policies, user consent requirements, applicable regulations, and your own data-retention policy before public deployment.

---

STT Cost and Quality

Speechmatics and Deepgram support Persian ("fa"), but transcription quality depends on:

- Speaker
- Accent
- Microphone
- Background noise
- Recording quality
- Domain-specific vocabulary
- Audio length

Do not assume one engine is universally more accurate for Persian academic lectures.

For production evaluation, benchmark both engines using the same real recordings and manually verify technical terminology.

---

Production Notes

This application maintains a persistent Telethon/MTProto connection.

It is designed for:

- Windows servers
- VPS machines
- Always-on Python processes

It is not designed for:

- Stateless serverless functions
- Traditional shared hosting
- cPanel-only deployments

For Windows production, run the bot as a persistent background process using Windows Task Scheduler, NSSM, or another process supervisor rather than starting it manually.

---

Useful PowerShell Commands

Check Python:

python --version

Check installed packages:

pip list

Update dependencies:

pip install -r requirements.txt --upgrade

Check FFmpeg:

ffmpeg -version

Check LibreOffice:

soffice --version

Stop the running bot:

Ctrl + C

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

License

Add your project license here, for example:

MIT License

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

The bot is then ready to receive Persian audio, video, and PowerPoint lecture files through Telegram.
