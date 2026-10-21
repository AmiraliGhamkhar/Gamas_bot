# Audio and video processing

Media is decoded with PyAV in a child Python process. The project does not call system `ffmpeg`, `ffprobe` or LibreOffice; install the Python packages from `requirements.txt`.

## Practical limits

- `MAX_FILE_SIZE` limits downloads. The bot checks actual download progress and final size.
- `DOWNLOAD_STALL_SECONDS` (default `300`) aborts a Telegram download that produces no bytes for that long. There is no job-level timeout by design, so without this a half-open socket held a worker and its temporary folder indefinitely.
- `TEMP_MIN_FREE_BYTES` (default `2 GiB`) is the free space that must remain on `TEMP_DIR` before a download starts; the check uses the reported upload size (or `MAX_FILE_SIZE` when Telegram reports none) and counts the prepared audio copy as well. A full disk would otherwise surface as an opaque provider failure after a multi-gigabyte download.
- `MEDIA_TIMEOUT` and `CONVERT_TIMEOUT` bound media operations and legacy `.ppt` conversion. `--check` reports a `MEDIA_TIMEOUT` that is too short for a multi-hour lecture (less than a twelfth of `STT_JOB_TIMEOUT`, with a 10-minute floor): decoding a six-hour video is minutes of work even at many times real time.
- `TEMP_DIR` needs free space for source and working files. Temporary job folders are removed after processing and stale folders are cleaned at startup.
- `MEDIA_WORKER_CONCURRENCY` limits CPU/disk-heavy work. Use `1` on shared hosting.
- A file with no measurable audio duration is refused rather than charged blindly.
- Prepared audio is sized to the STT chain's largest upload ceiling (see [Speech-to-text](STT_PROVIDERS.md)); a source already small enough is sent unchanged, and only the first audio track is used.
- The raw transcript is delivered as one `.txt` file. If that upload fails and the text is too long for the paginated chat fallback, it is re-sent as numbered `.txt` parts (`RAW_FALLBACK_FILE_CHARS`, default `100 000` characters each, at most `RAW_FALLBACK_MAX_FILES`, default `8`). A transcript that would need more parts is refused rather than truncated, and every part must arrive before the job counts as delivered.

Check the installed media stack with:

```powershell
python -m gamas_bot.media_worker check
```

Run the same command with the cPanel virtualenv Python when checking the server. PowerPoint package limits are described in [Presentations](PRESENTATIONS.md).
