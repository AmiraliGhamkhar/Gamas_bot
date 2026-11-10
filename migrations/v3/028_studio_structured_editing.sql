-- Gamas Studio 3.0: structured booklet editing and speaker management.
-- Additive only: no applied migration and no existing row is rewritten, so
-- rollback is an ALTER ... DROP COLUMN (SQLite >= 3.35) or a table rebuild
-- plus deleting this row from schema_migrations.

-- 1. Speaker management ----------------------------------------------------
-- An optional, user-authored role or description next to the chosen name.
-- The role is free text the owner typed (e.g. "مدرس", "دانشجو"); it is never
-- inferred from the voice. Empty string keeps the previous behaviour exactly.
ALTER TABLE speaker_names ADD COLUMN role TEXT NOT NULL DEFAULT '';

-- 2. Structured booklet editing ---------------------------------------------
-- The editable structured cache of a booklet library item. transcriptions.notes_json
-- stays the immutable pipeline output (the original generated booklet and the
-- key-points source); library_items.structured_json is the copy the Studio edits.
-- NULL means "booklet created before migration 028" and keeps the legacy
-- plain-text editor working.
ALTER TABLE library_items ADD COLUMN structured_json TEXT;

-- Every structured save stores its own copy of the canonical payload, so any
-- revision can be inspected or restored. Text-only rows (created by the legacy
-- plain-text editor) keep structured_json NULL.
ALTER TABLE booklet_revisions ADD COLUMN structured_json TEXT;

-- 3. Artifact-to-revision association ---------------------------------------
-- Which booklet revision the persisted PDF/DOCX bytes were rendered from.
-- NULL means "rendered by the pipeline at completion" (the original files).
-- No foreign key: booklet_revisions predates this column without one, and the
-- application clears the pointer when retention prunes the referenced revision.
ALTER TABLE submission_binary_artifacts ADD COLUMN revision_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_submission_binary_artifacts_revision
    ON submission_binary_artifacts(revision_id);
