# Test fixtures

`visual_minimal.ppt` and `visual_video.ppt` are copied from the test corpus of
[ppt2pptx](https://github.com/HuiTurn/ppt2pptx) (MIT License, © 2026
ppt2pptx contributors) and used to exercise the legacy `.ppt → .pptx`
conversion path with real PowerPoint 97–2003 binary files:

- `visual_minimal.ppt` — smallest real deck with normal slide content.
- `visual_video.ppt` — contains embedded media, so the conversion report's
  lossy-feature diagnostics (`MEDIA_ACTION_OMITTED`) are covered.

`notes/` holds the note-quality benchmark corpus: six Persian lecture
transcripts paired with hand-written reference documents and the content-
preservation floors declared in `notes/corpus.json`. See
`notes/README.md`; it is driven by `tests/test_note_evaluation.py`.
