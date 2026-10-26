-- Learning loop: quizzes, their questions/answers (the audit trail and the
-- source of the all-time points total) and per-user flashcard decks.
--
-- Foreign-key actions are deliberate because library_prune hard-deletes old
-- library_items while PRAGMA foreign_keys is ON:
--   * quiz_sessions.item_id  -> ON DELETE CASCADE: pruning a booklet retires
--     its gameplay rows; users.quiz_points (the durable leaderboard total) is
--     never rolled back.
--   * quiz_questions/answers  -> ON DELETE CASCADE so the session cascade can
--     actually run (the default NO ACTION would block it).
--   * flashcard_decks.item_id -> ON DELETE SET NULL: the user's stored deck is
--     an investment and survives the source booklet being pruned, becoming a
--     manual-style deck with no linked item.
ALTER TABLE users ADD COLUMN quiz_points INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS quiz_sessions (
    session_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(telegram_id),
    item_id        INTEGER NOT NULL REFERENCES library_items(item_id) ON DELETE CASCADE,
    question_count INTEGER NOT NULL,
    current_index  INTEGER NOT NULL DEFAULT 0,   -- next unanswered idx
    score          INTEGER NOT NULL DEFAULT 0,   -- points earned so far
    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','done','quit','expired')),
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quiz_sessions_user
    ON quiz_sessions(user_id, status, created_at);

CREATE TABLE IF NOT EXISTS quiz_questions (
    question_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES quiz_sessions(session_id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,               -- 0-based order
    question      TEXT    NOT NULL,
    options_json  TEXT    NOT NULL,               -- JSON array of exactly 4 strings
    correct_index INTEGER NOT NULL CHECK (correct_index BETWEEN 0 AND 3),
    explanation   TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_quiz_session_idx
    ON quiz_questions(session_id, idx);

CREATE TABLE IF NOT EXISTS quiz_answers (
    answer_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES quiz_sessions(session_id) ON DELETE CASCADE,
    question_id  INTEGER NOT NULL REFERENCES quiz_questions(question_id) ON DELETE CASCADE,
    chosen_index INTEGER NOT NULL CHECK (chosen_index BETWEEN 0 AND 3),
    is_correct   INTEGER NOT NULL,
    answered_at  TEXT    NOT NULL
);
-- Exactly-once answering: a second tap on an already answered question hits
-- this index instead of double-counting points.
CREATE UNIQUE INDEX IF NOT EXISTS idx_quiz_answer_once
    ON quiz_answers(question_id);

CREATE TABLE IF NOT EXISTS flashcard_decks (
    deck_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(telegram_id),
    item_id      INTEGER REFERENCES library_items(item_id) ON DELETE SET NULL,  -- NULL for manual decks
    title        TEXT    NOT NULL,
    source       TEXT    NOT NULL CHECK (source IN ('ai','manual')),
    study_cursor INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS flashcards (
    card_id INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id INTEGER NOT NULL REFERENCES flashcard_decks(deck_id) ON DELETE CASCADE,
    idx     INTEGER NOT NULL,
    front   TEXT    NOT NULL,
    back    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decks_user ON flashcard_decks(user_id, deck_id DESC);
CREATE INDEX IF NOT EXISTS idx_cards_deck ON flashcards(deck_id, idx);
