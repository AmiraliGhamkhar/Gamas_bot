# Gamas Bot

Persian Telegram study assistant for transcription, study booklets, quizzes,
flashcards and document exports. Runs on **Windows PowerShell** or **Linux cPanel**.

## Start here

1. Install Python **3.11–3.13** and Git.
2. Get a bot token from [BotFather](https://t.me/BotFather) and API credentials
   from [my.telegram.org](https://my.telegram.org).
3. Follow [Windows PowerShell](docs/DEPLOY_WINDOWS.md) or [cPanel](docs/DEPLOY_CPANEL.md).
4. Fill `.env`, run the checks and test with a short audio file.

Required: Telegram credentials, numeric `ADMIN_IDS` and at least one STT key.
Note-provider keys are optional. No system FFmpeg, LibreOffice, Redis, Docker or
external database is needed. Hosting must allow background processes and outbound
Telegram/provider connections.

## Features

| Input or menu | Result |
| --- | --- |
| Audio, video, voice note | Transcript first; RTL Word booklet when a note provider is configured |
| PDF, DOCX, PowerPoint | Extracted text, source-based notes and exports |
| Free Office / آفیس رایگان | Text, TXT, Markdown, CSV or TSV to Word, Excel, PowerPoint or PDF |
| Library | Saved Telegram file references and learning actions |
| Wallet | Credit, plans and manually reviewed receipts |
| Support | Persistent support tickets |

PowerPoint narration starts only from its audio button. Scans/photos need
[opt-in third-party OCR](docs/OCR.md). Without note keys, document notes and Office
use deterministic fallbacks; audio delivers only the transcript. Office/document
notes spend no user credit or learning gifts, but AI calls may cost the operator.

## Before production

- Keep the project, `.env` and private data outside all web roots, including `public_html`.
- Run one instance per token/session; do not run Windows and cPanel copies together.
- The membership gate is on by default. Make the bot a channel admin or explicitly
  disable the gate for testing: [channel setup](docs/TELEGRAM_SUBSCRIPTION.md).
- Back up before updates. Migrations run at startup; never downgrade against a newer database.
- A receipt does not add credit until a human administrator approves it.

[Documentation](docs/README.md) · [Configuration](docs/CONFIGURATION.md) ·
[Operations](docs/OPERATIONS.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

## License

Proprietary — all rights reserved. Bundled Vazirmatn has a separate
[font license](assets/fonts/LICENSE.txt).
