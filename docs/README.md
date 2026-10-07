# Gamas Bot documentation

| Page | What it covers |
| --- | --- |
| [`../README.md`](../README.md) | what the bot does, install, run, quick start |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | pipeline, module map, concurrency, data model, invariants |
| [`CONFIGURATION.md`](CONFIGURATION.md) | the single configuration model, precedence, aliases, secret handling |
| [`DOCX.md`](DOCX.md) | the Word booklet: RTL rules, styles, TOC policy, reproducibility |
| [`MEDIA.md`](MEDIA.md) | the Python-only media pipeline and its limits |
| [`PROVIDERS.md`](PROVIDERS.md) | STT and note-generation providers, health checks |
| [`BILLING.md`](BILLING.md) | plans, the usage ledger, payments, special users |
| [`SECURITY.md`](SECURITY.md) | threat model and the controls that are enforced |
| [`OPERATIONS.md`](OPERATIONS.md) | start/stop, health checks, logs, backups, capacity |
| [`TESTING.md`](TESTING.md) | gates, what the suite covers, benchmarks |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | symptom → cause → where to look |
| [`DEPLOY_CPANEL.md`](DEPLOY_CPANEL.md) | cPanel/Passenger deployment |
| [`DEPLOY_FA.md`](DEPLOY_FA.md) | راهنمای استقرار به فارسی |
| [`CHANGELOG.md`](CHANGELOG.md) | notable changes |
| [`AUDIT_HISTORY.md`](AUDIT_HISTORY.md) | superseded historical reports (kept for traceability) |

`.env.example` is the canonical, complete reference for every environment
variable; `CONFIGURATION.md` explains the model around it and a test fails when
the two drift apart.

Every page is expected to describe the code as it is. When a page and the code
disagree, the code and its tests win, and the page is a bug.
