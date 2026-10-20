# Troubleshooting

## Bot does not answer

1. Run `python -m gamas_bot --check` with the same Python environment as the bot.
2. On cPanel, run `python -m scripts.cpanel_preflight` and check `data/logs/launcher.err` and `data/logs/bot.log`.
3. Confirm `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and numeric `ADMIN_IDS` are correct.
4. Make sure only one process is using the bot token/session and the host permits outbound TCP 443.

## Upload fails

Use the `GMS-#####` reference in the log to find the cause. Check the upload limit, free disk space and `TEMP_DIR`. On cPanel, a process that disappears without an error may have been killed by the host's CPU, memory or process limits; reduce concurrency and file limits or use a VPS.

| Symptom | Check |
| --- | --- |
| File too large | `MAX_FILE_SIZE` and the host's upload/disk limits |
| No audio found | The media has no readable audio track; try another file |
| Every STT provider fails | Key, model, language, provider status and outbound network; see [STT providers](STT_PROVIDERS.md) |
| Only the raw transcript arrives | Note generation is optional; check note-provider keys and log errors in [Providers](PROVIDERS.md) |
| Note request times out | Increase `NOTE_REQUEST_TIMEOUT_SECONDS` or lower `NOTE_MAX_OUTPUT_TOKENS` |
| `av` or `ppt2pptx` import fails | Reinstall `requirements.txt`; on cPanel, check the OS/Python compatibility in preflight |
| Duplicate replies | Stop the extra bot process. Run one bot instance per token/session. |

## Document issues

- OCR on images/scanned PDFs is not supported. For Office generation, upload text or a supported text file, not `.docx` or `.pdf`.
- The Word topic list uses links and does not show page numbers; page layout is not rendered on the server.
- If Microsoft Word offers to repair a generated file, save the file and report the `GMS-#####` job reference plus the application/version. Do not send `.env`, API keys or user transcripts.

See [Operations](OPERATIONS.md) for update and backup steps.
