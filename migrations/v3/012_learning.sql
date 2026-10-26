-- Learning loop, part 2: quizzes and flashcard decks.
--
-- A quiz session is a cursor over an immutable question set: ``current_index``
-- is the next unanswered question, so a restart mid-quiz resumes in place
-- without any extra state. ``quiz_answers`` carries a unique index on
-- ``question_id``, which is what makes answering exactly-once even when two
-- taps race — the second insert is silently refused by SQLite rather than
-- scoring twice.
--
-- Options are stored as a JSON array of strings because a quiz question is a
-- value object, not a relational entity; nothing ever queries an individual
-- option by identity. The correct index therefore refers to the stored order,
-- which is the shuffled order the user actually saw.
--
-- Decks reference the booklet they were generated from (NULL for the single
-- hand-written deck) so a deck survives its library item being pruned; the
-- ON DELETE CASCADE on cards only covers deleting the deck itself.
CREATE TABLE IF NOT EXISTS quiz_sessions (
    session_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(telegram_id),
    item_id          INTEGER NOT NULL REFERENCES library_items(item_id),
    question_count   INTEGER NOT NULL,
    current_index    INTEGER NOT NULL DEFAULT 0,   -- next unanswered idx
    score            INTEGER NOT NULL DEFAULT 0,   -- points earned so far
    status           TEXT    NOT NULL DEFAULT 'active'  -- active | done | quit | expired
                     CHECK (status IN ('active','done','quit','expired')),
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quiz_session_user
    ON quiz_sessions(user_id, session_id DESC);

CREATE TABLE IF NOT EXISTS quiz_questions (
    question_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES quiz_sessions(session_id),
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
    session_id   INTEGER NOT NULL REFERENCES quiz_sessions(session_id),
    question_id  INTEGER NOT NULL REFERENCES quiz_questions(question_id),
    chosen_index INTEGER NOT NULL CHECK (chosen_index BETWEEN 0 AND 3),
    is_correct   INTEGER NOT NULL,
    answered_at  TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_quiz_answer_once
    ON quiz_answers(question_id);                -- exactly-once answering

CREATE TABLE IF NOT EXISTS flashcard_decks (
    deck_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(telegram_id),
    item_id      INTEGER REFERENCES library_items(item_id),  -- NULL for manual decks
    title        TEXT    NOT NULL,
    source       TEXT    NOT NULL CHECK (source IN ('ai','manual')),
    study_cursor INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS flashcards (
    card_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id   INTEGER NOT NULL REFERENCES flashcard_decks(deck_id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    front     TEXT    NOT NULL,
    back      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decks_user ON flashcard_decks(user_id, deck_id DESC);
CREATE INDEX IF NOT EXISTS idx_cards_deck ON flashcards(deck_id, idx);
