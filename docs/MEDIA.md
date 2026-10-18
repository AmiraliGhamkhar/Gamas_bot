# Media pipeline

The bot processes audio and video **without any system media binary**. There is
no `ffmpeg`, no `ffprobe` and no LibreOffice in the media path — installing the
Python dependencies is the whole requirement.

## How it works

`gamas_bot/media.py` runs `gamas_bot/media_worker.py` as a child process:

```
bot → media.py → asyncio.create_subprocess_exec(sys.executable, "-m", "gamas_bot.media_worker", …)
                 fixed argument vector, shell=False, hard timeout
```

* **Process isolation** buys two things: a decoder crash cannot take the bot
  down, and the hard timeout can kill a hung operation (cancelling a thread
  cannot).
* **`shell=False` with a fixed argument vector** means no user-controlled value
  ever reaches a shell. A file named `; rm -rf /` is just a file name.
* The worker reports structured JSON on stdout; a non-zero exit becomes
  `MediaToolError`. Merge JSON includes sample counts and a per-input clip span,
  so presentation orchestration can align timestamped STT output without
  re-probing the merged file.

`media_worker.py` uses **PyAV** (`av`), which ships its own FFmpeg *libraries*
inside the wheel. Probing, extraction, resampling and merging all go through the
library API.

A deck's clips are probed as a **batch**: one worker process walks the whole
deck, because the interpreter and PyAV import cost is paid once instead of per
clip. A clip that cannot be probed becomes an `error` entry in that report — one
broken file never hides the others — and if the batch itself fails (crash,
timeout, unparseable output) the files are re-probed one process each.

## Commands

| Worker subcommand | `media.py` entry point | Used for |
| --- | --- | --- |
| `probe` | `probe_media` / `probe_media_many` | duration and whether a stream carries audio — billing depends on the duration |
| `extract` | `extract_audio_track` | video → audio, and any audio that must be normalized before STT |
| `merge` | `merge_audio_tracks` | concatenating per-slide narration clips and returning source/output durations plus sample-accurate clip spans |
| `convert` | `convert_to_pptx` | legacy `.ppt` → `.pptx`, in process |
| `check` | `check_media_worker` | startup self-check that the worker imports and runs |

## Limits and safety

* `MAX_FILE_SIZE` bounds an upload before and after download.
* One operation is bounded by `MEDIA_TIMEOUT`; legacy `.ppt` conversion by
  `CONVERT_TIMEOUT`.
* Temporary work is confined to `TEMP_DIR` and removed in a `finally` block;
  stale `submission-*` / `deck-*` / `office-*` directories from a crash are
  removed at startup, before any handler runs (`GamasBot._sweep_stale_temp_dirs`).
  Only directories older than the process start are touched, so a sibling start
  that has not yet taken the instance lock is never robbed.
* An audio extraction whose duration cannot be measured is never rendered as an
  unbounded PCM WAV: the format selector always receives a byte budget and falls
  back to Opus when WAV would not fit.
* `needs_transcode()` re-encodes only when the container or codec is one the STT
  providers do not accept, so a normal upload is not re-encoded blindly. A single
  compatible slide-linked audio clip follows the same passthrough rule; video,
  multiple playback uses, and incompatible codecs are decoded/merged.
* `MEDIA_WORKER_CONCURRENCY` (default 1) caps the bot's CPU/disk-heavy media
  stages independently of `MAX_ACTIVE_JOBS`. Presentation extraction also obeys
  `PRESENTATION_MAX_TEMP_BYTES`; see [PRESENTATIONS.md](PRESENTATIONS.md).
* A `.webm` document with missing or generic MIME metadata is treated as video
  (the suffix is shared with audio), so its audio track is extracted before STT
  instead of sending a screen recording's video stream to the speech service.
  Explicit `audio/*` MIME types keep the audio-upload path.

## Verifying the claim

`python -m gamas_bot.media_worker check` proves `av` and `ppt2pptx` import in the
deployment environment, and `scripts/cpanel_preflight.py` runs it as part of the
host checklist. No code path in `media.py` or `media_worker.py` names a system
binary — grep is the check:

```bash
grep -rn "ffmpeg\|ffprobe\|soffice" gamas_bot/   # comments only
```
