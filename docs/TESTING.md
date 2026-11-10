# Testing

Use a development checkout with **no production `.env`, database or session**.
Tests use fake providers and synthetic fixtures: no live keys or billable calls.
CI targets Python 3.11–3.13. Provider probes are operator tools, not tests:
`--show-config` is offline, the default probe makes metadata-only network calls,
and only `python -m scripts.probe_note_providers --generate TRANSCRIPT_FILE`
sends real generation requests (quota). Never add a generation probe to CI or
the standard test suite. The provider-compatibility regressions in
`tests/test_note_provider_compatibility.py` are fully deterministic.

## Windows PowerShell

Run commands separately; stop on any error. PowerShell 5.1 does not fail-fast by
default. No activation is needed.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ocr]"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m scripts.sync_requirements --check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m compileall -q gamas_bot scripts tests passenger_wsgi.py
.\.venv\Scripts\python.exe -m gamas_bot.media_worker check
```

## Linux / cPanel Terminal

Use an isolated development copy, never production data. The OCR extra allows
transport tests against a local mock; installing it does not enable OCR.

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,ocr]'
set -e
python -m pip check
python -m scripts.sync_requirements --check
python -m ruff check .
python -m mypy
python -m pytest -q
python -m unittest discover -s tests -q
python -m compileall -q gamas_bot scripts tests passenger_wsgi.py
python -m gamas_bot.media_worker check
```

Both test runners are required. Cover failure paths, refunds, gifts, ownership,
migrations and races. Change dependencies in `pyproject.toml`, then generate
`requirements.txt` with `python -m scripts.sync_requirements`; never hand-edit it.

## Deployment smoke test

Use a dedicated test bot/account and non-sensitive files. Live calls can cost
money and are separate from offline tests.

1. Run `--check`; on cPanel also run `scripts.cpanel_preflight`. Fix failures.
2. Test membership with a non-admin, then `/start` as admin.
3. Send short audio: transcript first, booklet if configured; verify Library.
4. Upload text PDF/DOCX/PPTX; verify original download and source locators.
   Narration must wait for its explicit button.
5. Build all four Office formats; include quoted CSV and literal `=1+2`.
   Check unchanged wallet/gifts and fallback without note keys.
6. Preview/cancel/save learning items; check gift restoration and paid failure refunds.
7. Use a synthetic receipt on a test order: credit only after approval, no duplicate
   activation. Create/reply/close a support ticket.
8. Check orderly shutdown, restart recovery and logs. Test [OCR](OCR.md) only if enabled.

Automated tests do not prove provider availability, Persian visual layout or host
capacity. Review those manually before production. For the generated booklet
documents, `tests/test_pdf_visual_qa.py` narrows the visual gap automatically:
see “Gamas 3.0 verification” below.


---

## Gamas 3.0 verification (Studio structured editing)

```bash
python -m pytest -q tests/test_studio_structured.py   # store + WSGI + bot-side DB
python -m pytest -q tests/test_mini_app.py            # HTTP layer incl. new payloads
python -m pytest -q tests/test_pdf_visual_qa.py       # rendered PDF/DOCX visual QA
python -m pytest -q                                   # full suite
python -m ruff check . && python -m mypy
```

Counts from the full-suite run in this working session
(2026-11-10, Python 3.11): **1753 passed, 279 subtests, 152.62 s** — including
`tests/test_studio_structured.py` (35 tests: store round-trips over a fully
migrated database, malformed/oversized payloads, exports, speakers with roles,
bot-side persistence), the extended `tests/test_mini_app.py` (57 tests: payload
builders, validation, 404/400/413/503 paths, cross-user isolation) and
`tests/test_pdf_visual_qa.py` (9 tests / 35 subtests).

### Visual QA of generated documents

`tests/test_pdf_visual_qa.py` renders the curated note-quality fixtures
(`tests/fixtures/notes/references/`) plus a synthetic stress document and
checks what a student actually sees:

* **structural (pypdf, always on)** — page count and A4 geometry, non-empty
  content streams, embedded subset Vazirmatn TrueType fonts, expected headings
  in extracted text (NFKC-normalized: the renderer pre-shapes Persian into
  presentation forms), no replacement glyphs, no missing-glyph NULs, vector
  step-connector arrows present;
* **rasterized (PyMuPDF, dev-only)** — every page rasterized with a minimum ink
  ratio (no blank pages), text inside the side margins and the decorative
  border frame with the running header/footer accounted for, and no two text
  spans colliding (ink-tight boxes, so Vazirmatn's tall line metrics do not
  false-positive on normal leading);
* **DOCX** — valid OOXML package, `<w:bidi>`/`<w:rtl>` flags, Vazirmatn theme
  font, headings present in concatenated run text, glossary table, and the
  edited title winning over a stale one.

The rasterized checks skip cleanly when `pymupdf` is not installed; it is a
dev-only tool and not added to the runtime requirements.


---

## Gamas 2.0 verification

```bash
python -m pytest -q tests/test_gamas2.py      # credits, turns, media, Studio, eval
python -m pytest -q                           # full suite
python -m ruff check . && python -m mypy
python -m scripts.credit_price_preview --candidate 65
python -m scripts.evaluate_diarization --reference ref.json --hypothesis hyp.json
```

`docs/GAMAS2.md` §5 lists the acceptance checks and the migration gate.
