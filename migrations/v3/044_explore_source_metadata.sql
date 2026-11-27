-- Phase 5: academic source metadata for Explore questions.
--
-- Every curated Explore question must carry the real source its fact comes
-- from (textbook, institutional fact sheet, or reputable reference), so the
-- feed can show a "منبع علمی" panel instead of unsourced trivia. The columns
-- are nullable on purpose: questions a user shared from Review (created_by
-- NOT NULL) and rows seeded before this migration have no source yet, and
-- they must stay valid rows — the feed marks them "awaiting verification"
-- instead of pretending they are verified. The seeder backfills the curated
-- bank (seed_key rows) from gamas_bot.explore_sources, filling only NULLs.
--
-- Purely additive: ten nullable columns. No row is rewritten, no question is
-- deleted, and impressions/rewards/history are untouched. The migration
-- runner skips ADD COLUMN statements whose column already exists, so this
-- file is idempotent on a database that already has the columns.

ALTER TABLE explore_questions ADD COLUMN source_type TEXT;
ALTER TABLE explore_questions ADD COLUMN source_title TEXT;
ALTER TABLE explore_questions ADD COLUMN source_author TEXT;
ALTER TABLE explore_questions ADD COLUMN source_institution TEXT;
ALTER TABLE explore_questions ADD COLUMN source_url TEXT;
ALTER TABLE explore_questions ADD COLUMN source_year TEXT;
ALTER TABLE explore_questions ADD COLUMN source_page_or_section TEXT;
ALTER TABLE explore_questions ADD COLUMN evidence_level TEXT;
ALTER TABLE explore_questions ADD COLUMN verified_at TEXT;
ALTER TABLE explore_questions ADD COLUMN updated_at TEXT;
