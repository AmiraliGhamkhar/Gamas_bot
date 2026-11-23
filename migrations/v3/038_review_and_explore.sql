-- Explore public question bank, engagement impressions, topic preferences, and unique reward guard.

CREATE TABLE IF NOT EXISTS explore_questions (
    question_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    topic         TEXT NOT NULL,
    difficulty    TEXT NOT NULL DEFAULT 'easy',
    question_text TEXT NOT NULL,
    options_json  TEXT NOT NULL,
    correct_index INTEGER NOT NULL,
    explanation   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'approved',
    created_by    INTEGER,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_explore_questions_status_topic
    ON explore_questions(status, topic, question_id);

CREATE TABLE IF NOT EXISTS explore_impressions (
    impression_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    question_id   INTEGER NOT NULL,
    shown_at      TEXT NOT NULL,
    dwell_ms      INTEGER NOT NULL DEFAULT 0,
    signal        INTEGER NOT NULL DEFAULT 0,
    is_correct    INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
    FOREIGN KEY (question_id) REFERENCES explore_questions(question_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_explore_impressions_user_q
    ON explore_impressions(user_id, question_id, shown_at DESC);

CREATE TABLE IF NOT EXISTS user_topic_preferences (
    user_id    INTEGER NOT NULL,
    topic      TEXT NOT NULL,
    weight     REAL NOT NULL DEFAULT 1.0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, topic),
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reward_points_unique_ref
    ON reward_points_ledger(user_id, ref_id)
    WHERE ref_id IS NOT NULL;
