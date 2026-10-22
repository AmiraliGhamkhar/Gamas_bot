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
