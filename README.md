# Gamas Bot

> **Persian Telegram Lecture Notes Assistant** — turn voice messages, audio, video, and PowerPoint presentations into structured Persian lecture notes.

Gamas Bot is a Python-based Telegram bot built with [Telethon](https://github.com/LonamiWebs/Telethon) (MTProto) for processing Persian lectures.

It accepts audio, voice messages, videos, and PowerPoint presentations, converts speech to text using **Speechmatics**, **Deepgram**, or an optional **OpenAI-compatible STT endpoint**, and optionally generates structured lecture notes using **Gemini, Anthropic, or any OpenAI-compatible API** (OpenAI, OpenRouter, Groq, Together, DeepSeek, Ollama, vLLM, and similar services).

> Repository review and validation notes: [`docs/AUDIT.md`](docs/AUDIT.md).

---

## Features

- Persian speech-to-text (`fa`)
- Telegram voice messages and audio files
- Video-to-text processing
- PowerPoint lecture processing
- Slide audio extraction and merging
- Slide text and speaker-note extraction
- Slide-by-slide lecture notes
- Speechmatics + Deepgram STT, plus optional OpenAI-compatible STT
- Configurable STT fallback and per-provider upload-size routing
- Detailed, privacy-safe STT metrics logging (attempts, timings, polls, confidence, word counts — never transcript text)
- Provider-neutral note generation (Gemini, Anthropic, OpenAI-compatible APIs) behind a strict JSON-only system prompt with validation and a bounded repair pass
- Polished right-to-left Word (.docx) deliverable: complex-script fonts, shaded summary/callout boxes, RTL tables, Persian (Jalali) date header, page-number footer
- Raw-transcript companion `.txt` file sent alongside every result
- Playful animated progress bar: rocket-head bar, cycling spinner frames, stage emoji and a celebration on completion
- Inline glass-button menus for users and administrators
- Per-job editable progress bars
- Background processing for long jobs
- SQLite + WAL
- Automatic temporary-file cleanup
- Long-message splitting for Telegram (fallback path when document generation fails)
- Admin commands and user management
- Windows-friendly PowerShell setup and a Linux systemd unit

### Supported formats

| Type | Formats |
|---|---|
| Audio | MP3, M4A, WAV, OGG, FLAC, WMA, AMR and other formats PyAV's bundled FFmpeg libraries can read |
| Video | MP4, MKV, MOV, AVI, Telegram video notes |
| Presentations (native) | PPTX, PPTM, PPSX, PPSM, POTX, POTM |
| Presentations (legacy, converted in-process) | PPT, PPS, POT |
| Presentations (rejected with instructions) | ODP, OTP — re-save as PPTX first |

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
      │        PyAV Worker   Slide Parser
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
  (strict JSON system prompt)
                   │
                   ▼
     Validated structured notes
                   │
                   ▼
   RTL Word (.docx) + raw-text (.txt)
                   │
                   ▼
                Telegram
```

---

## Requirements

- Windows 10/11 or Linux
- Python 3.11+
- Telegram bot token
- Telegram API ID + API hash
- At least one STT credential (Speechmatics key, Deepgram key, or an OpenAI-compatible STT endpoint)
- A note-generation API (Gemini, Anthropic, or OpenAI-compatible; optional)

No system media or office software is required: stream probing, audio
extraction/merging and legacy `.ppt` conversion run inside Python through the
[`av`](https://pyav.org/) (PyAV) and [`ppt2pptx`](https://github.com/HuiTurn/ppt2pptx)
packages, and Word documents are written by the pure-Python
[`python-docx`](https://python-docx.readthedocs.io/) package. The FFmpeg
*libraries* ship inside the `av` wheel — no `ffmpeg`, `ffprobe` or `soffice`
binary is ever executed.

---

## Installation — Windows

### 1. Clone the repository

```powershell
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git
cd Gamas_bot
```

### 2. Create a virtual environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure environment variables

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
| OpenAI-compatible STT | Optional engine (`POST /audio/transcriptions`): OpenAI, Groq, or a local vLLM/Ollama gateway | your endpoint |

The note-generation and STT layers are both provider-extensible: each STT
engine is one entry in a registry with an availability check and its
direct-upload size cap, so a future engine can join the fallback chain without
rewriting the pipeline.

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
# Only for openai_compatible providers that implement response_format:
NOTE_API_JSON_MODE=false
# Note-generation mode: full (default; preserves explanations, examples and
# procedures), standard (balanced) or summary (intentionally concise):
NOTE_MODE=full
```

#### Strict JSON structured output

The note API is driven by a strict system prompt (sent as the OpenAI
`system` message, the Anthropic `system` field, or Gemini's
`systemInstruction` plus native `responseMimeType: application/json`).
The model must answer with a single JSON object:

```json
{
  "title": "…",
  "summary": "…",
  "sections": [
    {
      "heading": "…",
      "paragraphs": ["…"],
      "bullets": ["…"],
      "definitions": [{"term": "…", "term_en": "…", "definition": "…"}],
      "examples": ["…"],
      "steps": ["…"],
      "formulas": ["…"],
      "key_points": ["…"],
      "table": {"headers": ["…"], "rows": [["…"]]},
      "callouts": [{"kind": "نکته | هشدار | یادآوری", "text": "…"}]
    }
  ],
  "key_points": ["…"],
  "glossary": [{"term": "…", "definition": "…"}]
}
```

The answer is validated and normalised in code: fenced code blocks,
surrounding prose and trailing commas are repaired; unknown fields and
empty sections are dropped; callout kinds are normalised. An invalid
answer triggers exactly one bounded repair pass before the job falls
back to delivering the raw material. Long transcripts are chunked at
paragraph (then sentence) boundaries so definitions, procedures and
worked examples are never cut mid-unit; every chunk carries a short
positional context line instead of duplicated overlapping text, and the
per-chunk JSON notes are merged additively in order (only exact
duplicate strings are deduplicated). After merging, a deterministic
coverage check compares numbers with units, percentages, dosages,
blood-pressure pairs and English technical terms in the notes against
the source chunks and logs any gaps — the notes themselves are never
silently rewritten, and nothing is ever invented to fill a gap.

`NOTE_API_JSON_MODE=true` additionally sends
`response_format: {"type": "json_object"}` to OpenAI-compatible
gateways — it is opt-in because not every compatible service implements
it (Gemini's native JSON mode is always enabled).

Transient `429` and `5xx` responses and network failures are retried with bounded backoff. Raw provider error bodies are never logged, but on a failed request the bot parses the provider's structured error metadata (Gemini `error.status`/`details[].reason`, OpenAI `error.type`/`code`, and the bounded error message) and logs it with the key redacted — so a Gemini `HTTP 400` such as `API_KEY_INVALID` is visible and fixable in the logs without exposing keys or lecture content. Gemini authentication uses a header rather than a URL query parameter, and a `models/`-prefixed model name is accepted and normalized. Responses blocked by provider safety filters are reported with their block reason. If a provider explicitly reports an output-token limit, the incomplete note is rejected and the bot delivers the extracted source material instead. Increase `NOTE_API_MAX_OUTPUT_TOKENS` only within the selected model’s limits.

---

## Deliverables: Word document + raw text

Every finished job is delivered as **two documents** instead of a long
chat message:

1. **`جزوه - <title> - GMS-XXXXXX.docx`** — a polished right-to-left Word
   document generated with [python-docx](https://python-docx.readthedocs.io/):
   - RTL paragraphs (`w:bidi`) with **per-direction runs**: Persian text
     and embedded English terms (drug names, units, URLs, numbers such as
     `500 mg` or `120/80`) each keep their own direction and font, which
     is how Word itself models mixed Persian/English text
   - A title block with the Persian (Jalali) date, note-mode label, source
     filename, STT engine and tracking reference
   - Shaded summary box, definition rows, numbered procedure steps,
     verbatim formula lines, per-section key-point boxes, colour-coded
     callouts (نکته / هشدار / یادآوری) and RTL tables with a coloured,
     page-repeating header row
   - The glossary renders as a proper RTL table (اصطلاح / توضیح)
   - A running page header (document title + brand), page-number footers
     and document metadata
   - Fonts configurable per role: `DOCX_FONT` (default `Tahoma`, present
     everywhere) plus optional `DOCX_FONT_BODY`, `DOCX_FONT_HEADING`,
     `DOCX_FONT_LATIN` and `DOCX_FONT_FALLBACK`. The fallback is declared
     in the document's font table (`w:altName`) so readers without the
     primary Persian face substitute it gracefully; fonts are *not*
     embedded in the file.
2. **`متن خام - GMS-XXXXXX.txt`** — the raw extracted texts (the
   transcript, and for presentations the slide text as well) with a
   small metadata header, exactly as produced by the pipeline.

If the note API is unavailable, the Word document is still generated
from the raw material (headings/bullets preserved) and a notice is
attached to its caption. If Word generation itself fails (for example a
broken python-docx installation), the notes fall back to the old
in-chat text message so content is never lost — the `.txt` file is
always sent.

---

## STT Configuration

Example:

```dotenv
STT_PRIMARY=speechmatics
STT_LANGUAGE=fa

STT_FALLBACK_ENABLED=true
STT_MIN_CONFIDENCE=0.65

SPEECHMATICS_BASE_URL=https://eu1.asr.api.speechmatics.com/v2
SPEECHMATICS_MODEL=enhanced
# Optional custom dictionary for drug names / technical terms (comma separated)
SPEECHMATICS_ADDITIONAL_VOCAB=

DEEPGRAM_MODEL=nova-3
```

The pipeline is accuracy-first: `SPEECHMATICS_MODEL=enhanced` is the default
because Speechmatics documents it as the highest-accuracy tier; set `standard`
only if your account lacks the enhanced tier or throughput matters more.
`SPEECHMATICS_ADDITIONAL_VOCAB` feeds the provider's native custom dictionary
(up to 20,000 terms) — useful for Persian lectures full of English drug names
and technical vocabulary; it is a Speechmatics feature, not an LLM layer.

An optional third STT engine works with any OpenAI-compatible
`POST /audio/transcriptions` endpoint (OpenAI, Groq, or a self-hosted
vLLM/Ollama gateway), as a primary or a fallback:

```dotenv
STT_OPENAI_BASE_URL=https://api.openai.com/v1   # empty = engine disabled
STT_OPENAI_API_KEY=                             # optional for local endpoints
STT_OPENAI_MODEL=whisper-1
STT_OPENAI_MAX_UPLOAD_BYTES=25000000
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

`STT_JOB_TIMEOUT_SECONDS` (default `21600`) bounds **each entire provider attempt**,
including upload, polling and transcript download. A fallback attempt has its own
budget; this is not a timeout for the whole Telegram job. Socket-level timeouts
can fail earlier. A local timeout does not delete a Speechmatics job already
submitted to the provider.

If only a non-primary provider is configured, it is used even with fallback
disabled. Every engine has a direct-upload size cap (Speechmatics 1 GB,
Deepgram 2 GB, OpenAI-compatible STT `STT_OPENAI_MAX_UPLOAD_BYTES`): files at
or above a provider's cap are routed to another configured engine that accepts
them, and the job fails fast with a clear message when no engine can take the
file. Audio is never split into chunks for STT, because chunking loses word
context at every boundary. Provider/model availability, language support,
quotas and accepted upload sizes should be confirmed for your account before
production use.

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
         │          PyAV Worker
         │            (audio)
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
7. Converts audio to mono 16 kHz PCM WAV, or 48 kHz Opus when the estimated WAV is too large.
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

# Timeout for one media operation (probe/extract/merge), seconds
MEDIA_TIMEOUT_SECONDS=3600
# Timeout for one legacy .ppt → .pptx conversion, seconds
PPT_CONVERT_TIMEOUT_SECONDS=600
```

The legacy variable names `FFMPEG_TIMEOUT_SECONDS` and
`SOFFICE_TIMEOUT_SECONDS` are still honoured as fallbacks when the new names
are absent, so existing `.env` files keep working. The obsolete
`FFMPEG_BIN`/`FFPROBE_BIN`/`SOFFICE_BIN` settings no longer exist and are
silently ignored.

Disable PowerPoint support entirely:

```dotenv
PPTX_ENABLED=false
```

Presentations without audio can still produce notes from slide text and speaker notes.
Native slideshow/template variants (`ppsx`, `ppsm`, `potx`, `potm`) use the same
slide reader; embedded macros are not executed by the native parser.

All extracted slide text is retained for chunking and raw fallback, rather than
silently cutting off the final slides of a long deck. A short outline is repeated
with narration chunks; a long outline and narration are chunked as a complete
document. There is no timestamp-to-slide alignment, so cross-chunk context and
perfect slide-by-slide narration matching are not guaranteed. More material may
mean more API requests and higher cost.

Clip duration limits use available probe results. Unknown durations cannot be
fully bounded in advance; apply host disk/CPU/memory limits.
Inserted silence is included in WAV-size estimation, but reported narration
duration excludes that silence.

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
This setting bounds active jobs, **not** the number of pending uploads. The queue
is in memory and is not durable; after restart unfinished jobs are marked failed
and must be resubmitted. Restrict access to trusted users until you add admission
limits/rate limiting suitable for a public deployment.

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
- Versioned, transactional migrations
- Rollback of failed multi-statement writes

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

Each accepted upload gets one status message that is edited through the queue, download, media preparation, STT, note-generation, save, and delivery stages. The progress bar is animated: a 🚀 rides the fill edge, a braille spinner cycles on every frame, a stage emoji tells the story (📥 → ⬇️ → 🎚️ → 🎙️ → 📝 → 📖 → 📤), and completion gets a 🎉. While a single stage runs for a long time (STT can take hours), a background ticker keeps re-editing the message with the next spinner frame — every ~5 seconds by default, backing off automatically on Telegram flood waits, and capped so a job can never leak animation edits. Disable the ticker with `PROGRESS_ANIMATION_ENABLED=false`.

Administrators get an additional **پنل مدیریت** button. Statistics, user listing, broadcast, ban, and unban are all available through buttons; actions requiring text or a user ID prompt for the next message and provide a cancel button.

The old `/help`, `/users`, `/stats`, `/broadcast`, `/ban`, and `/unban` commands remain available for backward compatibility and automation, but they are no longer required for normal use.

Administrators are defined with:

```dotenv
ADMIN_IDS=123456789,987654321
```

Only configured administrator IDs can execute administrative commands, and
administrative commands/buttons are restricted to **private chats** with the bot.
Group messages cannot answer a pending private broadcast prompt. Navigation to a
user menu, `/start`, `/help`, or `/cancel` cancels a pending admin action; unknown
slash commands are not broadcast as prompt answers. Non-administrative media
handling remains available in chats where the bot receives messages.

---

## Input Handling

| Input | Processing |
|---|---|
| Telegram voice | Speech-to-text |
| Audio file | Speech-to-text |
| Video | Extract audio → STT |
| Video note | Extract audio → STT |
| PPTX / PPTM / PPSX / PPSM / POTX / POTM | Slides + audio + notes |
| PPT / PPS / POT | ppt2pptx → PPTX → same pipeline |
| ODP / OTP | Rejected with instructions to re-save as PPTX |
| PDF / image / ZIP | Rejected with instructions |
| Plain text | Ignored |

When the probe successfully reads the file, uploads without an audio stream are
rejected before consuming STT resources. GIF files are not treated as lecture
videos.

---

## Testing

Run all tests:

```bash
python -m unittest discover -s tests -v
```

The default suite uses temporary databases, synthetic presentation packages,
real generated media files (WAV/MP3/MP4 via PyAV), mock provider responses and
committed legacy `.ppt` fixtures; no API keys and no system media binaries are
needed. Media smoke tests run the actual in-repo worker on every platform, so
probing, extraction, merging, Opus/WAV output, protocol-whitelist enforcement
and legacy conversion are always exercised. The suite targets Linux/POSIX;
Windows application setup is documented but has not been validated by this CI
matrix.

### Note-quality benchmark

`tests/fixtures/notes/` ships a six-fixture corpus (medical, HCI/university,
computer science, Persian-only, Persian+English code-switching, and a PowerPoint
slide outline). Each transcript is paired with a hand-written reference document
and the coverage floor declared in `corpus.json`.
`tests/test_note_evaluation.py` runs the corpus through chunking, the QA layer,
merging and DOCX rendering and asserts those floors — with no network, provider
or model call, so a prompt or schema change cannot silently regress output
quality. See `tests/fixtures/notes/README.md` for details.

CI (`.github/workflows/tests.yml`) runs the suite and critical static checks on
Python 3.11, 3.12 and 3.13 on Linux. To reproduce additional checks locally:

```bash
python -m pip install ruff pip-audit
python -m pip check
ruff check gamas_bot scripts tests --select E9,F
pip-audit -r requirements.txt
```

A passing dependency audit reports known advisories for the resolved versions;
it is not a guarantee of security. Review dependency changes before upgrading.

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

The benchmark runs every **configured** engine (Speechmatics, Deepgram, and an
OpenAI-compatible endpoint when `STT_OPENAI_BASE_URL` is set) on the same files
and compares:

- Response time
- Provider confidence (where reported)
- Normalized Persian WER
- Per-sample success/failure status, so failure rate can be derived

Persian normalization includes `ي → ی` and `ك → ک` (plus diacritics and punctuation removal). The benchmark does not store full transcripts in the CSV report. Samples at or above an engine's direct-upload threshold are recorded as failed for that engine, not silently benchmarked through another provider under the wrong label. Output parent directories are created automatically.

---

## Security

PowerPoint packages are validated before extraction. Checks include:

- Path traversal protection
- Absolute-path rejection
- Entry-count and unpacked-size limits
- Media reference validation

Media-worker inputs are opened with the local `file`/`pipe` protocol whitelist
so uploaded playlists cannot fetch HTTP or other network URLs. This is **not** a
complete sandbox: the media libraries and legacy-conversion parser still
process untrusted bytes and may access local files readable by their service
account. Keep `av`/`ppt2pptx` updated, use a dedicated non-root account,
filesystem/container isolation and resource limits. On POSIX, timed-out or
cancelled worker processes and their process group are killed; on Windows only
the direct process is explicitly killed, so use a supervisor that cleans up
child processes too.

The unrelated Windows RDP workflow was removed because it used a hard-coded
administrator password and disabled Network Level Authentication. Git history
still contains that password: rotate it anywhere it was reused, terminate any
old runner sessions, and review/revoke their Tailscale access as appropriate.
GitHub Actions CI is not a production bot host.

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
│   ├── instance_lock.py   # single-instance file lock (crash-safe)
│   ├── launcher.py        # "start the bot if it is not running" for cron/Passenger
│   ├── docx_export.py     # RTL Word document + raw-text exporters
│   ├── media.py           # PyAV/ppt2pptx worker helpers (no external binaries)
│   ├── media_worker.py    # child process: probe/extract/merge/convert
│   ├── presentations.py   # PowerPoint parsing and audio extraction
│   ├── progress.py        # animated per-job Telegram progress bars
│   ├── logging_config.py  # text/JSON logging and file rotation
│   ├── structuring.py     # strict-JSON note generation (provider-neutral)
│   └── stt.py             # Speechmatics / Deepgram clients
│
├── migrations/
│   ├── 001_initial.sql
│   └── 002_presentations.sql
│
├── scripts/
│   ├── benchmark_stt.py
│   ├── cpanel_preflight.py # host self-check for cPanel/shared hosting
│   └── ensure_running.py   # cron entry point
│
├── passenger_wsgi.py      # optional cPanel "Setup Python App" status endpoint
│
├── tests/
│
├── deploy/
│   └── gamas-bot.service  # systemd unit for Linux
├── docs/
│   ├── DEPLOY_FA.md        # راهنمای فارسی استقرار و عیب‌یابی
│   ├── DEPLOY_CPANEL.md    # cPanel / shared-hosting deployment
│   └── AUDIT.md            # review findings and validation limits
│
├── .github/workflows/tests.yml
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Production Deployment

> راهنمای کامل فارسی نصب، استقرار، systemd و عیب‌یابی: [`docs/DEPLOY_FA.md`](docs/DEPLOY_FA.md)
>
> **cPanel / shared hosting:** [`docs/DEPLOY_CPANEL.md`](docs/DEPLOY_CPANEL.md) — cron watchdog, optional Passenger status URL and a host preflight (`python scripts/cpanel_preflight.py`). It works only on plans that allow long-running background processes.

Gamas Bot maintains a persistent Telethon/MTProto connection and does not expose an HTTP port. It must run as a long-lived worker. A VPS, dedicated server, container worker, or PaaS **background worker** is suitable; stateless functions (Vercel/Netlify/Lambda), sleeping free tiers, and traditional shared hosting are not — except cPanel plans that pass the checklist in [`docs/DEPLOY_CPANEL.md`](docs/DEPLOY_CPANEL.md).

For the default 2 GB upload limit, plan disk space for the original file, extracted media, converted presentation, and SQLite database. Start with at least 2 vCPU, 4 GB RAM, and 10–20 GB free disk, set `MAX_CONCURRENT_JOBS=1`, observe usage, and only then increase concurrency.

### Ubuntu/Debian: complete systemd setup

#### 1. Install OS packages

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip git ca-certificates
```

No media or office packages are needed: `ffmpeg`, `ffprobe` and `soffice` are
never invoked. Their FFmpeg *libraries* are bundled inside the `av` wheel that
`pip install -r requirements.txt` pulls in, and legacy `.ppt` conversion uses
the pure-Python `ppt2pptx` package.

Verify the Python environment (after the dependencies are installed in step 2)
instead of external binaries:

```bash
cd /opt/gamas-bot
.venv/bin/python -c "import av, ppt2pptx; print(av.__version__)"
# or run the bot's built-in self-check:
.venv/bin/python -m gamas_bot.media_worker check
```

If legacy conversion should be refused outright (for example on very small
hosts), disable it:

```dotenv
PPTX_LEGACY_ENABLED=false
```

Native PPTX files, audio and video continue to work either way.

#### 2. Create a locked-down service account and install the app

```bash
sudo useradd --system --no-create-home --home-dir /opt/gamas-bot \
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

Use `--no-create-home`: `git clone` must create the checkout in a missing or
empty directory, not a home directory already populated with shell skeleton files.
For an existing installation, skip account/clone steps; do not overwrite its data.

Test once as the service user before enabling systemd:

```bash
cd /opt/gamas-bot
sudo -u bot env HOME=/opt/gamas-bot/data \
  XDG_CACHE_HOME=/opt/gamas-bot/data/.cache \
  /opt/gamas-bot/.venv/bin/python -m gamas_bot
```

After the bot reports that it is online, stop this foreground test with `Ctrl+C`.

#### 3. Install and start the systemd service

The provided unit assumes `/opt/gamas-bot`, points `HOME`/`XDG_CACHE_HOME` at writable `data/`, writes application output to journald, and uses `ProtectSystem=strict` with
`data/` as its writable exception. Custom database, session, temporary-file and
file-log paths must stay under that directory or be added to `ReadWritePaths`:

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

### Media worker details and troubleshooting

The bot inspects streams, extracts the first audio track from video, normalizes
unsupported codecs and concatenates presentation narration inside a dedicated
Python child process (`python -m gamas_bot.media_worker`) backed by PyAV. The
parent executes argument arrays without a shell and enforces
`MEDIA_TIMEOUT_SECONDS`; a timed-out or cancelled worker and its process group
are killed.

Common checks:

```bash
# Are the media packages installed in the venv?
/opt/gamas-bot/.venv/bin/python -m gamas_bot.media_worker check

# Does the input actually contain audio?
/opt/gamas-bot/.venv/bin/python -m gamas_bot.media_worker probe -- /path/to/input.mp4

# Can the service user write temporary files?
sudo -u bot touch /opt/gamas-bot/data/tmp/write-test

# Check disk and inode exhaustion
df -h /opt/gamas-bot/data
df -i /opt/gamas-bot/data
```

`media dependencies are missing (run: pip install -r requirements.txt)` means
the `av` wheel was not installed (or the venv is wrong) — reinstall the
requirements inside the service venv. `Permission denied` generally means
`data/tmp` ownership is wrong. Increase `MEDIA_TIMEOUT_SECONDS` only after
checking CPU load, disk throughput, and corrupt input files.

### Legacy presentation conversion details

Legacy `.ppt`/`.pps`/`.pot` decks are converted to `.pptx` in-process by the
`ppt2pptx` package — there is no office suite on the host. Conversions are
bounded by `PPT_CONVERT_TIMEOUT_SECONDS` and by the same
`MAX_FILE_SIZE_BYTES` limit as every other upload. Lossy legacy features that
cannot be carried over (animations, embedded media playback, some OLE objects)
are reported as structured warnings in the logs instead of being silently
dropped.

`ODP`/`OTP` decks are deliberately rejected with an explanatory message; no
verified pure-Python converter exists for them, so users are asked to re-save
the deck as `.pptx`.

Test conversion with a real legacy file:

```bash
sudo -u bot env HOME=/opt/gamas-bot/data \
  XDG_CACHE_HOME=/opt/gamas-bot/data/.cache \
  /opt/gamas-bot/.venv/bin/python -m gamas_bot.media_worker convert \
  --output /opt/gamas-bot/data/tmp/sample.pptx \
  --max-input-bytes 2000000000 \
  -- /path/to/sample.ppt
```

### PaaS and containers

Configure the process as a worker command:

```text
python -m gamas_bot
```

The image/build layer only needs a compatible Python — `pip install -r requirements.txt` pulls in every media dependency. Mount a persistent volume at `/app/data` (or update `DATABASE_PATH`, `TELEGRAM_SESSION_PATH`, and `TEMP_DIR`). Do not run more than one replica against the same Telegram session or SQLite file. Local model URLs such as `127.0.0.1:11434` only work if that model server runs in the same container/VM; otherwise use its private network hostname.

### Windows hosting

Use a dedicated Windows server/VPS with one of:

- Windows Task Scheduler (run whether the user is logged on or not)
- [NSSM](https://nssm.cc/) as a Windows service wrapper
- Another process supervisor that restarts failed workers

Grant the service account modify permission on `data`, and redirect
stdout/stderr or set `LOG_FILE=data/logs/bot.log`. No media/office software
installation is required; the `av` and `ppt2pptx` wheels provide everything.

### Logging and error handling

Default production logs go to stdout/journald and include provider attempts, durations, external-tool exit codes, job IDs, migrations, startup dependency checks, retry events, and local error tracebacks. STT logging is metric-rich but content-free: job start with the routing decision (primary, fallback flag, candidate engines), per-attempt start/completion with elapsed time, confidence, character and word counts, Speechmatics job submission/poll/completion lifecycle (job ID, poll count, per-poll status at DEBUG), transcript-download timings, low-confidence threshold decisions, sanitized failure details, and the final engine-selection summary with the total elapsed time. Transcript/prompt contents and API keys are never logged. Users receive a stable reference such as `GMS-000123` on job failure; search it together with the submission ID in server logs.

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
3. Verify `.env` ownership/mode and the `data/` write permissions.
4. Run the exact foreground test as the `bot` user.
5. Check disk/RAM and API `401`, `403`, `429`, or `5xx` entries.
6. Use the user's `GMS-...` reference to identify the failed submission.

A `status=203/EXEC` systemd error means `ExecStart` is wrong or the virtual environment is missing. A SQLite `readonly database` error means the `bot` user cannot write `data/` (or `ReadWritePaths` does not match your customized path). Repeated API `429` errors require lower concurrency/rate, a larger provider quota, or a longer retry window—not a process restart.

---

## Useful Commands

```bash
python --version       # check Python
python -m gamas_bot.media_worker check   # verify the media stack

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

No license file is currently included. Contact the maintainer about permission to use, modify or redistribute the project; this README does not grant a license.

---

## Author

**Amirali Ghamkhar**
GitHub: <https://github.com/AmiraliGhamkhar>
