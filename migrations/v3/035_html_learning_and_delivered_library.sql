-- Delivered-only categorized library + SQLite HTML blob persistence for
-- interactive quizzes, interactive flashcards, booklets, and bot-delivered files.
--
-- SQLite cannot alter a CHECK constraint in place, so preserve every existing
-- column, item_id, text cache, structured_json, and child reference while:
-- 1. Widening library_items.kind to include 'quiz' and 'flashcards'.
-- 2. Adding library_items.delivered (1 = actually sent to the user by the bot,
--    0 = internal source mirror such as an uploaded document or undownloaded
--    virtual booklet record kept solely for quiz/flashcard generation).
-- 3. Adding library_items.artifact_kind so repeated downloads of the same
--    submission artifact update in place instead of duplicating library rows.
-- 4. Adding library_file_blobs to persist generated HTML/file bytes in SQLite
--    so library re-downloads remain reliable even if Telegram messages expire.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS library_file_blobs (
    item_id      INTEGER PRIMARY KEY REFERENCES library_items(item_id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    filename     TEXT    NOT NULL,
    content_type TEXT    NOT NULL,
    content      BLOB    NOT NULL,
    sha256       TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

INSERT OR IGNORE INTO users(telegram_id, joined_at)
    SELECT user_id, MIN(created_at) FROM library_items GROUP BY user_id;

DROP TABLE IF EXISTS library_items_rebuild;
CREATE TABLE library_items_rebuild (
    item_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(telegram_id),
    submission_id   INTEGER,
    kind            TEXT NOT NULL CHECK (kind IN ('booklet','transcript','office','quiz','flashcards')),
    chat_id         INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    unavailable     INTEGER NOT NULL DEFAULT 0,
    content_text    TEXT,
    structured_json TEXT,
    delivered       INTEGER NOT NULL DEFAULT 1 CHECK (delivered IN (0, 1)),
    artifact_kind   TEXT NOT NULL DEFAULT ''
);

INSERT INTO library_items_rebuild
    (item_id, user_id, submission_id, kind, chat_id, message_id, title,
     created_at, unavailable, content_text, structured_json, delivered, artifact_kind)
    SELECT item_id,
           user_id,
           submission_id,
           kind,
           chat_id,
           message_id,
           title,
           created_at,
           unavailable,
           content_text,
           structured_json,
           CASE
               WHEN EXISTS (
                   SELECT 1 FROM library_file_blobs b WHERE b.item_id = library_items.item_id
               ) THEN 1
               WHEN chat_id = 0 AND message_id = 0 THEN 0
               WHEN item_id IN (
                   SELECT library_item_id FROM documents WHERE library_item_id IS NOT NULL
               ) THEN 0
               ELSE 1
           END,
           ''
      FROM library_items;

DROP TABLE library_items;
ALTER TABLE library_items_rebuild RENAME TO library_items;

CREATE INDEX IF NOT EXISTS idx_library_user
    ON library_items(user_id, item_id DESC);
CREATE INDEX IF NOT EXISTS idx_library_user_delivered_kind
    ON library_items(user_id, delivered, kind, item_id DESC);
CREATE INDEX IF NOT EXISTS idx_library_submission_artifact
    ON library_items(user_id, submission_id, artifact_kind)
    WHERE submission_id IS NOT NULL AND delivered = 1;
CREATE INDEX IF NOT EXISTS idx_library_file_blobs_user
    ON library_file_blobs(user_id, item_id DESC);

-- Close any legacy in-chat active quiz sessions from before interactive HTML
-- delivery so upgraded users are never blocked by stale sessions.
UPDATE quiz_sessions SET status = 'done' WHERE status = 'active';

COMMIT;
PRAGMA foreign_keys=ON;
