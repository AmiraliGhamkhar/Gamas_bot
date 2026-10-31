-- OCR (opt-in) reads an uploaded photo or scan as a one-page document, so the
-- documents table must accept the `image` source type. SQLite cannot alter a
-- CHECK constraint in place: rebuild the table with the same columns, ids and
-- child links, exactly like the library rebuilds in migrations 014 and 016.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
DROP TABLE IF EXISTS documents_rebuild;
CREATE TABLE documents_rebuild (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(telegram_id),
    file_msg_ref TEXT    NOT NULL DEFAULT '',
    title        TEXT    NOT NULL DEFAULT '',
    source_type  TEXT    NOT NULL CHECK (source_type IN ('pdf', 'docx', 'pptx', 'image')),
    page_count   INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    chunks_json  TEXT    NOT NULL DEFAULT '[]',
    -- The mirrored library item: quiz_sessions/flashcard_decks reference
    -- library_items, so a document needs a row there to be a learning source.
    library_item_id INTEGER REFERENCES library_items(item_id) ON DELETE SET NULL,
    created_at   TEXT    NOT NULL
);
INSERT INTO documents_rebuild
    (id, user_id, file_msg_ref, title, source_type, page_count, sha256, text,
     chunks_json, library_item_id, created_at)
    SELECT id, user_id, file_msg_ref, title, source_type, page_count, sha256,
           text, chunks_json, library_item_id, created_at FROM documents;
DROP TABLE documents;
ALTER TABLE documents_rebuild RENAME TO documents;
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_user_sha
    ON documents(user_id, sha256);
CREATE INDEX IF NOT EXISTS idx_documents_user
    ON documents(user_id, id DESC);
COMMIT;
PRAGMA foreign_keys=ON;
