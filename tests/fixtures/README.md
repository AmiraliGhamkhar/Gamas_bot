# Test fixtures

> **Status: currently unused.** No test reads this directory. `FIXTURES_DIR` is
> defined in `tests/support.py` but nothing references it, and the modules that
> used to consume these files (`tests/test_note_evaluation.py`, `run_note_qa`)
> were removed in the v3 overhaul. They are kept because the Persian reference
> corpus below is hand-written and expensive to reproduce — see
> `notes/README.md` for what restoring a benchmark would need.

## Legacy PowerPoint decks

`visual_minimal.ppt` and `visual_video.ppt` are copied from the test corpus of
[ppt2pptx](https://github.com/HuiTurn/ppt2pptx) (MIT License, © 2026 ppt2pptx
contributors). They are real PowerPoint 97–2003 binary files, for exercising the
legacy `.ppt → .pptx` conversion path:

* `visual_minimal.ppt` — the smallest real deck with normal slide content.
* `visual_video.ppt` — contains embedded media, so the conversion report's
  lossy-feature `warning_codes` (collected in `media_worker._convert` from
  `ppt2pptx`'s report) are covered.

The conversion code they would exercise is live: `media.convert_to_pptx` and
`presentations.py`. Only the test that drives it is missing.

## Note-quality corpus

`notes/` holds six Persian lecture transcripts paired with hand-written reference
documents and the content-preservation floors declared in `notes/corpus.json`.
See [`notes/README.md`](notes/README.md).
