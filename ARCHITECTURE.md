# Gamas Architecture

One Python service, one SQLite database and one static bundle, on one origin.
This page describes what the code does today; `gamas_bot/` is the source of
truth, and [Development](docs/DEVELOPMENT.md) lists the module map.

```
Telegram ──► Telethon bot (long polling, one process)
              │  intake: submissions (audio/video/voice) / documents (PDF, DOCX, PPTX)
              │  jobs:   probe → extract → STT → notes → booklet/DOCX/HTML → delivery
              ▼
           SQLite (WAL, single writer, append-only ledger)
              │  study_packs (one per source) + study_assets (references, not copies)
              │  quiz_sessions / quiz_questions linked to the pack that produced them
              │  explore_questions (pending → approved/rejected, stable seed_key)
              │  explore_impressions (dwell/signal/is_correct)
              │  reward_points_ledger ↔ reward_redemptions ↔ ledger (credit lots)
              ▼
           Passenger WSGI app (passenger_wsgi.py, same host)
              ├── /api/v1/*   initData-authenticated JSON  (gamas_bot/webapp_api.py)
              └── /app/*      the built Mini App (Vite SPA, RTL, Persian)
```

## 1. Components

1. **Telegram bot** (`gamas_bot/bot*.py`) — uploads, job progress, the file
   interface, wallet, support, referrals and admin. Every accepted source is
   registered as one `study_packs` row plus `study_assets` references, alongside
   the `library_items`/`submissions` rows the rest of the bot uses.
2. **Document and booklet engine** (`notes*`, `docx_export`, `notes_pdf`,
   `notes_typst`, `document/`) — one validated `StructuredNotes` model rendered to
   Word and PDF; see [Booklet design](docs/BOOKLET_DESIGN.md).
3. **WSGI JSON API** (`gamas_bot/webapp_api.py`) — stdlib, no framework; shares
   `Database` and `Settings` with the bot. Routes are listed in
   [API contract](API_CONTRACT.md).
4. **Mini App** (`miniapp/`) — React/TS/Vite/Tailwind SPA with exactly four tabs:
   **مرور · کتابخانه · یک‌سؤال · حساب من**. It computes no money and shows no
   number the server did not send. Design law: `miniapp/DESIGN.md`.
5. **Question system** — private Review questions (`quiz_sessions`,
   `quiz_questions`, `quiz_answers`, scoped to the owner's packs) are strictly
   separated from the public Explore bank (`explore_questions`).
6. **Deterministic recommender** (`gamas_bot/explore_recommender.py`) — scores
   approved questions by topic affinity, quality, freshness and a discovery
   bonus, then enforces a consecutive-same-topic cap. Engagement signal
   (`3` answered, `2` dwell > 8 s, `1` skip < 3 s, `0` neutral) only ranks; it
   never grants points.
7. **Reward system** (`gamas_bot/rewards.py`, `db_rewards.py`) — reward points are
   an independent ledger. Only a first-time **correct** answer to an **approved**
   Explore question earns points, guarded by a unique `(user_id, ref_id)`.

## 2. Study Pack model

Each accepted source creates one `study_packs` row identified by `pack_id`
(integer, never a title; duplicate titles get distinct ids):

- `pack_id`, `user_id` (owner `telegram_id`), `title` (display only),
  `source_type` (`audio`, `video`, `voice`, `pdf`, `docx`, `pptx`, `txt`, `upload`),
  `status` (`queued`, `processing`, `ready`, `failed`),
  `created_at`/`updated_at`/`deleted_at` (ISO-8601 UTC).
- `study_assets`: `original_source`, `transcript`, `extracted_text`, `pdf`,
  `docx`, `quiz_html` — each a reference into the store that owns the bytes.
- Questions attach through `quiz_sessions.pack_id`, so Review can group by the
  pack that produced them. Sessions from before Study Packs are shown as explicit
  legacy groups.

## 3. Explore ranking

```
affinity(user, topic) = Σ signal_weight · exp(−λ · age_days)
score(user, q)        = w_affinity·affinity + w_quality·quality
                      + w_freshness·freshness + w_diversity·discovery
```

- Answered or recently seen questions are excluded from the candidate pool.
- At most `max_consecutive_same_topic` items from one topic run back to back.
- Cold start: new users get approved questions in ascending id order, because
  every topic has neutral affinity.
- The Explore UI never shows a bank count, a question index or a progress bar.

## 4. Rewards and credit

- **Separate ledgers.** Processing credit (`ledger`, integer seconds,
  lot-aware) and reward points (`reward_points_ledger`) are independent; a
  `reward_redemptions` row is the single bridge that ties a points debit to a real
  credit lot, written in the same transaction as both.
- **Earning.** Only `is_correct = 1` on an `approved` question inserts a positive
  delta, keyed `"explore_q:" || question_id` so a user earns at most once per
  question. Dwell, skips, neutral reads and wrong answers never grant points.
- **Redemption.** `POST /api/v1/rewards/redeem` checks the balance, debits points,
  grants the credit lot and records the redemption inside one `BEGIN IMMEDIATE`
  transaction; the response reports only what the ledger holds.

## 5. Security

- **Telegram authentication.** `X-Telegram-Init-Data` is validated server-side
  with HMAC-SHA256 over the sorted payload using `TELEGRAM_BOT_TOKEN`
  (`gamas_bot/telegram_auth.py`), constant-time compared, with `auth_date`
  freshness (24 h) and a required `user.id`. The client's `user_id` is never
  trusted.
- **Authorization.** Every query scopes by the authenticated `user_id`; a foreign
  id resolves to `404`, never to data. Admin routes require `ADMIN_IDS`
  (`403` otherwise).
- **Private files.** `study_storage.py` keeps bytes outside `public_html` with
  `0o600` permissions and traversal checks; downloads always send
  `Cache-Control: private, no-store`.
- **Money and rewards.** `BEGIN IMMEDIATE` prevents double-spend; unique indexes
  prevent duplicate Explore awards, duplicate redemptions and duplicate seeds.
- **Untrusted input.** JSON bodies are capped at 1 MiB, every value is bound as a
  SQL parameter, and the client never computes a price, a balance or an award.

## 6. Mini App tabs

1. **حساب من** — identity, credit split into hours/minutes, active plan window,
   purchase options, referral count and invite link, reward balance and catalogue,
   payment history; buying still happens in the bot via deep link.
2. **کتابخانه** — one row per Study Pack with its processing state; the detail
   screen downloads real assets, offers Review only when the pack owns questions,
   and re-reads itself while the pipeline is still working.
3. **مرور** — the user's own questions grouped by the pack that produced them;
   answers are recorded through `/api/v1/review/answers` and the stored verdict is
   shown.
4. **یک‌سؤال** — one Explore card at a time, with the server's verdict, the points
   the server says were awarded, and engagement recorded as dwell time.
