# Gamas Mini App — cPanel Deployment Guide

## 1. Build Configuration
- **Frontend directory**: `miniapp/`
- **Vite base path**: `/app/` (`miniapp/vite.config.ts`)
- **API base URL**: Relative `/api/v1/*` (same origin as Passenger, no hardcoded host or credentials)
- **Build command**:
  ```bash
  cd miniapp && npm ci && npm run build
  ```
- **Frontend tests**: `cd miniapp && npm test` (vitest + jsdom; walks the four tabs
  against the real API shapes)
- **Build output**: `miniapp/dist/` (`index.html`, `assets/*.js`, `assets/*.css`)

## 2. cPanel / Passenger Deployment Directory
- **Application root**: Existing Python App root (where `passenger_wsgi.py` lives).
- **Static Mini App path**: `miniapp/dist/` (served automatically at `/app` and `/app/` by `passenger_wsgi.py` -> `gamas_bot/webapp_api.py`).
- **Private file storage**: `private_assets/` outside `public_html` (configured via `study_storage.py`, `0o600` permissions).
- **Public URL**: `https://<your-domain>/app/`

## 3. Required Environment Configuration
Ensure `.env` on the server includes:
- `BOT_TOKEN` (already configured for the bot; used server-side for `initData` HMAC-SHA256 validation)
- `BOT_USERNAME=Gamas_bot` (default) so the Mini App can build its invite, support
  and purchase deep links (`t.me/<username>?start=ref_<id>` / `?start=buy`).
- `WEBAPP_URL=https://<your-domain>` so the bot's `/start` screen carries its one
  button (``${WEBAPP_URL}/app``) into the Mini App. Use the bare origin: the bot
  appends `/app` itself, and it strips a trailing slash. Leave it empty only when
  the Mini App is not deployed — the bot then works as a pure file interface with
  no menu button at all (nothing points at a dead URL).

## 4. Backup & Migration Steps (Before Deploying)
1. Back up SQLite database:
   ```bash
   sqlite3 gamas_bot.db ".backup gamas_bot.bak.$(date +%F)"
   ```
2. Restart the Python App in cPanel so the `migrations/v3/*.sql` files (including
   `042_explore_seed_keys.sql`) apply automatically on startup.
3. Seed the Explore bank (optional on a fresh server: the bot's `run()` does this
   once itself; both paths are idempotent and never duplicate a question):
   ```bash
   python3 scripts/validate_explore_bank.py --validate-only          # check the bank
   python3 scripts/validate_explore_bank.py --db gamas_bot.db --dry-run
   python3 scripts/validate_explore_bank.py --db gamas_bot.db        # insert what is missing
   ```
   The command inserts the 200 curated questions once, skips any that already
   exist, and demotes the numbered `(پرسش N)` placeholders written by the old
   generator to `status='rejected'` (they stay in the database with their
   engagement history; an admin can restore one). Use `--keep-legacy` to leave
   them approved, e.g. while comparing the two banks.

## 5. Telegram BotFather Setup
1. Open `@BotFather` -> `/mybots` -> select your bot -> **Bot Settings** -> **Menu Button** (or **Configure Mini App**).
2. Set URL to `https://<your-domain>/app/`.
3. Verify `https://<your-domain>/api/v1/health` returns `{"status": "ok"}`.

## 6. Smoke Test Checklist (Post-Deploy)
1. Send `/start` in the bot: one short message, one button. Press it and confirm
   `https://<your-domain>/app/` opens inside Telegram (Android, iOS, Desktop).
2. Verify **My Account** loads `user_id`, `credit_seconds`, `reward_points`, referrals, and payments.
3. Verify **Library** lists Study Packs and downloads an asset.
4. Verify **Review** groups the user's questions by Study Pack and records an answer.
5. Verify **Explore** loads one card (no total count/index shown), submits an answer,
   shows the points the server awarded, records engagement, and offers a new round
   at the end.
6. Verify the four tabs are exactly حساب من / کتابخانه / مرور / اکسپلور, that RTL
   text and Persian numerals render, and that «خرید در ربات» opens the bot's wallet.

## 7. Rollback Instructions
1. Restore the previous `miniapp/dist/` or git commit.
2. If needed, restore `gamas_bot.db` from `gamas_bot.bak.<date>`.
3. Restart the cPanel Python App.
