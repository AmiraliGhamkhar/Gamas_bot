# Audio and video processing

Media is decoded by PyAV inside a child Python process. No system `ffmpeg`, `ffprobe` or
LibreOffice is used; install the Python packages from `requirements.txt`.

## Limits

- `MAX_FILE_SIZE` caps downloads; the bot checks the reported size, the download progress and the
  final file size.
- `DOWNLOAD_STALL_SECONDS` (default 300) aborts a Telegram download that produces no bytes.
- `TEMP_MIN_FREE_BYTES` (default 2 GiB) must remain free on `TEMP_DIR` before a download starts.
  The check counts the source and the prepared audio.
- `MEDIA_TIMEOUT` bounds one media operation, `CONVERT_TIMEOUT` bounds legacy `.ppt` conversion.
  `--check` warns when `MEDIA_TIMEOUT` is too short for a multi-hour lecture.
- `MEDIA_WORKER_CONCURRENCY` limits CPU/disk-heavy jobs at once. Use `1` on shared hosting.
- A file with no measurable audio duration is refused instead of being charged at a guess.
- Only the first audio track of a file is transcribed.

## Prepared audio

- An already-supported audio file is sent unchanged when no transcode is needed.
- Otherwise the first audio track is extracted to mono 16 kHz WAV or Opus 48 kHz.
- `AUDIO_FORMAT_POLICY=auto` (default) compares **encoder CPU + upload time** using the transfer
  rate measured during this job's Telegram download, and picks the cheaper container. `wav` and
  `opus` force one, still bounded by `PRESENTATION_WAV_LIMIT_BYTES`, `MAX_FILE_SIZE` and the
  largest configured provider ceiling.
- Opus uses `application=audio, compression_level=0`: the speech+music path keeps the 4–8 kHz band
  that matters for word identity and encodes about 3× faster than the older setting.

## Telegram transfers

- MTProto transfers use `cryptg` and 512 KiB chunks.
- `TELEGRAM_DOWNLOAD_CONNECTIONS` (default `1`) can download one file as concurrent byte ranges on
  the same client. Any failure falls back to the single stream automatically. Measure on your host
  before raising it; parallel transfers reach flood waits sooner.

## Raw transcript delivery

The raw transcript is one `.txt` file. If that upload fails and the text is too long for the
paginated chat fallback, it is re-sent as numbered parts (`RAW_FALLBACK_FILE_CHARS`, default
100 000 characters; at most `RAW_FALLBACK_MAX_FILES`, default 8). A transcript needing more parts is
refused rather than truncated.

## Check the media stack

```powershell
.\.venv\Scripts\python.exe -m gamas_bot.media_worker check
```

```bash
python -m gamas_bot.media_worker check
```

PowerPoint-specific limits are in [Presentations](PRESENTATIONS.md).
