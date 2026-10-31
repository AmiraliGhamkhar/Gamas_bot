-- Persist Free Office exports as Telegram message references in the existing
-- owner-scoped library; the files themselves remain in Telegram.
-- SQLite cannot alter a CHECK constraint in place, so preserve every column,
-- item id, text cache and child reference while widening the allowed kinds.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO users(telegram_id, joined_at)
    SELECT user_id, MIN(created_at) FROM library_items GROUP BY user_id;
DROP TABLE IF EXISTS library_items_rebuild;
CREATE TABLE library_items_rebuild (
    item_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(telegram_id),
    submission_id INTEGER,
    kind          TEXT NOT NULL CHECK (kind IN ('booklet','transcript','office')),
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
CREATE INDEX IF NOT EXISTS idx_library_user ON library_items(user_id, item_id DESC);
COMMIT;
PRAGMA foreign_keys=ON;
