-- Interactive HTML review sheets are persisted in the same owner-scoped binary
-- artifact table as the PDF and DOCX booklets (no new storage system).
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt once:
--   1. create the widened table with every column migrations 027, 028 and 030
--      added (identical names, order, defaults, checks and UNIQUE key);
--   2. copy every existing row with its explicit binary_artifact_id, so ids,
--      bytes, SHA256 values, owners, revision_id and delivery counters survive;
--   3. carry the AUTOINCREMENT high-water mark over, so ids are never reused;
--   4. drop the old table and rename the new one into place;
--   5. recreate the two indexes that the old table owned.
--
-- The runner executes this file with executescript(), outside any transaction,
-- so foreign keys can be switched off for the rebuild and back on afterwards.
-- Nothing references submission_binary_artifacts, so no child row needs
-- rewriting. The whole rebuild is one transaction: a failure leaves the
-- previous table untouched.
PRAGMA foreign_keys = OFF;

BEGIN IMMEDIATE;

CREATE TABLE submission_binary_artifacts_next (
    binary_artifact_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id            INTEGER NOT NULL REFERENCES submissions(submission_id) ON DELETE CASCADE,
    user_id                  INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    kind                     TEXT    NOT NULL CHECK (kind IN ('booklet', 'docx', 'review_html')),
    filename                 TEXT    NOT NULL,
    content_type             TEXT    NOT NULL,
    content                  BLOB    NOT NULL,
    sha256                   TEXT    NOT NULL,
    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL,
    revision_id              INTEGER,
    delivery_attempts        INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    delivery_successes       INTEGER NOT NULL DEFAULT 0 CHECK (delivery_successes >= 0),
    delivery_failures        INTEGER NOT NULL DEFAULT 0 CHECK (delivery_failures >= 0),
    last_delivery_attempt_at TEXT,
    last_delivery_error      TEXT,
    UNIQUE (submission_id, kind)
);

INSERT INTO submission_binary_artifacts_next (
    binary_artifact_id, submission_id, user_id, kind, filename, content_type,
    content, sha256, created_at, updated_at, revision_id,
    delivery_attempts, delivery_successes, delivery_failures,
    last_delivery_attempt_at, last_delivery_error
)
SELECT
    binary_artifact_id, submission_id, user_id, kind, filename, content_type,
    content, sha256, created_at, updated_at, revision_id,
    delivery_attempts, delivery_successes, delivery_failures,
    last_delivery_attempt_at, last_delivery_error
FROM submission_binary_artifacts
ORDER BY binary_artifact_id;

INSERT INTO sqlite_sequence (name, seq)
SELECT 'submission_binary_artifacts_next', 0
WHERE NOT EXISTS (
    SELECT 1 FROM sqlite_sequence WHERE name = 'submission_binary_artifacts_next'
);

UPDATE sqlite_sequence
SET seq = MAX(
    seq,
    COALESCE(
        (SELECT seq FROM sqlite_sequence WHERE name = 'submission_binary_artifacts'),
        0
    )
)
WHERE name = 'submission_binary_artifacts_next';

DROP TABLE submission_binary_artifacts;

ALTER TABLE submission_binary_artifacts_next RENAME TO submission_binary_artifacts;

CREATE INDEX IF NOT EXISTS idx_submission_binary_artifacts_user
    ON submission_binary_artifacts(user_id, submission_id DESC);
CREATE INDEX IF NOT EXISTS idx_submission_binary_artifacts_revision
    ON submission_binary_artifacts(revision_id);

COMMIT;

PRAGMA foreign_keys = ON;
