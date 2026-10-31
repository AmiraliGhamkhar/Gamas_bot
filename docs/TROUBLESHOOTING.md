# Troubleshooting

## Bot does not answer

1. Run the same Python the bot/cron uses:
   ```powershell
   .\.venv\Scripts\python.exe -m gamas_bot --check
   ```
   ```bash
   python -m gamas_bot --check
   ```
2. On cPanel run `python -m scripts.cpanel_preflight` and check `data/logs/bot.log` and
   `data/logs/launcher.err`.
3. Confirm `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and numeric `ADMIN_IDS`.
4. Ensure outbound TCP 443 works and only one process uses the token/session. Check the lock:
   `ps -p "$(cat data/gamas.lock)"`.

## Upload or processing fails

Find the `GMS-#####` reference in the log first.

| Symptom | Check |
| --- | --- |
| File too large | `MAX_FILE_SIZE` and the host's disk/upload limits |
| "No audio track" | The file has no readable audio; try another file |
| Unknown duration | The file's duration cannot be measured; re-encode it and resend |
| All STT providers fail | Key, model, language, provider status, network; see [STT providers](STT_PROVIDERS.md) |
| First STT provider skipped | It is in cooldown after a transient error, or the file exceeds its upload ceiling; the next provider is used |
| Only the transcript arrives | Note generation is optional. Check note-provider keys and log errors; see [Note providers](PROVIDERS.md) |
| Note request times out | Raise `NOTE_REQUEST_TIMEOUT_SECONDS` or lower `NOTE_MAX_OUTPUT_TOKENS` |
| `av` / `ppt2pptx` import fails | Reinstall `requirements.txt`; on cPanel check OS/Python in the preflight |
| `DiskSpaceError` / temp failures | Free space on `TEMP_DIR`; lower `MAX_FILE_SIZE` or `TEMP_MIN_FREE_BYTES` |
| Duplicate replies | Stop the extra bot process; one instance per token/session |
| "Credit is not enough" | Balance is measured in seconds; open «🛒 خرید اشتراک» to buy a plan or grant credit as admin |
| Receipt sent but no credit | Expected: an admin must approve the payment |

## Document and Free Office issues

- If **🗂 دفتر آزاد** is missing, check `OFFICE_ENABLED=true`; the menu intentionally hides it when disabled.
- Only `.txt`, `.md`, `.markdown`, `.csv` and `.tsv` are accepted as Free Office input files. Check
  the file size (`OFFICE_MAX_INPUT_BYTES`) and that its MIME type agrees with the extension. A CSV
  with malformed quoting is rejected rather than partly imported.
- If an export is refused for size, split the source: direct drafts are limited by `OFFICE_MAX_CHARS`,
  while document note/export actions have a hard 250,000-character source ceiling. The original
  extracted document and original Telegram file remain available; the bot does not silently trim.
- Without note-provider keys, Office and document notes still use deterministic fallbacks. A
  provider failure can produce a fallback or a clear refusal if safe compacting is impossible.
- PowerPoint output is a new deck from text, not a pixel-perfect conversion. Narration/audio runs
  only when the explicit source-card button is pressed. No OCR is available for scanned PDFs.
- The Word topic list uses links, not page numbers; the server does not run a Word layout engine.
- If Word offers to repair a generated file, save it and report the `GMS-#####` reference, Word and
  Windows versions. Do not send `.env`, API keys or transcripts.
- Persian text needs the bundled Vazirmatn font installed on the viewing computer; see
  [Word output](DOCX.md).

See [Operations](OPERATIONS.md) for update and backup steps.
