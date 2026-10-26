-- Learning loop, part 1: the two columns everything else is built on.
--
-- ``users.first_name`` exists because the leaderboard must show a human label
-- and the baseline schema only ever stored a ``telegram_id``. It is captured
-- from the Telegram sender once per message dispatch and bounded by code, so
-- this is a display convenience, never an identity.
--
-- ``users.quiz_points`` is the all-time total the leaderboard sorts by. It is
-- denormalised on purpose: ``quiz_answers`` remains the audit trail, but a
-- board query must not aggregate every answer ever recorded.
--
-- ``library_items.content_text`` retains the generated artifact's plain text
-- so a quiz or flashcard deck can be produced later without re-running STT.
-- It is nullable: rows delivered before this migration keep NULL and are shown
-- as legacy items instead of being re-derived.
--
-- The runner strips an ``ADD COLUMN`` whose column already exists, so this
-- file is safe to retry after a crash midway through the script.
ALTER TABLE users ADD COLUMN first_name TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN quiz_points INTEGER NOT NULL DEFAULT 0;
ALTER TABLE library_items ADD COLUMN content_text TEXT;
