-- Persisted student-facing booklet files.  This is deliberately a new table:
-- existing submission_artifacts rows remain untouched and retain their compact
-- text/Telegram pointers, while PDF and DOCX bytes survive job workdir cleanup.
-- The table is owner-scoped at every read in application code.
CREATE TABLE IF NOT EXISTS submission_binary_artifacts (
    binary_artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id      INTEGER NOT NULL REFERENCES submissions(submission_id) ON DELETE CASCADE,
    user_id            INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    kind               TEXT    NOT NULL CHECK (kind IN ('booklet', 'docx')),
    filename           TEXT    NOT NULL,
    content_type       TEXT    NOT NULL,
    content            BLOB    NOT NULL,
    sha256             TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    UNIQUE (submission_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_submission_binary_artifacts_user
    ON submission_binary_artifacts(user_id, submission_id DESC);
