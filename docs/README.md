# Documentation

Every command below runs from the repository root. Windows examples use
PowerShell with the project virtualenv (`\.\.venv\Scripts\python.exe`); cPanel
examples assume you activated the virtualenv (`source .venv/bin/activate`).

## Deploy

| Guide | Use it for |
| --- | --- |
| [Windows PowerShell](WINDOWS.md) | Install, run, autostart and update on a Windows PC |
| [cPanel](CPANEL.md) | Install, cron watchdog, logs and updates on shared Linux hosting |

## Configure and operate

| Guide | Use it for |
| --- | --- |
| [Configuration](CONFIGURATION.md) | Required values, keys, limits, paths and validation |
| [Providers](PROVIDERS.md) | Approved models, task/fallback policy, quotas and offline/live probes |
| [Operations](OPERATIONS.md) | Start/stop, logs, backup, restore, retention, host sizing |
| [Troubleshooting](TROUBLESHOOTING.md) | Symptom → fix table |
| [Security](SECURITY.md) | Secrets, permissions, data flow, privacy |

## Product behaviour

| Guide | Use it for |
| --- | --- |
| [Features](FEATURES.md) | Media, documents, Office, narration, notes and exports |
| [Billing](BILLING.md) | Credit, plans, purchases, learning costs, refunds, referrals |
| [Admin](ADMIN.md) | Admin panel, commands, payments review, support, membership gate |

## Development

| Guide | Use it for |
| --- | --- |
| [Testing](TESTING.md) | Offline gates on Windows and cPanel/Linux, smoke checklist |
| [Development](DEVELOPMENT.md) | Architecture, code map, migration and dependency rules |
| [Booklet design](BOOKLET_DESIGN.md) | Design tokens, components, cover artwork, DOCX/PDF pipelines and visual QA |
| [Mini App design](../miniapp/DESIGN.md) | The Mini App design law: tokens, surfaces, motion and the enforced lint rules |
| [Mini App deployment](DEPLOY_MINIAPP.md) | Build, cPanel/Passenger placement, BotFather menu button, smoke test, rollback |
| [Changelog](CHANGELOG.md) | Latest release notes |

## Other documentation

- Every page under [AUDIT/](../AUDIT/) is a **historical snapshot** of one earlier
  checkout, not current documentation and not a security certification. Its
  generated evidence logs are not tracked; read `gamas_bot/` and these guides
  instead, and re-run the checks before relying on any finding.
- [API contract](../API_CONTRACT.md), [architecture](../ARCHITECTURE.md) and
  [database design](../DATABASE_DESIGN.md) describe the current code surface.
- [Production readiness](PRODUCTION_READINESS.md) is a historical verification
  record from one earlier checkout; its §7 residual-risk list is still current,
  its test counts are not.
- [Test fixtures](../tests/fixtures/README.md) describes the synthetic corpus
  and private-audio guidance.

Maintainers also read [AGENTS.md](../AGENTS.md). Settings are defined in
[`config.py`](../gamas_bot/config.py) and shown with defaults in
[`.env.example`](../.env.example).
