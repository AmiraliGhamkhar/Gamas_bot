# Gamas Bot

Persian Telegram study assistant: turn lecture audio, video, PowerPoint or
documents into a transcript, a right-to-left Word/PDF booklet, quizzes,
flashcards and Office exports.

Runs on **Windows with PowerShell** or on **shared Linux hosting with cPanel**.
One process per bot token, SQLite only — no Docker, Redis, external database,
system FFmpeg, LibreOffice or payment gateway.

## Quick start

Both platforms need Python **3.11–3.13**, Git, a BotFather token and
`api_id`/`api_hash` from [my.telegram.org](https://my.telegram.org).

**Windows PowerShell** (full guide: [docs/WINDOWS.md](docs/WINDOWS.md))

```powershell
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git
Set-Location Gamas_bot
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env                       # fill the required values
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe -m gamas_bot
```

**cPanel Terminal** (full guide: [docs/CPANEL.md](docs/CPANEL.md))

```bash
cd "$HOME" && git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git gamas_bot
cd "$HOME/gamas_bot"
python3.11 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env
python -m scripts.cpanel_preflight
python -m gamas_bot --check
python -m gamas_bot
```

Minimum `.env` values: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`,
`TELEGRAM_API_HASH`, numeric `ADMIN_IDS` and **one** speech-to-text key
(`SPEECHMATICS_API_KEY`, `DEEPGRAM_API_KEY` or `STT_OPENAI_API_KEY`).
Note-generation keys are optional: without them audio still delivers a
transcript and documents/Office use local fallbacks. Restart after any `.env`
change — settings are read once at startup.

### AI provider defaults

Audio transcription defaults to Speechmatics (`enhanced`), then Deepgram
(`nova-3`), then an OpenAI-compatible transcription API (`whisper-1`). Text
generation is restricted to **AvalAI** (`deepseek-v4.1-flash`), **Gemini**
(`gemini-3.5-flash-lite`) and **OpenRouter**
(`nvidia/nemotron-3.5-super-vl:free`). The default available chain is
`avalai,gemini,openrouter`; booklet work and large/difficult quizzes prefer
AvalAI, while smaller learning and Office tasks prefer Gemini. OpenRouter is
always the last fallback, never promoted by preferences, and is excluded for
unclassified (`UNKNOWN`) content. Missing credentials skip a route; stale
removed-provider settings are ignored safely without logging their values.
Each booklet's source is classified once (`NOTE_DATA_SENSITIVITY`, `auto` by
default and failing closed to `UNKNOWN`) and that label is reused for repair and
compilation. A booklet that still contains numbers or terms the transcript never
said is rejected by a deterministic final gate: the user keeps the transcript
and the submission is reported as `partial`, never as a delivered booklet.

Operating defaults remain 131,072 context tokens, 64,000 characters per chunk
and 16,384 output tokens, with `NOTE_COMPILE=false` and no hedged requests.
These are application budgets, not model/account guarantees. No live provider
or deployment compatibility is implied by offline tests. See
[Providers](docs/PROVIDERS.md) for task policy, quota budgets and safe probes.

## Features

| Input or menu | Result |
| --- | --- |
| Audio, video, voice note | Transcript first, then an RTL Word/PDF booklet when a note provider is configured |
| PDF, DOCX, PowerPoint | Extracted text, source-based notes, exports and per-slide mapping |
| Free Office / آفیس رایگان | Text, TXT, Markdown, CSV or TSV to Word, Excel, PowerPoint or PDF |
| Library | Saved Telegram file references |
| Wallet | Credit, plans and manually reviewed receipts |
| Support | Persistent support tickets |

PowerPoint narration starts only from its audio button. Scans and photos need
[opt-in third-party OCR](docs/FEATURES.md#ocr-opt-in-third-party). Details:
[features](docs/FEATURES.md) · [note providers](docs/PROVIDERS.md) ·
[billing](docs/BILLING.md).

## Before production

- Keep the project, `.env` and `data/` outside every web root, including `public_html`.
- Run **one** instance per token/session; never run a Windows and a cPanel copy together.
- The membership gate is on by default (`SUBSCRIPTION_CHANNEL=@gamas_AI`); make the
  bot a channel admin or disable the gate for testing.
- Back up before updates. Migrations run at startup; never run older code against a
  newer database.
- A receipt adds no credit until an administrator approves it.

## Documentation

Start at [docs/README.md](docs/README.md). Short version:
[Windows](docs/WINDOWS.md) · [cPanel](docs/CPANEL.md) ·
[Configuration](docs/CONFIGURATION.md) · [Operations](docs/OPERATIONS.md) ·
[Troubleshooting](docs/TROUBLESHOOTING.md).

## License

Proprietary — all rights reserved. The bundled Vazirmatn font has its own
[license](assets/fonts/LICENSE.txt).
