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

## AI note generation

`config.from_env()` reads `.env` **once at startup** into one frozen settings
object. `note_routes()` turns `NOTE_PROVIDERS` into the ordered failover chain;
each route keeps its own key, API base and model, and an uncredentialed route is
skipped. `llm.py` builds the provider-specific request (native Gemini
`generateContent` or OpenAI-compatible chat completions), retries only bounded
transient failures, and moves to the next route on a recoverable provider error.
`notes.py` then parses strict JSON, validates the booklet shape and runs the
grounding/quality gates before rendering the RTL Word booklet. A truncated
completion is refused, never delivered as a partial note. Logs contain metadata
and token counts only — never keys, prompts or transcript text.

Shipped **project defaults** are `NOTE_PROVIDERS=gemini,nara,openrouter,deepseek,avalai`,
`GEMINI_MODEL=gemini-2.5-flash-lite`, `OPENROUTER_MODEL=google/gemma-4-31b-it:free`,
`NOTE_MAX_OUTPUT_TOKENS=16384` and structured output enabled. They are not a
deployment recommendation. The profile below was verified by real metadata and
generation tests at one deployment on 2026-11-07 (see
[provider documentation](docs/PROVIDERS.md)); keys always stay in `.env`:

```dotenv
NOTE_PROVIDERS=gemini,openrouter
NOTE_STRUCTURED_OUTPUT=0
NOTE_REASONING_EFFORT=none
NOTE_MAX_OUTPUT_TOKENS=32768
NOTE_GEMINI_THINKING_BUDGET=-1
GEMINI_MODEL=gemini-3.5-flash-lite
OPENROUTER_MODEL=nvidia/nemotron-3.5-super-vl:free
```

NaraRouter remains fully configurable in the codebase but is excluded from this
chain: its `laguna-s-2.1` generation test failed with HTTP 502. Model
entitlement alone never proves that generation works. Provider settings are read
only at startup, so **restart the bot after editing `.env`**. Verify with
`python -m scripts.probe_note_providers --show-config` (offline), then the
metadata probe, then the opt-in `--generate` probe (consumes quota).

## Before production

- Keep the project, `.env` and private data outside all web roots, including `public_html`.
- Run one instance per token/session; do not run Windows and cPanel copies together.
- The membership gate is on by default. Make the bot a channel admin or explicitly
  disable the gate for testing: [channel setup](docs/TELEGRAM_SUBSCRIPTION.md).
- Back up before updates. Migrations run at startup; never downgrade against a newer database.
- A receipt does not add credit until a human administrator approves it.

[Documentation](docs/README.md) · [Configuration](docs/CONFIGURATION.md) ·
[Note providers](docs/PROVIDERS.md) · [Operations](docs/OPERATIONS.md) ·
[Troubleshooting](docs/TROUBLESHOOTING.md)

## License

Proprietary — all rights reserved. Bundled Vazirmatn has a separate
[font license](assets/fonts/LICENSE.txt).
