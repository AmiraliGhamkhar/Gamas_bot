# Test fixtures

This folder contains synthetic/curated test inputs only:

- `visual_minimal.ppt` and `visual_video.ppt` exercise legacy PowerPoint conversion and embedded-media handling.
- `notes/` contains six transcript/reference pairs used by the deterministic note-evidence tests. See [notes/README.md](notes/README.md).
- `stt/` is reserved for local STT samples; no audio is committed. See [stt/README.md](stt/README.md).

Do not commit private, medical or otherwise identifiable recordings. CI uses fake provider responses and does not need audio or API keys.
