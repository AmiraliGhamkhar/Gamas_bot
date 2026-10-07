# Testing

## Gates

```bash
python -m compileall -q gamas_bot scripts tests passenger_wsgi.py
ruff check .                      # config in pyproject.toml
python -m pytest -q               # 726 tests, ~55 s, no network
python -m pip check
python -m scripts.sync_requirements --check
```

`.github/workflows/tests.yml` runs the same suite on Python 3.11, 3.12 and 3.13,
through both `pytest` and `unittest discover` (the entry point deployments use).

## What the suite is organised around

The suite asserts behaviour, not implementation shape. The areas pinned by
dedicated files:

| Area | File(s) |
| --- | --- |
| Telegram rendering and splitting | `test_core.py`, `test_audit.py` |
| Job queue, back-pressure, shutdown | `test_job_queue.py` |
| Media intake and cancellation | `test_media_intake.py`, `test_media_runtime.py` |
| Media worker without system binaries | `test_media_runtime.py` (asserts no `ffmpeg`/`ffprobe`/`soffice` is ever executed) |
| Presentations and legacy `.ppt` | `test_presentations.py`, `test_bot_presentation_flow.py` |
| STT providers, retries, fallback | `test_provider_contracts.py`, `test_provider_robustness.py` |
| Provider credentials and health | `test_provider_credentials.py`, `test_provider_health.py` |
| Billing, payments, special users | `test_billing.py`, `test_payment_flow.py`, `test_special_users_and_plan_panel.py` |
| Note quality, coverage, QA | `test_note_quality.py`, `test_semantic_coverage.py`, `test_note_evaluation.py` |
| DOCX layout, RTL, TOC, pagination | `test_docx_export.py`, `test_docx_polish.py`, `test_docx_layout_requirements.py`, `test_mixed_script_typography.py` |
| Offline page renderer | `test_page_renderer.py` |
| Configuration and dependency drift | `test_config_consistency.py`, `test_dependency_consistency.py` |
| Deployment (cPanel, launcher, lock) | `test_cpanel_deploy.py` |

## Benchmarks

```bash
python -m scripts.benchmark_notes            # deterministic, offline, ~2 s
python -m scripts.benchmark_notes --live     # calls the configured note provider
python -m scripts.benchmark_stt audio.mp3    # per-provider STT latency
python -m scripts.validate_docx --render     # DOCX structure + offline pages
```

`benchmark_notes` reports compression ratio, number/term signal coverage and
**semantic coverage** per fixture. Semantic coverage is the measure that catches
a deleted explanation; a number/term check alone cannot. A low compression
ratio is never a failure by itself — it is read together with coverage.

The offline DOCX benchmark deliberately runs with the static TOC disabled
because exact page mapping needs the production renderer; static-TOC behaviour
is covered by its own tests with a stubbed page map.

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
