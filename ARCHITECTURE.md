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

Component meaning (all four are returned to the client in `explain`):

- `affinity` — smoothed, time-decayed topic interest learned from impression
  signals (`λ = 0.1`, prior 2.0 over 5 pseudo-observations).
- `quality` — difficulty fit × **academic source tier**: `textbook` 1.00 >
  `institutional` 0.95 > `educational` 0.85 > unverified 0.55. A cited question
  always outranks an equivalent uncited one; an uncited question can still appear
  so the archive is not lost, but it is labelled *awaiting verification*.
- `freshness` — `exp(−age_days / 120)` on `created_at`, giving new content a
  window of visibility.
- `discovery` — an explicit bonus for topics the user never engaged with, so
  personalisation cannot collapse the feed into one subject.

Structural rules on top of the score:

- Answered or recently seen questions are excluded from the candidate pool **in
  SQL**, never in a post-filter over a capped window.
- At most `max_consecutive_same_topic` (2) items from one topic run back to back,
  enforced across page boundaries via the cursor.
- Cold start: every topic has neutral affinity, so the feed is naturally diverse.
- Ranking is deterministic: score descending, then `question_id` ascending. The
  same database state always produces the same page, which is what makes the
  cursor stable.

### Pagination

`limit` is clamped to `1…20` (default `12`) and pages are cursor-based, not
offset-based. The cursor is an opaque base64 token carrying the ids already
served in the session plus the trailing topics. That closes the one hole offset
pagination cannot: a prefetched next page may arrive while cards are still
unread (so not yet impressioned), and the served-id set guarantees it cannot
repeat one of them. A malformed cursor degrades to a fresh session; the SQL
seen-filter still prevents repeats from the user's own history.

The Mini App asks for the next page when the reader is three cards from the end,
so questions are already in memory before the last card is on screen.

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
4. **یک‌سؤال** — a continuous vertical feed: one academic question per viewport,
   moved by the phone's own scroll with CSS snap points (no carousel library, no
   "next" button). Each card answers in place and reveals its explanation, the
   points the server awarded, and a folded «منبع علمی» citation. The next page is
   prefetched before the reader reaches the end; engagement is one event per card
   (dwell, plus whether the source was opened), never per scroll pixel.
