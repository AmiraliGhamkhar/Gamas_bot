# Audio and video processing

Media is decoded with PyAV in a child Python process. The project does not call system `ffmpeg`, `ffprobe` or LibreOffice; install the Python packages from `requirements.txt`.

## Practical limits

- `MAX_FILE_SIZE` limits downloads. The bot checks actual download progress and final size.
- `MEDIA_TIMEOUT` and `CONVERT_TIMEOUT` bound media operations and legacy `.ppt` conversion.
- `TEMP_DIR` needs free space for source and working files. Temporary job folders are removed after processing and stale folders are cleaned at startup.
- `MEDIA_WORKER_CONCURRENCY` limits CPU/disk-heavy work. Use `1` on shared hosting.
- A file with no measurable audio duration is refused rather than charged blindly.

Check the installed media stack with:

```powershell
python -m gamas_bot.media_worker check
```

Run the same command with the cPanel virtualenv Python when checking the server. PowerPoint package limits are described in [Presentations](PRESENTATIONS.md).
