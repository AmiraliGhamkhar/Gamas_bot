PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_seen TEXT NOT NULL,
    is_banned INTEGER NOT NULL DEFAULT 0 CHECK (is_banned IN (0, 1))
);

CREATE TABLE IF NOT EXISTS audio_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    duration REAL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    original_filename TEXT,
    mime_type TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_submissions_user_id ON audio_submissions(user_id);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON audio_submissions(status);
CREATE INDEX IF NOT EXISTS idx_submissions_received_at ON audio_submissions(received_at);

CREATE TABLE IF NOT EXISTS transcriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL UNIQUE REFERENCES audio_submissions(id) ON DELETE CASCADE,
    stt_engine TEXT NOT NULL,
    raw_transcript TEXT NOT NULL,
    structured_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    recipient_count INTEGER NOT NULL DEFAULT 0
);
