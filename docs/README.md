# Documentation

Every page describes the code as it exists now. When a page and the code
disagree, the code wins — and that is a bug worth reporting.

| Page | What it covers |
| --- | --- |
| [`../README.md`](../README.md) | What the bot does, install, run, limits |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Module map, pipeline, concurrency, data model, invariants |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Every environment variable, precedence, what `--check` validates |
| [`PROVIDERS.md`](PROVIDERS.md) | Note-generation providers and failover |
| [`STT_PROVIDERS.md`](STT_PROVIDERS.md) | Speech-to-text providers, languages, limits |
| [`MEDIA.md`](MEDIA.md) | The Python-only media pipeline |
| [`DOCX.md`](DOCX.md) | RTL Word generation contracts |
| [`BILLING.md`](BILLING.md) | Ledger, plans, card-to-card payments |
| [`SECURITY.md`](SECURITY.md) | Threat model and what is enforced |
| [`OPERATIONS.md`](OPERATIONS.md) | Start/stop, health, logs, backups |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Symptom → cause → where to look |
| [`TESTING.md`](TESTING.md) | CI gates and coverage |
| [`DEPLOY_CPANEL.md`](DEPLOY_CPANEL.md) | cPanel / Passenger deployment |
| [`DEPLOY_FA.md`](DEPLOY_FA.md) | راهنمای استقرار به فارسی |
| [`CHANGELOG.md`](CHANGELOG.md) | Notable changes |

## Start here

* **Running it locally** — [`../README.md`](../README.md)
* **Deploying** — [`DEPLOY_CPANEL.md`](DEPLOY_CPANEL.md) or
  [`DEPLOY_FA.md`](DEPLOY_FA.md)
* **Something is broken** — [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)
* **Changing configuration** — [`CONFIGURATION.md`](CONFIGURATION.md), then
  `.env.example`

## Two rules for editing these pages

1. **Never document a setting that `config.py` does not read.**
   `tests/test_config_consistency.py` fails the build when `.env.example` and
   `config.py` drift in either direction.
2. **Never document a file or test that does not exist.** Cite the module you
   actually read.
