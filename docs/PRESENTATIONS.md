# PowerPoint presentations

The bot reads slide text and extracts linked audio/video narration from supported PowerPoint packages. Legacy `.ppt` files are converted in-process. ODP/OTP and text embedded only in images are not supported; OCR is not implemented.

## Narration, timing and credit

Usable narration is combined into one STT request per deck. When a provider returns timestamps, text is assigned to a slide only if the timing fits that slide's audio span. Untimed, unlinked or ambiguous text is labeled as unmapped instead of guessed. Unreferenced media is excluded unless `PRESENTATION_INCLUDE_UNREFERENCED_MEDIA=true`.

Billing uses the measured source narration duration. Synthetic silence between clips counts toward `PRESENTATION_MAX_TOTAL_DURATION` but is not billed.

## Shared-host settings

| Setting | Purpose |
| --- | --- |
| `PRESENTATION_MAX_CLIPS` | Maximum playback uses in one deck |
| `PRESENTATION_MAX_TOTAL_DURATION` | Maximum combined STT audio duration, including gaps |
| `PRESENTATION_MAX_UNPACKED_BYTES` | Limit for expanded deck contents |
| `PRESENTATION_MAX_TEMP_BYTES` | Scratch-space limit for one deck |
| `MEDIA_WORKER_CONCURRENCY` | Heavy media jobs running at once; use `1` on cPanel |

Keep `TEMP_DIR` on a writable local disk and leave enough free space for the configured limits. Limits reject work; they do not reserve disk space.
