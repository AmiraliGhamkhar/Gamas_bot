# Testing

Tests use fake Telegram/provider responses: no API keys, no production data and no billable calls.
CI runs them on Python 3.11, 3.12 and 3.13.

## Windows PowerShell (local development)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m scripts.sync_requirements --check
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m gamas_bot.media_worker check
```

If `ruff.exe` is not on the path, activate the virtualenv and run `ruff check .`.

## cPanel / Linux

After installing `requirements.txt` with the interpreter cron uses:

```bash
python -m gamas_bot --check
python -m scripts.cpanel_preflight
python -m gamas_bot.media_worker check
```

The first is offline; the preflight also tests disk, permissions, the lock and outbound network.
Run the test suite only on a development machine, never with production `.env` or data.

## What the suite covers

| File | Covers |
| --- | --- |
| `tests/test_doc_extract.py` | PDF/Word/PowerPoint text extraction with page and slide locators, the scanned-PDF guard, encrypted PDFs, chunking (including a split page's `(۱/۲)` marker), SHA-256 dedupe, corrupt cache entries and the ~300-character preview |
| `tests/test_document_ingestion.py` | Upload → extract → store → preview → generate for a real PDF and a real DOCX, oversized uploads, identical re-uploads, the photo-instead-of-file hint, the library «📄 اسناد» section, document cards, chunk failures and coverage reporting |
| `tests/test_free_uses.py` | Atomic first-try claim, per-kind independence, refunds, the admin reset and its audit row, badges, the second deck/quiz hitting the paywall without a model call, refund on provider errors, curation delete-then-save, size presets and their prices |
| `tests/test_admin_hubs.py` | The three hubs, their defaults, the deep views kept from the old panel, the legacy `adm:pay`/`adm:ovpay`/`adm:ovu`/`adm:ovp`/`adm:ovs` aliases, forged sub-pages, and the approve path |
| `tests/test_migration_schema.py` | The CI migration gate run locally: every `migrations/v3/*.sql` file is applied, `schema_migrations` matches the directory, and the resulting tables equal `db.APPLICATION_TABLES` (so a new migration cannot silently drift from CI) |
| `tests/test_user_identity.py` | Identity columns, `@username · شناسه · name` rendering, an int64 id (`770000000123456789`), no duplicates on double `/start`, username changes, foreign-key integrity of user references, and `@username` admin search |

Fixtures build real documents in memory (reportlab, `python-docx`, `python-pptx`), so extraction is
exercised against the same libraries the bot uses at runtime — no binary fixtures are committed.
