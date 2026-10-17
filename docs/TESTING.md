# Testing

## CI gates

GitHub Actions runs on Python 3.11, 3.12 and 3.13 (`.github/workflows/tests.yml`).
The equivalent local run:

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

CI also applies every SQL file under `migrations/v3/` to a fresh temporary
database and asserts the resulting eight application tables, and runs a
non-blocking `pip-audit` scan.

`requirements.txt` is generated from `[project].dependencies` in
`pyproject.toml`; `scripts.sync_requirements --check` fails if the deployment file
has drifted.

## Coverage

| Area | Module |
| --- | --- |
| Bot intake, raw-first delivery, billing settlement | `tests/test_bot.py` |
| Settings parsing, precedence, `check()` | `tests/test_config.py` |
| `.env.example` ↔ `config.py` consistency | `tests/test_config_consistency.py` |
| Migrations, ledger, holds, payments, plans, special users | `tests/test_db_billing.py` |
| RTL Word output and document structure | `tests/test_docx_export.py` |
| One-shot v2 database import | `tests/test_import_v2.py` |
| Queue bounds, back-pressure, shutdown | `tests/test_jobs.py` |
| LLM request/response shapes, error classes, failover | `tests/test_llm.py` |
| Note parsing, slide-aware chunking, map/reduce, split recovery | `tests/test_notes.py` |
| PyAV merge metadata, PPTX media relationships, timing and XML fallback | `tests/test_media.py` |
| Free Office: parser, Word/Excel/PowerPoint output, bot flow | `tests/test_office.py` |
| STT adapters, retries, cooldowns, ordered failover | `tests/test_stt.py` |
| Transcript paragraphing, fidelity, chunk boundaries | `tests/test_transcript.py` |

The suite uses synthetic media and scripted HTTP/Telegram doubles. **No test
needs an API key and no test makes a billable call.** Presentation tests verify
mechanics (clip ordering/spans, source-vs-merged accounting, alignment,
extraction fallback and resource caps); they do not establish STT accuracy,
latency or denoising/VAD quality. Those claims require a separate
consented/reference-audio benchmark.

Two doubles are deliberately strict, because permissive ones hid real bugs:

* `tests/support.py` binds every fake Telegram call against the *installed*
  Telethon signature, so an unsupported keyword fails in the test the same way it
  fails in production.
* The provider doubles return real documented payloads (NaraRouter's error
  envelope, Gemini's `promptFeedback`, Deepgram's word timings) rather than
  whatever shape the code happens to expect.

## Writing a test here

* Prefer asserting the **documented provider contract** over the current code
  path — a test that only mirrors the implementation cannot catch drift.
* A regression test should fail against the commit before the fix. Verify that
  with `git stash` before claiming it.
* Report the count from the run you are looking at, never a copied-forward
  number.

## Manual checks

```bash
python -m gamas_bot --check    # offline; needs valid local configuration
```

An end-to-end run against live services needs a test bot and provider
credentials, may incur charges, and is not part of CI.
