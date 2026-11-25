# Database Design

SQLite (WAL, one writer, append-only money) is the only datastore. Every table
below is created by a numbered file in `migrations/v3/`; the authoritative list
of application tables is `APPLICATION_TABLES` in `gamas_bot/db_core.py`, and CI
asserts that a fresh database applies every migration and leaves exactly that
set.

## 1. Core entities, reused as-is

- Accounts and credit: `users`, `user_balances`, `balance_dirty`, `ledger`,
  `holds`, `retired_holds`, `debit_allocations`, `hold_allocations`,
  `billing_migrations`, `free_uses` (quiz gift only)
- Commerce: `plans`, `payments`, `referrals`, `referral_rewards`
- Library and study material: `library_items`, `library_file_blobs`,
  `submissions`, `submission_artifacts`, `submission_binary_artifacts`,
  `transcriptions`, `documents`, `jobs`
- Review questions: `quiz_sessions`, `quiz_questions`, `quiz_answers`
- Support, audit and operations: `support_tickets`, `support_messages`,
  `support_ticket_seq`, `audit`, `broadcast_runs`
- Historical, inert: `flashcard_decks`, `flashcards` (the feature is removed; the
  rows are preserved for the users who already own them and counted as "other"
  in the library)

## 2. Study Packs (migration 037)

- **`study_packs`** — one row per accepted source, `pack_id` primary key (never a
  title), `user_id`, `title`, `source_type`, `status`
  (`queued`/`processing`/`ready`/`failed`), `created_at`/`updated_at`/`deleted_at`.
  A unique partial index guards the pack/source link, so replaying delivery
  lands on the existing row.
- **`study_assets`** — `pack_id`, `user_id`, `kind`
  (`original_source`, `transcript`, `extracted_text`, `pdf`, `docx`, `quiz_html`),
  `storage_path`, `mime_type`, `byte_size`, and a `source_kind`/`source_ref`
  reference into the store that owns the bytes. Asset bytes are never duplicated.
- **`reward_points_ledger`** — `entry_id`, `user_id`, `delta`, `reason`, `ref_id`,
  `created_at`.

## 3. Review and Explore (migrations 038, 042, and later)

### `explore_questions` (public curated bank)
- `question_id INTEGER PRIMARY KEY AUTOINCREMENT`
- `topic TEXT NOT NULL`
- `difficulty TEXT NOT NULL DEFAULT 'easy'`
- `question_text TEXT NOT NULL`
- `options_json TEXT NOT NULL` (JSON array of four strings)
- `correct_index INTEGER NOT NULL` (`0..3`)
- `explanation TEXT NOT NULL DEFAULT ''`
- `status TEXT NOT NULL DEFAULT 'approved'` (`pending`, `approved`, `rejected`)
- `created_by INTEGER` (admin `telegram_id`; `NULL` for seeded questions)
- `seed_key TEXT` (`NULL` for user-published questions; unique when present, so
  re-seeding is idempotent — migration 042)
- `created_at TEXT NOT NULL`
- Index: `idx_explore_questions_status_topic(status, topic, question_id)`

### `explore_impressions` (engagement signal)
- `impression_id`, `user_id`, `question_id`, `shown_at`, `dwell_ms`,
  `signal` (`3` answered, `2` dwell > 8 s, `1` skip < 3 s, `0` neutral),
  `is_correct` (`NULL` unless answered)
- Foreign keys cascade from `users` and `explore_questions`

### `user_topic_preferences` (explicit preferences)
- `(user_id, topic)` primary key, `weight REAL NOT NULL DEFAULT 1.0`,
  `updated_at`; `user_id` cascades from `users`

### `reward_points_ledger` unique guard
`CREATE UNIQUE INDEX idx_reward_points_unique_ref ON reward_points_ledger(user_id, ref_id) WHERE ref_id IS NOT NULL`
guarantees at-most-once credit per `(user_id, "explore_q:" || question_id)`.

### `free_uses` (migration 043)
The free-first-try gift is quiz-only: `user_id`, `free_quiz_used` (`0`/`1`
CHECK), `quiz_claim` (in-flight claim token), `updated_at`. The retired
flashcard columns (`free_deck_used`, `deck_claim`) were dropped by migration 043.

## 4. Backfill, rollback and backups

- Existing `library_items` and `quiz_sessions` stay intact and readable; Study
  Packs were added alongside them, so no historical row had to be rewritten.
  Creating `study_packs` rows for historical `booklet`/`document` items remains
  an optional, non-destructive backfill.
- The new tables (`037` onward) are additive except for migration 043, which
  rebuilds `free_uses` to drop a removed feature's columns. There are no
  downgrade scripts: roll back code and data together.
- Take a consistent SQLite online backup before applying migrations in
  production (`sqlite3 data/gamas_v3.db ".backup gamas_v3.bak.<date>"`), and
  include it with the rest of `data/` in routine backups.

## 5. Transaction boundaries and compatibility

- Every reward grant, redemption and credit write runs inside
  `BEGIN IMMEDIATE`; reporting uses the query-only connection and never mutates.
- Foreign keys are on, ids are exact int64, and user-owned rows cascade.
- Migrations are additive where possible and never rewritten after they have been
  applied. Nothing here drops user data; migration 043 rebuilds `free_uses` only
  to drop columns of a removed feature, preserving every quiz row.
