# Gamas Bot

Persian Telegram study assistant. Send lecture audio/video for transcription and an RTL Word
booklet, send PDF/Word/PowerPoint documents for text extraction and study notes, or open **🗂 آفیس رایگان**
to turn messages and `.txt`, `.md`, `.markdown`, `.csv`, `.tsv` files into real Word, Excel,
PowerPoint and selectable-text PDF files. Free Office and document-note actions do not consume wallet
credit or the existing free quiz/flashcard uses. Saved outputs and source references are available in
the personal library; PowerPoint narration remains a separate explicit action on the source card.

Runs on Windows for local use and on Linux cPanel shared hosting for production.

## Requirements

- Python 3.11–3.13. On cPanel, Terminal/SSH, Cron Jobs and outbound TCP 443.
- Telegram API credentials and bot token from [my.telegram.org](https://my.telegram.org) and
  [@BotFather](https://t.me/BotFather).
- At least one STT key (Deepgram, Speechmatics, or an OpenAI-compatible endpoint). Note-provider
  keys are optional: without them audio still delivers transcripts, while Free Office and document
  notes use deterministic, source-preserving fallbacks.

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

- `/start` explains the lecture flow and shows 📚 Library, 💳 Wallet, 🗂 آفیس رایگان,
  🏆 Leaderboard, 🎧 Support and ℹ️ Help (admins also see their separate admin panel). The Office
  button is hidden when `OFFICE_ENABLED=false`.
- Send audio, video or a Telegram voice/video note. The bot delivers the transcript first, then the
  RTL Word booklet when note generation is configured. After sending a presentation, select
  «🎧 پردازش صدای ارائه» on its document card to run the existing presentation/audio pipeline.
- Delivered transcript and booklet messages are indexed in the personal library as Telegram message
  references, not copied to local disk. Users can forward a saved file back to themselves later.
- The Wallet combines remaining balance, plan purchase and recent payment history. Card-to-card
  receipts remain subject to administrator review.
- Send a study document (PDF, Word `.docx`, PowerPoint) to extract its text instead of transcribing
  it: the bot stores the extraction in the library under **📄 اسناد**, shows a preview, and offers a
  complete source-grounded booklet, distinct compact revision notes, Word/PDF exports from the full
  extracted text, the original-file download, plus the existing **🃏 فلش‌کارت** / **❓ سؤالات**.
  PDF page, Word section and PowerPoint slide locators are retained in cached chunks and note
  outputs. Original files remain in Telegram and are never overwritten. Re-uploading an identical
  file reuses the cached extraction per user (SHA-256) and refreshes its Telegram reference. Files up
  to `DOC_MAX_BYTES` (default 50 MB) are accepted, with a separate 5,000,000-character extraction
  limit; Office note/export work refuses oversized source text rather than silently truncating it.
  `.ppt` uses the existing converter; for unsupported `.doc`, save as `.docx` and resend. Scanned
  PDFs are refused and, with `OCR_ENABLED=true`, their text-poor pages are read by the opt-in OCR
  service ([docs/OCR.md](docs/OCR.md)); PowerPoint narration/audio is separate and starts only when
  its explicit button is pressed.
- **🗂 آفیس رایگان** accepts pasted text/messages or `.txt`, `.md`, `.markdown`, `.csv` and `.tsv`
  files and creates real `.docx`, `.xlsx`, `.pptx` or selectable-text `.pdf` outputs. The default
  per-draft limit is 100,000 characters, uploaded text files are limited to 1,000,000 bytes, and
  office-like table/slide limits reject overflow instead of trimming content. Excel stores user text
  as literal strings (including formula-looking cells); it does not calculate formulas. Generated
  PowerPoint is a new deck from text, not a pixel-perfect conversion of an arbitrary source deck.
  `OFFICE_ENABLED` defaults to `true`; see [Free Office](docs/OFFICE.md) for limits and behavior.
  With no note provider, Office still works using deterministic extractive notes and a basic
  source-grounded booklet. Configured provider calls may incur operator-side API costs, but users are
  never charged wallet credit or free quiz/flashcard allowances for Office.
- Quizzes and AI flashcard decks offer «⚡ سریع» (10 cards / 5 questions), «⭐ استاندارد» (20/10)
  and «🎯 کامل» (40/20). Source-anchored items are generated in parallel chunks with live progress,
  then shown for review and deletion before saving. Incomplete coverage and shortfalls are reported.
- Each user has one independent free AI deck and one free quiz, with the same models and presets.
  After each is used, the normal credit flow applies; the wallet appears if credit is insufficient.
  Failed, cancelled or expired previews restore the gift. A new user's existing free credit grant is
  separate from these two gifts.

## Processing support

- Audio, video and Telegram voice/video notes → transcript, then notes/booklet when a note provider
  is configured. `.pptx` / legacy `.ppt` → document extraction first; narrated presentation
  processing is optional from the document card. Without a note provider, audio yields transcript only,
  while document notes and Free Office use bounded deterministic fallbacks.
- STT: Deepgram, Speechmatics, OpenAI-compatible.
- Notes: Gemini, NaraRouter, OpenRouter, Groq, Cerebras, Anthropic, OpenAI-compatible.
- SQLite billing ledger with card-to-card purchases reviewed by an admin.

OCR is **opt-in and third-party**: by default a PDF that yields less than ~10 characters per page,
and any photo, is refused with a Persian explanation instead of storing an empty document. With
`OCR_ENABLED=true` (and `pip install 'gamas-bot[ocr]'`) the text-poor pages of a scan, or an
uploaded photo, are recognised by i2ocr.com and stored like any other document — see
[docs/OCR.md](docs/OCR.md) for the protocol review, limits and privacy trade-off. Free Office is an
explicit menu workflow (`OFFICE_ENABLED=true` by default); plain text is collected only after a user
starts that workflow. Its temporary text drafts expire after `OFFICE_SESSION_TTL_MINUTES` and are
cleared on cancel, navigation or shutdown.

## Documentation

[Documentation index](docs/README.md) · [Configuration](docs/CONFIGURATION.md) ·
[cPanel deployment](docs/DEPLOY_CPANEL.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

Back up `.env`, the database and the Telegram session before upgrades. Database migrations run at
startup. Payments are manual; uploading a receipt does not add credit.

## License

Proprietary — all rights reserved. The bundled Vazirmatn font is licensed separately; see
`assets/fonts/LICENSE.txt`.
