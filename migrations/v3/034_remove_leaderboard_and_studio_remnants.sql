-- Remove the unused Mini App revision history table and leaderboard
-- point-event table/index. Historical migrations 012/020/026/028 stay
-- untouched so existing databases upgrade cleanly without table rebuilds.
DROP TABLE IF EXISTS booklet_revisions;
DROP TABLE IF EXISTS quiz_point_events;
DROP INDEX IF EXISTS idx_users_quiz_points;
