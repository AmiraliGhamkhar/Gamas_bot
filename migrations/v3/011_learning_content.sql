-- Learning-loop prerequisites: a display name for the leaderboard and the
-- archived plain text of each delivered artifact, which the quiz and AI
-- flashcard generators are grounded in.
--
-- Both columns are nullable/defaulted: rows written before the upgrade keep
-- '' / NULL, which the screens treat as "unnamed user" / "legacy item without
-- archived text" rather than failing.
--
-- ADD COLUMN is replayed safely by db.Database._without_applied_columns, so a
-- crash midway through this file is safe to retry (same idiom as 002/009).
ALTER TABLE users ADD COLUMN first_name TEXT NOT NULL DEFAULT '';
ALTER TABLE library_items ADD COLUMN content_text TEXT;
