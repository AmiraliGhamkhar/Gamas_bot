# PowerPoint presentations

The bot reads slide text and extracts linked audio/video narration from `.pptx` files. Legacy
`.ppt`/`.pps`/`.pot` decks are converted in-process with `ppt2pptx`. ODP/OTP and text that exists
only inside images are not supported by the deck reader; a slide that is a picture needs the opt-in
OCR path ([OCR.md](OCR.md)), which reads images, not decks.

## How it works

- Slide text, speaker notes, diagrams and charts are read from one open `.pptx` archive with
  per-slide relationship caching.
- Narration clips are merged into one STT request per deck, with a short silence between clips.
- When the provider returns word timestamps, words are assigned to a slide only when the timing
  fits that slide's audio span. Untimed or ambiguous text is labelled unmapped instead of guessed.
- Unlinked media is ignored unless `PRESENTATION_INCLUDE_UNREFERENCED_MEDIA=true`.
- A legacy conversion that drops media is reported to the user (for example
  `MEDIA_ACTION_OMITTED`).

## Billing

Billing uses the measured **source narration** duration. Silence inserted between clips counts
toward `PRESENTATION_MAX_TOTAL_DURATION` but is not charged. A deck without narration is free, but
its delivery is still recorded.

## Settings

| Setting | Purpose |
| --- | --- |
| `PRESENTATION_MAX_CLIPS` | Maximum playback uses in one deck |
| `PRESENTATION_MAX_TOTAL_DURATION` | Maximum combined STT audio, including gaps |
| `PRESENTATION_MAX_UNPACKED_BYTES` | Expanded deck-content limit |
| `PRESENTATION_MAX_TEMP_BYTES` | Scratch-space limit for one deck |
| `PRESENTATION_WAV_LIMIT_BYTES` | WAV size ceiling before falling back to Opus |
| `PRESENTATION_SILENCE_SECONDS` | Gap inserted between clips |
| `MEDIA_WORKER_CONCURRENCY` | Heavy jobs at once; use `1` on cPanel |

Keep `TEMP_DIR` on a writable local disk with room for the configured limits. Limits reject work;
they do not reserve disk.
