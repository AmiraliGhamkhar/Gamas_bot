# Testing (v3)

## CI gates

GitHub Actions runs the v3 checks on Python 3.11, 3.12 and 3.13. From the
repository root, the equivalent local checks are:

```bash
python -m pip check
python -m scripts.sync_requirements --check
ruff check .
python -m mypy
python -m compileall -q gamas_bot scripts tests passenger_wsgi.py
python -m pytest -q
python -m unittest discover -s tests -q
python -m gamas_bot.media_worker check
```

The fresh-database CI smoke test opens a temporary SQLite database through
`gamas_bot.db.Database`, confirms that every SQL file under `migrations/v3/`
was applied, and asserts the expected eight application tables. The media
worker smoke test checks the PyAV and `ppt2pptx` runtime dependencies without
calling a media or AI provider.

`requirements.txt` is generated from `[project].dependencies` in
`pyproject.toml`; `scripts.sync_requirements --check` fails if the deployment
file has drifted. CI also runs `pip check` and a non-blocking `pip-audit` scan.

## Test coverage

The suite asserts v3 behavior rather than the deleted v2 provider-platform
internals:

| Area | Test module |
| --- | --- |
| Bot intake, raw-first delivery, billing settlement | `test_bot.py` |
| Settings and configuration validation | `test_config.py` |
| SQLite migrations, ledger, payments and plans | `test_db_billing.py` |
| RTL Word output and document structure | `test_docx_export.py` |
| One-shot v2 database import | `test_import_v2.py` |
| Queue bounds, back-pressure and shutdown | `test_jobs.py` |
| LLM request and response handling | `test_llm.py` |
| Note parsing, chunking, map/reduce and fallback | `test_notes.py` |
| STT adapters, retries and ordered failover | `test_stt.py` |
| Transcript paragraphing, fidelity and chunk boundaries | `test_transcript.py` |

The tests use synthetic media and mocked HTTP/Telegram boundaries; they do not
need API credentials and do not make billable provider calls. The legacy `.ppt`
fixtures are documented in `tests/fixtures/README.md`.

The pre-overhaul v2 suite count is historical, not the v3 test count. v3 keeps
focused behavior tests for its smaller architecture; current results should be
reported from the run being evaluated rather than copied forward as a fixed
count.

## Optional manual checks

For a deployment configuration self-check (requires valid local configuration,
but does not connect to Telegram or AI providers):

```bash
python -m gamas_bot --check
```

For an end-to-end test against live services, configure a test bot and provider
credentials separately. These calls may incur provider charges; they are not
part of CI and should only be run with explicit approval.
