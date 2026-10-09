# Testing

## Gates

```bash
python -m compileall -q gamas_bot scripts tests passenger_wsgi.py
ruff check .                      # config in pyproject.toml
python -m pytest -q               # latest local run: 923 passed, 1 skipped, 1,786 subtests, ~90 s
python -m pip check
python -m scripts.sync_requirements --check
```

`.github/workflows/tests.yml` runs the suite on Python 3.11, 3.12 and 3.13,
through both `pytest` and `unittest discover` (the entry point deployments use).
The latest local `unittest discover -s tests` run executed 924 tests with one
skip and passed. Counts are snapshots; update them when the suite changes.

## What the suite is organised around

The suite asserts behaviour, not implementation shape. The areas pinned by
dedicated files:

| Area | File(s) |
| --- | --- |
| Telegram rendering and splitting | `test_core.py`, `test_audit.py` |
| Job queue, back-pressure, shutdown | `test_job_queue.py` |
| Media intake and cancellation | `test_media_intake.py`, `test_media_runtime.py` |
| Presentations and legacy `.ppt` | `test_presentations.py`, `test_bot_presentation_flow.py` |
| STT providers, retries, fallback | `test_provider_contracts.py`, `test_provider_robustness.py` |
| AI provider registry, routing, quota guards, legacy compatibility, secret hygiene | `test_ai_platform.py`, `test_ai_schema_compat.py` |
| Account entitlement, extra-pass overrides, Neuron metering, NVIDIA endpoint lifecycle, log filters, admin panels | `test_provider_platform_gaps.py` |
| Per-provider request/response contracts | `test_provider_adapters.py` |
| Provider credentials and health | `test_provider_credentials.py`, `test_provider_health.py` |
| Telegram AI administration and billing attestation | `test_admin_ai.py` |
| Billing, payments, special users | `test_billing.py`, `test_payment_flow.py`, `test_special_users_and_plan_panel.py` |
| Note quality, coverage, QA | `test_note_quality.py`, `test_semantic_coverage.py`, `test_note_evaluation.py` |
| DOCX layout, RTL, TOC, pagination | `test_docx_export.py`, `test_docx_polish.py`, `test_docx_layout_requirements.py`, `test_mixed_script_typography.py` |
| Offline page renderer | `test_page_renderer.py` |
| DOCX hot-path equivalence (byte-identical fast paths) | `test_docx_run_styling.py` |
| Configuration and dependency drift | `test_config_consistency.py`, `test_dependency_consistency.py` |
| Deployment (cPanel, launcher, lock) | `test_cpanel_deploy.py` |

Provider tests use mock sessions and synthetic credentials; the suite never
calls a real AI provider and CI contains no API keys. The adapter contract tests
cover provider request shapes, auth headers, model-specific structured-output
strategies, normalized errors, and quota-header handling. Platform tests cover
live-catalog failure safety, per-key billing attestations, free-model filtering,
legacy `NOTE_API_*` caps, and in-flight local quota reservations.

## Benchmarks and manual visual checks

```bash
python -m scripts.benchmark_notes --mode standard --json --router
python -m scripts.benchmark_notes --mode full            # deterministic, offline
python -m scripts.benchmark_notes --live                # calls configured AI provider
python -m scripts.benchmark_stt audio.mp3                # per-provider STT latency
python -m scripts.benchmark_queue                       # bounded-queue load, 1..50 jobs
python -m scripts.validate_docx --render                 # structure + offline page render
python -m scripts.validate_provider_platform   # offline AI platform validation, no API key
```

`benchmark_notes` reports compression ratio, number/term signal coverage and
**semantic coverage** per fixture. Semantic coverage is the measure that catches
a deleted explanation; a number/term check alone cannot. A low compression
ratio is never a failure by itself—it is read together with coverage. The
`--router` profile is an offline dry-run of the free-first route, skip reasons,
and per-provider chunk/output budgets. Only `--live` makes provider calls; do
not run it without explicit billable-test approval and an attested key.

`validate_provider_platform` is the AI-platform counterpart of `validate_docx`:
it drives the real `structure_transcript` pipeline against scripted provider
responses and asserts the promises that matter before free-tier traffic is
trusted — the legacy NaraRouter configuration still produces notes with the
unchanged 22,000-character budget, a restrictive free provider gets a smaller
token-derived chunk budget, a hard 429 fails over to the next provider and is
accounted, `AI_FREE_ONLY` refuses a provider with an unverified account
entitlement or an unverified model, paid legs stay last, backend diagnostics
are scrubbed from student notes, and no secret or transcript reaches the log.
Exit code is non-zero on any failed check.

The offline DOCX benchmark deliberately runs with the static TOC disabled
because exact page mapping needs the production renderer; static-TOC behaviour
is covered by its own tests with a stubbed page map. The DOCX validator creates
sample files under a temporary directory by default. Its rendered pages can be
opened and inspected manually; automated RTL assertions remain in the DOCX
layout tests.

`benchmark_queue` drives the real `StudyBot` queue (bounded queue, worker pool,
job semaphore, SQLite status writes) with synthetic jobs and reports acceptance
vs back-pressure rejection, queue-wait and latency percentiles, throughput and
RSS per scenario. At the default capacity (3 workers + 8 pending) a burst of
25 or 50 uploads accepts 11 and rejects the rest with an explicit message—that
is the capacity model working, not a failure.

## Conventions

* Tests that need an office suite skip explicitly instead of failing.
* No test writes to the repository's `data/` directory; temporary directories
  are used and cleaned up.
* Deterministic output is asserted where it matters: document generation is
  byte-reproducible for identical inputs, which makes artifacts comparable by
  hash.

## Reproducible installs

`pyproject.toml` is the canonical dependency list and `requirements.txt` is
generated from it, so CI and a cPanel deployment install the same *set* of
packages.

The repository deliberately ships **bounded ranges, not a lock file**. This is
an application deployed in place (Passenger/systemd) rather than a library, and
its native wheels (`av`, `cryptography`, `lxml`, `Pillow`) are platform
specific: a lock resolved on a developer's machine would pin wheels that do not
exist on the deployment host. For a byte-reproducible environment anyway,
resolve on the target platform:

```bash
pip install uv
uv pip compile pyproject.toml -o requirements.lock   # on the target platform
uv pip sync requirements.lock
```

If a lock file is adopted, `scripts/sync_requirements.py` and
`tests/test_dependency_consistency.py` should be updated together so there is
still exactly one deployment list.
