-- Keep free attempts recoverable until a validated preview has been saved.
-- NULL means there is no in-flight claim; existing used flags stay unchanged.
ALTER TABLE free_uses ADD COLUMN deck_claim TEXT;
ALTER TABLE free_uses ADD COLUMN quiz_claim TEXT;
ALTER TABLE quiz_questions ADD COLUMN difficulty TEXT NOT NULL DEFAULT '';

-- Migration 010 declared library_items.user_id as INTEGER but omitted the
-- foreign key. Rebuild just that table, keeping item ids and all child links.
-- SQLite requires foreign_keys OFF while the old parent is replaced. The
-- transactional rebuild is safe to retry even if migration bookkeeping is
-- interrupted after COMMIT. Preserve historical items with missing owners by
-- creating their exact integer user ids before installing the constraint.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO users(telegram_id, joined_at)
    SELECT user_id, MIN(created_at) FROM library_items GROUP BY user_id;
CREATE TABLE library_items_rebuild (
    item_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(telegram_id),
    submission_id INTEGER,
    kind          TEXT NOT NULL CHECK (kind IN ('booklet','transcript')),
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    unavailable   INTEGER NOT NULL DEFAULT 0,
    content_text  TEXT
);
INSERT INTO library_items_rebuild
    (item_id, user_id, submission_id, kind, chat_id, message_id, title,
     created_at, unavailable, content_text)
    SELECT item_id, user_id, submission_id, kind, chat_id, message_id, title,
           created_at, unavailable, content_text FROM library_items;
DROP TABLE library_items;
ALTER TABLE library_items_rebuild RENAME TO library_items;
CREATE INDEX idx_library_user ON library_items(user_id, item_id DESC);
COMMIT;
PRAGMA foreign_keys=ON;
