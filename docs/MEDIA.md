# Audio and video processing

Media is decoded with PyAV in a child Python process. The project does not call system `ffmpeg`, `ffprobe` or LibreOffice; install the Python packages from `requirements.txt`.

## Practical limits

- `MAX_FILE_SIZE` limits downloads. The bot checks actual download progress and final size.
- `DOWNLOAD_STALL_SECONDS` (default `300`) aborts a Telegram download that produces no bytes for that long. There is no job-level timeout by design, so without this a half-open socket held a worker and its temporary folder indefinitely.
- `TEMP_MIN_FREE_BYTES` (default `2 GiB`) is the free space that must remain on `TEMP_DIR` before a download starts; the check uses the reported upload size (or `MAX_FILE_SIZE` when Telegram reports none) and counts the prepared audio copy as well. A full disk would otherwise surface as an opaque provider failure after a multi-gigabyte download.
- `MEDIA_TIMEOUT` and `CONVERT_TIMEOUT` bound media operations and legacy `.ppt` conversion. `--check` reports a `MEDIA_TIMEOUT` that is too short for a multi-hour lecture (less than a twelfth of `STT_JOB_TIMEOUT`, with a 10-minute floor): decoding a six-hour video is minutes of work even at many times real time.
- `TEMP_DIR` needs free space for source and working files. Temporary job folders are removed after processing and stale folders are cleaned at startup.
- `MEDIA_WORKER_CONCURRENCY` limits CPU/disk-heavy work. Use `1` on shared hosting.
- A file with no measurable audio duration is refused rather than charged blindly. The media pipeline probes the source once and reuses that `MediaInfo` when duration is already known or passthrough audio is kept unchanged.
- Prepared audio is sized to the STT chain's largest upload ceiling (see [Speech-to-text](STT_PROVIDERS.md)); a source already small enough is sent unchanged, and only the first audio track is used. The worker enables multi-threaded PyAV decoding (`thread_type="AUTO"`).
- **Container choice is a time decision.** `AUDIO_FORMAT_POLICY=auto` (default) weighs *encoder CPU + transfer time* for PCM WAV against Opus, using the transfer rate the job's own Telegram download measured: `cost = duration / encode_speed + bytes / rate`. A 2-hour lecture is 230 MB of PCM against 29 MB of Opus, so the PCM upload only wins on a link faster than ~19 MB/s; at 1.5 MB/s Opus is roughly four times cheaper end to end. `AUDIO_FORMAT_POLICY=wav` or `opus` forces one container, still bounded by `PRESENTATION_WAV_LIMIT_BYTES`, `MAX_FILE_SIZE` and the largest configured provider ceiling.
- Opus is encoded with `application=audio, compression_level=0`: the hybrid speech+music path keeps the 4–8 kHz band (fricatives, which carry word identity) that `voip` discards at this bitrate, and the cheap search mode is ~3x faster than the previous level for ~7 % more bytes. Measured with `python -m scripts.perf_bench audio`; the trade-offs are in [the performance plan](PERFORMANCE_PLAN.md).
- `TELEGRAM_DOWNLOAD_CONNECTIONS` (default `1`, the untouched single-stream path) downloads one file as concurrent byte ranges over the same client. **Measure it on your own host before raising it**: any failure falls back to the single stream automatically, but a wrong `BUILD`-side assumption is not worth guessing at. Parallel transfers also bring `FloodWaitError` sooner, which is why the default stays at one.
- Telegram MTProto downloads and document uploads use `cryptg` C-accelerated AES-IGE encryption and 512 KiB chunks (`get_appropriated_part_size`).
- The raw transcript is delivered as one `.txt` file. If that upload fails and the text is too long for the paginated chat fallback, it is re-sent as numbered `.txt` parts (`RAW_FALLBACK_FILE_CHARS`, default `100 000` characters each, at most `RAW_FALLBACK_MAX_FILES`, default `8`). A transcript that would need more parts is refused rather than truncated, and every part must arrive before the job counts as delivered.

Check the installed media stack with:

```powershell
python -m gamas_bot.media_worker check
```

Run the same command with the cPanel virtualenv Python when checking the server. PowerPoint package limits are described in [Presentations](PRESENTATIONS.md).
