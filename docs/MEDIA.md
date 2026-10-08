# Media pipeline

The bot processes audio and video **without any system media binary**. There is
no `ffmpeg`, no `ffprobe` and no LibreOffice in the media path — installing the
Python dependencies is the whole requirement.

## How it works

`gamas_bot/media.py` runs `gamas_bot/media_worker.py` as a child process:

```
bot → media.py → asyncio.create_subprocess_exec(sys.executable, "-m", "gamas_bot.media_worker", ...)
                   fixed argument vector, shell=False, hard timeout
```

* **Process isolation** buys two real things: a decoder crash cannot take the
  bot down, and the hard timeout can kill a hung operation (cancelling a thread
  cannot).
* **`shell=False` with a fixed argument vector** means no user-controlled value
  ever reaches a shell. A file name such as `; rm -rf /` is a file name.
* The worker reports structured results on stdout (a JSON report) and
  FFmpeg-level errors on stderr; non-zero exit codes are propagated as
  `MediaToolError`.

`media_worker.py` uses **PyAV** (`av`), which ships its own FFmpeg *libraries*
inside the wheel. Probing, audio extraction, resampling and multi-clip merging
all happen through the library API.

A presentation's clips are probed as a batch: one worker process walks the
whole deck, because ~90% of a single probe's wall time was interpreter + PyAV
import paid once per clip (measured on the reference VM: ~106 ms per clip
sequentially vs ~9 ms inside an already-started worker; a 40-clip deck probes
in ~0.1 s instead of ~4.3 s). A clip that cannot be probed becomes an `error`
entry in that process's report — one broken file never hides the others — and
if the batch itself fails (crash, timeout, unparseable output), the files are
re-probed with one isolated process each, exactly like before.

## Operations

| Operation | Used for |
| --- | --- |
| `probe_media` | duration, whether a stream carries audio (billing depends on the duration) |
| `probe_media_many` | probing every clip of a deck in **one** worker process |
| `extract_audio_track` | video → audio, and any audio that must be normalised before STT |
| `merge` | concatenating per-slide narration clips from a presentation |
| `check_media_worker` | startup self-check that the worker imports and runs |

## Limits and safety

* `MAX_FILE_SIZE_BYTES` bounds an upload before and after download.
* One operation is bounded by `MEDIA_TIMEOUT_SECONDS`; legacy `.ppt` conversion
  by `PPT_CONVERT_TIMEOUT_SECONDS`.
* Temporary work is confined to `TEMP_DIR` and removed in a `finally` block;
  stale `submission-*`/`deck-*` directories from a crash are removed at startup.
* `needs_transcode()` only re-encodes when the container/codec is one the STT
  providers do not accept, so a normal upload is not re-encoded blindly.
* A `.webm` document with missing or generic MIME metadata is treated as video
  (the suffix is shared with audio), so the audio track is extracted before STT
  instead of sending a screen recording's video stream to the speech service.
  Explicit `audio/*` MIME types continue to use the audio-upload path.

## Verifying the no-binary claim

`tests/test_media_runtime.py` asserts that no command executed anywhere in the
media path names `ffmpeg`, `ffprobe` or `soffice`, and `scripts/cpanel_preflight.py`
checks that `av` imports in the deployment environment.
