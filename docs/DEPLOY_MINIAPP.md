# Gamas Mini App — cPanel Deployment Guide

## 1. Build Configuration
- **Frontend directory**: `miniapp/`
- **Vite base path**: `/app/` (`miniapp/vite.config.ts`)
- **API base URL**: Relative `/api/v1/*` (same origin as Passenger, no hardcoded host or credentials)
- **Build command**:
  ```bash
  cd miniapp && npm ci && npm run build
  ```
- **Build output**: `miniapp/dist/` (`index.html`, `assets/*.js`, `assets/*.css`)

## 2. cPanel / Passenger Deployment Directory
- **Application root**: Existing Python App root (where `passenger_wsgi.py` lives).
- **Static Mini App path**: `miniapp/dist/` (served automatically at `/app` and `/app/` by `passenger_wsgi.py` -> `gamas_bot/webapp_api.py`).
- **Private file storage**: `private_assets/` outside `public_html` (configured via `study_storage.py`, `0o600` permissions).
- **Public URL**: `https://<your-domain>/app/`

## 3. Required Environment Configuration
Ensure `.env` on the server includes:
- `BOT_TOKEN` (already configured for the bot; used server-side for `initData` HMAC-SHA256 validation)
- `PUBLIC_URL=https://<your-domain>` (or `WEBAPP_URL=https://<your-domain>/app`) so the bot's `/start` button launches the Mini App.

## 4. Backup & Migration Steps (Before Deploying)
1. Back up SQLite database:
   ```bash
   sqlite3 gamas_bot.db ".backup gamas_bot.bak.$(date +%F)"
   ```
2. Restart the Python App in cPanel so `037_study_packs.sql` and `038_review_and_explore.sql` apply automatically on startup.
3. Optionally seed the 200-question Explore bank:
   ```bash
   python3 scripts/validate_explore_bank.py
   ```

## 5. Telegram BotFather Setup
1. Open `@BotFather` -> `/mybots` -> select your bot -> **Bot Settings** -> **Menu Button** (or **Configure Mini App**).
2. Set URL to `https://<your-domain>/app/`.
3. Verify `https://<your-domain>/api/v1/health` returns `{"status": "ok"}`.

## 6. Smoke Test Checklist (Post-Deploy)
1. Open `https://<your-domain>/app/` inside Telegram (Android, iOS, Desktop).
2. Verify **My Account** loads `user_id`, `credit_seconds`, `reward_points`, referrals, and payments.
3. Verify **Library** lists Study Packs and downloads an asset.
4. Verify **Review** lists personal quiz sessions and records an answer.
5. Verify **Explore** loads a question card (no total count/index shown), submits an answer, and records engagement/reward points.

## 7. Rollback Instructions
1. Restore the previous `miniapp/dist/` or git commit.
2. If needed, restore `gamas_bot.db` from `gamas_bot.bak.<date>`.
3. Restart the cPanel Python App.
