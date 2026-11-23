-- Phase 2: Review is defined by Study Pack ownership.
--
-- Purely additive: one nullable column, two indexes, and one idempotent
-- backfill that only fills ``quiz_sessions.pack_id`` where a *real* relationship
-- already exists. No question, answer, session or public Explore row is removed,
-- and no unrelated pack is ever assigned to fill a missing link.
--
-- A question reaches its source like this:
--   quiz_questions -> quiz_sessions (the generation run)
--                  -> quiz_sessions.pack_id (added in 039 by the pipeline)
--                  -> library_items.item_id  (the session's source row)
--                  -> study_packs.source_item_id / submission_id / document_id
--
-- Sessions that predate 039 have no pack link. The UPDATE below resolves them
-- through the library row they were generated from, which is a genuine foreign
-- relationship, not a guess by title or date. Anything still unresolved stays
-- NULL and is reported by Review as a legacy group.

-- Which review question was offered to Explore, so the Review screen can show
-- "pending/approved/rejected" and so a question cannot be published twice.
ALTER TABLE explore_questions ADD COLUMN source_question_id INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS idx_explore_source_question
    ON explore_questions(created_by, source_question_id)
    WHERE source_question_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_quiz_sessions_user_pack
    ON quiz_sessions(user_id, pack_id);

UPDATE quiz_sessions
SET pack_id = (
    SELECT p.pack_id
    FROM study_packs p
    JOIN library_items i
      ON i.item_id = quiz_sessions.item_id
     AND i.user_id = p.user_id
    WHERE p.user_id = quiz_sessions.user_id
      AND p.deleted_at IS NULL
      AND (
            p.source_item_id = i.item_id
         OR (p.submission_id IS NOT NULL AND p.submission_id = i.submission_id)
         OR (p.document_id IS NOT NULL AND p.document_id IN (
                SELECT d.id FROM documents d
                WHERE d.library_item_id = i.item_id AND d.user_id = p.user_id
            ))
      )
    ORDER BY p.pack_id DESC
    LIMIT 1
)
WHERE pack_id IS NULL;
