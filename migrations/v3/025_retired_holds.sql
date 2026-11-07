-- Keep a compact idempotency tombstone when released hold details are retained away.
CREATE TABLE IF NOT EXISTS retired_holds (
    submission_id INTEGER PRIMARY KEY REFERENCES submissions(submission_id),
    release_reason TEXT,
    settled INTEGER NOT NULL DEFAULT 0
);
