# Gamas Product Architecture Blueprint

## 1. System Architecture & Component Responsibilities

The architecture extends the existing `gamas_bot` Python package, SQLite database, and `passenger_wsgi.py` entry point rather than replacing them.

```
+-----------------------------------------------------------------------+
|                       Telegram Client & Mini App                      |
|  - Bot Chat (Telethon): file uploads, job progress, admin, support    |
|  - Mini App (React/TS/Vite/Tailwind at /app/):                        |
|      [1. My Account] [2. Library] [3. Review] [4. Explore]            |
+-----------------------------------+-----------------------------------+
                                    | HTTPS (X-Telegram-Init-Data)
                                    ▼
+-----------------------------------------------------------------------+
|                cPanel / Passenger (passenger_wsgi.py)                 |
|  - GET /, /health   -> gamas_bot.launcher.ensure_running()            |
|  - /api/*           -> gamas_bot.webapp_api (stdlib WSGI JSON API)    |
|  - /app/*           -> miniapp/dist/ (static SPA)                     |
+-----------------------------------+-----------------------------------+
                                    |
          +-------------------------+-------------------------+
          ▼                                                   ▼
+-------------------------------+               +-----------------------+
|   SQLite (gamas_bot.db)       |               | Private File Storage  |
|   - Existing Bot/Billing/Quiz |               | (study_storage.py)    |
|   - Study Packs & Assets      |               | - Outside public_html |
|   - Review & Explore Bank     |               | - Per-user/pack dirs  |
|   - Engagement & Reward Ledger|               | - 0o600 permissions   |
+-------------------------------+               +-----------------------+
```

### Component Responsibilities
1. **Existing Python Telegram Bot (`gamas_bot/bot*.py`)**:
   - Handles media/document uploads, STT (`stt.py`), booklet synthesis (`booklet_synthesis.py`), PDF/DOCX/HTML export, quiz generation (`bot_learning.py`), payments (`bot_wallet.py`), referrals (`bot_referrals.py`), support, and admin commands (`bot_admin.py`).
   - Registers newly processed uploads as `study_packs` + `study_assets` alongside existing `library_items` / `submissions`.
2. **Backend WSGI API (`gamas_bot/webapp_api.py`)**:
   - Stateless stdlib WSGI layer sharing `gamas_bot.db.Database` and `gamas_bot.config.Settings`.
   - Validates Telegram `initData` HMAC-SHA256 (`telegram_auth.py`), enforces per-user ownership, serves private files via `study_storage.py`, and exposes JSON endpoints for all 4 Mini App tabs.
3. **React Telegram Mini App (`miniapp/`)**:
   - Single-page React/TS/Vite/Tailwind app with 4 bottom-nav tabs: **My Account**, **Library**, **Review**, **Explore**.
4. **Question System**:
   - **Private Review Questions**: `quiz_sessions` / `quiz_questions` / `quiz_answers` linked to user Study Packs.
   - **Public Explore Question Bank**: Curated, admin-approved Persian multiple-choice questions (`explore_questions`) — strictly separated from private user media/transcripts.
5. **Deterministic Recommendation Engine**:
   - Ranks `explore_questions` per user using impression/engagement signals (`3` = answer submitted, `2` = dwell > 8s, `1` = fast skip < 3s, `neutral` = 3–8s), topic affinity, diversity, and time decay.
6. **Independent Reward System**:
   - `reward_points_ledger` tracks redeemable points separately from Gamas `credit_seconds`. Only **correct answers to approved public Explore questions** (first-time per question) earn answer-based reward points.

---

## 2. Study Pack Model

Each uploaded file creates one `study_packs` record identified by `pack_id` (integer primary key, never by title; duplicate titles get distinct `pack_id`s).

- **`pack_id`**: `INTEGER PRIMARY KEY AUTOINCREMENT`
- **`user_id`**: `INTEGER NOT NULL` (owner `telegram_id`)
- **`title`**: `TEXT NOT NULL` (display label only)
- **`source_type`**: `TEXT NOT NULL` (`audio`, `video`, `voice`, `pdf`, `docx`, `pptx`, `txt`, `upload`)
- **`status`**: `TEXT NOT NULL` (`queued`, `processing`, `ready`, `failed`)
- **`created_at` / `updated_at` / `deleted_at`**: ISO-8601 UTC timestamps
- **Linked Assets (`study_assets`)**:
  - `original_source` (private FS path)
  - `transcript` / `extracted_text` (private FS path)
  - `pdf` (private FS path)
  - `docx` (private FS path)
  - `quiz_html` (private FS path)
- **Linked Questions**: `quiz_sessions` / `quiz_questions` referenced via `library_item_id` or `pack_id`.

---

## 3. Recommendation Architecture (Deterministic)

- **Engagement Classification** (internal ranking signal only, never redeemable points):
  - Answer submitted = `3`
  - Long dwell (> 8s without answering) = `2`
  - Fast skip (< 3s without answering) = `1`
  - Medium dwell (3–8s without answering) = `0` (neutral)
- **Impression Lifecycle**:
  - `explore_impressions` records `(impression_id, user_id, question_id, shown_at, dwell_ms, action)`.
  - Duplicate prevention: questions answered or skipped within the cooldown window are excluded from the candidate pool.
- **Topic Affinity & Ranking**:
  - `affinity(user, topic) = sum(signal_weight * exp(-lambda * age_days))`
  - `score(user, q) = w_affinity * topic_affinity + w_novelty * novelty_bonus - w_seen * recent_penalty`
- **Diversity Rules**:
  - No more than 2 consecutive questions from the same `topic` in a feed page.
- **Cold-Start**:
  - New users with zero engagement history receive a balanced round-robin sample across active `topic`s ordered by global approval/ease.
- **Explore UI Rule**:
  - Never display total question counts, question indices, or bank progress indicators.

---

## 4. Reward Architecture

- **Independent Ledger**: `reward_points_ledger(entry_id, user_id, delta, reason, ref_id, created_at)`.
- **Earning Rule**: Only `is_correct = 1` on an `approved` public `explore_questions` row inserts a positive delta, guarded by a `UNIQUE(user_id, ref_id)` constraint on `(user_id, "explore_q:" || question_id)` so a user can earn at most once per question.
- **Non-Earning Actions**: Dwell (`2`), skip (`1`), neutral (`0`), and incorrect answers (`is_correct = 0`) never insert into `reward_points_ledger`.
- **Redemption**: `POST /api/rewards/redeem` atomically checks `SUM(delta) >= cost` inside a `BEGIN IMMEDIATE` transaction, inserts a negative `reward_points_ledger` entry, and grants the corresponding reward (e.g., Gamas credit seconds via `db_billing`).

---

## 5. Security Architecture

- **Telegram Authentication**: `X-Telegram-Init-Data` validated server-side via HMAC-SHA256 (`telegram_auth.py`) using `bot_token` and `max_age_seconds = 86400`. Frontend-supplied `user_id` is never trusted.
- **User Authorization**: Every query scopes by `user_id = authenticated_user_id`; cross-user access returns `404 Not Found`.
- **Private File Access**: `study_storage.py` stores files outside `public_html` with `0o600` permissions and `Path.is_relative_to()` traversal checks; `/api/assets/{id}/download` sets `Cache-Control: private, no-store`.
- **Payment & Reward Security**: `BEGIN IMMEDIATE` transactions prevent double-spend on reward redemption; `UNIQUE(user_id, ref_id)` prevents duplicate Explore answer rewards.
- **Admin Authorization**: Admin-only endpoints (e.g., Explore question moderation) check `user_id in settings.admin_ids`.

---

## 6. Frontend Architecture (`miniapp/`)

- **4 Primary Bottom-Nav Tabs**:
  1. **My Account (`account`)**: Profile, Gamas `credit_seconds`, separate `reward_points`, subscription/free-use status, referrals, payment/redemption actions.
  2. **Library (`library`)**: Study Packs list, pack detail view, private asset downloads, pack deletion.
  3. **Review (`review`)**: Private quiz sessions from user Study Packs, interactive question answering, score/history.
  4. **Explore (`explore`)**: Public curated Persian question feed (no counts/indices/progress bars), dwell/skip/answer telemetry, instant correctness feedback + reward point notification.
