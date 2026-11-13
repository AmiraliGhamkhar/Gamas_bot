# Testing

Install project test dependencies, then run `python -m pytest -q`. Tests should mock provider HTTP responses; do not consume live quota in routine CI. The repository's existing suite includes legacy-provider expectations and currently requires updates for the approved-provider policy. The focused task-order test is `python -m pytest tests/test_llm_task_routing.py -q`. Passing that focused test does not establish quota enforcement, provider compatibility, or end-to-end pipeline correctness.

For local checks use `python -m compileall -q gamas_bot`. A live smoke test is an explicitly operator-run deployment check: configure one approved provider's key, send a short non-sensitive request, validate both the returned JSON schema and finish reason, and confirm usage/latency in provider dashboards. Never use PHI/PII for smoke tests.
