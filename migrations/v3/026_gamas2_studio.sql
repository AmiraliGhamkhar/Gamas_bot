-- Gamas 2.0: speaker turns, central submission artifacts, revision history,
-- and time-windowed learning events. Additive only: no applied migration and
-- no existing row is rewritten, so rollback is a table drop (see docs/DEPLOYMENT.md).

-- 1. Diarization -----------------------------------------------------------
-- Word-level turns are stored as JSON next to the transcript they belong to.
ALTER TABLE transcriptions ADD COLUMN speaker_turns_json TEXT;
ALTER TABLE transcriptions ADD COLUMN diarization_attempted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE transcriptions ADD COLUMN overlap_count INTEGER NOT NULL DEFAULT 0;

-- User-chosen names for anonymous speakers. A rename is a mapping from the
-- stable label ('0', '1') to a custom name; identities are never inferred.
CREATE TABLE IF NOT EXISTS speaker_names (
    submission_id INTEGER NOT NULL REFERENCES submissions(submission_id) ON DELETE CASCADE,
    speaker_label TEXT    NOT NULL,
    custom_name   TEXT    NOT NULL,
    named_at      TEXT    NOT NULL,
    PRIMARY KEY (submission_id, speaker_label)
);

-- Optional ground-truth turns kept for offline DER/JER evaluation only. Never
-- read by the delivery path; see scripts/evaluate_diarization.py.
CREATE TABLE IF NOT EXISTS diarization_reference_turns (
    submission_id INTEGER NOT NULL,
    ordinal       INTEGER NOT NULL,
    speaker_label TEXT    NOT NULL,
    start         REAL,
    end           REAL,
    PRIMARY KEY (submission_id, ordinal)
);

-- 2. The library as one central submission with independent artifacts -------
-- One row per (submission, kind). ``payload`` is a compact JSON pointer: for a
-- Telegram-hosted file it holds chat_id/message_id; for text artifacts it holds
-- the text itself. Nothing is fabricated: a button only appears for a row here.
CREATE TABLE IF NOT EXISTS submission_artifacts (
    artifact_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id  INTEGER NOT NULL REFERENCES submissions(submission_id) ON DELETE CASCADE,
    user_id        INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    kind           TEXT    NOT NULL CHECK (kind IN
                       ('booklet','transcript','txt','keypoints','quiz','flashcards')),
    reference      TEXT,
    payload        TEXT,
    available      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL,
    UNIQUE (submission_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_submission_artifacts_user
    ON submission_artifacts(user_id, submission_id DESC);

-- 3. Booklet Studio revision history --------------------------------------
-- Every save appends a revision before overwriting the cache, so an edit can
-- always be restored. Bounded by the application's own retention helper.
CREATE TABLE IF NOT EXISTS booklet_revisions (
    revision_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    content_text TEXT    NOT NULL,
    source       TEXT    NOT NULL DEFAULT 'mini_app',
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_booklet_revisions_item
    ON booklet_revisions(item_id, revision_id DESC);

-- 4. Weekly / monthly leaderboard -----------------------------------------
-- users.quiz_points remains the all-time authority. These events only add the
-- time windows; a deployment that has not applied this migration still has the
-- all-time board. Points are written in the same transaction as the answer.
CREATE TABLE IF NOT EXISTS quiz_point_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    points     INTEGER NOT NULL,
    session_id INTEGER,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quiz_point_events_time
    ON quiz_point_events(created_at, user_id);
