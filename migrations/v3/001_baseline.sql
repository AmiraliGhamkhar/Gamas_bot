-- Gamas Bot v3 baseline schema. Fresh database; v2 data is imported with
-- scripts/import_v2.py when needed.

CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    joined_at   TEXT    NOT NULL,
    banned      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(telegram_id),
    kind             TEXT    NOT NULL,           -- audio | video | presentation | text
    filename         TEXT,
    status           TEXT    NOT NULL,           -- pending | processing | done | failed | rejected
    error            TEXT,
    duration_seconds REAL,
    created_at       TEXT    NOT NULL,
    finished_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id, submission_id);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);

CREATE TABLE IF NOT EXISTS transcriptions (
    submission_id INTEGER PRIMARY KEY REFERENCES submissions(submission_id),
    engine        TEXT NOT NULL,
    text          TEXT NOT NULL,
    notes_json    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    code           TEXT PRIMARY KEY,
    hours          REAL NOT NULL,
    price          INTEGER NOT NULL,             -- toman
    validity_days  INTEGER,                      -- NULL = never expires
    is_custom      INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(telegram_id),
    plan_code    TEXT    NOT NULL REFERENCES plans(code),
    status       TEXT    NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    receipt_path TEXT,
    created_at   TEXT    NOT NULL,
    reviewed_at  TEXT,
    reviewed_by  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);

-- Append-only accounting: kind 'credit' adds seconds (with optional expiry),
-- kind 'debit' consumes them. Balance is always derived, never stored.
CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(telegram_id),
    seconds    INTEGER NOT NULL,
    kind       TEXT    NOT NULL CHECK (kind IN ('credit', 'debit')),
    note       TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, id);

-- Seconds reserved by a running job. Settled (charged) or released exactly once.
CREATE TABLE IF NOT EXISTS holds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL UNIQUE REFERENCES submissions(submission_id),
    user_id         INTEGER NOT NULL REFERENCES users(telegram_id),
    seconds         INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    released_at     TEXT,
    settled         INTEGER NOT NULL DEFAULT 0,
    release_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_holds_user ON holds(user_id, released_at);

CREATE TABLE IF NOT EXISTS audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id      INTEGER NOT NULL,
    action        TEXT NOT NULL,
    detail        TEXT,
    subject_user  INTEGER,
    created_at    TEXT NOT NULL
);
