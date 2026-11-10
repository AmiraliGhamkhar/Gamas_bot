# Testing

Use a development checkout with **no** production `.env`, database or session.
The suite uses fake providers and synthetic fixtures: no live keys, no billable
calls. CI runs Python 3.11, 3.12 and 3.13 on Linux.

## Install the development extras

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ocr]"
```

```bash
python3.11 -m venv .venv && . .venv/bin/activate
python -m pip install -e '.[dev,ocr]'
```

`ocr` only adds the transport used by the OCR tests; installing it does not
enable OCR.

## The gate (both runners are required)

**Windows PowerShell** — run each line separately, stop on error:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m scripts.sync_requirements --check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m compileall -q gamas_bot scripts tests passenger_wsgi.py
.\.venv\Scripts\python.exe -m gamas_bot.media_worker check
```

**cPanel / Linux** — `set -e` first:

```bash
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

What the extra checks cover:

- `sync_requirements --check` fails when `requirements.txt` no longer matches
  `pyproject.toml`. Regenerate, never hand-edit:
  `python -m scripts.sync_requirements`.
- `pytest` and `unittest` run the same suite through two runners, because some
  deployments only have one; both must pass.
- `compileall` byte-compiles every module, including `passenger_wsgi.py`.
- `media_worker check` imports PyAV and the legacy-`.ppt` converter — the two
  binary-heavy runtime dependencies.
- CI also builds a fresh v3 database and asserts that every migration was applied
  and that `APPLICATION_TABLES` matches the schema exactly. Reproduce it locally
  by deleting a throwaway `DB_PATH` and starting the bot once.

## Focused runs

```bash
python -m pytest -q tests/test_mini_app.py tests/test_studio_structured.py
python -m pytest -q tests/test_pdf_visual_qa.py      # rendered PDF/DOCX QA
python -m pytest -q tests/test_config_consistency.py # docs/env consistency
python -m pytest -q tests/test_note_provider_compatibility.py
```

`tests/test_pdf_visual_qa.py` checks real output: A4 geometry, embedded Vazirmatn
subset, headings, no replacement glyphs, ink coverage and text collisions.
Rasterized checks skip cleanly when the dev-only `pymupdf` is not installed.

`tests/test_config_consistency.py` also verifies that every local Markdown link
resolves and that the docs never advertise removed settings — keep it green when
renaming a guide.

## Provider probes are not tests

- `python -m scripts.probe_note_providers --show-config` — offline, credential-free.
- `python -m scripts.probe_note_providers` — metadata only.
- `python -m scripts.probe_note_providers --generate FILE` — real request,
  **consumes quota**. Never add it to CI or cron.

## Deployment smoke test

Use a test bot and non-sensitive files. This proves more than the suite can:

1. `--check` (and `scripts.cpanel_preflight` on cPanel) — no `FAIL`.
2. Membership with a non-admin account, then `/start` as an admin.
3. Short voice note: transcript first, then the booklet; re-download from the
   Library.
4. Text PDF/DOCX/PPTX upload: extraction, source locators, original download,
   narration only after its button.
5. All four Office formats, including quoted CSV and a literal `=1+2`; wallet and
   gifts unchanged.
6. Quiz/flashcard preview → save, cancel and failure paths; gift restore and
   refund.
7. Synthetic receipt on a test order: credit only after approval, no duplicate.
8. Mini App: leaderboard, open a booklet, save a structured edit, export PDF and
   DOCX, restore a revision, rename a speaker.
9. Clean shutdown, restart recovery, and a log without secrets.

Automated tests cannot prove provider availability, Persian visual layout on the
student's device, or host capacity. Check those manually before a release.
