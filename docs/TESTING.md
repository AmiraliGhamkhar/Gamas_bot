# Testing

Use a development checkout with **no production `.env`, database or session**.
Tests use fake providers and synthetic fixtures: no live keys or billable calls.
CI targets Python 3.11–3.13.

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
capacity. Review those manually before production.
