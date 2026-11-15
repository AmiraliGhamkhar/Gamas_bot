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
| [Providers](PROVIDERS.md) | Approved models, task/fallback policy, quotas, sensitivity and offline/live probes |
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
| [Changelog](CHANGELOG.md) | Latest release notes |

## Other documentation

- [Audit report](../AUDIT/REPORT.md), [architecture](../AUDIT/architecture.md)
  and [working notes](../AUDIT/notes.md) are a historical audit, not a current
  security certification. Its generated evidence logs are not tracked.
- [Test fixtures](../tests/fixtures/README.md) describes the synthetic corpus
  and private-audio guidance.

Maintainers also read [AGENTS.md](../AGENTS.md). Settings are defined in
[`config.py`](../gamas_bot/config.py) and shown with defaults in
[`.env.example`](../.env.example).
