# Testing

Run tests from the repository root. Python 3.11–3.13 are exercised in CI. Replace `3.12` below with your installed version if needed.

## Windows PowerShell / local development

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

If the `ruff.exe` command is unavailable, run `ruff check .` after activating the virtual environment. Tests use fake Telegram/provider responses; they need no production keys and make no billable API calls.

## cPanel check

After installing runtime dependencies on the target account, run these from the project folder with the same Python executable used by cron:

```bash
python -m gamas_bot --check
python -m scripts.cpanel_preflight
```

The first checks local settings and packages. The preflight also tests disk, permissions, the lock and outbound network; it is the host-compatibility check. CI additionally checks dependency consistency and compiles the package. Do not run the test suite with production credentials or data.
