# Database Design

## 1. Existing Entities (Reused As-Is)
- `users`, `user_balances`, `balance_dirty`, `billing_migrations`, `free_uses`
- `payments`, `referrals`, `referral_rewards`
- `library_items`, `library_file_blobs`, `submissions`, `submission_artifacts`, `submission_binary_artifacts`, `transcriptions`, `documents`, `jobs`
- `quiz_sessions`, `quiz_questions`, `quiz_answers`
- `flashcard_decks`, `flashcards` (historical, inert)
- **Already added in `037_study_packs.sql`**: `study_packs`, `study_assets`, `reward_points_ledger`

## 2. Genuinely Missing Entities (To Add in `038_review_and_explore.sql`)

### `explore_questions` (Public Curated Question Bank)
- `question_id INTEGER PRIMARY KEY AUTOINCREMENT`
- `topic TEXT NOT NULL`
- `difficulty TEXT NOT NULL DEFAULT 'easy'`
- `question_text TEXT NOT NULL`
- `options_json TEXT NOT NULL` (JSON array of 4 strings)
- `correct_index INTEGER NOT NULL` (`0..3`)
- `explanation TEXT NOT NULL DEFAULT ''`
- `status TEXT NOT NULL DEFAULT 'pending'` (`pending`, `approved`, `rejected`)
- `created_by INTEGER` (admin `telegram_id`, nullable for seeded questions)
- `seed_key TEXT` (stable identity of a curated seed question, `NULL` for user-published ones; unique when present)
- `created_at TEXT NOT NULL`
- **Indexes**: `idx_explore_questions_status_topic ON explore_questions(status, topic, question_id)`

### `explore_impressions` (Engagement & Recommendation Signals)
- `impression_id INTEGER PRIMARY KEY AUTOINCREMENT`
- `user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE`
- `question_id INTEGER NOT NULL REFERENCES explore_questions(question_id) ON DELETE CASCADE`
- `shown_at TEXT NOT NULL`
- `dwell_ms INTEGER NOT NULL DEFAULT 0`
- `signal INTEGER NOT NULL DEFAULT 0` (`3`=answered, `2`=dwell>8s, `1`=skip<3s, `0`=neutral)
- `is_correct INTEGER` (`NULL` if not answered, `0` or `1` if answered)
- **Indexes**: `idx_explore_impressions_user_q ON explore_impressions(user_id, question_id, shown_at DESC)`

### `user_topic_preferences` (Explicit Recommendation Preferences)
- `user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE`
- `topic TEXT NOT NULL`
- `weight REAL NOT NULL DEFAULT 1.0`
- `updated_at TEXT NOT NULL`
- **Primary Key**: `PRIMARY KEY (user_id, topic)`

### `reward_points_ledger` Unique Guard
- `CREATE UNIQUE INDEX IF NOT EXISTS idx_reward_points_unique_ref ON reward_points_ledger(user_id, ref_id) WHERE ref_id IS NOT NULL;`
  - Guarantees at-most-once reward per `(user_id, "explore_q:" || question_id)`.

## 3. Transaction Boundaries & Backward Compatibility
- All reward-granting and redemption operations execute inside `BEGIN IMMEDIATE` transactions.
- Foreign keys remain `ON DELETE CASCADE` for user-owned rows.
- No existing tables or columns are dropped or altered destructively.
