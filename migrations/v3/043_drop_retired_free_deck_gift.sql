-- The flashcard feature is gone, and with it the second free-first-try gift it
-- was billed against. `free_uses` kept `free_deck_used` and `deck_claim` after
-- the flashcards were removed, so every read/write named a column no user
-- surface can reach any more. Drop the retired columns and keep the quiz gift.
--
-- No data is lost: `free_deck_used`/`deck_claim` only ever described a retired
-- feature, and `quiz` state stays in place. SQLite cannot drop a column that is
-- referenced by a table CHECK constraint (the original table declared
-- `free_deck_used ... CHECK (...)`), so the table is rebuilt the same way
-- migration 014 rebuilt `library_items`: foreign keys off, one transaction,
-- ids and rows preserved, then the constraint reinstalled.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
CREATE TABLE free_uses_rebuild (
    user_id        INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    free_quiz_used INTEGER NOT NULL DEFAULT 0 CHECK (free_quiz_used IN (0, 1)),
    quiz_claim     TEXT,
    updated_at     TEXT    NOT NULL
);
INSERT INTO free_uses_rebuild (user_id, free_quiz_used, quiz_claim, updated_at)
    SELECT user_id, free_quiz_used, quiz_claim, updated_at FROM free_uses;
DROP TABLE free_uses;
ALTER TABLE free_uses_rebuild RENAME TO free_uses;
COMMIT;
PRAGMA foreign_keys=ON;
