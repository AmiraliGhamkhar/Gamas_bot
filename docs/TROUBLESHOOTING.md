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
| "Credit is not enough" | Balance is measured in seconds; open «💳 حساب من» to buy a plan or grant credit as admin |
| Receipt sent but no credit | Expected: an admin must approve the payment |

## Document issues

- OCR on images and scanned PDFs is not supported.
- The Free Office bot UI is retired; bare text messages show the lecture-upload guide instead.
- The Word topic list uses links, not page numbers; the server does not run a Word layout engine.
- If Word offers to repair a generated file, save it and report the `GMS-#####` reference, Word and
  Windows versions. Do not send `.env`, API keys or transcripts.
- Persian text needs the bundled Vazirmatn font installed on the viewing computer; see
  [Word output](DOCX.md).

See [Operations](OPERATIONS.md) for update and backup steps.
