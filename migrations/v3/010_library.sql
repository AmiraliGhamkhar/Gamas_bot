-- Telegram-hosted artifacts are the user's durable library: we store only the
-- source chat/message references, never a second copy of the generated files.
CREATE TABLE IF NOT EXISTS library_items (
    item_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    submission_id INTEGER,
    kind          TEXT    NOT NULL CHECK (kind IN ('booklet','transcript')),
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    title         TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    unavailable   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_library_user ON library_items(user_id, item_id DESC);
