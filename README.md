# Gamas Bot

A Persian Telegram study assistant. Send lecture audio/video or a PowerPoint deck to get a raw transcript and an RTL Word booklet. The free Office menu turns text into Word, Excel, PowerPoint or PDF.

## Windows PowerShell: install and run

Use Python 3.11–3.13 and Git. From PowerShell (replace `3.12` below with your installed version if needed):

```powershell
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git
Set-Location Gamas_bot
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.\.venv\Scripts\python.exe -m gamas_bot --check
.\.venv\Scripts\python.exe -m gamas_bot
```

Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ADMIN_IDS`, and at least one STT key in `.env`. Note-generation keys are optional; without them, the bot delivers the transcript only. Stop a foreground run with **Ctrl+C**. Do not share or upload `.env`.

To run tests on Windows, install the `.[dev]` extra and follow [Testing](docs/TESTING.md). Production cPanel hosting runs Linux; use its Terminal, not PowerShell, for the server steps.

## cPanel shared hosting

Follow [Deploy on cPanel](docs/DEPLOY_CPANEL.md). Keep the project, `.env`, database, session and receipts **outside `public_html`**. The host must allow Python 3.11+, outbound TCP 443 and a cron job. Shared-host limits may stop long-running jobs; use one active worker and test the host before relying on it.

## What it supports

- Audio, video, PowerPoint (`.pptx` and legacy `.ppt`) → transcript and RTL `.docx`.
- Text or supported text files → free `.docx`, `.xlsx`, `.pptx` or `.pdf`.
- STT: Speechmatics, Deepgram or an OpenAI-compatible endpoint. Note generation supports Gemini, NaraRouter, OpenRouter, Groq, Cerebras, Anthropic and OpenAI-compatible APIs.
- No system `ffmpeg`, `soffice`, Redis, Docker or external database required. PyAV supplies the media libraries and `cryptg` accelerates Telegram MTProto transfers.

OCR for images and scanned PDFs is **not supported**. Free Office accepts text files, not source `.docx` or `.pdf` documents.

## Operations and help

- [Configuration](docs/CONFIGURATION.md) · [cPanel deployment](docs/DEPLOY_CPANEL.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Billing](docs/BILLING.md) · [Security](docs/SECURITY.md) · [All documentation](docs/README.md)

Back up the database, Telegram session and `.env` before upgrades. Database migrations run when the bot starts. Card-to-card payments require manual admin review; sending a receipt does not add credit.

## License

Proprietary — all rights reserved. See `assets/fonts/LICENSE.txt` for the bundled font license.
