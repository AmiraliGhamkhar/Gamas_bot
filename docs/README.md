# Gamas Bot documentation

**v3 note.** The codebase was overhauled (see
[`OVERHAUL_PLAN.md`](OVERHAUL_PLAN.md)): the v2 module stack (`ai/`,
`stt_platform/`, `structuring`, `editorial`, `qa`, credential encryption, the
admin provider panels) is gone, replaced by `stt.py`, `llm.py`,
`transcript.py`, `notes.py`, `db.py`, `jobs.py` and a much smaller `bot.py`.
Migrations live under `migrations/v3/`; the v2 SQL is archived under
`migrations/v2/` and `scripts/import_v2.py` moves live data into a fresh v3
database.

| Page | Status | What it covers |
| --- | --- | --- |
| [`../README.md`](../README.md) | **v3** | what the bot does, install, run, quick start, v2 import |
| [`OVERHAUL_PLAN.md`](OVERHAUL_PLAN.md) | **v3** | the design contract: module map, invariants, scope decisions |
| [`DEPLOY_CPANEL.md`](DEPLOY_CPANEL.md) | **v3** | cPanel/Passenger deployment |
| [`DEPLOY_FA.md`](DEPLOY_FA.md) | **v3** | راهنمای استقرار به فارسی |
| [`OPERATIONS.md`](OPERATIONS.md) | mostly v3 | start/stop, health checks, logs, backups |
| [`BILLING.md`](BILLING.md) | v2-era | plans and entitlements (v3: append-only ledger + holds) |
| [`DOCX.md`](DOCX.md) | v2-era | RTL contracts (still accurate; the soffice/pypdf TOC pass is gone) |
| [`MEDIA.md`](MEDIA.md) | current | the Python-only media pipeline and its limits |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | v2-era | module map (see OVERHAUL_PLAN.md for v3) |
| [`CONFIGURATION.md`](CONFIGURATION.md) | v2-era | configuration model (v3: `.env.example` + `config.py`) |
| [`PROVIDERS.md`](PROVIDERS.md) · [`STT_PROVIDERS.md`](STT_PROVIDERS.md) · [`AI_PROVIDERS.md`](AI_PROVIDERS.md) | v2-era | provider platforms that no longer exist |
| [`SECURITY.md`](SECURITY.md) | v2-era | threat model (credential encryption dropped: env-only keys) |
| [`TESTING.md`](TESTING.md) | v2-era | gates and benchmarks (benchmarks removed in v3) |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | v2-era | symptom → cause → where to look |
| [`CHANGELOG.md`](CHANGELOG.md) | log | notable changes |
| [`AUDIT_HISTORY.md`](AUDIT_HISTORY.md) · [`AI_PLATFORM_AUDIT.md`](AI_PLATFORM_AUDIT.md) · [`ENGINEERING_AUDIT.md`](ENGINEERING_AUDIT.md) · [`REPORT_PRODUCTION_IMPROVEMENTS.md`](REPORT_PRODUCTION_IMPROVEMENTS.md) | historical | superseded reports, kept for traceability |
