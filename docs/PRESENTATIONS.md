# PowerPoint presentations

Upload a deck for text extraction. To process narration, select
**🎧 پردازش صدای ارائه** on its source card. Upload/export alone never starts STT.

- `.pptx` is read directly; `.ppt`/`.pps`/`.pot` converts with `ppt2pptx` in-process.
- Slide text, speaker notes and supported diagram/chart content are read.
- Linked narration clips form one STT request. Unlinked media is ignored unless
  `PRESENTATION_INCLUDE_UNREFERENCED_MEDIA=true`.
- Timestamped words map to slides only when timing fits. Ambiguous/untimed text
  is labelled unmapped, not guessed. Legacy conversion warnings are reported.
- ODP/OTP and image-only text are unsupported by the deck reader. Send an image/PDF
  through the separate [OCR workflow](OCR.md) when appropriate.

## Defaults

| Setting | Value |
| --- | --- |
| `PRESENTATION_MAX_CLIPS` | 50 playback uses |
| `PRESENTATION_MAX_TOTAL_DURATION` | 7200 seconds, including gaps |
| `PRESENTATION_MAX_UNPACKED_BYTES` | 134217728 (128 MiB) |
| `PRESENTATION_MAX_TEMP_BYTES` | 268435456 (256 MiB) |
| `PRESENTATION_SILENCE_SECONDS` | 0.25 seconds |
| `MEDIA_WORKER_CONCURRENCY` | 1 |

Charge measured **source narration**, not inserted silence. No narration is free,
but delivery is still recorded. Keep scratch storage private; limits do not reserve
hosting resources.
