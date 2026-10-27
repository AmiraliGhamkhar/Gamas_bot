-- Admin UX consolidation, durable user identity, free-first-try gifts and
-- document ingestion (PDF / DOCX / PPTX).
--
-- Everything here is additive: no table is rebuilt and no existing row moves,
-- because the credit ledger, the payment state machine and the learning tables
-- must keep their exact shape (docs/BILLING.md). ADD COLUMN statements are
-- replayed safely by db.Database._without_applied_columns, so a crash midway
-- through this file is safe to retry (same idiom as 002/009/011).

-- Users: the @username, the second half of the display name and the last time
-- the account was seen. All three are informational; nothing authorises on
-- them. ``username`` is nullable because Telegram users may have none and the
-- column must distinguish "never known" from "known empty".
ALTER TABLE users ADD COLUMN username TEXT;
ALTER TABLE users ADD COLUMN last_name TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN last_seen_at TEXT;

-- Admin search by @username is a case-insensitive lookup; the index keeps that
-- cheap on a deployment with tens of thousands of users.
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

-- Free first try: one flashcard generation and one quiz generation per user,
-- tracked independently. A row exists only after the user's first claim, so
-- the table stays as small as the population that ever generated anything.
CREATE TABLE IF NOT EXISTS free_uses (
    user_id        INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    free_deck_used INTEGER NOT NULL DEFAULT 0 CHECK (free_deck_used IN (0, 1)),
    free_quiz_used INTEGER NOT NULL DEFAULT 0 CHECK (free_quiz_used IN (0, 1)),
    updated_at     TEXT    NOT NULL
);

-- Uploaded documents are a learning source in their own right: the extracted
-- text and its chunks are cached so generation never re-downloads the file.
-- ``sha256`` plus the per-user unique index make a re-upload of the same file
-- free (the stored extraction is reused instead of a second row).
CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(telegram_id),
    file_msg_ref TEXT    NOT NULL DEFAULT '',
    title        TEXT    NOT NULL DEFAULT '',
    source_type  TEXT    NOT NULL CHECK (source_type IN ('pdf', 'docx', 'pptx')),
    page_count   INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    chunks_json  TEXT    NOT NULL DEFAULT '[]',
    -- The mirrored library item: quiz_sessions/flashcard_decks reference
    -- library_items, so a document needs a row there to be a learning source.
    library_item_id INTEGER REFERENCES library_items(item_id) ON DELETE SET NULL,
    created_at   TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_user_sha
    ON documents(user_id, sha256);
CREATE INDEX IF NOT EXISTS idx_documents_user
    ON documents(user_id, id DESC);

-- Source anchoring: every generated item records the page/slide/section it was
-- built from. Existing rows keep '' which the screens render as "بدون منبع".
ALTER TABLE flashcards ADD COLUMN source TEXT NOT NULL DEFAULT '';
ALTER TABLE quiz_questions ADD COLUMN source TEXT NOT NULL DEFAULT '';
