# Gamas Bot

Persian Telegram study assistant. Send lecture audio/video or a PowerPoint deck—no mode button
needed—and get the transcript first, followed by an RTL Word booklet when note generation is
configured. Delivered transcript and booklet files are saved to the Telegram-backed personal
library; balance, subscription purchase and recent payments live together in «💳 حساب من».

The former Free Office bot UI is retired. Its standalone export modules remain available to code
and their offline tests, but text-to-document jobs are no longer accepted by the bot.

Runs on Windows for local use and on Linux cPanel shared hosting for production.

## Requirements

- Python 3.11–3.13. On cPanel, Terminal/SSH, Cron Jobs and outbound TCP 443.
- Telegram API credentials and bot token from [my.telegram.org](https://my.telegram.org) and
  [@BotFather](https://t.me/BotFather).
- At least one STT key (Deepgram, Speechmatics, or an OpenAI-compatible endpoint). Note-provider
  keys are optional; without them the bot delivers transcripts only.

No system `ffmpeg`, `soffice`, Redis, Docker or external database is needed.

## Windows PowerShell (local)

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

Fill `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ADMIN_IDS` and one STT key in
`.env`. Stop with **Ctrl+C**. Never commit or share `.env`.

## cPanel shared hosting

```bash
cd "$HOME"
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git gamas_bot
cd "$HOME/gamas_bot"
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
nano .env
python -m scripts.cpanel_preflight
python -m gamas_bot --check
```

Then add the once-per-minute cron job from [Deploy on cPanel](docs/DEPLOY_CPANEL.md). Keep the
project, `.env`, database, session, logs and receipts **outside `public_html`**. Shared hosts may
kill long processes; run one active worker and test the host before relying on it.

## User flow

- `/start` explains the one-action flow and shows 📚 Library, 💳 Wallet, 🎧 Support and ℹ️ Help
  (admins also see their separate admin panel).
- Send audio, video, a Telegram voice/video note or a PowerPoint deck directly in the chat. The bot
  delivers the transcript first, then the RTL Word booklet when note generation is configured.
- Delivered transcript and booklet messages are indexed in the personal library as Telegram message
  references, not copied to local disk. Users can forward a saved file back to themselves later.
- The Wallet combines remaining balance, plan purchase and recent payment history. Card-to-card
  receipts remain subject to administrator review.

## Processing support

- Audio, video, Telegram voice/video notes, `.pptx` and legacy `.ppt` → transcript, then notes/booklet
  when a note provider is configured. Without one, the bot delivers the transcript only.
- STT: Deepgram, Speechmatics, OpenAI-compatible.
- Notes: Gemini, NaraRouter, OpenRouter, Groq, Cerebras, Anthropic, OpenAI-compatible.
- SQLite billing ledger with card-to-card purchases reviewed by an admin.

OCR for images and scanned PDFs is **not** supported. The former Free Office bot UI is retired;
plain text and text-file submissions no longer start document jobs. `OFFICE_ENABLED` defaults to
`false`; the pure export modules remain in the repository for reuse and testing.

## Documentation

[Documentation index](docs/README.md) · [Configuration](docs/CONFIGURATION.md) ·
[cPanel deployment](docs/DEPLOY_CPANEL.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

Back up `.env`, the database and the Telegram session before upgrades. Database migrations run at
startup. Payments are manual; uploading a receipt does not add credit.

## License

Proprietary — all rights reserved. The bundled Vazirmatn font is licensed separately; see
`assets/fonts/LICENSE.txt`.
