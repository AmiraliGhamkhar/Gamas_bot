# Migration Plan

## 1. Forward Migrations
- `037_study_packs.sql` (already applied): creates `study_packs`, `study_assets`, `reward_points_ledger`.
- `038_review_and_explore.sql` (next): creates `explore_questions`, `explore_impressions`, `user_topic_preferences`, and `idx_reward_points_unique_ref`.

## 2. Backfill Strategy
- Existing `library_items` and `quiz_sessions` remain intact and readable without requiring destructive migration.
- Optional non-destructive backfill script can create `study_packs` rows for historical `booklet`/`document` `library_items` if needed, preserving `library_items` as-is.

## 3. Rollback Strategy
- All new tables (`037`, `038`) are additive. Rolling back application code leaves existing bot tables untouched; new tables can be ignored or dropped only if explicitly requested.

## 4. Backup Requirements
- Before applying `038_review_and_explore.sql` in production, take a consistent SQLite online backup (`sqlite3 gamas_bot.db ".backup gamas_bot.bak"`).
