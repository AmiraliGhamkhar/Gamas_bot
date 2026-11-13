# Testing

Run from the repository root in a Python 3.11–3.13 virtualenv. The authoritative
matrix and fresh-v3 migration assertion are in
[the CI workflow](../.github/workflows/tests.yml). A local pass on one Python
version does not establish that the other matrix jobs passed.

## Offline gates

Install runtime requirements and the declared OCR/test tools first:

```bash
python -m pip install -r requirements.txt
python -m pip install '.[ocr]' 'ruff>=0.6' 'pytest>=8' 'mypy>=1.11' 'pypdf>=4.0,<7.0'
python -m pip check
python -m scripts.sync_requirements --check
ruff check .
python -m mypy
python -m compileall -q gamas_bot scripts tests passenger_wsgi.py
python -m pytest -q
python -m unittest discover -s tests -q
python -m gamas_bot.media_worker check
```

Also run the workflow's **Migration integrity (fresh v3 database)** Python
block: create a temporary `Database`, connect, compare applied SQL filenames
with `MIGRATIONS_DIR`, compare the exact application table set with
`APPLICATION_TABLES`, and close it. Never point this check at production data.

PowerShell equivalents use `.\.venv\Scripts\python.exe` and
`.\.venv\Scripts\ruff.exe` instead of the activated environment commands.
`unittest discover` covers the unittest-compatible tests; pytest additionally
collects function-based regressions. Run both, and report skipped tests and
unavailable checks explicitly. The workflow's `pip-audit` is a separate
network-dependent advisory; never label it PASS without running it.

## Focused regressions

```bash
python -m pytest tests/test_provider_allowlist.py tests/test_llm_task_routing.py tests/test_provider_contracts_and_quality.py tests/test_note_provider_compatibility.py -q
python -m pytest tests/test_learning.py tests/test_document_ingestion.py -q
python -m pytest tests/test_llm_quota.py tests/test_concurrency_and_failover.py -q
```

Provider HTTP is mocked. The default test settings have **no LLM credential**;
generation fixtures opt into scoped fake approved keys only when their I/O is
scripted. Do not use live keys, paid quota or real patient data. Test behavior:
actual routed endpoints, task/preference policy, credential isolation,
sensitivity denials, strict/permissive parsing, bounded retries, cancellation
and slot release. Do not replace those assertions with source-string searches,
skips or placeholder settings checks.

Quota tests use temporary SQLite/WAL databases, covering request/token limits,
atomic reservations, reconciliation and circuit recovery. Provider/stage
concurrency regressions exercise real local capacity pools and temporary data;
they do not establish production throughput. `python -m scripts.concurrency_bench`
is an optional **synthetic** pipeline benchmark, not a recommended deployment
profile or a live-provider test.

## Live verification is separate

`python -m scripts.probe_note_providers --show-config` is offline. Metadata
probes make network requests but do not verify generation. `--generate` is an
explicit operator-only action over a short **synthetic, non-sensitive** file;
it consumes quota and must not run in routine CI. Verify schema/domain validity,
finish reason, dashboard usage and latency without recording secrets or source
text. Model availability, account limits, Telegram deployment behavior and
production performance remain unverified until those separate checks run.
