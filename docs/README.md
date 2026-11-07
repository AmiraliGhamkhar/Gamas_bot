# Documentation

Use **PowerShell on Windows** and **Bash in cPanel Terminal**. Run commands from the
repository root. Examples use `.venv`; Setup Python App users must substitute the
interpreter shown by cPanel.

## Setup and maintenance

| Guide | Purpose |
| --- | --- |
| [Windows PowerShell](DEPLOY_WINDOWS.md) | Install, check, run and update locally |
| [cPanel](DEPLOY_CPANEL.md) | Install, cron and optional Passenger |
| [Configuration](CONFIGURATION.md) | Required values, paths and safe defaults |
| [Operations](OPERATIONS.md) | Lifecycle, logs, backups, restore and retention |
| [Troubleshooting](TROUBLESHOOTING.md) | Common failures and fixes |
| [Security](SECURITY.md) | Secrets, uploads and privacy |
| [Testing](TESTING.md) | Offline checks and manual smoke tests |

## Features and internals

| Guide | Purpose |
| --- | --- |
| [STT providers](STT_PROVIDERS.md) / [Note providers](PROVIDERS.md) | Credentials and routing |
| [Free Office and documents](OFFICE.md) / [OCR](OCR.md) | Exports, scans and limits |
| [Media](MEDIA.md) / [Presentations](PRESENTATIONS.md) | Audio and narration |
| [Word output](DOCX.md) / [Note quality](NOTE_QUALITY.md) | RTL and accuracy limits |
| [Billing](BILLING.md) / [Admin](ADMIN.md) / [Support](SUPPORT.md) | Credit, payments and tickets |
| [Channel membership](TELEGRAM_SUBSCRIPTION.md) | Required subscription setup |
| [Performance](PERFORMANCE.md) / [Architecture](ARCHITECTURE.md) | Resources and code map |
| [Changelog](CHANGELOG.md) | Concise change history |

For credential-free inspection of the effective note-provider order and token
budgets, run `python -m scripts.probe_note_providers --show-config` (offline) or
`python -m gamas_bot --check`; see [Note providers](PROVIDERS.md).

Maintainers: read [AGENTS.md](../AGENTS.md). Settings are defined in
[`config.py`](../gamas_bot/config.py) and listed in [`.env.example`](../.env.example).
Revision-specific audit reports and the duplicate Persian deployment guide were
removed; use the current guides above. Earlier documents remain in Git history.
