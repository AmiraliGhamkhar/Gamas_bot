# PowerPoint presentations

## Media selection and playback order

The reader follows the deck's slide order and each slide's internal audio/video
and Office `p14:media` relationships (deduplicating redundant `media` links when
the same slide already references that target via an explicit audio/video
relationship). A media part referenced from two slides is treated as two playback
uses (while its bytes are extracted only once), so the merged timeline retains
both slide positions. Media parts without a direct slide relationship are
excluded by default. Set `PRESENTATION_INCLUDE_UNREFERENCED_MEDIA=true` to opt
into those files; the raw transcript lists any excluded media as a skip reason.

Video audio can be included with `PRESENTATION_INCLUDE_VIDEO_AUDIO` (default
`true`). Silent clips, unreadable clips, clips below
`PRESENTATION_MIN_CLIP_SECONDS`, and unsupported media are skipped with reasons.

## One STT request and slide attribution

Usable narration is submitted to STT once per deck. The media worker returns
sample-accurate source duration, output duration and each clip's `[start, end]`
span in that one output. It does not need a second probe of the merged file.
A single compatible audio clip is passed through unchanged; video audio,
multiple clips, and incompatible audio are decoded/merged as needed.

When STT returns timestamps, a word or segment is attached to a slide when its
timestamp interval lies inside (or within a bounded `80 ms` edge-jitter
tolerance of) a single span belonging to a slide-linked clip with at least half
of its duration inside that clip. Text wholly in synthetic gaps, orphan media,
clip-boundary-crossing segments, or responses without usable timestamps stays in
an explicit **unmapped narration** section. The bot never assigns untimed text to
the nearest slide. Slide-aware note input keeps each aligned narration block
beside its slide text, lists all slide headings per chunk in the lecture context
digest, and splits oversized slides or unmapped narration blocks with explicit
continuation headings only when necessary.

## Duration, billing and storage limits

`PRESENTATION_MAX_TOTAL_DURATION` caps the **merged STT audio duration**, including
the configured synthetic silence between clips. `PRESENTATION_SILENCE_SECONDS`
defaults to `0.25` seconds and is bounded to `0–60` seconds. The measured source
audio duration excludes that synthetic silence and is what the user is charged
for; both source and merged durations are stored in the in-memory preparation
result and logged/used consistently. A cap violation discovered after decoding
fails before the STT call and does not consume credit.

`PRESENTATION_MAX_UNPACKED_BYTES` bounds the OOXML package, and
`PRESENTATION_MAX_TEMP_BYTES` (default 8 GiB) bounds the source deck, extracted
media and prepared audio in one job. `MAX_FILE_SIZE` still applies to the upload,
each extracted clip and the STT artifact. Before merging, the remaining scratch
budget is passed to the format selector; a compact Opus output is chosen when a
WAV would exceed that budget, and work is rejected before decoding if even the
conservative Opus estimate cannot fit. A final byte-count check catches actual
outputs that exceed the estimate. `MEDIA_WORKER_CONCURRENCY` (default 1,
maximum 8) limits CPU/disk-heavy media stages independently of the overall job
queue. Provision `TEMP_DIR` for the configured per-job budget and active worker
count; the setting is an application guard, not a disk reservation.

## Slide text and limits of extraction

Text from `python-pptx` is merged with a package-XML fallback **per slide**, so a
slide that is partly readable does not prevent recovery of its missing text.
Speaker notes, SmartArt, chart labels, and up to 60 cached values per chart
series are included. Text that exists only as an image is not OCR'd. OCR,
voice-activity detection and denoising remain out of the default pipeline: they
need a representative Persian/code-switch benchmark and measured CPU, memory,
latency, and accuracy results before they can be enabled or described as an
improvement.

## Relevant settings

| Setting | Default | Meaning |
| --- | ---: | --- |
| `PRESENTATION_MAX_CLIPS` | 300 | Maximum playback uses, including repeated relationships |
| `PRESENTATION_MAX_TOTAL_DURATION` | 21600 s | Maximum merged STT duration, including gaps |
| `PRESENTATION_MAX_UNPACKED_BYTES` | 4 GiB | Total uncompressed OOXML package size |
| `PRESENTATION_MAX_TEMP_BYTES` | 8 GiB | Per-job temporary file budget |
| `PRESENTATION_INCLUDE_UNREFERENCED_MEDIA` | `false` | Opt in to package media not linked to a slide |
| `PRESENTATION_INCLUDE_VIDEO_AUDIO` | `true` | Include audio tracks from linked video clips |
| `PRESENTATION_SILENCE_SECONDS` | 0.25 s | Gap inserted between merged playback uses |
| `MEDIA_WORKER_CONCURRENCY` | 1 | Simultaneous media-heavy stages |

Synthetic media tests cover relationship selection, reused media, exact clip
spans, source-vs-output duration, no-guess timestamp attribution, per-slide XML
fallback, chart values, temporary-storage limits, and passthrough of a compatible
single clip. These tests exercise mechanics, not recognition accuracy or
performance; no speed/quality gain is claimed without a separate benchmark.
