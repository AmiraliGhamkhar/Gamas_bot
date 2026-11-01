# Audio and video

Send audio, video or a Telegram voice/video note. The bot delivers the transcript
first, then a Word booklet if a note provider is available.

## Processing limits

- Upload: `MAX_FILE_SIZE=134217728` (128 MiB); duration:
  `MAX_MEDIA_DURATION_SECONDS=7200` (2 hours).
- Unmeasurable audio duration is refused before charging. Only the first audio track is used.
- PyAV supplies media libraries; no system `ffmpeg`/`ffprobe` is required.
- Media runs in bounded child processes. Keep `MEDIA_WORKER_CONCURRENCY=1` on cPanel.
- `AUDIO_FORMAT_POLICY=auto` chooses WAV/Opus within file/provider limits using
  encoding and transfer costs. Change only after measured tests.
- `TEMP_MIN_FREE_BYTES=67108864` leaves a 64 MiB reserve; downloads also account
  for working copies. Default stall timeout is 300 seconds.
- `TELEGRAM_DOWNLOAD_CONNECTIONS=1` is safest on shared hosting; measure before raising it.

## Transcript delivery

Raw text is sent as `.txt`, with chat/numbered-file fallback when needed.
Defaults allow at most 8 fallback files of 100000 characters. Oversized output
is refused, not truncated. Charging requires complete delivery: [Billing](BILLING.md).

## Check dependencies

```powershell
.\.venv\Scripts\python.exe -m gamas_bot.media_worker check
```

```bash
python -m gamas_bot.media_worker check
```

These checks do not contact providers.
[STT setup](STT_PROVIDERS.md) · [Presentations](PRESENTATIONS.md)
