# Gamas Mini App — cPanel Deployment Guide

This guide describes the **single** cPanel Python App that serves the Telegram
Mini App and its API, while the existing Gamas bot keeps running as a
long-running Telegram client.

## 1. cPanel Python App configuration

| Field | Value |
| --- | --- |
| Python version | 3.11–3.13 (match the host-supported interpreter) |
| Application root | `/home/USER/gamas_bot` (repository root, **outside** `public_html`) |
| Application URL | `https://gamasrobot.ir` (or your deployment domain) |
| Application startup file | `passenger_wsgi.py` |
| Application Entry point | `application` |

`passenger_wsgi.py` is the authoritative WSGI entry point. It routes:

- `GET /app/` and `/app/*` → built Mini App SPA from `miniapp/dist/`
- `/api/v1/*` → Mini App JSON API (`gamas_bot.webapp_api`)
- `GET /api/v1/health` → `{"status":"ok"}`
- `GET /`, `GET /health` → bot watchdog status JSON

Do **not** point Passenger at `scripts/mini_app_wsgi.py`; that file is not part
of the repository. Do **not** create a separate Node.js production server.

## 2. Virtual environment and dependencies

Use the cPanel Python App virtualenv (or an equivalent venv in the app root):

```bash
cd /home/USER/gamas_bot
# Example cPanel venv path — replace <python-version> with the one cPanel shows:
. /home/USER/virtualenv/gamas_bot/<python-version>/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m scripts.cpanel_preflight
python -m gamas_bot --check
```

`requirements.txt` is generated from `pyproject.toml`; install from it on
cPanel. Keep `.env` at `chmod 600` and `data/` at `chmod 700`.

## 3. Environment variables

Configure these in `/home/USER/gamas_bot/.env` (never in `public_html`):

| Variable | Required | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | Bot token; used by the bot and by server-side Mini App `initData` HMAC-SHA256 validation. There is no separate `BOT_TOKEN`. |
| `TELEGRAM_API_ID` | yes | Telegram API id for the bot client. |
| `TELEGRAM_API_HASH` | yes | Telegram API hash for the bot client. |
| `ADMIN_IDS` | yes | Comma-separated numeric Telegram admin IDs. |
| `BOT_USERNAME` | recommended | Bot username without `@` (default `Gamas_bot`). Mini App builds invite/support/purchase links from it. |
| `WEBAPP_URL` | recommended | Public HTTPS origin, e.g. `https://gamasrobot.ir`. The bot appends `/app` for the menu button. Leave empty only if the Mini App is not deployed. |
| `SUBSCRIPTION_CHANNEL` | as configured | Mandatory channel (`@gamas_AI` or numeric id). |
| `SUBSCRIPTION_REQUIRED` | as configured | `true` to enforce membership gate. |

See `.env.example` and [Configuration](CONFIGURATION.md) for the full list.

## 4. Build the Mini App locally / in CI

```bash
cd miniapp
npm ci
npm run build
npm test
```

Build output: `miniapp/dist/` containing `index.html` and hashed files under
`assets/`. Vite base is `/app/` (see `miniapp/vite.config.ts`).

## 5. Upload destination for `miniapp/dist`

Upload the **contents** of the built `miniapp/dist/` directory to:

```text
/home/USER/gamas_bot/miniapp/dist/
```

The directory must contain `index.html` and an `assets/` subdirectory. Passenger
serves this directory at `https://gamasrobot.ir/app/` through
`passenger_wsgi.py` → `gamas_bot/webapp_api.py`.

Do not place the dist inside `public_html` as a separate static site; the
Python app serves it so SPA routes and API same-origin behavior work correctly.

## 6. Passenger restart procedure

After uploading code, dependencies, `.env`, or `miniapp/dist/`:

1. In cPanel, open **Setup Python App**.
2. Click **Restart** for the Gamas application.
3. If you also updated bot code and use cron watchdog, follow the safe update
   flow in [CPANEL.md](CPANEL.md#6-update-safely) (disable cron, stop bot,
   pull/install/check, restart, re-enable cron).

You can also touch the Passenger restart file if configured by your host, but
the cPanel Restart button is the supported path.

## 7. Health-check URLs

| URL | Expected response | Purpose |
| --- | --- | --- |
| `https://gamasrobot.ir/api/v1/health` | HTTP 200, `{"status":"ok"}` | Mini App API health |
| `https://gamasrobot.ir/app/` | HTTP 200, Mini App HTML | SPA entry |
| `https://gamasrobot.ir/health` | HTTP 200/503, `{"service":"gamas-bot","status":"..."}` | Bot watchdog status (monitor only; not public data) |

## 8. Telegram BotFather configuration

1. Open `@BotFather` → `/mybots` → select your bot.
2. **Bot Settings** → **Menu Button** (or **Configure Mini App** / **Main Mini App** depending on BotFather UI).
3. Set Menu Button URL to: `https://gamasrobot.ir/app/`
4. If configuring a Main Mini App, use the same URL.
5. Ensure `WEBAPP_URL=https://gamasrobot.ir` is set in `.env` so the bot’s
   `/start` button also opens the Mini App.

## 9. Smoke test checklist

1. `GET /api/v1/health` returns HTTP 200 and `{"status":"ok"}`.
2. `GET /app/` loads the Mini App in a browser and Telegram WebView.
3. Open the bot in Telegram, send `/start`, press the Gamas button; app opens on
   **مرور**.
4. **حساب من** shows identity/balance; «کیف پول» shows plans/rewards/payments/referrals.
5. **کتابخانه** lists Study Packs and downloads an asset.
6. **مرور** groups questions by Study Pack and records answers.
7. **یک‌سؤال** loads a card, submits an answer, shows awarded points, records
   engagement.
8. Tabs are exactly **مرور · کتابخانه · یک‌سؤال · حساب من**.
9. Unauthenticated protected API requests return 401.
10. Existing bot workflows (uploads, payments, admin, referrals) still work.

## 10. Rollback instructions

1. Keep a backup before deploy: `sqlite3 data/gamas_v3.db ".backup data/gamas_v3.bak.$(date +%F)"` and copy `.env`, `data/`, and the previous `miniapp/dist/`.
2. To roll back frontend only: restore previous `miniapp/dist/` and restart the
   Python App.
3. To roll back code: `git checkout <previous-good-commit>`, reinstall
   dependencies if needed, restore `miniapp/dist/`, restart Python App.
4. To roll back data-affecting release: restore code **and** the matching DB
   backup together. Never run older code against a migrated database.
5. Re-run health checks and smoke tests after rollback.

## 11. Backup & migration notes

- Back up SQLite before deploying: `sqlite3 gamas_bot.db ".backup gamas_bot.bak.$(date +%F)"` (adjust DB path to your `DB_PATH`).
- Restarting the Python App/bot applies migrations automatically on startup.
- The Explore bank seed is idempotent: `python3 scripts/validate_explore_bank.py --db gamas_bot.db --dry-run`.
